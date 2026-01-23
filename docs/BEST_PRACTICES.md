# Recipe Best Practices

**Strategic guidance for effective recipe design**

This document provides best practices for creating, maintaining, and using Amplifier recipes effectively.

## Table of Contents

- [Design Principles](#design-principles)
- [Recipe Structure](#recipe-structure)
- [Step Design](#step-design)
- [Context Management](#context-management)
- [Error Handling](#error-handling)
- [Performance](#performance)
- [Model Selection](#model-selection)
- [Reliability Patterns](#reliability-patterns)
- [Testing](#testing)
- [Maintenance](#maintenance)
- [Common Pitfalls](#common-pitfalls)

---

## Design Principles

### 1. Single Responsibility

**Each recipe should have one clear purpose.**

✅ **Good:**
```yaml
name: "security-audit"
description: "Comprehensive security analysis with vulnerability scanning"
```

❌ **Bad:**
```yaml
name: "code-analysis-and-refactoring-and-testing"
description: "Does everything related to code quality"
```

**Why:** Single-purpose recipes are easier to understand, test, and reuse. Complex workflows can compose multiple recipes.

### 2. Composability Over Complexity

**Prefer multiple simple recipes over one complex recipe.**

✅ **Good:**
- `security-audit.yaml` - Security scanning only
- `performance-audit.yaml` - Performance analysis only
- `full-audit.yaml` - Runs security-audit + performance-audit via recipe composition

❌ **Bad:**
- `mega-audit.yaml` - 20 steps covering everything

**Why:** Smaller recipes are easier to maintain, test, and reuse in different contexts.

### 3. Explicit Over Implicit

**Make dependencies and requirements clear.**

✅ **Good:**
```yaml
context:
  file_path: ""         # Required: path to file to analyze
  severity: "high"      # Optional: minimum severity (default: high)
  auto_fix: false       # Optional: apply fixes automatically (default: false)

# Usage example:
#   amplifier run "execute recipe.yaml with file_path=src/auth.py"
```

❌ **Bad:**
```yaml
context: {}  # User has to guess what's needed
```

**Why:** Clear requirements reduce errors and improve user experience.

### 4. Progressive Disclosure

**Start simple, add complexity only when needed.**

**Version 1.0:** Basic workflow
```yaml
steps:
  - id: "analyze"
    agent: "analyzer"
    prompt: "Analyze {{file}}"
```

**Version 1.1:** Add error handling when needed
```yaml
steps:
  - id: "analyze"
    agent: "analyzer"
    prompt: "Analyze {{file}}"
    timeout: 600
    retry:
      max_attempts: 3
```

**Why:** Simple recipes are easier to understand. Add complexity based on real needs, not speculation.

### 5. Fail-Fast Philosophy

**Detect problems early rather than late.**

✅ **Good:**
```yaml
steps:
  - id: "validate-inputs"
    agent: "validator"
    prompt: "Validate that {{file_path}} exists and is readable"
    # Fails fast if inputs invalid

  - id: "expensive-analysis"
    agent: "analyzer"
    prompt: "Deep analysis of {{file_path}}"
    # Only runs if validation passed
```

❌ **Bad:**
```yaml
steps:
  - id: "expensive-analysis"
    # Runs for 10 minutes...
    # THEN discovers file doesn't exist
```

**Why:** Fail fast saves time and provides better user experience.

---

## Sub-Recipe Modularization

Sub-recipes follow the "bricks and studs" philosophy: small, self-contained workflows with clear interfaces that snap together cleanly.

### The Core Question

> **"Would I name, test, and version this workflow independently?"**
>
> If yes → extract to a sub-recipe. If no → keep inline.

### When to Extract

**Extract a sub-recipe when:**

| Signal | Why It Matters |
|--------|----------------|
| **Clear independent purpose** | "security-audit" vs "step-2-prep" - if you can name it without referencing the parent, extract it |
| **Testable in isolation** | You want to verify this workflow works on its own |
| **Reused across recipes** | Multiple parent recipes call the same workflow |
| **Natural checkpoint** | Results are useful even if later steps fail |
| **Context boundary needed** | Parent has sensitive data the sub-workflow shouldn't see |
| **Cognitive load** | Parent recipe exceeds ~10 steps and becomes hard to reason about |
| **Different ownership** | Different teams maintain different parts |

**Keep steps inline when:**

| Signal | Why It Matters |
|--------|----------------|
| **Tightly coupled** | Steps are meaningless alone |
| **Single caller** | Only one recipe would ever use this |
| **Thin wrapper** | Would just pass through to another call |
| **Heavy context sharing** | Many variables flowing between steps |
| **Implementation detail** | "prepare-context-for-synthesis" isn't a workflow |

### Anti-Patterns

**Premature Extraction:**
```yaml
# ❌ Bad: Extracted before proving reuse
- type: "recipe"
  recipe: "analyze-structure.yaml"  # Only used here, one step inside

# ✅ Good: Keep inline until you have 2+ callers
- id: "analyze-structure"
  agent: "foundation:zen-architect"
  prompt: "Analyze {{file_path}}"
```

**Fragmentation:**
```yaml
# ❌ Bad: Natural flow split artificially
steps:
  - type: "recipe"
    recipe: "step1-scan.yaml"
  - type: "recipe"
    recipe: "step2-classify.yaml"
  - type: "recipe"
    recipe: "step3-report.yaml"

# ✅ Good: Keep cohesive workflows together
steps:
  - id: "scan"
    ...
  - id: "classify"
    ...
  - id: "report"
    ...
```

**Single-Step Sub-Recipes:**
```yaml
# ❌ Bad: Recipe overhead for one step
# validate-input.yaml contains just one agent call

# ✅ Good: Extract when there's actual workflow
# security-audit.yaml contains: scan → classify → prioritize → report
```

### Validation at Boundaries

When composing sub-recipes, validate at the seams:

```yaml
# ✅ Good: Validate outputs before passing to next sub-recipe
steps:
  - type: "recipe"
    recipe: "build-artifact.yaml"
    output: "build_result"

  - id: "validate-build"
    agent: "recipes:result-validator"
    prompt: "Verify build output is valid before deployment"
    output: "validation"

  - type: "recipe"
    recipe: "deploy-artifact.yaml"
    context:
      artifact: "{{build_result}}"
    condition: "{{validation.passed}}"
```

### Good Composition Example

See `examples/comprehensive-review.yaml` for a well-structured composition:
- Parent orchestrates high-level flow
- Sub-recipes (`code-review-recipe.yaml`, `security-audit-recipe.yaml`) are independently testable
- Clear context boundaries (only pass what sub-recipes need)
- Synthesis step combines results

---

## Recipe Structure

### Naming Conventions

**Recipe names:**
- Lowercase with hyphens
- Descriptive and specific
- Include domain if ambiguous

```yaml
✅ security-audit
✅ python-dependency-upgrade
✅ api-documentation-review

❌ audit
❌ upgrade
❌ review
```

**Step IDs:**
- Verb-noun format
- Descriptive of action
- Keep concise

```yaml
✅ analyze-security
✅ generate-report
✅ validate-results

❌ step1
❌ do-stuff
❌ analyze_security_vulnerabilities_and_generate_comprehensive_report
```

**Context variables:**
- Snake_case
- Descriptive
- Avoid abbreviations

```yaml
✅ file_path
✅ severity_threshold
✅ max_iterations

❌ fp
❌ sev_thresh
❌ maxIter
```

### Versioning

**Follow semantic versioning:**

- **MAJOR (1.x.x → 2.x.x):** Breaking changes
  - Different required inputs
  - Different output format
  - Incompatible behavior

- **MINOR (x.1.x → x.2.x):** Backward-compatible additions
  - New optional steps
  - New optional context variables
  - Enhanced functionality

- **PATCH (x.x.1 → x.x.2):** Bug fixes
  - Prompt improvements
  - Error handling fixes
  - Documentation updates

**Example:**
```yaml
# v1.0.0: Initial release
name: "code-review"
version: "1.0.0"

# v1.1.0: Added optional validation step (backward-compatible)
version: "1.1.0"

# v2.0.0: Changed required inputs (breaking change)
version: "2.0.0"
```

### Documentation

**Include helpful comments:**

```yaml
name: "security-audit"
description: "Comprehensive security analysis with vulnerability scanning"
version: "1.0.0"

# This recipe performs multi-stage security analysis:
# 1. Static analysis for common vulnerabilities
# 2. Dependency audit for known CVEs
# 3. Configuration review for security misconfigurations
#
# Typical runtime: 5-10 minutes
# Requires: security-guardian agent installed
#
# Usage:
#   amplifier run "execute security-audit.yaml with file_path=src/auth.py"
#
# Context variables:
#   - file_path (required): Path to Python file to audit
#   - severity_threshold (optional): Minimum severity to report (default: "high")

context:
  file_path: ""
  severity_threshold: "high"
```

**Why:** Good documentation helps users and future maintainers (including yourself).

---

## Step Design

### Prompt Design

**Be specific and directive:**

✅ **Good:**
```yaml
prompt: |
  Analyze {{file_path}} for SQL injection vulnerabilities.

  Check for:
  1. Unsanitized user input in SQL queries
  2. Dynamic query construction
  3. Missing parameterization

  Output format: List each finding with line number, severity, and explanation.
```

❌ **Bad:**
```yaml
prompt: "Look at {{file_path}}"
```

**Why:** Specific prompts produce better, more consistent results.

### Agent Selection

**Choose agents based on cognitive role, using namespaced references:**

```yaml
# Analytical tasks → zen-architect (ANALYZE mode)
- id: "analyze-structure"
  agent: "foundation:zen-architect"
  mode: "ANALYZE"

# Design tasks → zen-architect (ARCHITECT mode)
- id: "design-solution"
  agent: "foundation:zen-architect"
  mode: "ARCHITECT"

# Debugging → bug-hunter
- id: "investigate-crash"
  agent: "foundation:bug-hunter"

# Security → security-guardian
- id: "security-scan"
  agent: "foundation:security-guardian"
```

**Agent naming convention:** Always use `bundle:agent-name` format:
- `foundation:zen-architect` - from the foundation bundle
- `foundation:bug-hunter` - from the foundation bundle
- `foundation:test-coverage` - from the foundation bundle

**Why:** Namespaced references make bundle dependencies explicit and prevent ambiguity.

### Agent Dependencies

**Agent references create bundle dependencies.** When a recipe uses an agent like `foundation:zen-architect`, that agent's bundle must be loaded for the recipe to execute.

**Understanding the dependency chain:**

```yaml
# This recipe step:
- id: "analyze"
  agent: "foundation:zen-architect"
  prompt: "Analyze the code"

# Requires:
# 1. The foundation bundle (or a bundle that includes it) to be loaded
# 2. The zen-architect agent to be available through the coordinator
```

**Document requirements in recipe comments:**

```yaml
name: "code-analysis"
description: "Analyze code structure and quality"
version: "1.0.0"

# Requirements:
#   - foundation bundle (provides zen-architect, bug-hunter agents)
#   - OR a bundle that includes foundation
#
# The recipes bundle includes foundation, so these agents are available
# by default when using the recipes bundle.

steps:
  - id: "analyze"
    agent: "foundation:zen-architect"
    # ...
```

**Bundle dependency implications:**
- The recipes bundle includes the foundation bundle
- Therefore `foundation:*` agents are available by default
- If you need agents from other bundles, document the requirement
- Recipe validation should check agent availability before execution

**Why:** Explicit dependencies prevent runtime failures and make recipes more portable.

### Step Granularity

**One clear action per step:**

✅ **Good:**
```yaml
- id: "extract-functions"
  prompt: "Extract all function definitions from {{code}}"
  output: "functions"

- id: "analyze-complexity"
  prompt: "Analyze complexity of these functions: {{functions}}"
  output: "complexity_analysis"
```

❌ **Bad:**
```yaml
- id: "extract-and-analyze"
  prompt: "Extract functions from {{code}} and analyze their complexity"
  # Two actions in one step - harder to debug, no intermediate result
```

**Why:** Fine-grained steps enable better debugging, resumption, and reuse.

### Output Management

**Store outputs that later steps need:**

```yaml
- id: "analyze"
  prompt: "Analyze {{code}}"
  output: "analysis"      # ✅ Stored for later

- id: "report"
  prompt: "Generate report"
  # ❌ No output - can't reference result later
```

**When to skip output:**
- Final step (no later steps need it)
- Step is purely side-effect (writing file, notification)
- Result not useful in later steps

---

## Context Management

### Initial Context

**Define all required variables upfront:**

```yaml
context:
  # Required variables (empty string = must provide)
  file_path: ""
  project_name: ""

  # Optional variables (defaults provided)
  severity: "high"
  auto_fix: false
  timeout_minutes: 10

  # Computed variables (derived from others)
  log_file: "{{project_name}}_audit.log"
```

### Variable Naming

**Use consistent prefixes for related variables:**

```yaml
context:
  # Input files
  input_file: "src/main.py"
  input_dir: "src/"

  # Configuration
  config_severity: "high"
  config_timeout: 600
  config_retry_attempts: 3

  # Output locations
  output_report: "report.md"
  output_artifacts: "artifacts/"
```

### Variable Scope

**Understand variable lifecycles:**

```yaml
# Recipe-level: Available to all steps
context:
  global_setting: "value"

steps:
  # Step-level: Only available to subsequent steps
  - id: "step1"
    output: "step1_result"

  - id: "step2"
    # Has access to: global_setting, step1_result
    output: "step2_result"

  - id: "step3"
    # Has access to: global_setting, step1_result, step2_result
```

**Why:** Explicit scoping prevents confusion and errors.

---

## Error Handling

### Error Strategy by Step Criticality

**Critical steps (fail recipe on error):**
```yaml
- id: "validate-inputs"
  agent: "validator"
  # Default: on_error="fail"
  # Recipe stops if validation fails
```

**Optional steps (continue on error):**
```yaml
- id: "optional-enhancement"
  agent: "enhancer"
  on_error: "continue"
  # Recipe continues even if this fails
```

**Guard steps (skip remaining on error):**
```yaml
- id: "check-eligibility"
  agent: "checker"
  on_error: "skip_remaining"
  # If not eligible, skip remaining steps but don't fail recipe
```

### Retry Configuration

**Network operations:**
```yaml
- id: "fetch-external-data"
  agent: "fetcher"
  retry:
    max_attempts: 5
    backoff: "exponential"
    initial_delay: 10
    max_delay: 300
```

**LLM operations (already retried by provider):**
```yaml
- id: "analyze"
  agent: "analyzer"
  # No retry needed - provider handles it
```

**File operations (cloud sync issues):**
```yaml
- id: "read-file"
  agent: "reader"
  retry:
    max_attempts: 3
    backoff: "exponential"
    initial_delay: 5
```

### Timeout Guidelines

**By operation type:**

```yaml
# Quick analysis (< 1 minute)
- timeout: 60

# Standard analysis (1-5 minutes)
- timeout: 300

# Deep analysis (5-10 minutes)
- timeout: 600

# Very long operations (10-30 minutes)
- timeout: 1800
```

**Consider:**
- File size
- Analysis depth
- Agent complexity
- Network latency

---

## Performance

### Minimize Unnecessary Steps

❌ **Wasteful:**
```yaml
- id: "read-file"
  prompt: "Read {{file_path}}"
  output: "file_content"

- id: "analyze"
  prompt: "Analyze: {{file_content}}"
```

✅ **Efficient:**
```yaml
- id: "analyze"
  prompt: "Analyze {{file_path}}"
  # Agent can read file directly
```

### Optimize Context Size

**Keep context lean:**

```yaml
- id: "extract-summary"
  prompt: "Extract 3-sentence summary from {{document}}"
  output: "summary"  # ✅ Store summary, not entire document

- id: "use-summary"
  prompt: "Based on this summary: {{summary}}"
  # Uses small summary instead of large document
```

### Precomputed Values Pattern

**Eliminate redundant LLM calls in sub-recipes:**

When a parent recipe calls sub-recipes in a loop, avoid re-computing the same values:

```yaml
# Parent recipe - compute once, pass to all sub-recipes
context:
  _precomputed:
    date_since_iso: "{{parsed_date.iso_since}}"  # Computed once in parent
    repo_owner: "{{repo.owner}}"                  # Already known

steps:
  - id: "analyze-repos"
    foreach: "{{repos}}"
    type: "recipe"
    recipe: "sub-recipe.yaml"
    context:
      _precomputed: "{{_precomputed}}"  # Pass precomputed values
```

```yaml
# Sub-recipe - skip expensive step if precomputed available
- id: "parse-date"
  condition: "{{_precomputed.date_since_iso}} == ''"  # Only if not provided
  agent: "foundation:zen-architect"
  prompt: "Parse date..."
```

**Impact:** 12 sub-recipes × 1 LLM call = 12 calls → 0 calls (use parent's result).

### Bash vs Agent Decision

**Use bash when:**
- Output format is fixed/deterministic
- No semantic judgment needed
- Speed matters (bash: <1s, agent: 5-15s)

**Use agent when:**
- Adaptive tone/messaging needed
- Complex reasoning required
- Output varies based on context

```yaml
# ✅ Bash: Fixed format summary (fast, deterministic)
- id: "show-summary"
  type: "bash"
  command: |
    echo "Repos: {{count}} | Commits: {{commits}}"

# ✅ Agent: Requires judgment (slower, adaptive)
- id: "synthesize-report"
  agent: "foundation:zen-architect"
  prompt: "Create narrative from findings..."
```

#### Conditional LLM Bypass Pattern

**Skip expensive LLM calls when bash can handle simple cases.**

Many workflows have inputs that fall into "simple" vs "complex" categories. Use bash to handle simple cases directly, reserving LLM calls for cases that genuinely need interpretation.

```yaml
# Step 1: Check if input needs LLM interpretation
- id: "check-complexity"
  type: "bash"
  command: |
    scope="{{activity_scope}}"
    scope_lower=$(echo "$scope" | tr '[:upper:]' '[:lower:]')
    
    # Simple cases - handle directly without LLM
    if [ -z "$scope" ] || [ "$scope_lower" = "my activity" ]; then
      # Current user - no LLM needed
      jq -n --arg user "$(gh api user --jq '.login')" '{
        needs_llm: "false",
        filter_mode: "current_user",
        usernames: [$user]
      }'
    elif [ "$scope_lower" = "all" ] || [ "$scope_lower" = "everyone" ]; then
      # All activity - no LLM needed
      echo '{"needs_llm": "false", "filter_mode": "all", "usernames": []}'
    else
      # Complex case - flag for LLM interpretation
      jq -n --arg scope "$scope" '{needs_llm: "true", scope: $scope}'
    fi
  output: "complexity_check"
  parse_json: true

# Step 2: LLM interpretation (only for complex cases)
- id: "interpret-complex"
  condition: "{{complexity_check.needs_llm}} == 'true'"
  agent: "foundation:explorer"
  prompt: |
    Interpret: "{{complexity_check.scope}}"
    Return JSON with filter_mode, usernames, description.
  output: "interpreted_scope"
  parse_json: true
```

**Impact:** In ecosystem-activity-report, this pattern eliminates LLM calls for ~80% of typical inputs ("my activity", "all", single usernames).

**When to apply:**
- User input has common/predictable patterns
- Simple cases can be handled with string matching or regex
- LLM adds 5-15 seconds per call

**Reference:** See `setup-and-check-scope` step in `@amplifier:recipes/ecosystem-activity-report.yaml`

### Parallel Execution

**Enable parallel for independent iterations:**

```yaml
- id: "analyze-each"
  foreach: "{{items}}"
  parallel: true  # ~4x faster for 12 items
  type: "recipe"
  recipe: "analysis.yaml"
```

**Bounded Parallelism (Recommended):**

Use `parallel: N` to limit concurrent executions, preventing API rate limit issues:

```yaml
- id: "analyze-repos"
  foreach: "{{repos}}"
  parallel: 5  # Max 5 concurrent (not unbounded)
  type: "recipe"
  recipe: "repo-analysis.yaml"
```

| Value | Behavior | Use Case |
|-------|----------|----------|
| `false` | Sequential | Order-dependent operations |
| `true` | Unbounded parallel | Small loops, no rate limits |
| `5` | Max 5 concurrent | Large loops, API rate limits |

**Considerations:**
- Prefer bounded parallelism (`parallel: 5`) over unbounded (`parallel: true`)
- Use `parallel: "{{parallel_mode}}"` for user control
- Consider recipe-level rate limiting for global control

### Rate-Limited API Calls

**When calling external APIs in loops, implement rate limiting and retry logic.**

```yaml
context:
  # User-configurable rate limiting
  api_delay_seconds: 0.5      # Delay between API calls
  api_retry_attempts: 3       # Retries per call

steps:
  - id: "fetch-data"
    type: "bash"
    command: |
      delay={{api_delay_seconds}}
      max_retries={{api_retry_attempts}}
      
      # Retry wrapper with exponential backoff
      gh_api_retry() {
        local endpoint="$1"
        local jq_filter="$2"
        local attempt=1
        local result=""
        
        while [ $attempt -le $max_retries ]; do
          result=$(gh api "$endpoint" --jq "$jq_filter" 2>/dev/null) && break
          echo "Attempt $attempt failed, retrying..." >&2
          sleep $((attempt * 2))  # Exponential backoff: 2, 4, 8...
          attempt=$((attempt + 1))
        done
        
        echo "${result:-0}"
      }
      
      # Process items with rate limiting
      for item in {{items}}; do
        count=$(gh_api_retry "repos/$item/commits" 'length')
        echo "$item: $count commits"
        sleep "$delay"  # Rate limit between calls
      done
```

**Configuration guidance:**

| API | Recommended Delay | Notes |
|-----|------------------|-------|
| GitHub (authenticated) | 0.3-0.5s | 5000 requests/hour limit |
| GitHub (unauthenticated) | 1.0s | 60 requests/hour limit |
| Rate-limited APIs | 1.0-2.0s | Check provider docs |

**Expose as context variables** so users can adjust based on their rate limits:
```yaml
context:
  api_delay_seconds: 0.5    # Increase if hitting rate limits
  api_retry_attempts: 3     # Increase for unreliable networks
```

**Reference:** See `api_delay_seconds` and `api_retry_attempts` in `@amplifier:recipes/ecosystem-activity-report.yaml`

### Recipe-Level Rate Limiting

**For comprehensive control over LLM call rates across entire recipe trees, use the `rate_limiting` configuration:**

```yaml
name: "ecosystem-analysis"
version: "1.0.0"
description: "Analyze multiple repos with rate limiting"

rate_limiting:
  max_concurrent_llm: 5      # Max 5 concurrent LLM calls across recipe tree
  min_delay_ms: 500          # 500ms minimum between call completions
  backoff:
    enabled: true            # Auto-slow on 429 errors
    initial_delay_ms: 1000   # Start with 1s delay after rate limit hit
    max_delay_ms: 60000      # Cap at 1 minute
    multiplier: 2.0          # Double delay on each consecutive rate limit
    reset_after_success: 3   # Reset after 3 successful calls

steps:
  - id: "analyze-repos"
    foreach: "{{repos}}"
    parallel: true           # All 24 repos start concurrently...
    type: "recipe"           # ...but only 5 LLM calls run at once
    recipe: "repo-analysis.yaml"
```

**Key Points:**

| Feature | Description |
|---------|-------------|
| `max_concurrent_llm` | Global semaphore across entire recipe tree (including sub-recipes) |
| `min_delay_ms` | Pacing between LLM call completions (prevents bursts) |
| `backoff` | Automatic slowdown when 429 errors are detected |

**Inheritance Rules:**
- Sub-recipes **inherit** parent's rate limiter (cannot override)
- Parent recipe's limits apply to the entire execution tree
- This prevents sub-recipes from accidentally overwhelming APIs

**When to Use:**

| Scenario | Configuration |
|----------|---------------|
| Multi-user environment | `max_concurrent_llm: 3-5` |
| API with strict limits | `max_concurrent_llm: 2`, `min_delay_ms: 1000` |
| Single-user, fast API | `max_concurrent_llm: 10` or omit |

**Combining with Bounded Parallelism:**

```yaml
# Recipe-level: global LLM concurrency
rate_limiting:
  max_concurrent_llm: 5

steps:
  # Step-level: loop iteration concurrency
  - id: "outer-loop"
    foreach: "{{repos}}"
    parallel: 10             # Up to 10 repos analyzed concurrently...
    type: "recipe"           # ...but LLM calls capped at 5 globally
```

This separation allows high concurrency for non-LLM work (bash steps, file I/O) while respecting LLM rate limits.

---

## Model Selection

Recipe steps can specify which provider and model to use, enabling cost/capability optimization per step.

### The Model Selection Strategy

**Match model capability to task complexity:**

| Task Type | Model Tier | Example Models | Why |
|-----------|------------|----------------|-----|
| Simple classification, yes/no | Fast/Cheap | `claude-haiku`, `gpt-4o-mini` | No deep reasoning needed |
| Quick summaries, formatting | Fast/Cheap | `claude-haiku` | Speed over depth |
| Code implementation, analysis | Balanced | `claude-sonnet-*`, `gpt-4o` | Good speed/capability tradeoff |
| Codebase exploration | Balanced | `claude-sonnet-*` | Thorough but routine |
| Architecture, strategy | Powerful | `claude-opus-*`, `gpt-5*` | Best reasoning, worth the cost |
| Security analysis | Powerful | `claude-opus-*` | Critical decisions need best model |
| Complex design decisions | Powerful | `claude-opus-*` | Strategic thinking |

### Using Provider and Model Fields

```yaml
steps:
  # Fast/cheap for simple classification
  - id: "classify-severity"
    agent: "foundation:zen-architect"
    provider: "anthropic"
    model: "claude-haiku"
    prompt: |
      Classify the severity as exactly one word: none, low, medium, high, critical
    output: "severity"

  # Balanced for implementation work
  - id: "analyze-code"
    agent: "foundation:zen-architect"
    provider: "anthropic"
    model: "claude-sonnet-*"
    prompt: |
      Analyze the code structure and identify issues...
    output: "analysis"

  # Powerful for strategic decisions
  - id: "design-architecture"
    agent: "foundation:zen-architect"
    provider: "anthropic"
    model: "claude-opus-*"
    prompt: |
      Design the optimal architecture considering all tradeoffs...
    output: "architecture"
```

### Glob Pattern Matching

Model names support fnmatch-style glob patterns for flexible version matching:

| Pattern | Matches | Use Case |
|---------|---------|----------|
| `claude-sonnet-*` | Any claude-sonnet version | Auto-select latest sonnet |
| `claude-opus-4-*` | Any claude-opus-4 variant | Stay on opus-4 family |
| `gpt-5*` | gpt-5, gpt-5.1, gpt-5.2, etc. | Latest GPT-5 series |
| `claude-sonnet-4-5-20250514` | Exact match | Pin to specific version |

**Why glob patterns?** Model versions change frequently. Using `claude-sonnet-*` means your recipe automatically uses the latest sonnet without manual updates.

### Real-World Example: Code Review Recipe

```yaml
name: "code-review-optimized"
description: "Code review with model selection for cost/capability optimization"

steps:
  # Haiku: Simple structure analysis (fast, cheap)
  - id: "quick-scan"
    agent: "foundation:explorer"
    provider: "anthropic"
    model: "claude-haiku"
    prompt: "List the functions and classes in {{file_path}}"
    output: "structure"

  # Sonnet: Thorough code analysis (balanced)
  - id: "analyze-issues"
    agent: "foundation:zen-architect"
    provider: "anthropic"
    model: "claude-sonnet-*"
    prompt: "Identify code issues in {{file_path}}: {{structure}}"
    output: "issues"

  # Haiku: Simple classification (fast, cheap)
  - id: "classify-severity"
    agent: "foundation:zen-architect"
    provider: "anthropic"
    model: "claude-haiku"
    prompt: "Respond with one word - severity level: none, low, medium, high, critical"
    output: "severity"

  # Opus: Strategic recommendations (best reasoning)
  - id: "design-improvements"
    agent: "foundation:zen-architect"
    provider: "anthropic"
    model: "claude-opus-*"
    condition: "{{severity}} != 'none'"
    prompt: "Design concrete improvements for {{file_path}} addressing: {{issues}}"
    output: "improvements"
```

### Fallback Behavior

- **Provider not configured**: Falls back to default provider (warning logged)
- **Model pattern has no matches**: Uses provider's default model
- **No provider/model specified**: Uses session's configured provider

### Anti-Patterns

❌ **Using expensive models for simple tasks:**
```yaml
# Bad: Opus for yes/no question
- id: "is-python"
  model: "claude-opus-*"
  prompt: "Is this file Python? Answer yes or no."
```

❌ **Using cheap models for critical decisions:**
```yaml
# Bad: Haiku for security analysis
- id: "security-audit"
  model: "claude-haiku"
  prompt: "Identify all security vulnerabilities..."
```

✅ **Match model to task:**
```yaml
# Good: Haiku for classification, Opus for security
- id: "is-python"
  model: "claude-haiku"
  prompt: "Is this file Python? Answer yes or no."

- id: "security-audit"
  model: "claude-opus-*"
  prompt: "Identify all security vulnerabilities..."
```

---

## Reliability Patterns

These patterns ensure consistent, predictable recipe behavior.

### Explicit File Write Pattern

**Never rely on LLM to write files. Use bash for guaranteed I/O.**

LLM file writes are non-deterministic—the agent might write, might not, might write to the wrong path. For critical outputs, always use explicit bash steps.

❌ **Unreliable:**
```yaml
- id: "synthesize"
  agent: "foundation:zen-architect"
  prompt: |
    Generate report and write to {{output_path}}.
  # Agent might: write file, forget to write, write partial content, wrong path
```

✅ **Reliable:**
```yaml
# Step 1: Generate content (LLM)
- id: "synthesize"
  agent: "foundation:zen-architect"
  prompt: |
    Generate the report.
    DO NOT write to files - return the content only.
  output: "report_content"

# Step 2: Write to file (bash - guaranteed)
- id: "write-report"
  type: "bash"
  command: |
    set -euo pipefail
    mkdir -p "$(dirname "{{output_path}}")"
    printf '%s\n' '{{report_content}}' > "{{output_path}}"
    
    # Verify write succeeded
    if [ -s "{{output_path}}" ]; then
      echo "Written: {{output_path}} ($(wc -c < "{{output_path}}") bytes)"
    else
      echo "ERROR: Write failed" >&2
      exit 1
    fi
  on_error: "fail"
```

**Key elements:**
1. **Explicit instruction** in LLM prompt: "DO NOT write to files"
2. **Bash step** for actual file I/O
3. **Verification** that write succeeded
4. **`on_error: fail`** for critical output steps

#### Atomic Write Pattern

**Write to temp file, then move. Prevents partial/corrupted files.**

```yaml
- id: "write-output"
  type: "bash"
  command: |
    set -euo pipefail
    
    # Write to temp file first
    printf '%s\n' '{{content}}' > "{{output_path}}.tmp"
    
    # Atomic move (either succeeds completely or fails)
    mv "{{output_path}}.tmp" "{{output_path}}"
    
    # Now {{output_path}} is guaranteed complete
```

**Why this matters:**
- If write fails mid-stream, temp file is corrupted (not the final file)
- `mv` on same filesystem is atomic—file either exists completely or not
- Prevents downstream steps from reading partial content
- Essential for files that other processes might read concurrently

**Reference:** See `write-report` step in `@amplifier:recipes/ecosystem-activity-report.yaml`

### Cleanup on Completion

**Remove intermediate files while preserving outputs.**

Long-running recipes create temporary files. Clean up at completion to avoid disk bloat and confusion.

```yaml
context:
  working_dir: "./ai_working"

steps:
  # ... processing steps that create files in working_dir ...
  
  - id: "complete"
    type: "bash"
    command: |
      # Remove intermediate/temporary directories
      rm -rf "{{working_dir}}/discovery"
      rm -rf "{{working_dir}}/temp"
      rm -rf "{{working_dir}}/cache"
      
      # Keep output directories
      # {{working_dir}}/reports  - final outputs
      # {{working_dir}}/logs     - audit trail (optional)
      
      echo "Cleanup complete. Remaining:"
      ls -la "{{working_dir}}/"
    on_error: "continue"  # Don't fail recipe if cleanup fails
```

**Best practices:**
- **Use `on_error: continue`** — cleanup failure shouldn't fail the recipe
- **Be explicit** about what to delete (not `rm -rf {{working_dir}}`)
- **Keep outputs** in a dedicated subdirectory (e.g., `reports/`)
- **Log what remains** for user visibility

**Directory structure pattern:**
```
{{working_dir}}/
├── discovery/    # ← DELETE: intermediate data
├── temp/         # ← DELETE: scratch files
├── cache/        # ← DELETE: cached API responses
├── reports/      # ← KEEP: final outputs
└── logs/         # ← KEEP (optional): execution logs
```

**Reference:** See `complete` step in `@amplifier:recipes/ecosystem-activity-report.yaml`

---

## Testing

### Test Strategy

**1. Unit testing (individual steps):**

```yaml
# Test single step in isolation
name: "test-analyze-step"
steps:
  - id: "analyze"
    agent: "analyzer"
    prompt: "Analyze {{test_file}}"

context:
  test_file: "tests/fixtures/simple.py"
```

**2. Integration testing (full recipe):**

```bash
# Run full recipe with test data
amplifier run "execute my-recipe.yaml with file_path=tests/fixtures/test.py"
```

**3. Validation testing:**

```bash
# Validate without execution
amplifier run "validate recipe my-recipe.yaml"
```

### Test Data

**Create realistic test fixtures:**

```
tests/
  fixtures/
    simple.py      # Minimal test case
    complex.py     # Comprehensive test case
    edge_case.py   # Known edge case
    invalid.py     # Should fail gracefully
```

### Regression Testing

**Document expected behavior:**

```yaml
# my-recipe.yaml

# Expected behavior (for regression testing):
#
# Input: Simple Python file (10 lines)
# Expected steps: 4 steps complete successfully
# Expected duration: ~2 minutes
# Expected outputs: analysis, suggestions, validation, report
#
# Input: Complex Python file (500 lines)
# Expected steps: 4 steps complete successfully
# Expected duration: ~10 minutes
# Expected outputs: analysis, suggestions, validation, report
```

---

## Maintenance

### Versioning Strategy

**When to bump version:**

**Patch (x.x.X):**
- Typo fixes in prompts
- Documentation updates
- Performance improvements (no behavior change)

**Minor (x.X.x):**
- New optional steps
- New optional context variables
- Enhanced error handling

**Major (X.x.x):**
- Changed required context variables
- Removed steps
- Changed output format
- Breaking behavior changes

### Changelog Requirements

**Every recipe edit MUST include a changelog entry.** The changelog provides critical context for understanding recipe evolution, debugging issues, and learning from past solutions.

**Location:** At the top of the recipe file, after the header comment block and before the `name:` field.

**Format:**
```yaml
# =============================================================================
# CHANGELOG
# =============================================================================
#
# v1.2.0 (2026-01-22):
#   - CATEGORY: Brief summary of change
#     * Root cause: Why this change was needed
#     * Fix/Change: What was actually done
#     * Result: What improved
#
# v1.1.0 (2026-01-15):
#   - BUGFIX: Description of bug fix
#   - IMPROVEMENT: Description of improvement
#
# v1.0.0 (2026-01-10):
#   - Initial recipe implementation
#
# =============================================================================
```

**Categories (use consistently):**

| Category | When to Use |
|----------|-------------|
| `BUGFIX` | Fixing broken behavior |
| `CRITICAL FIX` | Urgent fix for blocking issues |
| `IMPROVEMENT` | Enhancing existing functionality |
| `REFACTOR` | Code restructuring without behavior change |
| `NEW FEATURE` | Adding new capabilities |
| `BREAKING CHANGE` | Changes that affect existing usage |

**Root Cause Documentation:**

For bug fixes, document the root cause to help future maintainers:

```yaml
# v1.3.1 (2026-01-22):
#   - BUGFIX: JSON parsing failures in build-outline step
#     * ROOT CAUSE: LLM outputs unescaped quotes in prompt strings like "~/repos/foo"
#       that weren't escaped, causing JSON parse errors at position ~11466
#     * FIX: Added lookahead heuristic in clean_json_control_chars() to detect quotes
#       that are data vs. string terminators
#     * RESULT: JSON parsing now handles embedded quotes in LLM output
```

**Key Insights:**

When you discover something non-obvious, document it explicitly:

```yaml
# v1.4.0 (2026-01-22):
#   - CRITICAL FIX: Classification logic incorrectly identified sources
#     * THE KEY INSIGHT: Just because doc A shares content with doc B does NOT mean
#       A is derived from B - must check if A actually CITES B
#     * A document with ZERO outbound citations CANNOT be synthesized
#     * WHY THIS WORKS: If it doesn't cite anything, it doesn't derive from anything
```

**Why Changelogs Matter:**

1. **Debugging**: When a recipe breaks, the changelog shows what changed and why
2. **Learning**: Root cause analysis prevents repeating the same mistakes
3. **Onboarding**: New maintainers understand design decisions
4. **Rollback**: Clear version history enables safe rollback decisions
5. **Patterns**: Successful fixes become reusable patterns

**Changelog Validation:**

The `result-validator` agent checks for changelog presence when validating recipe edits. Missing or incomplete changelogs will generate warnings.

**See also:** `amplifier:recipes/document-generation.yaml` and `amplifier:recipes/outline-generation-from-doc.yaml` for exemplary changelog practices.

### Deprecation Process

**1. Announce in comments:**
```yaml
# DEPRECATED: Use security-audit-v2.yaml instead
# This recipe will be removed in v3.0.0
```

**2. Update description:**
```yaml
description: "[DEPRECATED] Use security-audit-v2 instead"
```

**3. Provide migration guide:**
```yaml
# Migration from v1 to v2:
#
# Changed:
#   - Context variable "file" renamed to "file_path"
#   - Added required "project_name" variable
#   - Removed "quick_mode" option
#
# Example v1:
#   amplifier run "execute recipe-v1.yaml with file=auth.py"
#
# Example v2:
#   amplifier run "execute recipe-v2.yaml with file_path=auth.py project_name=myapp"
```

### Documentation Maintenance

**Keep in sync:**
- Recipe YAML
- Usage examples
- Expected behavior
- Dependencies (agent versions)

**Update on changes:**
- Prompt improvements
- New steps added
- Error handling changes
- Performance characteristics

---

## Common Pitfalls

### 1. Overly Generic Prompts

❌ **Problem:**
```yaml
prompt: "Analyze the code"
```

✅ **Solution:**
```yaml
prompt: |
  Analyze {{file_path}} for:
  1. Security vulnerabilities
  2. Performance bottlenecks
  3. Code complexity issues

  For each finding, provide:
  - Line number
  - Severity (critical/high/medium/low)
  - Explanation
  - Suggested fix
```

### 2. Missing Context Variables

❌ **Problem:**
```yaml
steps:
  - prompt: "Analyze {{file_path}}"
    # file_path never defined!
```

✅ **Solution:**
```yaml
context:
  file_path: ""  # Define upfront

steps:
  - prompt: "Analyze {{file_path}}"
```

### 3. Monolithic Steps

❌ **Problem:**
```yaml
- id: "do-everything"
  prompt: "Analyze code, find issues, suggest fixes, generate tests, write documentation"
```

✅ **Solution:**
```yaml
- id: "analyze"
  prompt: "Analyze code"
  output: "analysis"

- id: "suggest-fixes"
  prompt: "Based on {{analysis}}, suggest fixes"
  output: "fixes"

- id: "generate-tests"
  prompt: "Generate tests for {{fixes}}"
```

### 4. Tight Coupling

❌ **Problem:**
```yaml
- id: "step1"
  prompt: "Analyze {{file}} and store in {{step2_input_format}}"
  # Knows too much about step2's requirements
```

✅ **Solution:**
```yaml
- id: "step1"
  prompt: "Analyze {{file}}"
  output: "analysis"
  # Step2 adapts to step1's output format
```

### 5. No Error Handling

❌ **Problem:**
```yaml
- id: "external-api"
  agent: "fetcher"
  # No timeout, no retry, no error handling
```

✅ **Solution:**
```yaml
- id: "external-api"
  agent: "fetcher"
  timeout: 300
  retry:
    max_attempts: 3
    backoff: "exponential"
  on_error: "continue"  # Or "fail" if critical
```

### 6. Hidden Requirements

❌ **Problem:**
```yaml
# Recipe works only if security-guardian is configured with API key
# But this isn't documented anywhere
```

✅ **Solution:**
```yaml
# Requirements:
#   - security-guardian agent installed
#   - Security Guardian API key configured in profile
#   - Internet connection for vulnerability database updates
#
# Setup:
#   1. Install: amplifier collection add amplifier-collection-security
#   2. Configure: Add API key to profile
#   3. Verify: amplifier agents list | grep security-guardian
```

---

## Summary: The Recipe Quality Checklist

Before sharing or using a recipe in production, verify:

### Design
- [ ] Single, clear purpose
- [ ] Appropriate granularity (not too complex, not too simple)
- [ ] Follows semantic versioning
- [ ] Well-documented with usage examples

### Structure
- [ ] All required fields present and valid
- [ ] Descriptive names (recipe, steps, variables)
- [ ] Clear, specific prompts
- [ ] Appropriate agent selection with namespaced references (e.g., `foundation:zen-architect`)
- [ ] Agent dependencies documented (which bundles provide required agents)

### Context
- [ ] All required variables defined
- [ ] Defaults provided for optional variables
- [ ] No undefined variable references
- [ ] Variable naming consistent

### Error Handling
- [ ] Timeouts appropriate for operation
- [ ] Retry logic for transient failures
- [ ] Error strategy matches step criticality
- [ ] Graceful degradation where appropriate

### Reliability
- [ ] Critical file writes use explicit bash steps (not LLM)
- [ ] Atomic writes for important outputs (temp + mv)
- [ ] API calls include rate limiting if in loops
- [ ] Cleanup step removes intermediate files
- [ ] Outputs preserved in dedicated directory

### Testing
- [ ] Validated with test data
- [ ] Expected behavior documented
- [ ] Edge cases considered
- [ ] Regression tests possible

### Documentation
- [ ] Purpose clearly stated
- [ ] Usage examples provided
- [ ] Requirements listed
- [ ] Expected runtime documented

---

**See Also:**
- [Recipe Schema Reference](RECIPE_SCHEMA.md) - Technical specification
- [Recipes Guide](RECIPES_GUIDE.md) - Conceptual overview
- [Troubleshooting](TROUBLESHOOTING.md) - Common issues and solutions
- [Examples Catalog](EXAMPLES_CATALOG.md) - Working examples
