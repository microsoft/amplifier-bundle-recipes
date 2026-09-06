# Executor parity matrix — library engine vs legacy in-session engine

Two engines execute recipe steps in this repo, and they must not mean different
things by the same YAML.

| | Engine | Where it runs |
|---|---|---|
| **Library** | `src/amplifier_recipe_runner/engine.py` (`StepEngine`) | Standalone: `recipe-runner run`, or any host embedding the library. No Amplifier session, no coordinator. |
| **Legacy** | `modules/tool-recipes/amplifier_module_tool_recipes/executor.py` (`RecipeExecutor`) | In-session, inside Amplifier. Years of production use; its ~1,050 tests are the reference behaviour. |

This document is the **contract between them**: one row per step-type ×
behaviour, saying whether the two agree and — where they do not — exactly what
differs and why. A difference that is not in the "Deliberate deltas" section
below is a **defect**, not a design.

**Scope note (recipes-xov).** This lane made the *library* capable and proved
it. It deliberately did **not** change how the in-session tool picks its
engine: `modules/tool-recipes` still routes v2 in-session execution through the
legacy engine fed a closed-world catalog (`execution_mode:
v2-closed-world-legacy-engine`). Flipping that switch is a later decision, and
this matrix is the evidence it will be decided on.

## How the claims here are checked

| Claim | Checked by |
|---|---|
| Conditions mean the same thing | `src/amplifier_recipe_runner/expressions.py` is a **verbatim vendored copy** of the legacy evaluator, and `tests/test_expressions_ported.py` runs the legacy engine's own 28 expression tests against it. |
| Step semantics match end to end | `conformance/kit` fixture `good-full-step-vocabulary-matches-the-legacy-engine`: one full-vocabulary recipe, run on **both** engines, every recipe-visible context variable diffed. |
| Individual behaviours match | `src/amplifier_recipe_runner/tests/test_engine.py` — scenario-for-scenario ports of the legacy step tests, each class naming the legacy file it mirrors. |
| Mid-loop resume means the same thing | `conformance/kit` fixture `good-checkpointed-foreach-resumes-mid-loop-on-both-engines`: one `checkpoint_iterations:` foreach, interrupted at item 3 and resumed, on **both** engines — comparing *which items each engine actually re-executed*, not just the final result. |
| Where the model comes from does not change the outcome | `conformance/kit` fixture `good-provider-agnostic-recipe-runs-on-either-layer-and-refuses-on-neither`: one recipe naming no provider, run with the host's port supplying it, with the recipe pinning it, and with neither — all three diffed against each other **and** against the legacy engine (Δ10). |

The kit fixture is **self-discriminating**: it compares the library against the
other implementation rather than against an authored expectation, so a drift in
*either* engine fails it. Measured during this lane, two independent injected
mutations were each caught:

| Injected mutation | Result |
|---|---|
| `extract_json_aggressively` returns the first parse even when trivial (`{}`) | FAIL — `payload` lost its keys, and the next step's `{{payload.count}}` failed by name |
| `update_context` applied *before* the loop body instead of after | FAIL — `ticks` differed: legacy `tick-N-seen-N`, library `tick-N-seen-N+1` |

---

## Matrix

Legend: **=** identical semantics · **Δ** deliberate difference (see below) ·
**n/a** not applicable to that engine.

### Step dispatch

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| `type:` defaults to `agent` | yes | yes | = |
| Types accepted: `agent`, `bash`, `recipe` | yes | yes | = |
| An unknown `type:` | refused by name (`UnsupportedStepError`) | validator rejects at parse | = (both refuse; the message differs) |
| A step with neither agent+prompt, command, nor recipe | refused by name | validator rejects at parse | = |
| Step id defaults when `id:` is absent | positional `step-<n>` over the flattened step list | validator requires `id` | Δ1 |
| Declaration order is execution order; `depends_on` is advisory only | yes | yes (logs one advisory warning per recipe) | = (library does not warn) |

