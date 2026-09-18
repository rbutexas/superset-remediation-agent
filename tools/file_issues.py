#!/usr/bin/env python3
"""
File the issue set on the fork, from docs/ISSUES.md as the single source of truth.

Idempotent: an issue whose title already exists is updated in place rather than
duplicated, so this is safe to re-run. Trigger labels (agent:*) are deliberately
NOT applied here — labelling is what starts a Devin session, and that is a separate,
explicit step.

Usage:
    python3 tools/file_issues.py --repo rbutexas/superset [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
SOURCE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "ISSUES.md"

# Descriptive labels used by the issue set, with colours. Trigger labels are created
# too so they exist ahead of Phase 4, but are not attached to anything yet.
LABELS = {
    "dependencies":      ("0366d6", "Dependency version or suppression work"),
    "frontend":          ("1d76db", "superset-frontend"),
    "build":             ("5319e7", "Build tooling and bundling"),
    "dashboard":         ("0e8a16", "Dashboard surface"),
    "test-infrastructure": ("c5def5", "Test harness and CI reliability"),
    "flaky-tests":       ("fbca04", "Non-deterministic test behaviour"),
    "blocked-upstream":  ("b60205", "Blocked by an external project"),
    "security":          ("d93f0b", "Security-related"),
    "triage":            ("bfd4f2", "Needs a determination, not necessarily a fix"),
    "false-positive":    ("cfd3d7", "Scanner finding that is not actionable"),
    "agent:remediate":   ("6f42c1", "TRIGGER: dispatch a Devin remediation session"),
    "agent:triage":      ("8a63d8", "TRIGGER: dispatch a Devin triage session"),
}


# ---------------------------------------------------------------- github

def token() -> str:
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        env = pathlib.Path.home() / ".devin.env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    tok = line.split("=", 1)[1].strip()
                    break
    if not tok:
        sys.exit("ERROR: GITHUB_TOKEN not set and not found in ~/.devin.env")
    return tok


def api(method: str, path: str, tok: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from None


# ---------------------------------------------------------------- parsing

HEADER = re.compile(r"^## Issue (\d+)\s*$", re.M)


def parse_issues(text: str) -> list[dict]:
    """Split ISSUES.md into {number, title, labels, body}."""
    marks = [(m.start(), int(m.group(1))) for m in HEADER.finditer(text)]
    if not marks:
        sys.exit("ERROR: no '## Issue N' headings found in docs/ISSUES.md")

    end_of_last = text.find("\n## Summary")
    if end_of_last == -1:
        end_of_last = len(text)

    issues = []
    for i, (start, num) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else end_of_last
        chunk = text[start:stop]

        t = re.search(r"\*\*Title:\*\*\s*`([^`]+)`", chunk)
        if not t:
            sys.exit(f"ERROR: Issue {num} has no **Title:** line")
        title = t.group(1).strip()

        lab = re.search(r"\*\*Labels:\*\*(.+)", chunk)
        labels = re.findall(r"`([^`]+)`", lab.group(1)) if lab else []

        # body = everything after the first '---' separator that follows the labels line
        sep = chunk.find("\n---\n", lab.end() if lab else t.end())
        body = chunk[sep + 5:].strip() if sep != -1 else chunk[t.end():].strip()

        issues.append({"number": num, "title": title, "labels": labels, "body": body})
    return issues


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="rbutexas/superset")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    issues = parse_issues(SOURCE.read_text())
    print(f"parsed {len(issues)} issues from {SOURCE.name}\n")

    if args.dry_run:
        for it in issues:
            print(f"  [{it['number']}] {it['title']}")
            print(f"        labels: {', '.join(it['labels']) or '(none)'}")
            print(f"        body:   {len(it['body'])} chars, "
                  f"{it['body'].count(chr(10)) + 1} lines")
        print("\ndry run — nothing written")
        return 0

    tok = token()

    # ensure labels exist (ignore 'already exists')
    existing = {l["name"] for l in api("GET", f"/repos/{args.repo}/labels?per_page=100", tok)}
    for name, (colour, desc) in LABELS.items():
        if name in existing:
            continue
        api("POST", f"/repos/{args.repo}/labels", tok,
            {"name": name, "color": colour, "description": desc})
        print(f"  created label: {name}")

    # index open+closed issues by title so re-runs update instead of duplicating
    by_title: dict[str, int] = {}
    page = 1
    while True:
        batch = api("GET", f"/repos/{args.repo}/issues?state=all&per_page=100&page={page}", tok)
        if not batch:
            break
        for it in batch:
            if "pull_request" not in it:
                by_title[it["title"]] = it["number"]
        page += 1

    print()
    for it in issues:
        payload = {"title": it["title"], "body": it["body"], "labels": it["labels"]}
        if it["title"] in by_title:
            n = by_title[it["title"]]
            api("PATCH", f"/repos/{args.repo}/issues/{n}", tok, payload)
            print(f"  updated  #{n}  {it['title'][:66]}")
        else:
            created = api("POST", f"/repos/{args.repo}/issues", tok, payload)
            print(f"  created  #{created['number']}  {it['title'][:66]}")
            print(f"           {created['html_url']}")

    print("\nNote: no agent:* trigger labels were applied. Labelling starts a Devin "
          "session and is a separate, explicit step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
