# Plan: Fix Non-JSON-Serializable Objects Crashing Recipe Checkpoints

**Branch:** `fix/recipe-checkpoint-serialization`
**Author:** robotdad
**Date:** 2026-04-25
**Status:** COE-Reviewed (Rev 2)

## Problem

Recipe execution crashes with `TypeError: Object of type Usage is not JSON serializable`
when an agent step returns a result containing non-JSON-serializable objects (e.g.,
Anthropic SDK `Usage` Pydantic models, raw provider response objects).

The crash is deterministic and blocks all recipe-based agent workflows that receive
LLM responses with rich metadata — specifically, recipes invoked via
`amplifier tool invoke recipes` where `session.spawn` returns richer result objects
than in standalone interactive sessions.

### Reproduction

1. Create a recipe with an agent step that calls an LLM provider.
2. Execute it via `amplifier tool invoke recipes operation=execute recipe_path=<recipe>`.
3. The agent step completes successfully and returns a result.
4. The recipe engine attempts to checkpoint state.
5. Crash: `TypeError: Object of type Usage is not JSON serializable`.

### Impact

- All recipe-based agent workflows that go through `tool invoke` are affected.
- The crash occurs at checkpoint time (after the agent's work is done), so no data
  is lost from the LLM call itself — but the recipe cannot proceed past the first
  agent step.
- This is the sole blocker for the Amplifier Resolve dev-machine build loop, which
  invokes build recipes inside containers via `amplifier tool invoke recipes`.

## Root Cause

Four code sites in the recipe engine use bare `json.dumps`/`json.dump` on context
values without sanitization. Non-serializable objects (Pydantic models, dataclasses,
objects with `__dict__`) pass through unchecked and crash the serializer.

### Site 1: `_trim_context_for_checkpoint` (executor.py:1548-1588)

The primary crash site. This method iterates over context values and tests each with
`json.dumps`. When serialization fails, the except block **intentionally passes the
raw non-serializable object through**:

```python
except (TypeError, ValueError):
    # Value is not JSON-serialisable — keep as-is so save_state can
    # surface the error naturally rather than silently dropping data.
    trimmed[key] = value   # <-- passes Usage object through
```

This then reaches `save_state` → `json.dump(state, f)` (session.py:158), which
crashes with `TypeError`.

### Site 2: `substitute_variables` (executor.py:2926, 2941)

Two `json.dumps(value)` calls on dict/list context values during template
substitution. If a context variable contains non-serializable objects and is
referenced in a recipe template via `{{varname}}`, these lines raise uncaught
`TypeError`.

### Site 3: `_save_foreach_checkpoint` (executor.py:2153-2180)

When a `foreach` step has `collect: true`, collected results are stored in
`progress["collected_results"]`. The size-check at line 2162 uses `json.dumps`
in a try/except that passes on failure (comment: "save_state will surface the
error"). The unsanitized `progress` dict is then written to state at line 2177
and reaches `save_state` → crash. Same bug class as Site 1.

```python
except (TypeError, ValueError):
    pass  # Non-serializable results — save_state will surface the error
state["foreach_progress"] = progress   # <-- unsanitized progress with collected_results
```

### How non-serializable objects enter context

`_process_step_result` (executor.py:1590-1631) unwraps the spawn result and stores
it in `context[step.output]`. When the spawn capability returns rich objects
(containing `anthropic.types.Usage`, Pydantic models, etc.), they pass through
`_process_step_result` unmodified because the method only parses string results.

## Fix

### Approach: Use `sanitize_for_json` from amplifier-foundation

`amplifier_foundation.serialization.sanitize_for_json` already exists and handles
exactly this case. It recursively converts arbitrary Python objects to
JSON-serializable equivalents:

- Primitives (`None`, `bool`, `int`, `float`, `str`) — pass through
- `dict`, `list`, `tuple` — recurse into children
- Pydantic v2 models — call `model_dump()`
- Objects with `__dict__` — convert to dict and recurse
- Last resort — attempt `json.dumps`; return `None` on total failure

The function is already exported from `amplifier_foundation` and the recipes module
already imports from `amplifier_foundation` (executor.py lines 21-22).

### Changes

#### 1. Add import (executor.py, line ~21)

Add `sanitize_for_json` to the existing `amplifier_foundation` import.

#### 2. Fix `_trim_context_for_checkpoint` (executor.py:1584-1587)

Replace the passthrough with sanitization:

```python
except (TypeError, ValueError):
    # Value is not JSON-serialisable — sanitize to a JSON-safe
    # representation rather than passing the raw object through
    # to crash save_state.
    trimmed[key] = sanitize_for_json(value)
```

**Rationale:** The original comment says "keep as-is so save_state can surface
the error naturally." But "surfacing naturally" means crashing the entire recipe.
Sanitizing preserves the data in a usable form (e.g., `Usage(input_tokens=100,
output_tokens=50)` becomes `{"input_tokens": 100, "output_tokens": 50}`) and
allows the recipe to continue.

#### 3. Fix `substitute_variables` (executor.py:2926, 2941)

Change the two `json.dumps(value)` calls to use `default=` parameter:

```python
# Line 2926 and 2941: change
return json.dumps(value)
# to
return json.dumps(value, default=_sanitize_for_json_default)
```

Where `_sanitize_for_json_default` is a thin adapter:

```python
def _sanitize_for_json_default(obj: Any) -> Any:
    """json.dumps default= hook: convert non-serializable objects."""
    result = sanitize_for_json(obj)
    if result is None:
        return f"[non-serializable: {type(obj).__name__}]"
    return result
```

**Rationale (COE feedback):** Using `default=` instead of wrapping the entire
value preserves existing behavior for already-serializable data. In particular,
`sanitize_for_json` drops `None` values from dicts/lists, which would silently
change `{"a": 1, "b": null}` to `{"a": 1}` for currently-working recipes.
The `default=` hook is only invoked for objects the default encoder rejects.

#### 4. Fix `_save_foreach_checkpoint` (executor.py:2175-2177)

Use `json.dumps(default=)` round-trip to sanitize `progress` before writing to
state, preserving `None` values in collected results:

```python
# Line 2177: change
state["foreach_progress"] = progress
# to
state["foreach_progress"] = json.loads(
    json.dumps(progress, default=_sanitize_for_json_default)
)
```

**Rationale (COE feedback, Rev 2):** When a `foreach` step has `collect: true`,
collected results may contain non-serializable objects from agent spawn results.
Using `default=` instead of wrapping with `sanitize_for_json()` preserves
`None` entries in `collected_results` — critical because the resume path at
executor.py:1979 assumes `len(collected_results) == completed_iterations` for
index alignment. A `None` result from a side-effect-only step would be dropped
by `sanitize_for_json()` but preserved by the `default=` round-trip.

#### 5. No changes to `save_state` (session.py:158)

`save_state` is the terminal serialization point. With the upstream fixes in
`_trim_context_for_checkpoint` and `substitute_variables`, non-serializable
objects will never reach `save_state`. Adding sanitization there would be
defense-in-depth but masks bugs — the engine should ensure clean data before
it reaches persistence.

### Files Changed

| File | Change |
|------|--------|
| `modules/tool-recipes/amplifier_module_tool_recipes/executor.py` | Add `sanitize_for_json` import; add `_sanitize_for_json_default` helper; fix except block in `_trim_context_for_checkpoint`; use `default=` in `substitute_variables` (2 sites); sanitize foreach progress (1 site) |

### Files NOT Changed (and why)

| File | Reason |
|------|--------|
| `session.py` | Terminal serialization point — should receive clean data from executor |
| `pyproject.toml` | `amplifier_foundation` is already available at runtime transitively via `amplifier-core`; adding an explicit dependency is a separate concern |

## Testing

### Manual verification

1. Create a minimal recipe with one agent step that triggers an LLM call.
2. Run via `amplifier tool invoke recipes operation=execute recipe_path=<recipe>`.
3. Verify: recipe completes without `TypeError`, checkpoint `state.json` contains
   serialized (dict) form of the Usage data rather than a crash.

### Unit test coverage

**Test 1: `_trim_context_for_checkpoint` with non-serializable object**

1. Creates a mock context containing a non-serializable object (e.g., a Pydantic
   model with `input_tokens` and `output_tokens` fields).
2. Calls `_trim_context_for_checkpoint` on that context.
3. Asserts the result is JSON-serializable (`json.dumps` succeeds).
4. Asserts the sanitized value preserves the data (dict with correct keys/values).

**Test 2: `substitute_variables` with non-serializable dict value**

1. Creates a context with a key mapped to a dict containing a Pydantic model.
2. Calls `substitute_variables` with a template referencing that key.
3. Asserts the result is a valid JSON string containing the sanitized data.

**Test 3: `substitute_variables` preserves None values (regression, COE req)**

1. Creates a context with `{"data": {"a": 1, "b": None}}`.
2. Calls `substitute_variables` with `"{{data}}"`.
3. Asserts the result is `{"a": 1, "b": null}` — None is NOT dropped.

**Test 4: `_save_foreach_checkpoint` with non-serializable collected results (COE req)**

1. Creates a mock step with `collect: true`.
2. Calls `_save_foreach_checkpoint` with results containing a Pydantic model.
3. Asserts `save_state` is called with JSON-serializable data.
4. Asserts the sanitized data preserves the model's field values.

## Risks

| Risk | Mitigation |
|------|------------|
| `sanitize_for_json` changes data shape (e.g., Pydantic model → dict) | This is intentional — checkpoint data should be portable JSON. The agent's work is already complete by checkpoint time. |
| `sanitize_for_json` returns `None` for truly un-convertible objects | Acceptable — `None` serializes cleanly and is better than crashing. The original data was going to crash anyway. |
| `sanitize_for_json` drops `None` values from dicts/lists | **Mitigated by using `default=` in `substitute_variables`** (COE finding). The `default=` hook is only invoked for objects the default encoder rejects, so `{"a": 1, "b": null}` round-trips correctly. The `_trim_context_for_checkpoint` call is inside the except branch (value already failed `json.dumps`) so None-dropping there is acceptable. |
| `amplifier_foundation` not available at runtime | Already available transitively. If this changes, a runtime ImportError is a clearer signal than a checkpoint crash. Follow-up: file issue to add explicit dependency in `pyproject.toml`. |
| Non-serializable values remain raw in live `context` for downstream steps | Accepted. Only persistence and template paths need clean data. In-memory step→step handoff preserves rich objects intentionally. Document as known limitation. |

## Out of Scope

- **Why `session.spawn` returns non-serializable objects in `tool invoke` mode** — that's a separate investigation in the app layer. This fix makes the recipe engine resilient regardless of what spawn returns.
- **Adding `amplifier-foundation` as explicit `pyproject.toml` dependency** — valid concern but separate PR scope.
- **Fixing `_process_step_result` to sanitize before storing in context** — an alternative approach, but sanitizing at checkpoint time is more defensive (catches ALL sources of non-serializable values, not just spawn results).