### Variable substitution

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| `{{var}}` and `{{a.b.c}}` dotted paths | yes | yes | = |
| Booleans render `true`/`false`, not `True`/`False` | yes | yes | = |
| dict/list render as JSON, not Python `repr` | yes | yes | = |
| Undefined variable → `ValueError` naming available keys | yes | yes | = (message text ported verbatim) |
| Non-dict intermediate → error naming `parse_json` as the likely fix | yes | yes | = (message text ported verbatim) |
| Whole-variable reference keeps its native type (dict stays a dict into a sub-recipe `context:`) | yes | yes | = |
| Composite string (`"id={{x}}"`) is stringified | yes | yes | = |

### `type: bash`

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| Executed via a resolved real `bash -c` (never `/bin/sh`) | yes | yes | = |
| Windows: Git-Bash preferred, WSL launcher refused with a named remedy | yes | yes | = |
| `AMPLIFIER_PYTHON` injected into the environment | yes | yes | = |
| `AMPLIFIER_RECIPE_SCRATCH_DIR` injected | when the run has a state dir | when the run has a session | Δ2 |
| `env:` values substituted | yes | yes | = |
| `cwd:` substituted, resolved relative to the workspace, must exist and be a directory | yes | yes (relative to `project_path`) | = |
| stdout is the step result; stderr is not | yes | yes | = |
| `output_exit_code:` records the exit code **as a string** | yes | yes | = |
| Non-zero exit under `on_error: fail` → run fails | yes | yes | = |
| Non-zero exit under `on_error: continue` → absorbed, stdout still recorded | yes | yes | = |
| Non-zero exit under `on_error: skip_remaining` → stop early, run still succeeds | yes | yes | = |
| Exec refusal (E2BIG/ENOENT) honours `on_error` like a non-zero exit, synthetic exit code 126 | yes | yes | = |
| Oversized rendered command (>100 KB) warns before the OS cliff | event `step:warning` | logger warning | = (same threshold, different channel) |
| Timeout kills the process and fails the step by name | yes | yes | = |

### `parse_json` and result processing

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| Conservative default: only a *whole* clean-JSON string parses | yes | yes | = |
| `type: bash` falls back to aggressive extraction when the whole string is not JSON | yes | yes | = |
| `parse_json: true` → aggressive extraction, 3 strategies in order (whole / fenced via `raw_decode` / first non-trivial `{`\|`[` in document order) | yes | yes | = |
| A trivial `{}`/`[]` never beats a real structure later in the text | yes | yes | = |
| Nothing parses → the original string is returned (never `None`) | yes | yes | = |
| `parse_json: true` appends the JSON-output rider to an agent instruction | yes | yes | = (rider text copied verbatim) |
| A spawn envelope `{"output": ...}` is unwrapped | yes | yes | = |

### Conditions

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| Grammar, precedence, numeric-first comparison, truthiness | **vendored verbatim** | source | = by construction |
| Top-level step: `evaluate_condition(condition, context)` (evaluator substitutes) | yes | yes | = |
| Loop sub-step / `while_condition` / `break_when`: `substitute_condition_variables` first, then evaluate | yes | yes | = (asymmetry reproduced, not tidied -- but it now resolves values *identically* to the top level, see below) |
| A `{{var}}` in condition text renders as a **literal**: string quoted+escaped, bool `true`/`false`, `None` `null`, number bare | yes | yes | = (`recipes-kft`) |
| A `{{var}}` the author already quoted (`'{{var}}' == ''`) is spliced in raw, not re-quoted | yes | yes | = (`recipes-kft`) |
| A `{{var}}` absent from the context is an error; one whose *value* is `None` is not | yes | yes | = (`recipes-kft`) |
| False condition → step skipped, id appended to `_skipped_steps`, skip announced | yes | yes | = |
| Expression error → the *step* fails, naming the step and "condition error" | yes | yes | = |

