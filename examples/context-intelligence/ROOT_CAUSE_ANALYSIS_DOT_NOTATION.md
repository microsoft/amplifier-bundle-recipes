# Root Cause Analysis: Dot Notation Access to JSON in Recipe Outputs

## Issue Summary

When a recipe step stores an agent's output that contains JSON, subsequent steps cannot access nested properties using dot notation (e.g., `{{result.property}}`). This forces users to add intermediate parsing steps, making recipes more complex than necessary.

## Example Scenario

```yaml
steps:
  - id: "select-files"
    agent: "foundation:zen-architect"
    prompt: |
      Select files to analyze.
      
      OUTPUT FORMAT (JSON):
      {
        "selected_files": ["/path/1.py", "/path/2.py"],
        "rationale": "These are the core files"
      }
    output: "selection"
  
  - id: "analyze-files"
    foreach: "{{selection.selected_files}}"  # ❌ FAILS - "selection" is a string, not a dict
    agent: "foundation:analyzer"
    prompt: "Analyze {{item}}"
```

**Error:**
```
Step 'analyze-files': foreach variable must be a list, got dict
```

## Root Cause Analysis

### 1. How Agent Results Are Stored

When a recipe step executes, the flow is:

```python
# executor.py, line 619
result = await spawn_fn(
    agent_name=step.agent,
    instruction=instruction,
    ...
)

# executor.py, line 265
if step.output:
    context[step.output] = result  # Store the raw spawn result
```

### 2. What spawn_fn Returns

The `spawn_fn` capability (implemented in `Bundle.spawn()`) returns:

```python
# bundle.py, line 695
{
    "output": "<agent response text>",  # The actual text response
    "session_id": "recipe_xyz_abc123"
}
```

**Key insight:** The agent's response is in the `"output"` field as a **string**, even if that string contains JSON.

### 3. Actual Context Structure

When an agent returns JSON text, the context looks like:

```python
context = {
    "selection": {
        "output": '{"selected_files": ["/p1.py", "/p2.py"], "rationale": "..."}',  # JSON as STRING
        "session_id": "recipe_xyz_abc123"
    }
}
```

**Not:**
```python
context = {
    "selection": {
        "selected_files": ["/p1.py", "/p2.py"],  # Parsed JSON (this doesn't happen)
        "rationale": "..."
    }
}
```

### 4. How Dot Notation Works

The variable substitution code supports dot notation:

```python
# executor.py, lines 894-911
pattern = r"\{\{(\w+(?:\.\w+)?)\}\}"

if "." in var_ref:
    parts = var_ref.split(".")
    value = context
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]  # Traverse dict hierarchy
        else:
            raise ValueError(...)
```

**Problem:** This only works for **nested dicts**, not for **JSON strings**.

### 5. Why `{{selection.selected_files}}` Fails

When resolving `{{selection.selected_files}}`:

1. Get `context["selection"]` → `{"output": "...", "session_id": "..."}`
2. Try to get `["selected_files"]` from that dict
3. **Key not found** - "selected_files" is INSIDE the JSON string in "output", not a dict key

### 6. Why `foreach: "{{selection}}"` Says "got dict"

When resolving `{{selection}}` for foreach:

1. Get `context["selection"]` → `{"output": "...", "session_id": "..."}`
2. foreach expects a **list**
3. Error: "foreach variable must be a list, got **dict**"

The error message is misleading - the issue isn't that it's a dict, it's that the JSON is unparsed inside the dict.

## Current Workaround

Users must add intermediate parsing steps:

```yaml
steps:
  - id: "select-files"
    agent: "foundation:zen-architect"
    prompt: |
      Select files.
      OUTPUT: JSON with "selected_files" array
    output: "selection_raw"
  
  - id: "parse-selection"  # ← Extra step needed
    agent: "foundation:zen-architect"
    prompt: |
      Extract ONLY the selected_files array from this JSON:
      {{selection_raw}}
      
      Return ONLY the array, no markdown, no explanation.
    output: "file_list"
  
  - id: "analyze-files"
    foreach: "{{file_list}}"  # ✓ Now works
    agent: "foundation:analyzer"
    prompt: "Analyze {{item}}"
```

