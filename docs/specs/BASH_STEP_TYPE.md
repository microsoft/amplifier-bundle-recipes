# Specification: `type: "bash"` Step Type

**Status:** Draft  
**Version:** 0.1.0  
**Author:** Amplifier Team  
**Created:** 2025-12-29  

---

## Summary

Add a new step type `type: "bash"` that executes shell commands directly without spawning an LLM agent. This eliminates unnecessary overhead for deterministic operations like API calls, file operations, and data transformations.

---

## Motivation

### Problem Statement

Currently, all recipe steps require agent invocation:

```yaml
- id: "fetch-issues"
  agent: "foundation:explorer"
  prompt: |
    Run this command:
    ```bash
    gh api repos/{{owner}}/{{repo}}/issues
    ```
```

Even though the work is a simple shell command, this incurs:

| Overhead | Cost |
|----------|------|
| Agent spawn | 1-3 seconds latency |
| LLM invocation | Token costs + API latency |
| Context marshalling | Prompt construction + response parsing |
| Non-determinism | Agent may add commentary or modify command |

**Real-world impact:** A recipe that checks activity across 46 repositories was spawning 46 agents just to run `gh api` commands, hitting rate limits and taking 10+ minutes.

### Use Cases for Direct Shell Execution

| Use Case | Example | Why Agent is Overkill |
|----------|---------|----------------------|
| API calls | `gh api repos/.../issues` | Deterministic, no reasoning needed |
| File operations | `mkdir -p build && cp...` | Trivial, sub-millisecond |
| JSON transforms | `jq '.dependencies' package.json` | Native tool, faster than LLM |
| Git operations | `git log --oneline -10` | Direct output, no interpretation |
| Environment setup | `export VAR=value` | Pure side effect |
| Data fetching | `curl -s https://api.example.com` | Network I/O, no reasoning |

### Design Principles

1. **Right tool for the job:** LLM for reasoning, shell for execution
2. **Zero overhead for deterministic work:** No token costs for `mkdir`
3. **Predictable behavior:** Same input → same output (deterministic)
4. **Composable:** Bash steps can feed into agent steps and vice versa

---

## Specification

### Schema

```yaml
- id: string                    # Required - Unique step identifier
  type: "bash"                  # Required - Specifies bash step type (no default)
  command: string               # Required - Shell command(s) to execute
  
  # Output handling
  output: string                # Optional - Variable name for stdout
  output_stderr: string         # Optional - Variable name for stderr
  parse_json: boolean           # Optional - Parse stdout as JSON (default: false)
  
  # Execution control
  shell: string                 # Optional - Shell to use (default: "/bin/bash")
  cwd: string                   # Optional - Working directory
  env: dict                     # Optional - Environment variables
  timeout: integer              # Optional - Max execution time in seconds (default: 60)
  max_output_size: integer      # Optional - Max stdout size in bytes (default: 10485760 = 10MB)
  
  # Error handling
  on_error: string              # Optional - "fail" | "continue" | "skip_remaining"
  retry: RetryConfig            # Optional - Retry configuration
  
  # Dependencies
  depends_on: list[string]      # Optional - Step IDs that must complete first
  
  # Conditional execution
  condition: string             # Optional - Expression that must evaluate to true
  
  # Looping
  foreach: string               # Optional - Variable containing list to iterate
  as: string                    # Optional - Loop variable name (default: "item")
  collect: string               # Optional - Variable to collect results
  parallel: boolean             # Optional - Run iterations in parallel (default: false)
  max_iterations: integer       # Optional - Safety limit (default: 100)
  delay_between: integer        # Optional - Milliseconds between iterations (default: 0)
```

### Mutual Exclusivity with Agent Steps

A step is either a bash step OR an agent step, never both:

```yaml
# Agent step (existing):
- id: "analyze"
  type: "agent"    # Default, can be omitted
  agent: string    # Required for agent steps
  prompt: string   # Required for agent steps

# Bash step (new):
- id: "fetch"
  type: "bash"     # Required, no default
  command: string  # Required for bash steps
```

**Validation:** If `type: "bash"`, then `command` is required and `agent`/`prompt` are forbidden.

### Field Definitions

