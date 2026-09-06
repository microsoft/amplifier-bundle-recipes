# Recipe Schema Reference

**Complete YAML specification for Amplifier recipes**

This document defines the complete schema for recipe YAML files. Every field, constraint, and behavior is documented here.

## Overview

Recipes are declarative YAML specifications that define multi-step agent workflows. The tool-recipes module parses and executes these specifications.

**Schema Version:** 1.3.0

## Quick start: the v2 header

**Any recipe with an `agent:` step should carry this header.** It is the default
for new recipes, not an opt-in.

```yaml
schema_version: 2

dependencies:
  - source: "git+https://github.com/microsoft/amplifier-foundation@v2.1.2"
    kind: bundle
    required_agents:
      - "foundation:zen-architect"

name: my-recipe            # everything below is unchanged from v1
description: "..."
version: "1.0.0"
steps: [...]
```

Without it, a step's `agent:` resolves from the **calling session's** agent map:
the recipe runs in the bundle it was authored in and nowhere else. With it,
agents resolve from the recipe's own declared closure, so the same file runs
from any bundle. List every namespaced agent under the `required_agents` of the
dependency whose bundle ships it (including the recipe's own bundle), and pin
each `source` to a tag or SHA — never a branch.

A recipe with **no** `schema_version` is a *legacy recipe*: still executable
through the Amplifier `recipes` tool, still caller-bound, and rejected outright
by the standalone `recipe-runner` CLI.

Full reference: [Schema v2 — Dependency Manifests](#schema-v2--dependency-manifests).
Everything between here and that section describes the v1 fields, which v2
recipes use unchanged.

## Top-Level Structure

```yaml
name: string                    # Required
description: string             # Required
version: string                 # Required (semver format)
author: string                  # Optional
created: ISO8601 datetime       # Optional
updated: ISO8601 datetime       # Optional
tags: list[string]             # Optional
context: dict                   # Optional - Initial context variables
recursion: RecursionConfig      # Optional - Recursion protection limits
rate_limiting: RateLimitingConfig # Optional - Global LLM rate limiting
steps: list[Step]              # Required - At least one step
```

### Top-Level Fields

#### `name` (required)

**Type:** string
**Constraints:**
- Must be unique within your recipe library
- Alphanumeric, hyphens, underscores only
- Max length: 100 characters

**Purpose:** Identifies the recipe in logs and UI.

**Examples:**
```yaml
name: "code-review-flow"
name: "dependency-upgrade"
name: "test-generation-pipeline"
```

#### `description` (required)

**Type:** string
**Constraints:**
- Max length: 500 characters
- Should be a single paragraph

**Purpose:** Human-readable explanation of what the recipe does.

**Examples:**
```yaml
description: "Multi-stage code review with analysis, feedback, and validation"
description: "Systematic dependency upgrade with audit, planning, and validation"
```

#### `version` (required)

**Type:** string (semantic versioning)
**Constraints:**
- Must follow semver: `MAJOR.MINOR.PATCH` (no pre-release tags)
- Example: `1.0.0`, `2.3.1`, `0.1.0`

**Purpose:** Track recipe evolution and compatibility.

**Breaking change semantics:**
- MAJOR: Incompatible changes (different inputs/outputs)
- MINOR: Backward-compatible additions (new optional steps)
- PATCH: Bug fixes, documentation updates

**Examples:**
```yaml
version: "1.0.0"
version: "2.1.3"
version: "0.5.0"
```

#### `author` (optional)

**Type:** string
**Purpose:** Credit recipe creator.

**Examples:**
```yaml
author: "Jane Doe <jane@example.com>"
author: "DevOps Team"
```

#### `created` (optional)

**Type:** ISO8601 datetime string
**Purpose:** Track recipe creation date.

**Examples:**
```yaml
created: "2025-11-18T14:30:00Z"
created: "2025-11-18T14:30:00-08:00"
```

#### `updated` (optional)

**Type:** ISO8601 datetime string
**Purpose:** Track last modification date.

**Examples:**
```yaml
updated: "2025-11-20T09:15:00Z"
```

#### `tags` (optional)

**Type:** list of strings
**Purpose:** Categorize recipes for discovery.

**Examples:**
```yaml
tags: ["code-quality", "analysis", "python"]
tags: ["security", "audit", "dependencies"]
tags: ["documentation", "improvement"]
```

#### `context` (optional)

**Type:** dictionary (string keys, any values)
**Purpose:** Define initial context variables available to all steps.

**Examples:**
```yaml
context:
  project_name: "my-app"
  target_version: "3.11"
  severity_threshold: "high"
```

**Usage in steps:**
```yaml
steps:
  - id: "analyze"
    prompt: "Analyze {{project_name}} for Python {{target_version}} compatibility"
```

##### The declarative form (`type:` / `required:` / `default:`)

An entry's value may instead be a **declaration** — the shape every other
tool's input schema uses:

```yaml
context:
  topic:
    type: string
    required: true
    description: "The question to investigate"
  continue_from:
    type: string
    default: ""
    description: "Path to a previously verified document"
  depth:
    type: string
    default: "standard"
    enum: ["quick", "standard", "deep"]
```

**Recognition.** A value is a declaration when it is a **non-empty mapping
whose every key is drawn from `type`, `required`, `default`, `description`,
`enum`**. A mapping carrying any other key — or an empty mapping `{}` — is an
ordinary literal value and is bound unchanged, so a context entry that really
is a dict keeps working:

```yaml
context:
  # A value, not a declaration: `themes` is not a declaration key.
  commit_analysis: { themes: [], total_analyzed: 0 }
```

To bind a literal mapping that happens to use only declaration keys, nest it
or add another key.

**What each field does.**

| Field | Effect |
|-------|--------|
| `default` | The value bound when the caller supplies none. |
| `required` | `true` means the caller must supply a value; the run is refused by name if it does not. |
| `type` | Documentation only — nothing coerces the value. One of `string`, `number`, `integer`, `boolean`, `array`, `object`, `any`. |
| `description` | Documentation only. |
| `enum` | The permitted values, as a non-empty list. Checked against `default` at validation time. |

**Resolution, in order:**

1. The caller supplied a value → that value wins (exactly as for a plain entry).
2. `default:` is present → the default is bound.
3. `required: true` and nothing supplied → **the run is refused, naming the
   variable**, before any step executes.
4. Neither — a declaration that is documentation only → the variable is
   **not bound at all**. Referencing it then fails loudly with the usual
   "variable is not defined" error rather than substituting anything.

**The declaration mapping is never bound as the value.** That was the old
behaviour and it failed silently: `{{continue_from}}` substituted
`{'type': 'string', 'default': '', 'description': '...'}` into prompts as noise
and into conditions as a hard failure
(`Invalid expression: Unexpected character '{' at position 0`).

**Malformed declarations are reported by variable name** at validation time
(`recipes operation=validate`, and `recipe-runner validate` for
`schema_version: 2`) — a non-boolean `required`, an unknown `type`, a
non-string `description`, an `enum` that is not a non-empty list, or a
`default` outside its own `enum`.

`required: true` beside a `default:` is contradictory but harmless: the default
binds, so the variable is never missing. It is a **warning**, not an error.

#### `recursion` (optional)

**Type:** RecursionConfig object
**Purpose:** Configure recursion protection limits for recipe composition.

**Structure:**
```yaml
recursion:
  max_depth: integer      # Default: 5, range: 1-20
  max_total_steps: integer # Default: 100, range: 1-1000
```

**Fields:**
- `max_depth`: Maximum nesting depth for recipe-calling-recipe chains. Prevents infinite recursion.
- `max_total_steps`: Maximum total steps across all nested recipe executions. Prevents runaway workflows.

**Examples:**
```yaml
# Allow deeper nesting for complex orchestration
recursion:
  max_depth: 10
  max_total_steps: 200

# Strict limits for controlled workflows
recursion:
  max_depth: 3
  max_total_steps: 50
```

**Behavior:**
- Limits apply to entire recipe execution tree
- Exceeding limits raises error immediately
- Child recipes inherit limits unless overridden at step level

#### `rate_limiting` (optional)

**Type:** RateLimitingConfig object
**Purpose:** Configure global rate limiting for LLM calls across the entire recipe tree.

**Structure:**
```yaml
rate_limiting:
  max_concurrent_llm: integer   # Max concurrent LLM calls (default: unlimited)
  min_delay_ms: integer         # Minimum ms between call completions (default: 0)
  backoff:                      # Auto-slowdown on rate limit errors
    enabled: boolean            # Enable adaptive backoff (default: true)
    initial_delay_ms: integer   # Starting delay after first 429 (default: 1000)
    max_delay_ms: integer       # Maximum delay cap (default: 60000)
    multiplier: float           # Exponential multiplier (default: 2.0)
    reset_after_success: integer # Successes before reset (default: 3)
```

**Fields:**
- `max_concurrent_llm`: Global semaphore limiting concurrent LLM calls across entire recipe tree. Prevents overwhelming API providers.
- `min_delay_ms`: Minimum delay between LLM call completions. Provides pacing to avoid bursts.
- `backoff`: Adaptive backoff configuration for handling 429 errors gracefully.

**Examples:**
```yaml
# Conservative rate limiting for shared environments
rate_limiting:
  max_concurrent_llm: 3
  min_delay_ms: 500
  backoff:
    enabled: true
    initial_delay_ms: 2000

# Moderate rate limiting for typical usage
rate_limiting:
  max_concurrent_llm: 5
  min_delay_ms: 200

# Aggressive parallelism (dedicated API access)
rate_limiting:
  max_concurrent_llm: 10
```

**Behavior:**
- Rate limiter is created at root recipe level
- Sub-recipes **inherit** parent's rate limiter (cannot override)
- Applies to all `type: "agent"` steps (not bash or recipe steps themselves)
- Works in conjunction with step-level `parallel` setting

**Interaction with bounded parallelism:**
```yaml
rate_limiting:
  max_concurrent_llm: 5       # Global: max 5 LLM calls at once

steps:
  - id: "analyze-repos"
    foreach: "{{repos}}"
    parallel: 10              # Step: up to 10 iterations run concurrently
    type: "recipe"            # But LLM calls within them capped at 5 globally
    recipe: "repo-analysis.yaml"
```

This separation allows high concurrency for non-LLM work (bash steps, file I/O) while respecting LLM rate limits.

#### `steps` (required for flat mode)

**Type:** list of Step objects
**Constraints:**
- At least one step required
- Step IDs must be unique within recipe
- Steps execute in order

**Purpose:** Define the workflow in flat mode (sequential steps without approval gates).

**Note:** Recipes must use EITHER `steps` (flat mode) OR `stages` (staged mode with approval gates), not both.

#### `stages` (required for staged mode)

**Type:** list of Stage objects
**Constraints:**
- At least one stage required
- Stage names must be unique within recipe
- Stages execute in order
- Each stage can have an approval gate

**Purpose:** Define the workflow in staged mode with optional approval gates between stages.

**Note:** Recipes must use EITHER `steps` (flat mode) OR `stages` (staged mode with approval gates), not both.

---

## Recipe Modes: Flat vs Staged

Recipes support two execution modes. You must choose one mode per recipe - they cannot be mixed.

### Flat Mode (Sequential Steps)

**When to use:**
- Simple workflows without human checkpoints
- Automated processes that should run without interruption
- Development and testing scenarios

**Structure:**
```yaml
name: "simple-workflow"
version: "1.0.0"
description: "Sequential processing without approval gates"

steps:
  - id: "analyze"
    agent: "foundation:analyzer"
    prompt: "Analyze {{input}}"
    output: "analysis"
  
  - id: "process"
    agent: "foundation:processor"
    prompt: "Process {{analysis}}"
    output: "result"
```

**Characteristics:**
- Steps execute sequentially
- No human intervention required
- Fails fast on errors
- Resume from last successful step on interruption

### Staged Mode (Multi-Stage with Approval Gates)

**When to use:**
- High-stakes operations requiring human oversight
- Workflows where you want to review results before continuing
- Processes with distinct phases that need sign-off
- Situations where you might want to stop execution between phases

**Structure:**
```yaml
name: "staged-workflow"
version: "2.0.0"
description: "Multi-stage process with approval gates"

stages:
  - name: "planning"
    steps:
      - id: "analyze"
        agent: "foundation:analyzer"
        prompt: "Analyze {{input}}"
        output: "analysis"
    approval:
      required: true
      prompt: "Review analysis before proceeding to execution?"
      timeout: 3600  # 1 hour
      default: "deny"
  
  - name: "execution"
    steps:
      - id: "execute"
        agent: "foundation:executor"
        prompt: "Execute based on {{analysis}}"
        output: "result"
```

**Characteristics:**
- Stages execute sequentially
- Optional approval gates between stages
- Execution pauses at approval gates
- Resume after approval/denial via separate commands
- All steps within a stage execute together

### Approval Gates

Approval gates provide human-in-loop checkpoints between stages.

**Configuration:**
```yaml
approval:
  required: boolean       # Whether approval is needed (default: false)
  prompt: string         # Message shown to user
  timeout: integer       # Seconds to wait (0 = wait forever)
  default: string        # "approve" or "deny" on timeout (default: "deny")
  when: string           # "after_stage" (default) or "before_stage"
```

**Where the gate sits (`when`):**

| `when` | Pauses | The question it can answer | Denying it |
|---|---|---|---|
| `after_stage` (default) | after this stage's LAST step | "You have seen the output — may we go on?" | stops the FOLLOWING stages; this stage's work is already done |
| `before_stage` | before this stage's FIRST step | "May we do this at all?" | this stage never runs |

```yaml
# Review gate: fires once `audit` has run. {{findings}} exists.
approval: { required: true, prompt: "Reviewed {{findings}}. Continue?" }
```

```yaml
# Authorisation gate: fires before `apply-migration` runs. Denying stops it.
approval: { required: true, when: "before_stage", prompt: "Apply to prod?" }
```

Put the gate `before_stage` whenever the answer must be able to *prevent* the
stage. A prompt on a `before_stage` gate can only render outputs of EARLIER
stages — this stage has produced none yet.

**Workflow:**

1. **The gate is reached** → Recipe pauses at the approval gate
   (after the stage's steps by default; before its first step with
   `when: "before_stage"`)
2. **Tool returns status:** `paused_for_approval` with session_id and stage_name
3. **User reviews** → Decides to approve or deny
4. **User approves/denies:**
   ```bash
   # Approve and continue
   amplifier run "approve recipe session <session-id> stage <stage-name>"
   
   # Deny and stop
   amplifier run "deny recipe session <session-id> stage <stage-name>"
   ```
5. **Resume execution:**
   ```bash
   amplifier run "resume recipe session <session-id>"
   ```

**One session id, the whole way through.** The `session_id` in the
`paused_for_approval` result is the id `approve`, `deny`, `approvals` and
`resume` all accept and all report back. A `schema_version: 2` run executes on
a step engine in a session of its own, and its gate physically lives there --
so `approve`/`deny` additionally report `gate_session_id`. That is where the
approval was written, not a second id to resume with; `resume` takes the run's
own id (recipes-3f6).

**List pending approvals:**
```bash
amplifier run "list pending approvals"
```

**Example with timeout:**
```yaml
approval:
  required: true
  prompt: "Review security audit results. Critical findings require immediate action."
  timeout: 7200  # 2 hours
  default: "deny"  # Auto-deny if no response
```

### Choosing Between Modes

| Consideration | Flat Mode | Staged Mode |
|--------------|-----------|-------------|
| Human oversight needed? | No | Yes |
| Can pause between phases? | No (only on error) | Yes (approval gates) |
| Complexity | Simple | More complex |
| Use case | Automation, development | Production, high-stakes ops |
| Resume behavior | Resume from failed step | Resume after approval |

**Migration path:**
- Start with flat mode for simplicity
- Upgrade to staged mode when human oversight becomes necessary
- Version bump: Changing from flat to staged is a breaking change (major version)

---

## Stage Object

A Stage groups multiple steps together with an optional approval gate. Stages are only used in staged mode recipes.

```yaml
- name: string                  # Required - Unique stage name
  steps: list[Step]            # Required - At least one step
  approval: ApprovalConfig     # Optional - Approval gate configuration
  description: string          # Optional - Documentation only; recorded, never executed
```

Those four are the **complete** list. Any other stage key is **rejected at
load**, by name, with the valid keys and a remedy — see
[Rejected stage keys](#rejected-stage-keys).

### Stage Fields

#### `name` (required)

**Type:** string
**Constraints:**
- Must be unique within recipe
- Alphanumeric with hyphens, underscores, and spaces allowed
- Max length: 100 characters

**Purpose:** Identifies the stage in logs, UI, and approval operations.

**Examples:**
```yaml
- name: "planning"
- name: "security-review"
- name: "Phase 1: Critical Fixes"
```

#### `steps` (required)

**Type:** list of Step objects
**Constraints:**
- At least one step required
- Step IDs must be unique across ALL stages in recipe
- Steps within stage execute sequentially

**Purpose:** Define the work performed in this stage.

#### `description` (optional)

**Type:** string

**Purpose:** Documentation. Both parsers record it on the parsed stage
(`Stage.description` / `StageSpec.description`) and nothing executes it.

It is a real key rather than a tolerated one because a stage key that no
parser records is dropped in silence, and this schema no longer does that —
see [Rejected stage keys](#rejected-stage-keys).

#### `approval` (optional)

**Type:** ApprovalConfig object

**Purpose:** Define an approval gate on this stage. `when` decides which side
of the stage it sits on.

> **Read this before trusting the indentation.** By default `approval` under
> stage N gates the transition from N to N+1. It does **not** gate stage N's
> own steps — those have already run, and their outputs are already in
> context, by the time the prompt appears. To gate the work itself, say
> `when: "before_stage"`.

> **`approval:` is the only gate a stage has.** The flat stage keys
> `approval_required:`, `approval_message:` and `auto_approve_if:` are
> **REJECTED** at load, by name, with the remedy — by the legacy loader
> (`Recipe.from_yaml`) and by the schema-2 manifest parser alike. They are not
> aliases. They used to be dropped in silence, so a stage that read as a human
> checkpoint ran straight through it without ever prompting. See
> [Rejected stage keys](#rejected-stage-keys) below.

**Structure:**
```yaml
approval:
  required: boolean       # Default: false
  prompt: string         # Required if required=true
  timeout: integer       # Seconds, 0=forever (default: 0)
  default: string        # "approve" or "deny" (default: "deny")
  when: string           # "after_stage" (default) or "before_stage"
```

**Behavior:**
- If `required: false` or omitted, stage completes without pausing
- If `required: true` and `when` is `"after_stage"` (the default), every step
  in the stage runs, the stage is recorded in `completed_stages`, and only
  then does execution pause. Denying stops the stages that follow
- If `required: true` and `when` is `"before_stage"`, execution pauses before
  the stage's first step. At that pause the stage's steps are absent from
  `completed_steps` and the stage itself is absent from `completed_stages`.
  Denying means the stage never runs
- Either way, the pause, `approve`/`deny`, and `resume` work identically
- User must explicitly approve or deny to continue
- On timeout, applies `default` action
- Omitting `when` behaves exactly as it did before the field existed

**Example — a review gate (default):**
```yaml
- name: "analysis"
  steps:
    - id: "audit"
      agent: "foundation:auditor"
      prompt: "Audit security"
      output: "findings"
  approval:
    required: true
    prompt: |
      Security audit complete. Review findings before proceeding:
      {{findings}}

      Approve to continue with fixes.
    timeout: 3600
    default: "deny"
```

**Example — an authorisation gate:**
```yaml
- name: "apply-migration"
  approval:
    required: true
    when: "before_stage"
    prompt: |
      Plan: {{migration_plan}}

      Approve to APPLY this migration to production.
    default: "deny"
  steps:
    - id: "run-migration"
      agent: "foundation:integration-specialist"
      prompt: "Apply {{migration_plan}}"
```

**See also:** [Approval Gates](#approval-gates) for complete workflow details.

### Rejected stage keys

A Stage has exactly four keys: `name`, `steps`, `approval`, `description`.
**Every other stage key is rejected at load**, by name, with the valid keys
listed and a remedy — a key no parser reads is not inert, it is a declaration
that never happens.

One of them reads as behaviour and gets a remedy of its own:

| Rejected stage key | Status | Remedy |
|---|---|---|
| `condition` | **Rejected** — a stage has no condition | Put `condition:` on each of the stage's **steps**. Both engines evaluate a step-level condition and record the skip. |

**Why `condition:` is rejected and not honoured.** It was dropped in silence:
`Recipe._parse_stage` built its `Stage` by hand and `parse_program` read only
`name`/`steps`/`approval`, so a stage declaring a condition ran
unconditionally. Three stages of
`examples/context-intelligence/verification/adversarial-verification.yaml`
declared `condition: "{{continue_from}} == ''"` and documented themselves as
"SKIPPED when continue_from is provided" — and ran every time, re-running the
whole investigation in continuation mode. Honouring it would mean a second,
stage-shaped skip path threaded through approval gates, stage state, resume
and `steps.jsonl` in **both** engines; the remedy costs an author one line per
step and rides the step-level condition both engines already evaluate. That
example now carries the condition on its steps.

**What rejection looks like:**

```yaml
stages:
  - name: pre_check
    condition: "{{continue_from}} == ''"   # ← rejected
```

```
Stage 'pre_check' declares 'condition', which is not a stage key: it would be read
by nobody, so whatever it declares never happens. Valid stage keys are 'approval',
'description', 'name', 'steps'. 'condition': a stage has no condition -- put
'condition:' on each of the stage's steps, which both engines evaluate and record
as skipped
```

#### Flat approval keys

These three read like an approval gate, are **not** stage keys, and are
**rejected at load** — named individually, with the remedy:

| Rejected stage key | Status | Remedy |
|---|---|---|
| `approval_required` | **Rejected** — not an alias | `approval: { required: <bool>, prompt: <text> }` |
| `approval_message` | **Rejected** — not an alias | `approval: { required: true, prompt: <text> }` |
| `auto_approve_if` | **Rejected** — no equivalent exists | Delete it. This schema has no conditional auto-approval; gate the stage with `approval: { required: true, prompt: <text> }` if the checkpoint is real |

**Why rejected and not aliased.** Before this, all three were dropped in
silence: a stage that presented itself — often at length — as a human approval
gate had never gated anything, and the recipe ran through it to completion with
no prompt. Two of the three could have been aliased, but `auto_approve_if`
cannot: no engine here evaluates a conditional auto-approval, so "translating"
it would still drop the behaviour while now claiming to have honoured it. A
partial alias would leave the same silent hole in a different key, so all three
fail loudly instead.

**What rejection looks like:**

```yaml
stages:
  - name: final_review
    approval_required: true          # ← rejected
    approval_message: "Approve?"     # ← rejected
```

```
Stage 'final_review' declares 'approval_required', 'approval_message', which are
not stage keys: a stage's only approval gate is its 'approval:' block, so these
would be read by nobody and the declared human checkpoint would run ungated.
'approval_required': use 'approval: {required: <bool>, prompt: <text>}';
'approval_message': use 'approval: {required: true, prompt: <text>}'
```

Both engines refuse the same shape, so a recipe cannot be accepted by one and
refused by the other: the legacy loader (`Recipe.from_yaml`) and the schema-2
manifest parser (`parse_manifest`, which every `validate`, `plan` and `run` of a
`schema_version: 2` recipe passes through).

Scoping: only **stage mappings** are checked. `approval_required` as a
`context:` variable or a step field of the same name is untouched — see
`examples/context-intelligence/synthesis/action-executor.yaml`, which declares
exactly that.

---

## Step Object

Each step represents one unit of work in the workflow. Steps can be agent invocations (default), recipe compositions, or bash commands.

```yaml
- id: string                    # Required - Unique within recipe
  type: string                  # Optional - "agent" (default), "recipe", or "bash"

  # For agent steps (type: "agent"):
  agent: string                 # Required for agent steps - Agent name
  mode: string                  # Optional - Agent mode (if agent supports)
  prompt: string                # Required for agent steps - Prompt template
  provider: string              # Optional - Provider ID for this step (e.g., "anthropic", "openai")
  model: string                 # Optional - Model name or glob pattern (e.g., "claude-sonnet-4-5-*")

  # For recipe steps (type: "recipe"):
  recipe: string                # Required for recipe steps - Path to sub-recipe
  context: dict                 # Optional - Context to pass to sub-recipe

  # For bash steps (type: "bash"):
  command: string               # Required for bash steps - Shell command to execute
  cwd: string                   # Optional - Working directory (supports {{variable}})
  env: dict[string, string]     # Optional - Environment variables (values support {{variable}})
  output_exit_code: string      # Optional - Variable name to store exit code

  # Common fields:
  condition: string             # Optional - Expression that must evaluate to true
  foreach: string               # Optional - Variable containing list to iterate
  as: string                    # Optional - Loop variable name (default: "item")
  collect: string               # Optional - Variable to collect all iteration results
  max_iterations: integer       # Optional - Safety limit (default: 100)
  while_condition: string       # Optional - Expression: loop while true (mutually exclusive with foreach)
  max_while_iterations: integer # Optional - Safety limit for while loops (default: 100, range: 1-1000)
  break_when: string            # Optional - Expression: exit loop early if true (requires foreach or while_condition)
  update_context: dict          # Optional - Variables to update after each loop iteration
  while_steps: list             # Optional - Multi-step loop body (list of step definitions, requires while_condition)
  output: string                # Optional - Variable name for step result
  agent_config: dict            # Optional - Override agent configuration
  timeout: integer|string       # Optional - Max execution time (seconds), or a template resolving to one
  retry: dict                   # Optional - Retry configuration
  on_error: string              # Optional - Error handling strategy
  depends_on: list[string]      # Optional - Step IDs that must complete first
```

**Every other step key is rejected at load** — see
[Rejected step keys](#rejected-step-keys).

### Rejected step keys

A key no engine reads is not inert: whatever it declared never happens. So any
step key outside the list above is **refused at parse**, naming the offending
key, the step it sits on, the valid keys, and a remedy — by the legacy loader
(`Recipe.from_yaml`) and by the schema-2 parsers alike.

Three of them read like an approval gate and get a remedy of their own:

| Rejected step key | Status | Remedy |
|---|---|---|
| `requires_approval` | **Rejected** — approval is not a step feature | Put the step in a `stages:` block and gate the stage: `approval: { required: true, prompt: <text>, when: before_stage }` |
| `approval_message` | **Rejected** — a step has no approval prompt | The gate's text is the stage's `approval: { prompt: <text> }` |
| `approval` | **Rejected** — `approval:` belongs to a stage | Move it up one level, onto the stage containing this step |

**Why this one mattered.** `README.md` documented `requires_approval: true` /
`approval_message:` on a step as *the* way to declare an approval gate. Neither
is a step field, so the recipe could not load at all — it died on a raw
`TypeError: Step.__init__() got an unexpected keyword argument
'requires_approval'`, which named no remedy and no valid keys. The README now
documents the staged shape; no step-level gate was invented to match it,
because none exists.

**What rejection looks like:**

```yaml
steps:
  - id: "plan-changes"
    agent: "zen-architect"
    prompt: "Plan dependency upgrades"
    requires_approval: true                     # ← rejected
    approval_message: "Review before applying"  # ← rejected
```

```
Step 'plan-changes' declares 'approval_message', 'requires_approval', which are not
step keys: they would be read by nobody, so whatever they declare never happens.
Valid step keys are 'agent', 'agent_config', 'as', ... . 'approval_message': the
gate's text is the stage's 'approval: {prompt: <text>}' -- a step has no approval
prompt; 'requires_approval': approval gates are a staged-mode feature -- put the
step in a 'stages:' block and gate the stage with 'approval: {required: true,
prompt: <text>, when: before_stage}'
```

**One documented difference between the engines.** The runner library also
accepts `instruction:` and `message:` as aliases for `prompt:` (its own
recipes were written that way); the legacy loader never has, and still does
not. Every other key is known to both engines or to neither — the two key
lists are pinned to each other by
`modules/tool-recipes/tests/test_unknown_step_and_stage_keys.py`.

Scoping: only **step mappings** are checked. A `context:` variable named
`requires_approval` is untouched.

### Step Fields

#### `id` (required)

**Type:** string
**Constraints:**
- Must be unique within recipe
- Alphanumeric, hyphens, underscores only
- Max length: 50 characters

**Purpose:** Identify step in logs, resumption, and dependency references.

**Examples:**
```yaml
- id: "analyze-code"
- id: "generate-tests"
- id: "validate-results"
```

#### `type` (optional)

**Type:** string
**Values:** `"agent"` (default), `"recipe"`, `"bash"`
**Purpose:** Specify what kind of execution this step performs.

**Examples:**
```yaml
# Default: agent step
- id: "analyze"
  agent: "foundation:zen-architect"
  prompt: "Analyze the code"

# Explicit agent step
- id: "review"
  type: "agent"
  agent: "foundation:code-reviewer"
  prompt: "Review the implementation"

# Recipe step (sub-workflow)
- id: "security-audit"
  type: "recipe"
  recipe: "security-audit.yaml"
  context:
    target: "{{file_path}}"

# Bash step (direct shell execution)
- id: "run-tests"
  type: "bash"
  command: "npm test"
  output: "test_results"
```

**Behavior:**
- `"agent"` (default): Step spawns an LLM agent with a prompt
- `"recipe"`: Step executes another recipe as a sub-workflow
- `"bash"`: Step executes a shell command directly (no LLM overhead)

See [Recipe Composition](#recipe-composition) for details on recipe steps.
See [Bash Steps](#bash-steps) for details on bash steps.

#### `agent` (required for agent steps)

**Type:** string (agent name with bundle namespace)
**Purpose:** Specify which agent to spawn for this step.

**Naming convention:**
Agents MUST use namespaced references in the format `bundle:agent-name`:
- `foundation:zen-architect` - Agent from the foundation bundle
- `foundation:bug-hunter` - Agent from the foundation bundle
- `foundation:security-guardian` - Agent from the foundation bundle

**Important: Agent references create bundle dependencies.**
When a recipe references an agent like `foundation:zen-architect`, it requires:
1. The foundation bundle (or a bundle that includes foundation) to be loaded
2. The agent to be available through the coordinator

**Agent sources:**
- Bundle agents (available via `bundle:agent-name` format)
- Custom agents (in `.amplifier/agents/` for local development)

**Examples:**
```yaml
- agent: "foundation:zen-architect"
- agent: "foundation:bug-hunter"
- agent: "foundation:test-coverage"
- agent: "foundation:security-guardian"
```

**Validation:**
- Agent must be available via coordinator when recipe executes
- Recipes should document required agents in header comments
- Tool checks agent availability before starting recipe
- Fails fast if agent not found

**Bundle dependency implications:**
If your recipe uses agents from a bundle, that bundle (or one that includes it) must be loaded. The recipes bundle includes the foundation bundle, so `foundation:*` agents are available by default when using the recipes bundle.

#### `recipe` (required for recipe steps)

**Type:** string (recipe path)
**Purpose:** Specify which recipe to execute as a sub-workflow.

**Path resolution:**
- Relative paths resolved from current recipe's directory
- Absolute paths used as-is
- Recipe must exist and be valid

**Examples:**
```yaml
# Relative path (same directory)
- id: "security-check"
  type: "recipe"
  recipe: "security-audit.yaml"

# Relative path (subdirectory)
- id: "lint-check"
  type: "recipe"
  recipe: "checks/linting.yaml"

# Parent directory
- id: "shared-validation"
  type: "recipe"
  recipe: "../shared/validation.yaml"
```

**Validation:**
- Recipe file must exist
- Recipe must be valid (passes schema validation)
- Circular references prevented via recursion tracking

#### `context` (optional, for recipe steps)

**Type:** dictionary (string keys, any values)
**Purpose:** Pass context variables to the sub-recipe.

**Key feature:** Context isolation - sub-recipes receive ONLY the variables explicitly passed, not the parent's entire context. This prevents context poisoning and ensures predictable behavior.

**Examples:**
```yaml
# Pass specific variables
- id: "security-audit"
  type: "recipe"
  recipe: "security-audit.yaml"
  context:
    target_file: "{{file_path}}"
    severity_threshold: "high"

# Pass computed values
- id: "detailed-analysis"
  type: "recipe"
  recipe: "analysis.yaml"
  context:
    files: "{{discovered_files}}"
    previous_results: "{{initial_scan}}"

# No context (sub-recipe uses only its own defaults)
- id: "standalone-check"
  type: "recipe"
  recipe: "standalone.yaml"
```

**Behavior:**
- Variables use template syntax: `{{variable_name}}`
- Sub-recipe's `context` dict is REPLACED with passed context
- Sub-recipe's outputs available via step's `output` field
- Empty context dict `{}` passes nothing (sub-recipe uses defaults)

**Why context isolation?**
- Prevents accidental variable leakage
- Makes sub-recipes predictable and testable
- Enables recipe reuse across different contexts
- Follows principle of least privilege

#### `mode` (optional)

**Type:** string
**Purpose:** Specify how an agent should operate. Modes are agent-specific - consult each agent's documentation to see what modes it supports.

**How it works:** The mode string is prepended to the instruction as `"MODE: {mode}\n\n"` when spawning the agent. Agents that support modes will recognize this prefix and adjust their behavior accordingly.

**Example (zen-architect):**

The `foundation:zen-architect` agent supports three modes:
- `ANALYZE`: For breaking down problems and designing solutions
- `ARCHITECT`: For system design and module specification
- `REVIEW`: For code quality assessment and recommendations

```yaml
- id: "design"
  agent: "foundation:zen-architect"
  mode: "ARCHITECT"
  prompt: "Design a caching layer for the API"

- id: "review"
  agent: "foundation:zen-architect"
  mode: "REVIEW"
  prompt: "Review the implementation for simplicity and maintainability"
```

**Important notes:**
- Not all agents support modes. If an agent doesn't recognize the MODE prefix, it will simply treat it as part of the instruction text.
- Modes are defined by each agent. See agent documentation (e.g., `foundation/agents/zen-architect.md`) for supported modes and their meanings.
- If omitted, the agent uses its default behavior.

#### `provider` (optional, agent steps only)

**Type:** string
**Purpose:** Specify which LLM provider to use for this step, overriding the session's default.

**How it works:**
- The specified provider is promoted to priority 0 (highest) for this step's agent session
- Provider must be configured in the session (via `~/.amplifier/settings.yaml` or bundle config)
- If provider not found, a warning is logged and the default provider is used

**Examples:**
```yaml
# Use Anthropic for this step
- id: "analyze"
  agent: "foundation:zen-architect"
  provider: "anthropic"
  prompt: "Analyze the architecture"

# Use OpenAI for implementation
- id: "implement"
  agent: "foundation:modular-builder"
  provider: "openai"
  prompt: "Implement the changes"
```

**Provider matching:**
Provider IDs are matched flexibly:
- `"anthropic"` matches `provider-anthropic`
- `"openai"` matches `provider-openai`
- Full module name also works: `"provider-anthropic"`

**Validation:**
- Only valid for agent steps (`type: "agent"` or default)
- Ignored if specified on bash or recipe steps (validation error)

#### `model` (optional, agent steps only)

**Type:** string (exact name or glob pattern)
**Purpose:** Specify which model to use, with optional glob pattern matching for flexibility.

**How it works:**
1. If not a glob pattern (no `*`, `?`, or `[` characters), used as-is
2. If a glob pattern, resolves against available models from the provider:
   - Queries provider for available model list
   - Filters with `fnmatch` (shell-style wildcards)
   - Sorts matches descending (latest date/version first)
   - Returns first match

**Examples:**
```yaml
# Exact model name
- id: "analyze"
  agent: "foundation:zen-architect"
  provider: "anthropic"
  model: "claude-sonnet-4-5-20250514"
  prompt: "Analyze the code"

# Glob pattern - gets latest claude-sonnet-4-5-*
- id: "implement"
  agent: "foundation:modular-builder"
  provider: "anthropic"
  model: "claude-sonnet-4-5-*"
  prompt: "Implement the changes"

# Glob pattern for OpenAI
- id: "review"
  agent: "foundation:zen-architect"
  provider: "openai"
  model: "gpt-5*"
  prompt: "Review for quality"
```

**Glob pattern syntax:**
| Pattern | Matches |
|---------|---------|
| `*` | Any sequence of characters |
| `?` | Any single character |
| `[abc]` | Any character in the set |
| `[!abc]` | Any character NOT in the set |

**Pattern examples:**
```yaml
model: "claude-sonnet-*"        # Any claude-sonnet model
model: "claude-sonnet-4-5-*"    # Any claude-sonnet-4-5 dated version
model: "gpt-5*"                 # Any gpt-5 variant
model: "gpt-5.?"                # gpt-5.0, gpt-5.1, gpt-5.2, etc.
```

**Resolution behavior:**
- Pattern matches are sorted descending alphabetically
- This means dated versions (e.g., `20250514`) sort newest-first
- If the provider's model list came back and **nothing matched**, the step falls back to
  that provider's **default model** and logs a WARNING naming the dropped pattern. The
  pattern itself is never handed to the provider: no model is literally named
  `claude-haiku-*`, so passing it through would guarantee a `not_found_error` (404).
  The fallback resolves to a **real model id** — the one the mount plan declares for that
  provider instance, or failing that the one the mounted provider itself reports. It is
  never the empty string: a preference's model is written straight onto the promoted
  provider's `default_model`, so an empty model *blanks* that provider's configured model
  and the request fails with `invalid_request_error` — "model: String should have at least
  1 character" (a 400 instead of a 404 is not a fallback). On the rare host that names no
  default at all, the preference is **dropped** with a WARNING and the step runs on the
  calling session's provider ordering.
- If the provider's model list **could not be read** (no provider configured, no
  `list_models` support, query failed), the pattern is left as-is for the host to resolve
  against whichever provider instance it finally selects. "Could not enumerate" is not
  evidence of "no match", so the author's pattern is not discarded on it.
- Resolution details are logged at DEBUG level

**Validation:**
- Only valid for agent steps (`type: "agent"` or default)
- Ignored if specified on bash or recipe steps (validation error)
- **`model` without `provider` is discarded, not applied.** The engine honours a
  step-level `model:` only together with a `provider:` (`executor.execute_step`'s
  `elif step.provider and step.model:` branch); written alone it matches no branch, no
  preference is built, and the step silently runs on the session's default model.
  Validation reports this as a warning coded `RECIPE_MODEL_WITHOUT_PROVIDER`. Add the
  `provider:` the model belongs to, or drop the `model:` line.

**Combining provider and model:**
```yaml
# Use specific provider with pattern-matched model
- id: "creative-task"
  agent: "foundation:zen-architect"
  provider: "anthropic"
  model: "claude-opus-*"
  prompt: "Design an innovative architecture"

# Different models for different task types
steps:
  - id: "quick-analysis"
    agent: "foundation:explorer"
    provider: "anthropic"
    model: "claude-sonnet-*"  # Fast model for exploration
    prompt: "Survey the codebase"
    
  - id: "deep-reasoning"
    agent: "foundation:zen-architect"
    provider: "anthropic"
    model: "claude-opus-*"    # Powerful model for design
    prompt: "Design the architecture based on {{analysis}}"
```

#### `provider_preferences` (optional, agent steps only)

**Type:** list of `{class}` or `{provider, model}` objects
**Purpose:** Specify an ordered list of model class and/or provider/model preferences with automatic fallback.

**How it works:**
- The system tries each entry in order until one is available
- **Class entries** (`class: reasoning`) resolve to the best available model matching that capability class
- **Provider entries** (`provider: anthropic, model: ...`) try a specific provider/model combination
- First available match is promoted to priority 0 (highest) for this step
- If no entries in the list are available, falls back to session default
- Each provider entry can include a model glob pattern that gets resolved

**This is the preferred approach** for production recipes that need resilience across different provider configurations.

**Example:**
```yaml
# Class-based with explicit fallbacks
- id: "analyze"
  agent: "foundation:zen-architect"
  provider_preferences:
    - class: reasoning               # Try best reasoning model first
    - provider: anthropic             # Explicit fallback chain
      model: claude-sonnet-*
    - provider: openai
      model: gpt-4o
  prompt: "Analyze the architecture"

# Provider-only fallback chain (legacy style, still supported)
- id: "analyze-legacy"
  agent: "foundation:zen-architect"
  provider_preferences:
    - provider: anthropic
      model: claude-sonnet-*
    - provider: openai
      model: gpt-4o
    - provider: azure
      model: gpt-4o
  prompt: "Analyze the architecture"
```

**Entry types:**

Class entry (provider-agnostic):
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `class` | string | Yes | Model class: `"reasoning"`, `"fast"`, `"vision"`, `"research"` |

Provider entry (explicit):
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `provider` | string | Yes | Provider ID (e.g., "anthropic", "openai") |
| `model` | string | No | Model name or glob pattern (e.g., "claude-haiku-*") |

**Available model classes:**

| Class | Matches Models With | Typical Use |
|-------|---------------------|-------------|
| `reasoning` | `reasoning` or `thinking` capability | Architecture, security, complex analysis |
| `fast` | `fast` capability | File ops, classification, simple tasks |
| `vision` | `vision` capability | Image analysis |
| `research` | `deep_research` capability | Research tasks |

**When to use which approach:**

| Use Case | Recommended Approach |
|----------|---------------------|
| Provider-agnostic, portable recipes | `class:` entries |
| Multi-provider fallback | `class:` + `provider` entries |
| Pinned to specific model version | `provider` + `model` entries |
| Single provider, simple use | `provider` + `model` (legacy) |

**Validation:**
- Cannot be used together with `provider` or `model` fields (mutual exclusivity)
- Only valid for agent steps (`type: "agent"` or default)
- List cannot be empty
- Each entry must have either a `class` key or a `provider` key

**Examples with different fallback strategies:**

```yaml
# Class-based: best reasoning model from any provider
- id: "complex-design"
  agent: "foundation:zen-architect"
  provider_preferences:
    - class: reasoning
  prompt: "Design a complex distributed system"

# Class-based with fallback: fast model preferred, explicit backup
- id: "quick-check"
  agent: "foundation:explorer"
  provider_preferences:
    - class: fast
    - provider: anthropic
      model: claude-haiku-*
    - provider: openai
      model: gpt-4o-mini
  prompt: "Quick survey of the codebase"

# Provider-only fallback (use each provider's default model)
- id: "flexible-task"
  agent: "foundation:modular-builder"
  provider_preferences:
    - provider: anthropic
    - provider: openai
    - provider: azure
  prompt: "Implement the changes"
```

#### `prompt` (required)

**Type:** string (template)
**Purpose:** Define what the agent should do.

**Template variables:**
- `{{variable_name}}` - Replaced with context value
- Context sources:
  - Top-level `context` dict
  - Previous step outputs (if step specified `output`)
  - Recipe metadata (`{{recipe.name}}`, `{{recipe.version}}`)

**Examples:**

Simple prompt:
```yaml
prompt: "Analyze the Python code for type safety issues"
```

With variables:
```yaml
prompt: "Analyze {{file_path}} for compatibility with Python {{target_version}}"
```

Multi-line prompt:
```yaml
prompt: |
  Review this code for security issues:

  File: {{file_path}}
  Previous analysis: {{analysis}}

  Focus on: {{focus_areas}}
```

Accessing previous step output:
```yaml
steps:
  - id: "analyze"
    prompt: "Analyze {{file_path}}"
    output: "analysis"

  - id: "improve"
    prompt: "Given this analysis: {{analysis}}, suggest improvements"
```

**Undefined variables:**
- If variable undefined, step fails with clear error
- Use `context` dict to define required variables upfront
- Check variable availability with `depends_on`

#### `condition` (optional)

**Type:** string (expression)
**Purpose:** Skip step if condition evaluates to false.

**Syntax:**
- Variable references: `{{variable}}` or `{{object.property}}`
- Comparison operators: `==`, `!=`
- Boolean operators: `and`, `or`
- String literals: `'value'` or `"value"`

**Examples:**

Simple equality:
```yaml
- id: "critical-fix"
  condition: "{{severity}} == 'critical'"
  agent: "foundation:auto-fixer"
  prompt: "Auto-fix critical issues"
```

With nested variable access:
```yaml
- id: "apply-fixes"
  condition: "{{analysis.severity}} == 'critical'"
  agent: "foundation:fixer"
  prompt: "Apply fixes for: {{analysis.issues}}"
```

Compound conditions:
```yaml
- id: "deploy"
  condition: "{{tests_passed}} == 'true' and {{review_approved}} == 'true'"
  agent: "foundation:deployer"
  prompt: "Deploy to production"
```

Alternative conditions:
```yaml
- id: "escalate"
  condition: "{{severity}} == 'critical' or {{severity}} == 'high'"
  agent: "foundation:notifier"
  prompt: "Escalate to on-call team"
```

**Behavior:**
- Condition is `true` → Execute step normally
- Condition is `false` → Skip step, continue to next
- Undefined variable in condition → **Fail recipe** with clear error
- Invalid syntax → **Fail recipe** with parse error
- Skipped step with `output` field → Output variable remains undefined

**Rationale:** Fail fast on errors. Silent skips would mask configuration problems.

See [Condition Expressions](#condition-expressions) for complete syntax reference.

#### `foreach` (optional)

**Type:** string (variable reference)
**Purpose:** Iterate over a list, executing the step once per item.

**Syntax:**
- Must contain a variable reference: `{{variable_name}}`
- Referenced variable must be a list at runtime

**Examples:**
```yaml
- id: "discover-files"
  agent: "foundation:explorer"
  prompt: "List all Python files in {{directory}}"
  output: "files"  # Returns list: ["a.py", "b.py", "c.py"]

- id: "analyze-each"
  foreach: "{{files}}"
  as: "current_file"
  agent: "foundation:analyzer"
  prompt: "Analyze {{current_file}} for issues"
  collect: "file_analyses"
```

**Behavior:**
- `foreach` variable is list → Iterate over each item
- `foreach` variable is empty list → Skip step (no error)
- `foreach` variable is not a list → **Fail recipe** with clear error
- `foreach` variable undefined → **Fail recipe** with clear error
- Exceeds `max_iterations` → **Fail recipe** with limit error
- Any iteration fails → **Fail recipe** immediately (fail-fast)

**Rationale:** Fail fast and visibly. Silent partial failures hide bugs.

See [Looping and Iteration](#looping-and-iteration) for complete syntax reference.

#### `as` (optional)

**Type:** string (variable name)
**Default:** `"item"`
**Purpose:** Name of loop variable available within the iteration.

**Constraints:**
- Must be valid variable name (alphanumeric, underscores)
- Cannot conflict with reserved names (`recipe`, `step`, `session`)

**Examples:**
```yaml
# Using default "item"
- foreach: "{{files}}"
  prompt: "Process {{item}}"

# Using custom name
- foreach: "{{files}}"
  as: "current_file"
  prompt: "Process {{current_file}}"
```

**Scope:**
- Loop variable only available within the loop step
- Not available in subsequent steps (loop-scoped)

#### `collect` (optional)

**Type:** string (variable name)
**Purpose:** Aggregate all iteration results into a list variable.

**Constraints:**
- Must be valid variable name (alphanumeric, underscores)
- Cannot conflict with reserved names (`recipe`, `step`, `session`)

**Examples:**
```yaml
- id: "analyze-each"
  foreach: "{{files}}"
  as: "file"
  prompt: "Analyze {{file}}"
  collect: "all_analyses"  # List of all iteration results

- id: "summarize"
  prompt: "Summarize these analyses: {{all_analyses}}"
```

**Behavior:**
- Collects results in order of iteration
- Available to subsequent steps after loop completes
- If `collect` omitted and `output` specified, `output` contains last iteration result only

#### `max_iterations` (optional)

**Type:** integer
**Default:** 100
**Purpose:** Safety limit to prevent runaway loops.

**Constraints:**
- Must be positive integer

**Examples:**
```yaml
# Default limit of 100
- foreach: "{{files}}"
  prompt: "Process {{item}}"

# Higher limit for large batches
- foreach: "{{large_dataset}}"
  max_iterations: 500
  prompt: "Process {{item}}"

# Lower limit for safety
- foreach: "{{untrusted_input}}"
  max_iterations: 10
  prompt: "Process {{item}}"
```

**Behavior:**
- If list length exceeds `max_iterations`, recipe fails with clear error
- Error message shows actual count vs limit

#### `output` (optional)

**Type:** string (variable name)
**Purpose:** Store step result in context for later steps.

**Constraints:**
- Must be valid variable name (alphanumeric, underscores)
- Cannot conflict with reserved names (`recipe`, `step`, `session`)

**Examples:**
```yaml
- id: "analyze"
  prompt: "Analyze code"
  output: "analysis"     # Stores result as {{analysis}}

- id: "improve"
  prompt: "Review: {{analysis}}"
  output: "improvements" # Stores as {{improvements}}
```

**Behavior:**
- If omitted, step result not stored (use for terminal steps)
- Stored results persist across session checkpoints
- Available to all subsequent steps

#### `parse_json` (optional)

**Type:** boolean
**Default:** `false`
**Purpose:** Control JSON extraction from agent output.

**Behavior:**

**`parse_json: false` (default) - Conservative**:
- Preserves output as-is (prose, markdown, formatting intact)
- Only parses if the ENTIRE output is clean JSON (no markdown, no prose)
- Best for human-readable outputs (analysis, reports, summaries)

**`parse_json: true` - Aggressive extraction**:
- Attempts to extract JSON using multiple strategies:
  1. Parse entire string as JSON (clean JSON)
  2. Extract from markdown code blocks (```json ... ```)
  3. Find JSON object/array embedded in prose text
- Best when prompting for structured data
- Returns original string if no JSON found

**When to use `parse_json: true`:**
- You prompt the agent to return structured data
- You need to parse specific fields from the response
- The agent might wrap JSON in markdown or prose
- You want to use the data in conditions or subsequent steps

**When to use `parse_json: false` (default):**
- You want prose/markdown output preserved
- The agent returns analysis, summaries, or reports
- Human readability is important
- You don't need to parse structured fields

**Examples:**

Conservative default (prose preserved):
```yaml
- id: "analyze"
  agent: "foundation:zen-architect"
  prompt: "Analyze the code and provide a detailed report with recommendations"
  output: "analysis"
  # parse_json: false (implicit default)
  # Result: Full prose report with formatting preserved
```

Agent returns:
```
Code Analysis Report

The code shows 3 main issues:

1. High Complexity (lines 45-52)
   Function has cyclic complexity of 15...

2. Missing Validation (line 78)
   No input validation on email parameter...
```

Result stored: `{{analysis}}` = (the full prose above, preserved)

Aggressive JSON extraction:
```yaml
- id: "extract-severity"
  agent: "foundation:zen-architect"
  prompt: |
    From the analysis above, extract the overall severity as JSON:
    
    {
      "severity": "critical|high|medium|low",
      "issue_count": number
    }
  output: "severity_data"
  parse_json: true  # Extract JSON even if wrapped in prose
```

Agent returns:
```
Based on the issues found, here's the severity assessment:

```json
{
  "severity": "high",
  "issue_count": 3
}
```

This indicates immediate attention is needed.
```

Result stored: `{{severity_data}}` = `{"severity": "high", "issue_count": 3}`

Using extracted data in conditions:
```yaml
- id: "conditional-action"
  condition: "{{severity_data.severity}} == 'high'"
  prompt: "Take action for high severity"
  # This step only runs if severity is "high"
```

**Backwards Compatibility:**

Existing recipes without `parse_json` continue working:
- Clean JSON responses still parse correctly (no change)
- Prose/markdown responses now preserved (improvement, not breaking)
- If you were relying on aggressive extraction, add `parse_json: true`

**Note:** If the agent returns pure JSON (without markdown/prose), both settings parse it successfully. The difference only matters when JSON is embedded in other text.

#### `agent_config` (optional)

**Type:** dictionary (partial agent config)
**Purpose:** Override agent configuration for this step.

**Use cases:**
- Adjust temperature for creative vs analytical steps
- Use different models for different steps
- Add step-specific tools

**Example:**
```yaml
- id: "creative-brainstorm"
  agent: "foundation:zen-architect"
  agent_config:
    providers:
      - module: "provider-anthropic"
        config:
          temperature: 0.8  # More creative than agent's default
          model: "claude-opus-4"
    tools:
      - module: "tool-web-search"  # Add web search for this step only
  prompt: "Brainstorm innovative architectures"
```

**Merge behavior:**
- Specified fields override agent defaults
- Unspecified fields inherit from agent config
- Deep merge for nested dicts (providers, tools, etc.)

#### `timeout` (optional)

**Type:** integer (seconds), or a template string resolving to one
**Default:** 600 (10 minutes)
**Purpose:** Prevent hanging on unresponsive steps.

**Examples:**
```yaml
- timeout: 300   # 5 minutes
- timeout: 1800  # 30 minutes for long-running analysis
- timeout: "{{step_timeout}}"   # resolved from context at execution time
```

**Behavior:**
- If step exceeds timeout, execution cancelled
- Error logged with clear timeout message
- Recipe can resume from checkpoint (step retries)
- Applies to both `agent` steps (the sub-session spawn) and `bash` steps

**Templated timeouts:**

A `timeout:` may be a `{{template}}` so one recipe can be run with different
limits — a short budget in CI, a long one for an overnight run:

```yaml
context:
  step_timeout: 1800   # overridable at run time

steps:
  - id: "deep-analysis"
    agent: "analyzer"
    prompt: "Analyze the codebase"
    timeout: "{{step_timeout}}"
```

The template is resolved once, immediately before the timeout is applied, and
must yield a positive number of seconds. Resolution failures are loud and
name the step, the template, and what it resolved to:

- an undefined variable — `Step 'deep-analysis': timeout template
  '{{step_timeout}}' could not be resolved: Undefined variable: ...`
- a non-numeric value — `... resolved to 'soon', which is not a number of seconds`
- a non-positive value — `... resolved to 0, but timeout must be positive`

Each of these fails *before* the agent is spawned or the command is run, so an
unusable timeout never burns an invocation. A non-templated string that is not
a number (`timeout: "soon"`) is rejected earlier still, at recipe validation.

Literal numbers are unaffected — they never touch the substitution machinery.

#### `retry` (optional)

**Type:** dictionary
**Purpose:** Configure retry behavior for transient failures.

**Schema:**
```yaml
retry:
  max_attempts: integer     # Default: 3
  backoff: string          # "exponential" or "linear", default: "exponential"
  initial_delay: integer   # Seconds, default: 5
  max_delay: integer       # Seconds, default: 300
```

**Example:**
```yaml
- id: "fetch-data"
  agent: "foundation:data-fetcher"
  prompt: "Fetch latest data from API"
  retry:
    max_attempts: 5
    backoff: "exponential"
    initial_delay: 10
    max_delay: 300
```

**Retry behavior:**
- Only retries on transient errors (network, timeout, rate limit)
- Does not retry on validation errors or agent failures
- Each retry logs attempt number and delay
- Exponential backoff: delay doubles each attempt (10s, 20s, 40s, ...)

#### `on_error` (optional)

**Type:** string (error handling strategy)
**Values:**
- `"fail"` (default) - Stop recipe execution
- `"continue"` - Log error, continue to next step
- `"skip_remaining"` - Skip remaining steps, mark recipe as partial success

**Examples:**
```yaml
- id: "optional-validation"
  agent: "foundation:validator"
  prompt: "Validate results"
  on_error: "continue"  # Don't fail recipe if validation fails
```

**Use cases:**
- `"continue"`: Optional validation, non-critical steps
- `"skip_remaining"`: Guard steps that make remaining work unnecessary
- `"fail"`: Default - any failure stops recipe

#### `depends_on` (optional)

**Type:** list of strings (step IDs)
**Purpose:** Explicit dependencies between steps.

**Default behavior:**
- Steps execute in order
- Each step depends on all previous steps

**Use `depends_on` when:**
- Explicit dependency documentation
- Complex step ordering requirements

**Example:**
```yaml
steps:
  - id: "analyze-security"
    prompt: "Security analysis"
    output: "security_report"

  - id: "analyze-performance"
    prompt: "Performance analysis"
    output: "performance_report"

  - id: "generate-summary"
    depends_on: ["analyze-security", "analyze-performance"]
    prompt: "Summarize: {{security_report}} and {{performance_report}}"
```

**Validation:**
- Referenced step IDs must exist in recipe
- No circular dependencies
- Dependencies must appear before dependent step in YAML

---

## Variable Substitution

### Template Syntax

Variables use double-brace syntax: `{{variable_name}}`

### Variable Sources

Variables come from multiple sources (priority order):

1. **Step outputs** - `output` from previous steps
2. **Top-level context** - `context` dict in recipe
3. **Recipe metadata** - `recipe.*` variables
4. **Session metadata** - `session.*` variables

### Reserved Variables

Available in all steps:

```yaml
{{recipe.name}}         # Recipe name
{{recipe.version}}      # Recipe version
{{recipe.description}}  # Recipe description

{{session.id}}          # Current session ID
{{session.started}}     # Session start timestamp
{{session.project}}     # Project path (slugified)

{{step.id}}             # Current step ID
{{step.index}}          # Step number (0-based)
```

### Example

```yaml
context:
  file_path: "src/auth.py"
  severity: "high"

steps:
  - id: "analyze"
    prompt: |
      Recipe: {{recipe.name}} v{{recipe.version}}
      Session: {{session.id}}

      Analyze {{file_path}} for {{severity}}-severity issues
    output: "analysis"

  - id: "report"
    prompt: |
      Create report for:
      File: {{file_path}}
      Analysis: {{analysis}}

      Step {{step.index}} of recipe
```

### Undefined Variables

If variable undefined at runtime:
- Execution fails with clear error
- Error message shows variable name and available variables
- Session checkpointed (can fix and resume)

---

## Condition Expressions

Step conditions use a simple expression syntax for runtime evaluation.

### Syntax Overview

```
<expression> := <or_expr>
<or_expr>    := <and_expr> ("or" <and_expr>)*
<and_expr>   := <not_expr> ("and" <not_expr>)*
<not_expr>   := "not" <not_expr> | <comparison> | "(" <expression> ")"
<comparison> := <value> <operator> <value>
<operator>   := "==" | "!=" | "<" | ">" | "<=" | ">="
<value>      := <variable> | <string-literal> | <number>
<variable>   := "{{" identifier ("." identifier)* "}}"
<string>     := "'" chars "'" | '"' chars '"'
<number>     := [0-9]+ ("." [0-9]+)?
```

### Operators

| Operator | Example | Description |
|----------|---------|-------------|
| `==` | `{{status}} == 'passed'` | Equal to |
| `!=` | `{{status}} != 'failed'` | Not equal to |
| `<` | `{{count}} < 10` | Less than (numeric-aware) |
| `>` | `{{score}} > 0.8` | Greater than (numeric-aware) |
| `<=` | `{{count}} <= {{max}}` | Less than or equal (numeric-aware) |
| `>=` | `{{score}} >= {{threshold}}` | Greater than or equal (numeric-aware) |
| `not` | `not {{converged}}` | Logical negation (unary) |
| `and` | `{{a}} and {{b}}` | Logical AND |
| `or` | `{{a}} or {{b}}` | Logical OR |
| `()` | `({{a}} or {{b}}) and {{c}}` | Grouping / precedence |

**Numeric comparison:** When both operands parse as numbers (int or float), comparison
operators (`<`, `>`, `<=`, `>=`) compare numerically. Otherwise they compare as strings.

**Boolean normalization:** These values are treated as falsy: `false`, `False`, `""`, `"0"`, `"none"`, `"None"`, `"null"`.
All other non-empty values are truthy.

**Operator precedence** (lowest to highest): `or` → `and` → `not` → comparison → `()`

### Variable References

Variables use the same `{{variable}}` syntax as prompt templates:

```yaml
# Simple variable
condition: "{{status}} == 'approved'"

# Nested access
condition: "{{report.severity}} == 'critical'"

# From step output
condition: "{{analysis_result}} != 'failed'"
```

**A condition substitutes VALUES, not text.** Everywhere a condition is
evaluated — `condition:`, `while_condition:`, `break_when:`, on a top-level
step, a staged step or a loop sub-step — a `{{reference}}` is rendered as a
*literal*, so the expression stays well-formed whatever the value is:

| Value | Renders as | Example expression after substitution |
|-------|-----------|----------------------------------------|
| `""` (empty string) | `''` | `'' == ''` |
| `"v3.md"` | `'v3.md'` | `'v3.md' != ''` |
| `"all clear"` (spaces) | `'all clear'` | `'all clear' == 'all clear'` |
| `"it's"` (inner quote) | `'it\'s'` (escaped) | `'it\'s' == 'it\'s'` |
| `5`, `3.14` | `5`, `3.14` (bare) | `5 > 3` |
| `true` / `false` | `true` / `false` | `true == true` |
| `None` | `null` (falsy, beside `none`/`None`) | `null == ''` → false |

Two consequences worth knowing:

- **You do not need to quote the reference.** `condition: "{{continue_from}}
  == ''"` is correct even when `continue_from` defaults to `""` — it renders
  to `'' == ''`, not to a dangling `== ''`. (Before `recipes-kft` a bare
  reference *was* pasted in as raw text inside loops, so an empty-string
  default killed the run and a value containing a space parsed as two
  tokens.)
- **Quoting it anyway is still fine.** `condition: "'{{continue_from}}' ==
  ''"` splices the value inside the quotes you wrote rather than adding a
  second pair, so both spellings mean the same thing.

A reference the context does not carry at all is still an error
(`Undefined variable: ...`). A reference whose *value* is `None` is not — it
compares as `null` and is falsy.

### String Literals

String values must be quoted with single or double quotes:

```yaml
# Single quotes
condition: "{{status}} == 'approved'"

# Double quotes
condition: '{{status}} == "approved"'
```

### Boolean Logic

Combine conditions with `and` / `or`:

```yaml
# Both conditions must be true
condition: "{{security_passed}} == 'true' and {{tests_passed}} == 'true'"

# Either condition can be true
condition: "{{severity}} == 'critical' or {{severity}} == 'high'"

# Chained conditions (evaluated left to right)
condition: "{{a}} == 'x' and {{b}} == 'y' or {{c}} == 'z'"
```

**Operator precedence** (lowest to highest): `or` → `and` → `not` → comparison → `()`.
Use parentheses for explicit grouping:

```yaml
# Parentheses for clarity
condition: "({{severity}} == 'critical' or {{severity}} == 'high') and {{auto_fix}} == 'true'"

# Negation
condition: "not {{skip_review}}"
```

### Error Handling

| Scenario | Behavior |
|----------|----------|
| Condition evaluates to `true` | Execute step normally |
| Condition evaluates to `false` | Skip step, continue to next |
| Undefined variable | **Fail recipe** with clear error message |
| Invalid syntax | **Fail recipe** with parse error |

**Example error:**
```
Step 'critical-fix' condition error: Undefined variable in condition: {{missing}}.
Available: severity, analysis, report
```

### Session State

Skipped steps are tracked in session state:

```json
{
  "skipped_steps": [
    {
      "id": "critical-fix",
      "reason": "condition evaluated to false",
      "condition": "{{severity}} == 'critical'"
    }
  ]
}
```

### Complete Example

```yaml
name: "conditional-code-review"
description: "Review with conditional fixes based on severity"
version: "1.0.0"

context:
  file_path: "src/auth.py"

steps:
  - id: "analyze"
    agent: "foundation:analyzer"
    prompt: "Analyze {{file_path}} for issues"
    output: "analysis"

  - id: "critical-fix"
    condition: "{{analysis.severity}} == 'critical'"
    agent: "foundation:fixer"
    prompt: "Fix critical issues in {{file_path}}: {{analysis.issues}}"
    output: "fixes"

  - id: "high-priority-review"
    condition: "{{analysis.severity}} == 'high' or {{analysis.severity}} == 'critical'"
    agent: "foundation:reviewer"
    prompt: "Review high-priority issues: {{analysis}}"
    output: "review"

  - id: "report"
    agent: "foundation:reporter"
    prompt: |
      Generate report:
      Analysis: {{analysis}}
      Fixes: {{fixes}}
      Review: {{review}}
```

### Deferred Features

These operators are not yet implemented but may be added based on need:

- String functions: `.contains()`, `.startswith()`, `.endswith()`

---

## Looping and Iteration

Steps with a `foreach` field iterate over a list variable, executing the step once per item.

### Basic Syntax

```yaml
- id: "process-each"
  foreach: "{{items}}"     # Variable containing list
  as: "current_item"       # Loop variable name (default: "item")
  agent: "foundation:processor"
  prompt: "Process {{current_item}}"
  collect: "all_results"   # Aggregates iteration results
```

### How It Works

1. **Resolve `foreach` variable** → Must be a list
2. **For each item** in list:
   - Set loop variable (`as`) to current item
   - Substitute variables in prompt
   - Execute step (spawn agent)
   - Add result to collect list (if `collect` specified)
3. **After all iterations**:
   - Remove loop variable from context (scope ends)
   - Store collected results (if `collect` specified)

### Variable Scoping

```yaml
steps:
  - id: "process"
    foreach: "{{files}}"
    as: "current_file"
    prompt: "Process {{current_file}}"  # current_file available here
    output: "result"
    collect: "all_results"

  - id: "summary"
    prompt: "Summarize {{all_results}}"  # all_results available
    # current_file NOT available here (loop-scoped)
```

**Scope rules:**
- Loop variable (`as`) only available within the loop step
- `collect` variable available after loop completes
- Step `output` is the LAST iteration result (if not using `collect`)

### Error Handling (Fail-Fast)

| Scenario | Behavior |
|----------|----------|
| `foreach` variable is list | Iterate over each item |
| `foreach` variable is empty list | Skip step (no error) |
| `foreach` variable is not list | **Fail recipe** with clear error |
| `foreach` variable undefined | **Fail recipe** with clear error |
| Iteration exceeds `max_iterations` | **Fail recipe** with limit error |
| Any iteration fails | **Fail recipe** immediately |

**Rationale:** Fail fast and visibly during development. Silent partial failures hide bugs. If partial completion is needed later, that can be added.

### Parallel Iteration

Use `parallel` to run iterations concurrently:

```yaml
- id: "multi-perspective-analysis"
  foreach: "{{perspectives}}"
  as: "perspective"
  collect: "analyses"
  parallel: true  # Run all iterations simultaneously (unbounded)
  agent: "foundation:zen-architect"
  prompt: "Analyze from {{perspective}} perspective"
```

**Bounded Parallelism (Recommended for Large Loops):**

Use an integer to limit concurrent iterations:

```yaml
- id: "analyze-repos"
  foreach: "{{repos}}"
  as: "repo"
  collect: "analyses"
  parallel: 5  # Max 5 concurrent iterations
  type: "recipe"
  recipe: "repo-analysis.yaml"
```

| Value | Type | Behavior |
|-------|------|----------|
| `false` | bool | Sequential (one at a time) |
| `true` | bool | Unbounded parallel (all at once) |
| `5` | int | Bounded parallel (max 5 concurrent) |

**Behavior with parallel execution:**
- All iterations are queued immediately
- With `true`: all run at once
- With integer N: max N run concurrently, others wait
- Results collected in input order (regardless of completion order)
- If ANY iteration fails, entire step fails (fail-fast)

**When to use each mode:**

| Mode | Use Case |
|------|----------|
| `false` | Order-dependent, incremental operations |
| `true` | Small loops (<10), no API rate limits |
| `5` (integer) | Large loops, API rate limits, shared resources |

**Default:** `parallel: false` (sequential iteration, as documented above)

### Interaction with Conditions

```yaml
- id: "process-if-needed"
  condition: "{{should_process}} == 'true'"  # Check BEFORE loop
  foreach: "{{files}}"
  as: "file"
  prompt: "Process {{file}}"
  collect: "results"
```

**Behavior:** Condition evaluated once. If false, entire loop skipped.

### Complete Example

```yaml
name: "batch-file-analyzer"
description: "Analyze multiple files and synthesize results"
version: "1.0.0"

context:
  directory: "src"

steps:
  - id: "discover-files"
    agent: "foundation:explorer"
    prompt: "List all Python files in {{directory}}"
    output: "files"

  - id: "analyze-each"
    foreach: "{{files}}"
    as: "current_file"
    agent: "foundation:analyzer"
    prompt: |
      Analyze {{current_file}} for:
      - Code complexity
      - Security issues
      - Performance concerns
    collect: "file_analyses"

  - id: "synthesize"
    agent: "foundation:zen-architect"
    mode: "ANALYZE"
    prompt: |
      Synthesize these individual file analyses into overall findings:

      {{file_analyses}}

      Prioritize by severity and provide actionable recommendations.
    output: "final_report"
```

### Edge Cases

1. **Empty list**: Skip step, no error (common case)
2. **Single item list**: Works like normal step (minimal overhead)
3. **Very large list**: Respect `max_iterations` (default 100)
4. **Nested variable in foreach**: `{{results.files}}` should work
5. **Loop variable shadows context**: Local scope takes precedence
6. **Condition + foreach**: Condition checked once, not per iteration

### While Loops (Convergence-Based Iteration)

While loops repeat a step until a condition becomes false. Unlike `foreach` which iterates
over a fixed list, `while_condition` enables open-ended iteration for convergence workflows.

```yaml
- id: "converge"
  type: "bash"
  command: |
    echo "{\"value\": \"$(({{counter}} + 1))\"}"
  output: "result"
  parse_json: true
  while_condition: "{{counter}} < {{max_count}}"
  max_while_iterations: 10
  update_context:
    counter: "{{result.value}}"
```

#### While Loop Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `while_condition` | string | - | Expression evaluated before each iteration. Loop exits when false. Must contain `{{`. Mutually exclusive with `foreach`. |
| `max_while_iterations` | integer | 100 | Safety limit (1-1000). Loop exits when reached. |
| `break_when` | string | - | Expression evaluated after each iteration body + `update_context`. Loop exits when true. Requires `foreach` or `while_condition`. |
| `update_context` | dict | - | Map of variable names to expressions. After each iteration, each expression is resolved and stored back into context. |
| `while_steps` | list | - | Multi-step loop body. Each entry is a full step definition. Requires `while_condition`. |

#### Execution Order

1. Check `max_while_iterations` safety limit
2. Evaluate `while_condition` → exit if false
3. Inject `_loop_index` (0-based) and `_loop_iteration` (1-based) into context
4. Execute step body (or `while_steps` sub-steps in sequence)
5. Store result in `context[step.output]`
6. Apply `update_context` mutations
7. Evaluate `break_when` → exit if true
8. Increment counter, go to 1

#### Loop Metadata Variables

| Variable | Type | Description |
|----------|------|-------------|
| `_loop_index` | integer | 0-based iteration counter |
| `_loop_iteration` | integer | 1-based iteration counter |

These are injected at runtime and available in the loop body's `command` and `prompt` fields.
They are cleaned up from context after the loop exits.

**Note:** The static validator does not recognize these runtime variables. If the validator
rejects `{{_loop_iteration}}`, use context variables managed via `update_context` instead.

#### Pattern: Sub-Recipe as Loop Body

For complex multi-step loop bodies, call a sub-recipe:

```yaml
- id: "factory-loop"
  type: "recipe"
  recipe: "./iteration.yaml"
  context:
    input_var: "{{parent_var}}"
  output: "iter_result"
  parse_json: true
  while_condition: "{{done}} != 'true'"
  break_when: "{{done}} == 'true'"
  update_context:
    done: "{{iter_result.last_step_output.done}}"
```

**Important:** Sub-recipe output is the sub-recipe's full context. Access nested step
outputs as `{{iter_result.step_output_name.field}}`, not `{{iter_result.field}}`.

### Deferred Features

These features may be added based on real usage needs:

- `continue_on_error` - partial completion on failures
- Checkpointing/resumability for long loops
- Nested loops (`nested_foreach`)

---

## Bash Steps

Bash steps execute shell commands directly without LLM overhead. Use them for:
- Running build tools, linters, or test suites
- Fetching data with `curl` or `wget`
- File operations that are simpler as shell commands
- Any deterministic command where an LLM isn't needed

### Basic Syntax

```yaml
- id: "run-tests"
  type: "bash"
  command: "npm test"
  output: "test_output"
```

### How It Works

1. **Variable substitution** in command, cwd, and env values
2. **Command execution** via subprocess shell
3. **Capture stdout** (stored in `output` variable)
4. **Capture stderr** (included in error messages on failure)
5. **Capture exit code** (optionally stored in `output_exit_code` variable)

### Bash-Specific Fields

#### `command` (required for bash steps)

**Type:** string (template)
**Purpose:** The shell command to execute.

**Examples:**
```yaml
# Simple command
- command: "echo hello"

# With variable substitution
- command: "curl {{api_url}}/data"

# Multi-line command
- command: |
    cd {{project_dir}}
    npm install
    npm test

# Piped commands
- command: "cat {{file}} | grep ERROR | wc -l"
```

#### `cwd` (optional)

**Type:** string (template)
**Purpose:** Working directory for the command.

**Behavior:**
- Relative paths resolved from project directory
- Supports `{{variable}}` substitution
- Must exist and be a directory

**Examples:**
```yaml
# Absolute path
- cwd: "/tmp/workspace"

# Relative to project
- cwd: "src/tests"

# From variable
- cwd: "{{build_dir}}"
```

#### `env` (optional)

**Type:** dict[string, string]
**Purpose:** Environment variables passed to the command.

**Behavior:**
- Merged with parent environment (command inherits all existing env vars)
- Values support `{{variable}}` substitution
- Keys are literal (no substitution)

**Examples:**
```yaml
- env:
    NODE_ENV: "production"
    API_KEY: "{{api_key}}"
    DEBUG: "true"
```

#### `output_exit_code` (optional)

**Type:** string (variable name)
**Purpose:** Store the command's exit code for conditional logic.

**Constraints:**
- Must be alphanumeric with underscores
- Cannot be reserved name (`recipe`, `session`, `step`)

**Examples:**
```yaml
- id: "check-health"
  type: "bash"
  command: "curl -f {{health_url}}"
  output_exit_code: "health_check_code"
  on_error: "continue"

- id: "handle-failure"
  condition: "{{health_check_code}} != '0'"
  agent: "foundation:bug-hunter"
  prompt: "Health check failed with code {{health_check_code}}"
```

### Error Handling

**Non-zero exit codes** are treated as errors by default:

```yaml
# Default: fail the recipe on non-zero exit
- id: "must-pass"
  type: "bash"
  command: "npm test"
  # on_error: "fail" (default)

# Continue on failure - capture exit code for later
- id: "optional-check"
  type: "bash"
  command: "npm audit"
  on_error: "continue"
  output_exit_code: "audit_code"

# Skip remaining steps on failure
- id: "guard-check"
  type: "bash"
  command: "test -f required-file.txt"
  on_error: "skip_remaining"
```

### Timeout

Bash steps respect the `timeout` field (default: 600 seconds):

```yaml
- id: "long-build"
  type: "bash"
  command: "npm run build"
  timeout: 1800  # 30 minutes

- id: "quick-check"
  type: "bash"
  command: "test -d node_modules"
  timeout: 10  # 10 seconds

- id: "configurable"
  type: "bash"
  command: "./run-suite.sh"
  timeout: "{{suite_timeout}}"  # resolved from context at execution time
```

If the command exceeds the timeout, it's killed and the step fails. Templated
timeouts resolve before the command is spawned — see
[`timeout` (optional)](#timeout-optional) for the resolution rules and error
messages.

### Complete Example

```yaml
name: "build-and-test"
description: "Build project and run tests"
version: "1.0.0"

context:
  project_dir: ""
  node_env: "test"

steps:
  - id: "install-deps"
    type: "bash"
    command: "npm ci"
    cwd: "{{project_dir}}"
    timeout: 300

  - id: "lint"
    type: "bash"
    command: "npm run lint"
    cwd: "{{project_dir}}"
    output: "lint_output"
    on_error: "continue"
    output_exit_code: "lint_code"

  - id: "test"
    type: "bash"
    command: "npm test"
    cwd: "{{project_dir}}"
    env:
      NODE_ENV: "{{node_env}}"
      CI: "true"
    output: "test_output"

  - id: "analyze-results"
    condition: "{{lint_code}} != '0'"
    agent: "foundation:zen-architect"
    prompt: |
      Lint failed. Output:
      {{lint_output}}
      
      Suggest fixes.
    output: "fix_suggestions"
```

### Security Considerations

**Bash steps execute arbitrary shell commands.** Be careful with:

- **User input in commands**: Variables from untrusted sources can enable command injection
- **Sensitive data**: Avoid echoing secrets; use env vars instead of command-line args
- **File permissions**: Commands run with the recipe executor's permissions

**Best practices:**
```yaml
# ✅ Good: API key in env var
- command: "curl -H 'Authorization: Bearer $API_KEY' {{url}}"
  env:
    API_KEY: "{{api_key}}"

# ❌ Bad: API key in command (visible in logs/history)
- command: "curl -H 'Authorization: Bearer {{api_key}}' {{url}}"
```

### When to Use Bash vs Agent Steps

| Use Bash When | Use Agent When |
|---------------|----------------|
| Deterministic commands | Judgment/analysis needed |
| Fast execution needed | Complex reasoning required |
| Build/test/deploy tools | Natural language output |
| File operations | Creative tasks |
| Data fetching (curl) | Code review/generation |

---

## Recipe Composition

Recipe composition allows recipes to invoke other recipes as sub-workflows. This enables modular, reusable workflow components.

### Basic Syntax

```yaml
- id: "run-sub-recipe"
  type: "recipe"
  recipe: "path/to/sub-recipe.yaml"
  context:
    variable_name: "{{parent_variable}}"
  output: "sub_result"
```

### How It Works

1. **Parent recipe** encounters a `type: "recipe"` step
2. **Context is prepared** - Only explicitly passed variables are included
3. **Sub-recipe loads** - Recipe file is parsed and validated
4. **Agents are scoped** - see [Agent Isolation](#agent-isolation) below
5. **Sub-recipe executes** - Runs with isolated context
6. **Results return** - Sub-recipe's final context becomes the step's output
7. **Parent continues** - Output available via `output` variable

### Agent Isolation

**A sub-recipe's `schema_version` is its own.** A sub-recipe that declares
`schema_version: 2` resolves every `agent:` reference from its own
`dependencies:` closure, exactly as it would if you invoked it directly —
whether the parent is `schema_version: 2` or a legacy recipe with no
`schema_version` at all.

```yaml
# parent.yaml — legacy, no schema_version: its OWN agent steps are caller-bound
steps:
  - id: "audit"
    type: "recipe"
    recipe: "repo-audit.yaml"   # schema_version: 2 → resolves from ITS OWN closure
```

Consequences worth knowing:

- **A v2 sub-recipe is portable through a legacy parent.** It runs under a
  bundle that carries none of the agents it names, because it brings its own.
- **Closures are not inherited or intersected.** A v2 sub-recipe of a v2
  parent gets its own catalog, not the parent's and not the overlap; each
  recipe declares what it needs.
- **There is no fallback.** If a v2 sub-recipe's closure cannot be resolved
  (unreachable dependency, undeclared agent, runner library unavailable), the
  step fails. It never silently reverts to the caller's agent map — that
  would resolve a *different* agent while reporting success.
- **A legacy sub-recipe is unchanged**: caller-bound, resolving from whatever
  coordinator the parent is running against.

### Context Isolation

**Critical design principle:** Sub-recipes receive ONLY the context explicitly passed to them.

```yaml
# Parent recipe
context:
  file_path: "src/auth.py"
  api_key: "secret-123"      # Sensitive - should NOT leak

steps:
  - id: "security-audit"
    type: "recipe"
    recipe: "security-audit.yaml"
    context:
      target: "{{file_path}}"  # Only this is passed
    output: "audit_result"
    # api_key is NOT available to sub-recipe
```

**Why context isolation?**
- Prevents accidental exposure of sensitive data
- Makes sub-recipes predictable (same inputs → same outputs)
- Enables testing sub-recipes in isolation
- Follows security principle of least privilege

#### Sub-Recipe Output Structure

When a parent recipe calls a sub-recipe, the step's `output` variable receives the
sub-recipe's **full context** — all variables including `recipe`, `session`, `step`,
and every step output defined in the sub-recipe.

To access a specific step's output from the sub-recipe, use nested dot notation:

```yaml
# If sub-recipe has a step with output: "result"
# And the parent step has output: "iter_out"
# Then access fields as:
#   "{{iter_out.result.field}}"   # CORRECT
#   "{{iter_out.field}}"          # WRONG — "field" is not a top-level context key
```

**Pattern:** `{{parent_output_name.sub_step_output_name.field}}`

### Recursion Protection

Recipe composition includes built-in protection against runaway recursion.

**Limits (configurable via `recursion` field):**
- `max_depth`: Maximum nesting depth (default: 5, range: 1-20)
- `max_total_steps`: Maximum steps across all recipes (default: 100, range: 1-1000)

**Example configuration:**
```yaml
name: "orchestrator"
recursion:
  max_depth: 10        # Allow deep nesting
  max_total_steps: 200 # Allow more total steps
```

**Step-level override:**
```yaml
- id: "deep-analysis"
  type: "recipe"
  recipe: "analysis.yaml"
  recursion:
    max_depth: 3  # Override for this specific invocation
```

**Error on limit exceeded:**
```
RecursionError: Recipe recursion depth 6 exceeds limit 5.
Recipe stack: main.yaml → sub1.yaml → sub2.yaml → sub3.yaml → sub4.yaml → sub5.yaml
```

### Complete Example

**Main recipe (code-review.yaml):**
```yaml
name: "comprehensive-code-review"
description: "Multi-stage review with reusable sub-recipes"
version: "1.0.0"

context:
  file_path: ""

recursion:
  max_depth: 5
  max_total_steps: 150

steps:
  - id: "security-audit"
    type: "recipe"
    recipe: "audits/security-audit.yaml"
    context:
      target_file: "{{file_path}}"
      severity_threshold: "high"
    output: "security_findings"

  - id: "performance-audit"
    type: "recipe"
    recipe: "audits/performance-audit.yaml"
    context:
      target_file: "{{file_path}}"
    output: "performance_findings"

  - id: "synthesize"
    agent: "foundation:zen-architect"
    prompt: |
      Synthesize findings:
      Security: {{security_findings}}
      Performance: {{performance_findings}}
    output: "final_report"
```

**Sub-recipe (audits/security-audit.yaml):**
```yaml
name: "security-audit"
description: "Focused security analysis"
version: "1.0.0"

context:
  target_file: ""
  severity_threshold: "medium"

steps:
  - id: "scan"
    agent: "foundation:security-guardian"
    prompt: "Scan {{target_file}} for vulnerabilities at {{severity_threshold}} severity"
    output: "scan_results"

  - id: "classify"
    agent: "foundation:security-guardian"
    prompt: "Classify findings: {{scan_results}}"
    output: "classified_findings"
```

### Interaction with Other Features

**With conditions:**
```yaml
- id: "optional-deep-scan"
  condition: "{{needs_deep_scan}} == 'true'"
  type: "recipe"
  recipe: "deep-scan.yaml"
  context:
    target: "{{file_path}}"
```

**With foreach:**
```yaml
- id: "audit-each-file"
  foreach: "{{files}}"
  as: "current_file"
  type: "recipe"
  recipe: "single-file-audit.yaml"
  context:
    file: "{{current_file}}"
  collect: "all_audits"
```

**With parallel:**
```yaml
- id: "parallel-audits"
  foreach: "{{audit_types}}"
  as: "audit_type"
  parallel: true
  type: "recipe"
  recipe: "{{audit_type}}-audit.yaml"
  context:
    target: "{{file_path}}"
  collect: "audit_results"
```

### Error Handling

Sub-recipe errors propagate to the parent:
- If a step in sub-recipe fails, the sub-recipe step fails
- Parent recipe's error handling applies (`on_error` field)
- Error messages include the recipe stack for debugging

```yaml
- id: "risky-audit"
  type: "recipe"
  recipe: "experimental-audit.yaml"
  context:
    target: "{{file_path}}"
  on_error: "continue"  # Don't fail parent if sub-recipe fails
```

### Best Practices

1. **Keep sub-recipes focused** - Single responsibility, reusable
2. **Document context requirements** - Clear about what variables are expected
3. **Use meaningful outputs** - Name outputs descriptively
4. **Set appropriate limits** - Adjust recursion limits based on workflow needs
5. **Test sub-recipes independently** - Each should work on its own

---

## Validation Rules

The tool-recipes module validates recipes before execution:

### Recipe-Level Validation

- [ ] `name` present and valid format
- [ ] `description` present
- [ ] `version` present and valid semver
- [ ] `steps` list not empty
- [ ] All step IDs unique

### Step-Level Validation

- [ ] `id` present and unique
- [ ] `agent` present and available
- [ ] `prompt` present and non-empty
- [ ] `condition` contains at least one variable if present
- [ ] `timeout` positive integer if present
- [ ] `retry.max_attempts` positive if present
- [ ] `on_error` valid value if present
- [ ] `depends_on` references existing step IDs
- [ ] No circular dependencies

### Variable Validation

- [ ] Template variables have valid syntax
- [ ] Referenced variables will be available at runtime
- [ ] No conflicts with reserved variable names

### Runtime Validation

Before execution:
- [ ] All referenced agents installed and available
- [ ] All context variables defined or will be defined by prior steps
- [ ] Session directory writable
- [ ] No conflicting sessions for same recipe

### Agent Availability Validation

The recipe validator can check if agents are available via the coordinator:

```python
from amplifier_module_tool_recipes.validator import validate_recipe

# Pass coordinator to enable agent availability checking
result = validate_recipe(recipe, coordinator=coordinator)

if result.warnings:
    # Agent availability issues are warnings (not errors)
    # since availability may vary by environment
    for warning in result.warnings:
        print(f"Warning: {warning}")
```

**How agent validation works:**
1. The validator checks `coordinator.available_agents` property
2. If the property exists, it compares agent names in the recipe against available agents
3. Unavailable agents generate warnings (not errors) since:
   - Agent availability varies by environment and bundle configuration
   - The agent may be available at runtime even if not at validation time

**For the coordinator to support agent validation:**
- Provide an `available_agents` property or method
- Return a list/set/dict of available agent names (including namespace)
- Example: `["foundation:zen-architect", "foundation:bug-hunter", ...]`

**Best practice:** Always use namespaced agent references (`foundation:agent-name`) to make bundle dependencies explicit and enable accurate validation.

---

## Complete Example

```yaml
name: "comprehensive-code-review"
description: "Multi-stage code review with security, performance, and maintainability analysis"
version: "2.1.0"
author: "Platform Team <platform@example.com>"
created: "2025-11-01T10:00:00Z"
updated: "2025-11-18T14:30:00Z"
tags: ["code-review", "security", "performance", "python"]

context:
  file_path: ""              # Required input
  severity_threshold: "high"  # Default severity level
  auto_fix: false            # Whether to auto-apply fixes

steps:
  - id: "security-scan"
    agent: "foundation:security-guardian"
    prompt: |
      Perform security audit on {{file_path}}
      Focus on severity: {{severity_threshold}}
    output: "security_findings"
    timeout: 600
    retry:
      max_attempts: 3
      backoff: "exponential"

  - id: "performance-analysis"
    agent: "foundation:performance-optimizer"
    prompt: "Analyze {{file_path}} for performance bottlenecks"
    output: "performance_findings"
    timeout: 600

  - id: "maintainability-review"
    agent: "foundation:zen-architect"
    mode: "REVIEW"
    prompt: |
      Review {{file_path}} for:
      - Code complexity
      - Philosophy alignment
      - Maintainability
    output: "maintainability_findings"
    timeout: 300

  - id: "synthesize-findings"
    agent: "foundation:zen-architect"
    mode: "ARCHITECT"
    prompt: |
      Synthesize findings from:

      Security: {{security_findings}}
      Performance: {{performance_findings}}
      Maintainability: {{maintainability_findings}}

      Prioritize by severity and provide actionable recommendations.
    output: "synthesis"
    timeout: 300

  - id: "generate-report"
    agent: "foundation:zen-architect"
    mode: "ANALYZE"
    prompt: |
      Create comprehensive review report:

      File: {{file_path}}
      Recipe: {{recipe.name}} v{{recipe.version}}
      Session: {{session.id}}

      Findings: {{synthesis}}

      Format as markdown with executive summary and detailed sections.
    output: "final_report"
    on_error: "continue"  # Report generation is non-critical
```

---

## Tool Result Output

When a recipe completes, the recipes tool returns a **compact summary** rather than the full accumulated context. This prevents oversized tool results that can break session resumption for complex workflows.

### What Gets Returned

The tool result includes:

```yaml
status: "completed"
recipe: "recipe-name"
session_id: "uuid"
summary:
  session: { id, project, started }
  recipe_metadata: { name, version }
  final_output: "..." # See priority below
  final_output_key: "key_name" # If from last step
  available_outputs: ["key1", "key2", ...] # All context keys
  full_results_location: "Full results saved in recipe session: ..."
```

### Final Output Priority

The `final_output` in the summary is determined by this priority:

1. **Explicit `final_output` key** (recommended) - If your recipe sets a context key named `final_output`, that value is returned
2. **Last step's output** - If no explicit `final_output`, the last step's `output` variable is used
3. **Available outputs list** - If neither exists, only the list of available keys is returned

### Using `final_output` (Recommended Pattern)

For recipes that need to return specific output to the caller, use `final_output` as your context key:

```yaml
steps:
  - id: "analyze"
    prompt: "Analyze {{file_path}}"
    output: "analysis"

  - id: "synthesize"
    prompt: "Create final report from {{analysis}}"
    output: "final_output"  # <-- This will be returned in tool result
```

Or copy a specific output to `final_output` in your last step:

```yaml
  - id: "prepare-output"
    type: "bash"
    command: "echo '{{detailed_report}}'"
    output: "final_output"
```

### Output Truncation

Large outputs are automatically truncated to prevent context overflow:

- **Strings**: Truncated at ~10KB with `[... truncated, see session for full output]`
- **Dicts/Lists**: Returns `{_truncated: true, _preview: "...", _full_size_bytes: N}`

### Accessing Full Results

Full results are always saved in the recipe session files. Use `recipes list` to find sessions, or access files directly at:

```
~/.amplifier/projects/{project}/recipe-sessions/{session-id}/
├── state.json      # Full context
├── checkpoint.json # Latest checkpoint
└── ...
```

---

## Schema v2 — Dependency Manifests

> **Shipped and executable. The underlying seam contract is still status DRAFT.**
>
> Schema v2 is the recommended format for any recipe with `agent:` steps, and it
> runs today: the `recipes` tool routes a `schema_version`-declaring recipe
> through the runner library for manifest parse and dependency resolution, then
> executes it on the proven step engine with a closed-world agent catalog. The
> format is exercised by the migrated recipes, templates and examples shipped in
> this repository.
>
> What remains DRAFT is the *contract*, not the availability of the feature —
> see "What is still DRAFT" below for the specific behaviors documented from the
> contract rather than verified end to end here.
>
> This section is authored from the seam contract
> [`contracts/recipe-dependency-manifest.v1.md`](../contracts/recipe-dependency-manifest.v1.md)
> (status DRAFT, 2026-09-01). Every behavior below is contract-backed and cited
> to its clause.
>
> **Manifest key spellings are verified.** The earlier caveat — that the key
> names here had never been checked against a parser — is resolved.
> `schema_version`, `dependencies`, `dependencies[].source`,
> `dependencies[].kind` and `dependencies[].required_agents` match the shipped
> parser `amplifier_recipe_runner.manifest` (landed under work item
> `recipes-yh0`, now resolved) and are additionally exercised by the 18 migrated
> recipe/template/example files in this repository (`recipes-5we`). The
> top-level `agents` alias map matches the same parser's `_parse_agent_aliases`,
> verified by parsing this section's alias example and its worked example
> through `parse_manifest` (`recipes-vci`).
>
> **What is still DRAFT.** The contract itself is status DRAFT, and the
> behaviors described below that live *outside* manifest parsing —
> preflight ordering and its typed failures (Core 6), resume and provenance
> records (Core 7), and capability *enforcement* during execution (Core 9) — are
> documented from the contract and are **not** verified here against an
> end-to-end run. The Core 9 *intersection* itself is an exception: the
> top-level `capabilities` key and the three-way intersection it feeds are
> parser- and planner-verified (`recipes-54n`) — see
> [Capabilities](#capabilities).
> The lock spellings (`locked` / `update-lock` / `unlocked`, `lock_version: 1`)
> match the shipped runner's `api.LockMode` and `lockfile.LOCK_VERSION` by
> inspection; their runtime semantics are likewise unverified here.
>
> Everything earlier in this document describes the **v1 field set**, which v2
> recipes use unchanged — v2 adds the manifest header, it does not replace the
> recipe body. A recipe that does not declare `schema_version` is a *legacy
> recipe* and keeps its existing caller-bound behavior — see
> [Legacy recipes](#legacy-recipes-no-schema_version) below.

**Citation key.** `manifest.v1 Core N` refers to numbered invariant N in
`contracts/recipe-dependency-manifest.v1.md`. `runner-lib.v1 Core N` refers to
`contracts/recipe-runner-lib.v1.md`. Nothing is documented here that those
contracts do not state.

### What v2 changes

In v1 a recipe borrows its agent catalog from whatever session invokes it. A
recipe that works in one caller bundle silently fails — or silently resolves a
*different* agent — in another.

Schema v2 makes a recipe a **self-contained, lockable dependency root**: it
declares the bundles and behaviors it needs, and its agents resolve *only* from
that declared closure. The same recipe run through the runner library, the
standalone `recipe-runner` CLI, or the Amplifier `recipes` tool resolves to the
same dependency provenance and the same agent catalog
(`runner-lib.v1` Conformance/GOOD).

### Recipe shape at a glance

```yaml
schema_version: 2               # Required for v2 (manifest.v1 Core 1)

name: string                    # As in v1
description: string
version: string
# ... all other v1 top-level fields are unchanged

dependencies:                   # Required for v2 (manifest.v1 Core 1)
  - source: string              # Foundation-resolvable URI (manifest.v1 Core 2)
    kind: bundle | behavior     # (manifest.v1 Core 2)
    required_agents:            # Optional (manifest.v1 Core 2)
      - "namespace:agent-name"

capabilities:                   # Optional (manifest.v1 Core 9)
  - capability-name             # Absent means the recipe declares NONE

agents:                         # Optional alias map (manifest.v1 Core 3)
  alias-name: "namespace:agent-name"

steps: list[Step]               # As in v1, but `agent:` resolves closed-world
```

### `schema_version` (required for v2)

**Type:** integer
**Value:** `2`

A portable recipe declares `schema_version: 2` **and** a `dependencies` block. A
recipe with neither is a *legacy recipe* (`manifest.v1` Core 1, Core 10).

**Unknown manifest keys are a parse ERROR, never silently ignored**
(`manifest.v1` Core 1). This is the sharpest behavioral break from v1: a typo in
a manifest key fails the recipe rather than being dropped.

Values above `2` are **Reserved** (`manifest.v1` Reserved) — do not use them.

### `dependencies`

**Type:** list of dependency entries
**Required for v2** (`manifest.v1` Core 1)

Each entry declares one Foundation-resolvable bundle or behavior source
(`manifest.v1` Core 2).

| Field | Type | Required | Contract | Meaning |
|-------|------|----------|----------|---------|
| `source` | string | yes | Core 2 | Foundation-resolvable bundle/behavior source URI. Partial behavior bundles are addressed with the `#subdirectory=` fragment. |
| `kind` | string | — | Core 2 | `bundle` or `behavior`. **v1 of this contract permits no other values**; anything else is Reserved. |
| `required_agents` | list[string] | no | Core 2 | Canonical `namespace:name` agents this dependency must supply. Verified in preflight (Core 6). |

```yaml
dependencies:
  # A whole bundle
  - source: "git+https://github.com/microsoft/amplifier-bundle-foundation@main"
    kind: bundle
    required_agents:
      - "foundation:zen-architect"

  # A partial behavior bundle, addressed with #subdirectory=
  - source: "git+https://github.com/example/some-bundle@v1.2.0#subdirectory=behaviors/review.yaml"
    kind: behavior
```

A behavior-partial dependency **composes only its declared contribution** —
not the whole bundle it lives in (`manifest.v1` Conformance/GOOD).

### Agent references and aliases

A step's `agent:` reference — either an **alias** or a canonical
`namespace:name` — resolves **only** from the recipe's declared dependency
closure plus the runner baseline. It never resolves from the caller session's
agent map (`manifest.v1` Core 3).

Two consequences worth internalizing:

- **Undeclared is unresolved.** Referencing an agent no declared dependency
  supplies is a preflight failure, not a runtime surprise (`manifest.v1`
  Core 6).
- **No namespace inference.** Writing `agent: "foundation:zen-architect"` does
  *not* declare a Foundation dependency. The runner never infers dependencies
  from agent-name namespaces (`manifest.v1` Core 11). You must declare the
  dependency explicitly.

**Alias declaration — the top-level `agents` map.** Aliases are declared once,
at the recipe's top level, in an `agents` mapping from **bare alias** to
**canonical `namespace:name`** (`manifest.v1` Core 3). This is the spelling the
shipped parser implements.

| Side | Rule |
|------|------|
| key (the alias) | Non-empty string that **must not contain `:`** — an alias is a bare name. |
| value (the canonical name) | Non-empty string with **exactly one `:`**, both halves non-empty: `namespace:name`. |

Two placement rules follow from Core 1, and both are parse ERRORs rather than
quiet no-ops:

- `agents` is a **manifest key**. Declaring it without `schema_version: 2` is an
  error naming it, not a silently ignored block.
- `agents` is **not** a per-dependency key. A `dependencies` entry accepts only
  `source`, `kind` and `required_agents`; any other key there — including
  `aliases` — is an error naming the offending key.

```yaml
schema_version: 2

dependencies:
  - source: "git+https://github.com/microsoft/amplifier-bundle-foundation@main"
    kind: bundle
    required_agents:
      - "foundation:zen-architect"

agents:                            # top-level alias map (Core 3)
  architect: "foundation:zen-architect"

steps:
  - id: "review"
    agent: "architect"             # alias — resolved from the closure
    prompt: "..."
```

#### `agent: self` is refused under v2

`self` is a **legacy** pseudo-agent meaning *"spawn the calling session's own
agent"*. In a legacy (no `schema_version`) recipe it keeps working exactly as it
always has. Under `schema_version: 2` it is **refused at preflight**, with
`SelfAgentUnsupportedError`:

```text
Agent 'self' referenced by step 'validate_inputs' cannot be resolved in a
`schema_version: 2` recipe: `self` is a legacy pseudo-agent meaning "spawn the
calling session's own agent", and no `dependencies:` block can supply it — it
names no bundle agent.

Remedy: Name the agent this step actually runs as. This recipe's closure
supplies: foundation:explorer, foundation:zen-architect. …
```

**Why it is refused rather than exempted.** `self` is not an undeclared name the
closure happens to be missing — it is a name no `dependencies:` block *can*
supply. And it is not undefined: the host's spawner defines it as an **empty
overlay merged onto the parent session's config**, and in a v2 run that parent
session is the **caller's**. Admitting `self` would therefore restore the
caller's entire world — agent map included — for that one step, with no error,
no warning and no provenance entry to show the closure had been bypassed. That
is precisely what `manifest.v1` Core 3/5 exist to prevent, and it would be
strictly worse than a loud refusal.

**Two supported alternatives:**

| You want | Do this |
|----------|---------|
| The step to run as a specific agent | Name that agent, and declare it under a dependency's `required_agents`. Its identity is then resolved, recorded in provenance and attributable like every other step's. |
| The step to genuinely run as the *calling* session's agent | Keep the recipe legacy: omit `schema_version: 2`. `self` retains its caller-bound meaning, labeled rather than smuggled. |

An alias does **not** get around this: `agents: {self: foundation:explorer}` is
refused too. It would leave `agent: self` in the file reading as the legacy
caller-bound meaning while doing something else, and a reader could not tell the
two apart.

The refusal is enforced twice — at plan time (the planner) and at resolve time
(the agent catalog, the last hop before a spawn). The pair is deliberate: a
preflight that is the *only* guard is one exemption away from silently reopening
the closed world.

### Isolation and precedence

**Isolation by default.** The runner builds the execution session *exclusively*
from the declared dependency closure plus the runner baseline (`manifest.v1`
Core 4). There are **no host imports** in v1 beyond five explicitly named runner
ports:

| Port | Contract |
|------|----------|
| workspace path | `manifest.v1` Core 4, `runner-lib.v1` Core 4 |
| approved provider access | `manifest.v1` Core 4, `runner-lib.v1` Core 4 |
| approval callback | `manifest.v1` Core 4, `runner-lib.v1` Core 4 |
| event sink | `manifest.v1` Core 4, `runner-lib.v1` Core 4 |
| cancellation | `manifest.v1` Core 4, `runner-lib.v1` Core 4 |

No port grants the host's ambient agent map to the recipe
(`runner-lib.v1` Core 4).

**Collision is an error — there is no precedence rule to learn.** Duplicate
agent names across the dependency closure are a **preflight ERROR**
(`manifest.v1` Core 5). There is no last-wins, no first-wins, no shadowing: two
dependencies supplying the same agent name is a failure you must resolve by
changing the declaration.

Correspondingly, a **caller agent with a colliding name can never satisfy,
alter, or override a recipe dependency** (`manifest.v1` Core 5,
Conformance/BAD). Caller-side composition cannot influence a recipe's result.

### Preflight

Preflight runs **before any side effects** (`manifest.v1` Core 6):

1. **Trust policy is enforced before any remote fetch or module activation.** A
   dependency requiring a trust-policy-disallowed module refuses *before*
   activation (`manifest.v1` Core 6, Conformance/BAD; `runner-lib.v1` Core 6).
2. **Every declared dependency and every referenced agent is verified before
   any recipe step executes** (`manifest.v1` Core 6).
3. **A missing declaration fails naming the undeclared reference and the
   remedy** (`manifest.v1` Core 6).

Preflight failures — undeclared agent, `agent: self` (see
[`agent: self` is refused under v2](#agent-self-is-refused-under-v2)), collision,
trust refusal, provenance mismatch — are **distinct, typed errors** raised before
recipe steps run. A missing artifact or refused dependency is a real result,
never a fabricated success (`runner-lib.v1` Core 8).

### Lock modes

A lockfile is **optional and generated** (`manifest.v1` Core 8).

| Mode | Behavior | Contract |
|------|----------|----------|
| `locked` | **Default for CI.** Requires exact lock entries. | Core 8 |
| `update-lock` | Rewrites the lock **explicitly**. | Core 8 |
| `unlocked` | **Interactive-only**, and emits a warning. | Core 8 |

**Locks are never updated silently on run** (`manifest.v1` Core 8). If you want
a lock rewritten, you ask for it with `update-lock`.

CI-mode execution requires locked immutable refs (`runner-lib.v1` Core 6).

Lockfile `lock_version` values above `1` are **Reserved** (`manifest.v1`
Reserved).

### Resume and provenance

Every run records (`manifest.v1` Core 7):

- the recipe digest
- each declared URI/ref
- the resolved immutable revision / content digest
- included partials
- the agent-to-dependency provenance map
- runner and Foundation versions
- the effective trust and capability policy

`plan`/`run` expose this resolved graph in a stable, documented shape
(`runner-lib.v1` Core 7).

**Resume uses recorded provenance.** A provenance mismatch **fails visibly** and
never silently re-resolves (`manifest.v1` Core 8) — a locked resume that sees a
different resolved revision is a failure (`manifest.v1` Conformance/BAD).

This holds for the `recipes` tool's own `resume` too, on both of its engines.
Before it hands the run to either, it re-plans the recipe and compares that
plan against the record above; a difference is refused as
`V2ProvenanceMismatchError`, naming what moved (the recipe digest, a
dependency's resolved revision, or the source supplying an agent), both
values, and the remedy — re-run with `execute`, or restore what was recorded.
Nothing runs. Continuing would execute the run's *remaining* steps against a
different closure than its *completed* ones, and report success either way.

Two deliberate non-refusals, both audible rather than silent: a session
recorded before this record existed, and a closure that cannot be re-resolved
at this moment, are **resumed with a warning** (logged, and readable on the
result as `provenance_warning`) rather than stranded. A failed re-plan is not
reported as drift — the resume itself re-plans and reports that failure as
itself. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#error-v2provenancemismatcherror-on-resume).

#### The per-agent provenance record

A declared bundle composes its own `includes`, so **the dependency you declared
is often not the tree that defines the agent you got.** Planning
`generate-recipe-docs.yaml` (one declared dependency, `amplifier-foundation`)
yields 39 agents drawn from **11 different checkouts** at 11 different commits.
Recording one dependency's revision against all 39 would state something false
about 26 of them, so the record separates the two facts:

| Field | What it names |
| --- | --- |
| `supplied_by` | The source tree that **defines** the agent — a declared dependency's URI, or, for one reached through includes, that included bundle's own URI. Falls back to the tree's local path if the resolver recorded no URI. |
| `declared_by` | The **declared dependency** the agent entered the closure through. Always one of the recipe's own `dependencies` — this is what a resume re-resolves. |
| `via_includes` | The include path from `declared_by`'s bundle to the defining tree, nearest first (e.g. `["amplifier-tester", "amplifier-tester-behavior", "digital-twin-universe"]`). Empty when the defining tree is itself a declared dependency. |
| `defined_in` | Root of `supplied_by`'s tree. `local_path` always lies inside it. |
| `resolved_revision` / `dependency_digest` | Identity of the **defining** tree — the commit the file at `local_path` actually came from, not the declared dependency's. |
| `local_path` | The agent's own definition file. |
| `alias` | The alias a step referenced, when it used one. |

An agent whose definition file no reported tree holds records `defined_in:
null` and an empty `via_includes`: nothing is claimed, rather than the reaching
dependency's tree being asserted by default.

```json
{
  "agent": "superpowers:implementer",
  "supplied_by": "git+https://github.com/microsoft/amplifier-bundle-superpowers@main",
  "declared_by": "git+https://github.com/microsoft/amplifier-foundation@v2.1.2",
  "via_includes": ["superpowers-methodology-behavior"],
  "defined_in": "/…/cache/amplifier-bundle-superpowers-1e7a6ff3d51f6d25",
  "local_path": "/…/cache/amplifier-bundle-superpowers-1e7a6ff3d51f6d25/agents/implementer.md",
  "resolved_revision": "47d43aa1dad1560e98286a77bcda113f099d2e64"
}
```

`conformance/analyze_provenance.py` checks the claim rather than the field's
presence: every agent's `local_path` must lie inside its `defined_in`, and
every `declared_by` must be a dependency the recipe actually declared.

```bash
PYTHONPATH=src python3 conformance/analyze_provenance.py --assert
```

**Compatibility.** `via_includes` was a boolean before it was a path; a manifest
carrying the boolean form reads back as an empty path, because "a chain
existed" does not name one. `declared_by` is absent in records written before
it existed and stays absent — it is not back-filled from `supplied_by`.

### Capabilities

Effective capabilities are the **intersection** of three inputs
(`manifest.v1` Core 9):

```
effective = host policy ∩ runner policy ∩ manifest-declared needs
```

A manifest cannot widen what the host or runner policy allows.

#### `capabilities` (optional)

**Type:** list of strings
**Optional** — a recipe that omits it declares **no** capability needs.

The top-level `capabilities` list is the recipe's own term of that
intersection — the "manifest-declared needs" the contract names. It is the only
term a recipe author controls; the other two come from the host and from the
runner's trust policy.

```yaml
schema_version: 2

dependencies: []

capabilities:                     # manifest term of the Core 9 intersection
  - net
  - fs.read
```

**Absent and `capabilities: []` mean the same thing: declares none.** Because an
intersection can never add, a recipe that asks for nothing is granted nothing —
no matter how permissive the host and runner policies are. There is deliberately
**no manifest spelling for "unconstrained"**; only the host and runner terms can
be unconstrained. A manifest that could opt out of the intersection would not be
a term of one.

| Declaration | Host term | Runner term | Effective |
|-------------|-----------|-------------|-----------|
| `[net, fs, exec]` | `[fs, exec]` | `[net, fs]` | `[fs]` |
| `[net, exec]` | `[fs]` | `[net, exec]` | `[]` (a manifest cannot widen) |
| absent, or `[]` | unconstrained | unconstrained | `[]` |
| `[net, fs]` | unconstrained | unconstrained | `[fs, net]` |

Parse rules, all ERRORs rather than quiet no-ops (`manifest.v1` Core 1, Core 9):

- `capabilities` is a **manifest key** — declaring it without `schema_version: 2`
  is an error naming it, not a silently ignored block.
- It must be a **list**; entries must be **non-empty strings**.
- A **duplicate entry is an error** naming both positions. A list the author
  wrote twice is never silently collapsed.
- It is **not** a per-dependency key. A `dependencies` entry accepts only
  `source`, `kind` and `required_agents`.

**Verified.** The key spelling and the rules above match the shipped parser
`amplifier_recipe_runner.manifest` (this section's example is parsed through
`parse_manifest`), and the three-way intersection reaching
`ExecutionPlan.policy.capabilities` is exercised by the runner library's planner
suite (work item `recipes-54n`). What remains unverified here is end-to-end
*enforcement* of a granted capability during execution.

### `agent_config` under v2

In v1, `agent_config` is parsed but ignored. Under this schema it must be
**either implemented or rejected at parse — never silently retained inert**
(`manifest.v1` Core 12). The shipped parser takes the second option: a step
declaring `agent_config` under `schema_version: 2` is a parse ERROR naming the
step and the clause. Staged steps and nested `foreach` / `while` step bodies are
walked as well, so it cannot hide in a sub-step. Declare the agent's dependency
in `dependencies` instead — v1's silent-no-op behavior does not carry over.

### Legacy recipes (no `schema_version`)

A recipe without `schema_version` and `dependencies` is a **legacy recipe**
(`manifest.v1` Core 1). Legacy mode is **labeled and confined**
(`manifest.v1` Core 10):

| Surface | Legacy recipe behavior |
|---------|------------------------|
| Amplifier `recipes` tool adapter | **Runs**, in explicitly labeled caller-bound mode, with a **deprecation warning**. Behavior is byte-identical to pre-contract behavior — *including* failing when the caller lacks a referenced agent. |
| Standalone `recipe-runner` CLI | **Rejected**, with an actionable error. |
| Runner library (isolated execution) | Not a legacy path — legacy runs *only* through the embedded tool adapter. |

The boundary is deliberate: legacy recipes keep working exactly where they
already worked, they are never silently upgraded, and they cannot be run from
the portable surfaces that promise isolation.

**How a legacy recipe announces itself.** Three surfaces, one cause:

| Surface | What you see |
|---------|--------------|
| Validation | `RECIPE_LEGACY_AGENT_REFS` — a WARNING naming every namespaced agent the recipe references and the remedy. Emitted by `validate_recipe`, surfaced by `validate-recipes.yaml`. Exempt: `agent: self`, bash-only recipes, and files under a `fixtures/` directory. |
| Execution | The result is labeled `legacy-caller-bound`, with a deprecation warning. |
| Failure | `Agent 'foundation:git-ops' not found in configuration`, from a caller whose bundle does not expose it. |

**The remedy is always the header, never a rename.** Renaming the agents to
whatever the running bundle happens to expose, or forking a local copy of a
shipped recipe, binds the recipe *harder* to one caller and leaves the shipped
file broken for everyone else. Declare the dependency and fix the shipped recipe.

### Worked example

A v2 recipe declaring a Foundation bundle dependency and using both an aliased
and a canonical agent reference. This recipe runs identically from a caller
bundle that has no Foundation agents at all (`manifest.v1` Conformance/GOOD).

```yaml
schema_version: 2

name: "architecture-review"
description: "Review a design document against architectural principles"
version: "1.0.0"
tags: ["review", "architecture"]

dependencies:
  - source: "git+https://github.com/microsoft/amplifier-bundle-foundation@main"
    kind: bundle
    required_agents:
      - "foundation:zen-architect"

agents:                               # top-level alias map (Core 3)
  architect: "foundation:zen-architect"

context:
  design_doc: "docs/DESIGN.md"

steps:
  # Aliased reference — resolves from the declared closure only (Core 3)
  - id: "review-design"
    agent: "architect"
    prompt: |
      Review the design document at {{design_doc}}.

      Assess: module boundaries, contract clarity, and whether any
      abstraction exists without a second caller justifying it.

      Return findings as a numbered list, most severe first.
    output: "findings"

  # Canonical reference to the same agent — equally valid (Core 3)
  - id: "summarize"
    agent: "foundation:zen-architect"
    prompt: |
      Summarize these findings into a one-paragraph verdict
      and a single recommended next action:

      {{findings}}
    output: "final_output"
```

**What preflight checks before step 1 runs** (`manifest.v1` Core 6):

- the Foundation dependency resolves under the active trust policy, *before*
  any fetch;
- `foundation:zen-architect` is actually supplied by it (`required_agents`);
- the alias `architect` resolves within the closure;
- no agent name collides across the closure (Core 5).

**What this recipe does *not* get:** any agent from the caller's session, even
one named `architect` or `foundation:zen-architect` (Core 3, Core 5).

### Not in v2 — named backlog

These are deliberately **out of scope** for v2, each with a named promotion
trigger (`manifest.v1` Backlogged). Do not write recipes that assume them:

| Capability | Promotion trigger |
|------------|-------------------|
| Constrained inline agents (instruction-only; no modules, hooks, or undeclared tools; `capabilities: []` mandatory) | First real recipe needing a recipe-private prompt-only role |
| Narrowly named host-import mechanism | First real embedding that cannot express a need via declared dependencies |
| Shared content-addressed cache dedup | Measured duplicate-cache cost |
| Signed archive / content-addressed dependency sources | First hermetic-distribution requirement |

### Reserved

Reserved by `manifest.v1` — do not use:

- `schema_version` values above `2`
- `dependencies[].kind` values beyond `bundle` / `behavior`
- Lockfile `lock_version` above `1`

---

## Schema Change History

### v1.7.0 — While Loops and Expression Enhancements

- **Added** `while_condition` step field for convergence-based iteration
- **Added** `max_while_iterations` step field (safety limit, default 100)
- **Added** `break_when` step field for early loop termination
- **Added** `update_context` step field for per-iteration state mutation
- **Added** `while_steps` step field for multi-step loop bodies
- **Added** `_loop_index` and `_loop_iteration` runtime loop metadata
- **Added** comparison operators: `<`, `>`, `<=`, `>=`
- **Added** `not` unary operator
- **Added** parentheses for expression grouping
- **Added** numeric-aware comparison (auto-detects numeric strings)
- **Added** boolean normalization for falsy values
- **Fixed** approval prompt variable resolution
- **Fixed** type-safe bool serialization (`true`/`false` instead of `True`/`False`)

### v1.6.0
- Bounded parallelism (`parallel: N` for integer concurrency limits)
- Recipe-level rate limiting (`rate_limiting` config)
- Global LLM concurrency control (`max_concurrent_llm`)
- Pacing between calls (`min_delay_ms`)
- Adaptive backoff on 429 errors (`backoff` config)

### v1.5.0
- Tool result output optimization (returns summary instead of full context)
- `final_output` context key convention for explicit output declaration
- Automatic truncation of large outputs in tool results
- Last step output fallback when `final_output` not specified

### v1.4.0
- Bash steps (`type: "bash"`) for direct shell execution without LLM overhead
- New bash-specific fields: `command`, `cwd`, `env`, `output_exit_code`
- Variable substitution in command, cwd, and env values
- Timeout and error handling for bash commands

### v1.3.0
- Recipe composition (`type: "recipe"` steps)
- Sub-recipe invocation with context isolation
- Recursion protection (`recursion` config, `max_depth`, `max_total_steps`)
- Step-level recursion overrides
- New step fields: `type`, `recipe`, `context` (for recipe steps)

### v1.2.0
- Parallel iteration (`parallel: true` on foreach steps)
- All iterations run concurrently with fail-fast behavior

### v1.1.0
- Looping and iteration (`foreach`, `as`, `collect`, `max_iterations`)
- Fail-fast iteration behavior

### v1.0.0 (Initial)
- Basic recipe structure
- Sequential step execution
- Context variables and template substitution
- Session persistence
- Conditional execution (`condition`)

---

**See Also:**
- [Recipes Guide](RECIPES_GUIDE.md) - Conceptual overview
- [Best Practices](BEST_PRACTICES.md) - Design patterns
- [Examples Catalog](EXAMPLES_CATALOG.md) - Working examples
