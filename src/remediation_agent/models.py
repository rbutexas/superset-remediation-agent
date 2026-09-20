"""Core domain types.

The central design point is that **the scanner does not decide what a finding
deserves.** It produces facts. A Devin triage session reads those facts plus the
repository and decides.

That split is deliberate. The argument this system makes is that scanners produce
findings and bots produce patches, but nothing produces *verdicts*. If the verdict
came from an `if` statement in this file, the argument would answer itself.

The scanner still records an opinion — `scanner_hint` — but it is never used for
routing. It exists so we can measure how often a cheap heuristic disagrees with an
agent that read the code, which is a more interesting number than either alone.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import Any


class TriageDecision(enum.StrEnum):
    """What a Devin triage session concluded a finding deserves.

    Produced by the agent, not by us.
    """

    REMEDIATE = "remediate"
    """Real, actionable, worth the change. Proceeds to a remediation session."""

    DOCUMENT_ONLY = "document_only"
    """No code fix exists or is wanted, but the determination itself belongs in
    the repository. Proceeds to a documentation session, which may write
    suppression entries, ignore-rule comments and dated re-check conditions —
    and nothing else.

    This verdict exists because `decline_not_actionable` used to absorb it, and
    that was a dead end. The xlsx advisory was investigated correctly, declined
    correctly, and then the reasoning lived only in a closed issue and an agent
    transcript. Nothing in Superset's own tree changed, so the next scan against
    a fresh checkout reproduced the finding exactly, and the next engineer
    started from zero. A determination nobody can find is not a resolution.

    Kept separate from REMEDIATE rather than folded into it, because the
    capability being granted is different in kind: writing a suppression is the
    power to silence a scanner. It always opens a pull request and never merges
    one, so a person still approves every silence."""

    DECLINE_NOT_ACTIONABLE = "decline_not_actionable"
    """Investigated; no action is the correct outcome, and there is nothing worth
    recording in the tree either. A resolution, not a failure."""

    BLOCKED_UPSTREAM = "blocked_upstream"
    """Cannot proceed until a named external thing changes. Must name it."""

    ESCALATE_TO_HUMAN = "escalate_to_human"
    """Needs a judgement outside the agent's remit. Must say what is needed."""

    @property
    def resolves_finding(self) -> bool:
        """True where the finding needs nothing further from a human.

        An hour saved dismissing a false positive is worth the same as an hour
        saved landing a fix, and it is the scarcer outcome. Counting only
        REMEDIATE creates an incentive to change code that should not change.
        """
        return self in (
            TriageDecision.DECLINE_NOT_ACTIONABLE,
            TriageDecision.BLOCKED_UPSTREAM,
        )

    @property
    def dispatches_work(self) -> bool:
        return self.dispatch_stage is not None

    @property
    def dispatch_stage(self) -> "Stage | None":
        """Which stage this verdict promotes to, if any.

        A property rather than a branch at the call site: the collector, the
        dispatcher and the report all need to know where a verdict goes, and
        three copies of the same `if` is how they drift apart.
        """
        return {
            TriageDecision.REMEDIATE: Stage.REMEDIATION,
            TriageDecision.DOCUMENT_ONLY: Stage.DOCUMENTATION,
        }.get(self)


class RemediationOutcome(enum.StrEnum):
    """What a Devin remediation session achieved."""

    FIXED = "fixed"
    """Change applied, verification ran and passed, PR opened."""

    MITIGATED = "mitigated"
    """Not upgradable; a compensating change was made instead."""

    FAILED_VERIFICATION = "failed_verification"
    """A change was made but could not be shown correct. Reported, not merged."""

    ABANDONED_ON_GUARDRAIL = "abandoned_on_guardrail"
    """Stopped because proceeding would have violated a stated prohibition.
    This is a success: the guardrail did its job."""

    ESCALATE_TO_HUMAN = "escalate_to_human"

    @property
    def is_success(self) -> bool:
        return self in (
            RemediationOutcome.FIXED,
            RemediationOutcome.MITIGATED,
            RemediationOutcome.ABANDONED_ON_GUARDRAIL,
        )

    @property
    def produced_code(self) -> bool:
        return self in (RemediationOutcome.FIXED, RemediationOutcome.MITIGATED)


