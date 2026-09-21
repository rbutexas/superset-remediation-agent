"""Tests for session completion semantics and payload mapping.

The completion rule here is not obvious and was wrong first time. On a real
session, Devin emitted valid structured output, ended its turn, and moved to
`waiting_for_user`. It never reported `finished`. Treating that as a stall meant
waiting forever for a session that had already answered.

So: **structured output is the completion signal, and status is not.** These
tests pin that, using a payload shaped like the one that caused the bug.
"""

from __future__ import annotations

import pytest

from remediation_agent.devin import to_record
from remediation_agent.models import (
    RemediationOutcome,
    SessionRecord,
    Stage,
    TriageDecision,
)


def session(**overrides) -> SessionRecord:
    base = dict(
        session_id="s", stage=Stage.TRIAGE, finding_key="f", issue_number=1,
        status="running", status_detail="working", acus=0.0, url="u",
    )
    base.update(overrides)
    return SessionRecord(**base)


# ------------------------------------------------------- completion semantics

def test_answered_then_idle_is_complete_not_stalled():
    """The exact shape observed on the first real session."""
    rec = session(status_detail="waiting_for_user",
                  structured_output={"decision": "decline_not_actionable"})

    assert rec.is_terminal
    assert not rec.is_stalled
    assert rec.needs_human          # still true — it just is not a problem


def test_waiting_with_no_output_is_stalled():
    rec = session(status_detail="waiting_for_user", structured_output=None)

    assert rec.is_stalled
    assert not rec.is_terminal


def test_awaiting_approval_with_no_output_is_stalled():
    rec = session(status_detail="waiting_for_approval")
    assert rec.is_stalled


def test_working_is_neither():
    rec = session()
    assert not rec.is_terminal and not rec.is_stalled


def test_exit_and_error_are_terminal_without_output():
    assert session(status="exit", status_detail=None).is_terminal
    assert session(status="error", status_detail=None).is_terminal


def test_explicit_finished_is_terminal():
    assert session(status="running", status_detail="finished").is_terminal


# ------------------------------------------------------------ payload mapping

REAL_SHAPE = {
    "session_id": "f8ff776236eb4dd3b7bed045d2b3745d",
    "url": "https://app.devin.ai/sessions/f8ff776236eb4dd3b7bed045d2b3745d",
    "status": "running",
    "status_detail": "waiting_for_user",
    "acus_consumed": 0.0,
    "tags": ["campaign:superset-debt", "stage:triage",
             "finding:unresolvable-advisory:xlsx", "issue:42"],
    "pull_requests": [],
    "created_at": 1789771743,
    "updated_at": 1789771827,
    "structured_output": {"decision": "decline_not_actionable",
                          "confidence": "high",
                          "reasoning": "verified the installed version"},
}


def test_context_is_recovered_entirely_from_tags():
    """Tags are the only join key — Devin has no callback carrying our context."""
    rec = to_record(REAL_SHAPE)

    assert rec.stage is Stage.TRIAGE
    assert rec.finding_key == "unresolvable-advisory:xlsx"
    assert rec.issue_number == 42


def test_finding_key_containing_a_colon_survives():
    """`finding:unresolvable-advisory:xlsx` must not be split on the wrong colon."""
    assert to_record(REAL_SHAPE).finding_key == "unresolvable-advisory:xlsx"


def test_triage_decision_is_parsed():
    rec = to_record(REAL_SHAPE)
    assert rec.triage_decision is TriageDecision.DECLINE_NOT_ACTIONABLE
    assert rec.remediation_outcome is None
    assert rec.is_terminal


def test_remediation_outcome_is_parsed():
    payload = {**REAL_SHAPE,
               "tags": ["stage:remediation", "finding:f"],
               "structured_output": {"outcome": "fixed", "summary": "done"},
               "pull_requests": [{"url": "https://github.com/x/y/pull/1"}]}
    rec = to_record(payload)

    assert rec.stage is Stage.REMEDIATION
    assert rec.remediation_outcome is RemediationOutcome.FIXED
    assert rec.pull_requests == ("https://github.com/x/y/pull/1",)


def test_unknown_enum_value_does_not_raise():
    """A schema change upstream must degrade to 'unparsed', not crash the loop."""
    payload = {**REAL_SHAPE, "structured_output": {"decision": "something_new"}}
    rec = to_record(payload)

    assert rec.triage_decision is None
    assert rec.structured_output == {"decision": "something_new"}


def test_missing_optional_fields_are_tolerated():
    rec = to_record({"session_id": "x", "status": "new"})

    assert rec.session_id == "x"
    assert rec.stage is None and rec.finding_key is None
    assert rec.acus == 0.0


def test_non_numeric_issue_tag_is_ignored():
    payload = {**REAL_SHAPE, "tags": ["issue:not-a-number", "finding:f"]}
    assert to_record(payload).issue_number is None


# ------------------------------------------------------------------- verdicts

def test_dismissals_resolve_a_finding():
    """An hour saved dismissing a false positive is worth an hour saved fixing."""
    assert TriageDecision.DECLINE_NOT_ACTIONABLE.resolves_finding
    assert TriageDecision.BLOCKED_UPSTREAM.resolves_finding
    assert not TriageDecision.ESCALATE_TO_HUMAN.resolves_finding
    assert not TriageDecision.REMEDIATE.resolves_finding  # it dispatches instead


