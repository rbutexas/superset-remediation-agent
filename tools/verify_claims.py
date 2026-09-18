#!/usr/bin/env python3
"""
Verify every factual claim used in the filed issues.

Each claim is a named check with an explicit expected value. A claim that does not
pass here does not go into an issue. Re-run this any time to re-prove the issue set.

Sources: the checked-out repo, the npm registry, the OSV API, the GitHub API.

Usage:
    python3 tools/verify_claims.py [--repo /path/to/superset] [--json]

Needs GITHUB_TOKEN in the environment (or ~/.devin.env) for the GitHub checks;
without it those checks report SKIP rather than failing.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
RESULTS: list[dict] = []


# ---------------------------------------------------------------- infrastructure

def record(issue: str, claim: str, status: str, actual: str, expected: str = "") -> None:
    RESULTS.append(
        {"issue": issue, "claim": claim, "status": status,
         "expected": expected, "actual": actual}
    )


def check(issue: str, claim: str, expected, actual) -> None:
    """PASS when actual matches expected. `expected` may be a value or a predicate."""
    if callable(expected):
        ok, exp_desc = expected(actual), getattr(expected, "__doc__", "predicate")
    else:
        ok, exp_desc = actual == expected, repr(expected)
    record(issue, claim, "PASS" if ok else "FAIL", repr(actual), exp_desc)


def get_json(url: str, token: str | None = None):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def post_json(url: str, payload: dict):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def npm(pkg: str) -> dict:
    return get_json(f"https://registry.npmjs.org/{urllib.parse.quote(pkg, safe='@')}")


def load_env() -> None:
    """Populate GITHUB_TOKEN from ~/.devin.env if it is not already set."""
    if os.environ.get("GITHUB_TOKEN"):
        return
    p = pathlib.Path.home() / ".devin.env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        if line.startswith("GITHUB_TOKEN="):
            os.environ["GITHUB_TOKEN"] = line.split("=", 1)[1].strip()


# ---------------------------------------------------------------- detector

USER_EVENT_ACTIONS = (
    "click|type|clear|hover|unhover|dblClick|tripleClick|selectOptions|"
    "deselectOptions|upload|tab|keyboard|paste|pointer"
)
CALL_RE = re.compile(rf"(?<![\w.])userEvent\s*\.\s*({USER_EVENT_ACTIONS})\s*\(")
AWAITED_RE = re.compile(r"(await|return|=>|=|\.then|Promise\.all\(|\[)\s*$")


def scan_unawaited_user_event(frontend: pathlib.Path) -> dict:
    """Find userEvent action calls in statement position that are not awaited.

    high_risk = the next meaningful statement is a *synchronous* assertion or query,
    which can therefore run against pre-event DOM state.
    """
    files: set[str] = set()
    high_risk_files: set[str] = set()
    sites = high_risk = scanned = 0

    for path in frontend.rglob("*.test.*"):
        if "node_modules" in path.parts or path.suffix not in (".ts", ".tsx", ".js", ".jsx"):
            continue
        scanned += 1
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "userEvent" not in src:
            continue

        lines = src.splitlines()
        for i, line in enumerate(lines):
            for m in CALL_RE.finditer(line):
                before = line[: m.start()].rstrip()
                if AWAITED_RE.search(before) or before.endswith(("(", ",")):
                    continue

                rel = str(path.relative_to(frontend))
                sites += 1
                files.add(rel)

                nxt = ""
                for j in range(i + 1, min(i + 6, len(lines))):
                    s = lines[j].strip()
                    if not s or s.startswith(("//", "*")):
                        continue
                    nxt = s
                    break

                sync_assert = re.match(r"expect\(", nxt) and "await" not in nxt
                sync_query = re.search(r"screen\.get(By|All)", nxt) and "await" not in nxt
                if sync_assert or sync_query:
                    high_risk += 1
                    high_risk_files.add(rel)

    return {
        "test_files_scanned": scanned,
        "files": len(files),
        "sites": sites,
        "high_risk_sites": high_risk,
        "high_risk_files": len(high_risk_files),
        "high_risk_file_list": sorted(high_risk_files),
    }


# ---------------------------------------------------------------- checks

def verify_react_checkbox_tree(repo: pathlib.Path, pkg: dict, token: str | None) -> None:
    I = "A/react-checkbox-tree"
    dependabot = (repo / ".github/dependabot.yml").read_text()

    check(I, "dependabot.yml suppresses react-checkbox-tree major updates",
          True, 'dependency-name: "react-checkbox-tree"' in dependabot)

    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    check(I, "package.json pins react-checkbox-tree", "^1.8.0",
          deps.get("react-checkbox-tree"))

    latest = npm("react-checkbox-tree")["dist-tags"]["latest"]
    check(I, "npm latest react-checkbox-tree is a 2.x major",
          True, latest.startswith("2."))
    record(I, "npm latest version", "INFO", latest)

    check(I, "superset already runs the unblocking plugin release",
          "^0.6.3", deps.get("@pmmmwh/react-refresh-webpack-plugin"))

    tree = repo / "superset-frontend/src/dashboard/components/filterscope/FilterScopeTree.tsx"
    check(I, "FilterScopeTree.tsx exists and imports react-checkbox-tree",
          True, tree.exists() and "react-checkbox-tree" in tree.read_text())

    if not token:
        record(I, "GitHub history checks", "SKIP", "no GITHUB_TOKEN")
        return

    gh = "https://api.github.com/repos"
    bump = get_json(f"{gh}/apache/superset/pulls/39261", token)
    check(I, "PR #39261 (bump to 2.0.1) was merged", True, bool(bump["merged_at"]))
    record(I, "  #39261 merged_at", "INFO", bump["merged_at"][:10])

    revert = get_json(f"{gh}/apache/superset/pulls/39660", token)
    check(I, "PR #39660 reverted the bump", True,
          bool(revert["merged_at"]) and "revert" in revert["title"].lower())
    record(I, "  #39660 merged_at", "INFO", revert["merged_at"][:10])

    broke = get_json(f"{gh}/apache/superset/issues/39600", token)
    check(I, "issue #39600 closed as completed (not stale/duplicate)",
          ("closed", "completed"), (broke["state"], broke.get("state_reason")))

    pr940 = get_json(f"{gh}/pmmmwh/react-refresh-webpack-plugin/pulls/940", token)
    check(I, "upstream PR #940 merged", True, bool(pr940["merged_at"]))
    record(I, "  #940 merged_at", "INFO", pr940["merged_at"][:10])

    rel = npm("@pmmmwh/react-refresh-webpack-plugin")
    v063 = rel["time"].get("0.6.3", "")[:10]
    record(I, "  v0.6.3 published", "INFO", v063)
    check(I, "v0.6.3 shipped AFTER PR #940 merged (so it contains the fix)",
          True, v063 > pr940["merged_at"][:10])


def verify_simple_zstd(repo: pathlib.Path, pkg: dict, token: str | None) -> None:
    I = "B/simple-zstd"
    dependabot = (repo / ".github/dependabot.yml").read_text()
    check(I, "dependabot.yml suppresses simple-zstd", True,
          'dependency-name: "simple-zstd"' in dependabot)

    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    check(I, "package.json pins simple-zstd", "^1.4.2", deps.get("simple-zstd"))

    meta = npm("simple-zstd")
    record(I, "npm latest simple-zstd", "INFO", meta["dist-tags"]["latest"])
    record(I, "1.4.2 published", "INFO", meta["time"].get("1.4.2", "?")[:10])
    check(I, "a 2.x major exists", True, meta["dist-tags"]["latest"].startswith("2."))

    readme = meta["versions"][meta["dist-tags"]["latest"]].get("readme", "") or meta.get("readme", "")
    check(I, "v2 README maps the old ZSTDDecompress() onto the new decompress()",
          True, bool(re.search(r"`ZSTDDecompress\(\)`\s*\|\s*`decompress\(opts\?\)`", readme)))
    check(I, "v2 decompress() returns a Promise (i.e. the call must be awaited)",
          True, bool(re.search(r"decompress\(opts\?:\s*ZSTDOpts\):\s*Promise<Duplex>", readme)))

    proxy = repo / "superset-frontend/webpack.proxy-config.js"
    src = proxy.read_text()
    check(I, "proxy still imports the v1 ZSTDDecompress API", True,
          "import { ZSTDDecompress } from 'simple-zstd'" in src)
    check(I, "proxy carries the hand-rolled child-process workaround", True,
          "zstdChild" in src and "killZstdChild" in src)

    test = repo / "superset-frontend/tools/webpack.proxy-config.test.js"
    tsrc = test.read_text() if test.exists() else ""
    check(I, "an existing test covers the zstd decompression path", True,
          "zstd/gzip HTML decompression" in tsrc)
    check(I, "an existing test covers the mid-response abort (the leak case)", True,
          "fails fast instead of hanging" in tsrc)

    if not token:
        record(I, "GitHub history checks", "SKIP", "no GITHUB_TOKEN")
        return
    gh = "https://api.github.com/repos/apache/superset"
    for num, label in ((38662, "bot bump"), (39138, "fix attempt"), (39139, "revert")):
        d = get_json(f"{gh}/issues/{num}", token)
        record(I, f"  #{num} ({label})", "INFO", f"{d['state']} — {d['title'][:60]}")


def verify_user_event(repo: pathlib.Path, pkg: dict) -> None:
    I = "C/user-event"
    frontend = repo / "superset-frontend"
    scan = scan_unawaited_user_event(frontend)

    record(I, "test files scanned", "INFO", scan["test_files_scanned"])
    record(I, "files containing un-awaited userEvent calls", "INFO", scan["files"])
    record(I, "un-awaited call sites", "INFO", scan["sites"])
    record(I, "HIGH-RISK sites (sync assertion follows)", "INFO", scan["high_risk_sites"])
    record(I, "HIGH-RISK files", "INFO", scan["high_risk_files"])

    check(I, "the defect is present at all", True, scan["files"] > 0)
    check(I, "a high-risk subset exists", True, scan["high_risk_sites"] > 0)

    dev = pkg.get("devDependencies", {})
    check(I, "user-event is on v14 (where these APIs became async)",
          True, str(dev.get("@testing-library/user-event", "")).startswith("^14"))

    check(I, "eslint-plugin-testing-library is already a devDependency",
          True, "eslint-plugin-testing-library" in dev)
    record(I, "  installed version", "INFO", dev.get("eslint-plugin-testing-library"))

    eslint = (frontend / "eslint.config.minimal.js")
    esrc = eslint.read_text() if eslint.exists() else ""
    check(I, "…but it is NOT wired into the eslint config",
          True, "testing-library" not in esrc)

    ox = (frontend / "oxlint.json")
    osrc = ox.read_text() if ox.exists() else ""
    check(I, "…and testing-library is NOT in oxlint's plugin list",
          True, '"testing-library"' not in osrc)
    check(I, "…and await-async-events is configured nowhere",
          True, "await-async-events" not in esrc + osrc)


def verify_babel(repo: pathlib.Path, pkg: dict) -> None:
    I = "D/babel-8"
    dependabot = (repo / ".github/dependabot.yml").read_text()
    check(I, "dependabot.yml suppresses @babel/* majors", True,
          'dependency-name: "@babel/*"' in dependabot)

    dev = pkg.get("devDependencies", {})
    check(I, "superset is on @babel/core 7.x", True,
          str(dev.get("@babel/core", "")).startswith("^7."))
    record(I, "  pinned", "INFO", dev.get("@babel/core"))

    latest = npm("@babel/core")["dist-tags"]["latest"]
    check(I, "@babel/core 8.x is available", True, latest.startswith("8."))
    record(I, "  npm latest", "INFO", latest)

    cfg = (repo / "superset-frontend/babel.config.js").read_text()
    check(I, "@emotion/babel-plugin is actually used in babel.config.js",
          True, "@emotion/babel-plugin" in cfg)
    check(I, "babel-plugin-jsx-remove-data-test-id is actually used in babel.config.js",
          True, "babel-plugin-jsx-remove-data-test-id" in cfg)

    blocker = npm("babel-plugin-jsx-remove-data-test-id")
    bl = blocker["dist-tags"]["latest"]
    peer = blocker["versions"][bl].get("peerDependencies", {}).get("@babel/core")
    check(I, "the blocking plugin caps @babel/core at ^7", "^7.0.0", peer)
    record(I, "  blocker latest version", "INFO", bl)
    record(I, "  blocker last published", "INFO", blocker["time"].get(bl, "?")[:10])


def verify_xlsx(pkg: dict) -> None:
    I = "E/xlsx"
    deps = pkg.get("dependencies", {})
    spec = deps.get("xlsx", "")
    check(I, "xlsx is installed from the vendor CDN, not npm",
          True, spec.startswith("https://cdn.sheetjs.com/"))
    record(I, "  install spec", "INFO", spec)

    m = re.search(r"xlsx-(\d+\.\d+\.\d+)\.tgz", spec)
    installed = m.group(1) if m else "?"
    record(I, "  installed version", "INFO", installed)

    npm_latest = npm("xlsx")["dist-tags"]["latest"]
    record(I, "  npm registry latest", "INFO", npm_latest)
    check(I, "npm's latest is OLDER than what superset installs",
          True, npm_latest < installed)

    for vid, fix in (("GHSA-4r6h-8v6p-xvw6", "0.19.3"), ("GHSA-5pgg-2g8v-p4x9", "0.20.2")):
        d = get_json(f"https://api.osv.dev/v1/vulns/{vid}")
        aff = d["affected"][0]
        events = aff["ranges"][0]["events"]
        check(I, f"{vid}: range is unbounded (no fixed event)",
              True, events == [{"introduced": "0"}])
        lka = aff.get("database_specific", {}).get("last_known_affected_version_range", "")
        check(I, f"{vid}: advisory notes the real fix as < {fix}", f"< {fix}", lka)
        check(I, f"{vid}: superset's version is at or above that fix",
              True, installed >= fix)
        check(I, f"{vid}: npm's version is BELOW that fix (downgrade hazard)",
              True, npm_latest < fix)


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/tmp/ss",
                    help="path to a checkout of apache/superset (default: /tmp/ss)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON results")
    args = ap.parse_args()

    repo = pathlib.Path(args.repo)
    if not (repo / ".github/dependabot.yml").exists():
        print(f"ERROR: {repo} is not a superset checkout.\n"
              f"  git clone --depth 1 https://github.com/apache/superset.git {repo}",
              file=sys.stderr)
        return 2

    load_env()
    token = os.environ.get("GITHUB_TOKEN")
    pkg = json.loads((repo / "superset-frontend/package.json").read_text())

    for fn in (
        lambda: verify_react_checkbox_tree(repo, pkg, token),
        lambda: verify_simple_zstd(repo, pkg, token),
        lambda: verify_user_event(repo, pkg),
        lambda: verify_babel(repo, pkg),
        lambda: verify_xlsx(pkg),
    ):
        try:
            fn()
        except Exception as exc:  # a broken check must not hide the others
            record("?", f"check raised {type(exc).__name__}", "ERROR", str(exc)[:160])

    n_fail = sum(1 for r in RESULTS if r["status"] in ("FAIL", "ERROR"))

    if args.json:
        print(json.dumps(RESULTS, indent=2))
        return 1 if n_fail else 0
    else:
        current = None
        for r in RESULTS:
            if r["issue"] != current:
                current = r["issue"]
                print(f"\n{'=' * 78}\n{current}\n{'=' * 78}")
            mark = {"PASS": "  ok  ", "FAIL": " FAIL ",
                    "INFO": " info ", "SKIP": " skip ", "ERROR": "ERROR "}[r["status"]]
            print(f"[{mark}] {r['claim']}")
            if r["status"] == "FAIL":
                print(f"           expected: {r['expected']}")
                print(f"           actual:   {r['actual']}")
            elif r["status"] == "INFO":
                print(f"           {r['actual']}")

    n_pass = sum(1 for r in RESULTS if r["status"] == "PASS")
    print(f"\n{'=' * 78}")
    print(f"{n_pass} passed, {n_fail} failed/errored, "
          f"{sum(1 for r in RESULTS if r['status'] == 'SKIP')} skipped")
    print("=" * 78)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
