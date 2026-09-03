"""Checkpoint serialization: non-JSON-serializable values must not crash a save.

Root cause: ``SessionManager.save_state`` writes state with a bare
``json.dump``.  Three executor paths used to hand it raw objects:

  * ``_trim_context_for_checkpoint`` caught ``TypeError`` from a
    non-serializable context value but then passed the *raw object through*,
    so the crash simply moved to ``save_state`` -- and because ``save_state``
    writes directly to ``state.json``, the crash left a truncated,
    unresumable checkpoint file behind.
  * ``_save_foreach_checkpoint`` stored ``collected_results`` unsanitized.
  * ``substitute_variables`` used bare ``json.dumps`` for dict/list values,
    which raises when a nested value is not serializable.

All three now route through ``_sanitize_for_json_default`` -- structured
objects keep their fields, opaque ones (``Path``/``datetime``/``set``)
round-trip as a readable ``[non-serializable: <Type>]`` placeholder string
rather than silently becoming ``null``.

Port of PR #69 (credits @robotdad), re-implemented on current main.
"""

import datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.executor import _json_safe
from amplifier_module_tool_recipes.executor import _sanitize_for_json_default
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step
from amplifier_module_tool_recipes.session import SessionManager


class FakeUsage:
    """Mimics a Pydantic-style model (e.g. ``anthropic.types.Usage``) that the
    stdlib JSON encoder cannot serialize, but which carries a ``__dict__``."""

    def __init__(self, input_tokens: int = 100, output_tokens: int = 50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@pytest.fixture
def mock_coordinator():
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    # A bare MagicMock attribute would look like a *cancelled* token to
    # _check_coordinator_cancellation (every attribute is truthy).
    coordinator.cancellation = None
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


@pytest.fixture
def real_session_manager(temp_dir: Path) -> SessionManager:
    """A real SessionManager -- the point is to exercise the actual json.dump."""
    return SessionManager(base_dir=temp_dir / "sessions", auto_cleanup_days=7)


@pytest.fixture
def executor(mock_coordinator, real_session_manager):
    return RecipeExecutor(mock_coordinator, real_session_manager)


# ===========================================================================
# The acceptance case: step output -> checkpoint save -> resume
# ===========================================================================


class TestNonSerializableStepOutputSurvivesSaveAndResume:
    @pytest.mark.asyncio
    async def test_step_output_round_trips_through_a_real_checkpoint(
        self, mock_coordinator, real_session_manager, temp_dir
    ):
        """A step whose output holds a non-serializable object checkpoints and resumes.

        Runs a two-step recipe for real (real SessionManager, real state.json),
        then resumes the finished session by id and inspects the context that
        the resumed run restored from disk.
        """
        executor = RecipeExecutor(mock_coordinator, real_session_manager)

        # Step 1 returns a set (opaque: no __dict__, no model_dump, not
        # JSON-serializable); step 2 returns a dict whose nested value is a
        # Path -- structured container, non-serializable leaf.
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = [
            {"a", "b"},
            {"where": Path("/tmp/artifact.txt"), "count": 2},
        ]

        recipe = Recipe(
            name="serialization-test",
            description="checkpoint with non-serializable outputs",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="p1", output="opaque_out"),
                Step(id="s2", agent="a", prompt="use {{opaque_out}}", output="mixed_out"),
            ],
            context={},
        )

        # Guard: these values genuinely defeat the stdlib encoder, so the test
        # would fail without the fix rather than passing vacuously.
        with pytest.raises(TypeError):
            json.dumps({"opaque_out": {"a", "b"}})

        # 1. Run: every per-step checkpoint must save without raising.
        result = await executor.execute_recipe(recipe, {}, temp_dir)
        session_id = result["session"]["id"]

        # The *live* context is never modified -- raw objects still there.
        assert result["opaque_out"] == {"a", "b"}
        assert result["mixed_out"]["where"] == Path("/tmp/artifact.txt")

        # 2. The on-disk checkpoint is valid JSON and holds sanitized values.
        state_file = (
            real_session_manager.get_session_dir(session_id, temp_dir) / "state.json"
        )
        on_disk = json.loads(state_file.read_text(encoding="utf-8"))
        assert on_disk["context"]["opaque_out"] == "[non-serializable: set]"
        assert (
            on_disk["context"]["mixed_out"]["where"]
            == "[non-serializable: PosixPath]"
        )
        assert on_disk["context"]["mixed_out"]["count"] == 2

        # 3. Resume the session -- the restored context carries the sanitized
        #    string, and resuming does not crash.
        resumed = await executor.execute_recipe(
            recipe, {}, temp_dir, session_id=session_id
        )
        assert resumed["opaque_out"] == "[non-serializable: set]"
        assert resumed["mixed_out"]["where"] == "[non-serializable: PosixPath]"
        assert resumed["mixed_out"]["count"] == 2

    @pytest.mark.asyncio
    async def test_save_state_is_reached_at_all(
        self, mock_coordinator, real_session_manager, temp_dir
    ):
        """Regression guard: before the fix this raised TypeError inside save_state."""
        executor = RecipeExecutor(mock_coordinator, real_session_manager)
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = [FakeUsage(input_tokens=7, output_tokens=3)]

        recipe = Recipe(
            name="usage-test",
            description="pydantic-like output",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="p", output="usage")],
            context={},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)
        session_id = result["session"]["id"]

        restored = real_session_manager.load_state(session_id, temp_dir)
        # An object with __dict__ keeps its fields rather than degrading to a string.
        assert restored["context"]["usage"] == {"input_tokens": 7, "output_tokens": 3}


