# Architectural decisions

Each entry: the decision, why, and what it costs. A decision with no stated cost
is a boast rather than a decision.

`★` marks the ones worth saying out loud in a demo. The rest are here because a
senior reviewer may ask, and "I hadn't thought about it" is the wrong answer.

This log is append-only. Where a later decision supersedes an earlier one, the
earlier entry is annotated rather than rewritten — the reasoning that turned out
to be incomplete is part of the record.

**Start at decision 18.** It is the most important one and it came last.

---

## ★ 1. A refusal is a successful outcome

> **Amended by [18](#-18-the-scanner-does-not-decide--devin-does).** The single
> `Verdict` enum described below was split into `TriageDecision` and
> `RemediationOutcome` when the pipeline became two-stage. The principle is
> unchanged; the type names are not.

`TriageDecision.DECLINE_NOT_ACTIONABLE` and `BLOCKED_UPSTREAM` both report
`resolves_finding = True`. Only `ESCALATE_TO_HUMAN` counts as unresolved.

**Why.** An engineer-hour saved dismissing a false positive is worth exactly as
much as an hour saved landing a fix, and it is the scarcer outcome — no existing
tool produces it. Counting only `FIXED` creates an incentive to change code that
should not be changed. The `xlsx` finding is the proof: the only action that
makes the scanner green also reintroduces a vulnerability.

**Cost.** "Issues closed" stops being a clean productivity number. Reporting has
to carry an outcome *mix* rather than a single figure, which is harder to put on
a slide and harder to compare across teams.

**Where.** `models.py::TriageDecision`, `models.py::RemediationOutcome`

---

## ★ 2. Findings carry guardrails — things the agent must not do

`Finding.guardrails` is a first-class field, rendered into the issue body under
a **Do not** heading and into the session prompt.

**Why.** "Resolve this finding" is an under-specified objective, and under-specified
objectives get satisfied in the cheapest available way. For `xlsx` the cheapest
way is to reinstall from the npm registry, which is a downgrade past both fixes.
The prohibition has to be data attached to the finding, not a hope about how the
model will behave.

**Cost.** Guardrails are hand-written per detector. They do not generalise, and a
missing one is invisible until something goes wrong.

**Where.** `models.py::Finding.guardrails`, `scanner/advisories.py`

---

## ★ 3. Structured output is a contract, not parsed prose

Every session is created with a `structured_output_schema` and
`structured_output_required: true`. Devin cannot end its turn without emitting
JSON that validates against it.

**Why.** The alternative is regexing English out of a transcript. That is
unreliable, and it silently degrades — a prompt change alters the phrasing and
the metrics quietly become wrong. Making the schema a required field moves the
contract from our parser into the platform. It is also how Superset's own
`AGENTS.md` requirement gets enforced: it demands that automated findings name
the principal and the SECURITY.md row, so those become required schema fields
and a session physically cannot finish without them.

**Cost.** Schema changes are a migration. Sessions created against an old schema
produce records the new reader has to tolerate, so the store keeps raw output
alongside the parsed verdict.

**Where.** `devin.py::create_session`, `models.py::SessionRecord.structured_output`

---

## ★ 4. Observability is a reconciliation loop, not a callback

The collector polls `GET /sessions` filtered by a campaign tag and reconciles
against local state.

**Why.** Devin has no outbound webhook — it receives events but never calls back
on completion. That is a platform constraint, but polling is the better design
here anyway: there are no deliveries to miss, and a collector that has been down
for an hour catches up on its next tick instead of losing an hour of history.

**Cost.** Latency is bounded by the poll interval (60s), and the loop must be
cheap enough to run continuously. Reads are unmetered, so the cost is wall-clock,
not credits.

**Where.** `devin.py` module docstring, `config.py::CAMPAIGN_TAG`

---

## 5. Zero runtime dependencies

Runtime code is stdlib only — `urllib`, `json`, `sqlite3`, `dataclasses`.
`pytest` is the single dev dependency.

**Why.** This tool's subject is dependency risk. Shipping it with a transitive
tree would be an odd look, and it makes the Docker image small and the build
reproducible with no lockfile to drift.

**Cost.** ~120 lines of hand-rolled HTTP with retry and pagination that `requests`
would have given free, and no `pydantic`-style validation — the schemas are
hand-written dicts.

**Where.** `pyproject.toml`, `http.py`

---

## 6. Detectors are pure

A detector takes a filesystem path and returns findings. It never files an issue,
starts a session, or writes to the database.

**Why.** It makes every detector unit-testable against a fixture tree with no
mocking of our own infrastructure, and it means `scan` is safe to run anywhere —
including in CI on an untrusted branch — because it cannot cause an effect.

**Cost.** Detectors that need network data (npm, OSV, GitHub) still reach out, so
"pure" means pure with respect to *our* side effects, not referentially
transparent. Tests either inject a fetcher or accept a live call.

**Where.** `scanner/base.py::Detector`

---

## 7. One failing detector does not fail the scan

`run_detectors` catches per detector, logs, and continues.

**Why.** A scan that returns three of four findings is useful. A scan that
crashes returns nothing, and — worse — a scan that silently returns zero looks
identical to a clean repository. The exception is logged loudly for exactly that
reason.

**Cost.** A detector can rot unnoticed if nobody reads the logs. The scan summary
reports how many detectors ran versus how many were registered.

**Where.** `scanner/base.py::run_detectors`

---

## 8. Two independent spend caps

`max_acu_limit` on every session (enforced by Devin, hard), plus our own
`max_concurrent_sessions`.

**Why.** They fail differently. The per-session cap stops one session running
away. The concurrency cap stops fifty sessions starting at once — and Devin
documents *no* concurrency limit, so nothing upstream provides that ceiling. A
large finding set with no local cap is how a credit budget disappears overnight.

**Cost.** Throughput is deliberately below what the platform allows.

**Where.** `config.py::max_acu_per_session`, `max_concurrent_sessions`

---

## 9. Dry-run mode, because the platform has none

`--dry-run` resolves findings, renders prompts, and prints what would be
dispatched, without creating anything.

**Why.** Devin's automations report `preflight_available: false` — there is no way
to test-fire one without it doing the work. For a system with a spend meter and
write access to a repository, "run it and see" is not an acceptable first
rehearsal.

**Cost.** Every code path that creates something needs a dry-run branch, which is
a class of bug — a path that forgets to check the flag. Mitigated by keeping
creation calls in one module.

**Where.** `config.py::dry_run`

---

## 10. Least-privilege credential, deliberately narrower than convenient

The GitHub token is fine-grained, scoped to two repositories, with five
permissions: Metadata, Issues, Contents, Pull requests (RW), Actions (R).
No Administration, Workflows, or Secrets.

**Why.** The credential that drives an autonomous loop should not be able to
reconfigure the repository it is working on. This is also why CI results are read
from the workflow-runs endpoint rather than check-runs — `Checks` is not offered
on fine-grained tokens, and the answer to a missing permission was to find
another read path, not to widen the grant.

**Cost.** The agent cannot enable a disabled workflow or change repository
settings; those need a human. That is the intended outcome.

**Where.** `github.py` module docstring

---

## 11. Verification comes from the project's own CI

Success is read from workflow runs on the PR's head SHA, not from the agent's
self-report.

**Why.** "Devin said the tests passed" is an agent grading its own homework. The
`react-checkbox-tree` history is the argument: PR #39261 passed CI and still
broke every dashboard, which is why that finding's acceptance criteria say
explicitly that unit tests alone do not close it.

**Cost.** CI is slow and sometimes flaky, so verification lags the session by
minutes, and a red build may mean a broken fix or an unrelated failure. The
reporter shows which checks failed rather than a single boolean.

**Where.** `github.py::verification_summary`

---

## 12. Idempotency via a marker embedded in the issue body

Each issue body ends with `<!-- finding-key: … -->`. Re-filing looks findings up
by that key and updates in place.

**Why.** The scheduled re-validation is the point of the system, and a weekly job
that refiles the same five issues is worse than no job at all. Keying on the
title would break the moment a title changes — and titles carry counts
("205 un-awaited calls"), which change by design.

**Cost.** An invisible HTML comment in every issue, and a full issue listing on
each run to build the index.

**Where.** `github.py::issues_by_finding_key`, `models.py::Finding.key`

---

## 13. Retry 429s on POST, never retry 500s on POST

`http.py` retries idempotent methods on any retryable status, but a POST is only
retried on 429.

**Why.** A 429 is rejected before the handler runs, so nothing happened and
retrying is safe. A 500 may have partially applied — retrying a session creation
that actually succeeded means paying twice and getting two agents on one issue.

**Cost.** A genuinely transient 500 on session creation surfaces as an error the
operator must re-run.

**Where.** `http.py::request`

---

## 14. Ambiguity is skipped, not guessed

Wildcard suppressions (`@babel/*`, `@deck.gl/*`) are skipped rather than expanded
by guessing which packages a family covers. Code-level probes are a curated table
keyed by dependency, not inferred from comment text.

**Why.** This project's entire argument is that false findings are expensive.
Producing one in order to raise the finding count would be self-defeating —
particularly against a repository whose `AGENTS.md` specifically asks automated
tools to file uncertain things as questions rather than findings.

**Cost.** Real work is missed. The Babel 8 blocker is a genuine finding this
detector will not produce, and it has to be authored by hand.

**Where.** `scanner/suppressions.py::CODE_PROBES`, the `"*" in dependency` skip

---

## 15. Line-oriented parsing of `dependabot.yml`, not a YAML parser

**Why.** Adding PyYAML to satisfy one file would break decision 5 for a small
benefit. We need two things — `dependency-name` entries and the comment block
above each — and comments are what matter here. Most YAML libraries discard
comments entirely, so a real parser would have thrown away the signal.

**Cost.** Unusual formatting could be misread. Mitigated by failing closed: a
file that does not match the expected shape yields no findings rather than wrong
ones.

**Where.** `scanner/suppressions.py::parse`

---

## 16. Every claim carries its source

`Evidence` is `(claim, value, source)`, and the source is rendered into the
issue body under each line.

**Why.** Superset's `AGENTS.md` asks automated tools to make findings testable
against the published model rather than assert them. Beyond that, it is what
makes the issues reviewable: a maintainer can re-check any single line without
trusting the tool.

**Cost.** Verbose issue bodies.

**Where.** `models.py::Evidence`

---

## 17. Claims are executable, not prose

`tools/verify_claims.py` re-derives every factual statement used in the issue
set — 42 assertions against the repo, npm, OSV, and GitHub.

**Why.** This was a response to a real failure during development. Several
claims in early drafts were overstated — one materially: a suppression described
as neglected for months had in fact been actionable for three weeks. The data was
right and the prose had drifted from it. Prose drifts; assertions do not.

The rule that came out of it: **if a claim is not in the script and passing, it
does not go in an issue, a slide, or the video.**

**Cost.** Every claim needs a check written, and checks can themselves be wrong —
the first run failed on an assertion string that did not match the real text.
That is the mechanism working.

**Where.** `tools/verify_claims.py`

---

# Added in the two-stage restructure

These came out of re-reading the brief: *"leverage Devin as a core primitive, not
just a helper tool."* The original design failed that test, and decisions 18–24
are the response.

---

## ★ 18. The scanner does not decide — Devin does

The scanner emits facts, evidence and open questions. It assigns no disposition.
A Devin **triage session** reads the finding plus the repository and decides:
`remediate`, `decline_not_actionable`, `blocked_upstream`, or
`escalate_to_human`.

**Why.** The original design had the scanner set the disposition and Devin
execute it. That is a capable helper being handed a decided task — and it
quietly refutes the argument the whole system makes. The pitch is *"scanners
produce findings, bots produce patches, nothing produces verdicts."* If the
verdict came from an `if` statement in `models.py`, the pitch answers itself.

It also makes the refusals real. Previously "Devin declined the xlsx finding"
would have meant the scanner labelled it triage and Devin agreed. Now Devin sees
a finding with no disposition attached and independently concludes that no action
is correct.

**Cost.** Roughly double the sessions — a triage session plus a remediation
session per finding — and a slower path to the first pull request. Triage should
be cheap because it reads and reasons without changing code, but that is an
assumption until measured. It also introduces a real failure mode: **Devin can
triage wrong.** It could decide to "fix" the xlsx finding. The guardrails exist
to make that unlikely, not impossible.

**Where.** `models.py` module docstring, `dispatch.py`, `schema.py::TRIAGE_SCHEMA`

---

## ★ 19. Promotion happens by label, not by an internal call

When triage returns `remediate`, the dispatcher adds the `agent:remediate` label
to the issue. A second automation fires on that label.

**Why.** Two reasons. The decision becomes **visible on the issue timeline** — a
reviewer can watch triage conclude, see the label appear, and see the remediation
session start, without reading our logs. And the second stage is driven by the
same event mechanism as the first, rather than by a private code path that
behaves differently.

The alternative — calling `create_session` directly from the collector — is
fewer moving parts but makes the interesting moment invisible.

**Cost.** A round trip through GitHub adds latency, and it means the remediation
automation must exist and be enabled for the pipeline to complete. A
misconfigured automation fails silently as "nothing happened" rather than loudly.

**Where.** `dispatch.py::act_on_triage`

---

## 20. The scanner's guess is recorded and never read

`Finding.scanner_hint` holds what a cheap heuristic would have concluded. Nothing
in the dispatch path reads it.

**Why.** Deleting it would lose a genuinely interesting measurement: how often a
regex-and-version-comparison heuristic disagrees with an agent that read the
code. Agreement is evidence the agent is calibrated; disagreement is where the
judgment is actually happening. Either way it is a better number than the
outcome mix alone.

Keeping it while never reading it is a deliberate discipline — the field is
documented as non-routing in three places so a future change does not quietly
start using it.

**Cost.** A field that looks unused and invites someone to wire it up.

**Where.** `models.py::Finding.scanner_hint`

---

## 21. The output schema invites the agent to contradict us

`TRIAGE_SCHEMA` has a `contradicted_evidence` array: *"Populate this if the
scanner got something wrong — that is useful, not impolite."*

**Why.** The scanner will be wrong sometimes. During development several of this
project's own claims were overstated, and one was materially wrong. A pipeline
where the agent can only agree or escalate has no channel for "your premise is
incorrect", so that information is lost precisely when it matters most.

It also improves the issues over time: a contradiction is a detector bug report
arriving through the pipeline.

**Cost.** Nothing structural. The risk is the opposite one — an agent that
contradicts the scanner incorrectly and talks itself out of real work. Mitigated
by requiring `evidence_checked` alongside it, so a contradiction has to cite what
was actually inspected.

**Where.** `schema.py::TRIAGE_SCHEMA`

---

## 22. Two playbooks, because they have different success conditions

Triage and remediation get separate playbooks, separate prompts, separate schemas.

**Why.** Triage succeeds by reaching a defensible decision, *including doing
nothing*. Remediation succeeds by making a change the project's own tests accept,
or by stopping when a guardrail says it should. A single playbook covering both
would have to hedge, and hedged instructions produce hedged behaviour.

Concretely: the triage playbook says "you are not fixing anything in this
session" and the remediation playbook says "a change you cannot demonstrate is
correct is not finished." Neither sentence belongs in the other.

**Cost.** Two artefacts to keep aligned, and a prompt change often needs making
twice.

**Where.** `playbooks.py`

---

## 23. "Stopped because a guardrail said so" is a success

`RemediationOutcome.ABANDONED_ON_GUARDRAIL` reports `is_success = True`, and the
schema requires naming which prohibition fired and what would have happened
otherwise.

**Why.** Without this the only ways to end a remediation session are success or
failure, so an agent facing a guardrail has no honest exit and is pushed toward
proceeding anyway. Making the stop a first-class positive outcome is what makes
the prohibition credible rather than decorative.

**Cost.** A verdict that could be over-used as an escape hatch. Requiring the
specific prohibition to be named makes that visible in review.

**Where.** `models.py::RemediationOutcome`, `schema.py::REMEDIATION_SCHEMA`

---

## 24. An admitted gap beats an unverified claim

`REMEDIATION_SCHEMA.verification` requires `commands_run` (verbatim, not
described) and carries an `unverifiable` field: *"anything the acceptance
criteria asked for that you could not verify in this environment."*

**Why.** The `react-checkbox-tree` history is the argument. PR #39261 passed CI
and still broke every dashboard, because the failure was a runtime error the
unit tests never touched. An agent that reports "tests pass" without saying what
the tests did not cover reproduces exactly that failure.

Requiring commands verbatim rather than a summary is the same instinct: "I ran
the test suite" is unfalsifiable, `pytest ./tests/common ./tests/unit_tests` is
checkable.

**Cost.** Longer outputs, and an agent can still claim to have run something it
did not. That is why CI verification is read independently (decision 11) rather
than trusted from the session.

**Where.** `schema.py::REMEDIATION_SCHEMA`, `playbooks.py::REMEDIATION_BODY`

---

## 25. Filing an issue and spending money are separate actions

`file_findings` creates issues with only the `agent:triage` label. It never
starts a session. Triggering is always a distinct, explicit step.

**Why.** The two operations have completely different risk profiles — one is
free and reversible, the other spends a metered budget and gives an agent write
access to a repository. Collapsing them into one command means a re-run of a
scan can silently cost money.

**Cost.** One more step in the happy path, and an operator can forget the second
one. The report shows findings with no session as an explicit state rather than
omitting them.

**Where.** `dispatch.py::file_findings`

---

## 26. Validate the schema locally as well, despite server-side validation

Devin validates structured output against the schema before a session can finish.
`schema.py::validate_minimal` checks it again on receipt.

**Why.** Defence in depth at a trust boundary. A partial or malformed payload —
from an older schema version, a truncated response, a future API change —
surfaces as a clear validation error naming the field, rather than a `KeyError`
three layers into the reporter.

**Cost.** ~40 lines duplicating logic the platform already performs, and it can
drift from the real schema. Kept minimal on purpose: required keys and enum
membership only, no attempt at full JSON Schema semantics.

**Where.** `schema.py::validate_minimal`

---

## ★ 27. The router decides whether a verdict is needed — not what it is

`routing.py` classifies each finding as `TRIAGE`, `DIRECT_TO_REMEDIATION`, or
`HOLD_FOR_HUMAN`, on observable properties: does it carry prohibitions, does it
carry unresolved questions, how many files are in scope.

**Why.** Decision 18 sends findings to a Devin triage session for a verdict.
Taken literally that means *every* finding gets a session — which is right for
ambiguous work and wasteful for everything else. An org processing five hundred
findings a week would pay for five hundred triage sessions to surface perhaps
forty non-obvious decisions. Paying an agent to conclude "yes, apply the
available patch" is overhead, not judgment.

So there are two distinct questions, and only one of them belongs to the agent:

| Question | Answered by |
|---|---|
| Is this ambiguous enough to need judgment? | the router — cheap, deterministic, auditable |
| What does it deserve? | **Devin** |

The router never reaches a `TriageDecision`. A test asserts that the two enums
share no values, so the distinction cannot quietly erode.

**The policy is deliberately asymmetric.** Ambiguity is the default, and
`allow_direct_remediation` is off out of the box. Mis-routing an ambiguous
finding to remediation means an agent changing code it should have questioned.
Mis-routing an unambiguous one to triage costs a few credits. Those errors are
not equivalent, so the policy is not either.

**On the current finding set this changes nothing** — all four carry
prohibitions or open questions and route to triage regardless. That is a
property of the findings, not a law, and the demo should say so: at volume you
route the obvious deterministically and reserve the agent for the tail. These
four *are* the tail. That was the selection criterion.

**Cost.** A policy object that can be misconfigured, and one more concept in the
pipeline. Mitigated by making every routing decision carry its reasons, so a dry
run prints exactly why each finding went where it did.

**Where.** `routing.py`, `dispatch.py::plan`, `tests/test_routing.py`

---

## 28. A scan reports whether it could see the truth

`run_detectors` returns a `ScanResult` carrying `ran`, `failed` and `degraded`,
rather than a bare list.

**Why.** This came from an accident. Running the scanner under an interpreter
with no CA bundle, two detectors failed their network lookups, logged a warning,
and returned nothing. The scan "succeeded" with one finding instead of four.

That is the failure mode decision 7 was written to avoid, and decision 7 did not
actually prevent it — it kept the scan alive but left the failure in the log,
where a caller cannot act on it. **Zero findings and "we could not look" must not
be the same value.** Filing issues from a degraded scan would imply the missing
findings had been resolved.

The same distinction now runs through the helpers: `NotInRegistry` is a fact a
detector can reason about, while a timeout or TLS failure propagates and degrades
the scan.

**Cost.** Callers must handle a result object, and `raise_if_degraded()` has to
be invoked deliberately at the points where acting on a partial picture would be
wrong. A caller that forgets it gets the old behaviour back.

**Where.** `scanner/base.py::ScanResult`, `NotInRegistry`

---

## 29. An empty upstream response is the one failure a scan cannot detect

`ScanResult.degraded` (decision 28) catches a detector that *errored*. It cannot
catch an upstream API that returns a successful, empty response.

Observed once: three consecutive container runs reported four findings, and a
fourth reported three. The OSV query for `xlsx` had returned `{"vulns": []}` —
HTTP 200, no error, nothing to degrade on. The scan was confidently wrong.

**Why it is not fixed properly.** Distinguishing "no advisories" from "the
advisory service hiccuped" needs either a known-good baseline to compare against
or a second source to cross-check, and both add a dependency and a maintenance
burden for a rare transient. The mitigation is a `WARNING` when a
non-registry-pinned package returns no advisories at all, since that combination
is unusual enough to deserve a human glance.

**Cost.** A real gap, honestly labelled rather than closed. Anything acting on a
scan result — filing issues, closing them — should treat a finding that
*disappeared* since the last run with more suspicion than one that appeared.

**Where.** `scanner/advisories.py`
