# Troubleshooting Guide

**Solutions to common recipe issues**

This guide helps you diagnose and fix problems when creating or executing recipes.

## Table of Contents

- [Validation Errors](#validation-errors)
- [Execution Errors](#execution-errors)
- [Session Issues](#session-issues)
- [Agent Problems](#agent-problems)
- [Performance Issues](#performance-issues)
- [Variable Problems](#variable-problems)
- [JSON and Data Format Issues](#json-and-data-format-issues)
- [The Process Hangs After the Recipe Finishes](#the-process-hangs-after-the-recipe-finishes)
- [Auditing a Finished Run: `steps.jsonl`](#auditing-a-finished-run-stepsjsonl)
- [Which Engine Actually Ran? (Proving a Change Live)](#which-engine-actually-ran-proving-a-change-live)
- [Debugging Tips](#debugging-tips)

---

## Validation Errors

### Error: "Invalid YAML syntax"

**Symptom:**
```
Error: Invalid YAML syntax at line 15
```

**Cause:** YAML formatting error (indentation, quotes, colons, etc.)

**Solution:**

1. **Check indentation** (YAML requires consistent spaces, not tabs):
   ```yaml
   # ❌ Wrong indentation
   steps:
   - id: "analyze"
     prompt: "Analyze"

   # ✅ Correct indentation
   steps:
     - id: "analyze"
       prompt: "Analyze"
   ```

2. **Check quotes** for multi-line strings:
   ```yaml
   # ❌ Missing quotes
   prompt: This is a
     multi-line prompt

   # ✅ Use pipe for multi-line
   prompt: |
     This is a
     multi-line prompt
   ```

3. **Use YAML validator:**
   ```bash
   # Install yamllint
   pip install yamllint

   # Validate your recipe
   yamllint my-recipe.yaml
   ```

### Error: "Required field missing: [field]"

**Symptom:**
```
Error: Required field missing: description
```

**Cause:** Recipe missing required field.

**Solution:**

Ensure all required fields present:
```yaml
name: "recipe-name"        # Required
description: "What it does" # Required
version: "1.0.0"           # Required
steps: [...]              # Required (at least one)
```

### Error: "Duplicate step ID: [id]"

**Symptom:**
```
Error: Duplicate step ID: analyze
```

**Cause:** Two steps have the same `id`.

**Solution:**

Make all step IDs unique:
```yaml
steps:
  - id: "analyze-security"    # Unique
    agent: "foundation:security-guardian"

  - id: "analyze-performance" # Different from above
    agent: "foundation:performance-optimizer"
```

### Error: "Invalid version format: [version]"

**Symptom:**
```
Error: Invalid version format: 1.0
```

**Cause:** Version doesn't follow semantic versioning (semver).

**Solution:**

Use MAJOR.MINOR.PATCH format:
```yaml
# ❌ Wrong
version: "1.0"
version: "v1.0.0"

# ✅ Correct
version: "1.0.0"
version: "2.1.3"
version: "0.5.0-beta"
```

---

## Execution Errors

### Error: "Variable undefined: {{variable}}"

**Symptom:**
```
Error: Variable undefined: {{file_path}}
Step: analyze-code
```

**Cause:** Variable referenced but not defined in context or previous outputs.

**Solution:**

1. **Add to context dict:**
   ```yaml
   context:
     file_path: ""  # Define variable

   steps:
     - prompt: "Analyze {{file_path}}"
   ```

2. **Or ensure previous step produces it:**
   ```yaml
   steps:
     - id: "get-path"
       prompt: "Determine file path"
       output: "file_path"  # Defines {{file_path}}

     - id: "analyze"
       prompt: "Analyze {{file_path}}"  # Now defined
   ```

3. **Check variable name spelling:**
   ```yaml
   # ❌ Typo
   context:
     file_path: "test.py"

   steps:
     - prompt: "Analyze {{filepath}}"  # Wrong: no underscore

   # ✅ Correct
     - prompt: "Analyze {{file_path}}"
   ```

### Error: "Agent not found: [agent-name]"

**Symptom:**
```
Error: Agent not found: custom-analyzer
```

**Cause:** Specified agent not installed or not available in your profile.

**Solution:**

1. **Check agent installed:**
   ```bash
   amplifier agents list | grep custom-analyzer
   ```

2. **Install missing agent:**
   ```bash
   # If it's from a collection
   amplifier collection add git+https://github.com/user/agent-collection@main

   # Verify it's available
   amplifier agents list
   ```

3. **Check agent name spelling:**
   ```yaml
   # Common misspellings
   agent: "foundation:zen-architect"    # ✅ Correct
   agent: "foundation:zenarchitect"     # ❌ Missing hyphen
   agent: "foundation:zen_architect"    # ❌ Underscore instead of hyphen
   ```

4. **Verify agent in profile:**
   ```yaml
   # Check your active profile includes the agent
   amplifier profile show
   ```

### Error: "This legacy recipe references agent(s) ... which the calling bundle ... does not mount"

**Symptom:**
```
This legacy recipe references agent(s) 'foundation:zen-architect' which the
calling bundle 'anchors' does not mount. ...
```

The recipe does not start — this is a plan-time refusal, before any step runs.

**Cause:** The recipe declares no `schema_version`, so it is a **legacy**
recipe: its `agent:` references resolve from the *calling session's* agent map,
not from anything the recipe itself declares. The same recipe works fine from a
bundle that mounts those agents and fails from one that does not.

**Solution — pick either:**

1. **Run it from a bundle that has the agents:**
   ```bash
   amplifier tool invoke -b foundation recipes \
     --operation execute --recipe-path ./my-recipe.yaml
   ```

2. **Migrate the recipe so it carries its own agents** (portable — recommended):
   ```yaml
   schema_version: 2

   dependencies:
     bundles:
       - foundation

   steps:
     - id: review
       agent: "foundation:zen-architect"
       prompt: "Review it"
       output: review_result
   ```
   See [RECIPE_SCHEMA.md](RECIPE_SCHEMA.md), "Recipe schema v2".

**Note:** the check is skipped (never guessed) when the host exposes no
readable agent registry, so it can only refuse a run that was going to fail at
its first agent step anyway.

### Error: "Step timeout after [N] seconds"

**Symptom:**
```
Error: Step timeout after 600 seconds
Step: deep-analysis
```

**Cause:** Step exceeded timeout limit.

**Solution:**

1. **Increase timeout for long-running steps:**
   ```yaml
   - id: "deep-analysis"
     agent: "analyzer"
     timeout: 1800  # 30 minutes instead of default 10
   ```

2. **Simplify the prompt:**
   ```yaml
   # ❌ Too much in one step
   prompt: "Analyze entire codebase, generate tests, and create documentation"

   # ✅ Break into smaller steps
   - id: "analyze"
     prompt: "Analyze codebase structure"
     timeout: 600

   - id: "generate-tests"
     prompt: "Generate tests based on: {{analysis}}"
     timeout: 300
   ```

3. **Check agent responsiveness:**
   - Agent might be waiting for input
   - Provider API might be slow
   - Network issues

### Error: "Recipe execution failed: [reason]"

**Symptom:**
```
Error: Recipe execution failed: Agent returned error
Step: analyze-code
```

**Cause:** Agent encountered an error during execution.

**Solution:**

1. **Check session logs:**
   ```bash
   # Find recent session
   ls -lt ~/.amplifier/projects/*/recipe-sessions/

   # View events log
   cat ~/.amplifier/projects/<project>/recipe-sessions/<session-id>/events.jsonl | grep error
   ```

2. **Add error handling:**
   ```yaml
   - id: "risky-step"
     agent: "analyzer"
     on_error: "continue"  # Don't fail recipe
     retry:
       max_attempts: 3
       backoff: "exponential"
   ```

3. **Test step in isolation:**
   ```yaml
   # Create minimal recipe with just the failing step
   name: "test-failing-step"
   steps:
     - id: "test"
       agent: "analyzer"
       prompt: "Same prompt that failed"

   context:
     # Use same context variables
   ```

---

## While Loop and Sub-Recipe Errors

### Error: `'dict' object has no attribute 'validate'`

**Cause:** Using `while_steps` with steps that have complex fields (e.g., `provider_preferences`
as a list of dicts). The step parsing didn't convert nested objects.

**Fix:** Use the sub-recipe pattern instead of `while_steps` for complex loop bodies.

### Error: `Undefined variable: {{_loop_iteration}}`

**Cause:** The static validator doesn't recognize runtime loop variables (`_loop_iteration`,
`_loop_index`). These are injected at execution time by the while-loop executor.

**Fix:** Use context variables managed via `update_context` instead of `_loop_iteration`:
```yaml
context:
  iteration: "0"
steps:
  - id: "loop"
    type: "bash"
    command: "echo iteration {{iteration}}"
    while_condition: "{{iteration}} < 5"
    update_context:
      iteration: "{{_loop_iteration}}"  # _loop_iteration is available at runtime
```

### Error: `Undefined variable: {{result.field}}` in update_context

**Cause:** The step's output is not stored in context before `update_context` runs.

**Fix:** The step output is stored in `context[step.output]`
before `update_context` expressions are evaluated.

### Error: `Key 'field' not found` when accessing sub-recipe output

**Cause:** Sub-recipe output is the sub-recipe's full context, not just the last step's output.

**Fix:** Use nested dot notation to access sub-recipe step outputs:
```yaml
# If sub-recipe step has output: "result" with field "done"
# And parent step has output: "iter_out"
update_context:
  done: "{{iter_out.result.done}}"    # CORRECT: nested path
  # done: "{{iter_out.done}}"         # WRONG: "done" is not a top-level key
```

### Error: `agent steps require 'agent' field` on a while loop step

**Cause:** Steps default to `type: "agent"`. A while-loop container step without an explicit
type triggers agent validation.

**Fix:** Add `type: "bash"` with `command: "true"` to while loop container steps, or use
`type: "recipe"` with a sub-recipe as the loop body.

---

## Session Issues

### Error: "Recipe session not found: [session-id]"

**Symptom:**
```
Error: Recipe session not found: recipe_20251118_143022_a3f2
```

**Cause:** Session doesn't exist or was auto-cleaned.

**Solution:**

1. **Check session exists:**
   ```bash
   ls ~/.amplifier/projects/*/recipe-sessions/ | grep recipe_20251118_143022_a3f2
   ```

2. **Check auto-cleanup settings:**
   - Default: Sessions older than 7 days deleted
   - Check tool config for `auto_cleanup_days`

3. **List active sessions:**
   ```bash
   amplifier run "list recipe sessions"
   ```

4. **If session lost, re-run recipe:**
   ```bash
   amplifier run "execute my-recipe.yaml with [context vars]"
   ```

### Error: "Session directory not writable"

**Symptom:**
```
Error: Cannot write to session directory
Path: ~/.amplifier/projects/<project>/recipe-sessions/
```

**Cause:** Permission issues or disk full.

**Solution:**

1. **Check permissions:**
   ```bash
   ls -ld ~/.amplifier/projects/<project>/recipe-sessions/

   # Fix if needed
   chmod 755 ~/.amplifier/projects/<project>/recipe-sessions/
   ```

2. **Check disk space:**
   ```bash
   df -h ~

   # Clean old sessions if disk full
   rm -rf ~/.amplifier/projects/*/recipe-sessions/recipe_202511*
   ```

3. **Check directory exists:**
   ```bash
   mkdir -p ~/.amplifier/projects/<project>/recipe-sessions/
   ```

### Issue: "Cannot resume session"

**Symptom:**
```
Error: Session state corrupted or incomplete
```

**Cause:** Session state file damaged or incomplete.

**Solution:**

1. **Check state file:**
   ```bash
   cat ~/.amplifier/projects/<project>/recipe-sessions/<session-id>/state.json

   # Should be valid JSON
   python3 -m json.tool state.json
   ```

2. **If corrupted, start fresh:**
   ```bash
   # Remove corrupted session
   rm -rf ~/.amplifier/projects/<project>/recipe-sessions/<session-id>/

   # Re-run recipe from beginning
   amplifier run "execute my-recipe.yaml with [context vars]"
   ```

3. **Enable more frequent checkpointing:**
   ```yaml
   # In tool config
   tools:
     - module: tool-recipes
       config:
         checkpoint_frequency: "per_step"  # Checkpoint after every step
   ```

---

## Agent Problems

### Issue: "Agent producing unexpected output"

**Cause:** Prompt unclear or agent mode incorrect.

**Solution:**

1. **Make prompt more specific:**
   ```yaml
   # ❌ Vague
   prompt: "Look at the code"

   # ✅ Specific
   prompt: |
     Analyze {{file_path}} for security vulnerabilities.

     Output format:
     - Line number
     - Severity (critical/high/medium/low)
     - Description
     - Suggested fix
   ```

2. **Specify agent mode (if applicable):**
   ```yaml
   - agent: "foundation:zen-architect"
     mode: "ANALYZE"  # Specify mode explicitly
   ```

3. **Adjust agent configuration:**
   ```yaml
   - id: "precise-analysis"
     agent: "analyzer"
     agent_config:
       providers:
         - module: "provider-anthropic"
           config:
             temperature: 0.2  # Lower for more deterministic
   ```

### Issue: "Agent not using provided context"

**Cause:** Context not properly passed to agent or prompt doesn't reference context.

**Solution:**

1. **Explicitly reference context in prompt:**
   ```yaml
   context:
     severity: "high"

   steps:
     - prompt: |
         Analyze for {{severity}}-severity issues.  # Explicitly use {{severity}}
         Focus only on {{severity}} level.
   ```

2. **Check variable substitution:**
   ```bash
   # In session logs, verify variables were substituted
   cat ~/.amplifier/projects/<project>/recipe-sessions/<session-id>/events.jsonl | \
     grep '"event":"step:start"' | jq '.data.prompt'
   ```

### Issue: "Agent taking too long"

**Cause:** Complex prompt, large input, or agent overloaded.

**Solution:**

1. **Break into smaller steps:**
   ```yaml
   # ❌ One huge step
   - id: "analyze-everything"
     prompt: "Analyze all 100 files"

   # ✅ Multiple smaller steps
   - id: "analyze-batch-1"
     prompt: "Analyze files 1-20"

   - id: "analyze-batch-2"
     prompt: "Analyze files 21-40"
   ```

2. **Reduce input size:**
   ```yaml
   # Instead of passing entire document
   - id: "summarize"
     prompt: "Create 3-sentence summary of {{document}}"
     output: "summary"

   - id: "analyze"
     prompt: "Analyze this summary: {{summary}}"  # Much smaller input
   ```

3. **Use faster model for some steps:**
   ```yaml
   - id: "quick-check"
     agent: "analyzer"
     agent_config:
       providers:
         - module: "provider-anthropic"
           config:
             model: "claude-haiku-4"  # Faster model
   ```

---

## Performance Issues

### Issue: "Recipe runs very slowly"

**Causes and solutions:**

1. **Too many sequential steps:**
   ```yaml
   # Sequential (slower)
   steps:
     - id: "step1"  # Waits for completion
     - id: "step2"  # Waits for completion
     - id: "step3"  # Waits for completion

   # Use parallel foreach for independent analyses
   context:
     perspectives: ["security", "performance", "quality"]

   steps:
     - id: "multi-analysis"
       foreach: "{{perspectives}}"
       as: "perspective"
       parallel: true  # All run concurrently
       collect: "analyses"
       agent: "analyzer"
       prompt: "Analyze from {{perspective}} perspective"
   ```

2. **Large context passed between steps:**
   ```yaml
   # ❌ Passing large document
   - output: "full_document"
   - prompt: "Analyze {{full_document}}"

   # ✅ Passing summary
   - output: "summary"
   - prompt: "Analyze {{summary}}"
   ```

3. **Unnecessary steps:**
   - Review each step: is it needed?
   - Combine steps that could be one
   - Remove validation steps for dev/testing

### Issue: "High memory usage"

**Cause:** Large context accumulation across many steps.

**Solution:**

1. **Clear unused variables (future feature):**
   ```yaml
   - id: "large-analysis"
     output: "large_result"

   - id: "summarize"
     prompt: "Summarize: {{large_result}}"
     output: "summary"
     # Future: clear: ["large_result"]  # Free memory
   ```

2. **Store only what's needed:**
   ```yaml
   # ❌ Store everything
   - id: "analyze"
     output: "complete_analysis"  # 10MB of data

   # ✅ Store only key findings
   - id: "analyze"
     prompt: "List top 10 findings"
     output: "key_findings"  # Much smaller
   ```

---

## Variable Problems

### Issue: "Variable contains unexpected value"

**Cause:** Variable overwritten or not passed correctly.

**Solution:**

1. **Check variable shadowing:**
   ```yaml
   context:
     file_path: "original.py"

   steps:
     - id: "override"
       prompt: "Set file_path to modified.py"
       output: "file_path"  # Shadows context variable!

     - id: "use"
       prompt: "Analyze {{file_path}}"  # Gets "modified.py", not "original.py"
   ```

2. **Use namespaced variables:**
   ```yaml
   context:
     input_file: "original.py"

   steps:
     - output: "modified_file"  # Different name
   ```

3. **Debug with session state:**
   ```bash
   # View all variables at any point
   cat ~/.amplifier/projects/<project>/recipe-sessions/<session-id>/state.json | \
     jq '.context'
   ```

### Issue: "Template variable not substituting"

**Symptom:**
```
Output shows: "Analyze {{file_path}}" instead of "Analyze src/auth.py"
```

**Cause:** Incorrect template syntax or escaping.

**Solution:**

1. **Check syntax:**
   ```yaml
   # ❌ Wrong
   prompt: "Analyze {file_path}"     # Single braces
   prompt: "Analyze { {file_path} }" # Space in braces

   # ✅ Correct
   prompt: "Analyze {{file_path}}"
   ```

2. **Escape if you need literal braces:**
   ```yaml
   # To output literal "{{file_path}}"
   prompt: "Template syntax: \\{{variable\\}}"
   ```

---

## JSON and Data Format Issues

### Issue: "Cannot access field on step output"

**Symptom:**
```
Error: Variable undefined: {{commits_data.count}}
# Or output shows literal "{{commits_data.count}}" instead of value
```

**Cause:** Step output is stored as a string, not parsed JSON. Field access (`.field`) only works on parsed objects.

**Solution:**

Add `parse_json: true` to steps whose output you'll access via field notation:

```yaml
# ❌ Wrong: bash outputs JSON string, stored as string
- id: "get-commits"
  type: "bash"
  command: "git log --oneline | wc -l | jq '{count: .}'"
  output: "commits_data"

- id: "use-count"
  prompt: "There are {{commits_data.count}} commits"  # FAILS: commits_data is a string

# ✅ Correct: parse_json extracts JSON from output
- id: "get-commits"
  type: "bash"
  command: "git log --oneline | wc -l | jq '{count: .}'"
  output: "commits_data"
  parse_json: true  # Now commits_data is an object

- id: "use-count"
  prompt: "There are {{commits_data.count}} commits"  # Works!
```

**When to use `parse_json: true`:**

| Step Type | Use `parse_json: true` When |
|-----------|----------------------------|
| `bash` | Output is JSON you'll access via `{{var.field}}` |
| `agent` | Agent returns structured data you'll access via `{{var.field}}` |

**When NOT to use it:**
- Output is prose/markdown you'll pass as-is to another step
- You only need the raw string value

### Issue: "Bash step produces malformed JSON"

**Symptom:**
```
jq: parse error (in bash command output)
# Or downstream steps fail with JSON parsing errors
```

**Cause:** Shell variable expansion corrupts JSON when content has quotes, newlines, or special characters.

**Solution:**

Use `jq` to construct JSON safely—never use shell interpolation:

```bash
# ❌ Wrong: Shell expansion breaks on quotes/newlines
json_var='{"message": "Hello \"world\""}'
echo "{\"items\": $json_var}"  # Breaks!

# ✅ Correct: Use jq for safe JSON construction
echo "$json_var" | jq -c '{items: .}'

# ✅ Correct: Read from file
jq -c '{items: .}' data.json

# ✅ Correct: Combine multiple values
jq -n --arg msg "$message" --argjson count "$count" \
  '{message: $msg, count: $count}'
```

**Pattern for bash steps producing JSON:**

```yaml
- id: "gather-data"
  type: "bash"
  command: |
    # Gather data into temp files
    git log --oneline -5 > /tmp/commits.txt
    
    # Use jq to construct JSON safely
    jq -Rsc 'split("\n") | map(select(. != "")) | {commits: ., count: length}' /tmp/commits.txt
  output: "git_data"
  parse_json: true
```

### Issue: "Agent output not parsing as expected"

**Symptom:**
```
# Agent was asked for JSON, but {{result.field}} doesn't work
# Or result contains prose with JSON embedded in it
```

**Cause:** Agents return natural language by default. Without `parse_json: true`, the entire response is stored as a string.

**Solution:**

```yaml
# ❌ Wrong: Agent returns prose, stored as string
- id: "analyze"
  agent: "foundation:zen-architect"
  prompt: "Return JSON with {findings: [...], severity: 'high'|'medium'|'low'}"
  output: "analysis"

- id: "check"
  condition: "{{analysis.severity}} == 'high'"  # FAILS: analysis is a string

# ✅ Correct: parse_json extracts JSON from prose response
- id: "analyze"
  agent: "foundation:zen-architect"
  prompt: "Return JSON with {findings: [...], severity: 'high'|'medium'|'low'}"
  output: "analysis"
  parse_json: true  # Extracts JSON from agent's response

- id: "check"
  condition: "{{analysis.severity}} == 'high'"  # Works!
```

**Tip:** When using `parse_json: true` with agents, be explicit in your prompt about the expected JSON structure.

---

## The Process Hangs After the Recipe Finishes

### Symptom: every step completed, the outputs are on disk, and nothing is ever printed

```
# The recipe's own work is finished:
#   state.json  -> completed_steps: ['extract-mechanisms', 'synthesize-model']
#   output file -> written, complete
# And then... nothing. No status, no execution_mode, no result JSON.
$ ss -tnp | grep <pid>
CLOSE-WAIT  127.0.0.1:39188  127.0.0.1:8100     # telemetry
CLOSE-WAIT  192.168.1.5:36822  <api-host>:443   # model provider
```

The process sleeps indefinitely and exits instantly on `SIGTERM`.

**Cause:** not the recipe. Something *downstream of the run* -- a provider's
HTTP client, a telemetry exporter, a spawned session's transport -- is closing
a half-closed (CLOSE-WAIT) connection with no deadline. `httpx.AsyncClient
.aclose()` has none of its own, and on a half-closed connection it can block
forever.

A hang here is worse than a failure: the work succeeded, and no caller can
learn it.

**What the recipes tool now does about it** (recipes-8sr):

1. It **logs the run's outcome the moment the engine returns**, before any
   teardown is allowed to block on it. If the process later wedges, the log
   still carries what happened.
2. It **bounds its own post-run drain**. Background work the run started gets
   at most `shutdown_drain_timeout` seconds (default 30), then is named in a
   warning and abandoned:

   ```
   recipes execute completed (success=True {'status': 'completed', ...}), but
   background work it started did not drain within 30s and was abandoned:
   task 'ci-exporter' (_deliver). The run's result is complete; the process may
   stay alive until the work named here ends.
   ```

**Solution:**

1. **Read the warning.** It names what did not drain. That name is the owner to
   chase -- the recipes tool cannot close a client it does not own.
2. **Tune the bound** if 30s is wrong for your host:

   ```yaml
   tools:
     - module: tool-recipes
       config:
         shutdown_drain_timeout: 10   # seconds; 0 = report immediately, never wait
   ```

   A negative or non-numeric value is refused at mount time rather than
   silently defaulted.
3. **If nothing is named and the process still hangs,** the block is *outside*
   the recipes tool -- most often in the host's own session cleanup, which runs
   after the tool has already returned. Capture a thread dump before killing it:

   ```bash
   PYTHONFAULTHANDLER=1 amplifier tool invoke recipes ... # then: kill -ABRT <pid>
   # or: py-spy dump --pid <pid>   (may need sudo)
   ```

---

## Auditing a Finished Run: `steps.jsonl`

Every recipe session writes an append-only per-step log beside `recipe.yaml`
and `state.json`:

```
<session-id>/
├── recipe.yaml     # the recipe as it was when the run started
├── state.json      # resumability checkpoint (which steps finished)
└── steps.jsonl     # what each step was asked, what came back, how long it took
```

`state.json` answers *which* steps finished. `steps.jsonl` answers **what was
this step actually asked, and how long did it take** — the two facts that exist
only at run time and cannot be reconstructed from the recipe file: the prompt
*after* `{{variable}}` substitution, and the provider/model the step actually
resolved to.

### Two lines per step attempt

A step writes a `started` line **before** its body runs and a `finished` line
when it settles. The pair shares a `record_id`.

The `started` line is the point: `state.json` is rewritten only at step
boundaries, so a run killed mid-step leaves no trace that a step was ever in
flight. A `started` line with no matching `finished` line is exactly that
trace — **the step that was running when the process died**.

```bash
SESSION=$(ls -td ~/.amplifier/projects/*/recipe-sessions/*/ | head -1)

# Which step died in flight?
jq -s 'group_by(.record_id)[] | select(length == 1) | .[0]
       | {step_id, started_at, prompt_resolved}' "$SESSION/steps.jsonl"
```

### Common questions

```bash
# Timeline: what ran, in what order, with what outcome and duration
jq -r 'select(.event=="finished")
       | "\(.started_at)  \(.status|ascii_upcase)  \(.step_id)  \(.duration_s)s"' \
  "$SESSION/steps.jsonl"

# Which step was slow?
jq -s 'map(select(.event=="finished"))
       | sort_by(-.duration_s)[:5]
       | map({step_id, duration_s, step_type})' "$SESSION/steps.jsonl"

# What was this step ACTUALLY asked, post-substitution?
jq -r 'select(.step_id=="identify-key-points" and .event=="finished")
       | .prompt_resolved' "$SESSION/steps.jsonl"

# Which model did it really resolve to? (not what the YAML asked for)
jq -r 'select(.event=="finished" and .provider)
       | "\(.step_id): \(.provider)/\(.model)"' "$SESSION/steps.jsonl"

# Anything that did not simply succeed
jq -c 'select(.event=="finished" and .status != "completed")
       | {step_id, status, reason, error}' "$SESSION/steps.jsonl"

# What a bash step ran, and what it printed
jq -r 'select(.step_type=="bash" and .event=="finished")
       | "$ \(.command)\nexit \(.exit_code)\n\(.stdout)"' "$SESSION/steps.jsonl"
```

### Record fields

| Field | Meaning |
|-------|---------|
| `v` | Record schema version (currently `1`) |
| `record_id` | Joins a step's `started` line to its `finished` line |
| `event` | `started` (in flight) or `finished` (settled) |
| `status` | On `finished` only — see the status table below |
| `step_id`, `step_type` | `agent` / `bash` / `recipe` / `foreach` / `while` |
| `attempt` | 1-based; a retried step writes one record **per attempt** |
| `started_at`, `finished_at`, `duration_s` | ISO-8601 UTC, and elapsed seconds |
| `parent_step_id`, `iteration` | Set for foreach/while bodies and sub-recipe steps; `null` at top level |
| `stage`, `step_index`, `parallel_group_id` | Position within the run |
| `prompt_resolved` | Agent steps: the prompt **as sent**, mode prefix and all |
| `response` | Agent steps: what came back |
| `provider`, `model`, `model_role` | What the step resolved to. Absent when no chain was pinned and the spawn ran on the parent session's own ordering — that is a different fact from "no provider", so it is left unstated rather than guessed |
| `agent_session_id`, `turn_count`, `agent_status` | The spawned agent's own Amplifier session — the hop to its full transcript |
| `command`, `cwd`, `exit_code`, `stdout`, `stderr` | Bash steps |
| `sub_recipe`, `sub_recipe_path` | Recipe steps: what the path resolved to |
| `error`, `reason` | Why it failed / why it was skipped |

### Status vocabulary

| Status | Meaning |
|--------|---------|
| `completed` | Succeeded |
| `failed` | Raised, or exited non-zero — recorded even when `on_error: continue` absorbed it |
| `skipped` | Never ran: condition was false, or a `foreach` list was empty |
| `retried` | This attempt failed and another followed |
| `timed_out` | Exceeded the step's `timeout` |
| `paused` | Stopped at an approval gate — not a failure |
| `cancelled` | Cancellation was requested |

A step that is simply **absent** from the log was never reached. That is the
distinction `state.json` cannot make: a skipped step and an unreached step are
both merely missing from `completed_steps`.

### Sub-recipes span two files

A `type: recipe` step gets a record in the **parent's** log (how long the
sub-recipe took as a unit, and which file it resolved to). The sub-recipe's own
steps are recorded in the **child's** session directory, each naming the
composing step as its `parent_step_id`. The child's `state.json` also carries
`parent_session_id`, so the link can be walked from either end.

### Truncation is marked, never silent

Large payloads are capped at 64 KB each. Any capped field is written with two
companions:

```json
{"response": "…first 64 KB…", "response_truncated": true, "response_bytes": 5242880}
```

`<field>_truncated` is **always** present (`false` when the value is whole), so
completeness never has to be inferred from a missing marker, and
`<field>_bytes` preserves the original size.

The cap is safe precisely because nothing is actually lost: the agent's own
Amplifier session keeps the full transcript, and `agent_session_id` is the hop
to it.

```bash
# Full detail for a step whose response was truncated here
jq -r 'select(.step_id=="summarise" and .response_truncated==true)
       | .agent_session_id' "$SESSION/steps.jsonl"
```

### Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `AMPLIFIER_RECIPE_STEPS_LOG` | on | `0`/`false`/`no`/`off` disables the log entirely |
| `AMPLIFIER_RECIPE_STEPS_LOG_MAX_BYTES` | `65536` | Per-field byte cap; `0` means no cap |

Writing the log is fail-soft: if it cannot be written, the run continues
without it and the reason is logged at DEBUG level. It never changes what a run
does, and it never changes `state.json`.

---

## Which Engine Actually Ran? (Proving a Change Live)

You changed engine code in a worktree, ran `amplifier tool invoke recipes ...`,
and it passed. **That is not yet evidence.** On a machine with more than one
checkout of this repo, the run may have executed somebody else's code and said
nothing about it.

### The one command

```bash
# From your worktree. Asserts the engine, THEN runs your invocation.
./scripts/live-proof.sh "$PWD" -- recipes operation=validate \
    recipe_path=examples/bash-step-example.yaml -b anchors-amp-dev
```

It exits non-zero, naming the path that actually answered, if the engine is not
the worktree you pointed it at. `live-proof.sh <worktree>` with no `--` runs the
assertion alone and nothing else.

It needs **no cache symlink and no venv reinstall**: `PYTHONPATH` is consulted
before `site-packages`, and the tool-recipes editable install is a plain-path
`.pth` rather than an import-hook finder, so putting the worktree first wins for
that one process and changes nothing on disk for anyone else.

### Ask the engine who it is

```bash
amplifier tool invoke recipes operation=engine_info -b anchors-amp-dev -o json
```

`engine_info` starts no session and touches no recipe. It reports:

| Field | Answers |
|-------|---------|
| `module_file` / `module_dir` | which file is running, exactly as imported |
| `module_dir_real` | where that path *resolves* — differs when a symlink is in play |
| `package_version` | installed distribution version, when there is one |
| `git_sha` / `git_ref` / `git_root` | the commit of the tree it was imported from |
| `bundle_cache_dir`, `bundle_cache_is_symlink`, `bundle_cache_resolves_to`, `bundle_cache_commit` | the cached bundle it came from, and whether that cache is really the cache |
| `editable_pth` | every editable install on this interpreter naming this module, and where each points |
| `warnings` | the shadowing conditions below, as sentences naming both paths |

The same record rides on **every** result as `result.engine` (beside the
payload, never inside it — the legacy-compat baselines pin `execute`/`resume`
payloads byte-for-byte), and opens every `steps.jsonl` as a `header` line:

```bash
jq -r 'select(.event=="header") | .engine | "\(.module_dir_real)  \(.git_sha[0:12])"' \
  "$SESSION/steps.jsonl"
```

One header line per process, so a run **resumed by a different engine** says so
rather than looking seamless.

### DO NOT: symlink the bundle cache

```bash
# ❌ NEVER
ln -s /path/to/my/worktree ~/.amplifier/cache/amplifier-bundle-recipes-<hash>
```

That path is the cache key **every** bundle mounts tool-recipes from. While the
symlink exists, every `amplifier` run on the host — other lanes, other agents,
real user sessions — executes your worktree's engine, whatever bundle they
asked for. A recorded incident (`recipes-669`): a lane registered its own local
bundle, ran a recipe that had to trip a warning it had just added, and the
warning never fired — the module still came from the symlinked tree. The run
completed and looked like a clean pass.

### DO NOT: `uv pip install -e` into the CLI's venv

```bash
# ❌ NEVER
uv pip install -e modules/tool-recipes  # into the amplifier tool venv
```

Running `amplifier` re-resolves editable installs and **rewrites**
`.../site-packages/_editable_impl_amplifier_module_tool_recipes.pth` to the
resolved realpath. Removing a symlink afterwards does not undo it: a recorded
incident (`recipes-ecd`) restored the cache directory correctly — symlink gone,
real directory back, `.amplifier_cache_meta.json` intact, cache `git status`
clean — and the CLI still imported the lane's worktree until the `.pth` was
edited by hand. The leak outlives the procedure that caused it.

### The engine warns when it notices

At import, tool-recipes checks whether it was loaded out of the bundle cache
while the cache is a symlink, or while an editable `.pth` names somewhere else.
Either way it logs a WARNING naming **both** paths:

```
tool-recipes engine: bundle cache /home/u/.amplifier/cache/amplifier-bundle-recipes-<hash>
is a SYMLINK resolving to /lanes/other-lane -- every `amplifier` run on this
machine imports that tree, not the cached bundle.
```

It only warns. A diagnostic that could fail a run would be a worse bug than the
silence it replaces. Set `AMPLIFIER_RECIPES_ENGINE_GUARD=0` to silence it.

### If the assertion fails

| Symptom | Meaning | Fix |
|---------|---------|-----|
| `WRONG ENGINE`, `actual` under another lane | The cache is symlinked, or a `.pth` points at that lane | `ls -l ~/.amplifier/cache/amplifier-bundle-recipes-*` and `cat .../site-packages/_editable_impl_amplifier_module_tool_recipes.pth`; restore **both** |
| `NO ENGINE PROVENANCE`, `Unknown operation: engine_info` | The engine that answered predates `engine_info`, so it is not your worktree | Check `PYTHONPATH` reached the CLI; do not "fix" it by installing |
| `not a directory` (exit 3) | First argument is not a worktree root | Pass the repo root, not `modules/tool-recipes` |

---

## Debugging Tips

### Enable Detailed Logging

```yaml
# In your profile
tools:
  - module: tool-recipes
    config:
      log_level: "DEBUG"  # More verbose logging
```

### Inspect Session State

```bash
# View current session state
SESSION=$(ls -t ~/.amplifier/projects/*/recipe-sessions/ | head -1)
cat ~/.amplifier/projects/*//recipe-sessions/$SESSION/state.json | jq '.'
```

`state.json` tells you *which* steps finished. For what each step was actually
asked, what it replied, and how long it took, read `steps.jsonl` in the same
directory — see [Auditing a Finished Run](#auditing-a-finished-run-stepsjsonl).

### Test Steps Individually

Create minimal recipe with just the problematic step:

```yaml
name: "test-step"
description: "Testing problematic step in isolation"
version: "1.0.0"

context:
  # Use same context as full recipe
  file_path: "test.py"

steps:
  - id: "test"
    agent: "analyzer"
    # Copy exact prompt from full recipe
    prompt: "Analyze {{file_path}}"
```

### Use Validation Before Execution

```bash
# Validate recipe without executing
amplifier run "validate recipe my-recipe.yaml"

# Shows all potential issues before execution
```

### Check Event Logs

```bash
# View all events for a session
cat ~/.amplifier/projects/<project>/recipe-sessions/<session-id>/events.jsonl | \
  jq 'select(.event | startswith("step:"))' | \
  jq '{step: .data.step_id, event: .event, status: .status}'
```

### Common Log Filters

```bash
# Show all errors
cat events.jsonl | jq 'select(.status == "error")'

# Show step timings
cat events.jsonl | jq 'select(.event | contains("step:")) | {step: .data.step_id, duration_ms: .duration_ms}'

# Show agent invocations
cat events.jsonl | jq 'select(.event == "agent:spawn")'
```

---

## Getting Help

### Self-Service

1. **Check documentation:**
   - [Recipe Schema Reference](RECIPE_SCHEMA.md)
   - [Recipes Guide](RECIPES_GUIDE.md)
   - [Best Practices](BEST_PRACTICES.md)
   - [Examples Catalog](EXAMPLES_CATALOG.md)

2. **Use recipe-author agent:**
   ```bash
   amplifier run "validate my-recipe.yaml and explain any issues"
   ```

3. **Search examples:**
   - Browse `examples/` directory
   - Look for similar patterns

### Community Support

1. **GitHub Discussions:**
   - [amplifier-collection-recipes/discussions](https://github.com/microsoft/amplifier-collection-recipes/discussions)
   - Search existing discussions
   - Ask new questions

2. **GitHub Issues:**
   - [amplifier-collection-recipes/issues](https://github.com/microsoft/amplifier-collection-recipes/issues)
   - Report bugs
   - Request features

### When Reporting Issues

Include:

1. **Recipe YAML** (or minimal reproduction)
2. **Error message** (complete text)
3. **Session ID** (if applicable)
4. **Environment:**
   - Amplifier version: `amplifier --version`
   - Collection version
   - Installed agents: `amplifier agents list`
5. **Steps to reproduce**
6. **Expected vs actual behavior**

---

**Still stuck?** Join the discussions on GitHub - the community is here to help!