### `foreach`

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| Loop variable name from `as:`, defaulting to `item` | yes | yes | = |
| Loop variable deleted after each iteration (does not leak) | yes | yes | = |
| Non-list value → failure naming the actual type | yes | yes | = |
| Empty list → body skipped, id appended to `_skipped_steps`, **`collect:` still set to `[]`** | yes | yes | = |
| `len(items) > max_iterations` → failure | yes | yes | = |
| `collect:` gets every iteration result; else `output:` gets the last | yes | yes | = |
| Multi-step body under `steps:` — iteration result is the last sub-step's | yes | yes | = |
| Nested loops inside a body route back through the loop executor | yes | yes | = |
| `on_error: continue` keeps an index-aligned slot for a failed iteration | yes | yes | = |
| `on_error: skip_remaining` propagates out of the loop | yes | yes | = |
| Otherwise a failed iteration fails the step, naming the index | yes | yes | = |
| `parallel: true` unbounded, `parallel: N` bounded by a semaphore | yes | yes | = |
| Parallel iterations get a private context copy; results stay **input-ordered** | yes | yes | = |
| Parallel gathers with `return_exceptions=True` (no orphaned tasks) then aggregates failures | yes | yes | = |
| Parallel agent loop pre-checks `max_total_steps` for the whole batch | yes | yes | = |
| `checkpoint_iterations:` mid-loop resume | yes | yes | = |
| A checkpoint lands at every iteration boundary, and is cleared when the step completes | yes | yes | = |
| An `on_error: continue` iteration counts as completed (its index-aligned slot is checkpointed) | yes | yes | = |
| An empty `foreach` list writes no checkpoint at all | yes | yes | = |
| `checkpoint_iterations:` **with `parallel:`** | supported, per completed item | validator **rejects the combination** | Δ3 |

### `while_condition` (convergence loops)

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| Condition substituted, then evaluated, before each iteration | yes | yes | = |
| `_loop_index` (0-based) and `_loop_iteration` (1-based) injected per iteration | yes | yes | = |
| Both removed from the context on every exit path | yes | yes | = |
| `update_context:` applied **after** the body | yes | yes | = |
| `break_when:` evaluated **after** `update_context` | yes | yes | = |
| A `break_when` expression error warns and continues (does not fail the run) | yes | yes | = |
| `max_while_iterations` bounds a condition that never falsifies | yes | yes | = |
| `collect:` gets every iteration; else `output:` gets the last | yes | yes | = |
| Single-step body also writes `output:` each iteration | yes | yes | = |

### `type: recipe` (sub-recipes)

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| `recipe:` path substituted, then resolved **relative to the parent recipe's directory** | yes | yes | = |
| Missing sub-recipe → failure naming the resolved path | yes | yes | = |
| `context:` block is the sub-recipe's *entire* input (isolation) | yes | yes | = |
| `context:` values substituted recursively, preserving native types | yes | yes | = |
| Only keys the sub-recipe **added** come back (input keys and engine-internal keys excluded) | yes | yes | = |
| `recursion.max_depth` / `max_total_steps`, with per-step override | yes | yes | = |
| A `schema_version: 2` sub-recipe resolves from **its own** declared closure | yes (fresh plan + session) | yes (closed-world engine) | = in policy |
| A legacy sub-recipe runs inside the parent's world | yes | yes | = |
| `@mention` sub-recipe paths | refused by name | resolved via the host's `mention_resolver` | Δ4 |
| A sub-recipe pausing at an approval gate mirrors onto the parent run | refused by name | yes | Δ5 |

### Timeouts, retry and `on_error` (agent steps)

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| Numeric-string `timeout:` normalised at parse time | yes | yes | = |
| Template `timeout:` resolved immediately before use, never inside `wait_for` | yes | yes | = |
| Unresolvable / non-numeric / non-positive template → failure naming step **and** field | yes | yes | = (messages ported verbatim) |
| Timeout resolved **before** the spawn, so a bad template burns no invocation | yes | yes | = |
| Agent timeout message names the agent | yes | yes | = |
| `retry.max_attempts` / `backoff` (`exponential`\|linear) / `initial_delay` / `max_delay` | yes | yes | = |
| `on_error: fail` re-raises after the last attempt | yes | yes | = |
| `on_error: continue` returns `None` as the step result | yes | yes | = |
| `on_error: skip_remaining` stops the run, still reporting success | yes | yes | = |
| Rate limiting (`rate_limiting:` block, 429 backoff) | **not implemented** | yes | Δ6 |