#### `command` (required)

The shell command(s) to execute. Supports:

- **Single command:** `"gh api repos/{{owner}}/{{repo}}/issues"`
- **Multi-line script:** Using YAML literal block scalar

```yaml
command: |
  cd {{working_dir}}
  gh api repos/{{owner}}/{{repo}}/issues > issues.json
  jq 'length' issues.json
```

- **Variable substitution:** `{{variable}}` syntax, same as prompts

#### `output` (optional)

Variable name to store stdout. If omitted, stdout is discarded.

```yaml
- id: "count-files"
  type: "bash"
  command: "ls -1 | wc -l"
  output: "file_count"  # {{file_count}} = "42\n"
```

#### `parse_json` (optional, default: false)

If `true`, parse stdout as JSON before storing in output variable.

```yaml
- id: "fetch-issues"
  type: "bash"
  command: "gh api repos/{{owner}}/{{repo}}/issues"
  output: "issues"
  parse_json: true  # {{issues}} = [{...}, {...}] (parsed JSON)
```

**Error behavior:** If `parse_json: true` and stdout is not valid JSON:
- Step fails with clear error message
- `on_error` handling applies

#### `shell` (optional, default: "/bin/bash")

Shell interpreter to use.

```yaml
shell: "/bin/bash"    # Default
shell: "/bin/sh"      # POSIX-compatible
shell: "/bin/zsh"     # Zsh
```

#### `cwd` (optional)

Working directory for command execution. Supports variable substitution.

```yaml
cwd: "{{working_dir}}/build"
```

**Default:** The recipe session's working directory (from `session.working_dir` capability if available, otherwise `Path.cwd()`).

#### `env` (optional)

Environment variables for the command. Merged with existing environment.

```yaml
env:
  GH_TOKEN: "{{github_token}}"
  DEBUG: "true"
  PATH: "/custom/bin:$PATH"
```

**Variable substitution:** Values support `{{variable}}` syntax.

#### `timeout` (optional, default: 60)

Maximum execution time in seconds.

```yaml
timeout: 300  # 5 minutes for long-running operations
```

**Behavior on timeout:**
- Command is terminated (SIGTERM, then SIGKILL after 5s)
- Step fails with timeout error
- `on_error` handling applies

#### `on_error` (optional, default: "fail")

Error handling strategy when command fails (non-zero exit code).

| Value | Behavior |
|-------|----------|
| `"fail"` | Stop recipe execution |
| `"continue"` | Log error, continue to next step |
| `"skip_remaining"` | Skip remaining steps, mark recipe as partial success |

#### `retry` (optional)

Retry configuration for transient failures.

```yaml
retry:
  max_attempts: 3
  backoff: "exponential"  # or "linear"
  initial_delay: 5        # seconds
  max_delay: 60           # seconds
```

**Retryable conditions:**
- Non-zero exit code
- Timeout
- NOT: Command not found, permission denied (fail fast)

#### `condition` (optional)

Skip step if condition evaluates to false. Same syntax as agent steps.

```yaml
condition: "{{should_deploy}} == 'true'"
```

---

## Behavior Specification

### Execution Model

1. **Variable substitution:** Replace all `{{variable}}` in `command`, `cwd`, `env` values
2. **Shell invocation:** Execute via specified shell with `-c` flag
3. **Output capture:** Capture stdout and stderr separately
4. **Exit code handling:** Non-zero exit code = failure (unless `on_error: "continue"`)
5. **Output storage:** Store stdout in `output` variable (optionally parsed as JSON)

### Exit Code Semantics

| Exit Code | Meaning | Default Behavior |
|-----------|---------|------------------|
| 0 | Success | Continue to next step |
| 1-125 | Command error | Fail (apply `on_error`) |
| 126 | Command not executable | Fail immediately (no retry) |
| 127 | Command not found | Fail immediately (no retry) |
| 128+ | Signal termination | Fail (apply `on_error`) |

### Stdout/Stderr Handling

- **stdout:** Captured, stored in `output` variable if specified
- **stderr:** Captured, logged, included in error message on failure
- **Interleaved output:** Not preserved (stdout and stderr captured separately)

### Input Validation