# ===========================================================================
# _trim_context_for_checkpoint
# ===========================================================================


class TestTrimContextSanitizes:
    def test_opaque_value_becomes_a_placeholder_string(self, executor):
        trimmed = executor._trim_context_for_checkpoint(
            {"when": datetime.datetime(2026, 9, 2, 12, 0, 0)}
        )
        json.dumps(trimmed)  # must not raise
        assert trimmed["when"] == "[non-serializable: datetime]"

    def test_object_with_dict_keeps_its_fields(self, executor):
        trimmed = executor._trim_context_for_checkpoint(
            {"usage": FakeUsage(input_tokens=150, output_tokens=75)}
        )
        json.dumps(trimmed)
        assert trimmed["usage"] == {"input_tokens": 150, "output_tokens": 75}

    def test_nested_non_serializable_inside_a_dict(self, executor):
        trimmed = executor._trim_context_for_checkpoint(
            {"response": {"text": "hello", "usage": FakeUsage(10, 5)}}
        )
        json.dumps(trimmed)
        assert trimmed["response"]["text"] == "hello"
        assert trimmed["response"]["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_serializable_values_pass_through_unchanged(self, executor):
        context = {
            "string": "hello",
            "number": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
            "list": [1, 2, 3],
            "dict": {"a": 1},
        }
        assert executor._trim_context_for_checkpoint(context) == context

    def test_oversized_values_are_still_trimmed(self, executor):
        """The size-trim branch is untouched by the sanitize change."""
        trimmed = executor._trim_context_for_checkpoint({"big": "x" * 200_000})
        assert trimmed["big"].startswith("[trimmed:")


# ===========================================================================
# _save_foreach_checkpoint
# ===========================================================================


class TestForeachCheckpointSanitizes:
    def test_collected_results_with_a_non_serializable_item(
        self, mock_coordinator, real_session_manager, temp_dir
    ):
        executor = RecipeExecutor(mock_coordinator, real_session_manager)
        recipe = Recipe(
            name="foreach-test",
            description="d",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="p {{item}}",
                    foreach="{{items}}",
                    collect="collected",
                    checkpoint_iterations=True,
                )
            ],
        )
        session_id = real_session_manager.create_session(recipe, temp_dir)

        executor._save_foreach_checkpoint(
            session_id=session_id,
            project_path=temp_dir,
            step=recipe.steps[0],
            completed_iterations=2,
            results=["plain string", {"text": "response", "usage": FakeUsage(50, 25)}],
            total_items=5,
            context={"items": ["a", "b", "c", "d", "e"]},
        )

        restored = real_session_manager.load_state(session_id, temp_dir)
        progress = restored["foreach_progress"]
        assert progress["completed_iterations"] == 2
        assert len(progress["collected_results"]) == 2
        assert progress["collected_results"][0] == "plain string"
        assert progress["collected_results"][1] == {
            "text": "response",
            "usage": {"input_tokens": 50, "output_tokens": 25},
        }

    def test_none_entries_in_collected_results_are_preserved(
        self, mock_coordinator, real_session_manager, temp_dir
    ):
        """``on_error: continue`` appends None at a failed index -- the index
        alignment must survive the checkpoint (``sanitize_for_json`` alone
        would drop them, which is why the default= hook is used here)."""
        executor = RecipeExecutor(mock_coordinator, real_session_manager)
        recipe = Recipe(
            name="foreach-none",
            description="d",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="p {{item}}",
                    foreach="{{items}}",
                    collect="collected",
                    checkpoint_iterations=True,
                )
            ],
        )
        session_id = real_session_manager.create_session(recipe, temp_dir)

        executor._save_foreach_checkpoint(
            session_id=session_id,
            project_path=temp_dir,
            step=recipe.steps[0],
            completed_iterations=3,
            results=["ok", None, {"usage": FakeUsage(1, 2)}],
            total_items=3,
            context={"items": ["a", "b", "c"]},
        )

        restored = real_session_manager.load_state(session_id, temp_dir)
        collected = restored["foreach_progress"]["collected_results"]
        assert len(collected) == 3
        assert collected[1] is None


