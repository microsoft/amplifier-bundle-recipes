This repo had **no `.github/workflows` directory at all**. Its 1,798 tests, its
20-fixture conformance kit, and the byte-for-byte tool-description pin that
landed in `cd859d1` ran only when a human remembered to run them. **Six
separate things were red, and nothing said so.** Every one is fixed here, each
in its own named commit.

## The workflow

Eleven checks from five job definitions, on `push: main` and every
`pull_request`, `permissions: contents: read`:

| Check | What it runs | AGENTS.md gate |
|---|---|---|
| `Lint (ruff)` | `uvx ruff@0.16.6 check --isolated --select E4,E7,E9,F .` | — |
| `Tests (runner library)` ×3 | `pytest src/amplifier_recipe_runner/tests` on 3.11 / 3.12 / 3.13 | gate 2 |
| `Tests (tool-recipes + conformance kit)` ×3 | `pytest modules/tool-recipes`, then `conformance/kit/kit.py --run`, on 3.11 / 3.12 / 3.13 | gates 1 + 3 |
| `Conformance (legacy-compat)` ×3 | `conformance/legacy-compat/harness.py --assert` on 3.11 / 3.12 / 3.13 | gate 4 |
| `Bundle structure (YAML)` | parses `bundle.md` frontmatter and `behaviors/*.yaml` | — |

**All four of AGENTS.md's gates are wired.** None is skipped, narrowed, or run
behind an error-tolerant step key.

The commands are AGENTS.md's own **Gates** section — all four of them — so a
green run here means the same thing as a clean local run. Ruff's **tool version and rule set are
both pinned**, with `--isolated` so no config discovered up the tree can change
the answer. The Python matrix is every version `requires-python = ">=3.11"`
claims — which is how two of the four findings below were caught.

**No path filters, no error-tolerant step keys, no shell failure suppression.**
A grep of the workflow for `paths:`, `paths-ignore:`, `continue-on-error` or
`|| true` returns **zero** matches, prose included.

## What the suite actually covers

Not an import smoke. Real, and large:

- **1,224 tests** in `modules/tool-recipes` (1 skipped) — the legacy engine,
  session handling, schema-v2 adapter, approval gates, `steps.jsonl`, the
  `bundle.dot` freshness gate, and `test_tool_description_pin.py` (the pin
  `cd859d1` landed and nothing executed until now).
- **574 tests** in `src/amplifier_recipe_runner/tests` (4 skipped) — the runner
  library.
- **20 conformance fixtures** in `conformance/kit`. CI reports **19/20 passed +
  1 SKIPPED**, and the skip is honest, not silent: `good-activation-install-
  resolves-the-in-bundle-runner [RCP-101]` says *"this host environment does
  not provide amplifier_foundation, which module activation assumes … checked
  NOTHING, not a pass."* It needs the Amplifier CLI's own environment, which a
  runner does not have.

- **5 legacy-compat golden baselines**, asserted byte-for-byte: the serialized
  `ToolResult` of `execute` / `resume` / `approve` for legacy recipes, including
  a staged recipe driven through all four of its approval gates.

Total: **1,798 tests + 19 executed fixtures + 5 golden baselines per Python
version.**

## Red-then-green, proven

