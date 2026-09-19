"""Reporting.

The question this has to answer is "if I were an engineering leader, how would I
know this is working?" — and the honest answer is not a count of fixes.

A pipeline measured on fixes will produce fixes, including for findings that
should have been left alone. So the headline metric here is the **outcome mix**:
how many findings reached a verdict, and what the verdicts were. A dismissal with
evidence resolves a finding just as a pull request does, and it is the cheaper
and scarcer outcome.

Four things are surfaced that a naive dashboard would omit:

  * **Unresolved findings** — filed but with no verdict. The queue's true backlog.
  * **Stalled sessions** — billing, blocked on a person, nobody told.
  * **Heuristic agreement** — how often the cheap rule matched the agent. Where
    they disagree is where the judgment actually happened.
  * **Unverifiable claims** — where a remediation admitted it could not prove
    something. Hiding these would make the dashboard less trustworthy, not more.
"""

from __future__ import annotations

import dataclasses
import html
import json
import sqlite3
import time
from collections import Counter
from typing import Any

from .models import RemediationOutcome, Stage, TriageDecision
from .store import Store

BAR_WIDTH = 28


@dataclasses.dataclass(slots=True)
class FindingRow:
    key: str
    title: str
    detector: str
    severity: str
    issue_number: int | None
    issue_url: str | None
    scanner_hint: str | None
    triage: str | None
    outcome: str | None
    acus: float
    prs: list[str]
    first_seen: int
    verdict_at: int | None
    stalled: bool
    abandoned: bool
    unverifiable: str | None

    @property
    def resolved(self) -> bool:
        """A finding is resolved when it has an answer — not only when it has a fix."""
        if self.outcome:
            return RemediationOutcome(self.outcome).is_success
        if self.triage:
            return TriageDecision(self.triage).resolves_finding
        return False

    @property
    def time_to_verdict(self) -> int | None:
        return (self.verdict_at - self.first_seen) if self.verdict_at else None

    @property
    def status(self) -> str:
        if self.stalled:
            return "STALLED"
        if self.abandoned and not (self.triage or self.outcome):
            return "ABANDONED"
        if self.outcome:
            return self.outcome
        if self.triage:
            return self.triage if not TriageDecision(self.triage).dispatches_work \
                else "remediating"
        if self.issue_number:
            return "awaiting triage"
        return "detected"


@dataclasses.dataclass(slots=True)
class Report:
    generated_at: int
    findings: list[FindingRow]
    total_acus: float
    sessions: int
    sessions_complete: int
    stalled_sessions: list[dict[str, Any]]
    acus_are_reliable: bool
    abandoned_sessions: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    """Stopped without ever answering. Would otherwise disappear: a stalled
    session is only `waiting_for_user` for about thirty minutes before Devin
    suspends it for inactivity, at which point it stops looking stalled and
    starts looking complete."""

    active_sessions: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    """Sessions in flight right now. Only meaningful while the collector is
    running, which is why the live view exists at all — a static render of this
    is always empty by the time anyone reads it."""

    # ------------------------------------------------------------ derived

    @property
    def resolved(self) -> list[FindingRow]:
        return [f for f in self.findings if f.resolved]

    @property
    def unresolved(self) -> list[FindingRow]:
        return [f for f in self.findings if not f.resolved]

    @property
    def outcome_mix(self) -> Counter:
        return Counter(f.status for f in self.findings)

    @property
    def prs(self) -> list[str]:
        return [pr for f in self.findings for pr in f.prs]

    @property
    def heuristic_agreement(self) -> tuple[int, int]:
        """(agreed, compared). Disagreement is where the judgment happened."""
        compared = [f for f in self.findings if f.scanner_hint and f.triage]
        agreed = [f for f in compared if f.scanner_hint == f.triage]
        return len(agreed), len(compared)

    @property
    def cost_per_resolution(self) -> float | None:
        if not self.resolved or not self.acus_are_reliable:
            return None
        return self.total_acus / len(self.resolved)

    @property
    def admitted_gaps(self) -> list[FindingRow]:
        return [f for f in self.findings if f.unverifiable]


# ---------------------------------------------------------------- building

def build(store: Store) -> Report:
    rows: list[FindingRow] = []
    stalled_sessions: list[dict[str, Any]] = []

    for finding in store.findings():
        sessions = store.sessions_for(finding["key"])
        triage = next((s for s in sessions if s["stage"] == Stage.TRIAGE.value), None)
        remediation = next(
            (s for s in sessions if s["stage"] == Stage.REMEDIATION.value), None)

        acus = sum(float(s["acus"] or 0) for s in sessions)
        prs = [pr for s in sessions for pr in Store.prs(s)]

        verdict_at = None
        for candidate in (remediation, triage):
            if candidate and candidate["completed_at"]:
                verdict_at = candidate["completed_at"]
                break

        unverifiable = None
        if remediation:
            verification = Store.output(remediation).get("verification") or {}
            unverifiable = verification.get("unverifiable") or None

        rows.append(FindingRow(
            key=finding["key"],
            title=finding["title"],
            detector=finding["detector"],
            severity=finding["severity"],
            issue_number=finding["issue_number"],
            issue_url=finding["issue_url"],
            scanner_hint=finding["scanner_hint"],
            triage=triage["triage_decision"] if triage else None,
            outcome=remediation["remediation_outcome"] if remediation else None,
            acus=acus,
            prs=prs,
            first_seen=finding["first_seen"],
            verdict_at=verdict_at,
            stalled=any(_is_stalled(s) for s in sessions),
            abandoned=any(_is_abandoned(s) for s in sessions),
            unverifiable=unverifiable,
        ))

    active_sessions: list[dict[str, Any]] = []
    abandoned_sessions: list[dict[str, Any]] = []
    for session in store.sessions():
        if _is_abandoned(session):
            abandoned_sessions.append({
                "session_id": session["session_id"],
                "url": session["url"],
                "finding": session["finding_key"],
                "detail": session["status_detail"] or session["status"],
                "ended": session["completed_at"],
            })
        elif _is_stalled(session):
            stalled_sessions.append({
                "session_id": session["session_id"],
                "url": session["url"],
                "finding": session["finding_key"],
                "waiting_since": session["last_seen"],
            })
        elif session["completed_at"] is None:
            active_sessions.append({
                "session_id": session["session_id"],
                "url": session["url"],
                "finding": session["finding_key"],
                "stage": session["stage"],
                "status": session["status"],
                "detail": session["status_detail"],
                "acus": float(session["acus"] or 0),
                "started": session["first_seen"],
            })

    counts = store.counts()
    total = float(counts["total_acus"])

    return Report(
        generated_at=int(time.time()),
        findings=rows,
        total_acus=total,
        sessions=counts["sessions"],
        sessions_complete=counts["sessions_complete"],
        stalled_sessions=stalled_sessions,
        abandoned_sessions=abandoned_sessions,
        active_sessions=active_sessions,
        # Devin's consumption endpoints aggregate on a delay; a zero here means
        # "not reported yet", not "free". Saying so is better than printing a
        # confident nought.
        acus_are_reliable=total > 0,
    )


