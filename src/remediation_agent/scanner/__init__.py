"""Detectors.

Importing this package registers every detector. Each one is pure with respect to
our infrastructure: it reads a checkout and returns findings. Nothing here files
an issue or starts a session.
"""

from . import advisories, awaits, suppressions  # noqa: F401  (import registers)
from .base import Detector, register, registry, run_detectors

__all__ = ["Detector", "register", "registry", "run_detectors"]
