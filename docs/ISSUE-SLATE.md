> # ⚠️ SUPERSEDED — v1, kept for history
>
> This slate was rejected during review on 2026-09-17/18. Three of its six items
> (`paramiko`, `python-multipart`, `pytest`) edited the same two Python requirements
> files with the same procedure — that was padding, not six issues.
>
> It was also almost entirely dependency hygiene, which is the least Superset-specific
> thing about the repo, and 3 of 5 were tasks a script could do — i.e. Devin as a helper,
> not a primitive, which the grading note warns against.
>
> **See `STATE.md` for the current v3 thesis (the `dependabot.yml` ignore list).**
> The verified evidence below is still accurate and worth keeping.

# Issue Slate — what the agent will remediate, and why each one is here

Every finding below was **verified live** against the OSV API, the GitHub API, and
the current contents of `apache/superset@master` on 2026-09-17. None are hypothetical.

The slate is deliberately heterogeneous. A set of six clean version bumps would prove
nothing — Dependabot already does that, daily, in this very repo. Each issue here is
chosen to exercise a **different failure mode of deterministic automation**.

## The bar

Two incumbents already exist and must be beaten, or the work scores zero:

1. **Dependabot** — already configured in `apache/superset/.github/dependabot.yml`,
   running daily across 7 ecosystems, with grouped security updates.
2. **Devin's own `security-dependency-vulns` template** — ships in-product, one click:
   *"Daily scan for known CVEs… prioritizes by severity, and opens fix PRs."*

Note the template's own prompt: *"For critical/high severity issues **with available
patches**, update the dependency and run tests."* Everything it skips is the interesting
part. That skipped set is this slate.

## Pre-registered predictions

Outcomes are declared **before** execution. The rubric asks us to explain why Devin
worked or didn't for each issue; predicting first and reporting actuals afterwards is
the honest version of that. A miss is a finding, not an embarrassment.

