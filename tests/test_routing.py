"""Tests for the routing policy.

Routing decides whether a finding is ambiguous enough to need an agent's
judgment. It must never decide *what* the judgment is, and the asymmetry test
below is the one that matters: mis-routing an ambiguous finding to remediation
means an agent changing code it should have questioned.
"""

from __future__ import annotations

import pytest

from remediation_agent.models import Evidence, Finding, Severity, TriageDecision
from remediation_agent.routing import (
    DEFAULT_POLICY,
    Policy,
    Route,
    route,
    summarise,
)


def make_finding(**overrides) -> Finding:
    base = dict(
        key="test:finding",
        title="a finding",
        summary="something was observed",
        detector="test-detector",
        severity=Severity.MEDIUM,
        evidence=(Evidence("claim", "value", "source"),),
    )
    base.update(overrides)
    return Finding(**base)


# ---------------------------------------------------------------- ambiguity

def test_guardrails_force_triage():
    """A prohibition means something must not be done — that is a judgement."""
    finding = make_finding(guardrails=("do not change the install source",))
    decision = route(finding)

    assert decision.route is Route.TRIAGE
    assert any("prohibition" in r for r in decision.reasons)


def test_open_questions_force_triage():
    finding = make_finding(open_questions=("why was this reverted before?",))
    decision = route(finding)

    assert decision.route is Route.TRIAGE
    assert any("could not resolve" in r for r in decision.reasons)


def test_non_remediate_hint_forces_triage():
    """A heuristic may flag ambiguity; it may not conclude 'decline'."""
    finding = make_finding(scanner_hint=TriageDecision.DECLINE_NOT_ACTIONABLE)
    decision = route(finding)

    assert decision.route is Route.TRIAGE
    assert any("not a conclusion a heuristic is entitled to reach" in r
               for r in decision.reasons)


def test_missing_hint_forces_triage():
    finding = make_finding(scanner_hint=None)
    assert route(finding).route is Route.TRIAGE


def test_large_blast_radius_forces_triage():
    permissive = Policy(allow_direct_remediation=True, max_paths_for_direct=3)
    finding = make_finding(
        scanner_hint=TriageDecision.REMEDIATE,
        paths=("a.ts", "b.ts", "c.ts", "d.ts"),
    )
    decision = route(finding, permissive)

    assert decision.route is Route.TRIAGE
    assert any("files in scope" in r for r in decision.reasons)


def test_all_reasons_are_recorded_not_just_the_first():
    """Dry-run output must show every reason, so routing is auditable."""
    finding = make_finding(
        guardrails=("do not downgrade",),
        open_questions=("is it exploitable?",),
        scanner_hint=None,
    )
    decision = route(finding)

    assert len(decision.reasons) == 3


# ---------------------------------------------------------------- the default

def test_unambiguous_finding_still_triages_under_default_policy():
    """Conservative by default: during rollout every finding gets a verdict on
    the record, even where the answer looks obvious."""
    finding = make_finding(scanner_hint=TriageDecision.REMEDIATE)
    decision = route(finding, DEFAULT_POLICY)

    assert decision.route is Route.TRIAGE
    assert any("disabled by policy" in r for r in decision.reasons)


def test_unambiguous_finding_routes_direct_once_enabled():
    finding = make_finding(scanner_hint=TriageDecision.REMEDIATE, paths=("a.ts",))
    decision = route(finding, Policy(allow_direct_remediation=True))

    assert decision.route is Route.DIRECT_TO_REMEDIATION


@pytest.mark.parametrize("field,value", [
    ("guardrails", ("do not do the thing",)),
    ("open_questions", ("an unresolved question",)),
])
def test_ambiguity_beats_the_permissive_policy(field, value):
    """Enabling direct remediation must not override an ambiguity signal.

    This is the asymmetry: sending an ambiguous finding straight to remediation
    means an agent changing code it should have questioned. Wasting a few
    credits triaging an obvious one is the cheaper mistake.
    """
    finding = make_finding(scanner_hint=TriageDecision.REMEDIATE, **{field: value})
    decision = route(finding, Policy(allow_direct_remediation=True))

    assert decision.route is Route.TRIAGE


# ---------------------------------------------------------------- hold policy

def test_held_severity_never_dispatches():
    finding = make_finding(severity=Severity.HIGH,
                           scanner_hint=TriageDecision.REMEDIATE)
    decision = route(finding, Policy(hold_severities=frozenset({Severity.HIGH})))

    assert decision.route is Route.HOLD_FOR_HUMAN


def test_hold_policy_is_checked_before_anything_else():
    """A held finding must not leak its routing rationale into the decision —
    the hold is the whole answer."""
    finding = make_finding(
        severity=Severity.HIGH,
        guardrails=("do not",),
        open_questions=("why?",),
    )
    decision = route(finding, Policy(hold_severities=frozenset({Severity.HIGH})))

    assert decision.route is Route.HOLD_FOR_HUMAN
    assert len(decision.reasons) == 1


# ---------------------------------------------------------------- purity

def test_routing_never_returns_a_verdict():
    """Route is not TriageDecision. The router decides whether a verdict is
    needed; Devin decides what it is. Nothing here may collapse the two."""
    assert not set(Route) & {d.value for d in TriageDecision}


def test_routing_is_deterministic():
    finding = make_finding(guardrails=("x",), open_questions=("y",))
    assert route(finding) == route(finding)


# ---------------------------------------------------------------- summary

def test_summarise_groups_and_explains():
    decisions = {
        "a": route(make_finding(guardrails=("x",))),
        "b": route(make_finding(open_questions=("y",))),
    }
    text = summarise(decisions)

    assert "2 finding(s) routed" in text
    assert "triage" in text
    assert "a" in text and "b" in text
    assert "prohibition" in text


def test_summarise_handles_empty():
    assert summarise({}) == "no findings to route"
