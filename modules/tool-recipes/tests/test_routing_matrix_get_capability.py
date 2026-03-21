"""Tests verifying routing_matrix reads use get_capability("session.routing_matrix").

These tests verify BOTH routing_matrix read sites in executor.py use get_capability
instead of the legacy session_state pattern:
  - Site 1: step.model_role resolution (line ~1626)
  - Site 2: agent model_role fallback resolution (line ~1707)
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe, Step


@pytest.fixture
def mock_session_manager():
    """Create a mock session manager for routing_matrix tests."""
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


# =============================================================================
# Helpers
# =============================================================================

_ROUTING_MATRIX = {
    "name": "balanced",
    "roles": {
        "coding": {
            "candidates": [{"provider": "anthropic", "model": "claude-sonnet-4-6"}]
        },
        "general": {
            "candidates": [{"provider": "anthropic", "model": "claude-sonnet-4-6"}]
        },
    },
}


def _make_coordinator_capability_only(
    *,
    agents: dict | None = None,
    routing_matrix: dict | None = None,
) -> MagicMock:
    """Create a coordinator that uses get_capability API for routing_matrix.

    Critically: coordinator.session_state does NOT contain routing_matrix,
    so old code (session_state.get("routing_matrix")) would return None.
    Only the new get_capability("session.routing_matrix") call returns data.
    """
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": agents or {}}
    coordinator.hooks = None
    # session_state exists but does NOT contain routing_matrix
    coordinator.session_state = {}

    # Provide routing_matrix only through get_capability
    spawn_fn = AsyncMock(return_value="step result")
    capabilities = {
        "session.spawn": spawn_fn,
        "session.routing_matrix": routing_matrix,
    }

    def get_capability(key):
        return capabilities.get(key)

    coordinator.get_capability = get_capability
    coordinator.get.return_value = {"anthropic": MagicMock()}
    return coordinator


def _make_session_manager() -> MagicMock:
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


def _install_mock_resolver(mock_resolve_fn):
    """Install a mock amplifier_hooks_routing.resolver in sys.modules.

    Returns a cleanup callable.
    """
    mock_resolver_mod = types.ModuleType("amplifier_hooks_routing.resolver")
    mock_resolver_mod.resolve_model_role = mock_resolve_fn  # type: ignore[attr-defined]
    mock_hooks_mod = types.ModuleType("amplifier_hooks_routing")

    originals = {}
    for mod_name in ("amplifier_hooks_routing", "amplifier_hooks_routing.resolver"):
        if mod_name in sys.modules:
            originals[mod_name] = sys.modules[mod_name]

    sys.modules["amplifier_hooks_routing"] = mock_hooks_mod
    sys.modules["amplifier_hooks_routing.resolver"] = mock_resolver_mod

    def cleanup():
        for mod_name in ("amplifier_hooks_routing", "amplifier_hooks_routing.resolver"):
            if mod_name in originals:
                sys.modules[mod_name] = originals[mod_name]
            elif mod_name in sys.modules:
                del sys.modules[mod_name]

    return cleanup


# =============================================================================
# Site 1: step.model_role routing matrix via get_capability
# =============================================================================


class TestStepModelRoleViaGetCapability:
    """Verify Site 1 uses get_capability("session.routing_matrix") not session_state."""

    @pytest.mark.asyncio
    async def test_step_model_role_resolved_via_get_capability(
        self, mock_session_manager, temp_dir
    ):
        """step.model_role resolves via get_capability, NOT session_state.

        Before migration: reads self.coordinator.session_state.get("routing_matrix")
        After migration:  reads self.coordinator.get_capability("session.routing_matrix")

        The coordinator here has an EMPTY session_state (no routing_matrix),
        but get_capability("session.routing_matrix") returns the routing matrix.
        The resolved prefs should come from get_capability, not session_state.
        """
        async def mock_resolve(roles, matrix, providers):
            return [{"provider": "anthropic", "model": "claude-sonnet-4-6"}]

        cleanup = _install_mock_resolver(mock_resolve)
        try:
            coordinator = _make_coordinator_capability_only(
                routing_matrix=_ROUTING_MATRIX,
            )
            spawn_fn = coordinator.get_capability("session.spawn")

            recipe = Recipe(
                name="test-recipe",
                description="Test step model_role via get_capability",
                version="1.0.0",
                steps=[
                    Step(
                        id="do-work",
                        agent="coding-agent",
                        prompt="Write some code",
                        output="result",
                        model_role="coding",
                    ),
                ],
                context={},
            )

            executor = RecipeExecutor(coordinator, mock_session_manager)
            await executor.execute_recipe(recipe, {}, temp_dir)

            spawn_fn.assert_called_once()
            call_kwargs = spawn_fn.call_args[1]
            prefs = call_kwargs["provider_preferences"]

            assert prefs is not None, (
                "Expected routing-matrix-resolved prefs via get_capability, got None — "
                "step model_role Site 1 still uses session_state instead of get_capability"
            )
            assert len(prefs) == 1
            assert prefs[0].provider == "anthropic"
            assert prefs[0].model == "claude-sonnet-4-6"
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_step_model_role_no_routing_matrix_in_get_capability_returns_none(
        self, mock_session_manager, temp_dir
    ):
        """When get_capability("session.routing_matrix") returns None, prefs=None.

        This verifies the None check still works after migration.
        """
        async def mock_resolve(roles, matrix, providers):
            return [{"provider": "anthropic", "model": "claude-sonnet-4-6"}]

        cleanup = _install_mock_resolver(mock_resolve)
        try:
            coordinator = _make_coordinator_capability_only(
                routing_matrix=None,  # No routing matrix available
            )
            spawn_fn = coordinator.get_capability("session.spawn")

            recipe = Recipe(
                name="test-recipe",
                description="Test step model_role no routing matrix",
                version="1.0.0",
                steps=[
                    Step(
                        id="do-work",
                        agent="coding-agent",
                        prompt="Write some code",
                        output="result",
                        model_role="coding",
                    ),
                ],
                context={},
            )

            executor = RecipeExecutor(coordinator, mock_session_manager)
            await executor.execute_recipe(recipe, {}, temp_dir)

            spawn_fn.assert_called_once()
            call_kwargs = spawn_fn.call_args[1]
            prefs = call_kwargs["provider_preferences"]

            assert prefs is None, (
                "When routing matrix is None from get_capability, prefs should be None"
            )
        finally:
            cleanup()


# =============================================================================
# Site 2: agent model_role fallback routing matrix via get_capability
# =============================================================================


class TestAgentModelRoleFallbackViaGetCapability:
    """Verify Site 2 uses get_capability("session.routing_matrix") not session_state."""

    @pytest.mark.asyncio
    async def test_agent_model_role_resolved_via_get_capability(
        self, mock_session_manager, temp_dir
    ):
        """Agent model_role fallback resolves via get_capability, NOT session_state.

        Before migration: reads self.coordinator.session_state.get("routing_matrix")
        After migration:  reads self.coordinator.get_capability("session.routing_matrix")

        The coordinator here has an EMPTY session_state (no routing_matrix),
        but get_capability("session.routing_matrix") returns the routing matrix.
        Agent model_role fallback (Site 2) should resolve prefs via get_capability.
        """
        coordinator = _make_coordinator_capability_only(
            agents={
                "coding-agent": {
                    "description": "A coding agent",
                    "model_role": ["coding"],
                    # NO provider_preferences — routing hook hasn't fired yet
                }
            },
            routing_matrix=_ROUTING_MATRIX,
        )
        spawn_fn = coordinator.get_capability("session.spawn")

        recipe = Recipe(
            name="test-recipe",
            description="Test agent model_role fallback via get_capability",
            version="1.0.0",
            steps=[
                Step(
                    id="do-work",
                    agent="coding-agent",
                    prompt="Write some code",
                    output="result",
                    # No step-level model config — falls through to agent fallback
                ),
            ],
            context={},
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        spawn_fn.assert_called_once()
        call_kwargs = spawn_fn.call_args[1]
        prefs = call_kwargs["provider_preferences"]

        assert prefs is not None, (
            "Expected routing-matrix-resolved prefs via get_capability, got None — "
            "agent model_role fallback Site 2 still uses session_state instead of get_capability"
        )
        assert len(prefs) == 1
        assert prefs[0].provider == "anthropic"
        assert prefs[0].model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_agent_model_role_no_routing_matrix_in_get_capability(
        self, mock_session_manager, temp_dir
    ):
        """When get_capability("session.routing_matrix") returns None, prefs=None.

        Agent model_role fallback: no routing matrix → no prefs resolved.
        """
        coordinator = _make_coordinator_capability_only(
            agents={
                "coding-agent": {
                    "description": "A coding agent",
                    "model_role": ["coding"],
                }
            },
            routing_matrix=None,  # No routing matrix
        )
        spawn_fn = coordinator.get_capability("session.spawn")

        recipe = Recipe(
            name="test-recipe",
            description="Test agent model_role fallback no routing matrix",
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

        spawn_fn.assert_called_once()
        call_kwargs = spawn_fn.call_args[1]
        prefs = call_kwargs["provider_preferences"]

        assert prefs is None, (
            "When routing matrix is None from get_capability, prefs should be None"
        )
