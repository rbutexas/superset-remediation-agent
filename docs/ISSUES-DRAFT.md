# Issue drafts — read these as a reviewer would

Nothing here is filed yet. Every factual claim was verified against the GitHub API,
the npm registry, and the current contents of `apache/superset@master` on 2026-09-18.

**Register check.** Superset's own non-bug issues read like:

> `nightly pre-commit (next) mypy fails: UUID | None passed unnarrowed in 4 versions`
> `Flaky CI: un-awaited userEvent calls left across 40 test files`
> `i18n: messages.pot is 428 strings behind source, so those strings cannot be translated`

Specific, evidence-first, a number in the title, actionable. These six are written to match.

**The test each one must pass:** a senior engineer reads it cold, knowing nothing about
our system, and thinks *"yes, that's a real problem, and I know what to do about it."*

---

## #1 — Umbrella

**Title:** `deps: Dependabot ignore rules are never re-validated — 2 of 6 suppressions are stale`

**Labels:** `dependencies` `audit`

> `.github/dependabot.yml` suppresses six dependency updates. Each carries a comment
> stating the condition under which it should be removed. Nothing re-checks those
> conditions, so a suppression outlives its cause silently.
>
> Audited all six on 2026-09-18:
>
> | Suppression | Stated condition | Status |
> |---|---|---|
> | `react-checkbox-tree` | upstream PR lands + #39600 fixed | **both cleared — Jan and Apr** |
> | `simple-zstd` | proxy code migrated to async `decompress()` | **never done** |
> | `@babel/*` | ecosystem support for Babel 8 | still blocked (#4) |
> | `currencyformatter.js` | `just-handlebars-helpers` peer cap | still blocked, target is from 2018 |
> | `@deck.gl/*` / `@luma.gl/*` | coordinated manual upgrade | not attempted (#6) |
> | `react` / `react-dom` | app supports React 19 | still blocked — but see #2 |
>
> Two suppressions have outlived their stated cause by five and eight months.
>
> Children: #2, #3, #4, #5, #6.
>
> **Ask:** these rules need a periodic re-validation pass, not a one-time cleanup.
> Conditions clear upstream without anyone here noticing.

---

## #2 — Fix expected

**Title:** `deps(frontend): react-checkbox-tree major updates still suppressed after both stated preconditions cleared`

**Labels:** `dependencies` `npm` `frontend`

> `.github/dependabot.yml` suppresses major updates for `react-checkbox-tree`:
>
> ```yaml
> # TODO: remove below clause once https://github.com/pmmmwh/react-refresh-webpack-plugin/pull/940
> # lands onto a future release and confirm the issue
> # https://github.com/apache/superset/issues/39600 is fixed
> - dependency-name: "react-checkbox-tree"
>   update-types: ["version-update:semver-major"]
> ```
>
> Both conditions are satisfied:
>
> | Condition | Status |
> |---|---|
> | `react-refresh-webpack-plugin#940` merged | **2026-01-18** |
> | …released, and in use here | we run `@pmmmwh/react-refresh-webpack-plugin@^0.6.3`, published **2026-08-27** |
> | superset#39600 fixed | **closed 2026-04-27** |
>
> We remain on `react-checkbox-tree@^1.8.0`. `2.0.2` has been available since 2026-05-28.
>
> **This also blocks something else.** `react-checkbox-tree@2.0.0` release notes list
> *"Add support for React 19"* under Added. v1 does not support React 19 — so this
> suppressed upgrade is itself a prerequisite for the React 19 migration, which is
> separately suppressed in the same file.
>
> **Scope** — 5 source files, all under `superset-frontend/src/dashboard/components/filterscope/`:
> `FilterScopeSelector.tsx`, `FilterScopeTree.tsx`, `FilterFieldTree.tsx`, `treeIcons.tsx`,
> and `FilterScope.test.tsx`. Plus `webpack.config.js`.
>
> **Breaking changes to handle** (from the v2.0.0 notes):
> - `iconsClass` now defaults to `'fa6'` instead of Font Awesome 4 — relevant to `treeIcons.tsx`
> - `lang`: `toggle` key replaced by `collapseNode` / `expandNode`
> - CSS classes renamed: `rct-options`→`rct-actions`, `rct-option`→`rct-action`, `rct-title`→`rct-label`
> - Clickable label `role="link"` → `role="button"`
> - Less.js styles dropped
> - `id` no longer auto-generates a UUID when empty
>
> **Acceptance:** upgraded to 2.x, the six breaking changes addressed, `FilterScope.test.tsx`
> passes, filter-scope UI verified, suppression removed from `dependabot.yml`.

---

## #3 — Fix expected

**Title:** `deps(frontend): simple-zstd pinned to v1 — webpack dev proxy still uses the removed ZSTDDecompress API`

**Labels:** `dependencies` `npm` `build`

> `simple-zstd` is pinned at `^1.4.2`; `2.1.0` is current. The suppression comment in
> `.github/dependabot.yml` states the exit condition:
>
> > *"v2.0.0 renamed ZSTDDecompress to decompress and made it async, breaking the webpack
> > dev proxy (see #38662, #39138, #39139). Dependabot reopened the same bump in #39369
> > after the first revert, so pin it here… Remove this once the proxy code is updated to
> > await the async decompress() API."*
>
> The proxy code was never updated. `superset-frontend/webpack.proxy-config.js:22` still reads:
>
> ```js
> import { ZSTDDecompress } from 'simple-zstd';
> ```
>
> **Prior attempts:** #38662 (bot bump) → #39138 (fix attempt) → #39139 (**reverted**) →
> #39369 (bot reopened it) → suppressed.
>
> **Why this isn't a rename.** `webpack.proxy-config.js:122-152` carries a hand-written
> workaround for a v1 defect: `ZSTDDecompress()` returns a stream backed by a real `zstd -d`
> child process that doesn't forward `.destroy()`, so the child leaks when `pipeline`
> destroys the stream on an upstream error. The code tracks `zstdChild` / `killZstdChild`
> and listens for a `started` event to handle the race. Migrating to v2 means reworking
> that and determining whether it's still needed.
>
> **This is testable.** `superset-frontend/tools/webpack.proxy-config.test.js` already has:
> ```
> describe('webpack.proxy-config zstd/gzip HTML decompression')
>   ✓ decompresses a complete zstd-encoded HTML response and injects the [DEV] title
>   ✓ fails fast instead of hanging when the backend connection drops mid-response (zstd)
> ```
> The second test exercises exactly the leak the workaround exists for. Note the test
> itself imports `ZSTDCompress` from `simple-zstd`, so it needs migrating too.
>
> **Acceptance:** upgraded to 2.x, proxy migrated to the async API, obsolete workaround
> removed if the v2 stream destroys cleanly, both existing tests pass, suppression removed.

---

## #4 — Decline expected, with evidence

**Title:** `deps(frontend): Babel 8 is blocked by babel-plugin-jsx-remove-data-test-id, unmaintained since 2021`

**Labels:** `dependencies` `npm` `build` `blocked-upstream`

> `.github/dependabot.yml` suppresses `@babel/*` major updates with:
>
> > *"Babel 8 (7.x -> 8.x) is blocked on the surrounding ecosystem: @emotion/babel-plugin
> > (NodePath#hoist) and babel-plugin-jsx-remove-data-test-id (t.jSXOpeningElement)…
> > Remove when Babel 8 support is viable."*
>
> Re-checked on 2026-09-18. **Still blocked**, and the reason is more specific than
> "the ecosystem":
>
> | Package | Latest | Published | Constraint |
> |---|---|---|---|
> | `babel-plugin-jsx-remove-data-test-id` | 3.0.0 | **2021-02-06** | `peerDependencies: {"@babel/core": "^7.0.0"}` |
> | `@emotion/babel-plugin` | 11.13.5 | 2024-11-20 | still on `@babel/helper-module-imports ^7.16.7` |
>
> We're on `@babel/core@^7.29.7`; Babel 8.0.5 is current.
>
> The hard blocker is a package **last published five years ago** that caps `@babel/core`
> at `^7`. It is not going to be updated. "Blocked on the ecosystem" is indefinite unless
> that plugin is replaced or forked.
>
> **Ask:** convert the open-ended suppression into a tracked decision — either drop the
> plugin (it strips `data-test-id` attributes in production builds; evaluate whether that's
> still needed), replace it, or vendor a Babel 8-compatible fork. Until one of those
> happens, the suppression is correct and should say so.
>
> **Acceptance:** no dependency change. A documented determination with the specific
> blocking constraint named, and the suppression comment updated to reflect the real cause.

---

## #5 — Decline expected, with evidence

**Title:** `security(frontend): xlsx advisories cannot be resolved by upgrade, and the npm fallback would downgrade past the fix`

**Labels:** `security` `npm` `frontend`

> Dependency scanners flag `xlsx@0.20.3` against `GHSA-4r6h-8v6p-xvw6` (prototype pollution)
> and `GHSA-5pgg-2g8v-p4x9` (ReDoS). This will recur on every scan indefinitely, and the
> obvious remediation is actively harmful.
>
> **Why it recurs.** Both advisories declare `introduced: 0` with **no `fixed` event** —
> an unbounded affected range. SheetJS stopped publishing to npm, so the advisory database
> has no npm version it can mark as fixed. No upgrade can clear it.
>
> **Why the obvious fix is harmful.** We install from the vendor CDN:
> ```json
> "xlsx": "https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz"
> ```
> The npm registry's latest published `xlsx` is **0.18.5** (verified against the registry
> on 2026-09-18). The upstream fixes landed in 0.19.3 and 0.20.2. So "install it from npm
> so the scanner goes quiet" is a **two-minor-version downgrade past both fixes**, into a
> genuinely vulnerable build.
>
> We are on 0.20.3, above both fix versions. The finding appears to be a false positive
> caused by ecosystem metadata, not by our code.
>
> **Acceptance:** no dependency change. A documented determination comparing our pinned
> CDN version against the upstream fix versions, and a suppression entry so this stops
> consuming triage attention on every scan.

---

## #6 — Uncertain: fix or escalate

**Title:** `deps(frontend): @deck.gl / @luma.gl 9.2.5 → 9.4.x requires a coordinated bump across both manifests`

**Labels:** `dependencies` `npm` `frontend` `charts`

> Both families are suppressed together:
>
> > *"deck.gl and luma.gl share strict peer constraints across the root and plugin
> > workspaces, and root overrides pin their transitive versions. Upgrade both families
> > together in a manually validated change."*
>
> Current: `~9.2.5` for both. Latest: `@deck.gl/core` 9.4.0, `@luma.gl/core` 9.4.1.
> Minor versions within v9 — not a major migration.
>
> **Scope** — exactly two manifests declare them: `superset-frontend/package.json` and
> `superset-frontend/plugins/preset-chart-deckgl/package.json`. Root `overrides` pin the
> transitives, so all three locations must move together or peer resolution breaks.
> 19 files reference deck.gl APIs, concentrated in `plugins/preset-chart-deckgl/src/`.
>
> **Acceptance:** both families on 9.4.x across both manifests and the root overrides,
> peer warnings clean, deck.gl chart plugin tests pass. If peer constraints can't be
> satisfied without a major bump elsewhere, report the specific conflict instead of forcing it.

---

## Reviewer's-eye summary

| # | Reads as | Stands alone without our system? | Files |
|---|---|---|---|
| 1 | Audit finding | Yes — it's a process gap | — |
| 2 | Stale suppression, concrete upgrade | Yes | 6 |
| 3 | Known regression never finished | Yes — cites their own revert chain | 2 |
| 4 | Upstream blocker analysis | Yes | 0 |
| 5 | Security false-positive triage | Yes | 0 |
| 6 | Coordinated upgrade | Yes | 2 manifests |

Different directories, different skills, five separate PRs, no conflicts. Two produce no
code change at all — which is what makes them interesting, and what no scanner reports.
