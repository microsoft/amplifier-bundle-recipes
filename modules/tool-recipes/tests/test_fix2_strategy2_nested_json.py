"""Tests for Fix 2: parse_json Strategy 2 handles nested JSON correctly.

Root cause: Strategy 2 used a non-greedy regex:
    r"```(?:json)?\\s*([\\[{].*?[\\]}])\\s*```" (re.DOTALL)

The .*? is non-greedy and stops at the FIRST closing } or ], which truncates
nested JSON structures. For example {"tasks": [{"id": 1}]} would match only
the inner-most closing "}" not the full object.

Fix: Replace the regex-based extraction with a code-fence scanner + raw_decode,
letting the JSON parser itself handle balanced-brace counting.

These tests verify:
  - RED: deeply nested JSON in a fenced block was silently truncated before
  - GREEN: the full nested structure is returned after the fix
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
# Unit tests: _extract_json_aggressively — Strategy 2 nested JSON
# ---------------------------------------------------------------------------


class TestStrategy2NestedJsonExtraction:
    """Verify that Strategy 2 returns the full nested structure from code fences."""

    def test_nested_object_in_json_fence(self, executor):
        """Deeply nested object inside ```json ``` block is returned intact."""
        output = '```json\n{"tasks": [{"id": 1, "nested": {"a": [2, 3]}}]}\n```'
        result = executor._extract_json_aggressively(output)
        assert isinstance(result, dict), (
            f"Expected dict, got {type(result).__name__}: {result!r}"
        )
        # Full nested structure must survive — the old regex would have truncated here
        assert "tasks" in result, f"Key 'tasks' missing from result: {result!r}"
        assert isinstance(result["tasks"], list)
        assert result["tasks"][0]["id"] == 1
        assert result["tasks"][0]["nested"]["a"] == [2, 3]

    def test_deeply_nested_object_in_plain_fence(self, executor):
        """Deeply nested object inside plain ``` ``` block (no 'json' label) works."""
        output = '```\n{"tasks": [{"id": 1, "nested": {"a": [2, 3]}}]}\n```'
        result = executor._extract_json_aggressively(output)
        assert isinstance(result, dict)
        assert result["tasks"][0]["nested"]["a"] == [2, 3]

    def test_nested_object_with_multiple_levels(self, executor):
        """Multiple nesting levels are handled correctly (the regex bug's core failure)."""
        payload = {
            "level1": {
                "level2": {
                    "level3": {"value": "deep", "arr": [1, 2, 3]},
                    "sibling": "x",
                }
            }
        }
        import json

        output = f"```json\n{json.dumps(payload)}\n```"
        result = executor._extract_json_aggressively(output)
        assert isinstance(result, dict)
        assert result["level1"]["level2"]["level3"]["value"] == "deep"
        assert result["level1"]["level2"]["level3"]["arr"] == [1, 2, 3]
        assert result["level1"]["level2"]["sibling"] == "x"

    def test_array_of_objects_in_fence(self, executor):
        """Array of objects inside a code fence is fully extracted."""
        output = (
            '```json\n'
            '[{"task_id": "1", "name": "first", "meta": {"k": "v"}}, '
            '{"task_id": "2", "name": "second", "meta": {"k": "w"}}]\n'
            '```'
        )
        result = executor._extract_json_aggressively(output)
        assert isinstance(result, list), (
            f"Expected list, got {type(result).__name__}: {result!r}"
        )
        assert len(result) == 2
        assert result[0]["task_id"] == "1"
        assert result[0]["meta"]["k"] == "v"
        assert result[1]["task_id"] == "2"
        assert result[1]["meta"]["k"] == "w"

    def test_fence_with_surrounding_prose(self, executor):
        """Code fence surrounded by prose text — only the fenced JSON is returned."""
        output = (
            "Here is the structured output:\n\n"
            '```json\n{"tasks": [{"id": 1, "nested": {"a": [2, 3]}}]}\n```\n\n'
            "Process each task."
        )
        result = executor._extract_json_aggressively(output)
        assert isinstance(result, dict)
        assert result["tasks"][0]["nested"]["a"] == [2, 3]

    def test_regression_simple_flat_object_in_fence_still_works(self, executor):
        """Simple flat object in a code fence still works after the fix."""
        output = '```json\n{"key": "value", "count": 42}\n```'
        result = executor._extract_json_aggressively(output)
        assert isinstance(result, dict)
        assert result["key"] == "value"
        assert result["count"] == 42

    def test_regression_simple_array_in_fence_still_works(self, executor):
        """Simple array in a code fence still works after the fix."""
        output = '```json\n["a", "b", "c"]\n```'
        result = executor._extract_json_aggressively(output)
        assert isinstance(result, list)
        assert result == ["a", "b", "c"]

    def test_strategy2_fallback_to_strategy3_on_bad_fence_content(self, executor):
        """If fenced content isn't valid JSON, Strategy 3 takes over and finds JSON."""
        output = (
            "```json\nnot valid json\n```\n\n"
            '{"fallback": true, "found_by": "strategy3"}'
        )
        result = executor._extract_json_aggressively(output)
        # Strategy 3 should find the bare object after the fence
        assert isinstance(result, dict)
        assert result.get("fallback") is True

    def test_real_world_nested_task_list(self, executor):
        """
        Representative real-world scenario: agent returns a task list with nested
        structure inside a markdown code fence. This is the exact pattern that
        triggered the original COE bug report.
        """
        output = """I've analyzed the requirements. Here's the task breakdown:

```json
{
  "tasks": [
    {
      "task_id": "task-001",
      "title": "Implement authentication",
      "details": {
        "priority": "high",
        "estimate_hours": 8,
        "dependencies": ["task-000"]
      }
    },
    {
      "task_id": "task-002",
      "title": "Write unit tests",
      "details": {
        "priority": "medium",
        "estimate_hours": 4,
        "dependencies": ["task-001"]
      }
    }
  ],
  "metadata": {
    "total_tasks": 2,
    "generated_at": "2025-01-01"
  }
}
```

Please process each task in order."""

        result = executor._extract_json_aggressively(output)
        assert isinstance(result, dict), (
            f"Expected dict, got {type(result).__name__}: {result!r}"
        )
        # Both tasks must be present
        assert "tasks" in result
        assert len(result["tasks"]) == 2
        assert result["tasks"][0]["task_id"] == "task-001"
        assert result["tasks"][0]["details"]["priority"] == "high"
        assert result["tasks"][0]["details"]["dependencies"] == ["task-000"]
        assert result["tasks"][1]["task_id"] == "task-002"
        # Metadata must survive too
        assert result["metadata"]["total_tasks"] == 2
