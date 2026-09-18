"""Tests for persistence and the event log.

The behaviour that matters here is idempotency. The collector polls on a timer,
so almost every observation is identical to the last one — if those produced
events the log would be noise, and if they reset `first_seen` the time-to-verdict
numbers would always read zero.
"""

from __future__ import annotations

import pytest

from remediation_agent.models import (
    RemediationOutcome,
    SessionRecord,
    Stage,
    TriageDecision,
)
from remediation_agent.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


def make_session(**overrides) -> SessionRecord:
    base = dict(
        session_id="sess-1",
        stage=Stage.TRIAGE,
        finding_key="f:1",
        issue_number=7,
        status="running",
        status_detail="working",
        acus=0.0,
        url="https://app.devin.ai/sessions/sess-1",
    )
    base.update(overrides)
    return SessionRecord(**base)


# ---------------------------------------------------------------- findings

def test_first_upsert_is_new_second_is_not(store):
    assert store.upsert_finding("f:1", "t", "d", "high") is True
    assert store.upsert_finding("f:1", "t", "d", "high") is False


def test_first_seen_survives_rescans(store):
    """Time-to-verdict is measured from first detection. A weekly re-scan must
    not reset the clock, or the metric always reads zero."""
    store.upsert_finding("f:1", "original", "d", "high")
    original = store.finding("f:1")["first_seen"]

    store.upsert_finding("f:1", "retitled", "d", "low")
    after = store.finding("f:1")

    assert after["first_seen"] == original
    assert after["title"] == "retitled"
    assert after["last_seen"] >= original


def test_detection_is_logged_once_not_per_scan(store):
    for _ in range(4):
        store.upsert_finding("f:1", "t", "d", "high")

    detected = [e for e in store.events("f:1") if e["kind"] == "finding.detected"]
    assert len(detected) == 1


# ---------------------------------------------------------------- sessions

def test_unchanged_poll_produces_no_events(store):
    record = make_session()
    assert store.record_session(record)          # first sighting is a transition
    assert store.record_session(record) == []    # identical poll is silent
    assert store.record_session(record) == []


def test_status_change_is_recorded(store):
    store.record_session(make_session())
    transitions = store.record_session(make_session(status_detail="finished"))

    assert any("working -> " in t for t in transitions)


def test_triage_decision_logged_once(store):
    store.record_session(make_session())
    first = store.record_session(make_session(
        triage_decision=TriageDecision.DECLINE_NOT_ACTIONABLE,
        structured_output={"decision": "decline_not_actionable"},
    ))
    second = store.record_session(make_session(
        triage_decision=TriageDecision.DECLINE_NOT_ACTIONABLE,
        structured_output={"decision": "decline_not_actionable"},
    ))

    assert any("triage decided" in t for t in first)
    assert not any("triage decided" in t for t in second)


def test_acus_never_decrease(store):
    """Consumption is reported with a lag and can come back lower on a later
    poll. Taking the max stops the cost figure oscillating downwards."""
    store.record_session(make_session(acus=4.0))
    store.record_session(make_session(acus=0.0))

    assert store.sessions()[0]["acus"] == 4.0


def test_completed_at_is_set_once_and_kept(store):
    store.record_session(make_session())
    assert store.sessions()[0]["completed_at"] is None

    store.record_session(make_session(
        status_detail="waiting_for_user",
        structured_output={"decision": "remediate"},
        triage_decision=TriageDecision.REMEDIATE,
    ))
    completed = store.sessions()[0]["completed_at"]
    assert completed is not None

    store.record_session(make_session(
        status_detail="waiting_for_user",
        structured_output={"decision": "remediate"},
        triage_decision=TriageDecision.REMEDIATE,
    ))
    assert store.sessions()[0]["completed_at"] == completed


def test_structured_output_is_not_lost_by_a_later_empty_poll(store):
    """A payload that omits structured_output must not erase what we already
    have — the analytics are built on it."""
    store.record_session(make_session(structured_output={"decision": "remediate"}))
    store.record_session(make_session(structured_output=None))

    assert Store.output(store.sessions()[0])["decision"] == "remediate"


def test_two_stages_of_one_finding_are_separate_rows(store):
    store.record_session(make_session(session_id="a", stage=Stage.TRIAGE))
    store.record_session(make_session(
        session_id="b", stage=Stage.REMEDIATION,
        remediation_outcome=RemediationOutcome.FIXED))

    assert len(store.sessions_for("f:1")) == 2
    assert store.latest_session("f:1", "remediation")["session_id"] == "b"
    assert store.latest_session("f:1", "triage")["session_id"] == "a"


def test_counts_roll_up(store):
    store.upsert_finding("f:1", "t", "d", "high")
    store.attach_issue("f:1", 7, "https://github.com/x/y/issues/7")
    store.record_session(make_session(acus=2.5))
    store.record_session(make_session(session_id="s2", acus=1.5))

    counts = store.counts()
    assert counts["findings"] == 1
    assert counts["issues_filed"] == 1
    assert counts["sessions"] == 2
    assert counts["total_acus"] == 4.0


def test_state_survives_reopening(tmp_path):
    """Devin has no outbound webhook, so a collector that forgets loses history
    permanently — there is nothing to replay from."""
    path = tmp_path / "t.db"
    with Store(path) as s:
        s.upsert_finding("f:1", "t", "d", "high")
        s.record_session(make_session(acus=3.0))

    with Store(path) as s:
        assert s.counts() == {
            "findings": 1, "issues_filed": 0,
            "sessions": 1, "sessions_complete": 0, "total_acus": 3.0,
        }
