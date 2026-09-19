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

LABEL_AUTOMATION = "superset-debt: dispatch on agent label"
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


def _label_trigger() -> dict[str, Any]:
    """Fire on either trigger label being added to an issue.

    Conditions are a two-level DNF — an `any` of `all` groups — so this reads as
    (action is labeled AND label is agent:triage) OR (action is labeled AND label
    is agent:remediate). Filtering here rather than in the session prompt means
    unrelated issue activity never starts a billable session.
    """
    return {
        "event_type": "github:issues",
        "conditions": {
            "any": [
                {"all": [
                    {"field": "action", "operator": "eq", "value": "labeled"},
                    {"field": "label.name", "operator": "eq", "value": label},
                ]}
                for label in (Stage.TRIAGE.trigger_label, Stage.REMEDIATION.trigger_label)
            ]
        },
        "replies": [],
    }


def label_automation_body(cfg: Config, *, enabled: bool = True) -> dict[str, Any]:
    return {
        "name": LABEL_AUTOMATION,
        "run_as": {"type": "organization"},
        "enabled": enabled,
        "metadata": {"campaign": "superset-debt", "managed_by": "remediation-agent"},
        "triggers": [_label_trigger()],
        "actions": [{
            "type": "start_session",
            "prompt": (
                f"An issue on @{cfg.repo} has been labelled for agent work.\n\n"
                "Read the issue body in full. It states the evidence, the open "
                "questions, the prohibitions under 'Do not', and the acceptance "
                "criteria.\n\n"
                "If the label is `agent:triage`, decide what the finding deserves "
                "and make no code change. 'No action' is a valid and valued "
                "outcome.\n\n"
                "If the label is `agent:remediate`, make the change, verify it, "
                "and open a pull request. The prohibitions override the goal: if "
                "completing the task requires violating one, stop and report that "
                "instead.\n\n"
                "Report via structured output."
            ),
            "session": {"repos": [cfg.repo], "tags": ["campaign:superset-debt"]},
        }],
        # Devin imposes no concurrency limit of its own. A label applied to a
        # dozen issues at once would otherwise start a dozen billable sessions.
        "concurrency": {
            "max_concurrent_runs": cfg.max_concurrent_sessions,
            "max_queue_depth": 25,
        },
        "limits": {"max_acu_limit": cfg.max_acu_per_session},
    }


def schedule_automation_body(cfg: Config, rrule: str = WEEKLY_RRULE, *,
                             enabled: bool = True) -> dict[str, Any]:
    return {
        "name": SCHEDULE_AUTOMATION,
        "run_as": {"type": "organization"},
        "enabled": enabled,
        "metadata": {"campaign": "superset-debt", "managed_by": "remediation-agent"},
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
                "each entry a human wrote the condition under which it should be "
                "removed. Nothing re-reads those comments, so a suppression "
                "outlives its cause silently.\n\n"
                "For every ignore entry: read the stated condition, follow any "
                "links it contains — including into other repositories — and "
                "determine whether the condition still holds.\n\n"
                "Report every suppression whose stated condition has cleared, "
                "naming the specific evidence. Do not open pull requests; this is "
                "a reporting pass.\n\n"
                "A suppression that exists usually exists for a reason. Evidence "
                "that its condition has cleared means the upgrade is worth "
                "retrying, not that it is safe."
            ),
            "session": {"repos": [cfg.repo],
                        "tags": ["campaign:superset-debt", "stage:revalidation"]},
        }],
        "limits": {"max_acu_limit": cfg.max_acu_per_session},
    }


def set_enabled(devin: DevinClient, enabled: bool) -> dict[str, str]:
    """Arm or disarm both automations.

    Creating them disabled and arming them separately keeps "this exists and is
    inspectable" apart from "this will now spend money when an issue is
    labelled". With the label automation live, filing an issue is no longer a
    free action.
    """
    result: dict[str, str] = {}
    for a in devin.list_automations():
        name = a.get("name")
        if name not in (LABEL_AUTOMATION, SCHEDULE_AUTOMATION):
            continue
        aid = a.get("automation_id") or a.get("id")
        devin.set_automation_enabled(str(aid), enabled)
        result[str(name)] = "enabled" if enabled else "disabled"
        log.info("%s -> %s", name, result[str(name)])
    return result


def ensure(devin: DevinClient, cfg: Config, *, dry_run: bool = False,
           enabled: bool = True) -> dict[str, str]:
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

    for body in (label_automation_body(cfg, enabled=enabled),
                 schedule_automation_body(cfg, enabled=enabled)):
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