### Staged recipes and approval gates

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| Stages run in order; `stage` metadata in context | yes | yes | = |
| `approval.when: after_stage` (default) gates after the stage's last step | yes | yes | = |
| `approval.when: before_stage` gates before the stage's first step | yes | yes | = |
| A `before_stage` gate does not re-park on the resume that came through it | yes | yes | = |
| Approval prompt is template-substituted before it is shown | yes | yes | = |
| A recorded **approve** injects `_approval_message` into the context | yes | yes | = |
| A recorded **deny** fails the run naming the stage | yes | yes | = |
| Pause persists position + context for a *later process* to resume | `<run dir>/engine-state.json` | session state via `SessionManager` | = in effect, Δ7 in mechanism |
| A gate with no way to answer it **pauses** (never auto-passes) | yes | yes | = |
| `approval.timeout` / `approval.default` auto-decision | **not implemented** | yes | Δ8 |
| Host answers a gate inline via a callback | `approval_callback` port | Amplifier approval UI | = in effect |

### Cancellation and run lifecycle

| Behaviour | Library | Legacy | Status |
|---|---|---|---|
| Cancellation checked before every step and every loop iteration | `cancellation` port | session cancellation file | = in effect |
| A cancelled run reports `CANCELLED`, not failure | yes | yes | = |
| An errored step is never listed in `completed_steps` | yes | yes | = |
| Failure is surfaced at the top level of the result, never only in a summary | yes | n/a (raises) | = in effect |
| Per-step audit log (`steps.jsonl`) | **not implemented** | yes | Δ9 |
| Where an agent step's model provider comes from | the recipe's own declared closure | the calling Amplifier session | Δ10 |
| An agent step reached with no provider configured | refused loudly at spawn, naming the remedy | n/a (the session always has one) | Δ10 |
| A nested `steps:` body without `foreach:`/`while_condition:` | refused by name | body silently ignored, step runs as an agent step | Δ11 |
| A recipe declaring both `steps:` and `stages:` | refused at parse | refused at parse | = (Δ12, listed for history) |

---

## Deliberate deltas

Each is a difference the library makes **on purpose**. Nothing here is an
accident, and nothing here is silent.

### Δ1 — A step with no `id:` gets a positional id instead of being rejected

*Legacy:* the validator requires `id:` on every step.
*Library:* falls back to `step-<n>`, numbered over the flattened step list.

**Why:** the library's `resume` matches recorded completed steps **by id**, and
must be able to name a step that the recipe did not. The fallback is the same
derivation `execution._step_ids` has always used, so a resumed run and the run
that recorded it cannot disagree about which step is which. Recipes still
*should* declare ids; the validator still says so.

### Δ2 — Scratch directory is scoped to the run directory, not a session

*Legacy:* `AMPLIFIER_RECIPE_SCRATCH_DIR` points inside the Amplifier session
directory.
*Library:* points at `<state dir>/<run id>/scratch`, and is **absent** when the
run has no state directory.

**Why:** the library has no session. The requirement the variable exists to
serve — "somewhere run-scoped that two concurrent runs of the same recipe over
the same repo cannot share" — is met either way. Recipes already fall back to
the system temp dir when it is unset (that is how they behave on an older
engine), so absence is a supported state rather than a break.

### Δ3 — `checkpoint_iterations:` with `parallel:` is supported here and refused there

Mid-loop resume itself is now **=**: both engines write a per-iteration
checkpoint and skip the completed iterations on resume. What still differs is
one combination.

*Legacy:* `Step.validate` **rejects** `checkpoint_iterations` together with
`parallel` — "parallel is all-or-nothing"
(`modules/tool-recipes/amplifier_module_tool_recipes/models.py`).
*Library:* honours both together, checkpointing **per completed item** and
re-running only the items that never finished.

**Why:** all-or-nothing is a property of the legacy checkpoint's *shape*, not
of parallelism. Legacy records a `completed_iterations` **prefix count**, and a
prefix cannot describe a batch that finished out of order — so refusing the
combination was the correct call for that shape. The library records the
completed **indices**, which can. Refusing a combination the state shape
handles correctly would be an invented limit.

**Consequence to know:** a recipe using both runs on the standalone runner and
is rejected by the in-session validator. Sequential `checkpoint_iterations:`
behaves identically on both.

#### The state shape

Legacy writes `foreach_progress` into session state; the library writes it into
the `ResumeState` it already persists at `<run dir>/engine-state.json`
(Δ7 — the library has no session). Same three facts, one different field:

| Legacy `foreach_progress` | Library `engine_state.foreach_progress` |
|---|---|
| `step_id` | `step_id` — same meaning: progress is applied only to the step that recorded it, and only once |
| `total_items` | `total_items` — a mismatch on resume is reported (`foreach:items-changed`), never silently trusted |
| `completed_iterations` (prefix count) | `completed_iterations` — kept, and still the contiguous prefix, but **advisory**: `completed_indices` is what resume reads |
| `collected_results` (list, only when `collect:` is set) | `results` — **index-keyed** (`{"0": ..., "3": ...}`), and written whether or not `collect:` is set |
| — | `completed_indices` — every finished index; non-contiguous after a parallel loop |

The context at each iteration boundary rides along in the same write: the
checkpoint is a whole `ResumeState`, so `context`, `outputs` and
`completed_steps` are captured with it and get the existing oversized-value
trimming for free.

Two shape decisions worth naming:

* **Index-keyed, not positional.** A parallel loop finishes out of order; a
  positional list could not say which slot a value belongs in without
  inventing one. Restored results are slotted back by index, so
  `collect:` stays input-ordered exactly as an uninterrupted run would leave it.
* **Results are persisted even without `collect:`.** Legacy omits them there
  to save bytes, which is safe only because it resumes a contiguous prefix.
  It costs the same O(N²) write amplification legacy warns about at 10 MB —
  the library warns at the same threshold, on the event sink
  (`foreach:checkpoint-large`) rather than the log, and once per step rather
  than on every iteration past it.

Only the **top-level** engine writes this file. A sub-recipe shares the
parent's store, so a checkpointing loop inside one would otherwise overwrite
the parent's position with the child's.

### Δ4 — `@mention` sub-recipe paths are refused by name

*Legacy:* resolves `@recipes:examples/thing.yaml` through the host's
`mention_resolver` capability.
*Library:* raises `UnsupportedStepError` naming the path and the reason.

**Why:** `@mention` resolution is Amplifier bundle machinery; the standalone
runner has no host to ask. Guessing a filesystem path from a bundle mention
would resolve to the wrong file about as often as the right one. A path
relative to the parent recipe works in both engines.

### Δ5 — A sub-recipe's approval gate does not mirror onto the parent run

*Legacy:* mirrors the child's pending approval onto the parent session under a
compound stage name (`parent-stage/child-stage`) and re-raises, so both appear
paused.
*Library:* fails the parent step with an error naming the child recipe and its
stage.

**Why:** the mirroring depends on two live session records and a parent/child
session graph the library does not have. Rather than half-implement it — which
would produce a run that looks resumable and is not — the library refuses and
names the remedy (run the staged sub-recipe directly, or lift its gate into the
parent). Single-level staged recipes with gates are fully supported.

### Δ6 — No rate limiting

*Legacy:* a `rate_limiting:` block bounds concurrent LLM calls, paces them, and
backs off on 429.
*Library:* the field is ignored.

**Why:** rate limiting is a property of the *provider layer*, which in the
library is behind the `provider_access` port and owned by the host. A second
limiter inside the engine would fight the host's own. This is a real gap for a
host that has no limiter of its own; it is recorded rather than papered over.

### Δ7 — Run state lives in a file the run owns, not in session state

*Legacy:* `SessionManager` state under the Amplifier session directory.
*Library:* `<run dir>/engine-state.json`, written after every terminal or
paused outcome, plus an `approvals` ledger the `approve`/`deny` commands write.

**Why:** there is no session to put it in. The mechanism differs; the guarantee
does not — pause → approve → resume works across three separate OS processes,
which is exactly what the legacy flow provides in-session.

### Δ8 — `approval.timeout` / `approval.default` are not auto-decided

*Legacy:* an unanswered gate can auto-approve or auto-deny after `timeout`
seconds.
*Library:* the fields are parsed and ignored; the run simply stays paused until
a verdict is recorded.

**Why:** auto-deciding requires a process that outlives the paused run and
watches a clock. The standalone runner exits at the pause — there is nothing
left running to fire the timer. A timer that silently never fires is worse than
no timer, so it is declared absent. A host that embeds the library and *does*
stay alive can implement the policy on its own `approval_callback`.

