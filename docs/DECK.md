# Deck + narrative

Audience: a VP of Engineering, with senior ICs in the room who will test every claim.

**Rule: every number here comes from `tools/verify_claims.py` (42 assertions) or
from the live analytics. Nothing is estimated. If a number is not in the script,
it does not go on a slide.**

Runtime target: 5 minutes. Slide count deliberately low — the artifacts carry it.

---

## Slide 1 — The problem is not finding things

> ## Your scanners work.
> ## Nothing acts on what they find.

**Speaker notes (~40s)**

Apache Superset runs Dependabot across seven ecosystems, daily. CodeQL. Dependency
review on every PR. Forty-one active CI workflows. By any measure, best-in-class
tooling.

They are not short of findings. They are short of the engineer who reads the
output and decides what to do.

That decision — *is this real, can it be fixed, will fixing it break something,
is it worth the churn* — is the expensive step. It's also the only step with no
tooling at all. Scanners produce findings. Bots produce patches for the easy
ones. **Nothing produces verdicts.**

---

## Slide 2 — What that looks like in a real repository

> ## Three things I found in Superset that every one of their tools missed

| | |
|---|---|
| **A suppression nobody re-read** | An upgrade was blocked pending two things. Both happened. Nobody checked. |
| **A migration left half-finished** | Tried, broke, reverted, pinned with a TODO. The TODO is still there. |
| **Debris from a merged upgrade** | A bump updated 114 test files and missed 52. Tests now fail at random. |

**Speaker notes (~50s)**

None of these are exotic. All three are the same failure: **a dependency upgrade
doesn't end when the version number changes, and nobody owns the tail.**

The second one is worth dwelling on. `simple-zstd` — the history is in their repo:
a bot opened the upgrade, a human tried to fix the breakage it caused, it was
reverted, and then it was pinned so the bot would stop suggesting it. Three rounds
of engineering effort, no result. Still pinned to a release from **October 2022**.

That's not a hypothetical about automation's limits. It's their commit history.

---

## Slide 3 — The system

```
   ┌────────────┐   findings    ┌──────────┐   label    ┌────────────┐
   │  Scanner   │──────────────▶│  Issues  │───────────▶│ Automation │
   │ 3 detectors│   (0 credits) │ on fork  │  = event   │  (Devin)   │
   └────────────┘               └──────────┘            └─────┬──────┘
         ▲                                                    │ session
         │ weekly                                             ▼
   ┌─────┴──────┐                                      ┌─────────────┐
   │  Schedule  │                                      │  Playbook   │
   │ re-validate│                                      │  + output   │
   └────────────┘                                      │   schema    │
                                                       └──────┬──────┘
   ┌────────────┐   poll by tag                               │
   │ Collector  │◀────────────────────────────────────────────┘
   │ + verdicts │                                      verdict + PR
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ Dashboard  │  outcome mix · cost per outcome · CI verification · stalls
   └────────────┘
```

**Speaker notes (~45s) — the decision to lead with**

I deliberately did **not** use Devin for detection. Finding these is
deterministic and cheap, so it's a Python script with zero dependencies and zero
credit cost.

Devin is used only where the work requires judgment. That's why the credit cost
is what it is, and it's how you'd actually deploy this.

So when you ask *"couldn't a script do this?"* — yes, and it does. That's the
scanner. The agent does the part the script can't.

---

## Slide 4 — Live: from detection to a session

**Demo, not a slide.**

1. Run the scanner. Four findings, from the real repo, live.
2. It files them as issues with evidence — every line carrying a `source:`.
3. Apply the `agent:remediate` label.
4. A Devin session appears within seconds. The dashboard picks it up.

**Speaker notes (~30s)**

The label is the event. Nothing else is wired — no polling on GitHub's side, no
glue service. Devin's automation watches the repo and fires on the condition.

Cut here; come back to it later.

---

## Slide 5 — What Devin actually did

**Session replay, narrated (~60s).**

`simple-zstd`. Devin reads `webpack.proxy-config.js` and finds a hand-written
workaround: the v1 decompression stream is backed by a real `zstd -d` child
process that doesn't forward `.destroy()`, so the child is orphaned when the
stream is torn down on error. The code tracks the process and races to kill it.

v2 replaced that whole lifecycle with a process pool. So the migration isn't a
rename — **the question is whether that workaround is now obsolete, wrong, or
still needed.** Devin has to read the code, understand why the workaround exists,
and decide.

Then it runs the existing test — including the one named *"fails fast instead of
hanging when the backend connection drops mid-response."* That test exercises
exactly the teardown path the workaround was written for.

**That's the verification. Their test suite, not our claim.**

---

## Slide 6 — Results

> ## Four findings. Four verdicts. One of them was "don't."

| Finding | Verdict | Credits | Verified by |
|---|---|---|---|
| `simple-zstd` — migration never finished | *(live)* | | existing zstd tests |
| `react-checkbox-tree` — suppression stale since August | *(live)* | | unit tests + no-crash check |
| `userEvent` — 205 sites, 29 high-risk | *(live)* | | Jest |
| `xlsx` — advisory unresolvable | **declined** | | version evidence |

> **Total: N credits ≈ $X**

**Speaker notes (~45s) — the analytics callback**

Three things I'd point at.