def test_exactly_two_verdicts_dispatch_work():
    dispatching = [d for d in TriageDecision if d.dispatches_work]
    assert dispatching == [TriageDecision.REMEDIATE, TriageDecision.DOCUMENT_ONLY]


def test_every_dispatching_verdict_names_a_stage():
    """`dispatches_work` is derived from `dispatch_stage`, so a new verdict that
    forgets to name a stage silently stops dispatching rather than failing."""
    for decision in TriageDecision:
        assert decision.dispatches_work == (decision.dispatch_stage is not None)


def test_document_only_dispatches_to_the_restricted_stage():
    """The whole point: a verdict of "no fix, but write it down" must reach a
    session, not close the issue with its recommendation unread."""
    assert TriageDecision.DOCUMENT_ONLY.dispatch_stage is Stage.DOCUMENTATION
    assert TriageDecision.REMEDIATE.dispatch_stage is Stage.REMEDIATION
    assert TriageDecision.DECLINE_NOT_ACTIONABLE.dispatch_stage is None


def test_document_only_is_not_a_resolution_on_its_own():
    """It has dispatched work that has not happened yet. Counting it as resolved
    at triage time would report the finding answered before anything was written."""
    assert not TriageDecision.DOCUMENT_ONLY.resolves_finding


def test_every_label_triggered_stage_has_a_distinct_label():
    """Two stages sharing a label would mean one automation firing for both, and
    the restricted stage is only restricted because it has its own trigger."""
    triggered = [s for s in Stage if s.is_label_triggered]
    labels = [s.trigger_label for s in triggered]
    assert len(labels) == len(set(labels)) == len(triggered)


def test_the_scheduled_stage_has_no_label_and_says_so():
    """Revalidation is started by a schedule. Asking it for a label is a bug in
    the caller, and it should say which property to check instead of raising a
    bare KeyError from a private dict."""
    assert not Stage.REVALIDATION.is_label_triggered
    with pytest.raises(ValueError, match="is_label_triggered"):
        Stage.REVALIDATION.trigger_label


def test_the_scheduled_stage_produces_no_pull_request():
    assert not Stage.REVALIDATION.is_work


def test_only_work_stages_produce_changes():
    assert Stage.REMEDIATION.is_work
    assert Stage.DOCUMENTATION.is_work
    assert not Stage.TRIAGE.is_work


def test_stopping_on_a_guardrail_is_a_success():
    """Without this an agent facing a prohibition has no honest exit."""
    assert RemediationOutcome.ABANDONED_ON_GUARDRAIL.is_success
    assert not RemediationOutcome.ABANDONED_ON_GUARDRAIL.produced_code
    assert not RemediationOutcome.FAILED_VERIFICATION.is_success


# ------------------------------------------------------------------ hygiene

def test_sessions_are_created_disposable_by_default():
    """The API defaults `resumable` to true, which preserves VM state after a
    session stops. Ours are read-once and never resumed, so that state is
    overhead we should not be holding."""
    import inspect

    from remediation_agent.devin import DevinClient

    sig = inspect.signature(DevinClient.create_session)
    assert sig.parameters["resumable"].default is False


def test_only_answered_sessions_are_torn_down():
    """A stalled session must survive: a human may still want to reply to it.
    An answered one has given us everything it is going to."""
    answered = session(status_detail="waiting_for_user",
                       structured_output={"decision": "remediate"})
    stalled = session(status_detail="waiting_for_user")

    assert answered.answered and not answered.is_stalled
    assert stalled.is_stalled and not stalled.answered


def test_trigger_labels_match_the_labels_actually_registered():
    """The label an automation listens for must be the one we create.

    These drifted once: the enum value spelled `agent:remediation` while the
    repository registered `agent:remediate`. GitHub creates an unknown label
    silently, so nothing would have failed loudly.
    """
    from remediation_agent.dispatch import TRIGGER_LABELS

    for stage in Stage:
        if not stage.is_label_triggered:
            continue
        assert stage.trigger_label in TRIGGER_LABELS, (
            f"{stage} listens for {stage.trigger_label!r}, which is not a label "
            f"this tool creates: {sorted(TRIGGER_LABELS)}"
        )


def test_pull_request_url_is_read_from_the_real_field():
    """Devin returns `pr_url`, not `url`. Reading the wrong key dropped every
    pull request silently: the dashboard showed a remediation as fixed with no
    link to what it produced. Shape taken from a real session payload."""
    payload = {**REAL_SHAPE,
               "tags": ["stage:remediation", "finding:f"],
               "structured_output": {"outcome": "fixed", "summary": "x" * 90},
               "pull_requests": [{"pr_url": "https://github.com/o/r/pull/3",
                                  "pr_state": "open"}]}

    assert to_record(payload).pull_requests == ("https://github.com/o/r/pull/3",)


def test_legacy_url_key_still_works():
    payload = {**REAL_SHAPE,
               "pull_requests": [{"url": "https://github.com/o/r/pull/9"}]}
    assert to_record(payload).pull_requests == ("https://github.com/o/r/pull/9",)


def test_an_archived_session_is_recognised():
    """Devin keeps returning archived sessions from GET /sessions, so the
    collector has to filter them itself. If this field stops being read, a
    retired verdict silently comes back and is re-applied to its issue."""
    assert not to_record(REAL_SHAPE).archived
    assert to_record({**REAL_SHAPE, "is_archived": True}).archived


def test_archived_is_not_persisted_as_a_column():
    """It is a reason not to write the row, not a property of the row."""
    assert "archived" not in to_record(REAL_SHAPE).to_row()