### Δ9 — No per-step audit log

*Legacy:* writes `steps.jsonl` — one record per step attempt, with resolved
prompt, provider/model reached, timing and status.
*Library:* emits the same moments on the `event_sink` port
(`step:start` / `step:complete` / `step:failed` / `step:skipped` /
`loop:iteration` / `approval:pending`), and leaves persistence to the host.

**Why:** the event schema is explicitly unstable in `recipe-runner-lib.v1`
("streaming/event schema stabilization" is Backlogged), and writing a second,
differently-shaped log file would freeze a shape the contract has not agreed
yet. A host that wants a file writes one from the events.

### Δ10 — The recipe's provider wins; the host's port is the fallback, not the default

*Legacy:* an agent step spawns through the calling Amplifier session, which
brings the user's configured providers with it (`~/.amplifier/settings.yaml`).
*Library:* **layered**, and the recipe wins:

| # | Composed closure | Host `provider_access` port | What happens | `provider_source` |
|---|---|---|---|---|
| 1 | declares providers | anything | the closure's providers are used, **pinned** | `recipe-closure` |
| 2 | declares none | offers a mountable `ProviderSpec` | the host's providers are bridged into the composed session | `host-port` |
| 3 | declares none | offers nothing mountable | the run **refuses**, naming both remedies | `none` |

**Why layered, and why the recipe wins.** A recipe's closed world is its
**agents, tools, context and hooks** — those must come from the declared
closure and nowhere else, because a recipe that silently rebinds to whatever
agents the caller happens to have is the exact failure schema v2 exists to end
(`recipe-dependency-manifest.v1` Core 3, Core 4). A **model provider is not one
of those**. It is an execution *resource*, like the workspace directory or the
approval callback — which is precisely why the contract already gives it a host
port of its own (`recipe-runner-lib.v1` Core 4, port 1). Refusing to use that
port would leave a named port that never did anything, and would force every
recipe to hard-code a model even where the operator is entitled to choose one.

But a recipe that *does* declare a provider has made a real statement — "this
recipe is written for this model" — and a pin a host could quietly override is
not a pin. So layer 1 is authoritative and the port is not even consulted when
it applies. That precedence is asserted, not assumed: the kit fixture
`good-provider-agnostic-recipe-runs-on-either-layer-and-refuses-on-neither`
fails if the port is read while a closure is pinned.

**What "mountable" means (`ProviderSpec`).** `ProviderHandle` stays opaque by
default; the runner now understands exactly **one** shape — `ProviderSpec`
(`module`, `source`, `config`, optional instance `id`), the same four keys a
bundle's own `providers:` entry and an Amplifier settings `config.providers`
entry already use. A handle that is *not* that shape is bridged nowhere and
recorded as uninterpretable. In particular a provider-*preference* chain
(`{provider: anthropic, model: claude-sonnet-5}`) names no module source, and
inventing one from the nickname would be a guess — so it is refused rather than
guessed at. **Consequence, stated plainly:** the in-session adapter's
`CoordinatorProviderAccess` hands over preference chains, so an in-session v2
run does **not** bridge today; it still needs the recipe to declare a provider
(remedy 1). Making it bridge is a one-call change in
`modules/tool-recipes` — hand `ProviderSpec`s instead — which this lane did not
make.

**Model roles.** When the port is bridged, its **roles come with it**: every
role is resolved once at composition and each role's module is activated, so a
step's `model_role:` selects that role's provider at spawn. A role the host
does not serve is refused by name (`ModelRoleUnavailableError`) — never
silently downgraded onto another provider. With no `model_role:`, a single-role
host is unambiguous; several roles require a conventional name (`default`, then
`general`), and a host serving several with none of those refuses rather than
choosing which model to spend money on. Under a **pinned** closure there is no
role routing at all, so a step naming `model_role:` is refused too — running it
on the pinned provider would be the same silent downgrade.

**The refusal (case 3) is loud and fires at spawn.** `FoundationSpawnBackend`
raises `NoProviderError` naming the step, the agent, the roles the host *did*
offer, and **both** remedies. It fires when a step actually reaches for a model,
not at composition, so a recipe whose agent steps are all skipped by conditions
is not failed for a provider it never needed.

