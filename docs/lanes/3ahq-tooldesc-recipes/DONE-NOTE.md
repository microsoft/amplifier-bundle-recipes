# Lane `3ahq-tooldesc-recipes` — DONE-NOTE

**Item:** `model_performance-d8s3` — *lean head repo 7/13 amplifier-bundle-recipes: `recipes` description*
**Outcome:** **A. RESOLVED.** Every deliverable DONE except one recorded NOT-POSSIBLE for a
reason the goal itself names in advance (this repo has no CI).
**Spend:** **$0.00** of a $0.00 authority. No API call, no DTU, no infrastructure created,
nothing to tear down.

---

## Headline

`recipes` tool description: **1,788 → 1,181 chars, 607 saved (33.9%)**.
`bundle.dot` token estimate: **~765 → ~613 tok**.

This string renders into the tool-schema block of **every request of every session** that
mounts this bundle, used or not — so the saving is per-request and unconditional. It ships
on `g7h3`'s **-13.57% $/task, 95% CI [-22.27%, -4.86%]** (all three pre-registered
estimators excluding zero). Nothing here re-bought that measurement.

Two commits, RED then GREEN, on `lane/3ahq-tooldesc-recipes`:

| | Commit | What |
|---|---|---|
| 1 | `b2b9615` | guardrail alone — **fails** against the 1,788-char description |
| 2 | `cab473d` | the description change — turns it green |

---

## Deliverables

| # | Deliverable | State |
|---|---|---|
| 1 | The patch applied (or hand-ported, divergence named, never fuzzed) | **DONE** — hand-ported; two independent divergences named below |
| 2 | Fidelity table re-verified at today's head, not inherited | **DONE** — and it **found a genuine loss** |
| 3 | Stock → lean char counts | **DONE** — 1,788 → 1,181 (−607) |
| 4 | Byte-for-byte pin test against the v1 text | **DONE** — `modules/tool-recipes/tests/test_tool_description_pin.py`, 15 tests |
| 5 | CI red/green run URLs | **NOT POSSIBLE** — see below |
| 6 | Draft PR, ready when local suite green, not merged | **DONE** |
| 7 | DONE-NOTE at the lane artifact root | **DONE** — this file |

### 5 — CI run URLs: NOT POSSIBLE, and why

**`.github/workflows` does not exist in this repository.** There is no CI to go red or green,
so no run URLs can exist to quote. This is not a cap effect and not an omission: the goal
names it in advance (*"CI: this repo has NONE … Say that plainly rather than implying a green
run; your pin tests plus the local suite are the evidence, and the CI lane follows you"*), and
this repo is one of the 18 in the queued `j1e6-ci-*` lane, deliberately sequenced **behind**
this one so that CI, when it lands, has this pin test to execute.

**What was executed in its place** — the same RED-then-GREEN discipline, locally, captured to
disk rather than asserted:

* `evidence/guardrail-local-red.txt` — guardrail against the **pre-change** description:
  `2 failed, 13 passed`; the two failures are exactly the byte pin and the char budget
  (`AssertionError: 1788 chars exceeds budget 1181`).
* `evidence/gates-green.txt` — all four AGENTS.md gates after the change.

---

## The patch did not apply. Both reasons, stated.

`git apply --check -p1` of the upstream artifact exits **128** —
`error: corrupt patch at line 40`. Captured verbatim in
`evidence/fidelity-at-head.txt`; the artifact itself is vendored at
`evidence/recipes.patch.upstream`.

1. **The patch file is malformed at its final hunk.** Both the stock and lean texts end
   without a trailing newline, and neither `\ No newline at end of file` marker was emitted —
   so the last `-` line and the first `+` line are concatenated onto one physical line. No
   unified-diff parser can read it. `patch(1)` fails differently and just as loudly
   (`can't find file to patch`, since the header names a synthetic `a/recipes.description`).
2. **The patch's stock context predates `engine_info`.** It reconstructs a 1,581-char
   description; today's head is **1,788**. The divergence is exactly three lines — two
   `Operations:` lines and one `Example:` line, all `engine_info`.

**Ported by hand. No `--fuzz`, no `--3way`, no force.** Fuzz is a silent placement decision:
precedent `l4s1` hit *"Hunk #1 succeeded at 56 with fuzz 2"* and hand-ported instead, and
elsewhere in this batch a fuzzy apply put a diff **147 lines out of position**, caught only by
grepping for numbers that should have been there.

---

## Fidelity, re-verified at today's head — and it found a real loss

Re-run at head with **zc6t's own extractor** (`fidelity_diff.missing_rules`, fetched from
`amplifier-foundation@main`) *plus* an operation-name check that extractor does not perform.
Full output: `evidence/fidelity-at-head.txt`.

| Token | In stock @ head | zc6t v1 (1,020) | **Shipped (1,181)** | Verdict |
|---|---|---|---|---|
| `execute` `resume` `list` `validate` `approvals` `approve` `deny` `cancel` | yes | yes | yes | clean |
| **`engine_info`** | **yes** | **MISSING** | **yes** | **GENUINE LOSS — restored, +161 chars** |
| `engine_info` body (*"git sha, bundle-cache/editable-install shadowing…"*) | yes | MISSING | yes | same root cause |
| `@recipes:examples/code-review.yaml` | yes | yes | yes | clean — the real pointer |
| `@recipes:examples/my-recipe.yaml` | yes | absent | absent | **false positive, already adjudicated** — an invented filename inside a second usage example. Not re-litigated. |

**Why zc6t could not have caught this.** `engine_info` was added to this tool *after* zc6t
took its measurement — its artifact was cut from a 1,581-char stock that had no such
operation, so its report had nothing to flag. This is precisely the case the goal warned
about (*"a real one existed once; assume it can again"*): the second genuine weakening in the
batch, found only because the table was re-derived rather than inherited.

