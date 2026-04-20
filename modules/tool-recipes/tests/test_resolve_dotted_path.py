"""Tests for RecipeExecutor._resolve_dotted_path() helper.

This method does not yet exist; these tests must FAIL (RED) before the
helper is extracted from the two duplicate dotted-path traversal blocks
inside _substitute_variables_recursive and substitute_variables.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

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
# Happy paths
# ---------------------------------------------------------------------------


class TestResolveDottedPathHappy:
    """Happy-path tests: valid dot paths resolve to native Python values."""

    def test_single_dot_resolves(self, executor):
        """Single-level dot path a.b resolves to the leaf value."""
        context = {"a": {"b": 1}}
        result = executor._resolve_dotted_path("a.b", context)
        assert result == 1

    def test_multi_dot_resolves(self, executor):
        """Multi-level dot path a.b.c resolves to the deeply nested leaf."""
        context = {"a": {"b": {"c": 2}}}
        result = executor._resolve_dotted_path("a.b.c", context)
        assert result == 2

    def test_returns_dict_at_intermediate(self, executor):
        """Resolving a.b where b is a dict returns the dict, not a string."""
        context = {"a": {"b": {"c": 2}}}
        result = executor._resolve_dotted_path("a.b", context)
        assert result == {"c": 2}
        assert isinstance(result, dict)

    def test_returns_list(self, executor):
        """Resolving a.b where b is a list returns the list, not a string."""
        context = {"a": {"b": [1, 2]}}
        result = executor._resolve_dotted_path("a.b", context)
        assert result == [1, 2]
        assert isinstance(result, list)

    def test_returns_none_value(self, executor):
        """Resolving a.b where b is None returns None (not the string 'None')."""
        context = {"a": {"b": None}}
        result = executor._resolve_dotted_path("a.b", context)
        assert result is None

    def test_returns_bool_value(self, executor):
        """Resolving a.b where b is True returns the bool True, not the string 'true'."""
        context = {"a": {"b": True}}
        result = executor._resolve_dotted_path("a.b", context)
        assert result is True
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestResolveDottedPathErrors:
    """Error-path tests: missing keys or non-dict intermediates raise ValueError."""

    def test_missing_key_raises_valueerror(self, executor):
        """KeyError at a leaf position must raise ValueError."""
        context = {"a": {}}
        with pytest.raises(ValueError, match="Key 'b' not found"):
            executor._resolve_dotted_path("a.b", context)

    def test_non_dict_parent_raises_valueerror(self, executor):
        """Attempting to traverse into a non-dict must raise ValueError."""
        context = {"a": {"b": "str"}}
        with pytest.raises(ValueError, match="it's a str, not a dict"):
            executor._resolve_dotted_path("a.b.c", context)

    def test_missing_root_key_raises_valueerror(self, executor):
        """Missing key at the root level must raise ValueError."""
        context = {}
        with pytest.raises(ValueError, match="Key 'x' not found"):
            executor._resolve_dotted_path("x.y", context)

    def test_error_includes_available_keys(self, executor):
        """Error message for missing key must list available sibling keys."""
        context = {"a": {"x": 1, "y": 2}}
        with pytest.raises(ValueError) as exc_info:
            executor._resolve_dotted_path("a.z", context)
        msg = str(exc_info.value)
        # Both x and y should appear (sorted) in the error message
        assert "x" in msg
        assert "y" in msg

    def test_error_includes_hint_text(self, executor):
        """Error for non-dict parent must contain the Hint: suffix."""
        context = {"a": "str"}
        with pytest.raises(ValueError, match="Hint:"):
            executor._resolve_dotted_path("a.b", context)