Three red runs, so **each of the five job definitions has been seen red for its
own reason**. Both scratch PRs (**#115**, **#117**) are **closed with their
branches deleted** — verified by remote read (`git ls-remote --heads origin
<branch>` → **0 refs** for each), not by trusting the close message.

**RED run 1 — <https://github.com/microsoft/amplifier-bundle-recipes/actions/runs/34159167170>**

```
Tests (runner library) (3.11)   1 failed, 574 passed, 4 skipped in 6.52s
Tests (runner library) (3.12)   1 failed, 574 passed, 4 skipped in 6.86s
Tests (runner library) (3.13)   1 failed, 574 passed, 4 skipped in 6.58s
Tests (tool-recipes …) (3.11)   1 failed, 1224 passed, 1 skipped in 31.27s
Tests (tool-recipes …) (3.12)   1 failed, 1224 passed, 1 skipped in 30.69s
Tests (tool-recipes …) (3.13)   1 failed, 1224 passed, 1 skipped in 29.76s
Lint (ruff)                     F401 [*] `os` imported but unused / Found 1 error.
Bundle structure (YAML)         success  (no defect planted for it in this commit)
```

The **passing counts beside each failure are the load-bearing evidence**: the
real suites collected and executed. A setup or import error would show `0
passed` and would prove nothing. The lint defect was placed in a file no
testpath collects, so a lint failure could never be mistaken for a test one.

**RED run 2 — <https://github.com/microsoft/amplifier-bundle-recipes/actions/runs/34159670751>**

A single malformed `behaviors/red-proof.yaml`:

```
Bundle structure (YAML)         BUNDLE STRUCTURE FAILURES:
                                  - behaviors/red-proof.yaml is not valid YAML: …
Tests (tool-recipes …) (3.13)   2 failed, 1222 passed  (test_bundle_dot_freshness)
Lint (ruff)                     success
Tests (runner library) ×3       success
```

Two things worth noting. The `bundle.dot` freshness gate caught the stale
diagram on its own, unprompted — that gate works. And lint and the runner
library went **green** here, which proves run 1's red came from the planted
defects and not from a broken workflow.

**RED run 3 — <https://github.com/microsoft/amplifier-bundle-recipes/actions/runs/34163006300>**

For the newly wired gate-4 job, scratch PR **#117** (closed, branch
`ci/red-proof-j1e6-gate4` deleted, **0 refs** on remote). One deliberate drift in
the frozen provider catalogue took **all three** `Conformance (legacy-compat)`
legs red while **the other 8 checks stayed green** — the new job fails for its
own reason and nothing else:

```
Conformance (legacy-compat) (3.11/3.12/3.13)  LEGACY-COMPAT DRIFT: 2 case(s) differ
  code-review-comprehensive   -"model": "claude-sonnet-4-5"  +"claude-sonnet-9-9"   <- planted
  bash-step-example           -"recipe-runner"               +"recipe-<USER>"       <- NOT planted
Lint / runner library ×3 / tool-recipes ×3 / bundle structure   success
```

That second failure was **not planted** — it is finding 6 below, a real bug this
red proof caught and that no local run could have.

**GREEN run — <https://github.com/microsoft/amplifier-bundle-recipes/actions/runs/34163577830>**
(commit `dd9381e`), **11/11**, all four gates wired:

```
Lint (ruff)                     All checks passed!
Tests (runner library) ×3       574 passed, 4 skipped
Tests (tool-recipes …) ×3       1224 passed, 1 skipped   +   19/20 fixtures passed
Conformance (legacy-compat) ×3  LEGACY-COMPAT OK: 5 case(s) byte-identical to baseline
Bundle structure (YAML)         bundle structure OK -- 1 bundle.md + 1 behaviour file(s)
```

