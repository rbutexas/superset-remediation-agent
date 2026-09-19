"""Tests for the dashboard and the live view.

Two things are worth pinning. First, that the live and static renders come from
one code path — if they diverge, the thing on screen during a demo is not the
thing in the saved artefact. Second, escaping: issue titles and finding keys are
repository content, and they land in HTML.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from remediation_agent.dashboard import OUTCOME_STYLE, render, render_partial
from remediation_agent.models import SessionRecord, Stage, TriageDecision
from remediation_agent.report import build
from remediation_agent.store import Store


@pytest.fixture
def report():
    with tempfile.TemporaryDirectory() as tmp:
        with Store(pathlib.Path(tmp) / "t.db") as store:
            store.upsert_finding("f:1", "a finding", "det", "high",
                                 "remediate", "triage", ["a reason"])
            store.attach_issue("f:1", 7, "https://github.com/o/r/issues/7")
            store.record_session(SessionRecord(
                session_id="s1", stage=Stage.TRIAGE, finding_key="f:1",
                issue_number=7, status="running", status_detail="working",
                acus=1.0, url="https://app.devin.ai/sessions/s1"))
            yield build(store)


def test_static_render_has_no_script(report):
    """A saved file is opened from disk and emailed around; it should not carry
    a polling loop that will 404 forever."""
    assert "<script" not in render(report)


def test_live_render_polls_and_preserves_scroll(report):
    html = render(report, live=True)
    assert "fetch('/partial'" in html
    assert "window.scrollTo" in html          # a reload would lose the position
    assert "livedot" in html


def test_partial_is_a_fragment(report):
    partial = render_partial(report)
    assert "<html" not in partial
    assert "<script" not in partial           # the shim must not re-inject itself
    assert "<section" in partial


def test_live_and_partial_come_from_one_renderer(report):
    """The fragment must be a literal substring of the full page. If these ever
    diverge, what is on screen during a demo is not what the artefact shows."""
    assert render_partial(report) in render(report, live=True)


def test_work_in_progress_is_described_as_work_not_sessions(report):
    """A VP cares whether the work is moving, not how many agent sessions are
    running. The panel names the finding and what is being done to it; the
    session is only a link."""
    html = render(report, live=True)

    assert "In progress" in html
    assert "f:1" in html
    assert "Being assessed" in html          # not "triage session running"
    assert "watch the agent" in html


def test_refresh_interval_is_honoured(report):
    assert "REFRESH=3000" in render(report, live=True, refresh_seconds=3)


# ------------------------------------------------------------------ escaping

def test_repository_content_is_escaped():
    with tempfile.TemporaryDirectory() as tmp:
        with Store(pathlib.Path(tmp) / "t.db") as store:
            store.upsert_finding(
                "f:<img src=x onerror=alert(1)>",
                '"><script>alert(1)</script>',
                "det", "high")
            html = render(build(store))

    # The property that matters is that no *tag* is emitted. The bare text
    # `onerror=alert(1)` contains no HTML-special characters and correctly
    # survives escaping as inert content — asserting on that substring would
    # be testing the wrong thing.
    assert "<script>alert" not in html       # no injected tag
    assert "<img" not in html
    assert "&lt;script&gt;" in html          # escaped, therefore inert
    assert "&lt;img" in html


# ------------------------------------------------------------------ palette

def test_resolution_outcomes_use_categorical_not_status_colours():
    """`fixed` and `decline_not_actionable` are different kinds of answer, not
    different grades of one. Painting the dismissal amber would assert it was
    second best — the claim this system argues against."""
    fixed_role, _ = OUTCOME_STYLE["fixed"]
    declined_role, _ = OUTCOME_STYLE["decline_not_actionable"]

    assert fixed_role.startswith("series-")
    assert declined_role.startswith("series-")
    assert fixed_role != declined_role


def test_status_colours_are_reserved_for_problems():
    for status in ("STALLED", "failed_verification"):
        assert OUTCOME_STYLE[status][0] == "critical"
    assert OUTCOME_STYLE["escalate_to_human"][0] == "warning"


def test_every_outcome_has_a_human_label():
    """A colour never carries meaning alone — each state ships with words."""
    for status, (_, label) in OUTCOME_STYLE.items():
        assert label and label != status.upper()


def test_dark_mode_is_declared_under_both_scopes(report):
    """The media query covers the OS setting; the data-theme scope covers an
    explicit toggle. Both are needed."""
    html = render(report)
    assert "prefers-color-scheme: dark" in html
    assert '[data-theme="dark"]' in html
