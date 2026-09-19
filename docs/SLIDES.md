# Slides

Ten slides. Every number is real and re-derivable — `tools/verify_claims.py`
(42 assertions) or the live dashboard. Nothing here is estimated.

Speaker notes are what you actually say. Slide text is what goes on screen, and
it is deliberately sparse: if they are reading, they are not listening.

---

## 1 — Title

> # Nobody produces verdicts
>
> ### Autonomous remediation on Apache Superset
> Riddhi Bhave

**Say (20s)**

I picked Superset because it is the hardest possible case for the argument I
want to make. They already have every tool you would buy. So if I can find
work worth doing there, the problem is not tooling.

---

## 2 — The problem

> ## Superset already has
>
> Dependabot · 7 ecosystems · daily
> CodeQL
> Dependency review on every PR
> **41 active CI workflows**
>
> ## They are not short of findings.

**Say (40s)**

They are short of the engineer who reads the output and decides what to do.

That decision — *is this real, can it be fixed, will fixing it break something,
is it worth the churn* — is the expensive step. It is also the only step in the
pipeline with no tooling at all.

Scanners produce findings. Bots produce patches for the easy ones. **Nothing
produces verdicts.** That gap is where the backlog actually lives.

---

## 3 — Three things I found in their repo

> | | |
> |---|---|
> | **A note nobody re-read** | Upgrade blocked pending two things. Both happened. Nobody checked. |
> | **A migration left half-done** | Tried → broke → reverted → pinned with a TODO. The TODO is still there. |
> | **Debris from a merged upgrade** | A bump updated 114 test files and missed 52. |

**Say (50s)**

None of these are exotic. All three are the same failure: **a dependency
upgrade does not end when the version number changes, and nobody owns the tail.**

The middle one is worth dwelling on. `simple-zstd`. The history is in their
repo: a bot opened the upgrade in March, it broke the dev proxy, an engineer
spent a day trying to fix it and reverted, the bot reopened it, it sat for two
months, and they pinned it so the bot would stop asking.

**Three rounds of engineering effort. No result.** Still on a release from
October 2022.

That is not a hypothetical about the limits of automation. It is their commit
history.

---

## 4 — The system

```
  scanner ──▶ issue ──▶ TRIAGE session ──▶ decision
  (free)      +label     (Devin)             │
     ▲                                       │ only if "remediate"
     │ weekly                   label ◀──────┘
  schedule                        │
                                  ▼
                        REMEDIATION session ──▶ pull request
     collector ◀────── poll ──────┘
         │
         ▼
      dashboard
```

**Say (45s) — the decision to lead with**

One architectural choice worth stating, because it pre-empts the obvious
objection.

**I deliberately did not use Devin for detection.** Finding these is
deterministic and cheap, so it is a Python script with no dependencies and no
credit cost.

Devin is used only where the work needs judgement. So when you ask *"couldn't a
script do this?"* — yes, and it does. That is the scanner. The agent does the
part the script cannot.

The second choice: **the scanner assigns no verdict.** It emits facts and open
questions. A Devin triage session reads those *and the repository* and decides.
If the verdict came from an `if` statement in my code, my whole argument would
answer itself.

---

## 5 — Live

**No slide. Demo.**

Label an issue `agent:triage`. A session appears in seconds. The board moves.

**Say (25s)**

There is no glue service here. GitHub emits a webhook on every label change,
Devin's automation matches a condition, and a session starts. Which means
anything that can label an issue can trigger this — Jira, a scanner, a person.

---

## 6 — What Devin actually did

**Session replay, narrated.**

**Say (75s)**

`simple-zstd`. The blocker was forty lines of hand-written workaround: v1's
decompression stream leaked a child process when the connection died mid-way,
so the code chased it down and killed it manually.

Devin deleted those forty lines. Not by guessing — it read the new library's
source and cited the mechanism that made them redundant. Then it wrote **its
own experiment**: pipe half a payload in, kill the source, and count running
`zstd` processes until they return to baseline.

Nobody asked it to write that test. The issue asked a question — *is the
workaround still needed* — and it designed a way to answer it.

It also **corrected my issue.** I had written that v2 uses a pre-spawned
process pool. It checked, and told me that only applies to a different class;
the function actually used spawns one child per call.

**−363 lines, +85.** The fix was mostly deletion, which is why nobody got there
by pattern-matching.

---

## 7 — Why it worked, per issue

> | Finding | Result | Why |
> |---|---|---|
> | `simple-zstd` | **Fixed** — PR #3 | Existing test covered the exact failure. Read the library source to justify deleting the workaround. |
> | `userEvent` | **Fixed** — PR #5 | 77 call sites across 15 files. Declared the one expectation it changed. Filed follow-up #6 for the remaining 120. |
> | `xlsx` | **Dismissed** | Correct answer was no change. See next slide. |

**Say (60s)**

The middle row is where I would push back on my own system, and it did the job.

My issue said: *if awaiting a call reveals that an assertion was only passing
because of the race, fix the assertion and say so explicitly — do not silently
change what a test means.*

It changed exactly one expectation, and called it out by name in the PR.

It also found that two of the sites I flagged were **inside block comments** —
commented-out test bodies, not executable code — and left them alone. My
detector could not tell the difference. It could.

And it contradicted my premise: *"the issue's claim that these 29 sites produce
flakes today could not be reproduced — all 15 files passed on the base branch."*
It fixed the latent defect anyway, and told me my framing was wrong.

---

## 8 — The one where the right answer was "no"