**The restoration, verbatim (+161 chars):**

```
- engine_info - report which tool-recipes engine is running (module file, version, git sha, bundle-cache/editable-install shadowing) without executing anything.
```

The `Engine info: {{"operation": "engine_info"}}` *example* line is deliberately not restored:
every worked example was dropped by zc6t's already-adjudicated compression, and that one
carries no argument the operations list does not already state.

---

## Char counts

| | chars |
|---|---|
| stock @ today's head | **1,788** |
| zc6t v1 artifact (against its own 1,581-char stock: −561) | 1,020 |
| **shipped** = zc6t v1 + `engine_info` restoration | **1,181** |
| **saved at today's head** | **607 (33.9%)** |

The budget arithmetic is **asserted in the test**, not merely stated:
`1,020 + 161 = 1,181`, and `1,181 < 1,788`.

---

## The pin test

`modules/tool-recipes/tests/test_tool_description_pin.py` — 15 tests, and it reads the real
`description` property off the class rather than re-deriving the string from source.

1. **Byte pin** against `tests/data/recipes_description.v1.txt`.
2. **Char budget** 1,181, with its arithmetic asserted and required to stay strictly under
   the pre-change 1,788.
3. **Both vendored texts pinned against each other** — the shipped pin must equal zc6t's
   untouched 1,020-char artifact (`tests/data/recipes_description.zc6t-v1-1020.txt`) plus
   exactly one restored line. Neither file can drift quietly.
4. **Every operation the input schema accepts must be named in the description — derived from
   the schema enum, not hardcoded.** This is the part that matters beyond a pin: adding an
   operation without documenting it is now a red test. That is the exact drift that produced
   the `engine_info` loss above, and it cannot recur silently.
5. **Pointer survival** for `@recipes:examples/code-review.yaml`.

Pinned **per artifact**, never to a whole-head absolute — zc6t measured a real head at 320,410
chars against the parent item's 48,249 threshold, which describes the eval container's bundle
composition, not the product.

---

## Gates (AGENTS.md — all four, before and after)

| Gate | Baseline | After |
|---|---|---|
| `pytest modules/tool-recipes` | 1209 passed, 1 skipped | **1224 passed, 1 skipped** (+15 = the new guardrail) |
| runner library tests | 574 passed, 4 skipped | **574 passed, 4 skipped** |
| conformance kit | 20/20 | **20/20** |
| legacy-compat | 5 cases byte-identical | **5 cases byte-identical** |

`bundle.dot` / `bundle.png` regenerated per AGENTS.md (the freshness gate went red on the
token estimate, as designed); both committed. Baselines were **not** re-recorded.

---

## Spend

**$0.00 spent against a $0.00 authority.** Arithmetic as stated in the goal:
`0 runs × 0 arms × $0 / 1.00 = $0.00`, slack `$0.00`.

**The arithmetic closes.** This deliverable buys no runs at all — applying a measured patch,
re-deriving a fidelity table locally, and running an existing test suite are all zero-cost.
Unlike lane `1ru`, there is no gap between what the authority funds and what the deliverable
requires, so there is nothing to report as branch B. No API call was made; `g7h3`'s $428.10
answer was not re-bought; no infrastructure was created, so nothing was registered in the
ledger and nothing needs tearing down.

---

## Deviations and findings

### F1 — DEFECT IN `GOAL.md`: it describes a different lane's slice

The lane's `GOAL.md` states:

> *"This lane owns ONLY the `amplifier-module-tool-filesystem` slice: `read_file`,
> `write_file`, `edit_file`, `grep`, `glob`."*

and further asserts that *"THIS REPO CARRIES THE ONE REAL WEAKENING `zc6t` FOUND — `edit_file`,
restored in-repo at +450 chars"*.

**Neither is true of this repo.** This worktree is `amplifier-bundle-recipes`; it contains no
filesystem tools, and `fidelity-report.json` attributes the `edit_file` / `ALWAYS` flag to
`amplifier-module-tool-filesystem`, a different repository. This is template bleed from a
sibling `3ahq` lane's goal text.

**Resolved by the goal's own Procedure step 1** — *"the returned description + acceptance
criteria are the authoritative spec; this file summarizes them"* — so the work item governed
and this lane executed the `recipes` slice. Recorded, not absorbed silently.

*A secondary irony worth logging:* the goal's filesystem-lane paragraph closes with *"A real
one existed once; assume it can again."* Applied to the slice this lane actually owns, that is
exactly what happened — `engine_info`.

### F2 — the upstream artifact is not machine-appliable

`recipes.patch` is a corrupt unified diff (missing no-newline-at-EOF markers). Any sibling lane
that reports "applied cleanly" for its own artifact from the same generator should be asked how,
since the generator would have produced the same defect wherever both texts lacked a trailing
newline. Worth one check by the manager across the batch; not actionable from inside this lane.

### F3 — acceptance criterion 1 is unsatisfiable as literally written, at today's head

The item's first criterion asks for byte-equality with the 1,020-char v1 entry; its second asks
that **every operation name including `engine_info`** survive. At today's head those cannot both
hold, because v1 predates `engine_info`. Resolved in favour of fidelity, per the goal's explicit
instruction — *"Restore anything dropped and note the byte delta"* — with the v1 artifact
vendored unmodified beside the pin so the divergence is one `diff` away for any reviewer.

## What remains open

Nothing in this lane. Downstream: `j1e6-ci-*` wires CI for this repo and will make this pin
test execute on every future PR — which is the whole reason it was sequenced after this lane.
The PR is a **draft, not merged**; the merge is the manager's stage.
