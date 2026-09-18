"""Re-validate every Dependabot suppression against its own stated condition.

`.github/dependabot.yml` suppresses updates, and next to each entry a human has
written why. Those comments are the only record of what would unblock the
upgrade — and nothing ever re-reads them. A suppression outlives its cause
silently.

This detector does what no scanner does: it reads the prose, follows the links
out to other repositories, checks whether the stated condition now holds, and
reports the ones that have gone stale.

Parsing note: dependabot.yml is read with line-oriented regex rather than a YAML
parser, to keep this package dependency-free. We need exactly two things — the
`dependency-name` entries and the comment block immediately above each — and
that structure is stable. A malformed file yields no findings rather than a
wrong one.
"""

from __future__ import annotations

import logging
import pathlib
import re
from dataclasses import dataclass, field
from typing import Iterable

from ..models import Disposition, Evidence, Finding, Severity
from .base import npm_latest, npm_metadata, npm_published, read_json, register

log = logging.getLogger(__name__)

DEP_RE = re.compile(r'^\s*-\s*dependency-name:\s*["\']([^"\']+)["\']')
COMMENT_RE = re.compile(r"^\s*#\s?(.*)$")
GH_PR_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
GH_ISSUE_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")

# Some comments state a condition about *our own code* rather than an external
# link ("remove once the proxy code is updated to await the async decompress()
# API"). Those cannot be resolved from a URL, so we probe the source directly.
# Curated deliberately: a wrong guess here would produce a false finding.
CODE_PROBES: dict[str, tuple[str, str, str]] = {
    # dependency -> (file to read, marker meaning "not yet done", human description)
    "simple-zstd": (
        "superset-frontend/webpack.proxy-config.js",
        "ZSTDDecompress",
        "the proxy still imports the v1 `ZSTDDecompress` API",
    ),
}


@dataclass(slots=True)
class Suppression:
    dependency: str
    comment: str
    line: int
    update_types: str = ""
    links: list[tuple[str, str, str, str]] = field(default_factory=list)
    """(kind, owner, repo, number) for each GitHub reference in the comment."""


def parse(dependabot_yml: str) -> list[Suppression]:
    """Extract each `dependency-name` ignore entry with the comment above it."""
    out: list[Suppression] = []
    buffer: list[str] = []

    for lineno, raw in enumerate(dependabot_yml.splitlines(), 1):
        comment = COMMENT_RE.match(raw)
        if comment:
            buffer.append(comment.group(1).strip())
            continue

        match = DEP_RE.match(raw)
        if match:
            text = " ".join(buffer).strip()
            sup = Suppression(dependency=match.group(1), comment=text, line=lineno)
            for kind, pattern in (("pull", GH_PR_RE), ("issue", GH_ISSUE_RE)):
                for owner, repo, num in pattern.findall(text):
                    sup.links.append((kind, owner, repo, num))
            out.append(sup)
            buffer = []
            continue

        # `update-types:` belongs to the entry immediately above it
        if "update-types:" in raw and out:
            out[-1].update_types = raw.split("update-types:", 1)[1].strip()
            continue

        # any other non-blank, non-comment line ends the comment block
        if raw.strip():
            buffer = []

    return out


