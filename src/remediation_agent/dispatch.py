"""Turning findings into sessions, and triage verdicts into remediation work.

The pipeline is two-stage on purpose:

    scanner  ->  issue (agent:triage)  ->  TRIAGE session  ->  decision
                                                                  |
                              agent:remediate label  <------------+  (only if
                                       |                              remediate)
                                       v
                              REMEDIATION session  ->  PR

The decision in the middle comes from Devin, not from us. The scanner records a
`scanner_hint` but it is never read here — it exists only so the report can show
how often a cheap heuristic disagreed with an agent that read the code.

Promotion from triage to remediation is done by applying a label rather than by
calling the sessions API directly. That keeps the whole chain visible on the
issue timeline — a reviewer can see the decision happen — and it means the
remediation stage is driven by the same event mechanism as the first, rather than
by a private code path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import CAMPAIGN_TAG, Config
from .devin import DevinClient
from .github import GitHubClient
from .models import Finding, Stage, TriageDecision
from .routing import DEFAULT_POLICY, Policy, Route, RoutingDecision, route, summarise
from .playbooks import (
    DOCUMENTATION_BODY,
    DOCUMENTATION_TITLE,
    REVALIDATION_BODY,
    REVALIDATION_TITLE,
    REMEDIATION_BODY,
    REMEDIATION_TITLE,
    TRIAGE_BODY,
    TRIAGE_TITLE,
    documentation_prompt,
    remediation_prompt,
    triage_prompt,
)
from .schema import REMEDIATION_SCHEMA, REVALIDATION_SCHEMA, TRIAGE_SCHEMA

log = logging.getLogger(__name__)

TRIGGER_LABELS = {
    "agent:triage": ("8a63d8", "TRIGGER: dispatch a Devin triage session"),
    "agent:remediate": ("6f42c1", "TRIGGER: dispatch a Devin remediation session"),
    "agent:document": ("0e8a16", "TRIGGER: dispatch a Devin documentation session "
                                 "(records a determination; changes no code)"),
}

DESCRIPTIVE_LABELS = {
    "dependencies": ("0366d6", "Dependency version or suppression work"),
    "frontend": ("1d76db", "superset-frontend"),
    "build": ("5319e7", "Build tooling and bundling"),
    "test-infrastructure": ("c5def5", "Test harness and CI reliability"),
    "flaky-tests": ("fbca04", "Non-deterministic test behaviour"),
    "security": ("d93f0b", "Security-related"),
    "triage": ("bfd4f2", "Needs a determination, not necessarily a fix"),
    "false-positive": ("cfd3d7", "Scanner finding that is not actionable"),
    "blocked-upstream": ("b60205", "Blocked by an external project"),
}


@dataclass(slots=True)
class Provisioned:
    triage_playbook_id: str | None
    remediation_playbook_id: str | None
    documentation_playbook_id: str | None = None
    revalidation_playbook_id: str | None = None


@dataclass(slots=True)
class DispatchResult:
    finding_key: str
    action: str
    issue_number: int | None = None
    session_id: str | None = None
    session_url: str | None = None
    detail: str = ""


class Dispatcher:
    def __init__(self, cfg: Config, devin: DevinClient, github: GitHubClient,
                 policy: Policy = DEFAULT_POLICY) -> None:
        self.cfg = cfg
        self.devin = devin
        self.github = github
        self.policy = policy

    # ---------------------------------------------------------- planning

    def plan(self, findings: list[Finding]) -> dict[str, RoutingDecision]:
        """Decide which findings need an agent's judgment, and which do not.

        This is the only place a finding's path through the pipeline is chosen,
        and it chooses on observable properties — never on a guess at the answer.
        `RoutingDecision.reasons` makes every choice auditable in a dry run.

        With the default policy everything routes to triage, because all the
        findings this scanner currently produces carry prohibitions or open
        questions. That is a property of these findings, not a law: enable
        `Policy.allow_direct_remediation` and unambiguous findings skip triage.
        The distinction matters at volume, where paying an agent to conclude
        "yes, apply the available patch" is overhead rather than judgment.
        """
        decisions = {f.key: route(f, self.policy) for f in findings}
        log.info("%s", summarise(decisions))
        return decisions

    # ---------------------------------------------------------- provisioning

    def provision(self) -> Provisioned:
        """Create or update the playbooks and labels. Safe to re-run."""
        if self.cfg.dry_run:
            log.info("[dry-run] would upsert 4 playbooks and %d labels",
                     len(TRIGGER_LABELS) + len(DESCRIPTIVE_LABELS))
            return Provisioned(None, None, None, None)

        created = self.github.ensure_labels({**DESCRIPTIVE_LABELS, **TRIGGER_LABELS})
        if created:
            log.info("created labels: %s", ", ".join(created))

        triage = self.devin.upsert_playbook(
            TRIAGE_TITLE, TRIAGE_BODY, output_schema=TRIAGE_SCHEMA)
        remediation = self.devin.upsert_playbook(
            REMEDIATION_TITLE, REMEDIATION_BODY, output_schema=REMEDIATION_SCHEMA)
        # Shares the remediation schema: a documentation session reports the same
        # shape — an outcome, a summary, and what it verified. What differs is the
        # permission boundary, and that is carried by the playbook and the label,
        # not by the output contract.
        documentation = self.devin.upsert_playbook(
            DOCUMENTATION_TITLE, DOCUMENTATION_BODY,
            output_schema=REMEDIATION_SCHEMA)
        # The weekly scan reports rather than changes anything, so it needs a
        # shape of its own. Without one it ran, did the work, and emitted
        # nothing the collector could read — a session that answered into a void.
        revalidation = self.devin.upsert_playbook(
            REVALIDATION_TITLE, REVALIDATION_BODY,
            output_schema=REVALIDATION_SCHEMA)

        # Standing repo conventions, applied to every session automatically.
        self.devin.upsert_knowledge(
            name="Superset: generated requirements files",
            trigger="When changing Python dependencies in apache/superset",
            body=("`requirements/*.txt` are generated by `uv pip compile`. Never "
                  "hand-edit them — change `requirements/*.in` or `pyproject.toml` "
                  "and regenerate. A hand-edited lock file fails CI."),
        )
        self.devin.upsert_knowledge(
            name="Superset: which tests can run in a session",
            trigger="When verifying a change in apache/superset",
            body=("`pytest ./tests/common ./tests/unit_tests` needs no database and "
                  "runs in-session. Integration and e2e suites need Docker services "
                  "and will not run. Frontend: `npm run test -- <path>` from "
                  "`superset-frontend`."),
        )

        return Provisioned(
            triage_playbook_id=triage.get("playbook_id") or triage.get("id"),
            remediation_playbook_id=remediation.get("playbook_id") or remediation.get("id"),
            documentation_playbook_id=(documentation.get("playbook_id")
                                       or documentation.get("id")),
            revalidation_playbook_id=(revalidation.get("playbook_id")
                                      or revalidation.get("id")),
        )

    # ---------------------------------------------------------- stage 1

    def file_findings(self, findings: list[Finding]) -> list[DispatchResult]:
        """Create or update an issue per finding. Does not start any session.

        Everything enters as `agent:triage`. Nothing is pre-judged, and the
        trigger label is applied separately so filing is never accidentally
        the same action as spending money.
        """
        results: list[DispatchResult] = []
        existing = {} if self.cfg.dry_run else self.github.issues_by_finding_key()

        for finding in findings:
            body = finding.issue_body()
            labels = finding.all_labels()

            if self.cfg.dry_run:
                log.info("[dry-run] would file: %s", finding.title)
                results.append(DispatchResult(finding.key, "would-file",
                                              detail=f"labels={','.join(labels)}"))
                continue

            prior = existing.get(finding.key)
            if prior:
                self.github.update_issue(prior["number"], title=finding.title, body=body)
                results.append(DispatchResult(finding.key, "updated",
                                              issue_number=prior["number"]))
                log.info("updated #%s %s", prior["number"], finding.title[:60])
            else:
                issue = self.github.create_issue(finding.title, body, labels)
                results.append(DispatchResult(finding.key, "created",
                                              issue_number=issue["number"],
                                              detail=issue["html_url"]))
                log.info("created #%s %s", issue["number"], finding.title[:60])

        return results

    # ---------------------------------------------------------- stage 2

    def start_triage(self, finding: Finding, issue_number: int | None,
                     playbook_id: str | None) -> DispatchResult:
        prompt = triage_prompt(finding, issue_number, self.cfg.repo)
        tags = self._tags(Stage.TRIAGE, finding.key, issue_number)

        if self.cfg.dry_run:
            log.info("[dry-run] TRIAGE session for %s\n%s", finding.key, prompt)
            return DispatchResult(finding.key, "would-triage",
                                  issue_number=issue_number,
                                  detail=f"tags={','.join(tags)}")

        session = self.devin.create_session(
            prompt,
            title=f"triage: {finding.title[:70]}",
            repos=[self.cfg.repo],
            tags=tags,
            playbook_id=playbook_id,
            output_schema=TRIAGE_SCHEMA,
        )
        return DispatchResult(finding.key, "triage-started",
                              issue_number=issue_number,
                              session_id=session["session_id"],
                              session_url=session.get("url", ""))

    # ---------------------------------------------------------- stage 3

    def act_on_triage(self, finding: Finding, issue_number: int,
                      decision: TriageDecision, output: dict[str, Any]) -> DispatchResult:
        """Apply Devin's triage decision.

        A verdict that dispatches work promotes the issue by adding that stage's
        trigger label. Every other decision resolves the finding and is recorded
        as a comment, because a determination that nobody can read is not a
        deliverable.

        The stage comes from `decision.dispatch_stage` rather than from an `if`
        on REMEDIATE. That branch was the bug: `document_only` did not exist, so
        a verdict of "no fix, but write this down" had nowhere to go and fell
        through to the close-the-issue path with its recommendation unread.
        """
        reasoning = str(output.get("reasoning", "")).strip()
        confidence = output.get("confidence", "?")

        stage = decision.dispatch_stage
        if stage is not None:
            if self.cfg.dry_run:
                return DispatchResult(finding.key, f"would-promote:{stage.value}",
                                      issue_number=issue_number)
            self.github.comment(issue_number, self._triage_comment(decision, output))
            self.github.add_label(issue_number, stage.trigger_label)
            log.info("#%s promoted to %s (confidence: %s)",
                     issue_number, stage.value, confidence)
            return DispatchResult(finding.key, "promoted", issue_number=issue_number,
                                  detail=f"stage={stage.value} confidence={confidence}")

        if self.cfg.dry_run:
            return DispatchResult(finding.key, f"would-resolve:{decision.value}",
                                  issue_number=issue_number)

        self.github.comment(issue_number, self._triage_comment(decision, output))

        extra_label = {
            TriageDecision.DECLINE_NOT_ACTIONABLE: "false-positive",
            TriageDecision.BLOCKED_UPSTREAM: "blocked-upstream",
        }.get(decision)
        if extra_label:
            self.github.add_label(issue_number, extra_label)

        # A decline or an upstream block is a resolution: close it.
        # An escalation stays open — it is waiting on a person.
        if decision.resolves_finding:
            self.github.update_issue(issue_number, state="closed",
                                     state_reason="completed")
            log.info("#%s closed as %s", issue_number, decision.value)
        else:
            log.info("#%s left open for a human (%s)", issue_number, decision.value)

        return DispatchResult(finding.key, f"resolved:{decision.value}",
                              issue_number=issue_number, detail=reasoning[:160])

    def start_remediation(self, finding: Finding, issue_number: int,
                          playbook_id: str | None,
                          triage_output: dict[str, Any] | None = None) -> DispatchResult:
        return self._start_work(Stage.REMEDIATION, finding, issue_number,
                                playbook_id, triage_output)

    def start_documentation(self, finding: Finding, issue_number: int,
                            playbook_id: str | None,
                            triage_output: dict[str, Any] | None = None
                            ) -> DispatchResult:
        """Start the restricted stage: record the verdict, change no code."""
        return self._start_work(Stage.DOCUMENTATION, finding, issue_number,
                                playbook_id, triage_output)

    def _start_work(self, stage: Stage, finding: Finding, issue_number: int,
                    playbook_id: str | None,
                    triage_output: dict[str, Any] | None) -> DispatchResult:
        """The manual path into a work stage, for the CLI and for rehearsals.

        In normal operation nothing calls this: `act_on_triage` applies the
        stage's label and Devin's own automation starts the session. Both paths
        exist on purpose — the automation is the product, and this is how the
        pipeline can be driven without one when an automation is disarmed.
        """
        triage_output = triage_output or {}
        build_prompt = {
            Stage.REMEDIATION: remediation_prompt,
            Stage.DOCUMENTATION: documentation_prompt,
        }[stage]

        prompt = build_prompt(
            finding, issue_number, self.cfg.repo,
            triage_reasoning=str(triage_output.get("reasoning", "")),
            extra=str(triage_output.get("recommended_prompt_additions", "")),
        )
        tags = self._tags(stage, finding.key, issue_number)
        title_prefix = "fix" if stage is Stage.REMEDIATION else "record"

        if self.cfg.dry_run:
            log.info("[dry-run] %s session for %s\n%s",
                     stage.value.upper(), finding.key, prompt)
            return DispatchResult(finding.key, f"would-{stage.value}",
                                  issue_number=issue_number,
                                  detail=f"tags={','.join(tags)}")

        session = self.devin.create_session(
            prompt,
            title=f"{title_prefix}: {finding.title[:70]}",
            repos=[self.cfg.repo],
            tags=tags,
            playbook_id=playbook_id,
            output_schema=REMEDIATION_SCHEMA,
        )
        return DispatchResult(finding.key, f"{stage.value}-started",
                              issue_number=issue_number,
                              session_id=session["session_id"],
                              session_url=session.get("url", ""))

    # ---------------------------------------------------------- helpers

    @staticmethod
    def _tags(stage: Stage, finding_key: str, issue_number: int | None) -> list[str]:
        """Tags are the join key between Devin and our store.

        There is no outbound webhook, so the collector finds sessions by
        campaign tag and recovers their context entirely from these.
        """
        tags = [CAMPAIGN_TAG, f"stage:{stage.value}", f"finding:{finding_key}"]
        if issue_number:
            tags.append(f"issue:{issue_number}")
        return tags

    @staticmethod
    def _triage_comment(decision: TriageDecision, output: dict[str, Any]) -> str:
        lines = [
            f"### Triage: `{decision.value}`",
            "",
            f"**Confidence:** {output.get('confidence', 'not stated')}",
            "",
            str(output.get("reasoning", "")).strip() or "_no reasoning supplied_",
            "",
        ]

        checked = output.get("evidence_checked") or []
        if checked:
            lines += ["**Evidence independently verified**", ""]
            lines += [f"- {c}" for c in checked]
            lines += [""]

        contradicted = output.get("contradicted_evidence") or []
        if contradicted:
            lines += ["**Scanner claims found to be incorrect**", ""]
            lines += [f"- {c}" for c in contradicted]
            lines += [""]

        if output.get("blocker"):
            lines += [f"**Blocked on:** {output['blocker']}", ""]
        if output.get("escalation_reason"):
            lines += [f"**Needs a human because:** {output['escalation_reason']}", ""]
        if output.get("estimated_blast_radius"):
            lines += [f"**Blast radius:** {output['estimated_blast_radius']}", ""]

        lines += ["---", "_Determination by a Devin triage session. "
                  "The scanner assigned no disposition._"]
        return "\n".join(lines)
