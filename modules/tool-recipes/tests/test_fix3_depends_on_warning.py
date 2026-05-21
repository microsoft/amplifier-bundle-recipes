"""Tests for Fix 3: depends_on advisory warning (per-recipe, not per-step).

Root cause: Step.depends_on is declared, validated, and documented but the
executor runs steps strictly in declaration order — it never reorders or gates
steps based on the depends_on list. Users who rely on it for ordering would
experience silent mis-ordering at runtime.

Fix (original): Log a WARNING once per unique (recipe_name, step_id) pair per
process lifetime.

Fix (updated — this PR): Collapse to ONE warning per recipe per process
lifetime.  The single message names the recipe and the count of steps that
declare depends_on.  This preserves the alerting intent without flooding logs
on recipes with many steps (e.g. validate-bundle-repo with 30 depends_on
steps used to emit 30 warnings — now emits 1).

These tests verify:
  - A recipe with depends_on steps emits exactly ONE WARNING log entry
  - The warning message names the recipe (not individual step IDs)
  - The warning includes the count of steps that declare depends_on
  - The warning fires at recipe load (execute_recipe), not per step run
  - Deduplication: calling the helper twice for the same recipe warns only once
  - Different recipes each get their own warning
  - Steps without depends_on do not trigger the warning
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_module_tool_recipes.executor import (
    RecipeExecutor,
    _warn_depends_on_unenforced,
    _warned_depends_on_recipes,
)
from amplifier_module_tool_recipes.models import Recipe, Step


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_warned_set():
    """Reset the module-level deduplication set before every test."""
    _warned_depends_on_recipes.clear()
    yield
    _warned_depends_on_recipes.clear()


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
# Unit tests: _warn_depends_on_unenforced() helper
# ---------------------------------------------------------------------------


class TestWarnDependsOnUnenforced:
    """Direct unit tests for the module-level warning helper."""

    def test_warning_emitted_for_step_with_depends_on(self, caplog):
        """A recipe with a depends_on step triggers exactly one WARNING log entry."""
        recipe = Recipe(
            name="test-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-a", agent="a", prompt="p", depends_on=["step-b"]),
                Step(id="step-b", agent="a", prompt="p"),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe)

        assert len(caplog.records) == 1, (
            f"Expected 1 warning record, got {len(caplog.records)}: "
            f"{[r.message for r in caplog.records]}"
        )
        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        # New recipe-level message: names the recipe and count, not individual steps
        assert "test-recipe" in record.getMessage()
        assert "1" in record.getMessage()  # count of depends_on steps
        assert "does not currently enforce" in record.getMessage()

    def test_warning_mentions_recipe_name_and_count(self, caplog):
        """The warning message names the recipe and the count of depends_on steps."""
        recipe = Recipe(
            name="my-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="step-c",
                    agent="a",
                    prompt="p",
                    depends_on=["step-a", "step-b"],
                ),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe)

        msg = caplog.records[0].getMessage()
        assert "my-recipe" in msg, (
            f"Warning should mention the recipe name; got: {msg!r}"
        )
        assert "1" in msg, (
            f"Warning should mention the count of depends_on steps (1); got: {msg!r}"
        )

    def test_no_warning_for_steps_without_depends_on(self, caplog):
        """Steps that don't declare depends_on produce no warning."""
        recipe = Recipe(
            name="clean-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-1", agent="a", prompt="p"),
                Step(id="step-2", agent="a", prompt="p"),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe)

        depends_on_warnings = [
            r for r in caplog.records if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 0, (
            f"Unexpected depends_on warning(s) for clean recipe: "
            f"{[r.getMessage() for r in depends_on_warnings]}"
        )

    def test_each_unique_step_warns_only_once(self, caplog):
        """Calling the helper twice for the same recipe/step warns only once."""
        recipe = Recipe(
            name="dup-test-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-x", agent="a", prompt="p", depends_on=["step-y"]),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe)
            _warn_depends_on_unenforced(recipe)  # second call — should be silent

        depends_on_warnings = [
            r for r in caplog.records if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 1, (
            f"Expected exactly 1 warning across two calls, got "
            f"{len(depends_on_warnings)}"
        )

    def test_deduplication_key_is_recipe_name_plus_step_id(self, caplog):
        """Same step ID in a different recipe generates a separate warning."""
        recipe_a = Recipe(
            name="recipe-A",
            description="test",
            version="1.0.0",
            steps=[Step(id="step-1", agent="a", prompt="p", depends_on=["x"])],
        )
        recipe_b = Recipe(
            name="recipe-B",
            description="test",
            version="1.0.0",
            steps=[Step(id="step-1", agent="a", prompt="p", depends_on=["x"])],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe_a)
            _warn_depends_on_unenforced(recipe_b)

        depends_on_warnings = [
            r for r in caplog.records if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 2, (
            f"Expected 2 warnings (one per recipe), got {len(depends_on_warnings)}"
        )

    def test_multiple_depends_on_steps_produce_one_recipe_warning(self, caplog):
        """Multiple depends_on steps in one recipe produce exactly ONE recipe-level warning.

        Previously this test asserted two warnings (one per step).  After the
        fix the warning is collapsed to one-per-recipe, so the expected count
        is 1.  The single message should mention the count (2 steps) not
        individual step IDs.
        """
        recipe = Recipe(
            name="multi-dep-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-a", agent="a", prompt="p", depends_on=["step-c"]),
                Step(id="step-b", agent="a", prompt="p", depends_on=["step-a"]),
                Step(id="step-c", agent="a", prompt="p"),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe)

        depends_on_warnings = [
            r for r in caplog.records if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 1, (
            f"Expected 1 recipe-level warning (not one per step), "
            f"got {len(depends_on_warnings)}: "
            f"{[r.getMessage() for r in depends_on_warnings]}"
        )
        msg = depends_on_warnings[0].getMessage()
        assert "multi-dep-recipe" in msg, f"Warning should name the recipe; got: {msg!r}"
        assert "2" in msg, f"Warning should mention 2 (step count); got: {msg!r}"

    def test_warning_mentions_declaration_order_note(self, caplog):
        """Warning text explicitly mentions declaration order execution."""
        recipe = Recipe(
            name="order-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="p", depends_on=["s0"])],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe)

        msg = caplog.records[0].getMessage()
        assert "declaration order" in msg, (
            f"Warning should mention 'declaration order'; got: {msg!r}"
        )


# ---------------------------------------------------------------------------
# New-behavior tests: one warning per recipe (not per step)
# These tests FAIL with the current per-step implementation and will PASS
# once _warn_depends_on_unenforced() is changed to warn once per recipe.
# ---------------------------------------------------------------------------


class TestWarnPerRecipeNotPerStep:
    """New behaviour: exactly one warning per recipe regardless of step count."""

    def test_three_steps_with_depends_on_emit_one_warning_not_three(self, caplog):
        """A recipe with 3 depends_on steps must produce exactly ONE warning, not 3."""
        recipe = Recipe(
            name="three-dep-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-z", agent="a", prompt="p"),
                Step(id="step-a", agent="a", prompt="p", depends_on=["step-z"]),
                Step(id="step-b", agent="a", prompt="p", depends_on=["step-a"]),
                Step(id="step-c", agent="a", prompt="p", depends_on=["step-b"]),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe)

        depends_on_warnings = [
            r for r in caplog.records if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 1, (
            f"Expected exactly 1 recipe-level warning for 3 depends_on steps, "
            f"got {len(depends_on_warnings)}: {[r.getMessage() for r in depends_on_warnings]}"
        )
        msg = depends_on_warnings[0].getMessage()
        assert "three-dep-recipe" in msg, f"Warning should name the recipe; got: {msg!r}"
        # Recipe-level message must NOT contain individual step IDs
        assert "step-a" not in msg, f"Recipe-level warning should not name step-a; got: {msg!r}"
        assert "step-b" not in msg, f"Recipe-level warning should not name step-b; got: {msg!r}"
        assert "step-c" not in msg, f"Recipe-level warning should not name step-c; got: {msg!r}"

    def test_same_recipe_called_twice_emits_one_warning_total(self, caplog):
        """Calling the helper twice for the same recipe must emit exactly ONE warning."""
        recipe = Recipe(
            name="twice-called-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-b", agent="a", prompt="p"),
                Step(id="step-a", agent="a", prompt="p", depends_on=["step-b"]),
                Step(id="step-c", agent="a", prompt="p", depends_on=["step-a"]),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe)
            _warn_depends_on_unenforced(recipe)  # second call — must be silent

        depends_on_warnings = [
            r for r in caplog.records if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 1, (
            f"Expected exactly 1 warning across two calls for the same recipe, "
            f"got {len(depends_on_warnings)}"
        )

    def test_two_different_recipes_emit_one_warning_each(self, caplog):
        """Two distinct recipes, each called once, produce exactly 2 total warnings."""
        recipe_x = Recipe(
            name="recipe-X",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="a", agent="a", prompt="p"),
                Step(id="b", agent="a", prompt="p", depends_on=["a"]),
                Step(id="c", agent="a", prompt="p", depends_on=["b"]),
            ],
        )
        recipe_y = Recipe(
            name="recipe-Y",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="a", agent="a", prompt="p"),
                Step(id="b", agent="a", prompt="p", depends_on=["a"]),
                Step(id="c", agent="a", prompt="p", depends_on=["b"]),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe_x)
            _warn_depends_on_unenforced(recipe_y)

        depends_on_warnings = [
            r for r in caplog.records if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 2, (
            f"Expected 2 warnings (one per recipe), got {len(depends_on_warnings)}"
        )
        warning_texts = " ".join(r.getMessage() for r in depends_on_warnings)
        assert "recipe-X" in warning_texts, "Missing recipe-X in warning texts"
        assert "recipe-Y" in warning_texts, "Missing recipe-Y in warning texts"

    def test_warning_message_mentions_depends_on_step_count(self, caplog):
        """The single recipe warning must mention the count of steps that declare depends_on."""
        recipe = Recipe(
            name="count-test-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="z", agent="a", prompt="p"),
                Step(id="a", agent="a", prompt="p", depends_on=["z"]),
                Step(id="b", agent="a", prompt="p", depends_on=["a"]),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            _warn_depends_on_unenforced(recipe)

        depends_on_warnings = [
            r for r in caplog.records if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 1
        msg = depends_on_warnings[0].getMessage()
        # The message should mention 2 (the count of steps that declare depends_on)
        assert "2" in msg, (
            f"Warning should mention the count of depends_on steps (2); got: {msg!r}"
        )


# ---------------------------------------------------------------------------
# Integration tests: warning fires via execute_recipe()
# ---------------------------------------------------------------------------


class TestDependsOnWarningViaExecuteRecipe:
    """Verify warning fires when execute_recipe() is called."""

    @pytest.mark.asyncio
    async def test_execute_recipe_emits_depends_on_warning(
        self, executor, mock_coordinator, temp_dir, caplog
    ):
        """
        When execute_recipe() is called with a recipe that contains a step
        with depends_on, a WARNING is logged before step execution begins.
        """
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        recipe = Recipe(
            name="warn-test-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="step-a",
                    agent="test-agent",
                    prompt="Do something",
                    output="out",
                    depends_on=["step-b"],  # depends_on declared but not enforced
                ),
            ],
        )

        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            await executor.execute_recipe(recipe, {}, temp_dir)

        depends_on_warnings = [
            r
            for r in caplog.records
            if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 1, (
            "Expected exactly one depends_on warning from execute_recipe(), "
            f"got {len(depends_on_warnings)}. All records: {[r.getMessage() for r in caplog.records]}"
        )
        msg = depends_on_warnings[0].getMessage()
        # New recipe-level message: names the recipe and count (not individual step IDs)
        assert "warn-test-recipe" in msg
        assert "1" in msg  # count of depends_on steps

    @pytest.mark.asyncio
    async def test_execute_recipe_no_warning_without_depends_on(
        self, executor, mock_coordinator, temp_dir, caplog
    ):
        """
        A recipe without any depends_on steps produces no depends_on warning.
        """
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        recipe = Recipe(
            name="clean-execute-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="step-1",
                    agent="test-agent",
                    prompt="Do something",
                    output="out",
                ),
            ],
        )

        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes.executor"):
            await executor.execute_recipe(recipe, {}, temp_dir)

        depends_on_warnings = [
            r
            for r in caplog.records
            if "does not currently enforce" in r.getMessage()
        ]
        assert len(depends_on_warnings) == 0, (
            f"Unexpected depends_on warnings for a clean recipe: "
            f"{[r.getMessage() for r in depends_on_warnings]}"
        )
