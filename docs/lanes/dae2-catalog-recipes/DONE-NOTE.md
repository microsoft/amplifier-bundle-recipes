# Lane `dae2-catalog-recipes` — delegate-catalog SOURCES sweep, `amplifier-bundle-recipes`

**Work item: `model_performance-e6ho`** (filed and claimed by this lane; child of
`model_performance-dae2`) · **Outcome branch: A — RESOLVED AT THE DRAFT PR** · **Spend: $0.00 of
$0.00 authority**

Both of this repo's delegate-catalog agents now carry a **trigger-first** `meta.description`
under the 600-char cap with an explicit **USE WHEN / DO NOT USE WHEN** and **zero**
`<example>`/`<commentary>` blocks. Frontmatter only — both bodies are md5-identical to
`origin/main`. `bundle.dot`/`bundle.png` regenerated because the repo gates their freshness.

---

## 0. The parent item was held — child filed, per the goal's own recovery path

`GOAL.md` Procedure 1 says to claim `model_performance-dae2`. It is **held by another session**
(`agent-spark-1-4096607`) — expected, since dae2 is a one-item/many-lanes container.

```
claim model_performance-dae2 ... failed: issue already claimed by agent-spark-1-4096607
```

Per the goal's stated recovery pattern (precedent `model_performance-k75p`), this lane filed a
per-repo child, `related: relates-to model_performance-dae2`, and claimed that:

| step | result |
|---|---|
| `work_claim item_id=model_performance-dae2` | refused, held elsewhere |
| `work_add` per-repo child naming `amplifier-bundle-recipes` | **`model_performance-e6ho`** |
| `work_claim item_id=model_performance-e6ho` | claimed, custody established |

---

## 1. The baseline had drifted — measure against current `origin/main`, not the census

`GOAL.md` says *"2 agents, ~1,199 ch (VERIFY … a sibling lane found its own baseline had
drifted)"*. It had, twice over:

- `origin/main` moved `71cf2c1` → **`fa1292b`** during this lane's own setup (two merges landed:
  #114, the lean `recipes` tool description, and #116, this repo's first CI). The branch was
  reset onto `fa1292b` before any measurement.
- The description total at `fa1292b` is **1,139 chars**, not ~1,199 — PR #81 and commit
  `3572697` had already removed the `<example>`/`<commentary>` blocks and pulled both
  descriptions under the cap.

**So the length half of the standard was already banked before this lane started, and the
example-sweep half had nothing to sweep.**

---

## 2. What was actually missing, and what it cost to add

| property | stock `fa1292b` | branch |
|---|---|---|
| trigger-first opening | ❌ both open by naming themselves | ✅ both open `USE WHEN` |
| explicit `USE WHEN` | ❌ | ✅ |
| explicit `DO NOT USE WHEN` | ❌ **absent from both** | ✅ both |
| ≤600 chars | ✅ 561 / 578 | ✅ 593 / 591 |
| `<example>` / `<commentary>` | ✅ 0 | ✅ 0 |

| agent | before | after | Δ chars | Δ est. tokens |
|---|---:|---:|---:|---|
| `recipe-author` | 561 | 593 | **+32** | ~140 → ~148 |
| `result-validator` | 578 | 591 | **+13** | ~144 → ~147 |
| **repo total** | **1,139** | **1,184** | **+45** | ~284 → ~295 |

**This is an honest negative on head cost and is reported as one.** The sweep's usual headline —
a large per-turn byte saving — is not available in this repo because a previous pass already took
it. What `+45 B/turn` buys is trigger-first ordering plus the missing negative half of the
trigger on both agents. A reviewer whose bar is "must not grow the head" should close the PR;
the trade is on the PR body, not buried.

**"An edit that exists to produce a diff is worse than no edit"** was the live question here.
The edit was made because *two of the five* standard properties were genuinely absent (trigger
ordering, `DO NOT USE WHEN`), not because a diff was wanted. Had the descriptions already been
trigger-first, the correct output would have been "left unedited" — see §7.

---

## 3. FIDELITY — zero facts lost

Every stock fact was enumerated and located in the lean text; the full table is in
`PR-BODY.md`. Summary: **no USE WHEN fact, trigger condition, or constraint present in stock is
absent from the branch.** Two clauses were **added**, each grounded in text already in this repo:

