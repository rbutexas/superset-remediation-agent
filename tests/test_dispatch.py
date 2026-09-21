"""What the dispatcher does with a verdict.

These exist because of a real dead end. `act_on_triage` branched on
`decision is TriageDecision.REMEDIATE`, so the only verdict that could reach a
second session was "fix it". A verdict of "no fix is correct, but record the
determination" fell through to the resolution path: the issue was closed, the
agent's recommendation was posted as a comment nobody was going to action, and
nothing in the target repository changed. The next scan reproduced the finding.

So the assertions here are mostly about *which label gets applied*, because the
label is the only thing that starts work.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from remediation_agent.config import Config                        # noqa: E402
from remediation_agent.dispatch import TRIGGER_LABELS, Dispatcher  # noqa: E402
from remediation_agent.models import (                             # noqa: E402
    Finding, Severity, Stage, TriageDecision,
)


class FakeGitHub:
    """Records what would have been done, so the assertions are about intent."""

    def __init__(self) -> None:
        self.labels: list[tuple[int, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.updates: list[tuple[int, dict]] = []

    def add_label(self, issue_number: int, label: str):
        self.labels.append((issue_number, label))

    def comment(self, number: int, body: str):
        self.comments.append((number, body))

    def update_issue(self, number: int, **fields):
        self.updates.append((number, fields))

    @property
    def closed(self) -> list[int]:
        return [n for n, f in self.updates if f.get("state") == "closed"]


@pytest.fixture
def setup():
    cfg = Config(devin_api_key="k", devin_org_id="o", github_token="t",
                 repo="acme/widgets")
    github = FakeGitHub()
    return Dispatcher(cfg, devin=None, github=github), github


def make_finding(**kw) -> Finding:
    return Finding(
        key=kw.pop("key", "unresolvable-advisory:xlsx"),
        title=kw.pop("title", "advisories cannot be resolved by upgrade"),
        summary=kw.pop("summary", "s" * 90),
        detector=kw.pop("detector", "unresolvable-advisory"),
        severity=kw.pop("severity", Severity.MEDIUM),
        **kw,
    )


OUTPUT = {"reasoning": "checked the lockfile and both advisories",
          "confidence": "high"}


# ------------------------------------------------------------------ promotion

def test_document_only_promotes_instead_of_closing(setup):
    """The regression this stage was added for."""
    dispatcher, github = setup

    dispatcher.act_on_triage(make_finding(), 7,
                             TriageDecision.DOCUMENT_ONLY, OUTPUT)

    assert (7, "agent:document") in github.labels
    assert github.closed == [], "a dispatched finding is not resolved yet"


def test_remediate_still_promotes_to_remediation(setup):
    dispatcher, github = setup

    dispatcher.act_on_triage(make_finding(), 7, TriageDecision.REMEDIATE, OUTPUT)

    assert (7, "agent:remediate") in github.labels
    assert github.closed == []


def test_a_dismissal_closes_and_starts_nothing(setup):
    dispatcher, github = setup

    dispatcher.act_on_triage(make_finding(), 7,
                             TriageDecision.DECLINE_NOT_ACTIONABLE, OUTPUT)

    assert github.closed == [7]
    trigger_labels = {label for _, label in github.labels} & set(TRIGGER_LABELS)
    assert not trigger_labels, "a dismissal must not start a session"


def test_an_escalation_stays_open_for_a_person(setup):
    dispatcher, github = setup

    dispatcher.act_on_triage(make_finding(), 7,
                             TriageDecision.ESCALATE_TO_HUMAN, OUTPUT)

    assert github.closed == []
    assert not {label for _, label in github.labels} & set(TRIGGER_LABELS)


def test_every_verdict_either_dispatches_or_resolves_or_escalates(setup):
    """No verdict may do nothing at all — that was the original defect, and it
    was invisible because the issue still got a comment."""
    for decision in TriageDecision:
        dispatcher, github = setup[0], FakeGitHub()
        dispatcher.github = github
        dispatcher.act_on_triage(make_finding(), 7, decision, OUTPUT)

        started = bool({label for _, label in github.labels} & set(TRIGGER_LABELS))
        closed = bool(github.closed)
        escalated = decision is TriageDecision.ESCALATE_TO_HUMAN

        assert started or closed or escalated, (
            f"{decision.value} leaves the finding in limbo")
        assert github.comments, f"{decision.value} recorded no reasoning"


# ------------------------------------------------------------------ labels

def test_every_trigger_label_is_registered_for_creation():
    """GitHub creates an unknown label silently on first use, so a label the
    dispatcher applies but never registers ends up uncoloured, undocumented, and
    indistinguishable from a typo."""
    for stage in Stage:
        if not stage.is_label_triggered:
            continue
        assert stage.trigger_label in TRIGGER_LABELS


def test_dry_run_names_the_stage_it_would_dispatch_to(setup):
    """A dry run that reports 'would-promote' without saying where is not an
    audit of anything."""
    dispatcher, _ = setup
    dispatcher.cfg = Config(devin_api_key="k", devin_org_id="o", github_token="t",
                            repo="acme/widgets", dry_run=True)

    result = dispatcher.act_on_triage(make_finding(), 7,
                                      TriageDecision.DOCUMENT_ONLY, OUTPUT)
    assert result.action == "would-promote:documentation"
