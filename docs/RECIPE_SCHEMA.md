# Recipe Schema Reference

**Complete YAML specification for Amplifier recipes**

This document defines the complete schema for recipe YAML files. Every field, constraint, and behavior is documented here.

## Overview

Recipes are declarative YAML specifications that define multi-step agent workflows. The tool-recipes module parses and executes these specifications.

**Schema Version:** 1.3.0

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
```

**Workflow:**

1. **Stage completes** → Recipe pauses at approval gate
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
```

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

#### `approval` (optional)

**Type:** ApprovalConfig object

**Purpose:** Define an approval gate that pauses execution after this stage completes.

**Structure:**
```yaml
approval:
  required: boolean       # Default: false
  prompt: string         # Required if required=true
  timeout: integer       # Seconds, 0=forever (default: 0)
  default: string        # "approve" or "deny" (default: "deny")
```

**Behavior:**
- If `required: false` or omitted, stage completes without pausing
- If `required: true`, execution pauses after stage and waits for approval
- User must explicitly approve or deny to continue
- On timeout, applies `default` action

**Example:**
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

**See also:** [Approval Gates](#approval-gates) for complete workflow details.

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
  timeout: integer              # Optional - Max execution time (seconds)
  retry: dict                   # Optional - Retry configuration
  on_error: string              # Optional - Error handling strategy
  depends_on: list[string]      # Optional - Step IDs that must complete first
```

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
- If no matches found, the pattern string is used as-is (provider will error if invalid)
- Resolution details are logged at DEBUG level

**Validation:**
- Only valid for agent steps (`type: "agent"` or default)
- Ignored if specified on bash or recipe steps (validation error)
- If `model` specified without `provider`, applies to the default (highest priority) provider

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

**Type:** list of `{provider, model}` objects
**Purpose:** Specify an ordered list of provider/model preferences with automatic fallback.

**How it works:**
- The system tries each provider in order until one is available
- First available provider is promoted to priority 0 (highest) for this step
- If no providers in the list are available, falls back to session default
- Each entry can include a model glob pattern that gets resolved

**This is the preferred approach** for production recipes that need resilience across different provider configurations.

**Example:**
```yaml
# Fallback chain: try Anthropic first, then OpenAI, then Azure
- id: "analyze"
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

**Entry fields:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `provider` | string | Yes | Provider ID (e.g., "anthropic", "openai") |
| `model` | string | No | Model name or glob pattern (e.g., "claude-haiku-*") |

**When to use `provider_preferences` vs `provider`/`model`:**

| Use Case | Recommended Approach |
|----------|---------------------|
| Single provider, simple use | `provider` + `model` (legacy) |
| Multi-provider fallback | `provider_preferences` |
| Production recipes | `provider_preferences` |
| CI/CD pipelines | `provider_preferences` |

**Validation:**
- Cannot be used together with `provider` or `model` fields (mutual exclusivity)
- Only valid for agent steps (`type: "agent"` or default)
- List cannot be empty
- Each entry must have a `provider` field

**Examples with different fallback strategies:**

```yaml
# Cost optimization: try cheap models first
- id: "quick-check"
  agent: "foundation:explorer"
  provider_preferences:
    - provider: anthropic
      model: claude-haiku-*
    - provider: openai
      model: gpt-4o-mini
  prompt: "Quick survey of the codebase"

# Quality optimization: prefer best reasoning models
- id: "complex-design"
  agent: "foundation:zen-architect"
  provider_preferences:
    - provider: anthropic
      model: claude-opus-*
    - provider: openai
      model: gpt-4o
  prompt: "Design a complex distributed system"

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

**Type:** integer (seconds)
**Default:** 600 (10 minutes)
**Purpose:** Prevent hanging on unresponsive steps.

**Examples:**
```yaml
- timeout: 300   # 5 minutes
- timeout: 1800  # 30 minutes for long-running analysis
```

**Behavior:**
- If step exceeds timeout, execution cancelled
- Error logged with clear timeout message
- Recipe can resume from checkpoint (step retries)

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

**Boolean normalization:** These values are treated as falsy: `false`, `False`, `""`, `"0"`, `"none"`, `"None"`.
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
```

If the command exceeds the timeout, it's killed and the step fails.

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
4. **Sub-recipe executes** - Runs with isolated context
5. **Results return** - Sub-recipe's final context becomes the step's output
6. **Parent continues** - Output available via `output` variable

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
