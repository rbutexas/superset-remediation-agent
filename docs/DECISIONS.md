# Architectural decisions

Each entry: the decision, why, and what it costs. A decision with no stated cost
is a boast rather than a decision.

`★` marks the four worth saying out loud in a demo. The rest are here because a
senior reviewer may ask, and "I hadn't thought about it" is the wrong answer.

---

## ★ 1. A refusal is a successful outcome

`Verdict.DECLINED_NOT_ACTIONABLE` and `Verdict.BLOCKED_UPSTREAM` both report
`is_success = True`. Only `ESCALATE_TO_HUMAN` counts as unresolved.

**Why.** An engineer-hour saved dismissing a false positive is worth exactly as
much as an hour saved landing a fix, and it is the scarcer outcome — no existing
tool produces it. Counting only `FIXED` creates an incentive to change code that
should not be changed. The `xlsx` finding is the proof: the only action that
makes the scanner green also reintroduces a vulnerability.

**Cost.** "Issues closed" stops being a clean productivity number. Reporting has
to carry an outcome *mix* rather than a single figure, which is harder to put on
a slide and harder to compare across teams.

**Where.** `models.py::Verdict`

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
