"""Tests for recipe composition (sub-recipe execution) functionality."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.executor import RecursionState
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import RecursionConfig
from amplifier_module_tool_recipes.models import Step


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


def create_sub_recipe_file(temp_dir, name: str, yaml_content: str):
    """Helper to create sub-recipe YAML file."""
    recipe_path = temp_dir / f"{name}.yaml"
    recipe_path.write_text(yaml_content)
    return recipe_path


class TestBasicComposition:
    """Tests for basic recipe composition functionality."""

    @pytest.mark.asyncio
    async def test_basic_composition(self, mock_coordinator, mock_session_manager, temp_dir):
        """Sub-recipe executes and returns context."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # Set up mock to return results for sub-recipe steps
        mock_spawn.side_effect = ["sub_result_1", "sub_result_2"]

        # Create sub-recipe file
        sub_recipe_yaml = """
name: sub-recipe
description: A sub-recipe for testing
version: "1.0.0"

context:
  input_value: ""

steps:
  - id: step1
    agent: test-agent
    prompt: "Process {{input_value}}"
    output: result1

  - id: step2
    agent: test-agent
    prompt: "Further process {{result1}}"
    output: result2
"""
        create_sub_recipe_file(temp_dir, "sub-recipe", sub_recipe_yaml)

        # Create parent recipe that invokes sub-recipe
        parent_recipe = Recipe(
            name="parent-recipe",
            description="Parent recipe",
            version="1.0.0",
            steps=[
                Step(
                    id="invoke-sub",
                    type="recipe",
                    recipe="sub-recipe.yaml",
                    step_context={"input_value": "test_input"},
                    output="sub_output",
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor.execute_recipe(parent_recipe, {}, temp_dir)

        # Sub-recipe should have been executed (2 steps)
        assert mock_spawn.call_count == 2
        # Output should contain sub-recipe's final context
        assert "sub_output" in result
        assert result["sub_output"]["result1"] == "sub_result_1"
        assert result["sub_output"]["result2"] == "sub_result_2"

    @pytest.mark.asyncio
    async def test_context_passing(self, mock_coordinator, mock_session_manager, temp_dir):
        """Only explicitly passed context is available in sub-recipe."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["processed_value"]

        sub_recipe_yaml = """
name: context-test-sub
description: Tests context passing
version: "1.0.0"

context:
  passed_var: ""

steps:
  - id: use-context
    agent: test-agent
    prompt: "Value is {{passed_var}}"
    output: result
"""
        create_sub_recipe_file(temp_dir, "context-sub", sub_recipe_yaml)

        parent_recipe = Recipe(
            name="parent",
            description="Parent",
            version="1.0.0",
            steps=[
                Step(
                    id="call-sub",
                    type="recipe",
                    recipe="context-sub.yaml",
                    step_context={"passed_var": "{{parent_value}}"},
                    output="output",
                ),
            ],
            context={"parent_value": "hello_from_parent"},
        )

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        await executor.execute_recipe(parent_recipe, {}, temp_dir)

        # Check that spawn was called and the instruction contains the passed value
        assert mock_spawn.call_count == 1
        call_kwargs = mock_spawn.call_args[1]
        instruction = call_kwargs.get("instruction", "")
        # The passed_var should be resolved to parent_value in the sub-recipe prompt
        assert "hello_from_parent" in instruction

    @pytest.mark.asyncio
    async def test_context_isolation(self, mock_coordinator, mock_session_manager, temp_dir):
        """Parent context variables are NOT automatically inherited by sub-recipe."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # Sub-recipe tries to use parent_only_var which should NOT be available
        sub_recipe_yaml = """
name: isolation-sub
description: Tests context isolation
version: "1.0.0"

context:
  explicit_var: ""

steps:
  - id: use-explicit
    agent: test-agent
    prompt: "Explicit: {{explicit_var}}"
    output: result
"""
        create_sub_recipe_file(temp_dir, "isolation-sub", sub_recipe_yaml)

        parent_recipe = Recipe(
            name="parent",
            description="Parent",
            version="1.0.0",
            steps=[
                Step(
                    id="call-sub",
                    type="recipe",
                    recipe="isolation-sub.yaml",
                    step_context={"explicit_var": "I_was_passed"},
                    output="output",
                ),
            ],
            context={
                "parent_only_var": "should_not_be_inherited",
                "another_parent_var": "also_not_inherited",
            },
        )

        mock_spawn.side_effect = ["result"]
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor.execute_recipe(parent_recipe, {}, temp_dir)

        # Sub-recipe should NOT have access to parent_only_var
        # Only explicit_var should be in sub-recipe context
        assert result["output"]["explicit_var"] == "I_was_passed"
        assert "parent_only_var" not in result["output"]
        assert "another_parent_var" not in result["output"]

    @pytest.mark.asyncio
    async def test_output_contains_sub_context(self, mock_coordinator, mock_session_manager, temp_dir):
        """Step output contains entire sub-recipe's final context."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["res1", "res2", "res3"]

        sub_recipe_yaml = """
name: multi-output-sub
description: Sub-recipe with multiple outputs
version: "1.0.0"

context:
  input: ""

steps:
  - id: s1
    agent: a
    prompt: "{{input}}"
    output: out1

  - id: s2
    agent: a
    prompt: "{{out1}}"
    output: out2

  - id: s3
    agent: a
    prompt: "{{out2}}"
    output: out3
"""
        create_sub_recipe_file(temp_dir, "multi-out", sub_recipe_yaml)

        parent_recipe = Recipe(
            name="parent",
            description="Parent",
            version="1.0.0",
            steps=[
                Step(
                    id="get-all",
                    type="recipe",
                    recipe="multi-out.yaml",
                    step_context={"input": "start"},
                    output="all_outputs",
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor.execute_recipe(parent_recipe, {}, temp_dir)

        # All sub-recipe outputs should be accessible
        assert result["all_outputs"]["out1"] == "res1"
        assert result["all_outputs"]["out2"] == "res2"
        assert result["all_outputs"]["out3"] == "res3"


class TestRecursionLimits:
    """Tests for recursion protection."""

    @pytest.mark.asyncio
    async def test_depth_limit_enforced(self, mock_coordinator, mock_session_manager, temp_dir):
        """Exceeding max_depth raises error."""
        # Create a self-referential recipe structure that would exceed depth
        # We'll create recipe-a -> recipe-b -> recipe-a (cycle at depth 3 with limit 2)
        recipe_a_yaml = """
name: recipe-a
description: First recipe
version: "1.0.0"

recursion:
  max_depth: 2

steps:
  - id: call-b
    type: recipe
    recipe: recipe-b.yaml
    context: {}
    output: result
"""
        recipe_b_yaml = """
name: recipe-b
description: Second recipe
version: "1.0.0"

steps:
  - id: call-a
    type: recipe
    recipe: recipe-a.yaml
    context: {}
    output: result
"""
        create_sub_recipe_file(temp_dir, "recipe-a", recipe_a_yaml)
        create_sub_recipe_file(temp_dir, "recipe-b", recipe_b_yaml)

        recipe_a = Recipe.from_yaml(temp_dir / "recipe-a.yaml")

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        with pytest.raises(ValueError, match="recursion depth.*exceeds limit"):
            await executor.execute_recipe(recipe_a, {}, temp_dir)

    @pytest.mark.asyncio
    async def test_total_steps_limit_enforced(self, mock_coordinator, mock_session_manager, temp_dir):
        """Exceeding max_total_steps raises error."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # Create recipe that would run more than max_total_steps
        sub_recipe_yaml = """
name: many-steps
description: Recipe with multiple steps
version: "1.0.0"

steps:
  - id: s1
    agent: a
    prompt: "step 1"
    output: r1

  - id: s2
    agent: a
    prompt: "step 2"
    output: r2

  - id: s3
    agent: a
    prompt: "step 3"
    output: r3
"""
        create_sub_recipe_file(temp_dir, "many-steps", sub_recipe_yaml)

        # Parent calls sub-recipe multiple times - total will exceed limit of 5
        parent_recipe = Recipe(
            name="parent",
            description="Parent",
            version="1.0.0",
            recursion=RecursionConfig(max_depth=10, max_total_steps=5),
            steps=[
                Step(
                    id="call1",
                    type="recipe",
                    recipe="many-steps.yaml",
                    step_context={},
                    output="r1",
                ),
                Step(
                    id="call2",
                    type="recipe",
                    recipe="many-steps.yaml",
                    step_context={},
                    output="r2",
                ),
            ],
            context={},
        )

        mock_spawn.side_effect = ["r"] * 10
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        with pytest.raises(ValueError, match="Total steps.*exceeds limit"):
            await executor.execute_recipe(parent_recipe, {}, temp_dir)

    @pytest.mark.asyncio
    async def test_step_level_recursion_override(self, mock_coordinator, mock_session_manager, temp_dir):
        """Per-step recursion config overrides recipe defaults."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # Deep sub-recipe that would fail with default depth=5
        # but succeeds with step-level override of depth=10
        sub_recipe_yaml = """
name: deep-sub
description: Deep recipe
version: "1.0.0"

steps:
  - id: s1
    agent: a
    prompt: "step"
    output: r1
"""
        create_sub_recipe_file(temp_dir, "deep-sub", sub_recipe_yaml)

        # Parent with strict default but lenient override on specific step
        parent_recipe = Recipe(
            name="parent",
            description="Parent",
            version="1.0.0",
            recursion=RecursionConfig(max_depth=1, max_total_steps=100),
            steps=[
                Step(
                    id="call-with-override",
                    type="recipe",
                    recipe="deep-sub.yaml",
                    step_context={},
                    output="result",
                    recursion=RecursionConfig(max_depth=5),  # Override allows deeper nesting
                ),
            ],
            context={},
        )

        mock_spawn.side_effect = ["result"]
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        # Should succeed because step-level override allows depth
        result = await executor.execute_recipe(parent_recipe, {}, temp_dir)
        assert "result" in result


class TestErrorHandling:
    """Tests for error handling in composition."""

    @pytest.mark.asyncio
    async def test_sub_recipe_failure_propagates(self, mock_coordinator, mock_session_manager, temp_dir):
        """Error in sub-recipe propagates up and raises."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # Sub-recipe that will fail
        sub_recipe_yaml = """
name: failing-sub
description: Recipe that fails
version: "1.0.0"

steps:
  - id: fail-step
    agent: a
    prompt: "will fail"
    output: result
"""
        create_sub_recipe_file(temp_dir, "failing-sub", sub_recipe_yaml)

        parent_recipe = Recipe(
            name="parent",
            description="Parent",
            version="1.0.0",
            steps=[
                Step(
                    id="may-fail",
                    type="recipe",
                    recipe="failing-sub.yaml",
                    step_context={},
                    output="result",
                ),
            ],
            context={},
        )

        # Sub-recipe step fails
        mock_spawn.side_effect = Exception("Sub-recipe failed")

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        # Error should propagate up
        with pytest.raises(Exception, match="Sub-recipe failed"):
            await executor.execute_recipe(parent_recipe, {}, temp_dir)


class TestCompositionWithLoops:
    """Tests for recipe composition with foreach loops."""

    @pytest.mark.asyncio
    async def test_composition_with_foreach(self, mock_coordinator, mock_session_manager, temp_dir):
        """Recipe step works in foreach loop."""
        mock_spawn = mock_coordinator.get_capability.return_value
        sub_recipe_yaml = """
name: process-item
description: Process single item
version: "1.0.0"

context:
  item: ""

steps:
  - id: process
    agent: a
    prompt: "Process {{item}}"
    output: processed
"""
        create_sub_recipe_file(temp_dir, "process-item", sub_recipe_yaml)

        parent_recipe = Recipe(
            name="parent",
            description="Parent",
            version="1.0.0",
            steps=[
                Step(
                    id="process-all",
                    type="recipe",
                    recipe="process-item.yaml",
                    step_context={"item": "{{current_item}}"},
                    output="item_result",
                    foreach="{{items}}",
                    as_var="current_item",
                    collect="all_results",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        mock_spawn.side_effect = ["processed_a", "processed_b", "processed_c"]
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor.execute_recipe(parent_recipe, {}, temp_dir)

        # Each item should have been processed via sub-recipe
        assert mock_spawn.call_count == 3
        assert "all_results" in result
        assert len(result["all_results"]) == 3

    @pytest.mark.asyncio
    async def test_composition_with_condition(self, mock_coordinator, mock_session_manager, temp_dir):
        """Recipe step respects conditions."""
        mock_spawn = mock_coordinator.get_capability.return_value
        sub_recipe_yaml = """
name: conditional-sub
description: Conditional sub-recipe
version: "1.0.0"

steps:
  - id: run
    agent: a
    prompt: "running"
    output: result
"""
        create_sub_recipe_file(temp_dir, "conditional-sub", sub_recipe_yaml)

        parent_recipe = Recipe(
            name="parent",
            description="Parent",
            version="1.0.0",
            steps=[
                Step(
                    id="conditional-call",
                    type="recipe",
                    recipe="conditional-sub.yaml",
                    step_context={},
                    output="result",
                    condition="{{should_run}}",
                ),
            ],
            context={"should_run": False},
        )

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor.execute_recipe(parent_recipe, {}, temp_dir)

        # Sub-recipe should NOT have been called due to false condition
        assert mock_spawn.call_count == 0
        assert "_skipped_steps" in result
        assert "conditional-call" in result["_skipped_steps"]

    @pytest.mark.asyncio
    async def test_composition_with_parallel(self, mock_coordinator, mock_session_manager, temp_dir):
        """Recipe step works with parallel: true in foreach."""
        mock_spawn = mock_coordinator.get_capability.return_value
        sub_recipe_yaml = """
name: parallel-sub
description: Parallel sub-recipe
version: "1.0.0"

context:
  item: ""

steps:
  - id: work
    agent: a
    prompt: "Process {{item}}"
    output: result
"""
        create_sub_recipe_file(temp_dir, "parallel-sub", sub_recipe_yaml)

        parent_recipe = Recipe(
            name="parent",
            description="Parent",
            version="1.0.0",
            steps=[
                Step(
                    id="parallel-call",
                    type="recipe",
                    recipe="parallel-sub.yaml",
                    step_context={"item": "{{current}}"},
                    output="item_result",
                    foreach="{{items}}",
                    as_var="current",
                    collect="results",
                    parallel=True,
                ),
            ],
            context={"items": ["x", "y", "z"]},
        )

        mock_spawn.side_effect = ["rx", "ry", "rz"]
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor.execute_recipe(parent_recipe, {}, temp_dir)

        # All items processed (in parallel)
        assert mock_spawn.call_count == 3
        assert "results" in result
        assert len(result["results"]) == 3


class TestRecursionState:
    """Tests for RecursionState tracking."""

    def test_recursion_state_default_values(self):
        """RecursionState has correct defaults."""
        state = RecursionState()
        assert state.current_depth == 0
        assert state.total_steps == 0
        assert state.max_depth == 5
        assert state.max_total_steps == 100
        assert state.recipe_stack == []

    def test_check_depth_within_limit(self):
        """check_depth passes when within limit."""
        state = RecursionState(current_depth=3, max_depth=5)
        # Should not raise
        state.check_depth("test-recipe")

    def test_check_depth_exceeds_limit(self):
        """check_depth raises when limit exceeded."""
        state = RecursionState(current_depth=5, max_depth=5)
        with pytest.raises(ValueError, match="recursion depth.*exceeds limit"):
            state.check_depth("test-recipe")

    def test_check_total_steps_within_limit(self):
        """check_total_steps passes when within limit."""
        state = RecursionState(total_steps=50, max_total_steps=100)
        # Should not raise
        state.check_total_steps()

    def test_check_total_steps_exceeds_limit(self):
        """check_total_steps raises when limit exceeded."""
        state = RecursionState(total_steps=100, max_total_steps=100)
        with pytest.raises(ValueError, match="Total steps.*exceeds limit"):
            state.check_total_steps()

    def test_increment_steps(self):
        """increment_steps increases count and checks limit."""
        state = RecursionState(total_steps=5, max_total_steps=100)
        state.increment_steps()
        assert state.total_steps == 6

    def test_increment_steps_exceeds_limit(self):
        """increment_steps raises when exceeding limit."""
        state = RecursionState(total_steps=98, max_total_steps=100)
        state.increment_steps()  # 99, under limit (99 < 100)
        # Next increment reaches 100, which triggers error (uses >= check)
        with pytest.raises(ValueError, match="Total steps.*exceeds limit"):
            state.increment_steps()  # 100, fails because 100 >= 100

    def test_enter_recipe_creates_child_state(self):
        """enter_recipe creates proper child state."""
        parent = RecursionState(
            current_depth=2,
            total_steps=10,
            max_depth=5,
            max_total_steps=100,
            recipe_stack=["recipe-a", "recipe-b"],
        )
        child = parent.enter_recipe("recipe-c")

        assert child.current_depth == 3  # Incremented
        assert child.total_steps == 10  # Preserved
        assert child.max_depth == 5  # Preserved
        assert child.max_total_steps == 100  # Preserved
        assert child.recipe_stack == ["recipe-a", "recipe-b", "recipe-c"]  # Appended

    def test_enter_recipe_with_override(self):
        """enter_recipe applies config override."""
        parent = RecursionState(max_depth=5, max_total_steps=100)
        override = RecursionConfig(max_depth=10, max_total_steps=500)
        child = parent.enter_recipe("recipe", override)

        assert child.max_depth == 10
        assert child.max_total_steps == 500
