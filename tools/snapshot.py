#!/usr/bin/env python3
"""Freeze the current store as committed evidence of a real run.

A reviewer without a Devin account can open the dashboard, but their store is
empty, so they see the layout and none of the results. This writes the real one
into `docs/evidence/` so the recorded run can be replayed:

    DB_PATH=docs/evidence/run.db remediation-agent serve --no-collect

Two formats, deliberately:

  * `run.db`   — the SQLite store, so the dashboard renders it unchanged. No
                 import path to maintain, and no chance of the replay diverging
                 from what the live view showed.
  * `run.json` — the same data as text, so it is diffable in review and readable
                 without running anything.

This is a recording, not a fixture. Every row came from a real session; nothing
here is authored. If the two ever disagree, the JSON is the one to read, because
a binary that nobody can inspect is not evidence.

    python3 tools/snapshot.py [--db data/agent.db] [--label "after the xlsx run"]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from remediation_agent.report import build, render_json, render_text  # noqa: E402
from remediation_agent.store import Store                             # noqa: E402

EVIDENCE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "evidence"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/agent.db")
    ap.add_argument("--label", default="", help="what this run was")
    args = ap.parse_args()

    source = pathlib.Path(args.db)
    if not source.exists():
        print(f"no store at {source}", file=sys.stderr)
        return 2

    EVIDENCE.mkdir(parents=True, exist_ok=True)

    with Store(source) as store:
        report = build(store)
        counts = store.counts()
        events = [dict(e) for e in store.events()]

    # The store, verbatim. WAL pages may hold recent writes, so checkpoint first
    # or the copy can be missing the last few rows.
    with Store(source) as store:
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(source, EVIDENCE / "run.db")

    (EVIDENCE / "run.json").write_text(render_json(report), encoding="utf-8")
    (EVIDENCE / "run.txt").write_text(render_text(report), encoding="utf-8")
    (EVIDENCE / "run-events.json").write_text(
        json.dumps({"label": args.label,
                    "captured_at": int(time.time()),
                    "counts": counts,
                    "events": events}, indent=2),
        encoding="utf-8")

    print(f"captured to {EVIDENCE.relative_to(pathlib.Path.cwd())}/")
    for name in ("run.db", "run.json", "run.txt", "run-events.json"):
        size = (EVIDENCE / name).stat().st_size
        print(f"  {name:<18} {size:>7,} bytes")
    print(f"\n  findings {counts['findings']} · sessions {counts['sessions']} · "
          f"events {len(events)}")
    print("\nreplay it with:")
    print("  DB_PATH=docs/evidence/run.db remediation-agent serve --no-collect")
    return 0


if __name__ == "__main__":
    sys.exit(main())
