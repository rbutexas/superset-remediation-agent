# Issues to file on `rbutexas/superset`

Every factual claim below maps to a passing check in `tools/verify_claims.py`
(42 checks, 0 failures). Anything not verified is labelled **Unverified** inline.

---

## Issue 1

**Title:** `deps(frontend): retry react-checkbox-tree 2.x — the upstream fix the April revert was waiting on shipped 2026-08-27`

**Labels:** `dependencies` `frontend` `dashboard`

---

### Background

`react-checkbox-tree` was upgraded 1.8.0 → 2.0.1 in #39261 (merged 2026-04-22) and
reverted five days later in #39660 (merged 2026-04-27). The upgrade broke every
dashboard in the Docker install path (#39600):

```
TypeError: Cannot set properties of undefined (setting 'runtime')
  at node_modules/react-checkbox-tree/lib/index.esm.js:12:53
  at ./src/dashboard/components/filterscope/FilterScopeTree.tsx:5:77
```

After the revert, a suppression was added to `.github/dependabot.yml` naming two
conditions for retrying:

```yaml
# TODO: remove below clause once https://github.com/pmmmwh/react-refresh-webpack-plugin/pull/940
# lands onto a future release and confirm the issue
# https://github.com/apache/superset/issues/39600 is fixed
- dependency-name: "react-checkbox-tree"
  update-types: ["version-update:semver-major"]
```

### Both conditions are now met

| Condition | Status |
|---|---|
| `react-refresh-webpack-plugin#940` merged | 2026-01-18 |
| …shipped in a release | `@pmmmwh/react-refresh-webpack-plugin@0.6.3`, published **2026-08-27** |
| …and we are on that release | `package.json` pins `^0.6.3` |
| superset#39600 resolved | closed 2026-04-27, `state_reason: completed` |

We remain on `react-checkbox-tree@^1.8.0`. `2.0.2` has been available since 2026-05-28.

### The technical question this issue has to answer

The crash is a webpack runtime-global injection failure inside an ESM bundle — the
module tries to set `.runtime` on an undefined global at import time. PR #940 is
titled *"fix: use `__webpack_global__` in loader if available"*, which addresses that
class of failure, and the revert authors named it as the gating fix.

**Unverified:** whether #940 actually resolves this specific crash. That is the
maintainers' hypothesis, not a confirmed fact, and confirming or refuting it is the
substance of this work. A clean bump that passes unit tests but reproduces the
dashboard crash in a Docker build is a failed fix.

### Scope

Five files, all under `superset-frontend/src/dashboard/components/filterscope/`:
`FilterScopeSelector.tsx`, `FilterScopeTree.tsx`, `FilterFieldTree.tsx`,
`treeIcons.tsx`, `FilterScope.test.tsx` — plus `webpack.config.js`.

v2.0.0 breaking changes that apply here:

- `iconsClass` now defaults to `'fa6'` rather than Font Awesome 4 — affects `treeIcons.tsx`
- `lang`: the `toggle` key is replaced by `collapseNode` / `expandNode`
- CSS classes renamed: `rct-options`→`rct-actions`, `rct-option`→`rct-action`, `rct-title`→`rct-label`
- Clickable label role changes from `link` to `button` — affects any `getByRole` queries
- Less.js styles dropped
- `id` no longer auto-generates a UUID when empty

### Acceptance criteria

1. `react-checkbox-tree` on 2.x; the six breaking changes above addressed.
2. `FilterScope.test.tsx` passes.
3. **The original crash is shown not to reproduce** — a production-mode bundle loads
   the dashboard filter-scope UI without the `index.esm.js` import-time TypeError.
   Unit tests alone do not close this issue; #39261 passed CI too.
4. The suppression block is removed from `.github/dependabot.yml`.
5. If the crash still reproduces, **do not force the upgrade** — report what fails and
   update the suppression comment with the current blocker.

---

## Issue 2

**Title:** `deps(frontend): migrate webpack dev proxy to the simple-zstd v2 async API — pinned to a 2022 release since the #39139 revert`

**Labels:** `dependencies` `frontend` `build`

---

### Background

`simple-zstd` is pinned at `^1.4.2`, published **2022-10-19**. `2.1.0` is current.

The upgrade was attempted and abandoned:

| PR | Outcome |
|---|---|
| #38662 | bot bump 1.4.2 → 2.1.0 |
| #39138 | attempt to fix the resulting v2 API breakage |
| #39139 | **revert** to 1.4.2 |

The suppression comment in `.github/dependabot.yml` states the exit condition:
*"Remove this once the proxy code is updated to await the async decompress() API."*

That update was never made. `superset-frontend/webpack.proxy-config.js:22` still reads:

```js
import { ZSTDDecompress } from 'simple-zstd';
```

### What v2 changed

From the v2 README's own migration table:

| v1 | v2 |
|---|---|
| `ZSTDCompress(level)` | `compress(level, opts?)` |
| `ZSTDDecompress()` | `decompress(opts?)` |
| `ZSTDDecompressMaybe()` | `decompress(opts?)` *(auto-detect built in)* |

Signature: `decompress(opts?: ZSTDOpts): Promise<Duplex>` — the stream must now be
awaited before it can be piped.

### Why this is more than a rename

`webpack.proxy-config.js:122-152` carries a hand-written workaround for a v1 defect.
The v1 decompress stream is backed by a real `zstd -d` child process that does not
forward `.destroy()`, so when `pipeline` tears the stream down on an upstream error
the child is orphaned with stdin still open. The code works around this by listening
for a `started` event, capturing the child process, and tracking `killZstdChild` to
cover the race where teardown happens before that event fires.

v2 introduces a pre-spawned process pool (`decompressQueueSize`), so the lifecycle
this workaround compensates for has changed. The migration has to determine whether
the workaround is still required, must be rewritten against the pool, or should be
deleted. Porting it verbatim is likely wrong in either direction.

### This is testable

`superset-frontend/tools/webpack.proxy-config.test.js` already contains:

```
describe('webpack.proxy-config zstd/gzip HTML decompression')
  ✓ decompresses a complete zstd-encoded HTML response and injects the [DEV] title
  ✓ fails fast instead of hanging when the backend connection drops mid-response (zstd)
```

The second test exercises precisely the teardown path the workaround exists for.
Note the test itself imports `ZSTDCompress` from `simple-zstd`, so it needs migrating
as part of this change.

### Acceptance criteria

1. `simple-zstd` on 2.x; proxy migrated to the awaited `decompress()` API.
2. The child-process workaround is either removed with a justification, or rewritten
   against the v2 lifecycle — with the reasoning recorded in the PR.
3. Both existing zstd tests pass, including the mid-response abort case.
4. The suppression block is removed from `.github/dependabot.yml`.

---

## Issue 3

**Title:** `test(frontend): 205 un-awaited userEvent calls across 52 files — and eslint-plugin-testing-library is installed but not enabled`

**Labels:** `test-infrastructure` `flaky-tests` `frontend`

---

### Problem

`@testing-library/user-event` is on `^14.6.7`. In v14 every action API returns a
promise; in v12 they were synchronous. Calls that were correct under v12 are races
under v14.

A scan of 1,318 frontend test files finds:

| | Count |
|---|---|
| Files containing un-awaited `userEvent` action calls | **52** |
| Un-awaited call sites | **205** |
| Sites where the next statement is a **synchronous** assertion or query | **29** |
| Files containing at least one of those | **15** |

The 29 high-risk sites are the actively dangerous form — the assertion can run against
pre-event DOM state, so the test passes or fails depending on timer scheduling. Example:

```tsx
// src/components/RowCountLabel/RowCountLabel.test.tsx:43
userEvent.hover(screen.getByText(expectedText));
expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
```

The remaining ~176 sites are followed by an `await` (typically `findBy*`), which
incidentally flushes the event loop. Those are latent rather than actively failing,
but they are the same defect and will surface under different scheduling.

### The guardrail already exists and is switched off

`superset-frontend/package.json` declares `eslint-plugin-testing-library@^7.16.2` as a
devDependency. That plugin ships `await-async-events`, which detects exactly this.

It is not wired up:

- not referenced in `eslint.config.minimal.js`
- `oxlint.json` plugin list is `["import","react","jest","jsx-a11y","typescript","unicorn","oxc"]` — no `testing-library`
- `await-async-events` appears nowhere in the frontend configuration

So the rule that would have caught all 205 at review time is already a dependency and
has never been enabled.

### Why this needs judgment, not a codemod

Blanket-inserting `await` is wrong in at least three situations, all present here:

1. Calls inside non-`async` callbacks — the enclosing function must change first.
2. Sequences where an intentional un-awaited call sets up a concurrent state being asserted.
3. Assertions written to pass *because* of the race, which will need rewriting rather
   than awaiting — fixing the call will expose a genuinely wrong expectation.

Enabling the rule repo-wide as `error` in the same change would fail CI on all 205
sites at once. A staged rollout is required.

### Acceptance criteria

1. All **29 high-risk sites across 15 files** corrected.
2. `await-async-events` enabled, scoped so CI passes — either `warn` repo-wide, or
   `error` on the corrected paths with the remainder tracked.
3. A follow-up issue filed for the remaining latent sites, with the file list.
4. No test's meaning changes silently: where awaiting reveals a wrong expectation, the
   corrected expectation is called out in the PR.

---

## Issue 4

**Title:** `deps(frontend): Babel 8 is blocked by babel-plugin-jsx-remove-data-test-id (last published 2021-02-06), not by "the ecosystem"`

**Labels:** `dependencies` `frontend` `build` `blocked-upstream`

---

### Problem

`.github/dependabot.yml` suppresses `@babel/*` major updates:

> *"Babel 8 (7.x -> 8.x) is blocked on the surrounding ecosystem: @emotion/babel-plugin
> (NodePath#hoist) and babel-plugin-jsx-remove-data-test-id (t.jSXOpeningElement)…
> Ignore the coordinated major bump until the ecosystem catches up."*

"Until the ecosystem catches up" has no owner and no end date. Re-checked 2026-09-18:

| Package | Latest | Last published | Constraint |
|---|---|---|---|
| `babel-plugin-jsx-remove-data-test-id` | 3.0.0 | **2021-02-06** | `peerDependencies: {"@babel/core": "^7.0.0"}` |
| `@emotion/babel-plugin` | 11.13.5 | 2024-11-20 | still on `@babel/helper-module-imports ^7.16.7` |

We are on `@babel/core@^7.29.7`. Babel 8.0.6 is current.

Both plugins are genuinely in use — `superset-frontend/babel.config.js` references
`@emotion/babel-plugin` at line 56 and `babel-plugin-jsx-remove-data-test-id` at line 107.

### The actual blocker

`babel-plugin-jsx-remove-data-test-id` has a hard `^7.0.0` peer cap and has not been
published in five years. There is no plausible path where it gains Babel 8 support.
The suppression as written implies a wait; in practice it is permanent until we act.

The plugin's job is to strip `data-test-id` attributes from production bundles. That is
a small, self-contained transform.

### Acceptance criteria

No dependency change. This issue closes with a decision recorded in the suppression
comment, choosing one of:

1. **Drop it** — quantify the bundle-size cost of shipping `data-test-id` attributes and
   decide whether the transform still earns its place.
2. **Replace it** — identify a maintained equivalent with Babel 8 support.
3. **Vendor it** — the transform is small enough to maintain in `eslint-rules/`-style
   local tooling, alongside the three plugins already maintained there.

Also: confirm whether `@emotion/babel-plugin` independently blocks Babel 8, or whether
its `^7` ranges resolve acceptably. If the emotion plugin is not in fact a blocker, the
comment should stop naming it.

---

## Issue 5

**Title:** `security-triage: xlsx advisories are unresolvable by upgrade and will recur on every scan — record a determination`

**Labels:** `security` `triage` `false-positive`

---

### Summary

Dependency scanning flags `xlsx` against two advisories. Neither can be cleared, and
the obvious remediation is a downgrade. Recording the determination so this is not
re-investigated on every scan.

### Determination: not affected

`superset-frontend/package.json` installs from the vendor CDN:

```json
"xlsx": "https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz"
```

| Advisory | CVE | Machine-readable range | Real fix, per the advisory | 0.20.3 |
|---|---|---|---|---|
| GHSA-4r6h-8v6p-xvw6 | CVE-2023-30533 | `introduced: 0`, **no `fixed` event** | `< 0.19.3` | above |
| GHSA-5pgg-2g8v-p4x9 | CVE-2024-22363 | `introduced: 0`, **no `fixed` event** | `< 0.20.2` | above |

0.20.3 is above both fix versions.

### Why it recurs permanently

SheetJS stopped publishing to npm. With no npm release carrying the fix, the advisory
database has no version to record in the `fixed` field, so the machine-readable range
stays open at "all versions." Scanners read that field. The real fix version appears
only in the descriptive `last_known_affected_version_range`, which tools do not parse.

No action by this project can clear the finding.

### Do not remediate by changing the install source

The npm registry's latest `xlsx` is **0.18.5** — below both fixes. Any remediation that
"reinstalls from the registry" is a downgrade into genuinely affected code, and it would
turn the scanner green while doing so.

### Acceptance criteria

No dependency change. A recorded suppression carrying this rationale, plus a guard
preventing automated remediation of this package.

---

## Summary

| # | Complexity | Expected outcome | Verified claims |
|---|---|---|---|
| 1 | High — runtime/ESM failure, must reproduce a non-crash | Fix, or a re-blocked report | 10 |
| 2 | High — async migration + child-process lifecycle rework | Fix | 9 |
| 3 | High — 205 sites, staged lint rollout, semantics may change | Partial fix + rule enabled | 7 |
| 4 | Analysis | Decision recorded, no code change | 6 |
| 5 | Analysis | Determination recorded, no code change | 10 |

Issues 1–3 are separately assignable: different directories, different skills, no
overlapping files. Issues 4 and 5 produce written determinations rather than PRs.