| added clause | grounding |
|---|---|
| `recipe-author`: *"DO NOT USE WHEN no recipe file is produced or changed -- a single-step or ad-hoc task is direct delegation, not a recipe."* | `context/recipe-awareness.md:14-16` |
| `result-validator`: *"DO NOT USE WHEN nothing exists yet to judge, or the check needs filesystem state -- it sees only what it is passed."* | `agents/result-validator.md:7` (`tools: []`, *"no filesystem access needed"*) and `:42` (*"evaluates content passed to it, not filesystem state"*) |

Two facts were restored on a second pass after the first draft dropped them — recorded because a
silent drop is exactly what the fidelity gate exists to catch:

| restored | cost |
|---|---|
| `recipe-author`: **"conversational"** (the agent asks clarifying questions rather than one-shotting) | +24 chars (569 → 593) |
| `result-validator`: **"simple"** binary validation (names the body's own `Simple Binary Validation` mode) | +7 chars (584 → 591) |

Bodies, md5, both sides:

```
recipe-author      29968bfc324e64c987e369c91998ea70  ==  29968bfc324e64c987e369c91998ea70
result-validator   5eb03563eb521e9473a8ac3a49ee573f  ==  5eb03563eb521e9473a8ac3a49ee573f
```

---

## 4. `validate-agents` — structural verdict unchanged, and why `needs_work` is not a regression

Foundation `validate-agents` **v1.8.0**, deterministic phases 0–3 only (all `type: bash`, no
LLM, **$0**). BEFORE is a detached `git worktree` at `origin/main` `fa1292b`; AFTER is this
branch. Both arms:

```
agents discovered   6
structural summary  {'total': 6, 'passed': 6, 'errors': 0, 'warnings': 8}
quality_level       needs_work
quality summary     {'total': 6, 'good': 2, 'polish': 0, 'needs_work': 4, 'critical': 0}
  recipe-author     561 -> 593 chars, examples=0 commentary=0, errors=[] warnings=[]
  result-validator  578 -> 591 chars, examples=0 commentary=0, errors=[] warnings=[]
```

**Structural: 6/6 passed, 0 errors — identical before and after. Both real agents are clean on
both sides.**

The recipe's three LLM phases (`description-quality-check`, `tool-access-analysis`,
`synthesize-report`) were **not** run — $0 authority — so **no full-recipe verdict is claimed
here**. Precedent for running the deterministic subset: the `kp79` lane on
`amplifier-bundle-attractor`.

### Finding for `amplifier-foundation` (not fixed here — not this lane's repo)

4 of the 6 "agents" `validate-agents` discovers in this repo are **conformance-kit test
fixtures**, and they alone produce all 8 warnings and the `needs_work` level, in **both** arms:

```
conformance/kit/fixtures/bundles/impostor/agents/reviewer.md     chars=63
conformance/kit/fixtures/bundles/lean-caller/agents/packager.md  chars=47
conformance/kit/fixtures/bundles/supplier/agents/reviewer.md     chars=60
conformance/kit/fixtures/bundles/supplier/agents/summarizer.md   chars=64
```

v1.7.0's exclusion set is `['.git', '.venv', 'docs', 'node_modules', 'test-fixtures', 'tests']`
— it carries `test-fixtures` and `tests` but **not `fixtures`**, so `conformance/kit/fixtures/**`
is walked as production code. (`src/**/tests/fixtures/**` *is* excluded, via `tests` — so the
same repo's fixtures are treated inconsistently depending on which directory word they happen to
sit under.) Effect: a repo with two clean agents reports `quality_level: needs_work` forever,
which is the "constant alarm" failure mode `00-what-we-know.md` §2(q) names — an instrument that
cannot come out both ways has not been tested.

---

## 5. Gates and CI

| gate | result |
|---|---|
| 1 `pytest modules/tool-recipes -q` | **1224 passed, 1 skipped** |
| 2 `pytest src/amplifier_recipe_runner/tests -q` | **574 passed, 4 skipped** |
| 3 `conformance/kit/kit.py --run` | **19/20 fixtures passed**, 1 skipped |
| 4 `conformance/legacy-compat/harness.py --assert` | **LEGACY-COMPAT OK: 5 case(s) byte-identical to baseline** |
| CI lint job | `ruff@0.16.6 --isolated --select E4,E7,E9,F` → **All checks passed!** |