Verdict vocabulary (enforced by the session's `structured_output_schema`):

| Verdict | Meaning |
|---|---|
| `fixed` | Patch applied, tests pass, PR opened |
| `mitigated` | Not upgradable; compensating change made |
| `declined_not_exploitable` | Investigated, no action correct, reasoned justification given |
| `escalate_to_human` | Requires a judgment call outside the agent's remit |

`declined_not_exploitable` is a **success**. An engineer-hour saved on a false positive
is worth the same as an engineer-hour saved on a fix, and no incumbent scores it.

---

## SR-1 — `simple-zstd` v1 → v2: webpack dev-proxy API break

**Class:** upgrade blocked by a source-level API change
**Complexity:** High — this is one of the two "complex" issues
**Evidence:**
- `superset-frontend/package.json` → `"simple-zstd": "^1.4.2"`
- `.github/dependabot.yml` → explicitly ignored, with a hand-written justification
- `superset-frontend/webpack.proxy-config.js:22` → `import { ZSTDDecompress } from 'simple-zstd'`

**Documented history — automation demonstrably failed here:**

| Issue | State | What happened |
|---|---|---|
| #38662 | closed | Dependabot bumped 1.4.2 → 2.1.0 |
| #39138 | closed | A human attempted to fix the v2 API breakage |
| #39139 | closed | **Reverted** to 1.4.2 |

Dependabot then reopened the same bump, so the maintainers pinned it in
`dependabot.yml` rather than fight it. The repo comment says it plainly:
*"Remove this once the proxy code is updated to await the async decompress() API."*

**Why it is genuinely hard.** It is not a rename. `webpack.proxy-config.js:122-152`
carries a hand-rolled workaround for a v1 defect — `ZSTDDecompress()` returns a stream
backed by a real `zstd -d` child process that does not forward `.destroy()`, leaking
the child on upstream error. The code tracks `zstdChild` / `killZstdChild` to handle
the race. Upgrading means reworking that workaround against v2's async API and
deciding whether it is still needed at all.

**Incumbent coverage:** Dependabot — tried, reverted, now ignores it.
Devin template — no CVE, so out of scope entirely.

**Prediction:** `fixed`, but with **low confidence on verification**. This is dev
tooling; there is no unit test for the webpack dev proxy. Devin can make the change
correct-looking but cannot easily prove it. Expect an honest
`escalate_to_human` on the verification step, which is the *right* answer.

---

## SR-2 — `xlsx@0.20.3`: the permanently-unfixable advisory

**Class:** false positive that a naive bot would make *worse*
**Complexity:** High — the second "complex" issue
**Evidence:**
- `package.json` → `"xlsx": "https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz"`
- OSV flags `GHSA-4r6h-8v6p-xvw6` (prototype pollution) and `GHSA-5pgg-2g8v-p4x9` (ReDoS)
- **Both advisories have `introduced: 0` and no `fixed` event** — an unbounded range

**Why the advisory is unbounded.** SheetJS stopped publishing to npm at `0.18.5` and
now ships only from their own CDN. The fixes (0.19.3, 0.20.2) exist, but not as npm
versions — so the GitHub Advisory Database has no version it can point at as fixed.
The finding is therefore *permanent by construction*.

Superset is on **0.20.3, above both fix versions**. They are almost certainly already
patched. Every scanner will flag this forever regardless.

**The trap.** The obvious remediation — "install `xlsx` from npm" — resolves to
`0.18.5` and lands in a **genuinely vulnerable** version. An automated bot optimising
for "make the scanner green" actively degrades security here.

**Incumbent coverage:** Dependabot cannot act — it is not a registry dependency.
`npm audit` is largely blind to CDN tarball installs. The Devin template's own prompt
scopes to *"issues with available patches"* and skips it.

**Prediction:** `declined_not_exploitable`, with the CDN version compared against the
advisory fix versions as evidence, plus a recommendation to record a suppression so it
stops consuming triage attention. **The key result is that Devin does not downgrade.**

---

## SR-3 — `paramiko 3.5.1` → patched (`GHSA-r374-rxx8-8654`)

**Class:** clean runtime CVE — the control case
**Complexity:** Low, by design
**Evidence:** `requirements/base.txt` → `paramiko==3.5.1`; the only vulnerable package
in the 148 pinned **runtime** dependencies.

**Why it is in the slate.** Every experiment needs a control. SR-3 establishes the
baseline ACU cost of a clean end-to-end remediation — the denominator for the whole
ROI argument. It also exercises the one Superset-specific step a generic agent misses:
`requirements/*.txt` is generated by `uv pip compile` and must never be hand-edited.
Getting that right is the difference between a mergeable PR and CI failure.

**Verification:** `pytest ./tests/common ./tests/unit_tests` — confirmed to need no
Postgres or Redis (`.github/workflows/superset-python-unittest.yml` has no `services:`
block), so it runs inside a session.

**Prediction:** `fixed`, high confidence. If this one fails, the pipeline is broken
and nothing else in the slate is trustworthy.

---

## SR-4 — `python-multipart 0.0.29`: eight advisories, one package

**Class:** severity triage across a cluster of findings
**Evidence:** `requirements/development.txt`; OSV returns 8 advisories —
`GHSA-5rvq-cxj2-64vf`, `GHSA-6jv3-5f52-599m`, `GHSA-v9pg-7xvm-68hf`,
`GHSA-vffw-93wf-4j4q`, plus 4 PYSEC identifiers.

**Why it is here.** Tests whether the agent reasons about **reachability** rather than
counting advisories. This is a dev-tree dependency; per Superset's `SECURITY.md` trust
model, exposure depends on whether it sits in a path an untrusted principal can reach.
Eight advisories on a dev dependency is not eight production vulnerabilities, and the
output must say which.

**Prediction:** `fixed` or `mitigated`. The interesting artifact is the reasoning, not
the diff.

---

## SR-5 — `underscore@1.6.0`: fixing what is already being deleted

**Class:** correct refusal — the churn-avoidance case
**Evidence:** `underscore@1.6.0` in the frontend lockfile —
`GHSA-cf4h-3jhx-xvhq` (arbitrary code execution), `GHSA-qpx9-hpmf-5gmw`. A deep
transitive of the **Cypress** tree. `AGENTS.md` states: *"Cypress is deprecated — will
be removed once migration is completed"*; Playwright is the replacement and
`superset-playwright.yml` already runs in CI.

**Why it is here.** The correct engineering answer is probably *do nothing*. Forcing a
transitive override into a dependency tree scheduled for deletion is churn: it risks
breaking a working E2E suite to remediate a dev-only dependency with no untrusted-input
path. The right output is a reasoned decline plus a pointer to the migration.

This is the issue that distinguishes **judgment** from **activity**. Both incumbents
would either open a noisy override PR or silently skip it; neither explains itself.

**Prediction:** `declined_not_exploitable`, referencing the Playwright migration.
Failure mode to watch: Devin "helpfully" forcing an override anyway.

---

## SR-6 — `pytest 7.4.4` → 8.x: the bump that breaks the thing that proves bumps work

**Class:** upgrade with blast radius across the verification harness itself
**Evidence:** `requirements/development.txt` → `pytest==7.4.4`; `GHSA-6w46-j5rx-g56g`.

**Why it is here.** pytest 8 changed fixture scoping and collection behaviour. Superset
has a large suite with custom fixtures (`SupersetTestCase`, `@with_config`,
`@with_feature_flags`). The bump is trivial to *write* and non-trivial to *land*.

Critically, pytest is the tool every other issue's verification depends on. This tests
whether the agent notices it is modifying its own measuring instrument.

**Prediction:** `escalate_to_human` or a partial `fixed` with failures reported
honestly. **A confident green here would be a red flag** — it would suggest the agent
did not actually run the suite.

---

## Coverage check against the rubric

| Requirement | Met by |
|---|---|
| 5+ issues, or 2+ very complex | 6 issues; SR-1 and SR-2 are both genuinely complex |
| Demonstrates judgment | SR-2 and SR-5 are *correct refusals*, not fixes |
| "Explains why Devin worked/didn't work for each issue" | Pre-registered predictions per issue, actuals reported after |
| Beats the incumbents | Every issue names why Dependabot and the built-in template miss it |

## Distribution of predicted outcomes

| Verdict | Issues |
|---|---|
| `fixed` | SR-1, SR-3, SR-4 |
| `declined_not_exploitable` | SR-2, SR-5 |
| `escalate_to_human` | SR-6 |

Three fixes, two reasoned declines, one honest escalation. A slate that predicted six
green results would not be believable, and would not be worth building a system for.

## Open items

- [ ] Confirm session network egress permits PyPI, npm, and `cdn.sheetjs.com`
      (`default_net_policy` currently advertises only `git-manager.devin.ai`)
- [ ] Measure baseline ACU on SR-3 before running the rest
- [ ] Decide snapshot vs cold-start after measuring install cost
