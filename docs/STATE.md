# Project state — Devin take-home

**Last updated:** 2026-09-18 (end of session 1)
**Credits spent so far: 0 ACU.** No Devin session has been created yet.

---

## The assignment

Build an event-driven automation using the Devin API that remediates issues in a fork
of `apache/superset`. Deliverables: working system (Docker, public repo, clear README),
a ≤5-min Loom pitched at a VP of Engineering + senior ICs, and the forked Superset repo
containing the issues.

Grading bar (from the recruiter note):

- 5+ issues remediated, **or** 2+ very complex ones, demonstrating strong judgment
- Must use **Session + Analytics APIs** AND (**Scheduled Devin** or **Advanced Capabilities**);
  webhooks for real-time detection and/or playbooks for repeated tasks
- Docker, no exposed credentials, immediately reproducible, zero errors
- Meaningful new tests, clear evidence of verification
- Clean, production-quality architecture
- Commercial: story not a status report; slides; business-impact framing with a callback
  to the analytics; explains **why Devin worked/didn't work for each issue**; compelling
  next steps. Litmus test: *would they trust this person in front of a VP of Eng?*

---

## Accounts and access

| Thing | Value |
|---|---|
| GitHub account | **`rbutexas`** — everything lives here. `rbhaveMeta` is deliberately NOT involved. |
| Superset fork | `https://github.com/rbutexas/superset` (master-only, public) |
| Solution repo | `https://github.com/rbutexas/superset-remediation-agent` (created, **empty**) |
| Devin org | `org-3410a2ac982f458ab46913537161e78a` |
| Devin service user | `superset-remediation-agent` / `service-user-ddb9f705b17a46f592f41424b891295d` (all roles) |
| Local working dir | `/Users/rbhave/superset-remediation-agent` (git init'd, not pushed) |
| Credentials | `~/.devin.env`, mode 600 — `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `GITHUB_TOKEN` |
| Probe script | `/Users/rbhave/devin-probe.sh` (read-only, 17 GETs, costs nothing) |

### ⚠️ Rotate before shipping

**Both the Devin API key and the GitHub PAT were pasted into the chat transcript.**
Rotate both when the demo is done. Neither is in git (`.gitignore` covers `.env*`).

### GitHub token scope
Fine-grained PAT on `rbutexas`, limited to the two repos above:
Metadata:R, Issues:RW, Contents:RW, Pull requests:RW, Actions:R.
Deliberately **no** Administration / Workflows / Secrets — so it cannot change repo
settings or enable workflows. ("Checks" isn't offered on fine-grained PATs; `Actions: Read`
covers CI results via `/actions/runs?head_sha=…`.)

---

## Environment status

| Item | State |
|---|---|
| Fork created | ✅ master-only |
| Issues enabled on fork | ✅ (was off by default; 0 issues filed so far) |
| Actions enabled on fork | ✅ 50 workflows, **41 active** |
| `superset-python-unittest.yml` | ✅ **active** — this is the independent verification |
| `pre-commit.yml` | ❌ `disabled_fork` (has a `schedule:` trigger). Needs a manual click at `/actions/workflows/pre-commit.yml`, or skip — Devin can run pre-commit in-session. |
| Other 8 `disabled_fork` | All have `schedule:` triggers. Includes `codeql-analysis.yml`. Expected, not a problem. |
| Devin → GitHub connection | ✅ live; Devin sees `rbutexas/superset` |
| Repo indexing in Devin | ⬜ off (`indexing_enabled: false`) — optional, enables semantic search |

---

## Verified Devin API facts (probe run 2026-09-17, 17/17 reachable, 0 blocked)

Source of truth was the **published OpenAPI spec** (`docs.devin.ai/v3-openapi.yaml`,
231 operations), not the prose docs. Local copies in `/tmp/devin-*.yaml` (may be
cleared — re-download if needed).

- Teams tier reaches **all** org-scoped endpoints. Enterprise-only endpoints (`org=0` in
  the tag breakdown) will 403 — 14 of the 30 tags are enterprise-only.
- **Automations are enabled** on this org. Not a support ticket.
- **Code Scans are entitled** (scans / findings / profiles all 200).
- Org analytics work: `metrics/{usage,sessions,prs}`, `consumption/daily`.
  `metrics/sessions` returns `avg_acus_per_session`, `sessions_created_by_size`,
  `sessions_with_merged_prs_count` — merged-PRs-per-session is a real outcome metric.
- **`/schedules` is deprecated → 403 from 2026-09-24** for migrated orgs. Use
  **Automations with a `schedule:recurring` trigger** (iCal RRULE, UTC). Do not build on `/schedules`.
- **No outbound webhooks.** Devin receives webhooks; it never calls you back on session
  completion. Observability must be a **tag-scoped polling reconciliation loop**, not a callback.
- **`preflight_available: false`** — automations have no dry-run. A `--dry-run` mode in our
  own orchestrator is a genuine value-add worth demoing.
- No rate-limit headers advertised.
- Live trigger catalogue: **11 sources**. `pagerduty` (4 events), `jira:issue_updated`,
  `slack:usergroup_mentioned` are served but **absent from the published spec enum**.
- Condition engine: two-level DNF (`any` of `all`), 22 operators incl. regex `matches`,
  `globs`, numeric ranges, `between`, `recurrence`.

### Key session fields (from `SessionCreateRequest` / `SessionResponse`)
`prompt`, `repos`, `tags`, `playbook_id`, `knowledge_ids`, `secret_ids`,
**`max_acu_limit`** (hard cap, 1–1000), `devin_mode` (normal/fast/lite/ultra/fusion),
**`structured_output_schema`** (Draft-7 JSON Schema, validated — the highest-leverage
field in the whole API, and invisible from the endpoint list).

Response: `status` (new/claimed/running/exit/error/suspended/resuming) ×
`status_detail` (**`working` / `waiting_for_user` / `waiting_for_approval` / `finished`** /
quota states), `acus_consumed`, `pull_requests[]`, auto-assigned `category`.

`waiting_for_user` = the silent-stall signal. Surfacing it is a real product insight.

### Billing facts (docs/admin/billing/usage)
- **NOT metered:** waiting for your response, **waiting for a test suite**, **setting up and
  cloning repositories**. (I initially told the user the opposite — corrected. Snapshots buy
  *latency*, not credits.)
- Metered: actions taken — planning, context gathering, code execution, files touched.
  **Cost scales with reasoning complexity, not repo size.**
- **"There are no concurrent session limits"** — explicit. Fan-out is free of throttling.
- Teams plan: Automations draw directly from the shared on-demand credit pool.
- No published $/ACU rate anywhere. Must be measured.

### ⚠️ Competitive fact that shapes everything
Devin ships **21 built-in automation templates**. One is **`security-dependency-vulns`**
("Daily scan for known CVEs… prioritizes by severity, and opens fix PRs") and another is
`dependency-updates`. **The naive version of this take-home is a one-click product feature.**
Note its prompt scopes to *"issues **with available patches**"* — everything it skips is the
interesting part.

---

## Verified Superset facts

- 74.8k stars, Python 27.4MB + TypeScript 23.9MB, 12,664 paths, 629 open issues, Apache-2.0.
- **Shallow clone: 441 MB in 16 seconds** (163MB `.git`, ~278MB tree, 10,926 files).
  The "1.1 GB" GitHub figure is the bare repo with full history — not a real cost.
- Repo is **already agent-instrumented**: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `GPT.md`,
  `.claude/`, `.cursor/`.
- **`AGENTS.md` contains an anti-AI-slop policy**: automated findings must name (1) the
  SECURITY.md role/capability matrix row violated and (2) the principal the attacker holds.
  *"Findings that cannot identify both should be filed as questions, not vulnerabilities."*
  → Enforceable via required fields in `structured_output_schema`.
- **Unit tests need no DB.** `superset-python-unittest.yml` has no `services:` block:
  `pytest -n auto ./tests/common ./tests/unit_tests`. Integration/E2E do need Docker — avoid.
- `requirements/*.txt` are generated by `uv pip compile`. Must be regenerated, never hand-edited.
- Superset **already runs Dependabot** (7 ecosystems, daily, grouped security updates) **and CodeQL**.
- TypeScript migration in `superset-frontend/src` is **done** (0 `.js`, 2020 `.ts/.tsx`).
- Cypress→Playwright migration: **29 Cypress files left vs 127 Playwright files**.

### Verified vulnerable packages (live OSV scan, 2026-09-17)

Python (148 runtime pins / 315 dev): `paramiko==3.5.1` (runtime, GHSA-r374-rxx8-8654),
`python-multipart==0.0.29` (8 advisories), `pytest==7.4.4`, `jaraco-context==6.0.1`.

npm (2,793 resolved): `xlsx@0.20.3`, `underscore@1.6.0`, `js-yaml@4.3.1`,
`brace-expansion@5.0.8`, `image-size@0.7.5`, `fflate@0.7.4`, `pacote@21.0.1/21.5.0`,
`smol-toml@1.6.1`.

### The two findings that survived every round of scrutiny

**`xlsx@0.20.3` — the trap.** Both advisories have `introduced: 0` and **no `fixed` event**
→ permanently unfixable in npm's ecosystem. Superset installs it from
`https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz`. **npm's latest is 0.18.5** (verified
against the registry) — so "install it from npm" is a *downgrade* past both fixes into a
genuinely vulnerable version. **An automated remediation bot would make security worse.**

**`simple-zstd` ^1.4.2 → 2.1.0.** Documented failure of automation, in their own repo:
#38662 (Dependabot bump) → #39138 (human fix attempt) → #39139 (**reverted**) → pinned in
`dependabot.yml` with the comment *"Remove this once the proxy code is updated to await the
async decompress() API."* Still undone: `webpack.proxy-config.js:22` still imports
`ZSTDDecompress`, and lines 122-152 carry a hand-rolled workaround for a v1 child-process
leak that the upgrade has to rework.

---

## Where the thinking landed (this is the live question)

Three iterations:

- **v1 (6 issues, all CVEs)** — rejected. Three of the six edited the same two Python
  requirements files with the same procedure; that was padding, and the user caught it.
- **v2 (5 issues across the brief's three named categories)** — better, still weak.
- **v3 — the current proposal.** On stress-testing, v2 failed three ways:
  1. Almost all dependency hygiene — the *least* Superset-specific thing about the repo.
     Superset's real surface is authorization/SQL injection per its SECURITY.md.
  2. 3 of 5 were things a script could do → **Devin as a helper, not a primitive**, which
     the grading note explicitly warns against.
  3. Filing CVEs as GitHub issues is slightly artificial (Dependabot opens PRs, not issues).

### The v3 thesis

> **Superset's `.github/dependabot.yml` ignore list is a graveyard of work no bot can do —
> and the maintainers wrote the acceptance criteria themselves, in comments, next to each entry.**

Measured state of the ignored packages:

| Package | Pinned | npm latest | Stated blocker |
|---|---|---|---|
| `simple-zstd` | ^1.4.2 | **2.1.0** | *"once the proxy code is updated to await the async decompress() API"* |
| `react-checkbox-tree` | ^1.8.0 | **2.0.2** | blocked on issue #39600 — **#39600 is now CLOSED** |
| `@babel/*` | ^7.29.7 | **8.0.5** | `@emotion/babel-plugin` + `babel-plugin-jsx-remove-data-test-id` |
| `@deck.gl/*` + `@luma.gl/*` | ~9.2.5 | 9.4.x | *"must be upgraded together in a manually validated change"* |
| `react` / `react-dom` | ^18.3.0 | **19.3.0** | *"until the application supports React >= 19"* — **too big, out of scope** |
| `currencyformatter.js` | (transitive) | 2.2.0 | peer constraint via `just-handlebars-helpers` |

The `react-checkbox-tree` row is the showcase: Dependabot still ignores it because of an
issue that has since closed. Finding that means reading a config comment, following a
reference, and checking current state. No scanner does that. **That is Devin as a primitive.**

### Open question for next session
**Pressure-test the ignore-list slate for feasibility before committing.** "Too hard to
finish" is a real failure mode. React 19 is definitely out. Need to size `@deck.gl`,
`@babel`, `react-checkbox-tree`, `currencyformatter.js`.

Keep `xlsx` as the single security item regardless — it's the best story in the set.

---

## Planned architecture (not yet built)

- **Trigger:** label on a GitHub issue (`agent:remediate` / `agent:triage`) → Devin
  Automation with a `github:issues` trigger. Plus a `schedule:recurring` automation for
  the periodic re-scan. Satisfies "webhooks for real-time detection" + "Scheduled Devin".
- **Execution:** one Devin session per issue, via a **playbook** (repeated tasks), with
  `max_acu_limit` and a required **`structured_output_schema`**.
- **Verdict vocabulary** (enforced by schema): `fixed` / `mitigated` /
  `declined_not_exploitable` / `escalate_to_human`.
  **`declined_not_exploitable` is a success**, not a failure — an engineer-hour saved on a
  false positive counts the same as one saved on a fix. No incumbent scores it.
- **Schema must require** `assumed_principal` and `security_md_matrix_row` for security
  findings → provably satisfies Superset's published anti-slop policy.
- **Observability:** polling reconciliation loop over `GET /sessions?tags=…` (no outbound
  webhooks), joined with `consumption/daily/sessions/{id}` for cost-per-outcome and
  `metrics/prs` for merged-PR throughput. Surface `waiting_for_user` as the stall signal.
- **Differentiator over the built-in template:** dry-run mode (Devin has none),
  typed verdicts, refusals as first-class outcomes, cost-per-outcome analytics.

---

## Next actions, in order

1. **Pressure-test ignore-list feasibility** (free, GitHub/npm API only) → lock the slate.
2. Rewrite `docs/ISSUE-SLATE.md` — **the current file is v1 and is STALE/SUPERSEDED.**
3. File the issues on `rbutexas/superset` via the API (free).
4. **Calibration session** — one session, `max_acu_limit: 15`, on the cheapest item.
   Measures: real ACU cost, whether egress reaches PyPI/npm/cdn.sheetjs.com, whether
   Superset's unit suite runs in-session, whether structured output validates.
   **User must approve before this runs — it is the first thing that spends credits.**
5. Build the orchestrator against fixtures (Docker, tests, analytics collector) — no credits,
   no dependency on step 4.
6. Scale to the full slate, then the deck and the Loom.

## Standing user preferences

- **Tell them before spending any credits**, every time. Give an estimate and a cap.
- They are not deeply familiar with this tooling — **explain in plain language, define
  jargon, no invented codenames** (the "SR-1/SR-2" labels confused things and were dropped).
- They push back well on weak reasoning. Don't defend a bad idea — re-derive it.
- Everything under `rbutexas`. Nothing touches `rbhaveMeta`.