class Severity(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Stage(enum.StrEnum):
    """Which role a session was created for. Same API, different playbook,
    different output schema, different prompt."""

    TRIAGE = "triage"
    REMEDIATION = "remediation"
    DOCUMENTATION = "documentation"
    """Writes the determination into the tree, and may not touch anything else.

    A separate stage rather than a flag on REMEDIATION because the stage *is*
    the permission boundary. A session started by a GitHub automation receives a
    fixed prompt and fixed tags — the automation cannot vary them per event — so
    the only thing that can tell a restricted session from an unrestricted one
    is which label fired it. Encoding the restriction in a flag we pass locally
    would leave the automation-started path unrestricted, which is the path that
    actually runs."""

    @property
    def is_work(self) -> bool:
        """Produces a change and a pull request, as opposed to a verdict."""
        return self in (Stage.REMEDIATION, Stage.DOCUMENTATION)

    @property
    def trigger_label(self) -> str:
        """The GitHub label whose appearance starts this stage.

        Mapped explicitly rather than derived from the enum value. Deriving it
        gave `agent:remediation` while the label registered on the repository
        was `agent:remediate` — GitHub creates an unknown label on first use, so
        the mismatch would not have raised anything; it would have left one
        uncoloured label in use and another unused, with the automation
        listening for whichever the enum happened to spell.

        The label is a user-facing name and the enum value is an internal one.
        They are allowed to differ, but not by accident.
        """
        return _TRIGGER_LABELS[self]


_TRIGGER_LABELS: dict[Stage, str] = {
    Stage.TRIAGE: "agent:triage",
    Stage.REMEDIATION: "agent:remediate",
    Stage.DOCUMENTATION: "agent:document",
}


@dataclasses.dataclass(frozen=True, slots=True)
class Evidence:
    """One verifiable statement supporting a finding.

    `source` says where it can be re-checked, so nothing rests on the scanner's
    assertion alone. Superset's own AGENTS.md asks automated tools to make
    findings testable rather than assert them; this is how that is honoured.
    """

    claim: str
    value: str
    source: str

    def as_markdown(self) -> str:
        return f"- **{self.claim}:** {self.value}  \n  _source: {self.source}_"


@dataclasses.dataclass(frozen=True, slots=True)
class Finding:
    """Something the scanner observed. Deliberately carries no decision."""

    key: str
    """Stable identifier, embedded in the issue body. Deduplicates across runs so
    a weekly scan updates rather than refiles."""

    title: str
    summary: str
    detector: str
    severity: Severity
    evidence: tuple[Evidence, ...] = ()
    labels: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()

    guardrails: tuple[str, ...] = ()
    """Things the agent must not do, rendered into both the issue and the prompt.

    'Resolve this finding' is an under-specified objective, and under-specified
    objectives get satisfied the cheapest way available. For the xlsx finding the
    cheapest way is a downgrade. The prohibition has to be data, not a hope about
    model behaviour."""

    scanner_hint: TriageDecision | None = None
    """What a cheap heuristic would have guessed. **Never used for routing.**
    Recorded only so agreement with Devin's triage can be measured."""

    open_questions: tuple[str, ...] = ()
    """What the scanner could not determine. Passed to triage as the things it
    specifically needs to resolve."""

    def issue_body(self) -> str:
        parts = [self.summary.strip(), ""]

        if self.evidence:
            parts += ["### Evidence", ""]
            parts += [e.as_markdown() for e in self.evidence]
            parts += [""]

        if self.paths:
            parts += ["### Files in scope", "", "```"]
            parts += list(self.paths)
            parts += ["```", ""]

        if self.open_questions:
            parts += ["### To determine", ""]
            parts += [f"- {q}" for q in self.open_questions]
            parts += [""]

        if self.guardrails:
            parts += ["### Do not", ""]
            parts += [f"- {g}" for g in self.guardrails]
            parts += [""]

        if self.acceptance:
            parts += ["### Done when", ""]
            parts += [f"{i}. {a}" for i, a in enumerate(self.acceptance, 1)]
            parts += [""]

        parts += [
            "---",
            f"<sub>filed by <code>{self.detector}</code> · "
            f"<code>remediation-agent scan --detector {self.detector}</code></sub>",
            f"<!-- finding-key: {self.key} -->",
        ]
        return "\n".join(parts)

    def all_labels(self) -> list[str]:
        """Everything enters the pipeline as triage. Nothing is pre-judged."""
        return sorted({*self.labels, Stage.TRIAGE.trigger_label})


@dataclasses.dataclass(slots=True)
class SessionRecord:
    """Our view of a Devin session, assembled by polling.

    Devin has no outbound webhook, so this is reconciled rather than pushed.
    """

    session_id: str
    stage: Stage | None
    finding_key: str | None
    issue_number: int | None
    status: str
    status_detail: str | None
    acus: float
    url: str
    title: str | None = None
    triage_decision: TriageDecision | None = None
    remediation_outcome: RemediationOutcome | None = None
    structured_output: dict[str, Any] | None = None
    pull_requests: tuple[str, ...] = ()
    created_at: int = 0
    updated_at: int = 0

    archived: bool = False
    """Retired from the board by a human, in Devin's own UI or API.

    Archiving does not remove a session from `GET /sessions`, so the collector
    has to honour it explicitly. It is the only way to retire a superseded run —
    a finding re-triaged under a changed policy has two verdicts on record, and
    without this the board keeps rendering both. Nothing is deleted: the session,
    its output and its replay all survive, and un-archiving brings it back."""

    @property
    def answered(self) -> bool:
        """Produced structured output — the only reliable completion signal.

        Devin does not report `finished` for a session that emits output and
        ends its turn; it moves to `waiting_for_user`, and roughly thirty
        minutes later to `suspended`/`inactivity`. Status alone therefore cannot
        distinguish "done" from "gave up".
        """
        return bool(self.structured_output)

    @property
    def is_terminal(self) -> bool:
        """Will not progress further without intervention.

        Terminal is not the same as successful — a session that ran out of
        budget is terminal and useless. `answered` is what separates them.
        """
        if self.status in ("exit", "error", "suspended"):
            return True
        if self.status == "running" and self.status_detail == "finished":
            return True
        return self.needs_human and self.answered

    @property
    def needs_human(self) -> bool:
        return self.status_detail in ("waiting_for_user", "waiting_for_approval")

    @property
    def is_stalled(self) -> bool:
        """Blocked on a person right now, with nothing to show for it.

        Distinct from `is_terminal`: a session that answered and then idled is
        done, while one that stopped to ask a question is blocked and billing.
        """
        return self.needs_human and not self.answered

    @property
    def abandoned(self) -> bool:
        """Stopped without ever answering.

        This is the case that would otherwise vanish. A stalled session is only
        `waiting_for_user` for about thirty minutes before Devin suspends it for
        inactivity — at which point it stops matching `is_stalled` and starts
        matching `is_terminal`. Without this it would quietly be counted as a
        completed session that produced nothing.
        """
        return self.is_terminal and not self.answered

    def to_row(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        # Not a column: an archived session is never written at all.
        d.pop("archived", None)
        d["stage"] = self.stage.value if self.stage else None
        d["triage_decision"] = self.triage_decision.value if self.triage_decision else None
        d["remediation_outcome"] = (
            self.remediation_outcome.value if self.remediation_outcome else None
        )
        d["pull_requests"] = json.dumps(list(self.pull_requests))
        d["structured_output"] = (
            json.dumps(self.structured_output) if self.structured_output else None
        )
        return d
