"""Configuration, loaded from the environment.

No secret is ever written to disk by this package, logged, or included in a
report. `Config.redacted()` exists so config can be printed safely.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib

DEFAULT_ENV_FILE = pathlib.Path.home() / ".devin.env"
CAMPAIGN_TAG = "campaign:superset-debt"
"""Every session we create carries this tag. It is the join key between our
store and Devin's API, and it is how the collector scopes its polling."""


class ConfigError(RuntimeError):
    pass


def load_env_file(path: pathlib.Path = DEFAULT_ENV_FILE) -> None:
    """Populate os.environ from a KEY=VALUE file, without overriding real env vars.

    Supports the local developer flow without requiring secrets on the command
    line. Missing file is not an error: in Docker the values come from the
    environment instead.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclasses.dataclass(frozen=True, slots=True)
class Config:
    devin_api_key: str
    devin_org_id: str
    github_token: str
    repo: str
    """owner/name of the fork the agent works on."""

    devin_base: str = "https://api.devin.ai"
    github_base: str = "https://api.github.com"

    max_acu_per_session: int = 25
    """Hard per-session spend cap, passed to Devin. A session cannot exceed it."""

    max_concurrent_sessions: int = 3
    """Devin imposes no concurrency limit. This one is ours — an unbounded fan-out
    across a large finding set is how a credit budget disappears overnight."""

    poll_interval_seconds: int = 60
    db_path: pathlib.Path = pathlib.Path("data/agent.db")
    dry_run: bool = False
    """When set, nothing is created: no issues, no sessions, no labels. The
    pipeline resolves and renders everything and prints what it would dispatch.
    Devin's own automations have no dry-run (`preflight_available: false`), so
    this is ours."""

    @classmethod
    def from_env(cls, *, dry_run: bool = False, env_file: pathlib.Path | None = None,
                 require: tuple[str, ...] = ("DEVIN_API_KEY", "DEVIN_ORG_ID",
                                             "GITHUB_TOKEN")) -> "Config":
        """Build config, demanding only the credentials the caller will use.

        `scan` and `report` are read-only and touch neither API, so requiring a
        Devin key to run them turns "check that the findings are real" — the
        claim most worth verifying — into "first go and get an account". The
        caller states what it needs; anything else is left blank.
        """
        load_env_file(env_file or DEFAULT_ENV_FILE)

        missing = [k for k in require if not os.environ.get(k)]
        if missing:
            raise ConfigError(
                "missing required environment variables: " + ", ".join(missing)
                + f"\nSet them in the environment or in {DEFAULT_ENV_FILE}"
            )

        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be an integer, got {raw!r}") from None

        return cls(
            devin_api_key=os.environ.get("DEVIN_API_KEY", ""),
            devin_org_id=os.environ.get("DEVIN_ORG_ID", ""),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            repo=os.environ.get("TARGET_REPO", "rbutexas/superset"),
            devin_base=os.environ.get("DEVIN_BASE", "https://api.devin.ai"),
            github_base=os.environ.get("GITHUB_BASE", "https://api.github.com"),
            max_acu_per_session=_int("MAX_ACU_PER_SESSION", 25),
            max_concurrent_sessions=_int("MAX_CONCURRENT_SESSIONS", 3),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 60),
            db_path=pathlib.Path(os.environ.get("DB_PATH", "data/agent.db")),
            dry_run=dry_run,
        )

    def redacted(self) -> dict[str, object]:
        """Safe to print or log. Secrets become a length and a prefix only."""

        def mask(secret: str) -> str:
            head = secret[:4] if len(secret) > 8 else ""
            return f"{head}…({len(secret)} chars)"

        return {
            "devin_api_key": mask(self.devin_api_key),
            "devin_org_id": self.devin_org_id,
            "github_token": mask(self.github_token),
            "repo": self.repo,
            "max_acu_per_session": self.max_acu_per_session,
            "max_concurrent_sessions": self.max_concurrent_sessions,
            "poll_interval_seconds": self.poll_interval_seconds,
            "db_path": str(self.db_path),
            "dry_run": self.dry_run,
        }
