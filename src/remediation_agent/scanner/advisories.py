"""Find advisories that cannot be resolved by upgrading — and spot the ones where
the obvious remediation is a downgrade.

Most dependency scanners answer "is there an advisory?". That is the easy half.
The half that costs engineering time is "can it be acted on, and is acting on it
safe?" — and for a specific, recurring class of finding the answer to both is no.

The class: a package whose publisher has stopped releasing to the registry. The
advisory database keys `fixed` versions to registry releases, so when the fix
ships elsewhere there is no version to record. The machine-readable range stays
open at `introduced: 0` — "every version, forever" — while the real fix version
appears only in a descriptive field that tools do not parse.

Two consequences, both bad:

  * the finding recurs on every scan and can never be cleared;
  * "reinstall it from the registry" — the obvious fix, and the one an
    unsupervised agent will reach for — resolves to an *older* release than the
    one already installed, moving the project backwards past the fix.

This detector reports both, and attaches a guardrail so the remediation path is
explicitly closed off.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import urllib.request
from typing import Iterable

from ..models import Evidence, Finding, Severity, TriageDecision
from .base import NotInRegistry, npm_metadata, read_json, register

log = logging.getLogger(__name__)

OSV_QUERY = "https://api.osv.dev/v1/query"
CDN_VERSION_RE = re.compile(r"[-/](\d+\.\d+\.\d+)\.tgz$")


def version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in v.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def osv_query(name: str, version: str, ecosystem: str = "npm") -> list[dict]:
    payload = {"package": {"name": name, "ecosystem": ecosystem}, "version": version}
    req = urllib.request.Request(
        OSV_QUERY, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("vulns", [])


def unbounded(vuln: dict, ecosystem: str = "npm") -> tuple[bool, str]:
    """Is the affected range open-ended, and what does the advisory say the real
    fix version is? Returns (is_unbounded, last_known_affected_range)."""
    for affected in vuln.get("affected", []):
        if affected.get("package", {}).get("ecosystem") != ecosystem:
            continue
        for rng in affected.get("ranges", []):
            events = rng.get("events", [])
            has_fix = any("fixed" in e or "last_affected" in e for e in events)
            if not has_fix:
                lka = affected.get("database_specific", {}).get(
                    "last_known_affected_version_range", "")
                return True, lka
    return False, ""


class UnresolvableAdvisory:
    name = "unresolvable-advisory"

    def detect(self, repo: pathlib.Path) -> Iterable[Finding]:
        manifest = repo / "superset-frontend/package.json"
        if not manifest.exists():
            return []

        pkg = read_json(manifest)
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        findings: list[Finding] = []

        # Only packages installed from outside the registry can exhibit this.
        # A registry-installed package with a real fix is ordinary Dependabot work.
        for name, spec in sorted(deps.items()):
            if not spec.startswith(("http://", "https://")):
                continue

            match = CDN_VERSION_RE.search(spec)
            if not match:
                log.info("skipping %s: cannot determine version from %r", name, spec)
                continue
            installed = match.group(1)

            # An OSV failure propagates: "no advisories" and "could not check"
            # must not look the same to the caller.
            vulns = osv_query(name, installed)
            if not vulns:
                continue

            try:
                registry_latest = npm_metadata(name)["dist-tags"]["latest"]
            except NotInRegistry:
                log.info("%s is not on npm at all; no downgrade hazard", name)
                continue

            rows, unresolvable, downgrade_risk = [], [], False
            for vuln in vulns:
                is_open, lka = unbounded(vuln)
                if not is_open:
                    continue
                unresolvable.append(vuln["id"])

                fix = lka.lstrip("< ").strip()
                installed_ok = bool(fix) and version_tuple(installed) >= version_tuple(fix)
                registry_ok = bool(fix) and version_tuple(registry_latest) >= version_tuple(fix)
                if installed_ok and not registry_ok:
                    downgrade_risk = True

                rows.append(
                    f"| `{vuln['id']}` | {', '.join(vuln.get('aliases', [])) or '—'} "
                    f"| `introduced: 0`, no `fixed` event | {lka or 'not stated'} "
                    f"| {'above' if installed_ok else 'BELOW'} |"
                )

            if not unresolvable:
                continue

            table = (
                "| Advisory | Alias | Machine-readable range | Real fix, per advisory | "
                f"`{installed}` |\n|---|---|---|---|---|\n" + "\n".join(rows)
            )

            evidence = [
                Evidence("Install source", spec, "superset-frontend/package.json"),
                Evidence("Installed version", installed, "superset-frontend/package.json"),
                Evidence("Registry latest", registry_latest, f"npm:{name}"),
                Evidence("Advisories with no fixed version",
                         ", ".join(unresolvable), "osv.dev"),
            ]

            guardrails = ["Do not close this by changing the dependency version."]
            if downgrade_risk:
                guardrails.insert(0, (
                    f"**Do not change the install source to the npm registry.** "
                    f"The registry's latest `{name}` is `{registry_latest}`, which is "
                    f"below the fix version. That change would move the project "
                    f"backwards into genuinely affected code while turning the scanner green."
                ))

            summary = (
                f"Dependency scanning flags `{name}@{installed}` against "
                f"{len(unresolvable)} advisor{'y' if len(unresolvable) == 1 else 'ies'} "
                f"that cannot be cleared by any upgrade. Recording a determination so "
                f"this is not re-investigated on every scan.\n\n"
                f"{table}\n\n"
                f"**Why it recurs permanently.** `{name}` is installed from outside the "
                f"npm registry. With no registry release carrying the fix, the advisory "
                f"database has no version to record in the `fixed` field, so the "
                f"machine-readable range stays open at 'all versions'. Scanners read "
                f"that field; the real fix version appears only in "
                f"`last_known_affected_version_range`, which they do not parse.\n\n"
                + (
                    f"**The obvious remediation is a downgrade.** The registry's latest "
                    f"`{name}` is `{registry_latest}` — below the fix version, and below "
                    f"what is installed today. Any automated remediation that "
                    f"'reinstalls from the registry' introduces the vulnerability it "
                    f"claims to resolve.\n\n"
                    if downgrade_risk else ""
                )
                + "No action by this project can clear the finding."
            )

            findings.append(Finding(
                key=f"unresolvable-advisory:{name}",
                title=(f"security-triage: {name} advisories cannot be resolved by "
                       f"upgrade and will recur on every scan"),
                summary=summary,
                detector=self.name,
                scanner_hint=TriageDecision.DECLINE_NOT_ACTIONABLE,
                severity=Severity.MEDIUM if downgrade_risk else Severity.LOW,
                evidence=tuple(evidence),
                labels=("security", "triage", "false-positive"),
                guardrails=tuple(guardrails),
                acceptance=(
                    "A determination recorded, comparing the installed version against "
                    "each advisory's stated fix version.",
                    "A suppression entry carrying that rationale, so the finding stops "
                    "consuming triage attention.",
                    "A guard preventing automated remediation of this package.",
                    "No dependency change.",
                ),
                open_questions=(
                    "Is the installed version genuinely at or above every fix version "
                    "the advisories name? Verify each one independently.",
                    "Is there any remediation that improves the position, or is the "
                    "correct answer to record a determination and move on?",
                ),
            ))

        return findings


register(UnresolvableAdvisory())
