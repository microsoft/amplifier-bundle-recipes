# perf(agents): trigger-first delegate-catalog descriptions for both agents

**Frontmatter only.** Both agent bodies are byte-identical to `origin/main`. `bundle.dot` /
`bundle.png` are regenerated because AGENTS.md says an agent-description change makes them
stale and `test_bundle_dot_freshness.py` is the gate — red-then-green proven below.

---

## Why this is worth a PR at all

`meta.description` is not documentation. It is injected into the `delegate` tool schema of
**every session, on every turn**, whether or not either agent is ever spawned. That makes it
pay-per-turn text and worth shaping to the catalog standard applied across the ecosystem
(`kp79`): **trigger-first** (the first clause says *when* to reach for the agent, not what it
is), **≤600 chars**, an explicit **USE WHEN / DO NOT USE WHEN**, and **zero**
`<example>`/`<commentary>` blocks.

**Half of that standard was already banked here.** PR #81 and commit `3572697` removed the
example/commentary blocks and pulled both descriptions inside the length cap. What remained was
framing:

| | stock `origin/main` | this branch |
|---|---|---|
| opens with a trigger | ❌ *"Conversational recipe expert…"* / *"Objective pass/fail validation agent…"* | ✅ `USE WHEN …` |
| explicit `USE WHEN` | ❌ | ✅ |
| explicit `DO NOT USE WHEN` | ❌ **neither agent had one at all** | ✅ both |
| ≤600 chars | ✅ 561 / 578 | ✅ 593 / 591 |
| `<example>` / `<commentary>` | ✅ 0 / 0 | ✅ 0 / 0 |

The missing `DO NOT USE WHEN` is the substantive gap: the catalog told a caller when to reach
for each agent and never when *not* to, which is exactly the half that prevents mis-delegation.

---

## Cost, stated plainly: this does NOT shrink the head

| agent | stock chars | branch chars | Δ | est. tokens (bundle.dot) |
|---|---:|---:|---:|---|
| `recipe-author` | 561 | 593 | **+32** | ~140 → ~148 |
| `result-validator` | 578 | 591 | **+13** | ~144 → ~147 |
| **repo total** | **1,139** | **1,184** | **+45** | ~284 → ~295 |

Measured statically against **current `origin/main` (`fa1292b`)**, not against the program's
original census (which said ~1,199 chars — main has drifted since). Reproduce:

```bash
python3 docs/lanes/dae2-catalog-recipes/measure_descriptions.py <stock-checkout> .
```

**+45 bytes per turn buys two DO NOT USE WHEN clauses and trigger-first ordering on both
agents.** If the reviewer's bar is "must not grow the head", this PR fails that bar and should
be closed — the honest trade is stated here rather than buried.

---

## FIDELITY TABLE — facts present in stock and absent from this branch

**None.** Every fact was checked individually:

### `recipe-author`

| stock fact | where it lives now |
|---|---|
| all recipe work: creating, editing, validating, debugging recipe YAML | `creating, editing, validating or debugging recipe YAML` |
| deciding factor: the task produces or changes a recipe file | `USE WHEN a task produces or changes an Amplifier recipe file` |
| flat or staged recipes, approval gates, recipe composition, foreach loops, while/convergence loops, conditional execution | preserved verbatim |
| delegate here rather than writing recipe YAML directly | `Delegate rather than write recipe YAML directly` |
| **conversational** expert | `this conversational expert carries…` |
| carries the complete schema, design patterns, best practices | preserved |
| afterwards, run `result-validator` to verify the recipe meets the user's original intent | preserved |
| — | **ADDED:** `DO NOT USE WHEN no recipe file is produced or changed -- a single-step or ad-hoc task is direct delegation, not a recipe.` |

### `result-validator`

| stock fact | where it lives now |
|---|---|
| objective, evidence-based pass/fail verdict against stated criteria | preserved |
| a recipe `recipe-author` just created or edited | preserved |
| **pass the conversation context too, so intent can be checked** | preserved |
| a recipe step's output · a deployment result · code against a quality rubric · workflow results against compliance requirements | all four preserved |
| **simple** binary validation **and** semantic rubric-based evaluation | `Does simple binary and semantic rubric-based validation` |
| always concludes with a clear machine-readable verdict signal | preserved |
| — | **ADDED:** `DO NOT USE WHEN nothing exists yet to judge, or the check needs filesystem state -- it sees only what it is passed.` |

Both added clauses are grounded in text already in this repo, not invented:

- `context/recipe-awareness.md:14-16` — *"Use direct agent delegation when: Tasks are
  single-step or simple … The workflow is exploratory or ad-hoc."*
- `agents/result-validator.md:7` — *"Pure evaluation agent - receives results as input, no
  filesystem access needed"* — and `:42` — *"This agent evaluates content passed to it, not
  filesystem state."*

---

## Bodies byte-identical — md5, both sides

```
recipe-author      29968bfc324e64c987e369c91998ea70  ==  29968bfc324e64c987e369c91998ea70
result-validator   5eb03563eb521e9473a8ac3a49ee573f  ==  5eb03563eb521e9473a8ac3a49ee573f
```

Body = everything after the closing frontmatter fence. Evidence:
`docs/lanes/dae2-catalog-recipes/evidence/description-ab.txt`.

