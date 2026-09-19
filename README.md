# Remediation agent

Event-driven remediation of engineering debt in [Apache Superset](https://github.com/apache/superset), built on the [Devin API](https://docs.devin.ai/api-reference/overview).

### See it in 60 seconds — no account, no credentials

```bash
docker compose up replay
```

Then open **<http://localhost:8766>** — the dashboard, showing a recorded run
against the real repository.

```bash
docker compose run --rm checkout    # shallow-clone superset, ~440 MB / 16s
docker compose run --rm scan        # find the issues yourself, live
```

Neither reads a token. [More on reproducing this](#reproducing-this), including
what you **cannot** reproduce without your own Devin organisation.

---

## The problem

Superset runs Dependabot across seven ecosystems, daily. CodeQL. Dependency review on every pull request. Forty-one active CI workflows.

They are not short of findings. They are short of the engineer who reads the output and decides what to do with it.

That decision — *is this real, can it be fixed, will fixing it break something, is it worth the churn* — is the expensive step, and it is the only step with no tooling at all. **Scanners produce findings. Bots produce patches for the easy ones. Nothing produces verdicts.**

This produces verdicts.

## What it found

Running against `apache/superset@master`, with nothing hard-coded:

| Finding | What it is |
|---|---|
| `stale-suppression:react-checkbox-tree` | An upgrade suppressed pending two conditions. Both have since been met. |
| `unfinished-migration:simple-zstd` | Tried, broke the build, reverted, pinned with a TODO. The TODO is still there; the package is on a release from 2022. |
| `unawaited-user-event` | A merged upgrade updated 114 test files and missed 52. The lint rule that catches it is installed and switched off. |
| `unresolvable-advisory:xlsx` | A security warning that can never be cleared — and whose obvious fix is a downgrade past the patch. |

The first of those was found by reading a comment in a config file, following its links **into a different GitHub repository**, checking whether a pull request had merged and shipped, and checking an issue's close *reason*. No scanner does that.

## How it works

```
  scanner  ──▶  issue (agent:triage)  ──▶  TRIAGE session  ──▶  decision
  (free)          on the fork               (Devin)               │
     ▲                                                            │
     │ weekly                          agent:remediate  ◀─────────┘ only if
  schedule:recurring                          │                     "remediate"
  automation                                  ▼
                                    REMEDIATION session ──▶ pull request
                                         (Devin)                 │
  collector ◀── poll by tag ─────────────────────────────────────┘
      │
      ▼
  dashboard   outcome mix · cost · time-to-verdict · stalls · CI verification
```

**The scanner does not decide what a finding deserves — Devin does.** The scanner emits facts, evidence and open questions with no disposition attached. A triage session reads those *and the repository* and returns one of four verdicts.

That split is the whole design. If the verdict came from an `if` statement in this codebase, the argument above would answer itself.

**A refusal is a success.** `decline_not_actionable` and `blocked_upstream` both resolve a finding. An hour saved dismissing a false positive is worth the same as an hour saved landing a fix, and it is the scarcer outcome. A pipeline measured on fixes will produce fixes — including for findings that should have been left alone.

---

## Reviewer walkthrough

Two tiers. The first needs nothing from you and takes about two minutes.

### Prerequisites

Docker, and that is all. Everything below runs in a container; nothing is
installed on your machine and no token is read.

*(Python 3.11+ optionally, if you would rather run the tests directly.)*

---

### Tier 1 — see it working, with no account

**Step 1. Clone and start the dashboard.**

```bash
git clone https://github.com/rbutexas/superset-remediation-agent
cd superset-remediation-agent
docker compose up replay
```

First run builds the image, roughly a minute. You will see:

```
  dashboard   http://localhost:8766
  json        http://localhost:8766/api/report.json
  refreshing  every 5s
```

**Step 2. Open <http://localhost:8766>.**

The dashboard, showing a **recorded run against the real repository** — findings
detected, what the agent decided about each, how long it took, and a link
through to the agent's own transcript. Every row came from a real session;
nothing is authored.

Port already in use? `REPLAY_PORT=8777 docker compose up replay`.

**Step 3. Find the issues yourself, live.**

```bash
docker compose run --rm checkout    # shallow-clone superset, ~440 MB / 16s
docker compose run --rm scan        # detect findings against the real repo
```

Four findings, and a routing table explaining why each one needs an agent's
judgement rather than a rule. This reads the actual repository over the network
— it is not replaying anything.

**Step 4. Check the claims those findings rest on.**

```bash
pip install pytest && python3 -m pytest        # 63 tests
python3 tools/verify_claims.py --repo ./work/superset
```

The second re-derives every factual statement in the issue set — versions,
advisory ranges, pull-request states across two repositories — as 42 assertions
against live APIs. If a claim is not in there and passing, it is not in an issue.

**Step 5. See what would be dispatched, without dispatching it.**

```bash
docker compose run --rm scan --json | head -40
```

---

### Tier 2 — run the pipeline against your own fork

The pipeline cannot be pointed at someone else's Devin organisation or GitHub
repository, so this means standing up your own.

**Step 1. Fork `apache/superset`.** Then enable two things that are **off by
default on a fork**:

- *Settings → Features → Issues*
- the *Actions* tab → "I understand my workflows, go ahead and enable them"

**Step 2. Connect the fork in Devin** — *Settings → Connections → GitHub*.
Grant access to that repository only.

> **If your fork is public, also set Automation scope → All installed repos**
> on the connection. GitHub automations are private-repo-only by default, and
> without this the triggers never fire — with no error anywhere. Devin's own
> reasoning is prompt-injection risk on public repos; the triggers here require
> a *label*, which requires write access, so the exposure is collaborators
> rather than the internet.

**Step 3. Create a fine-grained GitHub token** scoped to the fork, with five
permissions: Metadata:R, Issues:RW, Contents:RW, Pull requests:RW, Actions:R.
Deliberately not Administration, Workflows or Secrets.

**Step 4. Credentials.**

```bash
cp .env.example ~/.devin.env && chmod 600 ~/.devin.env
$EDITOR ~/.devin.env        # three values
```

**Step 5. Check everything is wired up before spending anything.**

```bash
docker compose run --rm agent status
```

Expect credentials to resolve, all required triggers present, and automations
absent.

**Step 6. Provision, disarmed.**

```bash
docker compose run --rm agent provision --disabled
```

Creates two playbooks, the labels, and three automations — **inert**. You can
inspect them in the Devin UI. Nothing fires.

**Step 7. File the issues.** Free, because nothing is listening yet.

```bash
docker compose run --rm agent file
```

**Step 8. Arm.** This is the switch: from here, labelling an issue starts a
session and spends credits.

```bash
docker compose run --rm agent arm
```

**Step 9. Start the dashboard**, with the collector advancing behind it.

```bash
docker compose up agent          # http://localhost:8765
```

**Step 10. Label an issue `agent:triage`** and watch. Within seconds a session
appears; the board moves to *Being assessed*; when the verdict lands the
reasoning is posted to the issue and the item is either closed or promoted to
`agent:remediate`, which starts a remediation session.

To stop everything: `docker compose run --rm agent arm --off`, then
`docker compose run --rm agent cleanup`.

---

### What you cannot reproduce

Being straight about it:

- **The exact sessions.** Session IDs and transcripts live in the Devin
  organisation that ran them. `docs/evidence/` carries the raw structured
  output so the results can be inspected without access.
- **The cost figures.** ACU consumption is not reported by the API on the Teams
  tier — see decision 31. The dashboard prints "not yet reported" rather than a
  confident zero.
- **Identical findings, forever.** The detectors read live data. When Superset
  merges the `simple-zstd` upgrade, that finding correctly disappears. These are
  a snapshot of a real repository, not a fixture — which is the point, and why
  `tools/verify_claims.py` exists to re-prove them on demand.

## Commands

Deliberately separate verbs, ordered by consequence. Scanning is free; filing writes to a repository; dispatching spends a metered budget. Collapsing those into one command means a re-run of a scan can silently cost money.

| Command | Effect |
|---|---|
| `scan` | Detect findings and show how each would route. **Free, read-only.** |
| `provision` | Create playbooks, labels and the two automations. Free. |
| `file` | Create or update the issues. **Writes to GitHub.** |
| `dispatch` | Start triage sessions. **Spends credits.** |
| `collect` | Poll sessions, record state, advance triage into remediation. Free. |
| `report` | Render the dashboard — `--format text\|html\|json`. Free. |
| `serve` | Live dashboard on localhost, updating as sessions run. Free. |
| `arm` | Arm or disarm the automations. **Arming makes `file` cost money.** |
| `cleanup` | Terminate finished sessions still holding resources. Free. |
| `status` | Credentials, trigger support, automation state, store totals. Free. |

`scan` and `report` need **no credentials at all** — they touch neither API.

`--dry-run` works on every verb that would otherwise create something. It resolves findings, renders the exact prompts, and prints what *would* be dispatched.

That flag exists because Devin's own automations report `preflight_available: false` — there is no way to test-fire one without it doing the work. For a system with a spend meter and write access to a repository, "run it and see" is not an acceptable first rehearsal.

### A full run

```bash
remediation-agent --dry-run dispatch     # read the prompts first
remediation-agent provision
remediation-agent file
remediation-agent dispatch --only unresolvable-advisory:xlsx
remediation-agent collect --until-idle
remediation-agent report
```

---

## Observability

> *"If I were an engineering leader, how would I know this is working?"*

Not from a count of fixes. The dashboard leads with the **outcome mix** — how many findings reached a verdict, and which verdicts — because a pipeline measured on fixes will produce fixes it should not have.

Four things it surfaces that a naive dashboard omits:

- **Unresolved findings** — filed, no verdict. The queue's real backlog.
- **Stalled sessions** — billing, blocked on a person, nobody told. This is how agent rollouts quietly fail.
- **Heuristic agreement** — how often the cheap rule matched the agent. Where they differ is where the judgment happened.
- **Admitted gaps** — where a remediation said it could not verify something. Hiding these would make the dashboard less trustworthy, not more.

```bash
remediation-agent report --format html --out report.html
```

---

## Security

- **No secret is written to disk, logged, or included in a report.** `Config.redacted()` exists so configuration can be printed safely.
- **The GitHub token is fine-grained and scoped to two repositories**, with five permissions: Metadata, Issues, Contents, Pull requests (RW), Actions (R). Deliberately **not** Administration, Workflows or Secrets — the credential that drives an autonomous loop should not be able to reconfigure the repository it is working on.
- **Two independent spend caps.** A hard per-session ACU limit enforced by Devin, plus our own concurrency limit. Devin documents *no* concurrency ceiling, so nothing upstream provides one.
- **Findings carry guardrails** — explicit prohibitions rendered into both the issue and the prompt. `unresolvable-advisory:xlsx` carries *"do not change the install source to the npm registry"* and the reason. An under-specified objective gets satisfied the cheapest way available, and for that finding the cheapest way is a downgrade.
- The container runs as a non-root user and mounts the audited checkout read-only.

---

## Verification

Success is read from **Superset's own CI** on the pull request's head commit, not from the agent's self-report. `github.py::verification_summary` returns which checks ran and which failed.

The argument for that is in the repository's own history: PR #39261 passed CI and still broke every dashboard, because the failure was a runtime error the unit tests never touched. So the `react-checkbox-tree` finding's acceptance criteria say explicitly that passing unit tests does not close it.

Separately, every factual claim used in the issue set is an executable assertion:

```bash
python3 tools/verify_claims.py --repo /tmp/ss    # 42 checks against repo, npm, OSV, GitHub
```

If a claim is not in that script and passing, it does not go in an issue.

---

## Documentation

| | |
|---|---|
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | 28 architectural decisions, each with its cost. Start at 18. |
| [`docs/ISSUES.md`](docs/ISSUES.md) | The issue set, with evidence |
| [`docs/DECK.md`](docs/DECK.md) | Presentation outline and speaker notes |
| [`docs/STATE.md`](docs/STATE.md) | Verified API and repository facts |

## Tests

```bash
.venv/bin/python -m pytest        # 44 tests, no warnings
```

`filterwarnings = ["error"]` is set, so a warning fails the suite.

## Licence

Apache-2.0, matching the repository under audit.
