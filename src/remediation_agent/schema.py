"""JSON Schemas that Devin sessions must satisfy before they can finish.

Passed as `structured_output_schema` with `structured_output_required: true`, so
the agent cannot end its turn without emitting conforming JSON. The alternative
is regexing English out of a transcript, which degrades silently — change a
prompt and the metrics quietly become wrong.

The triage schema is where Superset's own policy is enforced. Their `AGENTS.md`
requires that automated security findings name the SECURITY.md capability row
they believe is violated and the principal the attacker is assumed to hold, and
says findings that cannot do both should be filed as questions rather than
vulnerabilities. Making those required fields means a session physically cannot
report a security finding without meeting that bar.

Both schemas require `issue_number` and `finding_key`. That is not decoration:
a session started by an automation carries only the automation's static tags —
`campaign:superset-debt`, `stage:triage` — with nothing identifying which issue
fired it. Tags are set at automation-creation time and cannot vary per event.

So the agent reports its own context, and the collector reads it back. Without
this a session runs, answers, and the pipeline has no idea what it answered
about: no verdict posted, no promotion, no issue closed.

Draft 7, self-contained, no external `$ref` — Devin rejects schemas that are not.
"""

from __future__ import annotations

from typing import Any

from .models import RemediationOutcome, TriageDecision

MAX_REASONING = 4000


TRIAGE_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "reasoning", "confidence", "evidence_checked",
                 "issue_number", "finding_key"],
    "properties": {
        "issue_number": {
            "type": "integer",
            "description": (
                "The GitHub issue number you are working on. Read it from the "
                "issue itself."
            ),
        },
        "finding_key": {
            "type": "string",
            "description": (
                "The finding key, taken verbatim from the HTML comment at the "
                "bottom of the issue body: <!-- finding-key: ... -->"
            ),
        },
        "decision": {
            "type": "string",
            "enum": [d.value for d in TriageDecision],
            "description": (
                "What this finding deserves. 'decline_not_actionable' is a valid "
                "and valued outcome — it is not a failure. Choose it when no "
                "change is the correct engineering answer, and say why. "
                "Choose 'document_only' instead when no code change is correct "
                "but the determination itself belongs in the repository — a "
                "scoped scanner suppression, a comment beside an ignore rule, a "
                "dated re-check condition — because otherwise the next scan "
                "reproduces the finding and your reasoning is lost. Name the "
                "file and the wording in 'recommended_prompt_additions'."
            ),
        },
        "reasoning": {
            "type": "string",
            "minLength": 80,
            "maxLength": MAX_REASONING,
            "description": (
                "Why this decision, in terms a reviewer can check. Cite files, "
                "versions, PRs or issues you actually looked at. Do not restate "
                "the finding back."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "Low confidence with reasoning beats a confident guess.",
        },
        "evidence_checked": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
            "description": (
                "Each claim in the issue you independently verified, and what you "
                "found. Note any that turned out to be wrong."
            ),
        },
        "contradicted_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Claims in the issue you found to be incorrect. Populate this if "
                "the scanner got something wrong — that is useful, not impolite."
            ),
        },
        "blocker": {
            "type": "string",
            "description": (
                "Required when decision is 'blocked_upstream'. Name the specific "
                "package, PR, issue or release being waited on — not 'the ecosystem'."
            ),
        },
        "escalation_reason": {
            "type": "string",
            "description": "Required when decision is 'escalate_to_human'. What a "
                           "human needs to decide, and what you would recommend.",
        },
        "estimated_blast_radius": {
            "type": "string",
            "enum": ["single-file", "single-module", "cross-module", "repo-wide"],
        },
        "security_md_matrix_row": {
            "type": "string",
            "description": (
                "For security findings only. The SECURITY.md role/capability row "
                "believed violated. If you cannot name one, this is not a "
                "vulnerability — say so in reasoning and decline."
            ),
        },
        "assumed_principal": {
            "type": "string",
            "enum": ["public", "gamma", "sql_lab", "alpha", "admin",
                     "embedded_guest", "custom_role", "not_applicable"],
            "description": (
                "For security findings only. The principal the attacker is assumed "
                "to hold. 'not_applicable' for non-security findings."
            ),
        },
        "recommended_prompt_additions": {
            "type": "string",
            "description": (
                "If remediating, anything the remediation session should be told "
                "that is not already in the issue."
            ),
        },
    },
}


