"""Tests for Fix 4: Non-JSON-serializable objects crashing recipe checkpoints.

Root cause: _trim_context_for_checkpoint() caught TypeError from non-serializable
values (e.g. Pydantic Usage models from LLM providers) but passed the raw object
through to save_state(), which crashed on json.dump().  substitute_variables()
also had bare json.dumps() calls that would crash on non-serializable dict values.
_save_foreach_checkpoint() had the same passthrough pattern for collected results.

These tests verify:
  - _trim_context_for_checkpoint sanitizes non-serializable values
  - substitute_variables handles non-serializable dict values via default= hook
  - substitute_variables preserves None values in dicts (regression guard)
  - _save_foreach_checkpoint sanitizes collected results with non-serializable items

See: docs/plans/fix-checkpoint-serialization.md
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Step


# ---------------------------------------------------------------------------
# A non-JSON-serializable object mimicking anthropic.types.Usage
# ---------------------------------------------------------------------------


class FakeUsage:
    """Mimics a Pydantic model (like anthropic.types.Usage) that is not
    JSON-serializable by the default encoder."""

    def __init__(self, input_tokens: int = 100, output_tokens: int = 50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def __repr__(self) -> str:
        return f"FakeUsage(input_tokens={self.input_tokens}, output_tokens={self.output_tokens})"


class FakePydanticModel:
    """Mimics a Pydantic v2 model with model_dump()."""

    def __init__(self, data: dict):
        self._data = data

    def model_dump(self) -> dict:
        return self._data

    def __repr__(self) -> str:
        return f"FakePydanticModel({self._data})"


# ---------------------------------------------------------------------------
# Fixtures (follows test_fix1 pattern — local overrides of conftest)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_coordinator():
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    coordinator.get_capability = MagicMock(return_value=AsyncMock())
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


# ===========================================================================
# Test 1: _trim_context_for_checkpoint with non-serializable objects
# ===========================================================================


class TestTrimContextSanitizesNonSerializable:
    """_trim_context_for_checkpoint must produce JSON-serializable output
    even when context contains non-JSON-serializable objects."""

    def test_usage_object_is_sanitized(self, executor):
        """A Usage-like object with __dict__ is converted to a dict."""
        context = {
            "normal_key": "normal_value",
            "usage": FakeUsage(input_tokens=150, output_tokens=75),
        }
        result = executor._trim_context_for_checkpoint(context)

        # Must be JSON-serializable (no TypeError)
        serialized = json.dumps(result)
        assert serialized is not None

        # Normal values pass through unchanged
        assert result["normal_key"] == "normal_value"

        # Usage object is converted to a dict preserving field values
        usage_result = result["usage"]
        assert isinstance(usage_result, dict)
        assert usage_result["input_tokens"] == 150
        assert usage_result["output_tokens"] == 75

    def test_pydantic_model_is_sanitized(self, executor):
        """A Pydantic v2 model with model_dump() is converted to a dict.

        Note: sanitize_for_json uses __dict__ (which includes _data) before
        trying model_dump(). The key assertion is that the result is
        JSON-serializable and the data is preserved somewhere in the output.
        """
        context = {
            "model": FakePydanticModel({"status": "complete", "score": 0.95}),
        }
        result = executor._trim_context_for_checkpoint(context)

        # Must be JSON-serializable (no TypeError)
        serialized = json.dumps(result)
        assert serialized is not None

        # Result is a dict (converted from the object)
        model_result = result["model"]
        assert isinstance(model_result, dict)

        # The original data is preserved (may be nested under _data via __dict__)
        if "_data" in model_result:
            assert model_result["_data"]["status"] == "complete"
            assert model_result["_data"]["score"] == 0.95
        else:
            assert model_result["status"] == "complete"
            assert model_result["score"] == 0.95

    def test_nested_non_serializable_in_dict(self, executor):
        """Non-serializable objects nested inside dicts are handled."""
        context = {
            "response": {
                "text": "hello",
                "usage": FakeUsage(input_tokens=10, output_tokens=5),
            },
        }
        result = executor._trim_context_for_checkpoint(context)

        # The whole thing must be serializable
        serialized = json.dumps(result)
        assert serialized is not None

    def test_already_serializable_values_unchanged(self, executor):
        """Values that are already JSON-serializable pass through as-is."""
        context = {
            "string": "hello",
            "number": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
            "list": [1, 2, 3],
            "dict": {"a": 1},
        }
        result = executor._trim_context_for_checkpoint(context)

        assert result["string"] == "hello"
        assert result["number"] == 42
        assert result["float"] == 3.14
        assert result["bool"] is True
        assert result["none"] is None
        assert result["list"] == [1, 2, 3]
        assert result["dict"] == {"a": 1}


# ===========================================================================
# Test 2: substitute_variables with non-serializable dict values
# ===========================================================================


class TestSubstituteVariablesSanitizesNonSerializable:
    """substitute_variables must handle non-serializable objects in dict/list
    context values referenced by templates."""

    def test_dict_with_non_serializable_value(self, executor):
        """A dict containing a non-serializable object renders as valid JSON."""
        context = {
            "data": {"text": "hello", "usage": FakeUsage(input_tokens=10, output_tokens=5)},
        }
        result = executor.substitute_variables("result: {{data}}", context)

        # Must not raise TypeError
        assert result.startswith("result: ")
        json_part = result[len("result: "):]
        parsed = json.loads(json_part)
        assert parsed["text"] == "hello"
        # Usage object was sanitized — check it's present in some form
        assert "usage" in parsed


# ===========================================================================
# Test 3: substitute_variables preserves None (regression guard)
# ===========================================================================


class TestSubstituteVariablesPreservesNone:
    """substitute_variables must NOT drop None values from dicts/lists.
    This is a regression test per COE review — sanitize_for_json drops None
    entries, but the default= hook approach should preserve them."""

    def test_none_in_dict_preserved(self, executor):
        """{"a": 1, "b": None} must render as {"a": 1, "b": null}."""
        context = {"data": {"a": 1, "b": None}}
        result = executor.substitute_variables("{{data}}", context)

        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": None}

    def test_none_in_list_preserved(self, executor):
        """[1, None, 3] must render as [1, null, 3]."""
        context = {"items": [1, None, 3]}
        result = executor.substitute_variables("{{items}}", context)

        parsed = json.loads(result)
        assert parsed == [1, None, 3]


# ===========================================================================
# Test 4: _save_foreach_checkpoint with non-serializable collected results
# ===========================================================================


class TestForeachCheckpointSanitizesCollectedResults:
    """_save_foreach_checkpoint must sanitize collected results containing
    non-serializable objects so save_state doesn't crash."""

    def test_collected_results_with_usage_object(self, executor, mock_session_manager):
        """Collected results containing a Usage-like object are sanitized."""
        step = Step(
            id="foreach-step",
            agent="test-agent",
            prompt="Do work on {{item}}",
            output="result",
            foreach="items",
            collect="collected",
        )
        results = [
            "plain string result",
            {"text": "response", "usage": FakeUsage(input_tokens=50, output_tokens=25)},
        ]

        executor._save_foreach_checkpoint(
            session_id="test-session",
            project_path=Path("/tmp/test"),
            step=step,
            completed_iterations=2,
            results=results,
            total_items=5,
            context={"items": ["a", "b", "c", "d", "e"]},
        )

        # save_state must have been called (no TypeError crash)
        mock_session_manager.save_state.assert_called_once()

        # Inspect the state that was passed to save_state
        call_args = mock_session_manager.save_state.call_args
        saved_state = call_args[0][2]  # positional arg: (session_id, project_path, state)

        # foreach_progress must be JSON-serializable
        progress = saved_state["foreach_progress"]
        serialized = json.dumps(progress)
        assert serialized is not None

        # collected_results length must match completed_iterations
        assert len(progress["collected_results"]) == 2