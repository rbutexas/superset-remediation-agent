"""The two playbooks, and the prompts that invoke them.

A playbook is a procedure stored once in Devin and reused by every session, which
is what keeps results comparable across runs. If every prompt were bespoke, the
outcome mix would be noise.

Two playbooks because triage and remediation are different jobs with different
success conditions. Triage succeeds by reaching a defensible decision, including
"do nothing". Remediation succeeds by making a change that the project's own
tests accept — or by stopping when a guardrail says it should.
"""

from __future__ import annotations

from .models import Finding

TRIAGE_TITLE = "Triage an engineering-debt finding (Superset)"

TRIAGE_BODY = """\
You are triaging a finding on the Apache Superset codebase. Your job is to decide
what it deserves. You are not fixing anything in this session.

**"Do nothing" is a legitimate and valued outcome.** An hour saved dismissing a
false positive is worth as much as an hour saved landing a fix. Do not reach for
`remediate` because it feels more useful.

## Procedure

1. Read `AGENTS.md` and `SECURITY.md` in the repository root first. They define
   how this project expects automated tooling to behave, and they are binding here.

2. Independently verify each claim in the issue. Do not take the scanner's word
   for anything. Check the files, the versions, the referenced PRs and issues.
   If a claim is wrong, record it in `contradicted_evidence` — that is a useful
   result, not a criticism you should soften.

3. Answer the issue's "Open questions for triage" section specifically.

4. Read the "Do not" section. Those prohibitions are binding. If the only way to
   resolve the finding violates one, the answer is `decline_not_actionable` or
   `escalate_to_human`, and you should say which prohibition applies and why.

5. Assess blast radius by looking at what actually imports or depends on the code
   in question — not by guessing from the file count in the issue.

6. Decide:
   - `remediate` — real, actionable, and the change is worth making now
   - `document_only` — no code change is correct, **but the determination itself
     belongs in the repository**: a scanner suppression carrying this rationale,
     a comment beside an existing ignore rule naming what is being waited on, or
     a dated re-check condition. Choose this over `decline_not_actionable`
     whenever your conclusion would otherwise have to be rediscovered by the next
     person to look. Use `recommended_prompt_additions` to say exactly which file
     should change and what it should say.
   - `decline_not_actionable` — no change is the correct engineering answer *and*
     there is nothing worth recording in the tree either
   - `blocked_upstream` — cannot proceed until something external changes. Name
     the specific package, PR, issue or release. "The ecosystem" is not a blocker.
   - `escalate_to_human` — needs a judgement outside your remit. Say what a human
     must decide, and what you would recommend.

### Choosing between `document_only` and `decline_not_actionable`

Ask: *if this repository is scanned again next quarter by someone who has never
seen my reasoning, does the finding come back?*

If yes, and a suppression or a comment would stop that, the answer is
`document_only`. A determination that lives only in a closed issue is a
determination that will be made again from scratch.

`decline_not_actionable` is right when the finding is genuinely transient, or
when there is no file in the repository where the conclusion would belong.

## On security findings

`AGENTS.md` requires that any automated security finding name the SECURITY.md
role-and-capability row it believes is violated, and the principal the attacker
is assumed to hold. It states that findings which cannot identify both should be
filed as questions, not vulnerabilities.

If you cannot name both, the correct decision is `decline_not_actionable`, with
reasoning explaining that the finding does not meet the project's own bar — or
`document_only`, if the scanner will keep raising it and a scoped suppression
would stop that.

## Calibration

- Prefer `low` confidence with honest reasoning over a confident guess.
- A suppression that exists usually exists for a reason. Evidence that its stated
  condition has cleared means the upgrade is worth *retrying*, not that it is safe.
- Where a previous attempt was reverted, find out why before deciding.
"""


REMEDIATION_TITLE = "Remediate a finding in Apache Superset"