Earlier green runs on the workflow-only head, before gate 4 was wired:
[34160188969](https://github.com/microsoft/amplifier-bundle-recipes/actions/runs/34160188969)
and [34160837775](https://github.com/microsoft/amplifier-bundle-recipes/actions/runs/34160837775).

## Six things were red. Six fixes, each its own commit

### 1. `4ed60bb` — the library could not be imported on the Python 3.11 it claims to support

`requires-python = ">=3.11"`, and the 3.11 leg went red immediately, in two
independent places:

- `execution.py` used a bare `MappingProxyType({})` as a **class-level
  dataclass default** (`ProviderResolution.by_role`, `SpawnRequest.context`).
  `dataclasses` rejects a default whose class is unhashable, and
  `mappingproxy.__hash__` is `None` on 3.11 — it only became hashable in 3.12
  (gh-87995). Importing the module raised
  `ValueError: mutable default <class 'mappingproxy'> for field by_role`.
  Fixed with a `default_factory` handing back the **same singleton**, so every
  3.12+ instance keeps the exact object identity it has today.
- `cli.py:546` nested a single-quoted f-string inside a single-quoted f-string
  — **PEP 701 syntax, 3.12+ only**. On 3.11 that is a hard `SyntaxError` at
  compile time, not a runtime edge case.

3.11 goes from *2 collection errors, 0 tests run* to the identical
`574 passed, 4 skipped`. A whole-repo `compile()` sweep under 3.11 now reports
zero failing files.

### 2. `495dc02` — the conformance kit failed on Python 3.13

`foreign_types()` matched a type's home module by exact name against
`NEUTRAL_MODULES`. CPython 3.13 moved `Path` into the private `pathlib._local`
submodule, so **the standard library's own `Path` started reading as a foreign
type reaching the public surface**: RCP-004/RCP-104 and RCP-103 both failed on
3.13 while passing on 3.12 (18/20 vs 20/20). The check now also accepts the
top-level package. The probe's meaning is unchanged — `_TaintedStandIn` still
fails it, so the scanner is still proved able to discriminate.

Same commit: `Mapping` was used in two annotations and never imported (ruff
F821 ×2), latent only because `from __future__ import annotations` defers
evaluation — any `get_type_hints()` call would have raised `NameError`.

### 3. `d3078b8` — 37 ruff findings under the pinned rule set

`Found 37 errors` → `All checks passed!`, none of them a behaviour change:

- **28 × E402 in `executor.py`, all from one misplaced statement**:
  `logger = logging.getLogger(__name__)` sat between two halves of the import
  block, so ruff read the 28 imports after it as out of position.
- **4 × F401 + 1 × F841** in tests (unused `Path`, `AsyncMock`, `pytest`,
  `json`; one `execute_recipe` result bound to a name nothing reads — the
  sibling assertion block two functions down genuinely uses its `context` and
  is untouched).
- **1 × F401 in `__init__.py`**: `run_v2_recipe` is a pure re-export, now
  written as the redundant alias ruff reads as deliberate rather than deleted,
  which would have removed it from the package's public surface.

### 4. `5695b47` — two tests failing on main since `d5b72b7`

Reproduced at pristine `origin/main` `cd859d1` in a fresh environment, before
any commit on this branch: **`2 failed, 1222 passed, 1 skipped`**.

The cause is not in those tests' subject. Their fixture's fallback entry is
`provider: anthropic, model: claude-sonnet-*` — **a glob**. `d5b72b7` made an
unmatched pattern resolve to the provider's *real* default, and made a
preference whose model still cannot be filled get **dropped** before the spawn
rather than emitted with an empty model (which blanks the provider's configured
model and kills the run with a 400). That was deliberate, argued at length in
that commit, and covered by 26 tests of its own. But the mock coordinator these
two tests build carries **no model catalogue**, so the glob matches nothing, no
default can be found, and the fallback entry is dropped — `prefs[1]`
`IndexError` in one, `prefs is None` in the other.

The fixture now names a **concrete model** instead of a glob. Pattern
resolution is `test_model_pattern_fallback.py`'s subject, in 26 tests; these
two are about a `class:` entry being resolved and prepended while the explicit
fallback survives. **Neither assertion was weakened** — both still require the
fallback entry present, in position, with provider `anthropic`.

### 5. `4b02fbc` — AGENTS.md gate 4 was red, and the fixture had stopped checking anything

`conformance/legacy-compat --assert` was red on clean main:
`code-review-comprehensive`'s four recorded `provider_preferences` blocks read
`null` where the baseline has a model. Same `d5b72b7` cause as finding 4 — that
one commit left **three** checks stale.

The obvious move — re-record the baseline — would have been wrong, and the
harness's own README says why. It states that with no providers registered *"the
engine leaves the glob as-is and the **glob itself** is what the baseline pins.
That is the provenance that must not silently change."* `d5b72b7` made that
false: an unmatched pattern now resolves to the provider's real default, and a
preference whose model cannot be filled is **dropped** before the spawn. This
harness deliberately registers **no catalogue at all**, so nothing could ever be
filled, every preference was dropped, and the one case whose declared coverage is
*"model glob provenance"* silently pinned **nothing**. Recording `null` would
have made a vacuous fixture permanent — the exact decoration this whole gate
exists to prevent.

**Fixed at the harness seam; the engine is untouched.** A case that exercises
globs now declares a **frozen** catalogue in `cases.yaml`:

```yaml
    provider_catalog:
      anthropic: [claude-haiku-4-5, claude-opus-4-1, claude-sonnet-4-5]
```

Frozen, never live — the same fixture philosophy as `caller_agents`, the scripted
`agent_responses` and the hermetic `gh` shim. It cannot rot on a vendor's release
schedule, which is the entire reason the no-live-catalogue rule existed. The
baseline now pins the **resolved** model (`claude-sonnet-4-5`) instead of the raw
glob: strictly more provenance, because it captures the resolution and not just
its input.

Re-recorded exactly as the README's *"When `--assert` fails"* section sanctions —
read the diff first, then re-record as part of the change that caused it, so the
diff is reviewable. Scope verified rather than asserted:

- **One** baseline file changed. The other four are **byte-identical** and pass
  unchanged — they declare no catalogue, and the recorded key is omitted entirely
  when absent. That is the proof this change is inert where it does not apply.
- The only diff in the changed file is glob → resolved model ×4, the recorded
  `provider_catalog` input, and the `covers` text. `outcome`, `agents_by_step`,
  `agent_spawn_count`, every instruction and `final_context` are unchanged —
  items 1, 2, 4 and 5 of the README's own read-order.
- The gate still discriminates: adding a newer `claude-sonnet-9-9` to the frozen
  catalogue moves the pinned provenance and fails the assert.

### 6. `dd9381e` — the username normalizer corrupted literal fixture text on CI

**Found by the red-proof run for the new gate-4 job, and findable nowhere else.**

Normalization rule 11 replaces the current username with `<USER>` so a baseline
does not depend on whose laptop recorded it. It used `\b` boundaries — and `\b`
treats a **hyphen** as a boundary. Every GitHub runner's username is `runner`.
`bash-step-example`'s env step sets the literal value `User: recipe-runner`. So
on CI, and only on CI, the normalizer rewrote that literal fixture text to
`recipe-<USER>` and reported drift:

```
[assert] bash-step-example: FAIL - drift against baseline
-    "env_result": "Project: LegacyCompat, User: recipe-runner\n",
+    "env_result": "Project: LegacyCompat, User: recipe-<USER>\n",
```

The rule meant to *remove* machine variance was *introducing* it, on any machine
whose username appears hyphen-joined inside recorded content. Invisible on every
developer's laptop; guaranteed on every runner. Fixed by requiring the username
be adjacent to neither a word character nor a hyphen. Verified both ways —
`LEGACY-COMPAT OK: 5 case(s) byte-identical` as the real user *and* with
`USER=LOGNAME=runner` — while `/home/runner/work` and a bare `runner` both still
normalize. **No baseline was re-recorded: the baselines were right, the
normalizer was wrong.**

The regression guard is now structural rather than a test — gate 4 runs on
`ubuntu-latest`, whose username *is* `runner`, so any reintroduction fails this
job on the next PR.

## Also disclosed, not silently excluded

- Ruff's **full modern default tier** (beyond the pinned `E4,E7,E9,F`) reports
  additional opinion-tier findings, and `ruff format --check` would reformat
  files. Both are out of scope for a workflow PR; the pinned selection is
  chosen to match the sibling bundle CIs so the check means the same thing
  across repos.
- `amplifier-core` and `amplifier-foundation` are installed from **git @ main**,
  because neither is published to PyPI and
  `modules/tool-recipes/pyproject.toml` resolves `amplifier-core` through
  `[tool.uv.sources]` to `../../../amplifier-core` — a sibling checkout that
  exists on a developer's machine and never on a runner. Tracking `@main` is
  this repo's own declared intent (see the root pyproject's
  `amplifier-foundation` dependency); the cost is that a change in either repo
  can turn this job red on an unrelated PR. Pinning it is a follow-up.
- `astral-sh/setup-uv@v4` is used with **no cache input at all**. `enable-cache:
  true` keys the cache on `**/uv.lock` and this repo's root ships no lockfile,
  so enabling it fails setup before any check runs; the variant spelling
  `enable-caching:` is not a real input and only logs an *"Unexpected
  input(s)"* warning. Omitted rather than respelled.

## After merge

Confirm `main` HEAD reports a successful check-run — configured is not
installed:

```bash
gh api repos/microsoft/amplifier-bundle-recipes/commits/main/check-runs
```