REMEDIATION_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "summary", "verification",
                 "issue_number", "finding_key"],
    "properties": {
        "issue_number": {
            "type": "integer",
            "description": (
                "The GitHub issue number you are working on. Read it from the "
                "issue itself."
            ),
        },
        "finding_key": {
            "type": "string",
            "description": (
                "The finding key, taken verbatim from the HTML comment at the "
                "bottom of the issue body: <!-- finding-key: ... -->"
            ),
        },
        "outcome": {
            "type": "string",
            "enum": [o.value for o in RemediationOutcome],
            "description": (
                "'abandoned_on_guardrail' is a success — it means a stated "
                "prohibition correctly stopped you. 'failed_verification' means a "
                "change was made but could not be shown correct; report it rather "
                "than opening a PR that looks finished."
            ),
        },
        "summary": {
            "type": "string",
            "minLength": 80,
            "maxLength": MAX_REASONING,
            "description": "What you changed and why, for a reviewer.",
        },
        "verification": {
            "type": "object",
            "additionalProperties": False,
            "required": ["commands_run", "all_passed"],
            "properties": {
                "commands_run": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Exact commands, as run. Not a description of them.",
                },
                "all_passed": {"type": "boolean"},
                "failures": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Each failure, and whether it is caused by this "
                                   "change or pre-existing on the base branch.",
                },
                "unverifiable": {
                    "type": "string",
                    "description": (
                        "Anything the acceptance criteria asked for that you could "
                        "not verify in this environment. Say so plainly — an "
                        "unverified claim is worse than an admitted gap."
                    ),
                },
            },
        },
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "pr_url": {"type": "string"},
        "guardrail_triggered": {
            "type": "string",
            "description": "Required when outcome is 'abandoned_on_guardrail'. "
                           "Which prohibition, and what you would have done otherwise.",
        },
        "follow_up_required": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Work deliberately left undone, for a follow-up issue.",
        },
    },
}


def validate_minimal(payload: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Check required keys and enum membership without a jsonschema dependency.

    Devin validates against the full schema server-side; this is a defensive
    second pass so a malformed or partial payload surfaces as a clear error in
    our pipeline rather than a confusing KeyError later.
    """
    problems: list[str] = []
    props = schema.get("properties", {})

    for key in schema.get("required", []):
        if key not in payload:
            problems.append(f"missing required field: {key}")

    for key, value in payload.items():
        spec = props.get(key)
        if spec is None:
            problems.append(f"unexpected field: {key}")
            continue
        enum_values = spec.get("enum")
        if enum_values and value not in enum_values:
            problems.append(f"{key}={value!r} not one of {enum_values}")
        if spec.get("type") == "object" and isinstance(value, dict):
            problems += [f"{key}.{p}" for p in validate_minimal(value, spec)]

    return problems


REVALIDATION_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["suppressions_reviewed", "cleared", "still_blocked"],
    "properties": {
        "suppressions_reviewed": {
            "type": "integer",
            "description": (
                "How many ignore entries you actually read. Reporting nothing "
                "because you reviewed nothing is a different result from "
                "reporting nothing because everything is still blocked."
            ),
        },
        "cleared": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dependency", "stated_condition", "evidence"],
                "properties": {
                    "dependency": {"type": "string"},
                    "stated_condition": {
                        "type": "string",
                        "description": "The comment's own words, quoted.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": (
                            "What specifically resolved it — a merged PR, a "
                            "published release, a closed issue, with dates. "
                            "Say whether a closure was a real fix or a revert."
                        ),
                    },
                    "cleared_on": {
                        "type": "string",
                        "description": "ISO date the last condition cleared, if known.",
                    },
                },
            },
        },
        "still_blocked": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dependency", "blocker"],
                "properties": {
                    "dependency": {"type": "string"},
                    "blocker": {
                        "type": "string",
                        "description": "What is still outstanding. Name it specifically.",
                    },
                },
            },
            "description": (
                "Entries you checked and found genuinely still blocked. This is "
                "the useful negative result — it is how a reader knows the scan "
                "covered them rather than skipped them."
            ),
        },
        "unreadable": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Entries whose stated condition you could not evaluate — a "
                "wildcard, a dead link, or a condition about code rather than "
                "an external release. Say why."
            ),
        },
    },
}
