"""Detector protocol and registry.

A detector takes a checkout and returns findings. Detectors are pure with respect
to our infrastructure — they never file issues or start sessions — which keeps
them unit-testable against a fixture tree.
"""

from __future__ import annotations

import json
import logging
import pathlib
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


def run_detectors(repo: pathlib.Path, only: list[str] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for name, detector in sorted(_REGISTRY.items()):
        if only and name not in only:
            continue
        log.info("running detector %s", name)
        try:
            found = list(detector.detect(repo))
        except Exception:                             # noqa: BLE001
            # One broken detector must not take down a scan. A scan that silently
            # returns nothing is worse than one that reports a partial result.
            log.exception("detector %s failed", name)
            continue
        log.info("  %s -> %d finding(s)", name, len(found))
        findings.extend(found)
    return findings


# ------------------------------------------------------------------ helpers

def npm_metadata(package: str) -> dict:
    """Fetch and cache npm registry metadata for a package."""
    if package in _NPM_CACHE:
        return _NPM_CACHE[package]
    url = f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@')}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
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