Measured on `recipes/repo-audit.yaml` during recipes-xov: with the original
manifest, the run exited 0, 23 steps ran, provenance was correct — and the one
agent step wrote the literal text `Error: No providers available` into the
audit report. Green, and worthless. That is what these three cases replace.

**Provenance.** Every agent step's record (the `agent:start` / `agent:complete`
events on the `event_sink` port — see Δ9) carries `provider_source`, the
provider instance, the model and the `model_role`; `RunResult.provider` carries
the same for the run, on **every** terminal status; and the run manifest
(`run-manifest.json`, and the `v2_provenance` an Amplifier session records)
gains a `provider` field. The manifest is written at preflight, before a
session exists, so the standalone CLI re-writes just that field once the run has
resolved it. `provider` is recorded, never **compared** on resume: the resolved
dependency graph is what Core 8 pins, and a host that has since rotated its
model must not be refused for that.

**Supplying the host's providers from the CLI.** `recipe-runner run
--host-providers` reads the host's own Amplifier settings
(`$AMPLIFIER_HOME/settings.yaml`, or `--host-settings PATH`) and serves its
`config.providers` on the port — a copy, not a translation, since the shapes
already match. `${VAR}` placeholders are expanded from the environment and an
unresolved one is reported, never passed through silently. `--provider-id ID`
(repeatable) narrows to named instances in the order given; otherwise the
file's own `config.priority` order is used and the head is what provenance
records. One role is served, named `default`, because a settings file
configures providers and not *routing* — synthesizing several roles from a
priority list would be routing this CLI cannot actually perform.

Without the flag the port names roles (`provider_roles`, default `general`)
without saying what serves them, which is exactly case 3: honest, and refused.

Loading a provider partial required a fix in `FoundationSessionFactory._load`:
a `#subdirectory=` that names a *file* resolves to the file's parent
**directory** in `local_path`, which is not a bundle. The declared
`subdirectory` is now consulted when it names a `.yaml`/`.yml`; directory-style
partials are unaffected.

`recipes/repo-audit.yaml` keeps its pinned
`git+…/amplifier-foundation@v2.1.2#subdirectory=providers/anthropic-sonnet.yaml`
(`kind: behavior`) dependency. The pin is **not** made redundant by the host
port: it is what makes the recipe reproducible on a host that supplies nothing,
and case 1 is what guarantees it still wins where a host does.

### Δ11 — A nested `steps:` body without `foreach:`/`while_condition:` is refused

*Legacy:* silently ignores the nested body and runs the step as a plain agent
step.
*Library:* raises `UnsupportedStepError` naming the step.

**Why:** this is the only delta where the library is *stricter than* the legacy
engine, and it is the one that matters most. Discarding declared work while
reporting success is precisely the fabricated success `recipe-runner-lib.v1`
Core 8 forbids. A recipe that hits this has a real bug — it wrote a loop body
and forgot the loop — and the legacy engine's silence is how that bug survives.

### Δ12 — A recipe declaring both `steps:` and `stages:` is refused at parse

*Legacy:* `Recipe.from_yaml` raises the same refusal.
*Library:* raises `ExecutionError` with the same meaning.

Listed for completeness: the two agree. It appears here only because the
library's earlier executor **concatenated** both lists, which would have run a
staged recipe's flat steps without any stage's gates.

---

## Not applicable to the library, by design

These legacy behaviours have no library counterpart because they are properties
of the Amplifier session, not of the step vocabulary:

- `spawn_mode: subprocess` (process isolation for a spawned agent)
- `agent_config:` overlays and `provider_preferences` instance pinning — the
  library carries the fields through to the host's spawn seam and lets the host
  decide
- Amplifier's `model_role_resolver` **routing matrix** — the library does not
  compute a role's preference chain. It does now *honour* a role the host's
  `provider_access` port serves, and refuses one it does not (Δ10); what a role
  means is still the host's to say
- progress/`recipe:*` event emission into Amplifier's hook system
- session adoption (`attach_session_id`) and parent/child session graphs

A step declaring one of these still runs; the field is carried on `StepSpec`
and available to whatever backend the host injects.