class SuppressionAudit:
    """Detector. `gh` is injected so this is testable without network access."""

    name = "stale-suppression"

    def __init__(self, gh_get=None) -> None:
        self._gh_get = gh_get

    # ------------------------------------------------------------- link state

    def _link_state(self, kind: str, owner: str, repo: str, number: str) -> dict:
        if self._gh_get is None:
            from .base import npm_metadata  # noqa: F401  (keeps import graph flat)
            import json
            import os
            import urllib.request

            url = f"https://api.github.com/repos/{owner}/{repo}/{'pulls' if kind == 'pull' else 'issues'}/{number}"
            req = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            })
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        return self._gh_get(kind, owner, repo, number)

    def _resolve(self, sup: Suppression) -> tuple[bool, list[Evidence]]:
        """Are all referenced links resolved? Returns (resolved, evidence)."""
        evidence: list[Evidence] = []
        all_resolved = bool(sup.links)

        for kind, owner, repo, number in sup.links:
            ref = f"{owner}/{repo}#{number}"
            try:
                data = self._link_state(kind, owner, repo, number)
            except Exception as exc:                  # noqa: BLE001
                log.warning("could not resolve %s: %s", ref, exc)
                evidence.append(Evidence(f"Referenced {kind} {ref}",
                                         f"could not be checked ({exc})",
                                         f"github:{ref}"))
                all_resolved = False
                continue

            if kind == "pull":
                merged = bool(data.get("merged_at"))
                evidence.append(Evidence(
                    f"Referenced PR {ref}",
                    f"merged {data['merged_at'][:10]}" if merged
                    else f"{data.get('state')}, not merged",
                    f"github:{ref}",
                ))
                all_resolved &= merged
            else:
                closed = data.get("state") == "closed"
                reason = data.get("state_reason") or "closed"
                evidence.append(Evidence(
                    f"Referenced issue {ref}",
                    f"closed {data.get('closed_at', '')[:10]} ({reason})" if closed
                    else "still open",
                    f"github:{ref}",
                ))
                # closed-as-stale is not the same as fixed
                all_resolved &= closed and reason != "not_planned"

        return all_resolved, evidence

    # ------------------------------------------------------------- detection

    def detect(self, repo: pathlib.Path) -> Iterable[Finding]:
        config = repo / ".github/dependabot.yml"
        if not config.exists():
            return []

        suppressions = parse(config.read_text(encoding="utf-8"))
        if not suppressions:
            return []

        manifest = repo / "superset-frontend/package.json"
        pkg = read_json(manifest) if manifest.exists() else {}
        pinned = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

        findings: list[Finding] = []
        for sup in suppressions:
            if "*" in sup.dependency:       # family wildcards need a human to scope
                continue

            spec = pinned.get(sup.dependency)
            if not spec or spec.startswith("http"):   # not a registry pin
                continue

            try:
                latest = npm_latest(sup.dependency)
            except Exception as exc:                  # noqa: BLE001
                log.warning("npm lookup failed for %s: %s", sup.dependency, exc)
                continue

            evidence = [
                Evidence("Suppressed in dependabot.yml",
                         f"line {sup.line}" + (f", {sup.update_types}" if sup.update_types else ""),
                         ".github/dependabot.yml"),
                Evidence("Stated condition", sup.comment or "(none given)",
                         ".github/dependabot.yml"),
                Evidence("Currently pinned", spec, "superset-frontend/package.json"),
                Evidence("Latest published",
                         f"{latest} ({npm_published(sup.dependency, latest)})",
                         f"npm:{sup.dependency}"),
            ]

            resolved, link_evidence = self._resolve(sup)
            evidence.extend(link_evidence)

            probe = CODE_PROBES.get(sup.dependency)
            probe_outstanding = False
            if probe:
                path, marker, description = probe
                target = repo / path
                if target.exists() and marker in target.read_text(encoding="utf-8",
                                                                  errors="ignore"):
                    probe_outstanding = True
                    evidence.append(Evidence(
                        "Stated code change still outstanding", description, path))

            if resolved:
                findings.append(self._stale(sup, spec, latest, evidence))
            elif probe_outstanding:
                findings.append(self._unfinished(sup, spec, latest, evidence, probe[0]))

        return findings

    # ------------------------------------------------------------- builders

    def _stale(self, sup: Suppression, spec: str, latest: str,
               evidence: list[Evidence]) -> Finding:
        return Finding(
            key=f"stale-suppression:{sup.dependency}",
            title=(f"deps(frontend): the {sup.dependency} suppression's stated "
                   f"conditions are now met — retry the upgrade"),
            summary=(
                f"`{sup.dependency}` is pinned at `{spec}` and suppressed in "
                f"`.github/dependabot.yml`. The comment beside it names the conditions "
                f"for removing the suppression:\n\n"
                f"> {sup.comment}\n\n"
                f"Every referenced item has since resolved (see evidence). `{latest}` is "
                f"available and we have not moved.\n\n"
                f"**The suppression was added for a reason — do not assume the upgrade "
                f"is now safe, only that it is now worth retrying.** If the original "
                f"failure reproduces, leave the pin in place and update the comment with "
                f"the current blocker."
            ),
            detector=self.name,
            disposition=Disposition.REMEDIATE,
            severity=Severity.MEDIUM,
            evidence=tuple(evidence),
            labels=("dependencies", "frontend"),
            guardrails=(
                "Do not force the upgrade if the original failure reproduces.",
                "Passing unit tests is not sufficient evidence on its own where the "
                "original failure was a runtime error.",
            ),
            acceptance=(
                f"`{sup.dependency}` upgraded, with breaking changes handled.",
                "The original failure demonstrated not to reproduce.",
                "The suppression block removed from `.github/dependabot.yml`.",
                "If it still fails: pin retained, comment updated with the real blocker.",
            ),
        )

    def _unfinished(self, sup: Suppression, spec: str, latest: str,
                    evidence: list[Evidence], path: str) -> Finding:
        return Finding(
            key=f"unfinished-migration:{sup.dependency}",
            title=(f"deps(frontend): {sup.dependency} is pinned pending a code change "
                   f"that was never made"),
            summary=(
                f"`{sup.dependency}` is pinned at `{spec}`; `{latest}` is current. The "
                f"suppression comment states the exit condition:\n\n"
                f"> {sup.comment}\n\n"
                f"That change was never made — `{path}` still uses the old API. The pin "
                f"is not waiting on anything external; it is waiting on us."
            ),
            detector=self.name,
            disposition=Disposition.REMEDIATE,
            severity=Severity.MEDIUM,
            evidence=tuple(evidence),
            labels=("dependencies", "frontend", "build"),
            paths=(path,),
            acceptance=(
                f"`{sup.dependency}` upgraded and `{path}` migrated to the new API.",
                "Existing tests covering this path pass.",
                "The suppression block removed from `.github/dependabot.yml`.",
            ),
        )


register(SuppressionAudit())