def _is_stalled(session: sqlite3.Row) -> bool:
    """Blocked on a person right now, with nothing produced yet."""
    return (session["status_detail"] in ("waiting_for_user", "waiting_for_approval")
            and not session["structured_output"])


def _is_abandoned(session: sqlite3.Row) -> bool:
    """Stopped without answering — out of budget, errored, or idled out.

    The store sets `completed_at` from `SessionRecord.is_terminal`, so a row
    with a completion timestamp and no structured output is one that ended
    having produced nothing.
    """
    return bool(session["completed_at"]) and not session["structured_output"]


# ---------------------------------------------------------------- rendering

def _bar(count: int, total: int) -> str:
    if not total:
        return ""
    filled = round(BAR_WIDTH * count / total)
    return "█" * filled + "·" * (BAR_WIDTH - filled)


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def render_text(report: Report) -> str:
    rule = "─" * 78
    out: list[str] = [
        rule,
        " REMEDIATION PIPELINE",
        f" {time.strftime('%Y-%m-%d %H:%M', time.localtime(report.generated_at))}",
        rule,
        "",
    ]

    total = len(report.findings)
    out += [
        f"  {total} finding(s) detected · {len(report.resolved)} resolved · "
        f"{len(report.unresolved)} outstanding",
        "",
    ]

    # --- outcome mix: the headline, deliberately not a fix count ---
    out += ["  OUTCOME MIX", ""]
    for status, count in report.outcome_mix.most_common():
        out.append(f"    {status:<26} {count:>3}  {_bar(count, total)}")
    out.append("")

    # --- cost ---
    out += ["  COST", ""]
    if report.acus_are_reliable:
        out.append(f"    total                      {report.total_acus:>6.2f} ACU")
        per = report.cost_per_resolution
        if per is not None:
            out.append(f"    per resolved finding       {per:>6.2f} ACU")
    else:
        out.append("    not yet reported by the API — Devin aggregates consumption")
        out.append("    on a delay, so 0.00 here means 'unknown', not 'free'.")
    out.append("")

    # --- per finding ---
    out += ["  FINDINGS", ""]
    for f in sorted(report.findings, key=lambda r: r.key):
        issue = f"#{f.issue_number}" if f.issue_number else "not filed"
        out.append(f"    {f.key}")
        out.append(f"      {f.title[:70]}")
        out.append(f"      issue {issue:<10} status {f.status:<22} "
                   f"time-to-verdict {_duration(f.time_to_verdict)}")
        for pr in f.prs:
            out.append(f"      PR  {pr}")
        if f.unverifiable:
            out.append(f"      ADMITTED GAP: {f.unverifiable[:120]}")
        out.append("")

    # --- the things a naive dashboard omits ---
    if report.stalled_sessions:
        out += ["  ⚠ STALLED — blocked on a person, and nobody has been told", ""]
        for s in report.stalled_sessions:
            out.append(f"    {s['finding'] or s['session_id'][:12]}  "
                       f"waiting {_duration(report.generated_at - s['waiting_since'])}")
            out.append(f"      {s['url']}")
        out.append("")

    agreed, compared = report.heuristic_agreement
    if compared:
        out += [
            "  HEURISTIC vs AGENT",
            "",
            f"    the cheap rule matched the agent on {agreed}/{compared}",
            "    where they differ is where the judgement actually happened",
            "",
        ]

    if report.admitted_gaps:
        out += ["  ADMITTED GAPS — work the agent declined to claim as verified", ""]
        for f in report.admitted_gaps:
            out.append(f"    {f.key}: {f.unverifiable[:100]}")
        out.append("")

    out.append(rule)
    return "\n".join(out)


def render_html(report: Report) -> str:
    """Delegates to `dashboard.render`. Imported lazily so the text and JSON
    renderers stay usable if the dashboard module is ever stripped."""
    from .dashboard import render
    return render(report)


def render_json(report: Report) -> str:
    payload = dataclasses.asdict(report)
    payload["outcome_mix"] = dict(report.outcome_mix)
    payload["resolved_count"] = len(report.resolved)
    payload["pull_requests"] = report.prs
    agreed, compared = report.heuristic_agreement
    payload["heuristic_agreement"] = {"agreed": agreed, "compared": compared}
    return json.dumps(payload, indent=2)
