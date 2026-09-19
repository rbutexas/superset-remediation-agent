#!/usr/bin/env python3
"""Render the dashboard against a throwaway store, to see it with data in it.

Writes to a temporary database, never the real one. The xlsx row mirrors the
actual calibration session; the other three are plausible placeholders so the
layout can be judged before the real run. Nothing here touches the network.

    python3 tools/preview_report.py [--format text|html|json] [--out FILE]
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from remediation_agent.models import (                       # noqa: E402
    RemediationOutcome, SessionRecord, Stage, TriageDecision,
)
from remediation_agent.report import (                       # noqa: E402
    build, render_html, render_json, render_text,
)
from remediation_agent.store import Store                    # noqa: E402

HOUR = 3600


def seed(store: Store) -> None:
    now = int(time.time())

    def finding(key, title, detector, severity, hint, issue, age_hours):
        store.upsert_finding(key, title, detector, severity, hint, "triage",
                             ["stated prohibition(s)", "open question(s)"])
        store.attach_issue(key, issue, f"https://github.com/rbutexas/superset/issues/{issue}")
        store.conn.execute("UPDATE findings SET first_seen=? WHERE key=?",
                           (now - age_hours * HOUR, key))
        store.conn.commit()

    def session(sid, key, stage, issue, *, status="running",
                detail="waiting_for_user", acus=0.0, triage=None, outcome=None,
                output=None, prs=(), done_hours_ago=None):
        store.record_session(SessionRecord(
            session_id=sid, stage=stage, finding_key=key, issue_number=issue,
            status=status, status_detail=detail, acus=acus,
            url=f"https://app.devin.ai/sessions/{sid}",
            triage_decision=triage, remediation_outcome=outcome,
            structured_output=output, pull_requests=prs,
        ))
        if done_hours_ago is not None:
            store.conn.execute(
                "UPDATE sessions SET completed_at=? WHERE session_id=?",
                (now - done_hours_ago * HOUR, sid))
            store.conn.commit()

    # --- 1. remediated, PR open, CI green ---------------------------------
    finding("unfinished-migration:simple-zstd",
            "deps(frontend): simple-zstd is pinned pending a code change that was never made",
            "stale-suppression", "medium", "remediate", 101, age_hours=30)
    session("sess-zstd-t", "unfinished-migration:simple-zstd", Stage.TRIAGE, 101,
            acus=0.9, triage=TriageDecision.REMEDIATE,
            output={"decision": "remediate", "confidence": "high"},
            done_hours_ago=28)
    session("sess-zstd-r", "unfinished-migration:simple-zstd", Stage.REMEDIATION, 101,
            acus=6.4, outcome=RemediationOutcome.FIXED,
            output={"outcome": "fixed",
                    "verification": {"commands_run": ["npm run test -- tools/"],
                                     "all_passed": True}},
            prs=("https://github.com/rbutexas/superset/pull/112",),
            done_hours_ago=26)

    # --- 2. remediated, but honest about what it could not prove ----------
    finding("stale-suppression:react-checkbox-tree",
            "deps(frontend): the react-checkbox-tree suppression's stated conditions are now met",
            "stale-suppression", "medium", "remediate", 102, age_hours=30)
    session("sess-rct-t", "stale-suppression:react-checkbox-tree", Stage.TRIAGE, 102,
            acus=1.1, triage=TriageDecision.REMEDIATE,
            output={"decision": "remediate", "confidence": "medium"},
            done_hours_ago=27)
    session("sess-rct-r", "stale-suppression:react-checkbox-tree", Stage.REMEDIATION, 102,
            acus=8.2, outcome=RemediationOutcome.FIXED,
            output={"outcome": "fixed",
                    "verification": {
                        "commands_run": ["npm run test -- src/dashboard/"],
                        "all_passed": True,
                        "unverifiable": "Could not reproduce the original runtime "
                                        "crash: it needs a production bundle served "
                                        "under Docker, which this session cannot build."}},
            prs=("https://github.com/rbutexas/superset/pull/113",),
            done_hours_ago=24)

    # --- 3. the real calibration result: correctly declined ---------------
    finding("unresolvable-advisory:xlsx",
            "security-triage: xlsx advisories cannot be resolved by upgrade",
            "unresolvable-advisory", "medium", "decline_not_actionable", 103,
            age_hours=30)
    session("f8ff776236eb4dd3b7bed045d2b3745d", "unresolvable-advisory:xlsx",
            Stage.TRIAGE, 103, acus=0.0,
            triage=TriageDecision.DECLINE_NOT_ACTIONABLE,
            output={"decision": "decline_not_actionable", "confidence": "high"},
            done_hours_ago=29)

    # --- 4. stalled: billing, blocked on a person, nobody told ------------
    finding("unawaited-user-event",
            "test(frontend): 205 un-awaited userEvent calls across 52 files",
            "unawaited-user-event", "high", "remediate", 104, age_hours=30)
    session("sess-ue-t", "unawaited-user-event", Stage.TRIAGE, 104,
            acus=1.3, triage=TriageDecision.REMEDIATE,
            output={"decision": "remediate", "confidence": "high"},
            done_hours_ago=25)
    session("sess-ue-r", "unawaited-user-event", Stage.REMEDIATION, 104,
            detail="waiting_for_user", acus=3.1, output=None)

    # --- 5-7. three running at once, to exercise the in-progress list -----
    # The automation caps concurrency at 3, so this is the busiest the panel
    # ever gets; a fourth event queues rather than starting.
    for n, (key, title, stage, issue, detail, started) in enumerate([
        ("unresolvable-advisory:underscore",
         "security-triage: underscore reachable via an unmaintained dependency",
         Stage.TRIAGE, 105, "working", 40),
        ("stale-suppression:deck-gl",
         "deps(frontend): coordinated @deck.gl / @luma.gl bump across workspaces",
         Stage.REMEDIATION, 106, "working", 380),
        ("blocked-upstream:babel-8",
         "build(frontend): Babel 8 blocked by an unmaintained plugin",
         Stage.TRIAGE, 107, "working", 95),
    ]):
        finding(key, title, "stale-suppression", "medium", "remediate",
                issue, age_hours=2)
        session(f"sess-live-{n}", key, stage, issue, detail=detail)
        store.conn.execute(
            "UPDATE sessions SET first_seen=? WHERE session_id=?",
            (now - started, f"sess-live-{n}"))
        store.conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", choices=("text", "html", "json"), default="text")
    ap.add_argument("--out")
    ap.add_argument("--db", help="write the seeded store here and stop, so the "
                                 "live dashboard can serve it")
    args = ap.parse_args()

    if args.db:
        target = pathlib.Path(args.db)
        target.unlink(missing_ok=True)
        with Store(target) as store:
            seed(store)
            counts = store.counts()
        print(f"seeded {target} — {counts['findings']} findings, "
              f"{counts['sessions']} sessions")
        print(f"\n  DB_PATH={target} remediation-agent serve --no-collect\n")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        with Store(pathlib.Path(tmp) / "preview.db") as store:
            seed(store)
            report = build(store)

    rendered = {"text": render_text, "html": render_html,
                "json": render_json}[args.format](report)

    if args.out:
        pathlib.Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"written to {args.out}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