Before execution, validate:

| Check | Failure Behavior |
|-------|------------------|
| `command` is non-empty string | Fail validation (no execution) |
| `command` is not just whitespace | Fail validation (no execution) |
| `cwd` exists and is a directory | Fail before command execution |
| `shell` path exists and is executable | Fail before command execution |
| `timeout` is positive integer | Fail validation |
| `env` values are strings (or stringifiable) | Coerce to strings |

### Output Limits

- **Maximum stdout capture:** 10MB default (configurable via `max_output_size`)
- **Output exceeding limit:** Truncated with warning in session log
- **Binary content:** Stored as-is but may cause issues if used in prompts

### Security Considerations

#### Variable Substitution and Escaping

Variables are substituted **before** shell invocation using string replacement. This is NOT automatic shell escaping.

**⚠️ Important:** Variables substituted into `command` are **NOT automatically escaped** for shell metacharacters.

**Safe pattern - Use environment variables for untrusted input:**
```yaml
# SAFE: Input passed via environment variable
- id: "safe-fetch"
  type: "bash"
  command: "gh api repos/$OWNER/$REPO/issues"
  env:
    OWNER: "{{owner}}"    # Shell won't interpret metacharacters
    REPO: "{{repo}}"
```

**Dangerous pattern - Direct substitution of untrusted input:**
```yaml
# DANGEROUS: Direct substitution - injection risk!
- id: "unsafe-fetch"
  type: "bash"
  command: "gh api repos/{{user_input}}/issues"  # If user_input contains "; rm -rf /"
```

**Rationale:** Auto-escaping would break legitimate use cases (piping, redirects, subshells). Recipe authors must handle untrusted input safely.

#### Sandboxing

Commands run with the **same permissions as the recipe executor**. There is no additional sandboxing. The `cwd` field is not restricted - recipe authors are responsible for path safety.

#### Logging and Sensitive Data

Commands and environment are logged for debugging. To protect secrets:

1. **Environment variables:** Values marked as secrets in context are redacted in logs
2. **Command strings:** Not automatically redacted - avoid embedding secrets directly in commands
3. **Stdout/stderr:** Logged as-is (may contain secrets from command output)

**Best practice:**
```yaml
# Pass secrets via environment, not command string
- id: "authenticated-request"
  type: "bash"
  env:
    API_TOKEN: "{{api_token}}"  # Redacted in logs if api_token marked secret
  command: "curl -H 'Authorization: Bearer $API_TOKEN' https://api.example.com"
```

#### Variable Substitution Semantics

- **Single-pass substitution:** `{{variable}}` is replaced once; no recursive expansion
- **Undefined variables:** Fail with clear error (same as agent steps)
- **Special characters:** Preserved as-is after substitution

---

## Examples

### Example 1: Simple API Call

```yaml
- id: "fetch-user"
  type: "bash"
  command: "gh api user"
  output: "user_info"
  parse_json: true
```

### Example 2: Multi-Command Script

```yaml
- id: "setup-workspace"
  type: "bash"
  command: |
    mkdir -p {{working_dir}}/{discovery,reports,cache}
    echo '{"initialized": true, "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' 
  output: "setup_result"
  parse_json: true
```

### Example 3: With Environment Variables

```yaml
- id: "deploy-preview"
  type: "bash"
  command: "vercel deploy --prebuilt"
  cwd: "{{project_root}}"
  env:
    VERCEL_TOKEN: "{{vercel_token}}"
    VERCEL_PROJECT_ID: "{{project_id}}"
  output: "deploy_url"
  timeout: 300
```

### Example 4: Conditional Execution

```yaml
- id: "run-tests"
  type: "bash"
  condition: "{{skip_tests}} != 'true'"
  command: "npm test"
  cwd: "{{project_root}}"
  on_error: "continue"
```

### Example 5: With Retry

```yaml
- id: "fetch-with-retry"
  type: "bash"
  command: "curl -sf https://api.example.com/data"
  output: "api_data"
  parse_json: true
  retry:
    max_attempts: 3
    backoff: "exponential"
    initial_delay: 2
```

### Example 6: Batch Processing (foreach)