**Problems with this approach:**
- Extra step for every JSON output
- Extra LLM call (cost, latency)
- Fragile (agent might add markdown, explanation)
- Verbose recipe files

## Proposed Solutions

### Option 1: Unwrap spawn() Result (Simplest)

**Change:** Store only the `"output"` field, not the full spawn result.

```python
# executor.py, line 265 (current)
if step.output:
    context[step.output] = result  # {"output": "...", "session_id": "..."}

# executor.py, line 265 (proposed)
if step.output:
    context[step.output] = result.get("output", result)  # Just the text
```

**Pros:**
- Simple one-line change
- Users access the actual content, not a wrapper
- Backwards compatible (if accessing `.output` explicitly, still works)

**Cons:**
- Loses session_id (could store separately as `{step.output}_session_id`)
- Doesn't solve the JSON parsing issue

**Impact:** Medium - makes accessing agent text easier, but JSON still needs parsing.

---

### Option 2: Auto-Parse JSON Responses (Best UX)

**Change:** Detect and parse JSON in agent responses.

```python
# executor.py, after line 626
result = await spawn_fn(...)

# NEW: Auto-parse JSON if present
output_text = result.get("output", result)
try:
    import json
    parsed = json.loads(output_text)
    result = parsed  # Use parsed JSON as result
except (json.JSONDecodeError, TypeError):
    result = output_text  # Keep as string if not valid JSON
```

**Pros:**
- **Best user experience** - dot notation "just works" for JSON
- Recipes become simpler (no parsing steps)
- Aligns with user expectations

**Cons:**
- Might surprise users if they expect text to remain unparsed
- Could break recipes that explicitly handle JSON strings
- Need to handle edge cases (JSON in markdown code blocks)

**Impact:** High - solves the core issue, but needs careful implementation.

---

### Option 3: Explicit JSON Parsing Function

**Change:** Add a `json()` function to variable substitution.

```yaml
steps:
  - id: "select-files"
    output: "selection"
  
  - id: "analyze-files"
    foreach: "{{json(selection).selected_files}}"  # Explicit parsing
    agent: "foundation:analyzer"
    prompt: "Analyze {{item}}"
```

**Implementation:**
```python
# executor.py, in substitute_variables()
pattern = r"\{\{(json\()?([\w.]+)\)?\}\}"

if match.group(1) == "json(":  # json() function detected
    value = context[var_ref]
    value = json.loads(value)
```

**Pros:**
- Explicit and clear
- No surprising auto-behavior
- Backwards compatible

**Cons:**
- Verbose syntax
- Still requires awareness of when to use `json()`
- More complex implementation

**Impact:** Medium - solves issue with opt-in approach.

---

### Option 4: Document Limitation + Provide Helper Agent

**Change:** No code changes, just better documentation and a helper agent.

**Helper agent:** `recipes:json-parser`
```yaml
- id: "parse-json"
  agent: "recipes:json-parser"
  prompt: "Parse this JSON: {{raw_json}}"
  output: "parsed"
```

**Pros:**
- No code changes
- Clear pattern in examples
- Maintains current behavior

**Cons:**
- Doesn't fix the underlying UX issue
- Still verbose recipes

**Impact:** Low - acknowledges limitation, doesn't fix it.

---

## Recommended Solution

**Combination of Option 1 + Option 2:**

1. **Unwrap spawn() results** - Store `result["output"]` instead of full dict
2. **Auto-detect and parse JSON** - If output is valid JSON, parse it

**Why this combination:**
- **Option 1** makes accessing simple text easier
- **Option 2** makes JSON "just work" with dot notation
- Together they provide the best UX with minimal complexity

**Implementation:**

```python
# executor.py, line 261-265 (updated)
result = await self.execute_step_with_retry(step, context)

# Unwrap spawn() result and auto-parse JSON
if isinstance(result, dict) and "output" in result:
    output = result["output"]
    
    # Try to parse as JSON
    if isinstance(output, str):
        try:
            import json
            parsed = json.loads(output.strip())
            output = parsed
        except (json.JSONDecodeError, ValueError):
            pass  # Keep as string
    
    result = output

# Store output if specified
if step.output:
    context[step.output] = result
```

