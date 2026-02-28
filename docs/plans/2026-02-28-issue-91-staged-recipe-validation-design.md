# Staged Recipe Validation Fixes Design

## Goal

Fix three bugs in the recipe engine so that staged recipes receive proper semantic validation, the `retry` field is type-safe, and dot-path variable references are validated exhaustively.

## Background

Issue [#91](https://github.com/microsoft-amplifier/amplifier-support/issues/91) identified that staged recipes bypass all semantic validation. The root cause: three validation functions in `validator.py` iterate `recipe.steps`, which is always an empty list for staged recipes — steps live in `recipe.stages[*].steps` instead. This means undefined variable references, unavailable agents, broken `depends_on` chains, and circular dependencies all pass silently for any staged recipe.

Two additional bugs were identified in the same investigation:

- The `retry` field on steps accepts any YAML type (strings, integers) without complaint, then crashes at execution time when the executor tries to treat the value as a dict.
- Dot-path variable references like `{{requirements.character_design}}` only validate the top-level prefix (`requirements`), missing invalid nested keys even when the full structure is known at validation time.

## Approach

A single PR with all three fixes. The changes are small and localized to three files in `modules/tool-recipes/amplifier_module_tool_recipes/`. No new methods or abstractions are needed — the existing `Recipe.get_all_steps()` method already handles both flat and staged modes correctly.

## Architecture

All changes are within the recipe engine module:

```
modules/tool-recipes/amplifier_module_tool_recipes/
├── validator.py   ← Fix 1 (staged recipe iteration) + Fix 3 (deeper dot-path validation)
├── models.py      ← Fix 2 (retry type guard at parse time)
└── executor.py    ← Fix 2 (retry defense-in-depth guard at execution time)
```

No cross-module changes. No new dependencies. No schema changes.

## Components

### Fix 1: Validator Must Use `get_all_steps()` for Staged Recipes

Three functions in `validator.py` iterate `recipe.steps`, which is always an empty list for staged recipes. The `Recipe` model already provides `get_all_steps()` which returns steps from both flat and staged modes.

**Changes:**

- **`check_variable_references()`** (~line 69): Replace `recipe.steps` with `recipe.get_all_steps()`.
- **`check_agent_availability()`** (~line 222): Same replacement.
- **`check_step_dependencies()`** (~line 240): Replace `recipe.steps` with `all_steps = recipe.get_all_steps()`, and replace `recipe.steps.index(dep_step)` with `all_steps.index(dep_step)`. Ordering is enforced across the flattened list since `get_all_steps()` returns steps in stage order.

No new methods needed.

### Fix 2: `retry` Field Type Safety

Two changes, mirroring how `recursion` and `provider_preferences` are already handled in the codebase:

**Change 1 — Parse-time validation in `models.py`:**

In `_parse_step()`, add a type check: if `retry` is present and not a `dict`, raise `ValueError` with a clear message (e.g., `"retry must be a mapping, got str"`). This catches the issue at parse time, consistent with how `recursion` and `provider_preferences` already validate.

**Change 2 — Defense-in-depth in `executor.py`:**

In `execute_step_with_retry()` (~line 1160), change `step.retry or {}` to `step.retry if isinstance(step.retry, dict) else {}`. Even if parse-time validation catches bad input, the executor should not crash if a malformed value somehow reaches it.

No changes to the retry schema itself. No changes to how `max_attempts`, `backoff`, `initial_delay`, or `max_delay` are processed.

### Fix 3: Deeper Dot-Path Validation in `check_variable_references()`

Currently, dot-path references like `{{requirements.character_design}}` only check the top-level prefix:

```python
if "." in var:
    prefix = var.split(".")[0]
    if prefix not in available:
        errors.append(...)
```

The fix: when the recipe has a `context` dict with known values, traverse into it to verify the full dot-path resolves.

**Rule:** If the top-level prefix resolves to a key in the recipe's declared `context` dict and that value is itself a dict, traverse and validate all segments. Otherwise, fall back to the existing prefix-only check.

**Cases that CAN be validated** (context values known in recipe YAML):

| Reference | Context | Result |
|---|---|---|
| `{{requirements.character_design}}` | `requirements.character_design` exists | Valid |
| `{{requirements.nonexistent}}` | `requirements` exists but has no `nonexistent` key | Error |
| `{{requirements.character_design.detail_level}}` | 3+ levels deep, full traversal | Error if any segment missing |
| `{{requirements.character_design.detail_level.foo}}` | Traverses into a leaf (string/int) before `foo` | Error: "detail_level is not a mapping" |
| `{{totally_unknown.anything}}` | Top-level key doesn't exist in context | Error (existing behavior) |

**Cases that CANNOT be validated** (skip deeper check, prefix-only):

| Reference | Reason |
|---|---|
| `{{step_1.output}}` | Step output, populated at runtime |
| `{{_approval_message}}` | Injected by engine at runtime |
| Any variable whose top-level prefix is a step ID or engine-injected name | Runtime-only value |

## Data Flow

Validation runs before execution via `validate_recipe()` in `validator.py`. The flow is unchanged — only what gets iterated and how deeply references are checked differs:

1. Recipe YAML is parsed by `models.py` → `Recipe` object (Fix 2 type guard runs here)
2. `validate_recipe()` is called with the `Recipe` object
3. `check_variable_references()` iterates **all steps** (Fix 1) and validates dot-paths **deeply** (Fix 3)
4. `check_agent_availability()` iterates **all steps** (Fix 1)
5. `check_step_dependencies()` iterates **all steps** (Fix 1)
6. If validation passes, `executor.py` runs the recipe (Fix 2 defense-in-depth guard is here)

## Error Handling

All three fixes produce clear, actionable error messages:

- **Fix 1:** Same error messages as today, just now they actually fire for staged recipes.
- **Fix 2:** `ValueError` at parse time with message like `"retry must be a mapping, got str"`. At execution time, silent fallback to `{}` (defense-in-depth only).
- **Fix 3:** New error messages for invalid dot-paths:
  - Missing nested key: `"Variable 'requirements.nonexistent': key 'nonexistent' not found in 'requirements'"`
  - Traversal into non-mapping: `"Variable 'requirements.detail_level.foo': 'detail_level' is not a mapping"`

## Testing Strategy

### Fix 1 Tests — Validator + Staged Recipes

- A staged recipe with an undefined variable reference → validator catches it (currently passes silently)
- A staged recipe with a misspelled/unavailable agent name → validator catches it
- A staged recipe with a broken `depends_on` referencing a non-existent step ID → validator catches it
- A staged recipe with circular `depends_on` → validator catches it
- A flat recipe still validates correctly (no regression)

### Fix 2 Tests — Retry Type Safety

- `Step` with `retry: "3"` (string) → `_parse_step()` raises `ValueError`
- `Step` with `retry: 3` (int) → `_parse_step()` raises `ValueError`
- `Step` with `retry: {max_attempts: 3}` (dict) → passes validation as before
- `Step` with `retry: null` / absent → passes validation as before
- Executor with malformed retry that somehow bypasses validation → `isinstance` guard produces `{}` fallback, no crash

### Fix 3 Tests — Deeper Dot-Path Validation

- `{{requirements.character_design}}` with matching nested context → valid, no error
- `{{requirements.nonexistent}}` with context that has `requirements` but not `nonexistent` key → error
- `{{requirements.character_design.detail_level}}` — 3-level traversal → valid if exists, error if missing
- `{{requirements.character_design.detail_level.foo}}` — traverses into a leaf → error ("detail_level is not a mapping")
- `{{step_1.output}}` — step output reference → prefix-only check, no deeper traversal
- `{{_approval_message}}` — engine-injected → skipped from deeper validation

Existing tests must still pass with no regressions.

## Files to Modify

| File | Fixes | Changes |
|---|---|---|
| `validator.py` | Fix 1, Fix 3 | Replace `recipe.steps` with `recipe.get_all_steps()` in 3 functions; add deeper dot-path traversal logic in `check_variable_references()` |
| `models.py` | Fix 2 | Add `retry` type guard in `_parse_step()` |
| `executor.py` | Fix 2 | Add `isinstance` defense-in-depth guard in `execute_step_with_retry()` |

All files are in `modules/tool-recipes/amplifier_module_tool_recipes/`.

## Open Questions

None — the issue comments provide prescriptive guidance for all three fixes.