```yaml
- id: "check-repos"
  type: "bash"
  foreach: "{{repos}}"
  as: "repo"
  collect: "repo_statuses"
  parallel: true
  command: "gh api repos/{{repo.owner}}/{{repo.name}} --jq '{name: .name, stars: .stargazers_count}'"
  parse_json: true
  timeout: 30
```

---

## Migration Guide

### Before: Agent for Shell Work

```yaml
# OLD: Agent overhead for simple command
- id: "fetch-issues"
  agent: "foundation:explorer"
  parse_json: true
  prompt: |
    Run this exact command and return only the JSON output:
    
    ```bash
    gh api repos/{{owner}}/{{repo}}/issues
    ```
  output: "issues"
```

### After: Direct Bash Execution

```yaml
# NEW: Direct execution, no LLM overhead
- id: "fetch-issues"
  type: "bash"
  command: "gh api repos/{{owner}}/{{repo}}/issues"
  output: "issues"
  parse_json: true
```

### Migration Checklist

1. **Identify shell-only steps:** Steps where prompt just wraps a bash command
2. **Convert to `type: "bash"`:** Replace `agent` + `prompt` with `command`
3. **Keep `parse_json`:** If step expected JSON output
4. **Preserve error handling:** Migrate `on_error`, `timeout`, `retry`
5. **Test behavior:** Verify output format matches expectations

---

## Implementation Considerations

### Recipe Executor Changes

1. **Step type dispatch:** Check `type` field, route to appropriate handler
2. **Bash handler:** New handler that:
   - Substitutes variables in command
   - Spawns shell subprocess
   - Captures stdout/stderr
   - Handles exit codes
   - Stores output in context

### Checkpointing

Bash steps should checkpoint like agent steps:
- Store exit code, stdout, stderr in session state
- Allow resume from last successful step

### Event Emission

Emit events for observability:
- `step:bash:start` - Command about to execute
- `step:bash:complete` - Command finished (with exit code, duration)
- `step:bash:error` - Command failed

### Rate Limiting for `foreach`

When `type: "bash"` is used with `foreach`:
- Implement configurable delay between iterations
- Respect `parallel: true/false`

```yaml
foreach: "{{repos}}"
parallel: false
delay_between: 500  # milliseconds (new field, optional)
```

---

## Alternatives Considered

### 1. Special Agent Mode

```yaml
- agent: "foundation:executor"
  mode: "direct"
  command: "gh api ..."
```

**Rejected:** Still requires agent infrastructure, just bypasses LLM.

### 2. Built-in Tool Steps

```yaml
- type: "tool"
  tool: "bash"
  input:
    command: "gh api ..."
```

**Rejected:** Adds indirection, less intuitive than direct bash type.

### 3. Pre/Post Hooks

```yaml
pre_steps:
  - bash: "mkdir -p {{working_dir}}"
```

**Rejected:** Limited to setup/teardown, can't interleave with agent steps.

---

## Future Considerations

### 1. Other Step Types

The `type` field opens the door for additional step types:
- `type: "http"` - Direct HTTP requests without shell
- `type: "transform"` - Pure data transformation (jq/JSONPath)
- `type: "wait"` - Explicit delay/sleep

### 2. Streaming Output

For long-running commands, support streaming stdout to logs:
```yaml
stream: true  # Stream output in real-time
```

### 3. Interactive Commands

Support for commands requiring input (with caveats):
```yaml
stdin: "y\n"  # Provide stdin input
```

---

## References

- [Recipe Schema Reference](../RECIPE_SCHEMA.md)
- [Best Practices](../BEST_PRACTICES.md)
- [Original Issue: foreach spawns too many agents](#) (link TBD)

---

## Open Questions

1. **Should `output_exit_code` be added?** - Allow steps to access exit code in subsequent conditions
2. **Parallel execution limits?** - Should `parallel: true` have a max concurrency setting?
3. **Shell detection?** - Auto-detect available shell on Windows vs Unix?

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 0.1.0 | 2025-12-29 | Initial draft |
| 0.2.0 | 2025-12-29 | Added security guidance, input validation, output limits, `depends_on`, `output_stderr`, `max_output_size`, `delay_between`, `max_iterations` |
