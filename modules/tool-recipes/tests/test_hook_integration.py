"""Integration tests for recipe executor hook event emission.

These tests use real HookRegistry (not mocks) to verify that recipe lifecycle
events are properly emitted and can be observed by external systems.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest
from amplifier_core.hooks import HookRegistry
from amplifier_core.models import HookResult
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.session import SessionManager


class TestHookEventEmission:
    """Integration tests for hook event emission during recipe execution."""

    @pytest.fixture
    def temp_project(self, tmp_path: Path) -> Path:
        """Create temporary project directory."""
        project = tmp_path / "test_project"
        project.mkdir()
        return project

    @pytest.fixture
    def hooks_registry(self) -> HookRegistry:
        """Create real HookRegistry instance."""
        return HookRegistry()

    @pytest.fixture
    def session_manager(self, tmp_path: Path) -> SessionManager:
        """Create SessionManager with temp directory."""
        sessions_dir = tmp_path / ".amplifier" / "projects"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return SessionManager(base_dir=sessions_dir, auto_cleanup_days=7)

    @pytest.fixture
    def coordinator(self, hooks_registry: HookRegistry) -> MagicMock:
        """Create mock coordinator with real HookRegistry."""
        coordinator = MagicMock()
        coordinator.hooks = hooks_registry
        coordinator.display_system = None  # No display needed for event tests
        coordinator.cancellation = None  # Prevent MagicMock truthy cancellation

        # Mock spawn capability for agent steps
        async def mock_spawn(**kwargs):
            """Mock spawn that returns simple result."""
            return {"output": "Mock agent result"}

        coordinator.get_capability = MagicMock(return_value=mock_spawn)
        coordinator.session = MagicMock()
        coordinator.config = {"agents": {}}

        return coordinator

    @pytest.mark.asyncio
    async def test_executor_emits_lifecycle_events(
        self,
        coordinator: MagicMock,
        session_manager: SessionManager,
        temp_project: Path,
    ):
        """Verify recipe executor emits all lifecycle events correctly."""
        # Capture events
        events_captured = []

        async def capture_event(event: str, data: dict) -> HookResult:
            """Hook handler that captures events."""
            events_captured.append((event, data))
            return HookResult(action="continue")

        # Register handlers for all recipe lifecycle events
        hooks: HookRegistry = coordinator.hooks
        hooks.register("recipe:start", capture_event)
        hooks.register("recipe:step", capture_event)
        hooks.register("recipe:complete", capture_event)

        # Create simple recipe YAML
        recipe_yaml = """
name: test-hook-emission
version: 1.0.0
description: Test recipe for hook event emission

steps:
  - id: step1
    agent: test-agent
    prompt: "Test step 1"
    output: result1

  - id: step2
    agent: test-agent
    prompt: "Test step 2"
    output: result2
"""

        recipe_file = temp_project / "test-recipe.yaml"
        recipe_file.write_text(recipe_yaml)

        # Load and execute recipe
        recipe = Recipe.from_yaml(recipe_file)
        executor = RecipeExecutor(coordinator, session_manager)

        context = await executor.execute_recipe(
            recipe=recipe,
            context_vars={},
            project_path=temp_project,
            recipe_path=recipe_file,
        )

        # Wait briefly for async event tasks to complete
        await asyncio.sleep(0.1)

        # Verify events were emitted
        event_names = [event for event, data in events_captured]

        assert "recipe:start" in event_names, "recipe:start event not emitted"
        assert "recipe:step" in event_names, "recipe:step event not emitted"
        assert "recipe:complete" in event_names, "recipe:complete event not emitted"

        # Verify recipe:step emitted for each step
        step_events = [
            data for event, data in events_captured if event == "recipe:step"
        ]
        assert len(step_events) >= 2, (
            f"Expected at least 2 step events, got {len(step_events)}"
        )

        # Verify event data structure
        start_event = next(
            data for event, data in events_captured if event == "recipe:start"
        )
        assert "name" in start_event, "recipe:start missing 'name'"
        assert "total_steps" in start_event, "recipe:start missing 'total_steps'"
        assert "steps" in start_event, "recipe:start missing 'steps'"
        assert start_event["name"] == "test-hook-emission"

        complete_event = next(
            data for event, data in events_captured if event == "recipe:complete"
        )
        assert "status" in complete_event, "recipe:complete missing 'status'"
        assert complete_event["status"] == "completed"

    @pytest.mark.asyncio
    async def test_executor_handles_missing_hooks_gracefully(
        self, session_manager: SessionManager, temp_project: Path
    ):
        """Verify executor works when coordinator has no hooks (backwards compatibility)."""
        # Create coordinator WITHOUT hooks
        coordinator = MagicMock()
        coordinator.hooks = None  # No hooks available
        coordinator.display_system = None
        coordinator.cancellation = None  # Prevent MagicMock truthy cancellation

        async def mock_spawn(**kwargs):
            return {"output": "Mock result"}

        coordinator.get_capability = MagicMock(return_value=mock_spawn)
        coordinator.session = MagicMock()
        coordinator.config = {"agents": {}}

        # Create simple recipe
        recipe_yaml = """
name: test-no-hooks
version: 1.0.0
description: Test recipe without hooks

steps:
  - id: step1
    agent: test-agent
    prompt: "Test step"