Gate 3's single skip (`good-activation-install-resolves-the-in-bundle-runner [RCP-101]`) is
**environmental and pre-existing** — re-running gate 3 in the untouched `origin/main` worktree
gives the same `19/20 fixtures passed` + 1 skip.

**CI exists** (added by #116, `fa1292b`, one commit before this branch's base) and will run all
five jobs on the PR. Local equivalents are green above.

`bundle.dot` freshness, red-then-green on the final descriptions:

```
RED    2 failed (source_hash + byte-identical), 1222 passed, 1 skipped
GREEN  1224 passed, 1 skipped        source_hash c92d9c63… -> b21cdb63…
```

---

## 6. Census safety

`GOAL.md`'s CENSUS SAFETY rule was honoured: **no `amplifier` process was run**, with or without
a scratch `AMPLIFIER_HOME`. Every measurement is a static read of YAML frontmatter
(`measure_descriptions.py`) or a local deterministic test run. The `.pth` tripwire is clean —
see §8.

This also means the lane did **not** render the live `delegate` catalog through a scratch
`AmplifierSession` the way `kp79` did. The description string is the catalog row's payload, so
the static number answers the same question; the catalog-row framing bytes (`  - ns:name: `) are
unchanged by construction.

### Cross-lane hazard found and worked around

A first attempt staged the stock checkout at `/tmp/dae2-stock/`. It came back containing
`setup-digital-twin` and `validator` — **agents from `amplifier-bundle-amplifier-tester`, not
this repo**. Sibling lanes in this batch share the `dae2-` id prefix and the same host `/tmp`,
so they collided on the path. The measurement caught it only because the script asserts the
agent SET matches across arms — a script comparing totals would have silently reported
`1,139 → 1,184` as `4,046 → 1,184`, a fabricated 71% "saving". **Advice for sibling lanes: stage
A/B checkouts under your own lane directory, never a shared `/tmp` path keyed on the batch id,
and assert the agent set, not just the totals.**

---

## 7. Deliverables

| deliverable | status |
|---|---|
| Every targeted agent description trigger-first, ≤600 chars, USE WHEN / DO NOT USE WHEN, zero example/commentary | **DONE** — 2 of 2 agents |
| FIDELITY TABLE: facts in stock, absent in lean | **DONE — none.** Full table in `PR-BODY.md` |
| Bodies byte-identical, md5 quoted both sides | **DONE** — §3 |
| Before/after char counts per agent + repo total vs current `origin/main` | **DONE** — §2 (1,139 → 1,184, **+45**) |
| `validate-agents` run on the branch, verdict + agent count quoted | **PARTIAL, disclosed** — deterministic phases only ($0 authority): 6 discovered, 6/6 structural pass, 0 errors, unchanged. LLM phases not run; no full-recipe verdict claimed |
| CI green where the repo has CI | **DONE** — repo has CI (#116); all four gates + the lint job green locally; CI runs on the PR |
| Anything already compliant left unedited and named | **DONE** — both descriptions were already ≤600 and example-free; neither was trigger-first and neither had a `DO NOT USE WHEN`, which is why both were edited. No other agent exists in this repo |
| Draft PR | **DONE** — see §9 |

---

## 8. Census-safety tripwire

```
$ grep -l /tmp/ ~/.local/share/uv/tools/amplifier/lib/python3.13/site-packages/*.pth
(no output)
```

Clean.

---

## 9. Landing stage

Ships as a **draft PR** on `microsoft/amplifier-bundle-recipes` from
`lane/dae2-catalog-recipes`. **This lane does not merge.** The merge is the manager's next
stage.

---

## 10. Reproduce

```bash
# static A/B of every agent description + body md5 (no amplifier process, $0)
git worktree add --detach ../stock-worktree origin/main
python3 docs/lanes/dae2-catalog-recipes/measure_descriptions.py ../stock-worktree .

# validate-agents deterministic phases, either arm ($0, no LLM)
python3 docs/lanes/dae2-catalog-recipes/run_validate_phases.py \
    <amplifier-foundation>/recipes/validate-agents.yaml "$PWD"
```

Evidence under `docs/lanes/dae2-catalog-recipes/evidence/`:
`description-ab.txt` · `validate-agents-phases.txt` · `validate-agents-discovery.txt` ·
`gate1-red-then-green.txt` · `gates-2-3-4-lint.txt`.
