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


def test_store_is_usable_from_another_thread(tmp_path):
    """`serve` runs the collector on a background thread while the HTTP server
    answers on others. A connection pinned to its creating thread raises on
    every tick — in a thread nobody is watching — so the pipeline quietly stops
    advancing while appearing healthy."""
    import threading

    store = Store(tmp_path / "t.db")
    errors: list[Exception] = []

    def writer() -> None:
        try:
            store.upsert_finding("f:thread", "t", "d", "high")
            store.record_session(make_session(session_id="from-thread"))
        except Exception as exc:                      # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=writer)
    t.start(); t.join()

    assert not errors, f"store unusable off-thread: {errors[0]}"
    assert store.counts()["findings"] == 1
    store.close()


def test_concurrent_writers_do_not_corrupt(tmp_path):
    import threading

    store = Store(tmp_path / "t.db")
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            for i in range(10):
                store.record_session(make_session(session_id=f"s{n}-{i}"))
        except Exception as exc:                      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, errors[:1]
    assert store.counts()["sessions"] == 40
    store.close()


def test_a_finding_can_be_adopted_from_a_session(store):
    """An issue filed by hand never passes through `file`, so the store has
    never heard of its finding. The session's own report is enough to adopt it —
    otherwise the dashboard, which builds from findings, shows nothing."""
    assert store.finding("f:handfiled") is None

    store.upsert_finding("f:handfiled", "t", "d", "high")
    store.attach_issue("f:handfiled", 2, "https://github.com/o/r/issues/2")

    row = store.finding("f:handfiled")
    assert row["issue_number"] == 2


def test_a_read_only_store_can_be_read(tmp_path):
    """The committed evidence snapshot is mounted read-only into the replay
    container. SQLite cannot open it at all if the connection tries to set a
    pragma or run the schema — it fails as `unable to open database file`,
    naming neither the mount nor the write."""
    import os

    path = tmp_path / "ro.db"
    with Store(path) as s:
        s.upsert_finding("f:1", "recorded", "d", "high")
        s.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    os.chmod(tmp_path, 0o555)          # directory no longer writable
    try:
        store = Store(path)
        assert store.read_only
        assert store.findings()[0]["title"] == "recorded"
        store.close()
    finally:
        os.chmod(tmp_path, 0o755)


def test_a_read_only_store_refuses_writes(tmp_path):
    import os
    import pytest

    path = tmp_path / "ro.db"
    with Store(path) as s:
        s.upsert_finding("f:1", "t", "d", "high")
        s.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    os.chmod(tmp_path, 0o555)
    try:
        store = Store(path)
        with pytest.raises(RuntimeError, match="read-only"):
            store.upsert_finding("f:2", "t", "d", "high")
        store.close()
    finally:
        os.chmod(tmp_path, 0o755)
