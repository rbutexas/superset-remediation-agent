"""The reconciliation loop.

Devin has no outbound webhook — it receives events but never calls back when a
session finishes. So completion is discovered by polling `GET /sessions` filtered
by the campaign tag and diffing against what we already know.

That constraint turns out to suit the job. There are no deliveries to miss, and a
collector that has been down for an hour catches up on its next tick rather than
losing that hour permanently. Reads are unmetered, so the cost is wall-clock only.

The loop also carries the pipeline forward: when a triage session returns
`remediate`, the collector applies the `agent:remediate` label, which fires the
second automation. Nothing else in the system watches for that transition.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .config import CAMPAIGN_TAG, Config
from .devin import DevinClient, to_record
from .dispatch import Dispatcher
from .models import Finding, SessionRecord, Stage
from .store import Store

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Tick:
    """What one pass of the loop observed. Returned so a caller can decide
    whether to keep going, rather than the loop deciding for itself."""

    seen: int = 0
    transitions: list[tuple[str, str]] = field(default_factory=list)
    completed: list[SessionRecord] = field(default_factory=list)
    stalled: list[SessionRecord] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not (self.transitions or self.promoted or self.errors)

    def summary(self) -> str:
        bits = [f"{self.seen} session(s)"]
        for label, items in (("transitions", self.transitions),
                             ("completed", self.completed),
                             ("stalled", self.stalled),
                             ("promoted", self.promoted),
                             ("errors", self.errors)):
            if items:
                bits.append(f"{len(items)} {label}")
        return ", ".join(bits)


class Collector:
    def __init__(self, cfg: Config, devin: DevinClient, store: Store,
                 dispatcher: Dispatcher | None = None,
                 findings: dict[str, Finding] | None = None,
                 terminate_finished: bool = True) -> None:
        self.cfg = cfg
        self.devin = devin
        self.store = store
        self.dispatcher = dispatcher
        self.terminate_finished = terminate_finished
        self._terminated: set[str] = set()
        # Findings are needed to build a remediation prompt when triage promotes
        # one. Without them the collector can still observe, just not advance.
        self.findings = findings or {}
        self._promoted: set[str] = set()

    # ------------------------------------------------------------------ tick

    def tick(self) -> Tick:
        """One reconciliation pass. Safe to call repeatedly; does not block."""
        result = Tick()

        for payload in self.devin.list_sessions(tags=[CAMPAIGN_TAG]):
            try:
                record = to_record(payload)
            except Exception as exc:                  # noqa: BLE001
                log.exception("could not read session payload")
                result.errors.append(f"unreadable payload: {exc}")
                continue

            result.seen += 1
            for transition in self.store.record_session(record):
                result.transitions.append((record.session_id, transition))
                log.info("%s  %s", record.session_id[:12], transition)

            if record.is_stalled:
                result.stalled.append(record)
                log.warning("STALLED %s — waiting on a person, no output yet: %s",
                            record.session_id[:12], record.url)
                continue

            if not record.is_terminal:
                continue

            result.completed.append(record)
            self._advance(record, result)
            self._teardown(record, result)

        return result

    # -------------------------------------------------------------- teardown

    def _teardown(self, record: SessionRecord, result: Tick) -> None:
        """End a session once its output is safely recorded.

        Only ever called for a session that answered — a stalled one is left
        alone, because a human may still want to reply to it. The local set
        avoids re-issuing the call on every subsequent poll.
        """
        if not self.terminate_finished or not record.answered:
            return
        if record.session_id in self._terminated:
            return
        if record.status in ("exit", "error"):
            self._terminated.add(record.session_id)
            return

        try:
            self.devin.terminate_session(record.session_id)
        except Exception as exc:                      # noqa: BLE001
            log.warning("could not terminate %s: %s", record.session_id[:12], exc)
            result.errors.append(f"terminate {record.session_id[:12]}: {exc}")
            return

        self._terminated.add(record.session_id)
        self.store.log("session.terminated", record.session_id,
                       "output recorded; session ended")

    # --------------------------------------------------------------- advance

    def _advance(self, record: SessionRecord, result: Tick) -> None:
        """Carry a completed triage session forward into remediation."""
        if record.stage is not Stage.TRIAGE or record.triage_decision is None:
            return
        if not record.finding_key or not record.issue_number:
            return
        if record.session_id in self._promoted:
            return
        if self.dispatcher is None:
            return

        finding = self.findings.get(record.finding_key)
        if finding is None:
            log.warning("triage for %s decided %s but that finding is not loaded; "
                        "cannot act on it", record.finding_key,
                        record.triage_decision.value)
            return

        # Do not re-act on a decision already applied in an earlier run. The
        # store is the memory here; the in-process set only avoids duplicate work
        # within a single run.
        prior = self.store.latest_session(record.finding_key, Stage.REMEDIATION.value)
        if prior and record.triage_decision.dispatches_work:
            self._promoted.add(record.session_id)
            return

        try:
            outcome = self.dispatcher.act_on_triage(
                finding, record.issue_number, record.triage_decision,
                record.structured_output or {},
            )
        except Exception as exc:                      # noqa: BLE001
            log.exception("could not act on triage for %s", record.finding_key)
            result.errors.append(f"{record.finding_key}: {exc}")
            return

        self._promoted.add(record.session_id)
        self.store.log("triage.applied", record.finding_key, outcome.action)
        result.promoted.append(f"{record.finding_key}: {outcome.action}")
        log.info("%s -> %s", record.finding_key, outcome.action)

    # ------------------------------------------------------------------ loop

    def run(self, *, until_idle: bool = False, max_seconds: int | None = None) -> Tick:
        """Poll until interrupted, or until nothing is left in flight.

        `until_idle` is what a rehearsal or a CI run wants: work the queue, then
        stop. Without it this runs as a daemon.
        """
        started = time.time()
        last = Tick()

        while True:
            last = self.tick()
            log.info("tick: %s", last.summary())

            if max_seconds is not None and time.time() - started > max_seconds:
                log.warning("collector timed out after %ds", max_seconds)
                return last

            if until_idle and self._idle(last):
                log.info("nothing in flight; stopping")
                return last

            time.sleep(self.cfg.poll_interval_seconds)

    def _idle(self, tick: Tick) -> bool:
        """Idle means: nothing running, and nothing waiting to be promoted.

        A stalled session counts as idle on purpose. It is blocked on a human,
        and spinning a poll loop will not unblock it — that is what the stalled
        list in the report is for.
        """
        if tick.promoted:
            return False
        in_flight = [
            row for row in self.store.sessions()
            if row["completed_at"] is None
            and row["status_detail"] not in ("waiting_for_user", "waiting_for_approval")
        ]
        return not in_flight