"""

        recipe_file = temp_project / "test-recipe.yaml"
        recipe_file.write_text(recipe_yaml)

        recipe = Recipe.from_yaml(recipe_file)
        executor = RecipeExecutor(coordinator, session_manager)

        # Should not raise - executor handles None hooks gracefully
        context = await executor.execute_recipe(
            recipe=recipe,
            context_vars={},
            project_path=temp_project,
            recipe_path=recipe_file,
        )

        assert context is not None
        assert "result" in context or context  # Recipe completed successfully

    @pytest.mark.asyncio
    async def test_hook_emit_uses_correct_api(
        self,
        coordinator: MagicMock,
        session_manager: SessionManager,
        temp_project: Path,
    ):
        """Verify that emit() method (not fire()) is called on HookRegistry.

        This test specifically validates the bug fix from issue #51.
        Previously, executor called hooks.fire() which doesn't exist.
        Now it correctly calls hooks.emit().
        """
        # Replace coordinator.hooks with a trackable mock
        # (Cannot monkey-patch Rust-backed HookRegistry.emit attribute directly)
        mock_hooks = MagicMock()
        mock_hooks.emit = AsyncMock(return_value=None)
        coordinator.hooks = mock_hooks

        # Create minimal recipe
        recipe_yaml = """
name: test-api-validation
version: 1.0.0
description: Validate correct API method called

steps:
  - id: step1
    agent: test-agent
    prompt: "Test"
"""

        recipe_file = temp_project / "test-recipe.yaml"
        recipe_file.write_text(recipe_yaml)

        recipe = Recipe.from_yaml(recipe_file)
        executor = RecipeExecutor(coordinator, session_manager)

        # Execute recipe
        await executor.execute_recipe(
            recipe=recipe,
            context_vars={},
            project_path=temp_project,
            recipe_path=recipe_file,
        )

        # Wait for background tasks
        await asyncio.sleep(0.1)

        # Verify emit() was called (proving fire() is not being called)
        assert mock_hooks.emit.called, (
            "HookRegistry.emit() was never called - fix may not be working"
        )

        # Verify it would fail with fire()
        real_hooks = HookRegistry()
        assert not hasattr(real_hooks, "fire"), (
            "HookRegistry should not have fire() method - this test validates the bug fix"
        )


class TestRecipeStartParentSessionId:
    """Verify recipe:start event payload includes parent_session_id only for sub-recipes."""

    RECIPE_YAML = """\
name: test-parent-id
version: 1.0.0
description: Test recipe for parent_session_id in recipe:start

steps:
  - id: step1
    agent: test-agent
    prompt: "Test step"
    output: result1
"""

    @pytest.fixture
    def temp_project(self, tmp_path: Path) -> Path:
        project = tmp_path / "test_project"
        project.mkdir()
        return project

    @pytest.fixture
    def session_manager(self, tmp_path: Path) -> SessionManager:
        sessions_dir = tmp_path / ".amplifier" / "projects"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return SessionManager(base_dir=sessions_dir, auto_cleanup_days=7)

    @pytest.fixture
    def coordinator(self) -> MagicMock:
        coordinator = MagicMock()
        coordinator.hooks = HookRegistry()
        coordinator.display_system = None
        coordinator.cancellation = None

        async def mock_spawn(**kwargs):
            return {"output": "Mock result"}

        coordinator.get_capability = MagicMock(return_value=mock_spawn)
        coordinator.session = MagicMock()
        coordinator.config = {"agents": {}}
        return coordinator

    async def _run_and_capture_start_event(
        self,
        coordinator: MagicMock,
        session_manager: SessionManager,
        temp_project: Path,
        parent_session_id: str | None,
    ) -> dict:
        """Execute a recipe and return the captured recipe:start event data."""
        captured = []

        async def capture(event: str, data: dict) -> HookResult:
            if event == "recipe:start":
                captured.append(data)
            return HookResult(action="continue")

        coordinator.hooks.register("recipe:start", capture)

        recipe_file = temp_project / "test-recipe.yaml"
        recipe_file.write_text(self.RECIPE_YAML)
        recipe = Recipe.from_yaml(recipe_file)
        executor = RecipeExecutor(coordinator, session_manager)

        await executor.execute_recipe(
            recipe=recipe,
            context_vars={},
            project_path=temp_project,
            recipe_path=recipe_file,
            parent_session_id=parent_session_id,
        )

        assert len(captured) == 1, "Expected exactly one recipe:start event"
        return captured[0]

    @pytest.mark.asyncio
    async def test_top_level_recipe_start_has_no_parent_session_id(
        self,
        coordinator: MagicMock,
        session_manager: SessionManager,
        temp_project: Path,
    ):
        """Top-level recipe (parent_session_id=None) must NOT emit parent_session_id."""
        data = await self._run_and_capture_start_event(
            coordinator, session_manager, temp_project, parent_session_id=None
        )
        assert "parent_session_id" not in data, (
            "recipe:start for a top-level recipe must not contain parent_session_id"
        )

    @pytest.mark.asyncio
    async def test_sub_recipe_start_includes_parent_session_id(
        self,
        coordinator: MagicMock,
        session_manager: SessionManager,
        temp_project: Path,
    ):
        """Sub-recipe (parent_session_id set) must emit parent_session_id in recipe:start."""
        data = await self._run_and_capture_start_event(
            coordinator, session_manager, temp_project,
            parent_session_id="test-parent-session",
        )
        assert "parent_session_id" in data, (
            "recipe:start for a sub-recipe must contain parent_session_id"
        )
        assert data["parent_session_id"] == "test-parent-session"
