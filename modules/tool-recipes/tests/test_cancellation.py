"""Tests for recipe cancellation functionality."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_module_tool_recipes.executor import (
    CancellationRequestedError,
    RecipeExecutor,
)
from amplifier_module_tool_recipes.models import Recipe, Step
from amplifier_module_tool_recipes.session import CancellationStatus, SessionManager


class TestCancellationStatus:
    """Tests for CancellationStatus enum."""

    def test_all_statuses_exist(self):
        """All expected statuses are defined."""
        assert CancellationStatus.NONE.value == "none"
        assert CancellationStatus.REQUESTED.value == "requested"  # Graceful cancellation
        assert CancellationStatus.IMMEDIATE.value == "immediate"
        assert CancellationStatus.CANCELLED.value == "cancelled"

    def test_status_from_string(self):
        """Status can be created from string value."""
        assert CancellationStatus("none") == CancellationStatus.NONE
        assert CancellationStatus("requested") == CancellationStatus.REQUESTED
        assert CancellationStatus("immediate") == CancellationStatus.IMMEDIATE
        assert CancellationStatus("cancelled") == CancellationStatus.CANCELLED


class TestSessionManagerCancellation:
    """Tests for SessionManager cancellation methods."""

    def test_request_graceful_cancellation(self, session_manager: SessionManager, temp_dir: Path):
        """Request graceful cancellation sets correct status."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="p")],
        )
        session_id = session_manager.create_session(recipe, temp_dir)

        session_manager.request_cancellation(session_id, temp_dir, immediate=False)

        status = session_manager.get_cancellation_status(session_id, temp_dir)
        assert status == CancellationStatus.REQUESTED  # Graceful = REQUESTED
        assert session_manager.is_cancellation_requested(session_id, temp_dir)
        assert not session_manager.is_immediate_cancellation(session_id, temp_dir)

    def test_request_immediate_cancellation(self, session_manager: SessionManager, temp_dir: Path):
        """Request immediate cancellation sets correct status."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="p")],
        )
        session_id = session_manager.create_session(recipe, temp_dir)

        session_manager.request_cancellation(session_id, temp_dir, immediate=True)

        status = session_manager.get_cancellation_status(session_id, temp_dir)
        assert status == CancellationStatus.IMMEDIATE
        assert session_manager.is_cancellation_requested(session_id, temp_dir)
        assert session_manager.is_immediate_cancellation(session_id, temp_dir)

    def test_clear_cancellation(self, session_manager: SessionManager, temp_dir: Path):
        """Clear cancellation resets status to NONE (only works after CANCELLED status)."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="p")],
        )
        session_id = session_manager.create_session(recipe, temp_dir)

        # First mark as cancelled (clear only works on CANCELLED status)
        session_manager.mark_cancelled(session_id, temp_dir, cancelled_at_step="step-1")
        status = session_manager.get_cancellation_status(session_id, temp_dir)
        assert status == CancellationStatus.CANCELLED

        session_manager.clear_cancellation(session_id, temp_dir)

        assert not session_manager.is_cancellation_requested(session_id, temp_dir)
        status = session_manager.get_cancellation_status(session_id, temp_dir)
        assert status == CancellationStatus.NONE

    def test_mark_cancelled(self, session_manager: SessionManager, temp_dir: Path):
        """Mark cancelled sets final CANCELLED status with step info."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="p")],
        )
        session_id = session_manager.create_session(recipe, temp_dir)

        session_manager.mark_cancelled(session_id, temp_dir, cancelled_at_step="step-5")

        status = session_manager.get_cancellation_status(session_id, temp_dir)
        assert status == CancellationStatus.CANCELLED

        # Verify step info is saved
        state = session_manager.load_state(session_id, temp_dir)
        assert state.get("cancelled_at_step") == "step-5"
        assert "cancelled_at" in state

    def test_nonexistent_session_returns_none_status(
        self, session_manager: SessionManager, temp_dir: Path
    ):
        """Nonexistent session returns NONE status without error."""
        status = session_manager.get_cancellation_status("nonexistent-session", temp_dir)
        assert status == CancellationStatus.NONE
        assert not session_manager.is_cancellation_requested("nonexistent-session", temp_dir)

    def test_upgrade_graceful_to_immediate(self, session_manager: SessionManager, temp_dir: Path):
        """Can upgrade from graceful to immediate cancellation."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="p")],
        )
        session_id = session_manager.create_session(recipe, temp_dir)

        # Request graceful first
        session_manager.request_cancellation(session_id, temp_dir, immediate=False)
        assert not session_manager.is_immediate_cancellation(session_id, temp_dir)

        # Upgrade to immediate
        session_manager.request_cancellation(session_id, temp_dir, immediate=True)
        assert session_manager.is_immediate_cancellation(session_id, temp_dir)