---

## `validate-agents` — before vs after, both real checkouts

Foundation's `validate-agents` **v1.8.0**, DETERMINISTIC phases only (`environment-check`,
`agent-discovery`, `structural-validation`, `quality-classification` — all `type: bash`, no LLM,
**$0**). This lane's spend authority is $0, so the recipe's three LLM phases
(`description-quality-check`, `tool-access-analysis`, `synthesize-report`) were **not** run and
no full-recipe verdict is claimed. BEFORE is a detached `git worktree` at `origin/main`
(`fa1292b`); AFTER is this branch.

```
                          BEFORE (fa1292b)              AFTER (this branch)
agents discovered         6                             6
structural summary        total 6, passed 6,            total 6, passed 6,
                          errors 0, warnings 8          errors 0, warnings 8
quality_level             needs_work                    needs_work
quality summary           good 2, needs_work 4          good 2, needs_work 4

  recipe-author           chars=561 ex=0 com=0          chars=593 ex=0 com=0
                          errors=[] warnings=[]         errors=[] warnings=[]
  result-validator        chars=578 ex=0 com=0          chars=591 ex=0 com=0
                          errors=[] warnings=[]         errors=[] warnings=[]
```

**Structural verdict: 6/6 passed, 0 errors — unchanged.** The two real agents carry zero errors
and zero warnings on both sides.

**Read `quality_level: needs_work` before reacting to it.** It is identical in both arms and
comes entirely from four **conformance-kit test fixtures**, not from this repo's agents:

```
conformance/kit/fixtures/bundles/impostor/agents/reviewer.md     chars=63
conformance/kit/fixtures/bundles/lean-caller/agents/packager.md  chars=47
conformance/kit/fixtures/bundles/supplier/agents/reviewer.md     chars=60
conformance/kit/fixtures/bundles/supplier/agents/summarizer.md   chars=64
```

Each trips `SHORT_DESCRIPTION` + `NO_TOOLS_SECTION`, which is correct behaviour for a stub whose
whole job is to be a stub. v1.7.0's discovery exclusion set is
`['.git', '.venv', 'docs', 'node_modules', 'test-fixtures', 'tests']` — it has `test-fixtures`
and `tests` but not `fixtures`, so `conformance/kit/fixtures/**` is walked as production. (The
`src/**/tests/fixtures/**` agents are excluded, via `tests`.) **Not fixed here** — the recipe
lives in `amplifier-foundation`, which this lane does not own. Flagged for that repo.

Evidence: `evidence/validate-agents-phases.txt`, `evidence/validate-agents-discovery.txt`.

---

## Gates — all four green, plus the CI lint job

Run exactly as `AGENTS.md` § Gates and `.github/workflows/ci.yml` run them.

| gate | command | result |
|---|---|---|
| 1 | `pytest modules/tool-recipes -q` | **1224 passed, 1 skipped** |
| 2 | `pytest src/amplifier_recipe_runner/tests -q` | **574 passed, 4 skipped** |
| 3 | `conformance/kit/kit.py --run` | **19/20 fixtures passed**, 1 skipped |
| 4 | `conformance/legacy-compat/harness.py --assert` | **LEGACY-COMPAT OK: 5 case(s) byte-identical to baseline** |
| lint | `uvx ruff@0.16.6 check --isolated --select E4,E7,E9,F .` | **All checks passed!** |

Gate 3's one skip — `good-activation-install-resolves-the-in-bundle-runner [RCP-101]` — is
**environmental and pre-existing**: re-running gate 3 in the untouched `origin/main` worktree
gives the identical `19/20 fixtures passed` + 1 skip. Nothing in this PR touches it.

### `bundle.dot` freshness: red-then-green on the final descriptions

```
RED   (origin/main's bundle.dot/png + this branch's descriptions)
      FAILED test_bundle_dot_freshness.py::test_bundle_dot_source_hash_matches_regenerated
      FAILED test_bundle_dot_freshness.py::test_bundle_dot_is_byte_identical_to_regenerated
      2 failed, 1222 passed, 1 skipped

GREEN (regenerated bundle.dot/png, as committed)
      1224 passed, 1 skipped
```

Regenerated with the exact command AGENTS.md prescribes. `source_hash`
`c92d9c63…` → `b21cdb63…`. Evidence: `evidence/gate1-red-then-green.txt`,
`evidence/gates-2-3-4-lint.txt`.

**CI state:** this repo gained CI in #116 (`fa1292b`), one commit before this branch's base. The
five jobs (lint, runner tests ×3 pythons, module tests + conformance kit ×3, legacy-compat ×3,
bundle structure) will run on this PR; the equivalents were run locally and are green above.

---

## What was deliberately NOT done

- **No body edits.** An `<example>` sweep was unnecessary — there were none in either
  description to begin with.
- **No LLM measurement.** Spend authority for this lane is **$0.00**; actual spend **$0.00**. All
  numbers here are static reads of frontmatter and local deterministic test runs.
- **No scratch-`AMPLIFIER_HOME` catalog render.** Rendering the live `delegate` catalog needs an
  Amplifier session; on this shared host that has previously rewritten the real install's
  editable `.pth` files. The description string *is* the catalog row's payload, so measuring it
  statically answers the same question without that risk.
