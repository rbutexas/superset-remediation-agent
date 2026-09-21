#!/usr/bin/env python3
"""One capped triage session, to establish what this costs before scaling.

Answers four questions in priority order:
  1. What does a triage session actually cost in credits?
  2. Can a session reach the network and read the repository?
  3. Does structured output come back as valid JSON matching our schema?
  4. Is the verdict sensible?

Deliberately does not touch GitHub. A session needs a prompt, not an issue, so
the demo issues stay unfiled.

Usage:
    python3 tools/calibrate.py --repo-path ./work/superset --finding xlsx [--cap 15]
    python3 tools/calibrate.py --repo-path ./work/superset --finding xlsx --dry-run
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from remediation_agent.config import Config                      # noqa: E402
from remediation_agent.devin import DevinClient, to_record       # noqa: E402
from remediation_agent.playbooks import (                        # noqa: E402
    TRIAGE_BODY, TRIAGE_TITLE, triage_prompt,
)
from remediation_agent.schema import TRIAGE_SCHEMA, validate_minimal  # noqa: E402
from remediation_agent.scanner import run_detectors              # noqa: E402

RULE = "=" * 78


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-path", default="./work/superset")
    ap.add_argument("--finding", required=True,
                    help="substring matching the finding key, e.g. 'xlsx'")
    ap.add_argument("--cap", type=int, default=15, help="hard ACU limit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=2400)
    args = ap.parse_args()

    cfg = Config.from_env(dry_run=args.dry_run)
    print(RULE, "\nconfig:", json.dumps(cfg.redacted(), indent=2), "\n" + RULE)

    scan = run_detectors(pathlib.Path(args.repo_path))
    print(f"\nscan: {scan.summary()}")
    if scan.degraded:
        print("REFUSING to calibrate on a degraded scan — the finding may be stale.")
        return 2

    matches = [f for f in scan.findings if args.finding in f.key]
    if len(matches) != 1:
        print(f"\n--finding {args.finding!r} matched {len(matches)}: "
              f"{[f.key for f in scan.findings]}")
        return 2
    finding = matches[0]

    prompt = triage_prompt(finding, None, cfg.repo)
    print(f"\n{RULE}\nFINDING: {finding.key}\n{RULE}")
    print(f"guardrails      : {len(finding.guardrails)}")
    print(f"open questions  : {len(finding.open_questions)}")
    print(f"scanner hint    : {finding.scanner_hint}  (not used for routing)")
    print(f"prompt          : {len(prompt)} chars")
    print(f"\n{RULE}\nPROMPT AS SENT\n{RULE}\n{prompt}")

    if args.dry_run:
        print(f"{RULE}\ndry run — no session created, no credits spent\n{RULE}")
        return 0

    devin = DevinClient(cfg)
    who = devin.whoami()
    print(f"{RULE}\nauthenticated as {who.get('service_user_name')} "
          f"in {who.get('org_id')}\n{RULE}")

    print("\nupserting the triage playbook (config, not compute — free)...")
    playbook = devin.upsert_playbook(TRIAGE_TITLE, TRIAGE_BODY,
                                     output_schema=TRIAGE_SCHEMA)
    playbook_id = playbook.get("playbook_id") or playbook.get("id")
    print(f"  playbook: {playbook_id}")

    print(f"\ncreating ONE triage session, hard cap {args.cap} ACU...")
    session = devin.create_session(
        prompt,
        title=f"CALIBRATION triage: {finding.key}",
        repos=[cfg.repo],
        tags=["calibration", f"finding:{finding.key}", "stage:triage"],
        playbook_id=playbook_id,
        output_schema=TRIAGE_SCHEMA,
        max_acu=args.cap,
    )
    sid, url = session["session_id"], session.get("url", "")
    print(f"\n  session : {sid}\n  watch   : {url}\n")

    # ---- poll (reads are unmetered) --------------------------------------
    started, last = time.time(), None
    record = None
    while time.time() - started < args.timeout:
        record = to_record(devin.get_session(sid))
        state = (record.status, record.status_detail, round(record.acus, 2))
        if state != last:
            mins = int(time.time() - started) // 60
            print(f"  [{mins:>3}m] {record.status}/{record.status_detail} "
                  f"— {record.acus:.2f} ACU")
            last = state
        if record.is_terminal:
            if record.needs_human:
                print("  (answered, then idled — structured output is present, "
                      "so this is complete, not stalled)")
            break
        if record.is_stalled:
            print("\n  !! stalled: waiting on a person with no output yet. "
                  "Not replying — a reply restarts metered work.")
            break
        time.sleep(30)

    if record is None:
        print("no session state retrieved")
        return 1

    # ---- results ----------------------------------------------------------
    print(f"\n{RULE}\nRESULT\n{RULE}")
    print(f"status        : {record.status} / {record.status_detail}")
    print(f"COST          : {record.acus:.2f} ACU")
    print(f"wall clock    : {int(time.time() - started) // 60} min")

    out = record.structured_output
    if not out:
        print("\nstructured output: NONE — the analytics layer cannot rely on this.")
        return 1

    problems = validate_minimal(out, TRIAGE_SCHEMA)
    print(f"schema        : {'VALID' if not problems else 'INVALID ' + str(problems)}")
    print(f"\ndecision      : {out.get('decision')}")
    print(f"confidence    : {out.get('confidence')}")
    print(f"hint agreed   : {out.get('decision') == (finding.scanner_hint or '')}")
    print(f"\nreasoning:\n{out.get('reasoning', '')}\n")

    for label, key in (("evidence independently checked", "evidence_checked"),
                       ("scanner claims contradicted", "contradicted_evidence")):
        items = out.get(key) or []
        if items:
            print(f"{label}:")
            for item in items:
                print(f"  - {item}")
            print()

    artefact = pathlib.Path("data") / f"calibration-{finding.key.replace(':', '_')}.json"
    artefact.parent.mkdir(exist_ok=True)
    artefact.write_text(json.dumps(
        {"session_id": sid, "url": url, "acus": record.acus,
         "status": record.status, "status_detail": record.status_detail,
         "structured_output": out, "schema_problems": problems},
        indent=2))
    print(f"saved: {artefact}")

    print(f"\n{RULE}")
    print(f"EXTRAPOLATION: 4 findings x triage @ {record.acus:.1f} = "
          f"{record.acus * 4:.0f} ACU")
    print(f"               plus remediation sessions, which cost more")
    print(RULE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
