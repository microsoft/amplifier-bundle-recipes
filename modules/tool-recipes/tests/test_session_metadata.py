"""Tests for session_metadata passthrough to spawn_fn (CP-5 Part 3).

Verifies that:
- spawn_fn receives session_metadata kwarg with recipe_name, step_id, agent_name
- session_metadata includes recipe_step_index
- parallel_group_id is included for parallel foreach spawns
- all spawns in one parallel batch share the same parallel_group_id
- different parallel batches get different parallel_group_ids
- sequential foreach spawns do NOT include parallel_group_id
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator with async spawn capability."""
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None  # Prevent MagicMock from being awaited in _show_progress
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
    manager.is_cancellation_requested.return_value = False
    manager.is_immediate_cancellation.return_value = False
    return manager


class TestSessionMetadataOnSpawn:
    """Tests that spawn_fn receives session_metadata with recipe context."""

    @pytest.mark.asyncio
    async def test_session_metadata_passed_to_spawn_fn(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """spawn_fn receives session_metadata kwarg with recipe_name, step_id, agent_name."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="my-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="analyze",
                    agent="code-analyzer",
                    prompt="Analyze {{input}}",
                    output="result",
                ),
            ],
            context={"input": "test-data"},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        assert mock_spawn.called
        call_kwargs = mock_spawn.call_args.kwargs
        assert "session_metadata" in call_kwargs
        metadata = call_kwargs["session_metadata"]
        assert metadata["recipe_name"] == "my-recipe"
        assert metadata["recipe_step"] == "analyze"
        assert metadata["agent_name"] == "code-analyzer"

    @pytest.mark.asyncio
    async def test_session_metadata_includes_step_index(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """session_metadata includes recipe_step_index matching step position."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="indexed-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-zero", agent="agent-a", prompt="Step 0", output="r0"),
                Step(id="step-one", agent="agent-b", prompt="Step 1", output="r1"),
            ],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        calls = mock_spawn.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["session_metadata"]["recipe_step_index"] == 0
        assert calls[1].kwargs["session_metadata"]["recipe_step_index"] == 1

    @pytest.mark.asyncio
    async def test_session_metadata_correct_recipe_name_per_step(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """All steps in a recipe report the same recipe_name."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", "r2"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="multi-step-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-a", agent="agent-x", prompt="Step A", output="ra"),
                Step(id="step-b", agent="agent-y", prompt="Step B", output="rb"),
            ],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        calls = mock_spawn.call_args_list
        assert len(calls) == 2
        for c in calls:
            assert c.kwargs["session_metadata"]["recipe_name"] == "multi-step-recipe"

    @pytest.mark.asyncio
    async def test_session_metadata_no_recipe_context_still_works(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """execute_step called without recipe context in context dict still works."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)

        # Call execute_step directly with an empty context (no "recipe" key)
        step = Step(id="standalone", agent="helper", prompt="Do something")
        await executor.execute_step(step, {})

        # spawn_fn should still be called successfully
        assert mock_spawn.called
        # session_metadata should be passed (with empty/default values) not absent
        call_kwargs = mock_spawn.call_args.kwargs
        assert "session_metadata" in call_kwargs
        # With no recipe context, recipe_name should be empty string
        assert call_kwargs["session_metadata"]["recipe_name"] == ""


class TestParallelGroupId:
    """Tests for parallel_group_id in parallel foreach spawns."""

    @pytest.mark.asyncio
    async def test_parallel_group_id_included_in_metadata(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """All parallel foreach spawns include parallel_group_id in session_metadata."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", "r2", "r3"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="parallel-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="parallel-step",
                    agent="worker",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    parallel=True,
                    collect="results",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        calls = mock_spawn.call_args_list
        assert len(calls) == 3
        for c in calls:
            assert "session_metadata" in c.kwargs
            assert "parallel_group_id" in c.kwargs["session_metadata"]
            # group_id should be a non-empty string
            assert c.kwargs["session_metadata"]["parallel_group_id"]

    @pytest.mark.asyncio
    async def test_parallel_group_id_same_within_batch(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """All spawns within one parallel batch share the same parallel_group_id."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", "r2", "r3"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="parallel-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="parallel-step",
                    agent="worker",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    parallel=True,
                    collect="results",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        calls = mock_spawn.call_args_list
        group_ids = [c.kwargs["session_metadata"]["parallel_group_id"] for c in calls]
        # All iterations in same batch share one group_id
        assert len(set(group_ids)) == 1
        assert group_ids[0]

    @pytest.mark.asyncio
    async def test_different_parallel_batches_get_different_group_ids(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Two separate parallel steps each get their own unique parallel_group_id."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", "r2", "r3", "r4"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="multi-parallel-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="parallel-step-1",
                    agent="worker",
                    prompt="Process {{item}}",
                    foreach="{{items1}}",
                    parallel=True,
                    collect="results1",
                ),
                Step(
                    id="parallel-step-2",
                    agent="worker",
                    prompt="Process {{item}}",
                    foreach="{{items2}}",
                    parallel=True,
                    collect="results2",
                ),
            ],
            context={"items1": ["a", "b"], "items2": ["c", "d"]},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        calls = mock_spawn.call_args_list
        assert len(calls) == 4

        batch1_group_ids = {
            calls[0].kwargs["session_metadata"]["parallel_group_id"],
            calls[1].kwargs["session_metadata"]["parallel_group_id"],
        }
        batch2_group_ids = {
            calls[2].kwargs["session_metadata"]["parallel_group_id"],
            calls[3].kwargs["session_metadata"]["parallel_group_id"],
        }

        # Within each batch, same group_id
        assert len(batch1_group_ids) == 1
        assert len(batch2_group_ids) == 1
        # Between batches, different group_ids
        assert batch1_group_ids != batch2_group_ids

    @pytest.mark.asyncio
    async def test_sequential_foreach_no_parallel_group_id(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Sequential foreach spawns do NOT include parallel_group_id."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", "r2"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="sequential-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="sequential-step",
                    agent="worker",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    collect="results",
                ),
            ],
            context={"items": ["a", "b"]},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        calls = mock_spawn.call_args_list
        assert len(calls) == 2
        for c in calls:
            assert "session_metadata" in c.kwargs
            assert "parallel_group_id" not in c.kwargs["session_metadata"]