REMEDIATION_BODY = """\
You are making a change to the Apache Superset codebase, triaged as worth doing.

## Before you change anything

1. Read `AGENTS.md`. It is binding, and it describes active migrations you must
   not work against.
2. Read the issue's "Do not" section. Those prohibitions override the goal. If
   completing the task requires violating one, stop and report
   `abandoned_on_guardrail` naming the prohibition. That is a success, not a
   failure, and it is preferred over a change that satisfies the letter of the
   issue while doing the wrong thing.
3. Read the acceptance criteria. They are the definition of done.

## Repository-specific rules

- `requirements/*.txt` are generated by `uv pip compile`. **Never hand-edit them.**
  Change `requirements/*.in` or `pyproject.toml` and regenerate.
- Python unit tests run without a database:
  `pytest ./tests/common ./tests/unit_tests`
  Integration and e2e tests need Docker services; do not attempt them.
- Frontend tests: `npm run test -- <path>` from `superset-frontend`.
- Run `pre-commit run` on the files you changed before pushing. It matches CI.
- New files need the Apache licence header.
- PR titles follow Conventional Commits. Use `.github/PULL_REQUEST_TEMPLATE.md`.

## Verification is the deliverable

A change you cannot demonstrate is correct is not finished.

- Run the actual commands and record them verbatim in `verification.commands_run`.
- If a test fails, determine whether your change caused it or whether it already
  fails on the base branch. Say which.
- **Where the acceptance criteria ask for something you cannot verify in this
  environment, say so in `verification.unverifiable`.** An admitted gap is worth
  far more than an unverified claim. Passing unit tests is not evidence that a
  runtime failure is fixed.
- If you made a change but cannot show it correct, report `failed_verification`
  rather than opening a PR that looks finished.

## Scope

Change only what the issue asks for. Do not fix unrelated problems you notice,
do not reformat untouched files, and do not upgrade adjacent dependencies. If you
find something else worth doing, put it in `follow_up_required`.
"""


DOCUMENTATION_TITLE = "Record a triage determination in the repository (Superset)"

DOCUMENTATION_BODY = """\
Triage concluded that this finding has no correct code fix, but that the
determination itself belongs in the repository. Your job is to write it down
where the next scan and the next engineer will both find it.

## What you may change

Exactly one kind of thing: **configuration and comments that record a decision.**

- A scanner suppression or allowlist entry, with the rationale beside it.
- A comment next to an existing ignore rule, naming what is being waited on.
- A dated re-check condition, so the entry cannot outlive its reason silently.
- A short note in a security or dependency policy document, if one exists.

## What you must not change

- Any dependency version, version range, or install source.
- Any lock file.
- Any source file, test, or build configuration that affects what is built or run.

If the determination cannot be recorded without one of these, **stop** and report
`abandoned_on_guardrail`. Do not improvise a code change to make the finding go
away. That is the exact failure this stage exists to prevent.

## The suppression must be bounded

An unbounded suppression is worse than no suppression: it hides the real problem
if the situation later changes. Every entry you write must state the condition
under which it stops applying, in a form a tool can evaluate where the format
allows one — a version floor, a date, or a named upstream release.

Concretely: if a package is safe at or above a version, scope the suppression to
that floor so it **stops applying if anyone downgrades.** Do not write an entry
that silences the package unconditionally.

## Write the reasoning where it will be read

The rationale goes in the file you change, next to the entry — not only in the
pull-request description. A pull request is read once; the file is read every
time someone asks why the entry is there.

Keep it to a few lines: what was concluded, the versions or facts it rests on,
and what would invalidate it.

## Verification

There are no tests for a comment. Verify what can actually be verified:

- The file still parses. Run the project's own linter or config check on it.
- Run `pre-commit run` on the files you changed.
- If the suppression is in a format a tool consumes, show the tool accepting it.

State in `verification.unverifiable` that the suppression's *effect* on the
scanner was not observed, unless you actually ran the scanner and saw it.

## Outcome

Report `mitigated` — a compensating change was made and the underlying advisory
is unchanged. `fixed` would be wrong: nothing was fixed. Say so plainly.

Open a pull request. Do not merge it. A human approves every silence.
"""


# ------------------------------------------------------------------ prompts

def triage_prompt(finding: Finding, issue_number: int | None, repo: str) -> str:
    guardrails = (
        "\n".join(f"- {g}" for g in finding.guardrails)
        if finding.guardrails else "- (none stated)"
    )
    questions = (
        "\n".join(f"- {q}" for q in finding.open_questions)
        if finding.open_questions else "- (none stated)"
    )
    evidence = "\n".join(
        f"- {e.claim}: {e.value}  [source: {e.source}]" for e in finding.evidence
    )
    issue_ref = f"\nTracking issue: {repo}#{issue_number}" if issue_number else ""

    return f"""\
Triage this finding on @{repo}. Decide what it deserves. Do not make any code
change in this session.
{issue_ref}

## {finding.title}

{finding.summary}

### Evidence supplied by the scanner — verify each independently
{evidence}

### Open questions you must answer
{questions}

### Do not
{guardrails}

Return your decision via structured output. `decline_not_actionable` is a valid
and valued result.
"""


