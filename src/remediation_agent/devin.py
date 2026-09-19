"""Devin v3 API client.

Scoped deliberately to the organisation endpoints. This runs on a Teams-tier
service user, where `/v3/enterprise/*` returns 403 — every analytics call below
uses the org-scoped equivalent.

Two API facts shape this module:

1. There is no outbound webhook. Devin never calls back when a session finishes,
   so `list_sessions` + a tag filter is the only way to learn about completion.
   The collector is therefore a reconciliation loop, not a callback handler.

2. `/v3/organizations/{org}/schedules` is deprecated and returns 403 from
   2026-09-24 for migrated orgs. Recurring work must be an automation with a
   `schedule:recurring` trigger.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from .config import Config
from .http import Http
from .models import RemediationOutcome, SessionRecord, Stage, TriageDecision

log = logging.getLogger(__name__)


class DevinClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.org = cfg.devin_org_id
        self.http = Http(
            cfg.devin_base,
            {
                "Authorization": f"Bearer {cfg.devin_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    # -------------------------------------------------------------- identity

    def whoami(self) -> dict[str, Any]:
        return self.http.get("/v3/self")

    # -------------------------------------------------------------- sessions

    def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        repos: list[str] | None = None,
        tags: list[str] | None = None,
        playbook_id: str | None = None,
        knowledge_ids: list[str] | None = None,
        output_schema: dict[str, Any] | None = None,
        max_acu: int | None = None,
        resumable: bool = False,
    ) -> dict[str, Any]:
        """Create one session. `max_acu` is a hard cap enforced by Devin.

        `resumable` defaults to False here, against the API's own default of
        True. A resumable session preserves its VM state after stopping so it
        can be picked up again; ours are disposable — we read the structured
        output once and never resume — so holding that state is pure overhead.
        """
        body: dict[str, Any] = {
            "prompt": prompt,
            "max_acu_limit": max_acu if max_acu is not None else self.cfg.max_acu_per_session,
            "resumable": resumable,
        }
        if title:
            body["title"] = title
        if repos:
            body["repos"] = repos
        if tags:
            body["tags"] = tags
        if playbook_id:
            body["playbook_id"] = playbook_id
        if knowledge_ids:
            body["knowledge_ids"] = knowledge_ids
        if output_schema:
            body["structured_output_schema"] = output_schema
            body["structured_output_required"] = True

        log.info("creating session %r (cap %s ACU)", title or prompt[:50],
                 body["max_acu_limit"])
        return self.http.post(f"/v3/organizations/{self.org}/sessions", body)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.http.get(f"/v3/organizations/{self.org}/sessions/{session_id}")

    def list_sessions(self, *, tags: list[str] | None = None,
                      first: int = 100) -> Iterator[dict[str, Any]]:
        yield from self.http.paginate(
            f"/v3/organizations/{self.org}/sessions", first=first, tags=tags
        )

    def terminate_session(self, session_id: str) -> dict[str, Any]:
        """Stop a session for good.

        Verified against a real session: the record, its structured output and
        the web replay all survive termination, so nothing needed for evidence
        is lost. Devin also puts an idle session to sleep after about thirty
        minutes on its own — this just makes the end deliberate rather than
        waiting for a timeout.
        """
        log.info("terminating session %s", session_id)
        return self.http.delete(
            f"/v3/organizations/{self.org}/sessions/{session_id}")

    def archive_session(self, session_id: str) -> dict[str, Any]:
        """Sleep and hide a session without ending it. Kept for the case where
        a stalled session may still want a human reply."""
        return self.http.post(
            f"/v3/organizations/{self.org}/sessions/{session_id}/archive")

    def send_message(self, session_id: str, message: str) -> Any:
        return self.http.post(
            f"/v3/organizations/{self.org}/sessions/{session_id}/messages",
            {"message": message},
        )

    def session_insights(self, session_id: str) -> dict[str, Any]:
        return self.http.get(
            f"/v3/organizations/{self.org}/sessions/{session_id}/insights"
        )

    # -------------------------------------------------------------- playbooks

    def list_playbooks(self) -> list[dict[str, Any]]:
        return list(self.http.paginate(f"/v3/organizations/{self.org}/playbooks"))

    def upsert_playbook(self, title: str, body: str, *,
                        output_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create the playbook, or update it in place if the title already exists.

        Makes provisioning re-runnable, which matters because the playbook is
        edited far more often than it is created.
        """
        payload: dict[str, Any] = {"title": title, "body": body}
        if output_schema:
            payload["structured_output_schema"] = output_schema

        for existing in self.list_playbooks():
            if existing.get("title") == title:
                pid = existing.get("playbook_id") or existing.get("id")
                log.info("updating playbook %s", pid)
                return self.http.request(
                    "PUT", f"/v3/organizations/{self.org}/playbooks/{pid}", body=payload
                )

        log.info("creating playbook %r", title)
        return self.http.post(f"/v3/organizations/{self.org}/playbooks", payload)

    # -------------------------------------------------------------- knowledge

    def upsert_knowledge(self, name: str, trigger: str, body: str) -> dict[str, Any]:
        payload = {"name": name, "trigger": trigger, "body": body}
        for note in self.http.paginate(f"/v3/organizations/{self.org}/knowledge/notes"):
            if note.get("name") == name:
                nid = note.get("note_id") or note.get("id")
                return self.http.request(
                    "PUT", f"/v3/organizations/{self.org}/knowledge/notes/{nid}",
                    body=payload,
                )
        return self.http.post(f"/v3/organizations/{self.org}/knowledge/notes", payload)

    # -------------------------------------------------------------- automations

    def list_automations(self) -> list[dict[str, Any]]:
        return list(self.http.paginate(f"/v3/organizations/{self.org}/automations"))

    def create_automation(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.http.post(f"/v3/organizations/{self.org}/automations", body)

    def set_automation_enabled(self, automation_id: str, enabled: bool) -> dict[str, Any]:
        return self.http.patch(
            f"/v3/organizations/{self.org}/automations/{automation_id}",
            {"enabled": enabled},
        )

    def delete_automation(self, automation_id: str) -> Any:
        return self.http.delete(f"/v3/organizations/{self.org}/automations/{automation_id}")

    def automation_schemas(self) -> dict[str, Any]:
        """Live trigger catalogue. Served ahead of the published OpenAPI spec —
        this tenant exposes pagerduty and jira:issue_updated, which the spec's
        enum does not list. Read it rather than hard-coding event names."""
        return self.http.get(f"/v3/organizations/{self.org}/automations/schemas")

    # -------------------------------------------------------------- analytics

    def metrics_usage(self, after: int, before: int) -> dict[str, Any]:
        return self.http.get(
            f"/v3/organizations/{self.org}/metrics/usage",
            time_after=after, time_before=before,
        )

    def metrics_sessions(self, after: int, before: int) -> dict[str, Any]:
        return self.http.get(
            f"/v3/organizations/{self.org}/metrics/sessions",
            time_after=after, time_before=before,
        )

    def metrics_prs(self, after: int, before: int) -> dict[str, Any]:
        return self.http.get(
            f"/v3/organizations/{self.org}/metrics/prs",
            time_after=after, time_before=before,
        )

    def session_consumption(self, session_id: str) -> dict[str, Any]:
        return self.http.get(
            f"/v3/organizations/{self.org}/consumption/daily/sessions/{session_id}"
        )


# ------------------------------------------------------------------ mapping

def to_record(payload: dict[str, Any]) -> SessionRecord:
    """Translate a Devin session payload into our own record.

    Kept as a free function so it can be unit-tested against recorded fixtures
    without any network access.
    """
    tags = payload.get("tags") or []

    def tag_value(prefix: str) -> str | None:
        return next((t.split(prefix, 1)[1] for t in tags if t.startswith(prefix)), None)

    structured_early = payload.get("structured_output")
    reported = structured_early if isinstance(structured_early, dict) else {}

    # Tags are the primary join key for sessions we create ourselves. Sessions
    # created by an automation cannot carry them — automation tags are fixed at
    # creation time and cannot vary per event — so those report their own
    # context in structured output instead. Tags win when both are present.
    finding_key = tag_value("finding:") or reported.get("finding_key") or None

    raw_issue = tag_value("issue:")
    if raw_issue and raw_issue.isdigit():
        issue_number = int(raw_issue)
    else:
        candidate = reported.get("issue_number")
        issue_number = int(candidate) if isinstance(candidate, (int, str)) and \
            str(candidate).isdigit() else None

    stage: Stage | None = None
    raw_stage = tag_value("stage:")
    if raw_stage:
        try:
            stage = Stage(raw_stage)
        except ValueError:
            log.warning("session %s has unknown stage tag %r",
                        payload.get("session_id"), raw_stage)

    structured = payload.get("structured_output") or None
    triage_decision: TriageDecision | None = None
    remediation_outcome: RemediationOutcome | None = None

    if isinstance(structured, dict):
        # The two stages use different schemas, so read whichever key is present
        # rather than trusting the stage tag — a mislabelled session should still
        # yield a usable record.
        raw_decision = structured.get("decision")
        if isinstance(raw_decision, str):
            try:
                triage_decision = TriageDecision(raw_decision)
            except ValueError:
                log.warning("session %s returned unknown triage decision %r",
                            payload.get("session_id"), raw_decision)

        raw_outcome = structured.get("outcome")
        if isinstance(raw_outcome, str):
            try:
                remediation_outcome = RemediationOutcome(raw_outcome)
            except ValueError:
                log.warning("session %s returned unknown remediation outcome %r",
                            payload.get("session_id"), raw_outcome)

    # The field is `pr_url`, not `url`. Reading the wrong key here meant every
    # pull request Devin opened was silently dropped — the dashboard showed a
    # remediation as fixed with no link to what it produced. `url` is accepted
    # too, since the shape is not versioned and guessing wrong once was enough.
    prs = tuple(
        link for pr in (payload.get("pull_requests") or [])
        if (link := pr.get("pr_url") or pr.get("url"))
    )

    return SessionRecord(
        session_id=payload["session_id"],
        stage=stage,
        finding_key=finding_key,
        issue_number=issue_number,
        status=payload.get("status", "unknown"),
        status_detail=payload.get("status_detail"),
        acus=float(payload.get("acus_consumed") or 0.0),
        url=payload.get("url", ""),
        title=payload.get("title"),
        triage_decision=triage_decision,
        remediation_outcome=remediation_outcome,
        structured_output=structured,
        pull_requests=prs,
        created_at=int(payload.get("created_at") or 0),
        updated_at=int(payload.get("updated_at") or 0),
    )