class TestCancellationRequestedError:
    """Tests for CancellationRequestedError exception."""

    def test_exception_attributes(self):
        """Exception stores all attributes correctly."""
        error = CancellationRequestedError(
            session_id="test-session",
            is_immediate=True,
            current_step="analyze-code",
        )

        assert error.session_id == "test-session"
        assert error.is_immediate is True
        assert error.current_step == "analyze-code"

    def test_exception_message_graceful(self):
        """Graceful cancellation message is formatted correctly."""
        error = CancellationRequestedError(
            session_id="test-session",
            is_immediate=False,
            current_step="step-1",
        )

        assert "test-session" in str(error)
        assert "graceful" in str(error)
        assert "step-1" in str(error)

    def test_exception_message_immediate(self):
        """Immediate cancellation message is formatted correctly."""
        error = CancellationRequestedError(
            session_id="test-session",
            is_immediate=True,
            current_step="step-1",
        )

        assert "test-session" in str(error)
        assert "immediate" in str(error)

    def test_exception_is_an_exception(self):
        """CancellationRequestedError is a proper Exception subclass."""
        assert issubclass(CancellationRequestedError, Exception)
        assert issubclass(CancellationRequestedError, BaseException)


class TestExecutorCancellation:
    """Tests for RecipeExecutor cancellation behavior."""

    @pytest.fixture
    def mock_coordinator(self):
        """Create a mock coordinator."""
        coordinator = MagicMock()
        coordinator.session = MagicMock()
        coordinator.config = {"agents": {}}
        coordinator.get_capability.return_value = AsyncMock(return_value="result")
        # No cancellation token by default
        coordinator.cancellation = None
        return coordinator

    @pytest.fixture
    def real_session_manager(self, temp_dir: Path):
        """Create a real session manager for cancellation tests."""
        return SessionManager(base_dir=temp_dir, auto_cleanup_days=7)

    @pytest.mark.asyncio
    async def test_graceful_cancellation_before_step(
        self, mock_coordinator, real_session_manager, temp_dir: Path
    ):
        """Graceful cancellation requested before execution raises at first step."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-1", agent="a", prompt="First"),
                Step(id="step-2", agent="a", prompt="Second"),
            ],
        )

        # Create session and request graceful cancellation before execution
        session_id = real_session_manager.create_session(recipe, temp_dir)
        real_session_manager.request_cancellation(session_id, temp_dir, immediate=False)

        mock_coordinator.get_capability.return_value = AsyncMock(return_value="result")
        executor = RecipeExecutor(mock_coordinator, real_session_manager)

        with pytest.raises(CancellationRequestedError) as exc_info:
            await executor.execute_recipe(recipe, {}, temp_dir, session_id=session_id)

        # Should raise before executing any steps
        assert not exc_info.value.is_immediate
        assert exc_info.value.current_step == "step-1"
        # Verify spawn was never called (cancelled before step execution)
        mock_coordinator.get_capability.return_value.assert_not_called()

    @pytest.mark.asyncio
    async def test_immediate_cancellation_stops_immediately(
        self, mock_coordinator, real_session_manager, temp_dir: Path
    ):
        """Immediate cancellation stops without completing current step."""
        executor = RecipeExecutor(mock_coordinator, real_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-1", agent="a", prompt="First"),
                Step(id="step-2", agent="a", prompt="Second"),
            ],
        )

        # Pre-create session and request immediate cancellation
        session_id = real_session_manager.create_session(recipe, temp_dir)
        real_session_manager.request_cancellation(session_id, temp_dir, immediate=True)

        with pytest.raises(CancellationRequestedError) as exc_info:
            await executor.execute_recipe(recipe, {}, temp_dir, session_id=session_id)

        assert exc_info.value.is_immediate
        assert exc_info.value.current_step == "step-1"

    @pytest.mark.asyncio
    async def test_cancellation_in_foreach_loop(
        self, mock_coordinator, real_session_manager, temp_dir: Path
    ):
        """Cancellation during foreach loop stops at iteration boundary."""
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
            context={"items": ["a", "b", "c", "d", "e"]},
        )

        # Pre-create session
        session_id = real_session_manager.create_session(recipe, temp_dir)
        iteration_count = 0

        async def mock_spawn(*args, **kwargs):
            nonlocal iteration_count
            iteration_count += 1
            if iteration_count == 2:
                # Cancel after second iteration
                real_session_manager.request_cancellation(session_id, temp_dir, immediate=False)
            return f"result-{iteration_count}"

        mock_coordinator.get_capability.return_value = AsyncMock(side_effect=mock_spawn)
        executor = RecipeExecutor(mock_coordinator, real_session_manager)

        with pytest.raises(CancellationRequestedError):
            await executor.execute_recipe(recipe, {}, temp_dir, session_id=session_id)

        # Should have processed 2 items before cancellation took effect
        assert iteration_count == 2

    @pytest.mark.asyncio
    async def test_cancellation_saves_state_for_resumption(
        self, mock_coordinator, real_session_manager, temp_dir: Path
    ):
        """Cancellation saves session state including cancellation info."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-1", agent="a", prompt="First", output="r1"),
                Step(id="step-2", agent="a", prompt="Second", output="r2"),
            ],
        )

        # Create session and request immediate cancellation
        session_id = real_session_manager.create_session(recipe, temp_dir)
        real_session_manager.request_cancellation(session_id, temp_dir, immediate=True)

        mock_coordinator.get_capability.return_value = AsyncMock(return_value="result")
        executor = RecipeExecutor(mock_coordinator, real_session_manager)

        with pytest.raises(CancellationRequestedError):
            await executor.execute_recipe(recipe, {}, temp_dir, session_id=session_id)

        # Verify state was saved with cancellation info
        state = real_session_manager.load_state(session_id, temp_dir)
        assert state["cancellation_status"] == CancellationStatus.CANCELLED.value
        assert "cancelled_at_step" in state


