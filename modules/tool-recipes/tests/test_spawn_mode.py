"""Tests for spawn_mode support in recipe Step model and executor."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from amplifier_module_tool_recipes.models import Recipe, Step
from amplifier_module_tool_recipes.executor import RecipeExecutor


class TestStepSpawnMode:
    """Tests for spawn_mode field on Step dataclass."""

    def test_step_spawn_mode_defaults_to_none(self):
        """Step spawn_mode defaults to None."""
        step = Step(id="test", agent="test-agent", prompt="Do something")
        assert step.spawn_mode is None

    def test_step_spawn_mode_subprocess_passes_validation(self):
        """Step with spawn_mode='subprocess' passes validation for agent steps."""
        step = Step(
            id="test",
            agent="test-agent",
            prompt="Do something",
            spawn_mode="subprocess",
        )
        errors = step.validate()
        assert not any("spawn_mode" in e for e in errors)

    def test_step_spawn_mode_invalid_value_fails_validation(self):
        """Step with spawn_mode='invalid' fails validation for agent steps."""
        step = Step(
            id="test",
            agent="test-agent",
            prompt="Do something",
            spawn_mode="invalid",
        )
        errors = step.validate()
        assert any("spawn_mode" in e for e in errors)

    def test_step_spawn_mode_on_bash_step_fails_validation(self):
        """spawn_mode on bash steps fails validation."""
        step = Step(
            id="test",
            type="bash",
            command="echo hello",
            spawn_mode="subprocess",
        )
        errors = step.validate()
        assert any("spawn_mode" in e for e in errors)

    def test_step_spawn_mode_on_recipe_step_fails_validation(self):
        """spawn_mode on recipe steps fails validation."""
        step = Step(
            id="test",
            type="recipe",
            recipe="sub-recipe.yaml",
            spawn_mode="subprocess",
        )
        errors = step.validate()
        assert any("spawn_mode" in e for e in errors)


# --- Executor integration tests ---


@pytest.fixture
def mock_session_manager():
    """Create a mock session manager."""
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


def _make_coordinator(*, agents: dict | None = None) -> MagicMock:
    """Create a mock coordinator with configurable agents dict."""
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": agents or {}}
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


class TestExecutorSpawnModeWiring:
    """Tests that executor passes use_subprocess based on step.spawn_mode."""

    @pytest.mark.asyncio
    async def test_executor_passes_use_subprocess_true_when_spawn_mode_subprocess(
        self, mock_session_manager, temp_dir
    ):
        """When step.spawn_mode='subprocess', executor passes use_subprocess=True."""
        coordinator = _make_coordinator(
            agents={
                "test-agent": {
                    "description": "A test agent",
                }
            },
        )
        mock_spawn = coordinator.get_capability.return_value
        mock_spawn.return_value = "step result"

        recipe = Recipe(
            name="test-recipe",
            description="Test spawn_mode subprocess wiring",
            version="1.0.0",
            steps=[
                Step(
                    id="do-work",
                    agent="test-agent",
                    prompt="Do something",
                    output="result",
                    spawn_mode="subprocess",
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        mock_spawn.assert_called_once()
        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["use_subprocess"] is True, (
            "Expected use_subprocess=True when spawn_mode='subprocess'"
        )

    @pytest.mark.asyncio
    async def test_executor_passes_use_subprocess_false_when_spawn_mode_none(
        self, mock_session_manager, temp_dir
    ):
        """When step.spawn_mode is None (default), executor passes use_subprocess=False."""
        coordinator = _make_coordinator(
            agents={
                "test-agent": {
                    "description": "A test agent",
                }
            },
        )
        mock_spawn = coordinator.get_capability.return_value
        mock_spawn.return_value = "step result"

        recipe = Recipe(
            name="test-recipe",
            description="Test spawn_mode default wiring",
            version="1.0.0",
            steps=[
                Step(
                    id="do-work",
                    agent="test-agent",
                    prompt="Do something",
                    output="result",
                    # spawn_mode not set — defaults to None
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        mock_spawn.assert_called_once()
        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["use_subprocess"] is False, (
            "Expected use_subprocess=False when spawn_mode is None (backward compat)"
        )
