"""Detector protocol and registry.

A detector takes a checkout and returns findings. Detectors are pure with respect
to our infrastructure — they never file issues or start sessions — which keeps
them unit-testable against a fixture tree.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Protocol, runtime_checkable

from ..models import Finding

log = logging.getLogger(__name__)
_NPM_CACHE: dict[str, dict] = {}


@runtime_checkable
class Detector(Protocol):
    name: str

    def detect(self, repo: pathlib.Path) -> Iterable[Finding]: ...


_REGISTRY: dict[str, Detector] = {}


def register(detector: Detector) -> Detector:
    _REGISTRY[detector.name] = detector
    return detector


def registry() -> dict[str, Detector]:
    return dict(_REGISTRY)


@dataclasses.dataclass(slots=True)
class ScanResult:
    """Findings plus whether the scan was complete.

    `degraded` matters more than it looks. A detector that fails — usually a
    network error reaching npm, OSV or GitHub — yields zero findings, and a scan
    returning zero findings is indistinguishable from a clean repository. Callers
    must be able to tell "nothing to report" from "we could not look", so the
    failures are carried out of the scan rather than left in the log.
    """

    findings: list[Finding]
    ran: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    """(detector name, error) for each detector that could not complete."""

    @property
    def degraded(self) -> bool:
        return bool(self.failed)

    def summary(self) -> str:
        head = (f"{len(self.findings)} finding(s) from "
                f"{len(self.ran)}/{len(self.ran) + len(self.failed)} detector(s)")
        if not self.failed:
            return head
        detail = "; ".join(f"{n}: {e}" for n, e in self.failed)
        return f"{head}  [DEGRADED — {len(self.failed)} failed: {detail}]"

    def raise_if_degraded(self) -> None:
        """For callers that must not act on a partial picture — filing issues
        from a degraded scan would look like findings had been resolved."""
        if self.degraded:
            raise ScanDegraded(self.summary())


class ScanDegraded(RuntimeError):
    pass


def run_detectors(repo: pathlib.Path, only: list[str] | None = None) -> ScanResult:
    findings: list[Finding] = []
    ran: list[str] = []
    failed: list[tuple[str, str]] = []

    for name, detector in sorted(_REGISTRY.items()):
        if only and name not in only:
            continue
        log.info("running detector %s", name)
        try:
            found = list(detector.detect(repo))
        except Exception as exc:                      # noqa: BLE001
            # One broken detector must not take down the whole scan, but the
            # failure has to travel with the result rather than only to the log.
            log.exception("detector %s failed", name)
            failed.append((name, f"{type(exc).__name__}: {exc}"[:200]))
            continue
        log.info("  %s -> %d finding(s)", name, len(found))
        ran.append(name)
        findings.extend(found)

    result = ScanResult(findings, tuple(ran), tuple(failed))
    if result.degraded:
        log.warning("%s", result.summary())
    return result


# ------------------------------------------------------------------ helpers

class NotInRegistry(LookupError):
    """The package genuinely is not published. Distinct from "we could not look"."""


def npm_metadata(package: str) -> dict:
    """Fetch and cache npm registry metadata.

    Raises `NotInRegistry` when the package does not exist, and lets every other
    error propagate. That distinction is the point: a missing package is a fact a
    detector can reason about, while a timeout or a TLS failure means the scan
    did not see the truth and must not pretend otherwise.
    """
    if package in _NPM_CACHE:
        return _NPM_CACHE[package]
    url = f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@')}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotInRegistry(package) from None
        raise
    _NPM_CACHE[package] = data
    return data


def npm_latest(package: str) -> str:
    return npm_metadata(package)["dist-tags"]["latest"]


def npm_published(package: str, version: str) -> str:
    return npm_metadata(package).get("time", {}).get(version, "")[:10]


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def semver_major(spec: str) -> int | None:
    """Best-effort major version from a package.json spec like '^1.8.0'."""
    digits = ""
    for ch in spec.lstrip("^~>=< v"):
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None
