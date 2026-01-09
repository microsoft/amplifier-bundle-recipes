"""Tests for executor condition evaluation integration."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step

# Note: amplifier_app_cli mocking handled in conftest.py to ensure proper cleanup


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator with async spawn capability."""
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    # get_capability returns an AsyncMock that tests can configure
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


@pytest.fixture
def mock_session_manager(temp_dir):
    """Create a mock session manager."""
    manager = MagicMock()
    manager.create_session.return_value = "test-session-id"
    manager.load_state.return_value = {
        "current_step_index": 0,
        "context": {},
        "completed_steps": [],
        "started": "2025-01-01T00:00:00",
    }
    # Mock cancellation methods to return no cancellation by default
    manager.is_cancellation_requested.return_value = False
    manager.is_immediate_cancellation.return_value = False
    return manager


@pytest.fixture
def conditional_recipe():
    """Create a recipe with conditional steps."""
    return Recipe(
        name="conditional-test",
        description="Recipe with conditions",
        version="1.0.0",
        steps=[
            Step(
                id="classify",
                agent="test-agent",
                prompt="Classify this",
                output="category",
            ),
            Step(
                id="simple-path",
                agent="test-agent",
                prompt="Simple processing",
                condition="{{category}} == 'simple'",
                output="result",
            ),
            Step(
                id="complex-path",
                agent="test-agent",
                prompt="Complex processing",
                condition="{{category}} == 'complex'",
                output="result",
            ),
            Step(
                id="always-run",
                agent="test-agent",
                prompt="Final step",
                output="final",
            ),
        ],
        context={},
    )


class TestExecutorConditions:
    """Tests for condition evaluation in executor."""

    @pytest.mark.asyncio
    async def test_condition_true_executes_step(self, mock_coordinator, mock_session_manager, temp_dir):
        """Step executes when condition evaluates to true."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # Mock spawn to return category='simple'
        mock_spawn.side_effect = ["simple", "simple result"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="p", output="category"),
                Step(id="s2", agent="a", prompt="p", condition="{{category}} == 'simple'", output="result"),
            ],
            context={},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # Both steps should have been executed
        assert mock_spawn.call_count == 2
        assert result["category"] == "simple"
        assert result["result"] == "simple result"

    @pytest.mark.asyncio
    async def test_condition_false_skips_step(self, mock_coordinator, mock_session_manager, temp_dir):
        """Step is skipped when condition evaluates to false."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # Mock spawn to return category='complex'
        mock_spawn.side_effect = ["complex", "complex result"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="p", output="category"),
                Step(id="simple", agent="a", prompt="p", condition="{{category}} == 'simple'", output="simple_result"),
                Step(
                    id="complex", agent="a", prompt="p", condition="{{category}} == 'complex'", output="complex_result"
                ),
            ],
            context={},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # s1 and complex should execute, simple should be skipped
        assert mock_spawn.call_count == 2
        assert result["category"] == "complex"
        assert result["complex_result"] == "complex result"
        assert "simple_result" not in result

    @pytest.mark.asyncio
    async def test_skipped_steps_tracked(self, mock_coordinator, mock_session_manager, temp_dir):
        """Skipped step IDs are tracked in context."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["no", "final"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="p", output="flag"),
                Step(id="conditional", agent="a", prompt="p", condition="{{flag}} == 'yes'"),
                Step(id="final", agent="a", prompt="p", output="final"),
            ],
            context={},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # Conditional step should be tracked as skipped
        assert "_skipped_steps" in result
        assert "conditional" in result["_skipped_steps"]

    @pytest.mark.asyncio
    async def test_no_condition_always_executes(self, mock_coordinator, mock_session_manager, temp_dir):
        """Steps without condition always execute."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["result1", "result2"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="p", output="r1"),
                Step(id="s2", agent="a", prompt="p", output="r2"),
            ],
            context={},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        assert mock_spawn.call_count == 2
        assert result["r1"] == "result1"
        assert result["r2"] == "result2"

    @pytest.mark.asyncio
    async def test_undefined_variable_in_condition_raises(self, mock_coordinator, mock_session_manager, temp_dir):
        """Undefined variable in condition raises ValueError."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["value"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="p", output="defined"),
                Step(id="s2", agent="a", prompt="p", condition="{{undefined}} == 'value'"),
            ],
            context={},
        )

        with pytest.raises(ValueError, match="condition error"):
            await executor.execute_recipe(recipe, {}, temp_dir)

    @pytest.mark.asyncio
    async def test_condition_with_and_operator(self, mock_coordinator, mock_session_manager, temp_dir):
        """Condition with 'and' operator works correctly."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["yes", "yes", "both_yes"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="p", output="a"),
                Step(id="s2", agent="a", prompt="p", output="b"),
                Step(
                    id="conditional",
                    agent="a",
                    prompt="p",
                    condition="{{a}} == 'yes' and {{b}} == 'yes'",
                    output="result",
                ),
            ],
            context={},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        assert mock_spawn.call_count == 3
        assert result["result"] == "both_yes"

    @pytest.mark.asyncio
    async def test_condition_with_or_operator(self, mock_coordinator, mock_session_manager, temp_dir):
        """Condition with 'or' operator works correctly."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["no", "yes", "one_yes"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="p", output="a"),
                Step(id="s2", agent="a", prompt="p", output="b"),
                Step(
                    id="conditional",
                    agent="a",
                    prompt="p",
                    condition="{{a}} == 'yes' or {{b}} == 'yes'",
                    output="result",
                ),
            ],
            context={},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        assert mock_spawn.call_count == 3
        assert result["result"] == "one_yes"
