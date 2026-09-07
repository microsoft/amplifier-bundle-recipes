This repo had **no `.github/workflows` directory at all**. Its 1,798 tests, its
20-fixture conformance kit, and the byte-for-byte tool-description pin that
landed in `cd859d1` ran only when a human remembered to run them. **Four
separate things were red on clean `main`, and nothing said so.** Each is fixed
in its own named commit here; one more is reported and deliberately *not*
fixed.

## The workflow

Eight checks from four job definitions, on `push: main` and every
`pull_request`, `permissions: contents: read`:

| Check | What it runs | AGENTS.md gate |
|---|---|---|
| `Lint (ruff)` | `uvx ruff@0.16.6 check --isolated --select E4,E7,E9,F .` | — |
| `Tests (runner library)` ×3 | `pytest src/amplifier_recipe_runner/tests` on 3.11 / 3.12 / 3.13 | gate 2 |
| `Tests (tool-recipes + conformance kit)` ×3 | `pytest modules/tool-recipes`, then `conformance/kit/kit.py --run`, on 3.11 / 3.12 / 3.13 | gates 1 + 3 |
| `Bundle structure (YAML)` | parses `bundle.md` frontmatter and `behaviors/*.yaml` | — |

The commands are AGENTS.md's own **Gates** section, so a green run here means
the same thing as a clean local run. Ruff's **tool version and rule set are
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

Total: **1,798 tests + 19 executed fixtures per Python version.**

## Red-then-green, proven

Scratch PR **#115** on branch `ci/red-proof-j1e6`, since **closed with its
branch deleted** — verified by remote read (`git ls-remote --heads origin
ci/red-proof-j1e6` → **0 refs**), not by trusting the close message. Two red
runs, so each of the four job definitions was seen red **for its own reason**:

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

**GREEN runs — <https://github.com/microsoft/amplifier-bundle-recipes/actions/runs/34160188969>**
(commit `3189598`, the workflow-only head) and
**<https://github.com/microsoft/amplifier-bundle-recipes/actions/runs/34160837775>**
(commit `a3c6d4f`, with this lane's evidence artifacts). Both 8/8:

```
Lint (ruff)                     All checks passed!
Tests (runner library) ×3       574 passed, 4 skipped
Tests (tool-recipes …) ×3       1224 passed, 1 skipped   +   19/20 fixtures passed
Bundle structure (YAML)         bundle structure OK -- 1 bundle.md + 1 behaviour file(s)
```

## Clean main was red. Four fixes, each its own commit

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

## Reported, NOT fixed: AGENTS.md gate 4 is not wired

`conformance/legacy-compat/harness.py --assert` is **red on clean main** and is
**deliberately not a job in this workflow**. This is stated in the workflow's
own header, not hidden.

```
[assert] bash-step-example:          PASS
[assert] test-parse-json:            PASS
[assert] repo-activity-analysis:     PASS
[assert] code-review-comprehensive:  FAIL - drift against baseline
[assert] dependency-upgrade-staged:  PASS
LEGACY-COMPAT DRIFT: 1 case(s) differ from baseline.
```

Same root cause as finding 4: that case's fixture pins `claude-sonnet-*` /
`claude-opus-*` globs, and four recorded `provider_preferences` blocks are now
`null` because `d5b72b7` drops a preference it cannot fill. Reproduce in one
command:

```bash
PYTHONPATH=src:modules/tool-recipes python3 conformance/legacy-compat/harness.py --assert
```

**Why this branch does not fix it.** The fix is to re-record that baseline with
the diff reviewed — a call about legacy engine behaviour that belongs to
whoever owns `d5b72b7`, not to the change that installs CI. AGENTS.md is
explicit: *"Never re-record the legacy-compat baselines to make a change
pass."* So this branch does not re-record it, does not narrow it to the four
passing cases, and does not run it behind an error-tolerant step key. **Wire it
in as its own job the moment that baseline is settled** — the workflow header
says exactly that.

One deliberate commit, `d5b72b7`, therefore left **three** checks stale: two
unit tests and this baseline. With no CI, nobody saw any of them for 67
commits. That is the case for this PR in one sentence.

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
