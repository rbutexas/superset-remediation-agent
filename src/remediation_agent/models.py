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

    DECLINE_NOT_ACTIONABLE = "decline_not_actionable"
    """Investigated; no action is the correct outcome. A resolution, not a failure."""

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
        return self is TriageDecision.REMEDIATE


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

    @property
    def trigger_label(self) -> str:
        return f"agent:{self.value}"


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
            parts += ["### Open questions for triage", ""]
            parts += [f"- {q}" for q in self.open_questions]
            parts += [""]

        if self.guardrails:
            parts += ["### Do not", ""]
            parts += [f"- {g}" for g in self.guardrails]
            parts += [""]

        if self.acceptance:
            parts += ["### Acceptance criteria, if this is remediated", ""]
            parts += [f"{i}. {a}" for i, a in enumerate(self.acceptance, 1)]
            parts += [""]

        parts += [
            "---",
            f"<!-- finding-key: {self.key} -->",
            f"_Detected by `{self.detector}`. No disposition assigned — triage "
            f"decides. Re-runnable: `remediation-agent scan --detector {self.detector}`._",
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

    @property
    def has_output(self) -> bool:
        return bool(self.structured_output)

    @property
    def is_terminal(self) -> bool:
        """Has this session finished the work we asked for?

        Note the `waiting_for_user` case. Observed on a real session: the agent
        emitted its structured output, ended its turn, and moved to
        `waiting_for_user` — it never reports `finished`. Treating that as a
        stall would mean waiting forever for a session that had already
        answered. **Structured output is the completion signal; the status field
        is not.**
        """
        if self.status in ("exit", "error"):
            return True
        if self.status == "running" and self.status_detail == "finished":
            return True
        return self.needs_human and self.has_output

    @property
    def needs_human(self) -> bool:
        return self.status_detail in ("waiting_for_user", "waiting_for_approval")

    @property
    def is_stalled(self) -> bool:
        """Waiting on a person, with nothing to show for it.

        The distinction from `is_terminal` matters: a session that answered and
        then idled is done, while one that stopped to ask a question is blocked,
        billing, and nobody has been told. Only the second is a problem, and
        conflating them means either chasing sessions that finished or missing
        ones that are stuck.
        """
        return self.needs_human and not self.has_output

    def to_row(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
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