> ```
>   0.18.5   ← what the registry gives you      ✗ both advisories apply
>   0.19.3   ← advisory 1 fixed here
>   0.20.2   ← advisory 2 fixed here
>   0.20.3   ← what Superset installs           ✓ patched
> ```
>
> ### The obvious automated fix introduces the vulnerability.

**Say (55s)**

A scanner flags this library as vulnerable. It will flag it forever — the
advisory has no fixed version, because the publisher left the registry and the
database has nothing to record.

Superset installs from the vendor at 0.20.3. They are patched. But the obvious
remediation — *reinstall it properly from the registry* — resolves to 0.18.5.
**Backwards, past both fixes, into genuinely affected code. And the scanner goes
green.**

Devin investigated and refused. It also did something I did not ask for: it
checked *who imports the library*. One file, which only writes spreadsheets —
and both advisories concern parsing untrusted ones. So the vulnerable code is
not even reachable.

Your own `security-dependency-vulns` template would not have caught this. Its
prompt scopes to *"issues with available patches"*, and nothing in it checks
whether the fix is a downgrade.

**That is the difference between automation that closes tickets and automation
you can leave running.**

---

## 9 — How you would know it is working

> ## 3 findings in · 3 answered · 0 outstanding
>
> **2 pull requests · 1 dismissed with evidence**
>
> Not a fix count. An **outcome mix**.

**Say (55s) — the analytics callback**

Three things I would put in front of you as an engineering leader.

**First, the metric is the mix, not the fixes.** A pipeline scored on fixes will
produce fixes — including for the finding where fixing it introduces a
vulnerability. So a dismissal with evidence counts as a resolution, at equal
weight. It is also the scarcer outcome: nothing else on the market produces one.

**Second, cost against documented prior cost.** Superset has already spent
engineer time on `simple-zstd` three times and still has the problem. I can tell
you precisely what this run cost in agent time. I will not give you an
engineer-hours-saved figure, because that is an estimate and you would be right
to discount it.

**Third, the board tracks what nobody instruments: work blocked on a person.**
An agent waiting for an answer is consuming budget and has told no one. That is
how these rollouts quietly fail, and it is a first-class alert here.

---

## 10 — Next steps

> | When | What |
> |---|---|
> | **Weeks 1–2** | Same pipeline, your intake — Jira, Linear, PagerDuty, CI failures are all native triggers |
> | **Weeks 2–4** | Fan out across repos. One ignore file is a curiosity; two hundred is a programme. |
> | **Month 2** | Ingest your existing scanner via `code-scans/ingestion` — verdicts on the backlog you already have |
> | **Month 2** | Make the outcome mix an SLO: *95% of findings reach a verdict in 48 hours* |
> | **Ongoing** | Policy gates as the adoption dial: dry-run → triage only → remediate with review → autonomous on dev tooling |

**Say (50s)**

Two closing points.

**The scheduled re-validation is the product.** Blockers clear silently. One of
these became actionable in August and nothing told anyone. Every org has that,
in every repo, and nobody has a job that finds it.

**And on rollout — everything here is dev tooling and CI. Zero production blast
radius.** That is deliberate. You pilot an autonomous agent on the webpack
config, not on payment code. Once the outcome mix is trusted, you widen the
intake. The policy gate is the dial.

---

## Anticipated questions

**"Couldn't a script do this?"**
Half of it, and it does — that is the scanner, no credits. The agent does the
remediation and the judgement. `simple-zstd` is the proof: a bot tried, a human
tried, it was reverted.

**"How do I know the fixes are correct?"**
You do not take the agent's word. Verification reads Superset's own CI on the
PR's head commit. And where a runtime failure is the risk, the acceptance
criteria say explicitly that passing unit tests is not sufficient — because last
time it passed CI and still broke every dashboard.

**"What stops it running up a bill?"**
Two caps. A hard per-session ACU limit enforced by Devin, and our own
concurrency limit — Devin imposes none, and unbounded fan-out across a large
finding set is how a budget disappears overnight.

**"What if it does something wrong?"**
Findings carry guardrails — explicit prohibitions rendered into the prompt. The
`xlsx` finding carries *"do not change the install source to the npm registry"*
and the reason. It is data attached to the finding, not a hope about model
behaviour. All three runs respected theirs.

**"This is all dev tooling. Where is the customer impact?"**
Deliberate. Zero production blast radius is where you start. The chain is real —
flaky tests mean developers stop trusting CI, failures get re-run instead of
investigated, bugs ship — but I would rather show you a pilot you would actually
approve.

**"Your numbers are small."**
On one repo, yes. The unit is not the finding, it is the repository. And the
thing being automated — producing a verdict — is the step your queue is actually
blocked on.

**"Did it ever get anything wrong?"**
Not on these three. But I found three bugs in *my* code by running it for real —
a threading error that silently stopped the pipeline, a misread API field that
dropped every pull request, and a live view that could not name the work it was
showing. Unit tests missed all three, because in each case I had invented the
shape I expected rather than the one Devin produces.

---

## Timing

| Slides | Target |
|---|---|
| 1–3 problem + evidence | 1:50 |
| 4 architecture | 0:45 |
| 5–6 live + replay | 1:40 |
| 7–8 per-issue + the refusal | 1:55 |
| 9–10 analytics + next steps | 1:45 |
| | **7:55 — must cut to 5:00** |

**Cutting to five minutes.** Drop slide 7 to a single line on slide 9 ("two
fixed, one dismissed, all three respected their guardrails"), compress slide 3
to the `simple-zstd` story only, and trim slide 6 to the one beat that matters:
*it wrote its own test.* That lands at about 5:10, which is close enough.

Do not cut slide 8. It is the only one that is hard to argue with.
