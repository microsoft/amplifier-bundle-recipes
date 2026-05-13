"""Tests for agent-level provider_preferences fallback in recipe executor."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from amplifier_foundation.spawn_utils import ProviderPreference
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import ProviderPreferenceConfig, Recipe, Step


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


def _make_resolver(
    *,
    return_value: list | None = None,
    name: str = "test-matrix",
):
    """Build a duck-typed ``model_role_resolver`` mock (capability contract).

        async def resolve(model_role: str | list[str]) -> list[ProviderPreference]
    """
    resolver = MagicMock()
    resolver.name = name
    resolver.resolve = AsyncMock(return_value=return_value if return_value is not None else [])
    return resolver


def _make_coordinator(
    *,
    agents: dict | None = None,
    model_role_resolver=None,
) -> MagicMock:
    """Create a mock coordinator with configurable agents dict.

    The recipe executor looks up two capabilities:
      - ``session.spawn`` — the per-step sub-session spawn function (default:
        a fresh ``AsyncMock`` so tests can call ``mock_spawn = coordinator.
        get_capability.return_value`` and inspect it).
      - ``model_role_resolver`` — duck-typed model-role resolver. ``None`` (the
        default) simulates "no routing bundle installed".
    """
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": agents or {}}
    coordinator.hooks = None

    # Default spawn capability: an AsyncMock that the existing tests pull off
    # ``coordinator.get_capability.return_value``. Preserve that ergonomic.
    spawn_capability = AsyncMock()
    capabilities: dict = {
        "session.spawn": spawn_capability,
        "model_role_resolver": model_role_resolver,
    }
    coordinator.get_capability.return_value = spawn_capability
    coordinator.get_capability.side_effect = lambda name: capabilities.get(name)
    return coordinator


class TestAgentProviderPreferencesFallback:
    """Tests for agent-level provider_preferences fallback in execute_step().

    Priority order (highest first):
      1. step.provider_preferences
      2. step.model_role
      3. step.provider + step.model
      4. step.provider (only)
      5. agent config provider_preferences  <-- fallback 1 (PR #47)
      6. agent config model_role resolved directly  <-- fallback 2 (this fix)
      7. None (inherit parent model)
    """

    @pytest.mark.asyncio
    async def test_agent_prefs_applied_when_step_has_no_model_config(
        self, mock_session_manager, temp_dir
    ):
        """Agent's provider_preferences used when step has no model config at all."""
        coordinator = _make_coordinator(
            agents={
                "budget-agent": {
                    "description": "A budget-tier agent",
                    "provider_preferences": [
                        {"provider": "anthropic", "model": "claude-haiku-*"},
                        {"provider": "openai", "model": "gpt-5-mini"},
                    ],
                }
            },
        )
        mock_spawn = coordinator.get_capability.return_value
        mock_spawn.return_value = "step result"

        recipe = Recipe(
            name="test-recipe",
            description="Test agent-level prefs fallback",
            version="1.0.0",
            steps=[
                Step(
                    id="do-work",
                    agent="budget-agent",
                    prompt="Do something simple",
                    output="result",
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        mock_spawn.assert_called_once()
        call_kwargs = mock_spawn.call_args[1]
        prefs = call_kwargs["provider_preferences"]

        assert prefs is not None, "Expected agent-level prefs, got None"
        assert len(prefs) == 2
        assert prefs[0].provider == "anthropic"
        assert prefs[0].model == "claude-haiku-*"
        assert prefs[1].provider == "openai"
        assert prefs[1].model == "gpt-5-mini"

    @pytest.mark.asyncio
    async def test_step_prefs_override_agent_prefs(
        self, mock_session_manager, temp_dir
    ):
        """Step-level provider_preferences fully replace agent-level defaults."""
        coordinator = _make_coordinator(
            agents={
                "budget-agent": {
                    "description": "A budget-tier agent",
                    "provider_preferences": [
                        {"provider": "anthropic", "model": "claude-haiku-*"},
                    ],
                }
            },
        )
        mock_spawn = coordinator.get_capability.return_value
        mock_spawn.return_value = "step result"

        recipe = Recipe(
            name="test-recipe",
            description="Test step-level prefs win",
            version="1.0.0",
            steps=[
                Step(
                    id="do-work",
                    agent="budget-agent",
                    prompt="Do something",
                    output="result",
                    provider_preferences=[
                        ProviderPreferenceConfig(
                            provider="openai", model="gpt-5-turbo"
                        ),
                    ],
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        mock_spawn.assert_called_once()
        call_kwargs = mock_spawn.call_args[1]
        prefs = call_kwargs["provider_preferences"]

        assert len(prefs) == 1, "Step prefs should fully replace agent defaults"
        assert prefs[0].provider == "openai"
        assert prefs[0].model == "gpt-5-turbo"

    @pytest.mark.asyncio
    async def test_no_prefs_anywhere_passes_none(self, mock_session_manager, temp_dir):
        """No step or agent prefs -> provider_preferences is None (inherit parent)."""
        coordinator = _make_coordinator(
            agents={
                "plain-agent": {
                    "description": "Agent with no provider preferences",
                }
            },
        )
        mock_spawn = coordinator.get_capability.return_value
        mock_spawn.return_value = "step result"

        recipe = Recipe(
            name="test-recipe",
            description="Test no prefs anywhere",
            version="1.0.0",
            steps=[
                Step(
                    id="do-work",
                    agent="plain-agent",
                    prompt="Do something",
                    output="result",
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        mock_spawn.assert_called_once()
        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["provider_preferences"] is None, (
            "Without step or agent prefs, should be None to inherit parent model"
        )

    @pytest.mark.asyncio
    async def test_agent_model_role_resolved_when_no_prefs_set(
        self, mock_session_manager, temp_dir
    ):
        """Agent model_role resolved directly when routing hook hasn't fired yet.

        Simulates step 1 of a recipe: the routing hook fires lazily (after first
        child spawn), so provider_preferences is NOT yet in agent config. But
        model_role IS present. The executor should fall back to resolving the role
        via the model_role_resolver capability registered by a routing bundle.
        """
        # Simulate the resolver capability registered by a routing bundle.
        # The "no prefs yet" pathway hits the agent-level fallback: the
        # executor must call resolver.resolve(agent_model_role) and pass the
        # returned ProviderPreferences down to spawn_fn.
        resolver = _make_resolver(
            return_value=[
                ProviderPreference(provider="anthropic", model="claude-sonnet-4-6"),
            ]
        )
        coordinator = _make_coordinator(
            agents={
                "coding-agent": {
                    "description": "A coding agent",
                    "model_role": ["coding", "general"],
                    # NO provider_preferences — routing hook hasn't fired yet
                }
            },
            model_role_resolver=resolver,
        )
        coordinator.get.return_value = {"anthropic": MagicMock()}
        mock_spawn = coordinator.get_capability.return_value
        mock_spawn.return_value = "step result"

        recipe = Recipe(
            name="test-recipe",
            description="Test model_role direct resolution fallback",
            version="1.0.0",
            steps=[
                Step(
                    id="do-work",
                    agent="coding-agent",
                    prompt="Write some code",
                    output="result",
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        mock_spawn.assert_called_once()
        call_kwargs = mock_spawn.call_args[1]
        prefs = call_kwargs["provider_preferences"]

        assert prefs is not None, (
            "Expected resolver-resolved prefs, got None — "
            "agent model_role fallback didn't fire"
        )
        assert len(prefs) == 1
        assert prefs[0].provider == "anthropic"
        assert prefs[0].model == "claude-sonnet-4-6"

    async def test_step_model_role_resolved_via_resolver_capability(
        self, mock_session_manager, temp_dir
    ):
        """Step-level model_role resolves via the model_role_resolver capability.

        Regression test: prior to renaming routing_matrix → model_role_resolver,
        the executor read ``coordinator.session_state["routing_matrix"]`` and
        imported ``resolve_model_role`` from a hardcoded module path. The new
        capability-based contract is:

            resolver = coordinator.get_capability("model_role_resolver")
            prefs = await resolver.resolve(step.model_role)

        If the executor regresses to the wrong capability name, fails to await,
        or stops calling .resolve(), provider_preferences will stay ``None``
        and the assertions below will catch the regression.
        """
        # Resolver returns a deterministic preference for role "coding".
        resolver = _make_resolver(
            return_value=[
                ProviderPreference(provider="anthropic", model="claude-sonnet-4-6"),
            ]
        )
        coordinator = _make_coordinator(
            agents={
                "coding-agent": {
                    "description": "A coding agent",
                    # No agent-level model_role or provider_preferences —
                    # role is set on the step, not the agent.
                }
            },
            model_role_resolver=resolver,
        )
        coordinator.get.return_value = {"anthropic": MagicMock()}
        mock_spawn = coordinator.get_capability.return_value
        mock_spawn.return_value = "step result"

        recipe = Recipe(
            name="test-recipe",
            description="Test step-level model_role resolution",
            version="1.0.0",
            steps=[
                Step(
                    id="do-work",
                    agent="coding-agent",
                    prompt="Write some code",
                    model_role="coding",
                    output="result",
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        # Resolver was consulted once with the step's model_role string.
        resolver.resolve.assert_called_once_with("coding")

        mock_spawn.assert_called_once()
        call_kwargs = mock_spawn.call_args[1]
        prefs = call_kwargs["provider_preferences"]

        assert prefs is not None, (
            "Expected step-level model_role to resolve via model_role_resolver "
            "capability, got None — likely the executor failed to call "
            "coordinator.get_capability(\"model_role_resolver\")."
        )
        assert len(prefs) == 1
        assert prefs[0].provider == "anthropic"
        assert prefs[0].model == "claude-sonnet-4-6"