# ===========================================================================
# substitute_variables
# ===========================================================================


class TestSubstituteVariablesSanitizes:
    def test_dict_containing_a_non_serializable_value(self, executor):
        context = {"data": {"text": "hello", "usage": FakeUsage(10, 5)}}
        rendered = executor.substitute_variables("result: {{data}}", context)
        parsed = json.loads(rendered[len("result: ") :])
        assert parsed["text"] == "hello"
        assert parsed["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_dotted_path_dict_containing_a_non_serializable_value(self, executor):
        context = {"outer": {"inner": {"p": Path("/tmp/x"), "n": 1}}}
        parsed = json.loads(executor.substitute_variables("{{outer.inner}}", context))
        assert parsed == {"p": "[non-serializable: PosixPath]", "n": 1}

    def test_none_in_dict_is_preserved(self, executor):
        rendered = executor.substitute_variables("{{data}}", {"data": {"a": 1, "b": None}})
        assert json.loads(rendered) == {"a": 1, "b": None}

    def test_none_in_list_is_preserved(self, executor):
        rendered = executor.substitute_variables("{{items}}", {"items": [1, None, 3]})
        assert json.loads(rendered) == [1, None, 3]


# ===========================================================================
# The default hook itself
# ===========================================================================


class TestSanitizeForJsonDefault:
    def test_opaque_types_become_named_placeholders(self):
        assert _sanitize_for_json_default({1, 2}) == "[non-serializable: set]"
        assert (
            _sanitize_for_json_default(Path("/tmp/a"))
            == "[non-serializable: PosixPath]"
        )

    def test_never_returns_none(self):
        """A default hook returning None would write `null` and lose the value."""
        for value in ({1, 2}, Path("/tmp/a"), datetime.date(2026, 9, 2)):
            assert _sanitize_for_json_default(value) is not None


class TestJsonSafe:
    def test_keys_with_non_serializable_values_are_not_dropped(self):
        """Calling sanitize_for_json directly would delete 'p' entirely."""
        assert _json_safe({"p": Path("/tmp/a"), "n": 1}) == {
            "p": "[non-serializable: PosixPath]",
            "n": 1,
        }

    def test_none_entries_keep_their_positions(self):
        assert _json_safe([1, None, {2, 3}]) == [1, None, "[non-serializable: set]"]

    def test_reference_cycle_does_not_crash(self):
        cycle: dict = {"self": None}
        cycle["self"] = cycle
        assert _json_safe(cycle) == "[non-serializable: dict]"

    def test_non_string_dict_key_does_not_crash(self):
        assert _json_safe({(1, 2): "v"}) == "[non-serializable: dict]"
