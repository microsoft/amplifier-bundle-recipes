# Legacy-compat harness

A golden baseline of **today's** `tool-recipes` behavior for representative
*legacy* recipes, plus an `--assert` mode that fails loudly on any drift.

This is the conformance fixture for the `recipe-dependency-manifest.v1`
contract's "legacy identity pair" clause
(`contracts/recipe-dependency-manifest.v1.md`, Conformance → BAD):

> A representative legacy recipe (agents present in caller) produces an
> identical outcome and identical agent provenance before and after the runner
> lands.

The baselines in `baselines/` were captured against the engine as it exists
now. Re-running `--assert` after the runner work lands is the identity check.

---

## Usage

```bash
cd conformance/legacy-compat

python3 harness.py --list                      # what is covered
python3 harness.py --assert                    # re-run + diff (exit 1 on drift)
python3 harness.py --assert --case bash-step-example
python3 harness.py --record                    # re-capture ALL baselines
python3 harness.py --record --case NAME --show # capture one, print the record
```

`--assert` exits non-zero and prints a unified diff of the normalized record on
any difference. No dependency beyond `pyyaml`, `amplifier-core` and
`amplifier-foundation` (already required by the engine); the harness imports
`modules/tool-recipes` straight out of the repo.

---

## What "legacy recipe" means here

A recipe with **no `schema_version`, no `dependencies` block**. Its `agent:`
strings are handed verbatim to the caller's `session.spawn` capability and
resolved against the **caller's** agent map — the behavior contract Core 10
requires to stay byte-identical.

Every case therefore declares its `caller_agents` explicitly in `cases.yaml`.
The baseline is a statement about *resolution*, not about whichever bundles a
particular developer happens to have installed.

---

## How a run works

Each case runs through the **real caller-facing path**, not a private shortcut:

1. `RecipesTool` — the actual tool class the `recipes` tool mounts — is
   constructed over a real `RecipeExecutor` and a real `SessionManager` rooted
   in a throwaway workspace.
2. `tool.execute({"operation": "execute", ...})` loads the recipe with
   `Recipe.from_yaml`, runs `validate_recipe`, and executes it.
3. Staged cases are driven **through** their gates with the tool's own
   `approve` and `resume` operations, in a loop, exactly as a caller would.
4. Everything the engine does — variable substitution, dotted-path resolution,
   conditions, `parse_json` extraction, bash subprocesses, `on_error`
   handling, checkpointing, approval-gate state — runs for real.

The one substitution is at the **caller seam**:

| Seam | Real | Here |
|---|---|---|
| `session.spawn` capability | spawns an LLM sub-session | records the spawn, returns a scripted response from `cases.yaml` |
| `coordinator.get("providers")` | live provider catalog | `None` — model globs stay unresolved |

### Why the spawn seam is stubbed

An LLM's text is not reproducible, so it could never *be* a byte-identical
baseline. What the contract actually asserts identity over is **outcome and
agent provenance** — which agent name each step resolved to, with which
provider preferences, carrying which fully-substituted instruction. All of that
is produced by the engine and captured verbatim.

Scripting the responses is what makes the *engine's* downstream behavior
observable: `code-review-comprehensive` scripts `assess-severity: "high"` so the
conditional takes the branch that runs `suggest-improvements` and
`validate-suggestions` and skips `quick-approval`. Flip that one scripted value
and `--assert` reports drift in `_skipped_steps`, `agent_spawn_count`,
`agents_by_step`, the per-step instructions and the model provenance — verified.

**This is a fixture, not fabrication.** Every scripted input is declared in
`cases.yaml` and recorded in the baseline. No baseline field is hand-written.

### Why there is no provider catalog

`resolve_model_pattern` queries `coordinator.get("providers")` to expand a glob
like `claude-sonnet-*`. A live catalog changes when a vendor ships a model, so
resolving against it would make the baseline rot on someone else's release
schedule. With no providers registered, the engine leaves the glob as-is and the
**glob itself** is what the baseline pins. That is the provenance that must not
silently change.

---

## What a baseline records

`baselines/<case>.json`, JSON with sorted keys:

| Key | Meaning |
|---|---|
| `recipe`, `recipe_sha256` | which recipe, and its exact bytes |
| `caller_agents` | the caller agent map the case declared |
| `outcome` | tool-call count, final success, final status, final error |
| `tool_calls` | every `recipes` tool input and its full `ToolResult` (including each `paused_for_approval` payload) |
| `final_context` | **every** context key the engine produced — all step outputs, `_skipped_steps`, session/recipe/step metadata |
| `provenance.agents_by_step` | which agent name each agent step resolved to |
| `provenance.agent_spawns` | per spawn: agent name, whether the caller had it, the caller's config for it, `provider_preferences`, `session_metadata`, `use_subprocess`, and the **fully-substituted instruction** |
| `events` | the structured hook events emitted (`recipe:start`, `recipe:step`, `recipe:approval`, …) |
| `progress` | the human-facing progress messages shown |

