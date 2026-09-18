"""Detect un-awaited `userEvent` calls left behind by the user-event v14 upgrade.

Why this exists: in @testing-library/user-event v12 the action APIs were
synchronous. In v14 every one of them returns a promise. Calls that were correct
before the upgrade became races afterwards, and they do not fail loudly — they
fail intermittently, which is worse.

This detector separates two populations, because conflating them overstates the
problem:

  * every un-awaited call site is the same latent defect;
  * only those immediately followed by a *synchronous* assertion or query can
    observe pre-event DOM state, and those are the ones actively producing
    flakes today.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import Iterable

from ..models import Disposition, Evidence, Finding, Severity
from .base import read_json, register

ACTIONS = (
    "click|type|clear|hover|unhover|dblClick|tripleClick|selectOptions|"
    "deselectOptions|upload|tab|keyboard|paste|pointer"
)
CALL_RE = re.compile(rf"(?<![\w.])userEvent\s*\.\s*({ACTIONS})\s*\(")

# Contexts where the call is already handled: awaited, returned, assigned,
# chained, or passed as an argument.
HANDLED_RE = re.compile(r"(await|return|=>|=|\.then|Promise\.all\(|\[)\s*$")

SYNC_ASSERT_RE = re.compile(r"^expect\(")
SYNC_QUERY_RE = re.compile(r"screen\.get(By|All)")

TEST_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx"})


@dataclass(frozen=True, slots=True)
class Site:
    path: str
    line: int
    text: str
    high_risk: bool


def _next_statement(lines: list[str], index: int) -> str:
    for candidate in lines[index + 1: index + 6]:
        stripped = candidate.strip()
        if not stripped or stripped.startswith(("//", "*", "/*")):
            continue
        return stripped
    return ""


def scan(frontend: pathlib.Path) -> tuple[list[Site], int]:
    """Return (sites, number_of_test_files_scanned)."""
    sites: list[Site] = []
    scanned = 0

    for path in sorted(frontend.rglob("*.test.*")):
        if "node_modules" in path.parts or path.suffix not in TEST_SUFFIXES:
            continue
        scanned += 1
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "userEvent" not in source:
            continue

        lines = source.splitlines()
        rel = str(path.relative_to(frontend))

        for i, line in enumerate(lines):
            for match in CALL_RE.finditer(line):
                before = line[: match.start()].rstrip()
                if HANDLED_RE.search(before) or before.endswith(("(", ",")):
                    continue

                nxt = _next_statement(lines, i)
                high_risk = bool(
                    (SYNC_ASSERT_RE.match(nxt) or SYNC_QUERY_RE.search(nxt))
                    and "await" not in nxt
                )
                sites.append(Site(rel, i + 1, line.strip()[:120], high_risk))

    return sites, scanned


class UnawaitedUserEvent:
    name = "unawaited-user-event"

    def detect(self, repo: pathlib.Path) -> Iterable[Finding]:
        frontend = repo / "superset-frontend"
        if not frontend.is_dir():
            return []

        sites, scanned = scan(frontend)
        if not sites:
            return []

        high = [s for s in sites if s.high_risk]
        files = sorted({s.path for s in sites})
        high_files = sorted({s.path for s in high})

        pkg = read_json(frontend / "package.json")
        dev = pkg.get("devDependencies", {})
        user_event_version = dev.get("@testing-library/user-event", "?")
        plugin_version = dev.get("eslint-plugin-testing-library")

        # Is the rule that catches this actually switched on anywhere?
        config_text = ""
        for candidate in ("eslint.config.minimal.js", "oxlint.json"):
            path = frontend / candidate
            if path.exists():
                config_text += path.read_text(encoding="utf-8", errors="ignore")
        rule_enabled = "await-async-events" in config_text or "testing-library" in config_text

        evidence = [
            Evidence("Test files scanned", str(scanned),
                     "superset-frontend/**/*.test.*"),
            Evidence("Files with un-awaited userEvent calls", str(len(files)),
                     f"detector:{self.name}"),
            Evidence("Un-awaited call sites", str(len(sites)),
                     f"detector:{self.name}"),
            Evidence("High-risk sites (next statement is a synchronous assertion)",
                     str(len(high)), f"detector:{self.name}"),
            Evidence("High-risk files", str(len(high_files)),
                     f"detector:{self.name}"),
            Evidence("@testing-library/user-event", user_event_version,
                     "superset-frontend/package.json"),
        ]

        guardrails = [
            "Do not blanket-insert `await`. Calls inside non-async callbacks need the "
            "enclosing function changed first.",
            "Where awaiting reveals that an assertion was passing *because* of the race, "
            "fix the assertion and say so explicitly in the PR — do not silently change "
            "what a test means.",
            "Do not enable the lint rule repo-wide as `error` in the same change; that "
            f"fails CI on all {len(sites)} sites at once.",
        ]

        acceptance = [
            f"All {len(high)} high-risk sites across {len(high_files)} files corrected.",
            "`await-async-events` enabled in a way that leaves CI green — warn "
            "repo-wide, or error on corrected paths with the remainder tracked.",
            "A follow-up issue filed listing the remaining latent sites.",
            "Any changed test expectation called out in the PR description.",
        ]

        if plugin_version and not rule_enabled:
            evidence.append(Evidence(
                "eslint-plugin-testing-library installed but NOT enabled",
                f"{plugin_version} — `await-async-events` appears in no lint config",
                "superset-frontend/package.json, eslint.config.minimal.js, oxlint.json",
            ))
            summary_tail = (
                f"\n\nThe guardrail is already paid for and switched off. "
                f"`eslint-plugin-testing-library@{plugin_version}` is a declared "
                f"devDependency and ships `await-async-events`, which detects exactly "
                f"this. It is referenced in neither `eslint.config.minimal.js` nor "
                f"`oxlint.json`, so the rule that would have caught all {len(sites)} "
                f"sites at review time has never been turned on."
            )
        else:
            summary_tail = ""

        sample = "\n".join(
            f"{s.path}:{s.line}\n    {s.text}" for s in high[:5]
        )

        summary = (
            f"`@testing-library/user-event` is on `{user_event_version}`. In v14 every "
            f"action API returns a promise; in v12 they were synchronous. Calls that "
            f"were correct before the upgrade are races after it.\n\n"
            f"A scan of {scanned} frontend test files finds **{len(sites)} un-awaited "
            f"call sites across {len(files)} files**. Of those, **{len(high)} sites in "
            f"{len(high_files)} files** are immediately followed by a synchronous "
            f"assertion or query, so the assertion can run against pre-event DOM state. "
            f"Those are the ones producing flakes today; the remainder are latent — "
            f"followed by an `await` that incidentally flushes the event loop.\n\n"
            f"Highest-risk examples:\n\n```\n{sample}\n```"
            f"{summary_tail}"
        )

        return [Finding(
            key="unawaited-user-event",
            title=(f"test(frontend): {len(sites)} un-awaited userEvent calls across "
                   f"{len(files)} files, and the lint rule that catches them is disabled"),
            summary=summary,
            detector=self.name,
            disposition=Disposition.REMEDIATE,
            severity=Severity.HIGH if high else Severity.MEDIUM,
            evidence=tuple(evidence),
            labels=("test-infrastructure", "flaky-tests", "frontend"),
            paths=tuple(high_files),
            acceptance=tuple(acceptance),
            guardrails=tuple(guardrails),
        )]


register(UnawaitedUserEvent())