class TestCoordinatorCancellationIntegration:
    """Tests for integration with coordinator's CancellationToken."""

    @pytest.fixture
    def mock_coordinator_with_cancellation(self):
        """Create a mock coordinator with cancellation token."""
        coordinator = MagicMock()
        coordinator.session = MagicMock()
        coordinator.config = {"agents": {}}
        coordinator.get_capability.return_value = AsyncMock(return_value="result")

        # Mock cancellation token
        cancellation = MagicMock()
        cancellation.is_cancelled = False
        cancellation.is_immediate = False
        coordinator.cancellation = cancellation

        return coordinator

    @pytest.fixture
    def mock_session_manager(self):
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

    @pytest.mark.asyncio
    async def test_coordinator_cancellation_propagates_to_session(
        self, mock_coordinator_with_cancellation, mock_session_manager, temp_dir: Path
    ):
        """Coordinator cancellation token triggers session cancellation."""
        call_count = 0

        async def mock_spawn(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate Ctrl+C by setting coordinator cancellation
                mock_coordinator_with_cancellation.cancellation.is_cancelled = True
                mock_coordinator_with_cancellation.cancellation.is_immediate = False
            return f"result-{call_count}"

        mock_coordinator_with_cancellation.get_capability.return_value = AsyncMock(
            side_effect=mock_spawn
        )

        # After coordinator cancellation is set, session manager should report it
        def check_cancellation(*args, **kwargs):
            return mock_coordinator_with_cancellation.cancellation.is_cancelled

        mock_session_manager.is_cancellation_requested.side_effect = check_cancellation

        executor = RecipeExecutor(mock_coordinator_with_cancellation, mock_session_manager)

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-1", agent="a", prompt="First"),
                Step(id="step-2", agent="a", prompt="Second"),
            ],
        )

        with pytest.raises(CancellationRequestedError):
            await executor.execute_recipe(recipe, {}, temp_dir)

        # Verify request_cancellation was called to propagate coordinator cancellation
        mock_session_manager.request_cancellation.assert_called()


class TestNestedRecipeCancellation:
    """Tests for cancellation propagation to nested recipes."""

    @pytest.fixture
    def mock_coordinator(self):
        """Create a mock coordinator."""
        coordinator = MagicMock()
        coordinator.session = MagicMock()
        coordinator.config = {"agents": {}}
        coordinator.get_capability.return_value = AsyncMock(return_value="result")
        coordinator.cancellation = None
        return coordinator

    @pytest.fixture
    def real_session_manager(self, temp_dir: Path):
        """Create a real session manager."""
        return SessionManager(base_dir=temp_dir, auto_cleanup_days=7)

    @pytest.mark.asyncio
    async def test_nested_recipe_inherits_parent_cancellation(
        self, mock_coordinator, real_session_manager, temp_dir: Path
    ):
        """Nested recipe inherits cancellation from parent session."""
        # Create sub-recipe file
        sub_recipe_content = """
name: sub-recipe
description: A sub-recipe
version: 1.0.0
steps:
  - id: sub-step-1
    agent: test-agent
    prompt: "Sub step 1"
"""
        sub_recipe_path = temp_dir / "sub-recipe.yaml"
        sub_recipe_path.write_text(sub_recipe_content)

        parent_recipe = Recipe(
            name="parent",
            description="Parent recipe",
            version="1.0.0",
            steps=[
                Step(id="call-sub", type="recipe", recipe="sub-recipe.yaml"),
                Step(id="after-sub", agent="a", prompt="After sub"),
            ],
        )

        # Create session and request cancellation before execution
        session_id = real_session_manager.create_session(parent_recipe, temp_dir)
        real_session_manager.request_cancellation(session_id, temp_dir, immediate=True)

        mock_coordinator.get_capability.return_value = AsyncMock(return_value="result")
        executor = RecipeExecutor(mock_coordinator, real_session_manager)

        with pytest.raises(CancellationRequestedError):
            await executor.execute_recipe(
                parent_recipe, {}, temp_dir,
                session_id=session_id,
                recipe_path=temp_dir / "parent.yaml"
            )

        # Spawn should never be called since cancellation was pre-requested
        mock_coordinator.get_capability.return_value.assert_not_called()
