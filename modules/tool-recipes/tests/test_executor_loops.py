"""Tests for executor loop (foreach) functionality."""

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
    # Mock cancellation methods to return no cancellation by default
    manager.is_cancellation_requested.return_value = False
    manager.is_immediate_cancellation.return_value = False
    return manager


class TestExecutorLoops:
    """Tests for foreach loop execution."""

    @pytest.mark.asyncio
    async def test_foreach_iterates_over_list(self, mock_coordinator, mock_session_manager, temp_dir):
        """Step iterates over each item in list."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["result_a", "result_b", "result_c"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop-step",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    collect="results",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # Should have called spawn 3 times (once per item)
        assert mock_spawn.call_count == 3
        assert result["results"] == ["result_a", "result_b", "result_c"]

    @pytest.mark.asyncio
    async def test_empty_list_skips_step(self, mock_coordinator, mock_session_manager, temp_dir):
        """Empty foreach list skips step without error."""
        mock_spawn = mock_coordinator.get_capability.return_value

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop-step",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    collect="results",
                ),
            ],
            context={"items": []},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # No calls should be made for empty list
        assert mock_spawn.call_count == 0
        # Step should be tracked as skipped
        assert "_skipped_steps" in result
        assert "loop-step" in result["_skipped_steps"]

    @pytest.mark.asyncio
    async def test_collect_aggregates_results(self, mock_coordinator, mock_session_manager, temp_dir):
        """Collect variable contains all iteration results."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["analysis_1", "analysis_2"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="analyze-files",
                    agent="analyzer",
                    prompt="Analyze {{current_file}}",
                    foreach="{{files}}",
                    as_var="current_file",
                    collect="analyses",
                ),
            ],
            context={"files": ["file1.py", "file2.py"]},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        assert result["analyses"] == ["analysis_1", "analysis_2"]

    @pytest.mark.asyncio
    async def test_as_changes_loop_variable_name(self, mock_coordinator, mock_session_manager, temp_dir):
        """Custom 'as' name is used for loop variable."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["done"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{my_item}}",
                    foreach="{{items}}",
                    as_var="my_item",
                    collect="results",
                ),
            ],
            context={"items": ["x"]},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        # Verify the loop was executed once with custom variable name
        assert mock_spawn.call_count == 1

    @pytest.mark.asyncio
    async def test_max_iterations_enforced(self, mock_coordinator, mock_session_manager, temp_dir):
        """Exceeding max_iterations fails recipe."""
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    max_iterations=3,  # Limit to 3
                ),
            ],
            context={"items": ["a", "b", "c", "d", "e"]},  # 5 items exceeds limit
        )

        with pytest.raises(ValueError, match="exceeds max_iterations"):
            await executor.execute_recipe(recipe, {}, temp_dir)

    @pytest.mark.asyncio
    async def test_non_list_foreach_fails(self, mock_coordinator, mock_session_manager, temp_dir):
        """Non-list foreach variable fails with clear error."""
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{value}}",
                ),
            ],
            context={"value": "not-a-list"},  # String, not list
        )

        with pytest.raises(ValueError, match="must be a list"):
            await executor.execute_recipe(recipe, {}, temp_dir)

    @pytest.mark.asyncio
    async def test_undefined_foreach_variable_fails(self, mock_coordinator, mock_session_manager, temp_dir):
        """Undefined foreach variable fails with clear error."""
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{undefined_var}}",
                ),
            ],
            context={},  # No 'undefined_var' defined
        )

        with pytest.raises(ValueError, match="Undefined variable"):
            await executor.execute_recipe(recipe, {}, temp_dir)

    @pytest.mark.asyncio
    async def test_loop_variable_scoped_to_step(self, mock_coordinator, mock_session_manager, temp_dir):
        """Loop variable not available after loop completes."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["loop_result", "final_result"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    collect="results",
                ),
                Step(
                    id="after",
                    agent="a",
                    prompt="After loop: {{results}}",
                    output="final",
                ),
            ],
            context={"items": ["x"]},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # Loop variable 'item' should not be in final context
        assert "item" not in result
        # But collect variable should be available
        assert "results" in result

    @pytest.mark.asyncio
    async def test_iteration_failure_stops_loop(self, mock_coordinator, mock_session_manager, temp_dir):
        """Any iteration failure immediately fails the recipe (fail-fast)."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # First call succeeds, second fails
        mock_spawn.side_effect = ["ok", Exception("Iteration error")]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    collect="results",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        with pytest.raises(ValueError, match="iteration 1 failed"):
            await executor.execute_recipe(recipe, {}, temp_dir)

        # Only 2 calls made (first succeeded, second failed)
        assert mock_spawn.call_count == 2

    @pytest.mark.asyncio
    async def test_output_without_collect_returns_last(self, mock_coordinator, mock_session_manager, temp_dir):
        """Without collect, output stores last iteration result."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["first", "second", "last"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    output="result",  # Not using collect
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # Output should be last iteration result
        assert result["result"] == "last"

    @pytest.mark.asyncio
    async def test_nested_variable_in_foreach(self, mock_coordinator, mock_session_manager, temp_dir):
        """Nested variable reference in foreach works."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["result_a", "result_b"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{data.files}}",
                    collect="results",
                ),
            ],
            context={"data": {"files": ["a.py", "b.py"]}},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        assert result["results"] == ["result_a", "result_b"]


class TestLoopValidation:
    """Tests for loop field validation in Step model."""

    def test_foreach_must_contain_variable_reference(self):
        """foreach must contain {{variable}} syntax."""
        step = Step(
            id="test",
            agent="a",
            prompt="p",
            foreach="not-a-variable",
        )
        errors = step.validate()
        assert any("foreach must contain a variable reference" in e for e in errors)

    def test_as_must_be_valid_variable_name(self):
        """as must be alphanumeric with underscores."""
        step = Step(
            id="test",
            agent="a",
            prompt="p",
            foreach="{{items}}",
            as_var="invalid-name!",
        )
        errors = step.validate()
        assert any("'as' must be a valid variable name" in e for e in errors)

    def test_collect_must_be_valid_variable_name(self):
        """collect must be alphanumeric with underscores."""
        step = Step(
            id="test",
            agent="a",
            prompt="p",
            foreach="{{items}}",
            collect="invalid-name!",
        )
        errors = step.validate()
        assert any("'collect' must be a valid variable name" in e for e in errors)

    def test_max_iterations_must_be_positive(self):
        """max_iterations must be positive."""
        step = Step(
            id="test",
            agent="a",
            prompt="p",
            foreach="{{items}}",
            max_iterations=0,
        )
        errors = step.validate()
        assert any("max_iterations must be positive" in e for e in errors)

    def test_valid_loop_step_passes_validation(self):
        """Valid loop step passes validation."""
        step = Step(
            id="test",
            agent="a",
            prompt="p",
            foreach="{{items}}",
            as_var="current_item",
            collect="results",
            max_iterations=50,
        )
        errors = step.validate()
        assert not errors

    def test_parallel_requires_foreach(self):
        """parallel=True without foreach should fail validation."""
        step = Step(
            id="test",
            agent="a",
            prompt="p",
            parallel=True,  # parallel without foreach
        )
        errors = step.validate()
        assert any("parallel requires foreach" in e for e in errors)

    def test_parallel_with_foreach_valid(self):
        """parallel=True with foreach should pass validation."""
        step = Step(
            id="test",
            agent="a",
            prompt="p",
            foreach="{{items}}",
            parallel=True,
            collect="results",
        )
        errors = step.validate()
        assert not errors


class TestParallelExecution:
    """Tests for parallel foreach execution."""

    @pytest.mark.asyncio
    async def test_parallel_foreach_executes_all_items(self, mock_coordinator, mock_session_manager, temp_dir):
        """parallel=True executes all iterations concurrently."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["result_a", "result_b", "result_c"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="parallel-loop",
                    agent="analyzer",
                    prompt="Analyze {{item}}",
                    foreach="{{items}}",
                    parallel=True,
                    collect="results",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # Should have called spawn 3 times
        assert mock_spawn.call_count == 3
        # Results should be in order
        assert result["results"] == ["result_a", "result_b", "result_c"]

    @pytest.mark.asyncio
    async def test_parallel_foreach_preserves_order(self, mock_coordinator, mock_session_manager, temp_dir):
        """Parallel execution preserves result order matching input order."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # Simulate varying response times by using simple returns
        mock_spawn.side_effect = ["first", "second", "third"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="parallel-loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    parallel=True,
                    collect="ordered_results",
                ),
            ],
            context={"items": ["x", "y", "z"]},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # Results should maintain order
        assert result["ordered_results"] == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_parallel_foreach_fail_fast(self, mock_coordinator, mock_session_manager, temp_dir):
        """Any parallel iteration failure fails the entire step."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # Second iteration fails
        mock_spawn.side_effect = [
            "ok",
            Exception("Parallel error"),
            "ok",
        ]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="parallel-loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    parallel=True,
                    collect="results",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        with pytest.raises(ValueError, match="iteration 1 failed"):
            await executor.execute_recipe(recipe, {}, temp_dir)

    @pytest.mark.asyncio
    async def test_parallel_foreach_empty_list_skips(self, mock_coordinator, mock_session_manager, temp_dir):
        """Empty list skips parallel step without error."""
        mock_spawn = mock_coordinator.get_capability.return_value

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="parallel-loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    parallel=True,
                    collect="results",
                ),
            ],
            context={"items": []},  # Empty list
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # No calls for empty list
        assert mock_spawn.call_count == 0
        assert "parallel-loop" in result.get("_skipped_steps", [])

    @pytest.mark.asyncio
    async def test_parallel_foreach_with_custom_as_var(self, mock_coordinator, mock_session_manager, temp_dir):
        """Parallel execution respects custom 'as' variable name."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", "r2"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="parallel-loop",
                    agent="a",
                    prompt="Analyze {{perspective}}",
                    foreach="{{perspectives}}",
                    as_var="perspective",
                    parallel=True,
                    collect="analyses",
                ),
            ],
            context={"perspectives": ["security", "performance"]},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        assert result["analyses"] == ["r1", "r2"]

    @pytest.mark.asyncio
    async def test_parallel_vs_sequential_same_results(self, mock_coordinator, mock_session_manager, temp_dir):
        """Parallel and sequential execution produce identical result structure."""
        mock_spawn = mock_coordinator.get_capability.return_value

        # Test sequential first
        mock_spawn.side_effect = ["s1", "s2", "s3"]
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        sequential_recipe = Recipe(
            name="seq-test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    parallel=False,  # Sequential
                    collect="results",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        seq_result = await executor.execute_recipe(sequential_recipe, {}, temp_dir)
        seq_call_count = mock_spawn.call_count

        # Reset and test parallel
        mock_spawn.reset_mock()
        mock_spawn.side_effect = ["s1", "s2", "s3"]

        parallel_recipe = Recipe(
            name="par-test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    agent="a",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    parallel=True,  # Parallel
                    collect="results",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        par_result = await executor.execute_recipe(parallel_recipe, {}, temp_dir)
        par_call_count = mock_spawn.call_count

        # Same number of calls
        assert seq_call_count == par_call_count == 3
        # Same results structure
        assert seq_result["results"] == par_result["results"]