The full instruction text is deliberately kept: a substitution regression that
changed one interpolated value would otherwise pass unnoticed.

---

## Normalization rules

Applied recursively to every string in the record (keys and values), in this
order. Everything else is compared byte-for-byte.

| # | Pattern | Replacement | Why |
|---|---|---|---|
| 1 | the run's temp workspace path | `<WORKSPACE>` | fresh temp dir per run |
| 2 | the repo root path | `<REPO>` | differs per checkout |
| 3 | `$HOME` | `<HOME>` | differs per machine |
| 4 | `[0-9a-f]{16}-\d{8}-\d{6}_recipe` | `<SESSION_ID>` | generated per run |
| 5 | ISO-8601 timestamps | `<TIMESTAMP>` | wall clock |
| 6 | `date(1)` default output (12h and 24h forms) | `<DATE>` | wall clock |
| 7 | bare `YYYY-MM-DD` | `<DATE>` | wall clock |
| 8 | `HH:MM:SS` | `<TIME>` | wall clock |
| 9 | `/tmp/<name>` | `<TMP>` | any temp path not already covered by rule 1 |
| 10 | the host's hostname | `<HOSTNAME>` | differs per machine |
| 11 | the current username (word-bounded) | `<USER>` | differs per machine |

Paths are substituted longest-first so `<WORKSPACE>` never degrades to `<HOME>`.

### Volatile outputs

A context key listed under a case's `volatile_outputs` is replaced wholesale
with `"<VOLATILE>"`. This is for values that are *machine state, not engine
behavior*, and that no mechanical rule can stabilize.

Currently exactly one: `bash-step-example`'s `tmp_listing`, which is
`ls -la | head -5` against the real `/tmp`. Its presence and type are still
asserted; its content is not.

### Hermetic fixtures

A case may declare `fixture` (a directory under `fixtures/` copied into the
workspace) and `path_prepend` (a directory inside it prepended to `PATH` for
that case's bash steps).

`repo-activity-analysis` uses both: `fixtures/repo-activity/shims/gh` is a
hermetic stand-in for the GitHub CLI. The recipe's own bash, `jq`, dotted-path
substitution and `parse_json` handling all run for real against fixed data. The
shim answers only the two invocations the recipe makes and exits 64 on anything
else, rather than returning a plausible empty result.

Python logging output is **not** recorded: the engine's `depends_on` warning is
cached once per process lifetime, so recording it would make the baseline depend
on case ordering.

---

## Cases

| Case | Recipe | Exercises |
|---|---|---|
| `bash-step-example` | `examples/bash-step-example.yaml` | bash steps, `output_exit_code`, condition on an exit code, `on_error: continue`, `cwd`, `env` — zero agent spawns |
| `test-parse-json` | `examples/test-parse-json.yaml` | `parse_json: true` (extraction out of prose) vs the default (prose preserved), 3 agent steps |
| `repo-activity-analysis` | `examples/repo-activity-analysis.yaml` | **bash steps + `parse_json` in one recipe** — 7 bash steps and 4 agent steps with `parse_json`, dotted-path substitution into bash, conditional branch on a parsed bash output, `on_error: continue`, two caller agents |
| `code-review-comprehensive` | `examples/code-review-recipe.yaml` | 6 agent steps, conditional routing, legacy step-level `provider` + `model`, model-glob provenance |
| `dependency-upgrade-staged` | `examples/dependency-upgrade-staged-recipe.yaml` | staged recipe, **4 approval gates driven through** via the tool's `approve` + `resume`, 6 agent steps across 5 stages, two caller agents |

The staged case is baselined **through** every gate, not up-to-gate: each
`paused_for_approval` result is recorded, then cleared with `approve`, then
`resume`d, until the recipe completes.

---

## When `--assert` fails

The diff is the answer. Read it in this order:

1. `outcome` — did the recipe still complete / pause / fail the same way?
2. `provenance.agents_by_step` and `agent_spawn_count` — did a step resolve to a
   different agent, or stop spawning at all? **This is the contract clause.**
3. `provenance.agent_spawns[].provider_preferences` — did model/provider
   resolution change?
4. `provenance.agent_spawns[].instruction` — did variable substitution change?
5. `final_context` — did a step output or the skip set change?

If the change is **intended**, re-record with `--record` and commit the new
baseline as part of the change that caused it, so the diff is reviewable. Never
re-record to make a red assert go away without reading it first.