class TestCostAttributionMetadata:
    """Tests for recipe_path and model_role in session_metadata (cost telemetry v2)."""

    @pytest.mark.asyncio
    async def test_recipe_path_included_when_known(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """session_metadata carries the recipe file path when execute_recipe got one."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="pathful-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="analyze", agent="worker", prompt="Go", output="r")],
            context={},
        )
        recipe_file = temp_dir / "pathful-recipe.yaml"

        await executor.execute_recipe(recipe, {}, temp_dir, recipe_path=recipe_file)

        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert metadata["recipe_path"] == str(recipe_file.resolve())

    @pytest.mark.asyncio
    async def test_recipe_path_is_canonicalized(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Relative recipe paths are made absolute so telemetry groups stably."""
        from pathlib import Path

        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="relative-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="analyze", agent="worker", prompt="Go", output="r")],
            context={},
        )

        await executor.execute_recipe(
            recipe, {}, temp_dir, recipe_path=Path("recipes/check.yaml")
        )

        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert metadata["recipe_path"] == str(Path("recipes/check.yaml").resolve())

    @pytest.mark.asyncio
    async def test_recipe_path_none_when_unknown(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Without a recipe_path, session_metadata still has the key, set to None."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="pathless-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="analyze", agent="worker", prompt="Go", output="r")],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert metadata["recipe_path"] is None

    @pytest.mark.asyncio
    async def test_model_role_recorded_when_step_role_resolves(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """A step-level model_role that resolves to preferences lands in session_metadata."""
        spawn_mock = AsyncMock(return_value="result")
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=[MagicMock()])
        mock_coordinator.get_capability.side_effect = lambda name: {
            "session.spawn": spawn_mock,
            "model_role_resolver": resolver,
        }.get(name)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="role-recipe",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="fast-step",
                    agent="worker",
                    prompt="Go",
                    output="r",
                    model_role="fast",
                ),
            ],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        resolver.resolve.assert_awaited_once_with("fast")
        metadata = spawn_mock.call_args.kwargs["session_metadata"]
        assert metadata["model_role"] == "fast"

    @pytest.mark.asyncio
    async def test_model_role_is_none_when_no_role(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """No role anywhere still emits the key, set to None.

        The key is unconditional (like ``recipe_path``): a grouping key that
        is sometimes absent forces every telemetry consumer to distinguish
        "this step used no role" from "this executor predates the field", and
        those are different facts.
        """
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="no-role-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="plain", agent="worker", prompt="Go", output="r")],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert "model_role" in metadata
        assert metadata["model_role"] is None

    @pytest.mark.asyncio
    async def test_model_role_recorded_when_agent_role_resolves(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Agent-level model_role (fallback 2) is recorded in session_metadata."""
        spawn_mock = AsyncMock(return_value="result")
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=[MagicMock()])
        mock_coordinator.config = {
            "agents": {"worker": {"model_role": "reasoning"}}
        }
        mock_coordinator.get_capability.side_effect = lambda name: {
            "session.spawn": spawn_mock,
            "model_role_resolver": resolver,
        }.get(name)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="agent-role-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="step", agent="worker", prompt="Go", output="r")],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        resolver.resolve.assert_awaited_once_with("reasoning")
        metadata = spawn_mock.call_args.kwargs["session_metadata"]
        assert metadata["model_role"] == "reasoning"

    @pytest.mark.asyncio
    async def test_model_role_attributed_for_routed_agent_preferences(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Post-routing shape: agent has model_role AND provider_preferences.

        hooks-routing resolves an agent's model_role into provider_preferences
        at session:start and leaves the role in the config, so the executor
        takes the agent-default-preferences branch. The role must still be
        attributed.
        """
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"
        mock_coordinator.config = {
            "agents": {
                "worker": {
                    "model_role": "reasoning",
                    "provider_preferences": [
                        {"provider": "anthropic", "model": "claude-opus-4-7"}
                    ],
                }
            }
        }

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="routed-agent-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="step", agent="worker", prompt="Go", output="r")],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert metadata["model_role"] == "reasoning"

    @pytest.mark.asyncio
    async def test_model_role_list_becomes_flat_label(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """A fallback-list model_role is flattened to a comma-joined string."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"
        mock_coordinator.config = {
            "agents": {
                "worker": {
                    "model_role": ["general", "fast"],
                    "provider_preferences": [
                        {"provider": "anthropic", "model": "claude-sonnet-5"}
                    ],
                }
            }
        }

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="list-role-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="step", agent="worker", prompt="Go", output="r")],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert metadata["model_role"] == "general,fast"

    @pytest.mark.asyncio
    async def test_declared_role_recorded_for_hand_pinned_preferences(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Hand-pinned preferences + a declared role record the declared role.

        Post-routing, role-derived and hand-pinned preferences are
        indistinguishable in the agent config, so telemetry records the
        DECLARED role for both shapes (see the executor comment): for
        hand-pinned preferences it is a label, not proof the role selected
        the provider. This test documents that deliberate choice.
        """
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"
        mock_coordinator.config = {
            "agents": {
                "worker": {
                    "model_role": "fast",
                    "provider_preferences": [
                        {"provider": "openai", "model": "gpt-5"}
                    ],
                }
            }
        }

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="pinned-prefs-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="step", agent="worker", prompt="Go", output="r")],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert metadata["model_role"] == "fast"

    @pytest.mark.asyncio
    async def test_recipe_path_canonicalized_in_staged_execution(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """The staged-recipe context site canonicalizes recipe_path too."""
        from pathlib import Path

        from amplifier_module_tool_recipes.models import Stage

        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="staged-recipe",
            description="test",
            version="1.0.0",
            steps=[],
            stages=[
                Stage(
                    name="stage-one",
                    steps=[
                        Step(id="analyze", agent="worker", prompt="Go", output="r")
                    ],
                )
            ],
            context={},
        )

        await executor.execute_recipe(
            recipe, {}, temp_dir, recipe_path=Path("recipes/staged.yaml")
        )

        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert metadata["recipe_path"] == str(Path("recipes/staged.yaml").resolve())

    @pytest.mark.asyncio
    async def test_model_role_withdrawn_when_pinning_drops_the_chain(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """A role whose whole chain fails to pin is NOT attributed.

        `pin_preferences_to_instances` (PR #86-#88) runs after role
        resolution and returns None when no preference names a provider
        instance this session mounted. The spawn then goes out with
        `provider_preferences=None` -- the PARENT's ordering -- so the role
        selected nothing. Recording it anyway would attribute the child's
        cost to a role that never applied.
        """
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"
        mock_coordinator.config = {
            "agents": {
                "worker": {
                    "model_role": "reasoning",
                    # Names a provider module this host has NOT mounted.
                    "provider_preferences": [
                        {"provider": "anthropic", "model": "claude-opus-5"}
                    ],
                }
            },
            # The only mounted instance is an unrelated module, so the
            # preference above is dropped and nothing survives pinning.
            "providers": [
                {
                    "id": "sol",
                    "instance_id": "sol",
                    "module": "provider-openai",
                    "config": {"priority": 1, "default_model": "gpt-5"},
                }
            ],
        }

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="unpinnable-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="step", agent="worker", prompt="Go", output="r")],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        # The chain really was dropped -- otherwise this test proves nothing.
        assert mock_spawn.call_args.kwargs["provider_preferences"] is None
        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert metadata["model_role"] is None

    @pytest.mark.asyncio
    async def test_model_role_survives_pinning_when_the_chain_pins(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """The companion case: a chain that DOES pin keeps its attribution.

        Guards the withdrawal above from over-reaching -- pinning rewrites
        the chain to instance ids, which must not look like a dropped chain.
        """
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.return_value = "result"
        mock_coordinator.config = {
            "agents": {
                "worker": {
                    "model_role": "reasoning",
                    "provider_preferences": [
                        {"provider": "anthropic", "model": "claude-opus-5"}
                    ],
                }
            },
            "providers": [
                {
                    "id": "opus",
                    "instance_id": "opus",
                    "module": "provider-anthropic",
                    "config": {"priority": 1, "default_model": "claude-opus-5"},
                }
            ],
        }

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="pinnable-recipe",
            description="test",
            version="1.0.0",
            steps=[Step(id="step", agent="worker", prompt="Go", output="r")],
            context={},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        prefs = mock_spawn.call_args.kwargs["provider_preferences"]
        assert prefs is not None and prefs[0].provider == "opus"
        metadata = mock_spawn.call_args.kwargs["session_metadata"]
        assert metadata["model_role"] == "reasoning"
