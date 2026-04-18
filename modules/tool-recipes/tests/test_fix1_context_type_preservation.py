"""Tests for Fix 1: Sub-recipe context type preservation (Bug B).

Root cause: _substitute_variables_recursive() serialised dict/list values to
JSON strings when the template string was a single whole-variable reference
(e.g. "{{current_task}}"). Sub-recipes receiving that context entry would see
a str instead of a dict, and dot-access ({{current_task.task_id}}) failed
with "Cannot access 'task_id' on str, not dict".

These tests verify:
  - RED: the bug is present before the fix (if we revert to old behaviour)
  - GREEN: the fix returns the native Python object for whole-variable refs
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_module_tool_recipes.executor import RecipeExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_coordinator():
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


@pytest.fixture
def mock_session_manager():
    manager = MagicMock()
    manager.create_session.return_value = "test-session-id"
    manager.load_state.return_value = {
        "current_step_index": 0,
        "context": {},
        "completed_steps": [],
        "started": "2025-01-01T00:00:00",
    }
    manager.is_cancellation_requested.return_value = False
    manager.is_immediate_cancellation.return_value = False
    return manager


@pytest.fixture
def executor(mock_coordinator, mock_session_manager):
    return RecipeExecutor(mock_coordinator, mock_session_manager)


# ---------------------------------------------------------------------------
# Unit tests: _substitute_variables_recursive type preservation
# ---------------------------------------------------------------------------


class TestSubstituteVariablesRecursiveTypePreservation:
    """Unit tests confirming that whole-variable references return native types."""

    def test_whole_dict_ref_returns_dict(self, executor):
        """Whole {{var}} reference to a dict returns the dict, not a JSON string."""
        ctx = {"task": {"id": "t1", "name": "First Task"}}
        result = executor._substitute_variables_recursive("{{task}}", ctx)
        assert isinstance(result, dict), (
            f"Expected dict but got {type(result).__name__}: {result!r}"
        )
        assert result == {"id": "t1", "name": "First Task"}

    def test_whole_list_ref_returns_list(self, executor):
        """Whole {{var}} reference to a list returns the list, not a JSON string."""
        ctx = {"items": [1, 2, 3]}
        result = executor._substitute_variables_recursive("{{items}}", ctx)
        assert isinstance(result, list), (
            f"Expected list but got {type(result).__name__}: {result!r}"
        )
        assert result == [1, 2, 3]

    def test_whole_int_ref_returns_int(self, executor):
        """Whole {{var}} reference to an int returns the int, not a string."""
        ctx = {"count": 42}
        result = executor._substitute_variables_recursive("{{count}}", ctx)
        assert isinstance(result, int), (
            f"Expected int but got {type(result).__name__}: {result!r}"
        )
        assert result == 42

    def test_whole_bool_ref_returns_bool(self, executor):
        """Whole {{var}} reference to a bool returns the bool, not a string."""
        ctx = {"flag": True}
        result = executor._substitute_variables_recursive("{{flag}}", ctx)
        assert isinstance(result, bool), (
            f"Expected bool but got {type(result).__name__}: {result!r}"
        )
        assert result is True

    def test_whole_none_ref_returns_none(self, executor):
        """Whole {{var}} reference to None returns None, not a string."""
        ctx = {"val": None}
        result = executor._substitute_variables_recursive("{{val}}", ctx)
        assert result is None

    def test_composite_string_still_returns_string(self, executor):
        """Composite string with surrounding text returns a normal string."""
        ctx = {"name": "world"}
        result = executor._substitute_variables_recursive("Hello {{name}}!", ctx)
        assert isinstance(result, str)
        assert result == "Hello world!"

    def test_whitespace_around_whole_var_still_type_preserved(self, executor):
        """Optional surrounding whitespace on a whole-variable ref is stripped."""
        ctx = {"task": {"id": "t1"}}
        result = executor._substitute_variables_recursive("  {{task}}  ", ctx)
        assert isinstance(result, dict)
        assert result["id"] == "t1"

    def test_partial_string_interpolation_still_stringified(self, executor):
        """Non-whole-variable strings (prefix or suffix present) → string."""
        ctx = {"id": "abc"}
        result = executor._substitute_variables_recursive("prefix-{{id}}", ctx)
        assert isinstance(result, str)
        assert result == "prefix-abc"

    # --- dotted-path resolution ---

    def test_dotted_path_whole_ref_returns_nested_native_type(self, executor):
        """Whole {{a.b}} reference returns the native value at the nested path."""
        ctx = {"data": {"nested": {"value": 99}}}
        result = executor._substitute_variables_recursive("{{data.nested}}", ctx)
        assert isinstance(result, dict), (
            f"Expected dict but got {type(result).__name__}: {result!r}"
        )
        assert result == {"value": 99}

    def test_dotted_path_leaf_int_preserved(self, executor):
        """Whole {{a.b.c}} reference where leaf is int returns int."""
        ctx = {"data": {"count": 7}}
        result = executor._substitute_variables_recursive("{{data.count}}", ctx)
        assert isinstance(result, int)
        assert result == 7

    # --- dict/list recursion ---

    def test_dict_value_with_whole_ref_preserves_nested_type(self, executor):
        """Inside a dict value, whole-variable references preserve native type."""
        ctx = {"task": {"id": "t1"}}
        result = executor._substitute_variables_recursive(
            {"current_task": "{{task}}"}, ctx
        )
        assert isinstance(result, dict)
        assert isinstance(result["current_task"], dict), (
            f"Expected dict but got {type(result['current_task']).__name__}"
        )
        assert result["current_task"]["id"] == "t1"

    def test_list_item_with_whole_ref_preserves_nested_type(self, executor):
        """Inside a list, whole-variable references preserve native type."""
        ctx = {"task": {"id": "t1"}}
        result = executor._substitute_variables_recursive(["{{task}}", "literal"], ctx)
        assert isinstance(result, list)
        assert isinstance(result[0], dict)
        assert result[0]["id"] == "t1"
        assert result[1] == "literal"

    def test_deeply_nested_dict_type_preservation(self, executor):
        """Deeply nested dict with whole-variable refs at multiple levels."""
        ctx = {"task": {"id": "t1"}, "count": 5}
        result = executor._substitute_variables_recursive(
            {"sub": {"task_ref": "{{task}}", "num": "{{count}}"}}, ctx
        )
        assert isinstance(result["sub"]["task_ref"], dict)
        assert result["sub"]["task_ref"]["id"] == "t1"
        assert isinstance(result["sub"]["num"], int)
        assert result["sub"]["num"] == 5


# ---------------------------------------------------------------------------
# Integration tests: the specific bug scenario
# ---------------------------------------------------------------------------


class TestSubRecipeContextTypePreservation:
    """
    Integration tests for the foreach → sub-recipe dict context bug.

    Scenario: parent foreach iterates over [{"id": "a"}, {"id": "b"}].
    Each iteration passes current_item: "{{current_item}}" to the sub-recipe
    context. The sub-recipe must receive a dict and be able to access
    current_item.id via dot-access.
    """

    def test_dict_in_context_preserved_not_json_stringified(self, executor):
        """
        _substitute_variables_recursive("{{current_item}}", ctx) where
        ctx["current_item"] is a dict must return a dict, not a JSON string.

        This is the exact failure path: before the fix the returned type was
        str (because substitute_variables always returns str and called
        json.dumps on dicts).
        """
        task = {"task_id": "task-1", "label": "My Task"}
        parent_ctx = {"current_item": task}

        result = executor._substitute_variables_recursive(
            "{{current_item}}", parent_ctx
        )

        assert isinstance(result, dict), (
            "BUG REPRODUCED: _substitute_variables_recursive returned "
            f"{type(result).__name__!r} instead of dict. "
            "The sub-recipe would receive a JSON string and dot-access would fail."
        )
        assert result["task_id"] == "task-1"

    def test_dot_access_works_after_type_preserved_pass(self, executor):
        """
        Full round-trip: parent builds sub_context, then sub-recipe accesses
        {{current_item.task_id}} — this must resolve to "task-42".

        Before the fix this raised:
          ValueError: Cannot access 'task_id' on current_item — it's a str, not a dict.
        """
        task = {"task_id": "task-42", "name": "My Task"}
        parent_ctx = {"current_item": task}

        # Step 1: parent builds sub-recipe context (the previously broken path)
        sub_context = {
            "current_item": executor._substitute_variables_recursive(
                "{{current_item}}", parent_ctx
            )
        }

        # Step 2: sub-recipe does dot-access (this failed before the fix)
        result = executor.substitute_variables("{{current_item.task_id}}", sub_context)

        assert result == "task-42", f"Expected 'task-42', got {result!r}"

    def test_foreach_two_items_both_resolve_correctly(self, executor):
        """
        Simulate two foreach iterations: [{"id": "a"}, {"id": "b"}].
        Both must produce dict context entries and dot-access must resolve
        to "a" and "b" respectively.
        """
        items = [{"id": "a"}, {"id": "b"}]
        expected_ids = ["a", "b"]

        for item, expected_id in zip(items, expected_ids):
            parent_ctx = {"current_item": item}
            sub_context = {
                "current_item": executor._substitute_variables_recursive(
                    "{{current_item}}", parent_ctx
                )
            }
            assert isinstance(sub_context["current_item"], dict), (
                f"Iteration for expected_id={expected_id!r}: "
                f"current_item should be dict, got "
                f"{type(sub_context['current_item']).__name__}"
            )
            resolved_id = executor.substitute_variables(
                "{{current_item.id}}", sub_context
            )
            assert resolved_id == expected_id, (
                f"Expected id={expected_id!r}, got {resolved_id!r}"
            )

    def test_list_items_in_context_also_preserved(self, executor):
        """
        A list value forwarded via "{{my_list}}" must arrive as a list,
        not a JSON-serialised string.
        """
        parent_ctx = {"tags": ["alpha", "beta", "gamma"]}
        sub_context = {
            "tags": executor._substitute_variables_recursive("{{tags}}", parent_ctx)
        }
        assert isinstance(sub_context["tags"], list), (
            f"Expected list, got {type(sub_context['tags']).__name__}: "
            f"{sub_context['tags']!r}"
        )
        assert len(sub_context["tags"]) == 3
        assert sub_context["tags"][0] == "alpha"

    def test_original_error_message_no_longer_raised(self, executor):
        """
        The specific error "Cannot access 'task_id' on str, not dict" must NOT
        be raised when accessing dot-path after a type-preserved context pass.
        """
        task = {"task_id": "t99"}
        parent_ctx = {"current_item": task}
        sub_context = {
            "current_item": executor._substitute_variables_recursive(
                "{{current_item}}", parent_ctx
            )
        }
        # This must not raise ValueError
        try:
            result = executor.substitute_variables(
                "{{current_item.task_id}}", sub_context
            )
            assert result == "t99"
        except ValueError as exc:
            pytest.fail(
                f"Got ValueError (the old bug): {exc}\n"
                "Fix 1 did not prevent the 'Cannot access on str' error."
            )
