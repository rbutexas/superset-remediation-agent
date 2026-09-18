"""Core domain types.

The important idea here is `Verdict`. A remediation pipeline that can only
succeed by changing code will change code it should not touch. Three of these
five verdicts involve no code change at all, and two of those are successes.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import Any


class Verdict(enum.StrEnum):
    """What a session concluded. Enforced by the session's output schema."""

    FIXED = "fixed"
    """Change applied, verification ran and passed, PR opened."""

    MITIGATED = "mitigated"
    """Not upgradable; a compensating change was made instead."""

    DECLINED_NOT_ACTIONABLE = "declined_not_actionable"
    """Investigated; no action is the correct outcome. A success, not a failure."""

    BLOCKED_UPSTREAM = "blocked_upstream"
    """Cannot proceed until a named external dependency changes. Names the blocker."""

    ESCALATE_TO_HUMAN = "escalate_to_human"
    """Requires a judgement outside the agent's remit. Names what is needed."""

    @property
    def is_success(self) -> bool:
        """Every verdict except escalation is a resolved outcome.

        An hour saved dismissing a false positive is worth the same as an hour
        saved landing a fix. Counting only `FIXED` is how automation ends up
        optimising for a green dashboard.
        """
        return self is not Verdict.ESCALATE_TO_HUMAN

    @property
    def changed_code(self) -> bool:
        return self in (Verdict.FIXED, Verdict.MITIGATED)


class Severity(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Disposition(enum.StrEnum):
    """What the scanner believes this finding needs, before Devin looks at it."""

    REMEDIATE = "remediate"
    """Expected to require a code change."""

    TRIAGE = "triage"
    """Expected to require a determination. 'No action' is a valid result."""

    @property
    def trigger_label(self) -> str:
        return f"agent:{self.value}"


@dataclasses.dataclass(frozen=True, slots=True)
class Evidence:
    """One verifiable statement supporting a finding.

    Every claim a finding makes must arrive as one of these. `source` says where
    it can be re-checked, so nothing rests on the scanner's assertion alone.
    """

    claim: str
    value: str
    source: str

    def as_markdown(self) -> str:
        return f"- **{self.claim}:** {self.value}  \n  _source: {self.source}_"


@dataclasses.dataclass(frozen=True, slots=True)
class Finding:
    """Something the scanner detected that may warrant work."""

    key: str
    """Stable identifier. Used to deduplicate across runs, so a weekly scan does
    not refile the same issue."""

    title: str
    summary: str
    detector: str
    disposition: Disposition
    severity: Severity
    evidence: tuple[Evidence, ...] = ()
    labels: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    guardrails: tuple[str, ...] = ()
    """Things the agent must NOT do. The xlsx finding is only safe with one of
    these; without it the obvious remediation is a downgrade."""

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

        if self.guardrails:
            parts += ["### Do not", ""]
            parts += [f"- {g}" for g in self.guardrails]
            parts += [""]

        if self.acceptance:
            parts += ["### Acceptance criteria", ""]
            parts += [f"{i}. {a}" for i, a in enumerate(self.acceptance, 1)]
            parts += [""]

        parts += [
            "---",
            f"<!-- finding-key: {self.key} -->",
            f"_Detected by `{self.detector}`. Re-runnable: "
            f"`remediation-agent scan --detector {self.detector}`._",
        ]
        return "\n".join(parts)

    def all_labels(self) -> list[str]:
        return sorted({*self.labels, self.disposition.trigger_label})


@dataclasses.dataclass(slots=True)
class SessionRecord:
    """Our view of a Devin session. Assembled by polling; Devin does not call back."""

    session_id: str
    finding_key: str | None
    issue_number: int | None
    status: str
    status_detail: str | None
    acus: float
    url: str
    title: str | None = None
    verdict: Verdict | None = None
    structured_output: dict[str, Any] | None = None
    pull_requests: tuple[str, ...] = ()
    created_at: int = 0
    updated_at: int = 0

    @property
    def is_terminal(self) -> bool:
        if self.status in ("exit", "error"):
            return True
        return self.status == "running" and self.status_detail == "finished"

    @property
    def needs_human(self) -> bool:
        """The silent stall: running, billing, and waiting on a person who has
        not been told. This is the failure mode nobody instruments."""
        return self.status_detail in ("waiting_for_user", "waiting_for_approval")

    def to_row(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["verdict"] = self.verdict.value if self.verdict else None
        d["pull_requests"] = json.dumps(list(self.pull_requests))
        d["structured_output"] = (
            json.dumps(self.structured_output) if self.structured_output else None
        )
        return d
