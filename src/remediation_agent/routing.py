"""Deciding whether a finding needs a verdict — not deciding the verdict.

The distinction matters. `TriageDecision` is what a finding deserves, and that
comes from Devin. This module answers a different, cheaper question: *is this
finding ambiguous enough to be worth an agent's judgment at all?*

Why it exists. The obvious design is to route every finding through a triage
session. That is correct for ambiguous work and wasteful for everything else:
a customer processing five hundred findings a week would pay for five hundred
triage sessions to surface perhaps forty non-obvious decisions. Paying an agent
to conclude "yes, apply the available patch" is not judgment, it is overhead.

So the router classifies on observable properties of the finding — never on a
guess about the answer — and every decision carries its reasons so it can be
audited in a dry run.

One deliberate bias: **ambiguity is the default.** A finding routes direct only
when every unambiguity test passes. Mis-routing an ambiguous finding to
remediation means an agent changing code it should have questioned; mis-routing
an unambiguous one to triage costs a few credits. Those errors are not
symmetrical, so the policy is not either.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .models import Finding, Severity, TriageDecision


class Route(enum.StrEnum):
    TRIAGE = "triage"
    """Send to a Devin triage session. Devin decides what it deserves."""

    DIRECT_TO_REMEDIATION = "direct_to_remediation"
    """Unambiguous: skip triage and remediate. Reserved for findings with no
    prohibitions, no open questions, and uncontested evidence."""

    HOLD_FOR_HUMAN = "hold_for_human"
    """Too consequential to dispatch autonomously under the current policy."""


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    route: Route
    reasons: tuple[str, ...]

    def explain(self) -> str:
        return f"{self.route.value}: " + "; ".join(self.reasons)


@dataclass(frozen=True, slots=True)
class Policy:
    """Routing policy. Conservative by default.

    `allow_direct_remediation` is off by default: on a first rollout, every
    finding should get a verdict on the record even where the answer looks
    obvious, because the outcome mix is the thing being established. Turn it on
    once the triage decisions have been reviewed and are trusted.
    """

    allow_direct_remediation: bool = False
    hold_severities: frozenset[Severity] = frozenset()
    """Severities never dispatched autonomously. Empty by default; a customer
    piloting on production code would populate this with HIGH."""

    max_paths_for_direct: int = 3
    """Above this many files in scope, a finding is not 'unambiguous' regardless
    of its other properties."""


DEFAULT_POLICY = Policy()


def route(finding: Finding, policy: Policy = DEFAULT_POLICY) -> RoutingDecision:
    """Classify a finding. Pure, and every branch records why."""
    reasons: list[str] = []

    if finding.severity in policy.hold_severities:
        return RoutingDecision(
            Route.HOLD_FOR_HUMAN,
            (f"severity {finding.severity} is held by policy",),
        )

    # --- ambiguity signals, in the order a reviewer would check them ---

    if finding.guardrails:
        reasons.append(
            f"{len(finding.guardrails)} stated prohibition(s) — something here must "
            f"not be done, which is a judgement call"
        )

    if finding.open_questions:
        reasons.append(
            f"{len(finding.open_questions)} question(s) the scanner could not resolve"
        )

    if finding.scanner_hint is None:
        reasons.append("no heuristic reading available")
    elif finding.scanner_hint is not TriageDecision.REMEDIATE:
        reasons.append(
            f"heuristic suggests {finding.scanner_hint.value}, which is not a "
            f"conclusion a heuristic is entitled to reach"
        )

    if len(finding.paths) > policy.max_paths_for_direct:
        reasons.append(
            f"{len(finding.paths)} files in scope (> {policy.max_paths_for_direct})"
        )

    if reasons:
        return RoutingDecision(Route.TRIAGE, tuple(reasons))

    # --- nothing ambiguous found ---

    if not policy.allow_direct_remediation:
        return RoutingDecision(
            Route.TRIAGE,
            ("no ambiguity signals, but direct remediation is disabled by policy — "
             "every finding gets a verdict on the record during rollout",),
        )

    return RoutingDecision(
        Route.DIRECT_TO_REMEDIATION,
        ("no prohibitions, no open questions, uncontested evidence, "
         f"{len(finding.paths)} file(s) in scope",),
    )


def summarise(decisions: dict[str, RoutingDecision]) -> str:
    """Human-readable routing table, for dry runs and the report."""
    if not decisions:
        return "no findings to route"

    buckets: dict[Route, list[str]] = {}
    for key, decision in decisions.items():
        buckets.setdefault(decision.route, []).append(key)

    lines = [f"{len(decisions)} finding(s) routed:"]
    for route_value in (Route.TRIAGE, Route.DIRECT_TO_REMEDIATION, Route.HOLD_FOR_HUMAN):
        keys = buckets.get(route_value)
        if not keys:
            continue
        lines.append(f"\n  {route_value.value}  ({len(keys)})")
        for key in sorted(keys):
            lines.append(f"    {key}")
            for reason in decisions[key].reasons:
                lines.append(f"       - {reason}")
    return "\n".join(lines)