**First, cost against documented prior cost.** Superset has already spent engineer
time on `simple-zstd` three times and still has the problem. Devin resolved it for
N credits. That's a measured number against a documented history — not an
estimate of hours saved.

**Second, the outcome mix is the metric, not the fix count.** One of those four is
a refusal, and in this system a refusal is a success. An hour saved dismissing a
false positive is worth the same as an hour saved landing a fix, and it's the
scarcer outcome — nothing else produces it.

**Third, the dashboard tracks something nobody instruments: sessions stuck waiting
on a human.** An agent in `waiting_for_user` is billing and blocked and nobody has
been told. That's how agent rollouts quietly fail.

---

## Slide 7 — The one that matters most

> ## The obvious automated fix introduces the vulnerability

```
   0.18.5   ← what npm gives you            ✗ both advisories apply
   0.19.3   ← advisory 1 fixed here
   0.20.2   ← advisory 2 fixed here
   0.20.3   ← what Superset installs        ✓ patched
```

**Speaker notes (~40s)**

A scanner flags `xlsx` as vulnerable. It will flag it forever — the advisory has
no fixed version, because the publisher left npm and the database has nothing to
record.

Superset installs from the vendor CDN at 0.20.3. They're patched. But the obvious
remediation — *"reinstall it properly from the registry"* — resolves to 0.18.5.
**Backwards, past both fixes, into genuinely affected code. And the scanner goes
green.**

Devin investigated and refused, with the version comparison as evidence.

Your own `security-dependency-vulns` template would not have. Its prompt scopes to
*"issues with available patches"* — and nothing in it checks whether the fix is a
downgrade.

That's the difference between automation that closes tickets and automation you
can leave running.

---

## Slide 8 — Why this needs an agent

> ## Detection is a script. Verdicts are not.

| | A script can | An agent must |
|---|---|---|
| Stale suppression | parse a comment, follow the URLs, check state | retry an upgrade that broke every dashboard and judge whether it still does |
| Unfinished migration | grep for the old API | decide whether a child-process workaround is now obsolete |
| Post-upgrade debris | count 205 call sites | decide per site — blanket-`await` is wrong in three known cases, and some assertions pass *because* of the race |

**Speaker notes (~35s)**

Every one of these needs someone to read code, follow a reference into another
project, and form a judgment that could be wrong.

That's the work. It's never been automatable, and it's why these sit for months
in a repository with excellent tooling and 74,000 stars.

---

## Slide 9 — Next steps

> ## Scale is repos, not findings

| When | What |
|---|---|
| **Weeks 1–2** | Same pipeline, your intake — Jira, Linear, PagerDuty, CI failures are all native Devin triggers |
| **Weeks 2–4** | Fan out across repos. One ignore file is a curiosity. Two hundred is a programme. |
| **Month 2** | Ingest your real scanner via `code-scans/ingestion` — verdicts on the backlog you already have |
| **Month 2** | Make the outcome mix an SLO: *"95% of findings reach a verdict within 48 hours"* |
| **Ongoing** | Policy gates as an adoption dial: dry-run → triage only → remediate with review → autonomous on dev tooling |

**Speaker notes (~40s)**

Two closing points.

**The scheduled re-validation is the product.** Blockers clear silently.
`react-checkbox-tree` became actionable in August; nobody noticed. Every org has
that, in every repo, and nobody has a job that finds it.

**On rollout — everything here is dev tooling and CI. Zero production blast
radius.** That's deliberate. You pilot an autonomous agent on the webpack config,
not on payment code. Once the outcome mix is trusted, you widen the intake. The
policy gate is the dial.

---

## Anticipated questions

**"Couldn't a script do this?"**
Half of it, and it does — that's the scanner, zero credits. The agent does the
remediation and the judgment. `simple-zstd` is the proof: a bot tried, a human
tried, it was reverted.

**"How do I know the fixes are correct?"**
You don't take the agent's word. Verification reads Superset's own CI on the PR's
head commit. And where a runtime failure is the risk, the acceptance criteria say
explicitly that passing unit tests is not sufficient — because last time it passed
CI and still broke every dashboard.

**"What stops it running up a bill?"**
Two caps. A hard per-session ACU limit enforced by Devin, and our own concurrency
limit — Devin imposes none, and unbounded fan-out across a large finding set is
how a budget disappears overnight.

**"What if it does something wrong?"**
Findings carry guardrails — explicit prohibitions rendered into the prompt. The
`xlsx` finding carries *"do not change the install source to the npm registry"*
and the reason. It's data attached to the finding, not a hope about model
behaviour.

**"This is all dev tooling. Where's the customer impact?"**
Deliberate. Zero production blast radius is where you start. The chain is real —
flaky tests mean developers stop trusting CI, failures get re-run instead of
investigated, bugs ship — but I'd rather show you a pilot you'd actually approve
than a demo on code that matters.

**"Your numbers are small."**
On one repo, yes. The unit isn't the finding, it's the repository. And the thing
being automated — producing a verdict — is the step your queue is actually
blocked on.

---

## Timing

| Segment | Target |
|---|---|
| 1–2: problem + evidence | 1:30 |
| 3: architecture | 0:45 |
| 4–5: demo + session replay | 1:30 |
| 6–7: results + the refusal | 0:50 |
| 8–9: why an agent + next steps | 0:50 |
| | **5:25 — needs one cut** |

Cut slide 8 if needed; its content lives in the slide-3 notes and the Q&A.
