"""How a finding's state is derived from its sessions.

The cases here are the ones where a finding has more than one session for the
same stage, or a work session that is not a remediation. Both used to be
mis-read, and both are silent: the board renders a confident, wrong answer.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from remediation_agent.models import (
    RemediationOutcome, SessionRecord, Stage, TriageDecision,
)
from remediation_agent.report import build
from remediation_agent.store import Store


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        with Store(pathlib.Path(tmp) / "t.db") as s:
            s.upsert_finding("adv:xlsx", "advisories", "unresolvable-advisory",
                             "medium", "decline_not_actionable", "triage", [])
            s.attach_issue("adv:xlsx", 1, "https://github.com/o/r/issues/1")
            yield s


def session(store, sid, stage, *, first_seen, triage=None, outcome=None,
            output=None, prs=()):
    store.record_session(SessionRecord(
        session_id=sid, stage=stage, finding_key="adv:xlsx", issue_number=1,
        status="exit", status_detail=None, acus=0.0,
        url=f"https://app.devin.ai/sessions/{sid}",
        triage_decision=triage, remediation_outcome=outcome,
        structured_output=output or {"ok": True}, pull_requests=prs,
    ))
    store.conn.execute("UPDATE sessions SET first_seen=?, completed_at=? "
                       "WHERE session_id=?", (first_seen, first_seen, sid))
    store.conn.commit()


def only(store):
    rows = build(store).findings
    assert len(rows) == 1
    return rows[0]


# --------------------------------------------------------- re-triaged findings

def test_the_verdict_in_force_is_the_latest_one(store):
    """A finding can be triaged twice — re-labelled after a policy change, or
    re-run because the first verdict was one the pipeline could not act on. The
    board must show the current answer, not the first one ever reached."""
    session(store, "old", Stage.TRIAGE, first_seen=100,
            triage=TriageDecision.DECLINE_NOT_ACTIONABLE)
    session(store, "new", Stage.TRIAGE, first_seen=200,
            triage=TriageDecision.DOCUMENT_ONLY)

    assert only(store).triage == "document_only"


def test_the_latest_work_session_wins_too(store):
    session(store, "t", Stage.TRIAGE, first_seen=100,
            triage=TriageDecision.REMEDIATE)
    session(store, "r1", Stage.REMEDIATION, first_seen=200,
            outcome=RemediationOutcome.FAILED_VERIFICATION)
    session(store, "r2", Stage.REMEDIATION, first_seen=300,
            outcome=RemediationOutcome.FIXED)

    assert only(store).outcome == "fixed"


# ------------------------------------------------------- the restricted stage

def test_a_documentation_session_reports_its_outcome_and_pr(store):
    """Matching only Stage.REMEDIATION here made a documented finding render as
    though it had never left triage — no outcome, no PR, and not counted as
    resolved."""
    session(store, "t", Stage.TRIAGE, first_seen=100,
            triage=TriageDecision.DOCUMENT_ONLY)
    session(store, "d", Stage.DOCUMENTATION, first_seen=200,
            outcome=RemediationOutcome.MITIGATED,
            prs=("https://github.com/o/r/pull/9",))

    row = only(store)
    assert row.outcome == "mitigated"
    assert row.prs == ["https://github.com/o/r/pull/9"]
    assert row.resolved
    assert row.status == "mitigated"


def test_document_only_reads_as_in_progress_before_its_session_runs(store):
    """Between the verdict and the PR the finding is not resolved, and it is not
    'remediating' either — nothing is being fixed."""
    session(store, "t", Stage.TRIAGE, first_seen=100,
            triage=TriageDecision.DOCUMENT_ONLY)

    row = only(store)
    assert row.status == "documenting"
    assert not row.resolved


def test_a_dismissal_is_still_resolved_with_no_work_session(store):
    session(store, "t", Stage.TRIAGE, first_seen=100,
            triage=TriageDecision.DECLINE_NOT_ACTIONABLE)

    row = only(store)
    assert row.resolved
    assert row.status == "decline_not_actionable"


def test_an_unknown_stage_does_not_break_the_board(store):
    """The store holds whatever a past version wrote."""
    session(store, "t", Stage.TRIAGE, first_seen=100,
            triage=TriageDecision.REMEDIATE)
    store.conn.execute("UPDATE sessions SET stage='revalidation' "
                       "WHERE session_id='t'")
    store.conn.commit()

    assert only(store).status == "awaiting triage"