def remediation_prompt(finding: Finding, issue_number: int | None, repo: str,
                       triage_reasoning: str = "",
                       extra: str = "") -> str:
    guardrails = (
        "\n".join(f"- {g}" for g in finding.guardrails)
        if finding.guardrails else "- (none stated)"
    )
    acceptance = (
        "\n".join(f"{i}. {a}" for i, a in enumerate(finding.acceptance, 1))
        if finding.acceptance else "(see the issue)"
    )
    paths = "\n".join(f"- {p}" for p in finding.paths) if finding.paths else "- (not scoped)"

    triage_block = (
        f"\n### Why triage decided to proceed\n\n{triage_reasoning}\n"
        if triage_reasoning else ""
    )
    extra_block = f"\n### Additional context from triage\n\n{extra}\n" if extra else ""
    close = (
        f"\n\nOpen a pull request against @{repo} that closes #{issue_number}."
        if issue_number else ""
    )

    return f"""\
Remediate this finding on @{repo}.

## {finding.title}

{finding.summary}
{triage_block}{extra_block}
### Files in scope
{paths}

### Do not
{guardrails}

### Acceptance criteria
{acceptance}
{close}

Report the outcome via structured output, including the exact verification
commands you ran and anything you could not verify.
"""


def documentation_prompt(finding: Finding, issue_number: int | None, repo: str,
                         triage_reasoning: str = "",
                         extra: str = "") -> str:
    """Prompt for the restricted stage: record the verdict, change nothing else.

    Deliberately repeats the prohibition that the playbook already states. The
    playbook is standing procedure and the prompt is the specific instance, and
    the one instruction that must not be missed is the one that is only in one
    of them.
    """
    guardrails = (
        "\n".join(f"- {g}" for g in finding.guardrails)
        if finding.guardrails else "- (none stated)"
    )
    triage_block = (
        f"\n### The determination to record\n\n{triage_reasoning}\n"
        if triage_reasoning else ""
    )
    extra_block = (
        f"\n### What triage said should be written, and where\n\n{extra}\n"
        if extra else ""
    )
    close = (
        f"\n\nOpen a pull request against @{repo} that closes #{issue_number}. "
        f"Do not merge it."
        if issue_number else ""
    )

    return f"""\
Record a triage determination in @{repo}. **Make no functional change.**

Triage investigated this finding and concluded that no code fix is correct, but
that the conclusion belongs in the repository rather than only in a closed issue.

## {finding.title}

{finding.summary}
{triage_block}{extra_block}
### You may change

Scanner suppressions, allowlists, ignore-rule comments, and dated re-check
conditions — with the rationale written beside the entry.

### You must not change

Dependency versions, version ranges, install sources, lock files, or any source,
test or build file. If the determination cannot be recorded without one of these,
report `abandoned_on_guardrail` and stop.

### Also do not
{guardrails}

### The entry must be bounded

Scope the suppression so it stops applying if the underlying facts change — a
version floor, a date, or a named upstream release. An unconditional entry would
hide the real problem if the position ever regresses, which is the outcome this
whole finding is about.
{close}

Report `mitigated`, not `fixed` — nothing was fixed. Report the outcome via
structured output, including what you could not verify.
"""


REVALIDATION_TITLE = "Re-validate Dependabot suppressions (Superset)"

REVALIDATION_BODY = """\
Every ignore entry in `.github/dependabot.yml` has a human-written comment beside
it saying why it exists and, usually, what would make it removable. Nothing in
any toolchain ever re-reads those comments, so a suppression outlives its cause
silently. Your job is to read them and say which ones are no longer true.

**This is a reporting pass. Open no pull requests and change no files.**

## Procedure

For every `dependency-name` entry:

1. Read the comment above it. That is the stated condition, in the author's own
   words — quote it rather than paraphrasing.
2. Follow every link it contains, **including into other repositories.** A merged
   pull request is not the same as a released one: check whether the fix actually
   shipped in a version, and whether this project is on that version.
3. Decide whether the condition has genuinely cleared.

## The trap to avoid

**A closed issue is not proof of a fix.** An issue describing a failure is often
closed by *reverting* the change that caused it — which means the condition reads
as satisfied while the underlying problem is untouched. Find out what closed it
before you count it. If a closure was a revert, say so explicitly.

Equally: a condition that names something about *this* repository's own code
cannot be resolved from a link. Report those as unreadable rather than guessing.

## Calibration

A suppression that exists usually exists for a reason. Evidence that its stated
condition has cleared means the upgrade is worth **retrying**, not that it is
safe. Say that in your evidence rather than implying the work is done.

Report the entries still blocked as well as the cleared ones. A reader needs to
know the scan covered them rather than skipped them.
"""


def revalidation_prompt(repo: str) -> str:
    return f"""\
Re-validate the Dependabot suppressions in @{repo}.

Read `.github/dependabot.yml`. For every ignore entry, read the comment beside
it, follow any links it contains — including into other repositories — and
determine whether the stated condition still holds.

Do not open pull requests and do not change any files. This is a reporting pass.

A closed issue is not proof of a fix: an issue is often closed by reverting the
change that caused it. Find out what closed it, and say so if it was a revert.

Report every suppression whose condition has cleared, every one still blocked,
and every one you could not evaluate, via structured output.
"""