**Fallback behavior:**
- If JSON parsing fails, keep as string (no error)
- If result isn't wrapped, use as-is (backwards compatible)
- If output is already parsed (dict/list), use it (no double-parsing)

---

## Testing Requirements

### Test Case 1: JSON Response with Dot Notation
```yaml
- id: "get-json"
  agent: "test-agent"
  prompt: 'Return: {"foo": {"bar": "baz"}}'
  output: "data"

- id: "use-nested"
  prompt: "Value is: {{data.foo.bar}}"  # Should resolve to "baz"
```

### Test Case 2: Plain Text Response
```yaml
- id: "get-text"
  agent: "test-agent"
  prompt: "Return: hello world"
  output: "text"

- id: "use-text"
  prompt: "Value is: {{text}}"  # Should resolve to "hello world"
```

### Test Case 3: JSON Array for foreach
```yaml
- id: "get-list"
  agent: "test-agent"
  prompt: 'Return: ["a", "b", "c"]'
  output: "items"

- id: "iterate"
  foreach: "{{items}}"  # Should iterate over ["a", "b", "c"]
  prompt: "Process {{item}}"
```

### Test Case 4: Complex JSON Structure
```yaml
- id: "get-complex"
  agent: "test-agent"
  prompt: 'Return: {"files": ["/p1.py", "/p2.py"], "meta": {"count": 2}}'
  output: "result"

- id: "use-nested-array"
  foreach: "{{result.files}}"  # Should iterate over files
  prompt: "Analyze {{item}}"

- id: "use-nested-value"
  prompt: "Count: {{result.meta.count}}"  # Should resolve to 2
```

---

## Migration Guide

### For Recipe Authors

**Before (current):**
```yaml
steps:
  - id: "step1"
    output: "result"
  - id: "step2"
    prompt: "Use: {{result.output}}"  # Had to access .output
```

**After (with fix):**
```yaml
steps:
  - id: "step1"
    output: "result"
  - id: "step2"
    prompt: "Use: {{result}}"  # Direct access
```

**Breaking changes:**
- If recipes explicitly access `.output`, they'll break
- Migration: Remove `.output` from variable references
- Detection: Search for `{{.*\.output}}` in recipes

---

## Open Questions

1. **Should we store session_id separately?**
   - Current: `context["result"] = {"output": "...", "session_id": "..."}`
   - Proposed: `context["result"] = "..."` (session_id lost)
   - Alternative: `context["result_session_id"] = "..."`

2. **What about markdown code blocks?**
   - Agent returns: "Here's the JSON:\n```json\n{...}\n```"
   - Should we extract JSON from markdown?
   - Or require clean JSON responses?

3. **Should parsing be opt-in or opt-out?**
   - Opt-in: `output_json: "result"` (explicit)
   - Opt-out: `output_raw: "result"` (keep as string)
   - Auto (recommended): Always try to parse

4. **How to handle ambiguous cases?**
   - Agent returns: `"true"` (string) vs `true` (boolean)
   - Current: Stays as `"true"` (string)
   - After parse: Becomes `true` (boolean)
   - Is this desired?

---

## Conclusion

**Root cause:** Agent responses are wrapped in `{"output": text, "session_id": ...}`, and even when agents return JSON, it remains an unparsed string. Dot notation can't traverse JSON strings.

**Impact:** Users must add verbose parsing steps to access nested JSON properties, making recipes complex and slow.

**Recommended fix:** Unwrap spawn() results and auto-parse JSON. This provides the best UX with minimal code changes.

**Next steps:**
1. Implement Option 1 + 2 combination
2. Add test cases for JSON parsing
3. Update documentation and examples
4. Create migration guide for existing recipes

---

## Related Files

- **Implementation:** `amplifier-bundle-recipes/modules/tool-recipes/amplifier_module_tool_recipes/executor.py`
  - Line 261-265: Result storage
  - Line 880-920: Variable substitution
  - Line 851-878: foreach variable resolution
  
- **Documentation:** `amplifier-bundle-recipes/docs/RECIPE_SCHEMA.md`
  - Variable Substitution section needs update
  
- **Examples affected:** All recipes that return JSON (most complex workflows)
