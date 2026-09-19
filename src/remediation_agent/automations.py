"""Devin automations — the event triggers.

Two automations, and one of them is the actual product.

1. **Label-driven.** A `github:issues` trigger filtered to `agent:triage` and
   `agent:remediate`. Applying the label starts a session. This is the
   "event-driven" path: no polling on GitHub's side, no glue service.

2. **Scheduled re-validation.** A `schedule:recurring` trigger that re-runs the
   suppression audit weekly. This one matters more than it looks. Blockers clear
   silently — `react-checkbox-tree` became actionable when an upstream release
   shipped, and nothing told anyone. A system that only runs when a human
   remembers to run it does not solve that.

Two API facts shape this module:

  * `POST /v3/organizations/{org}/schedules` is **deprecated and returns 403 from
    2026-09-24** for migrated organisations. Recurring work must be an automation
    with a `schedule:recurring` trigger, expressed as an iCalendar RRULE in UTC.

  * The tenant serves a richer trigger catalogue than the published OpenAPI enum
    (it includes `pagerduty` and `jira:issue_updated`, which the spec omits). So
    `available_triggers()` reads the live schemas endpoint rather than trusting a
    hard-coded list, and `verify_trigger_support` fails loudly if an event type we
    depend on is missing.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Config
from .devin import DevinClient
from .models import Stage

log = logging.getLogger(__name__)

TRIAGE_AUTOMATION = "superset-debt: triage on agent:triage"
REMEDIATION_AUTOMATION = "superset-debt: remediate on agent:remediate"
SCHEDULE_AUTOMATION = "superset-debt: weekly suppression re-validation"

# Weekdays 09:00 UTC. Evaluated in UTC by Devin — not the caller's timezone.
WEEKLY_RRULE = "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0"

REQUIRED_EVENTS = ("github:issues", "schedule:recurring")


def available_triggers(devin: DevinClient) -> dict[str, list[str]]:
    """Live trigger catalogue, as this tenant actually serves it.

    `sources` maps a source name straight to its event names — `{"github":
    {"issues": {...}, "push": {...}}}` — with no intervening `events` key. An
    earlier version of this looked for one and silently returned an empty list
    for every source, which made `verify_trigger_support` report that nothing
    was supported. Tolerating both shapes, since the response is not versioned
    and this failure mode is quiet.
    """
    schemas = devin.automation_schemas()
    out: dict[str, list[str]] = {}
    for source, body in (schemas.get("sources") or {}).items():
        if isinstance(body, dict):
            events = body.get("events", body)
        else:
            events = body
        out[source] = sorted(events) if isinstance(events, dict) else sorted(events or [])
    return out


def verify_trigger_support(devin: DevinClient) -> list[str]:
    """Return the required event types this tenant does not offer.

    Checked rather than assumed: an automation created against an unsupported
    event fails at dispatch time, which looks like 'nothing happened' rather
    than an error.
    """
    catalogue = available_triggers(devin)
    missing = []
    for event in REQUIRED_EVENTS:
        source, _, name = event.partition(":")
        if name not in catalogue.get(source, []):
            missing.append(event)
    return missing


def _label_trigger(label: str, repo: str) -> dict[str, Any]:
    """Fire when one specific label is added to an issue in one specific repo.

    Conditions are a two-level DNF — `any` of `all` groups — reading as
    (repo is ours AND action is labeled AND label is <label>).

    The repository filter is mandatory: the API rejects an issue trigger without
    one — *"Issue trigger requires a filter on 'Repository'"*. That is the right
    constraint. Without it, a label added anywhere the org's GitHub app can see
    would start a billable session.

    Filtering here rather than in the prompt matters for the same reason:
    unrelated issue activity never reaches a session at all.
    """
    return {
        "event_type": "github:issues",
        "conditions": {"any": [{"all": [
            {"field": "repository.full_name", "operator": "eq", "value": repo},
            {"field": "action", "operator": "eq", "value": "labeled"},
            {"field": "label.name", "operator": "eq", "value": label},
        ]}]},
        "replies": [],
    }


def _session(cfg: Config, *, tags: list[str]) -> dict[str, Any]:
    """Session config for a spawned session.

    Note what is absent: `repos`. The API rejects it — *"session.repos is
    read-only; repos derive from the prompt's repo tokens"* — so the repository
    is named by an `@owner/repo` token in the prompt instead.
    """
    return {"tags": ["campaign:superset-debt", *tags]}


def triage_automation_body(cfg: Config, playbook_id: str | None = None, *,
                           enabled: bool = True) -> dict[str, Any]:
    """Triage on `agent:triage`.

    Split from remediation because each stage needs its own playbook, and the
    playbook is what carries the structured-output schema. One automation
    serving both labels could only attach one schema, so half the sessions
    would return the wrong shape.
    """
    playbook_token = f" @playbook:{playbook_id}" if playbook_id else ""
    return {
        "name": TRIAGE_AUTOMATION,
        "run_as": {"type": "organization"},
        "enabled": enabled,
        "metadata": {"campaign": "superset-debt", "stage": "triage",
                     "managed_by": "remediation-agent"},
        "triggers": [_label_trigger(Stage.TRIAGE.trigger_label, cfg.repo)],
        "actions": [{
            "type": "start_session",
            "prompt": (
                f"Triage this issue on @{cfg.repo}.{playbook_token}\n\n"
                "Read the issue body in full: it states the evidence to verify "
                "independently, the open questions to answer, and the "
                "prohibitions under 'Do not'.\n\n"
                "Decide what the finding deserves. Make no code change in this "
                "session. `decline_not_actionable` is a valid and valued "
                "outcome — do not reach for `remediate` because it feels more "
                "useful.\n\n"
                "Report your decision via structured output."
            ),
            "session": _session(cfg, tags=["stage:triage"]),
        }],
        # Devin imposes no concurrency ceiling of its own. Labelling a dozen
        # issues at once would otherwise start a dozen billable sessions.
        "concurrency": {"max_concurrent_runs": cfg.max_concurrent_sessions,
                        "max_queue_depth": 25},
        "limits": {"max_acu_limit": cfg.max_acu_per_session},
    }


def remediation_automation_body(cfg: Config, playbook_id: str | None = None, *,
                                enabled: bool = True) -> dict[str, Any]:
    """Remediate on `agent:remediate` — applied by the collector when triage
    decides the work is worth doing."""
    playbook_token = f" @playbook:{playbook_id}" if playbook_id else ""
    return {
        "name": REMEDIATION_AUTOMATION,
        "run_as": {"type": "organization"},
        "enabled": enabled,
        "metadata": {"campaign": "superset-debt", "stage": "remediation",
                     "managed_by": "remediation-agent"},
        "triggers": [_label_trigger(Stage.REMEDIATION.trigger_label, cfg.repo)],
        "actions": [{
            "type": "start_session",
            "prompt": (
                f"Remediate this issue on @{cfg.repo}.{playbook_token}\n\n"
                "Triage has already decided this is worth doing; its reasoning "
                "is in the issue comments.\n\n"
                "The prohibitions under 'Do not' override the goal. If "
                "completing the task requires violating one, stop and report "
                "`abandoned_on_guardrail` naming the prohibition — that is a "
                "success, not a failure.\n\n"
                "A change you cannot demonstrate is correct is not finished. "
                "Record the exact verification commands you ran, and say "
                "plainly what you could not verify.\n\n"
                "Report the outcome via structured output."
            ),
            "session": _session(cfg, tags=["stage:remediation"]),
        }],
        "concurrency": {"max_concurrent_runs": cfg.max_concurrent_sessions,
                        "max_queue_depth": 25},
        "limits": {"max_acu_limit": cfg.max_acu_per_session},
    }


def schedule_automation_body(cfg: Config, rrule: str = WEEKLY_RRULE, *,
                             enabled: bool = True) -> dict[str, Any]:
    return {
        "name": SCHEDULE_AUTOMATION,
        "run_as": {"type": "organization"},
        "enabled": enabled,
        "metadata": {"campaign": "superset-debt", "stage": "revalidation",
                     "managed_by": "remediation-agent"},
        "triggers": [{
            "event_type": "schedule:recurring",
            "conditions": {"any": [{"all": [
                {"field": "rrule", "operator": "recurrence", "value": rrule},
            ]}]},
            "replies": [],
        }],
        "actions": [{
            "type": "start_session",
            "prompt": (
                f"Re-validate the Dependabot suppressions in @{cfg.repo}.\n\n"
                "`.github/dependabot.yml` suppresses dependency updates. Beside "
                "each entry a human wrote the condition under which it should "
                "be removed. Nothing re-reads those comments, so a suppression "
                "outlives its cause silently.\n\n"
                "For every ignore entry: read the stated condition, follow any "
                "links it contains — including into other repositories — and "
                "determine whether the condition still holds.\n\n"
                "Report every suppression whose stated condition has cleared, "
                "naming the specific evidence. Do not open pull requests; this "
                "is a reporting pass.\n\n"
                "A suppression that exists usually exists for a reason. "
                "Evidence that its condition has cleared means the upgrade is "
                "worth retrying, not that it is safe."
            ),
            "session": _session(cfg, tags=["stage:revalidation"]),
        }],
        "limits": {"max_acu_limit": cfg.max_acu_per_session},
    }


def set_enabled(devin: DevinClient, enabled: bool) -> dict[str, str]:
    """Arm or disarm every automation this tool manages.

    Creating them disarmed and arming them separately keeps "this exists and is
    inspectable" apart from "this will now spend money when an issue is
    labelled".
    """
    managed = {TRIAGE_AUTOMATION, REMEDIATION_AUTOMATION, SCHEDULE_AUTOMATION}
    result: dict[str, str] = {}
    for a in devin.list_automations():
        name = a.get("name")
        if name not in managed:
            continue
        aid = a.get("automation_id") or a.get("id")
        devin.set_automation_enabled(str(aid), enabled)
        result[str(name)] = "enabled" if enabled else "disabled"
        log.info("%s -> %s", name, result[str(name)])
    return result


def ensure(devin: DevinClient, cfg: Config, *, dry_run: bool = False,
           enabled: bool = True, triage_playbook: str | None = None,
           remediation_playbook: str | None = None) -> dict[str, str]:
    """Create both automations if absent. Returns name -> id (or a dry-run note).

    Deliberately does not update an automation that already exists. Silently
    rewriting a live trigger is how a pipeline starts behaving differently from
    what an operator believes is deployed; `--force` in the CLI deletes and
    recreates, which is visible.
    """
    missing = verify_trigger_support(devin)
    if missing:
        raise RuntimeError(
            "this organisation does not offer required trigger(s): "
            + ", ".join(missing)
            + ". Available: "
            + ", ".join(f"{s}:{e}" for s, evs in available_triggers(devin).items()
                        for e in evs)
        )

    existing = {a.get("name"): a for a in devin.list_automations()}
    result: dict[str, str] = {}

    bodies = (
        triage_automation_body(cfg, triage_playbook, enabled=enabled),
        remediation_automation_body(cfg, remediation_playbook, enabled=enabled),
        schedule_automation_body(cfg, enabled=enabled),
    )
    for body in bodies:
        name = body["name"]
        if name in existing:
            aid = existing[name].get("automation_id") or existing[name].get("id")
            log.info("automation already present: %s (%s)", name, aid)
            result[name] = str(aid)
            continue

        if dry_run:
            log.info("[dry-run] would create automation %r (%s) with trigger(s) %s",
                     name, "enabled" if enabled else "DISABLED",
                     [t["event_type"] for t in body["triggers"]])
            result[name] = "dry-run"
            continue

        created = devin.create_automation(body)
        aid = created.get("automation_id") or created.get("id")
        log.info("created automation %s (%s)", name, aid)
        result[name] = str(aid)

    return result
