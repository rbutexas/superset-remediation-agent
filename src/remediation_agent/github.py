"""GitHub client, scoped to what a fine-grained token with five permissions can do.

Granted: Metadata:R, Issues:RW, Contents:RW, Pull requests:RW, Actions:R.
Not granted: Administration, Workflows, Secrets. Nothing here can change
repository settings, and that is deliberate — the credential that drives an
autonomous remediation loop should not be able to reconfigure the repository
it is remediating.

CI results are read from the workflow-runs endpoint rather than check-runs,
because `Checks` is not an available permission on fine-grained tokens.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from .config import Config
from .http import Http

log = logging.getLogger(__name__)

FINDING_MARKER = "<!-- finding-key: "


class GitHubClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.repo = cfg.repo
        self.http = Http(
            cfg.github_base,
            {
                "Authorization": f"Bearer {cfg.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )

    # -------------------------------------------------------------- labels

    def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> list[str]:
        """Create any label that does not exist. Returns the names created."""
        existing = {l["name"] for l in self.http.get(
            f"/repos/{self.repo}/labels", per_page=100)}
        created = []
        for name, (colour, description) in labels.items():
            if name in existing:
                continue
            self.http.post(f"/repos/{self.repo}/labels",
                           {"name": name, "color": colour, "description": description})
            created.append(name)
        return created

    def add_label(self, issue_number: int, label: str) -> Any:
        """Adding a trigger label is what starts a Devin session."""
        return self.http.post(
            f"/repos/{self.repo}/issues/{issue_number}/labels", {"labels": [label]}
        )

    # -------------------------------------------------------------- issues

    def iter_issues(self, state: str = "all") -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            batch = self.http.get(f"/repos/{self.repo}/issues",
                                  state=state, per_page=100, page=page)
            if not batch:
                return
            for item in batch:
                if "pull_request" not in item:   # the issues endpoint returns PRs too
                    yield item
            page += 1

    def issues_by_finding_key(self) -> dict[str, dict[str, Any]]:
        """Index existing issues by the finding key embedded in their body.

        This is what makes the scanner safe to run on a schedule: a weekly run
        that rediscovers the same problem updates the existing issue instead of
        filing a duplicate.
        """
        index: dict[str, dict[str, Any]] = {}
        for issue in self.iter_issues():
            body = issue.get("body") or ""
            if FINDING_MARKER not in body:
                continue
            key = body.split(FINDING_MARKER, 1)[1].split("-->", 1)[0].strip()
            index[key] = issue
        return index

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self.http.post(f"/repos/{self.repo}/issues",
                              {"title": title, "body": body, "labels": labels})

    def update_issue(self, number: int, **fields: Any) -> dict[str, Any]:
        return self.http.patch(f"/repos/{self.repo}/issues/{number}", fields)

    def comment(self, number: int, body: str) -> dict[str, Any]:
        return self.http.post(f"/repos/{self.repo}/issues/{number}/comments",
                              {"body": body})

    # -------------------------------------------------------------- verification

    def pull_request(self, number: int) -> dict[str, Any]:
        return self.http.get(f"/repos/{self.repo}/pulls/{number}")

    def ci_conclusions(self, head_sha: str) -> dict[str, str]:
        """Map workflow name -> conclusion for a commit.

        This is the independent verification: Superset's own CI grading Devin's
        work, rather than the agent reporting on itself. `superset-python-unittest`
        is the one that matters; it is active on the fork.
        """
        runs = self.http.get(f"/repos/{self.repo}/actions/runs",
                             head_sha=head_sha, per_page=100)
        out: dict[str, str] = {}
        for run in (runs or {}).get("workflow_runs", []):
            name = run.get("name") or run.get("path", "?")
            out[name] = run.get("conclusion") or run.get("status") or "unknown"
        return out

    def verification_summary(self, pr_url: str) -> dict[str, Any]:
        """Given a PR URL, return its CI verdict. Best-effort: a PR with no runs
        yet reports `pending` rather than failing the caller."""
        try:
            number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return {"pr": pr_url, "error": "unparseable PR url"}

        try:
            pr = self.pull_request(number)
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            return {"pr": pr_url, "error": str(exc)[:200]}

        sha = pr.get("head", {}).get("sha", "")
        conclusions = self.ci_conclusions(sha) if sha else {}
        failed = [n for n, c in conclusions.items() if c == "failure"]
        return {
            "pr": pr_url,
            "number": number,
            "state": pr.get("state"),
            "merged": bool(pr.get("merged_at")),
            "head_sha": sha[:8],
            "checks": conclusions,
            "failed_checks": failed,
            "verdict": (
                "pending" if not conclusions
                else "failing" if failed
                else "passing"
            ),
        }
