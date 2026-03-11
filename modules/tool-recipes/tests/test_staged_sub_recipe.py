"""Tests for staged sub-recipe support.

Tests cover:
- ApprovalGatePausedError.resume_session_id field for tracking child sessions
- _execute_recipe_step child session management (save/resume/cleanup)
- Flat execution loop mirrors child ApprovalGatePausedError to parent
- Staged execution loop mirrors child ApprovalGatePausedError to parent
- _forward_approval and _forward_denial helpers on RecipesTool
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from amplifier_module_tool_recipes.executor import ApprovalGatePausedError
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.executor import RecursionState
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step
from amplifier_module_tool_recipes.session import ApprovalStatus


# =============================================================================
# ApprovalGatePausedError resume_session_id Tests
# =============================================================================


class TestApprovalGatePausedErrorResumeSessionId:
    """Tests for the resume_session_id field on ApprovalGatePausedError."""

    def test_resume_session_id_defaults_to_none(self):
        """resume_session_id should default to None when not provided."""
        error = ApprovalGatePausedError(
            session_id="x",
            stage_name="y",
            approval_prompt="z",
        )
        assert error.resume_session_id is None

    def test_resume_session_id_preserved_when_set(self):
        """resume_session_id should preserve the value when explicitly set."""
        error = ApprovalGatePausedError(
            session_id="parent-session",
            stage_name="planning",
            approval_prompt="Approve?",
            resume_session_id="child-session",
        )
        assert error.resume_session_id == "child-session"

    def test_existing_fields_unchanged_when_resume_session_id_provided(self):
        """Existing fields remain unchanged when resume_session_id is provided."""
        error = ApprovalGatePausedError(
            session_id="parent-123",
            stage_name="execution",
            approval_prompt="Do you approve the execution?",
            resume_session_id="child-456",
        )
        assert error.session_id == "parent-123"
        assert error.stage_name == "execution"
        assert error.approval_prompt == "Do you approve the execution?"
        assert error.resume_session_id == "child-456"


# =============================================================================
# _execute_recipe_step Child Session Management Tests
# =============================================================================


def _make_executor():
    """Create a RecipeExecutor with minimal mocks."""
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock()

    session_manager = MagicMock()
    session_manager.create_session.return_value = "test-session-id"
    session_manager.is_cancellation_requested.return_value = False
    session_manager.is_immediate_cancellation.return_value = False

    return RecipeExecutor(coordinator, session_manager)


def _make_sub_recipe_file(tmp_path: Path) -> Path:
    """Create a minimal sub-recipe YAML file."""
    content = """\
name: sub-recipe
description: A sub-recipe for testing
version: "1.0.0"

steps: []
"""
    sub_recipe_path = tmp_path / "sub.yaml"
    sub_recipe_path.write_text(content)
    return sub_recipe_path


class TestExecuteRecipeStepChildSession:
    """Tests for child session management in _execute_recipe_step."""

    @pytest.mark.asyncio
    async def test_resume_saved_child_session(self):
        """Saved child session ID is passed to execute_recipe and cleaned up on success."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sub_recipe_path = _make_sub_recipe_file(tmp_path)

            executor = _make_executor()

            # Patch execute_recipe to return a successful result
            executor.execute_recipe = AsyncMock(return_value={"result": "done"})

            step = Step(
                id="call-sub",
                type="recipe",
                recipe=str(sub_recipe_path),
            )
            context = {"_child_session_call-sub": "saved-child-session-id"}
            recursion_state = RecursionState()

            await executor._execute_recipe_step(
                step=step,
                context=context,
                project_path=tmp_path,
                recursion_state=recursion_state,
                parent_recipe_path=None,
            )

            # The saved session ID should have been passed as session_id
            call_kwargs = executor.execute_recipe.call_args[1]
            assert call_kwargs["session_id"] == "saved-child-session-id"

            # The child session key should be cleaned up after success
            assert "_child_session_call-sub" not in context

    @pytest.mark.asyncio
    async def test_saves_child_session_id_on_approval_pause(self):
        """Child session ID is saved in context when ApprovalGatePausedError is raised."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sub_recipe_path = _make_sub_recipe_file(tmp_path)

            executor = _make_executor()

            # Patch execute_recipe to raise ApprovalGatePausedError
            child_error = ApprovalGatePausedError(
                session_id="child-paused-session",
                stage_name="review",
                approval_prompt="Approve?",
            )
            executor.execute_recipe = AsyncMock(side_effect=child_error)

            step = Step(
                id="call-sub",
                type="recipe",
                recipe=str(sub_recipe_path),
            )
            context: dict = {}
            recursion_state = RecursionState()

            with pytest.raises(ApprovalGatePausedError):
                await executor._execute_recipe_step(
                    step=step,
                    context=context,
                    project_path=tmp_path,
                    recursion_state=recursion_state,
                    parent_recipe_path=None,
                )

            # The child's session ID should be saved in context
            assert context.get("_child_session_call-sub") == "child-paused-session"


# =============================================================================
# Flat Loop Approval Mirroring Tests
# =============================================================================


def _make_flat_recipe_with_recipe_step(tmp_path: Path, sub_recipe_path: Path) -> Path:
    """Create a flat recipe YAML with a single recipe-type step."""
    content = f"""\
name: parent-recipe
description: A parent recipe for testing approval mirroring
version: "1.0.0"

steps:
  - id: call-sub
    type: recipe
    recipe: {sub_recipe_path}
"""
    parent_recipe_path = tmp_path / "parent.yaml"
    parent_recipe_path.write_text(content)
    return parent_recipe_path


class TestFlatLoopApprovalMirroring:
    """Tests that the flat execution loop mirrors child ApprovalGatePausedError to parent."""

    @pytest.mark.asyncio
    async def test_flat_loop_catches_child_ape_and_mirrors_approval(self):
        """When a recipe step raises ApprovalGatePausedError, the flat loop:
        - Saves parent state at current step index (not advanced)
        - Calls set_pending_approval on the parent session
        - Saves pending_child_approval metadata in state
        - Re-raises a new APE with the parent's session_id
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sub_recipe_path = _make_sub_recipe_file(tmp_path)
            parent_recipe_path = _make_flat_recipe_with_recipe_step(
                tmp_path, sub_recipe_path
            )

            executor = _make_executor()

            parent_session_id = "parent-session-id"
            executor.session_manager.create_session.return_value = parent_session_id

            # Track state saves so we can inspect what was saved
            saved_states = []
            executor.session_manager.save_state.side_effect = lambda sid, pp, state: (
                saved_states.append(dict(state))
            )

            # Mock _execute_recipe_step to raise child APE
            child_error = ApprovalGatePausedError(
                session_id="child-session-id",
                stage_name="review",
                approval_prompt="Approve the review?",
            )
            executor._execute_recipe_step = AsyncMock(side_effect=child_error)

            recipe = Recipe.from_yaml(parent_recipe_path)

            with pytest.raises(ApprovalGatePausedError) as exc_info:
                await executor.execute_recipe(
                    recipe=recipe,
                    context_vars={},
                    project_path=tmp_path,
                    recipe_path=parent_recipe_path,
                )

            raised_error = exc_info.value

            # 1. Re-raised APE has the parent's session_id (not the child's)
            assert raised_error.session_id == parent_session_id

            # 2. Re-raised APE has the child's session_id as resume_session_id
            assert raised_error.resume_session_id == "child-session-id"

            # 3. Re-raised APE preserves the child's stage name and prompt
            assert raised_error.stage_name == "review"
            assert raised_error.approval_prompt == "Approve the review?"

            # 4. set_pending_approval was called on the parent session
            executor.session_manager.set_pending_approval.assert_called_once_with(
                session_id=parent_session_id,
                project_path=tmp_path,
                stage_name="review",
                prompt="Approve the review?",
                timeout=0,
                default="deny",
            )

            # 5. State was saved with current_step_index=0 (not advanced to 1)
            approval_states = [s for s in saved_states if "pending_child_approval" in s]
            assert len(approval_states) >= 1, (
                "Expected at least one save_state call with pending_child_approval"
            )
            approval_state = approval_states[0]
            assert approval_state["current_step_index"] == 0

            # 6. pending_child_approval metadata is saved in parent state
            pca = approval_state["pending_child_approval"]
            assert pca["child_session_id"] == "child-session-id"
            assert pca["child_stage_name"] == "review"
            assert pca["parent_step_id"] == "call-sub"


# =============================================================================
# Flat Resume With Pending Child Approval Tests
# =============================================================================


class TestFlatResumeWithPendingChildApproval:
    """Tests for the flat resume path handling pending_child_approval in state."""

    @pytest.mark.asyncio
    async def test_approved_clears_pending_injects_message_removes_metadata(self):
        """When resuming a flat recipe with pending_child_approval and APPROVED status:
        - Pending approval is cleared
        - _approval_message is injected into context
        - pending_child_approval metadata is removed from state
        - State is saved without pending_child_approval
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sub_recipe_path = _make_sub_recipe_file(tmp_path)
            parent_recipe_path = _make_flat_recipe_with_recipe_step(
                tmp_path, sub_recipe_path
            )

            executor = _make_executor()
            parent_session_id = "parent-session-id"

            # Initial state has pending_child_approval
            initial_state = {
                "session_id": parent_session_id,
                "recipe_name": "parent-recipe",
                "recipe_version": "1.0.0",
                "started": "2024-01-01T00:00:00",
                "current_step_index": 0,
                "context": {"_child_session_call-sub": "child-session-id"},
                "completed_steps": [],
                "project_path": str(tmp_path.resolve()),
                "pending_child_approval": {
                    "child_session_id": "child-session-id",
                    "child_stage_name": "review",
                    "parent_step_id": "call-sub",
                },
            }

            # State returned by second load_state (after clearing approval)
            state_after_clear = {
                "session_id": parent_session_id,
                "recipe_name": "parent-recipe",
                "recipe_version": "1.0.0",
                "started": "2024-01-01T00:00:00",
                "current_step_index": 0,
                "context": {"_child_session_call-sub": "child-session-id"},
                "completed_steps": [],
                "project_path": str(tmp_path.resolve()),
                "_approval_message": "approved with message",
                "pending_child_approval": {
                    "child_session_id": "child-session-id",
                    "child_stage_name": "review",
                    "parent_step_id": "call-sub",
                },
            }

            executor.session_manager.load_state.side_effect = [
                initial_state,  # First load during flat state loading
                state_after_clear,  # Second load after clearing approval
            ]

            executor.session_manager.get_pending_approval.return_value = {
                "stage_name": "review",
                "approval_prompt": "Approve the review?",
            }
            executor.session_manager.get_stage_approval_status.return_value = (
                ApprovalStatus.APPROVED
            )
            executor.session_manager.check_approval_timeout.return_value = None

            # Track state saves
            saved_states = []
            executor.session_manager.save_state.side_effect = lambda sid, pp, state: (
                saved_states.append(dict(state))
            )

            # Mock _execute_recipe_step to succeed immediately
            executor._execute_recipe_step = AsyncMock(return_value="done")

            recipe = Recipe.from_yaml(parent_recipe_path)

            await executor.execute_recipe(
                recipe=recipe,
                context_vars={},
                project_path=tmp_path,
                session_id=parent_session_id,
                recipe_path=parent_recipe_path,
            )

            # 1. clear_pending_approval was called
            executor.session_manager.clear_pending_approval.assert_called_once_with(
                parent_session_id, tmp_path
            )

            # 2. pending_child_approval was removed from state in a save call
            cleanup_saves = [
                s for s in saved_states if "pending_child_approval" not in s
            ]
            assert len(cleanup_saves) >= 1, (
                "Expected at least one save_state call without pending_child_approval"
            )

            # 3. _approval_message was injected into context used during execution
            # Verify via the context passed to _execute_recipe_step
            call_args = executor._execute_recipe_step.call_args
            ctx_used = call_args[0][1] if call_args[0] else call_args[1]["context"]
            assert ctx_used.get("_approval_message") == "approved with message"


# =============================================================================
# Staged Loop Approval Mirroring Tests
# =============================================================================


def _make_staged_recipe_with_recipe_step(tmp_path: Path, sub_recipe_path: Path) -> Path:
    """Create a staged recipe YAML with a single recipe-type step in a stage."""
    content = f"""\
name: parent-staged-recipe
description: A staged parent recipe for testing approval mirroring
version: "1.0.0"

stages:
  - name: planning
    steps:
      - id: call-sub
        type: recipe
        recipe: {sub_recipe_path}
"""
    parent_recipe_path = tmp_path / "parent_staged.yaml"
    parent_recipe_path.write_text(content)
    return parent_recipe_path


class TestStagedLoopApprovalMirroring:
    """Tests that the staged execution loop mirrors child ApprovalGatePausedError to parent."""

    @pytest.mark.asyncio
    async def test_staged_loop_catches_child_ape_and_mirrors_approval(self):
        """When a staged recipe's recipe step raises ApprovalGatePausedError, the staged loop:
        - Saves parent staged state at current step (not advanced)
        - Creates compound stage name (parent-stage/child-gate)
        - Calls set_pending_approval on the parent session with compound stage name
        - Saves pending_child_approval metadata in state
        - Re-raises a new APE with the parent's session_id and compound stage name
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sub_recipe_path = _make_sub_recipe_file(tmp_path)
            parent_recipe_path = _make_staged_recipe_with_recipe_step(
                tmp_path, sub_recipe_path
            )

            executor = _make_executor()

            parent_session_id = "parent-staged-session-id"
            executor.session_manager.create_session.return_value = parent_session_id

            # Track state saves so we can inspect what was saved
            saved_states = []
            executor.session_manager.save_state.side_effect = lambda sid, pp, state: (
                saved_states.append(dict(state))
            )
            # Wire load_state to return a copy of the last saved state so the
            # implementation can load-augment-save without getting a MagicMock back
            executor.session_manager.load_state.side_effect = lambda sid, pp: (
                dict(saved_states[-1]) if saved_states else {}
            )

            # Mock _execute_recipe_step to raise child APE
            child_error = ApprovalGatePausedError(
                session_id="child-session-id",
                stage_name="child-gate",
                approval_prompt="Approve child?",
            )
            executor._execute_recipe_step = AsyncMock(side_effect=child_error)

            recipe = Recipe.from_yaml(parent_recipe_path)

            with pytest.raises(ApprovalGatePausedError) as exc_info:
                await executor.execute_recipe(
                    recipe=recipe,
                    context_vars={},
                    project_path=tmp_path,
                    recipe_path=parent_recipe_path,
                )

            raised_error = exc_info.value

            # 1. Re-raised APE has the parent's session_id (not the child's)
            assert raised_error.session_id == parent_session_id

            # 2. Re-raised APE has the child's session_id as resume_session_id
            assert raised_error.resume_session_id == "child-session-id"

            # 3. Compound stage name includes both parent stage and child stage
            assert raised_error.stage_name == "planning/child-gate"

            # 4. Re-raised APE preserves the child's approval prompt
            assert raised_error.approval_prompt == "Approve child?"

            # 5. set_pending_approval was called on the parent session with compound stage name
            executor.session_manager.set_pending_approval.assert_called_once_with(
                session_id=parent_session_id,
                project_path=tmp_path,
                stage_name="planning/child-gate",
                prompt="Approve child?",
                timeout=0,
                default="deny",
            )

            # 6. State was saved with pending_child_approval metadata
            approval_states = [s for s in saved_states if "pending_child_approval" in s]
            assert len(approval_states) >= 1, (
                "Expected at least one save_state call with pending_child_approval"
            )
            approval_state = approval_states[0]
            pca = approval_state["pending_child_approval"]
            assert pca["child_session_id"] == "child-session-id"
            assert pca["child_stage_name"] == "child-gate"
            assert pca["parent_step_id"] == "call-sub"

            # 7. State was saved at current step (not advanced)
            assert approval_state.get("current_stage_index") == 0
            assert approval_state.get("current_step_in_stage") == 0


# =============================================================================
# Approval Forwarding Helper Tests
# =============================================================================


def _make_recipes_tool():
    """Create a RecipesTool with minimal mocks."""
    from amplifier_module_tool_recipes import RecipesTool

    coordinator = MagicMock()
    coordinator.get_capability.return_value = None

    session_manager = MagicMock()

    executor = MagicMock()

    return RecipesTool(executor, session_manager, coordinator, {})


class TestApprovalForwarding:
    """Tests for _forward_approval and _forward_denial helper methods on RecipesTool."""

    def test_forward_approval_returns_early_when_no_pending_child_approval(self):
        """_forward_approval is a no-op when no pending_child_approval exists."""
        import tempfile

        tool = _make_recipes_tool()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # State without pending_child_approval
            tool.session_manager.load_state.return_value = {
                "session_id": "parent-session",
            }

            tool._forward_approval("parent-session", tmp_path)

            # Should not have attempted to approve any child session
            tool.session_manager.set_stage_approval_status.assert_not_called()

    def test_forward_approval_sets_child_approved_and_propagates_message(self):
        """_forward_approval sets APPROVED on child stage and propagates _approval_message."""
        import tempfile

        tool = _make_recipes_tool()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            parent_state = {
                "session_id": "parent-session",
                "pending_child_approval": {
                    "child_session_id": "child-session",
                    "child_stage_name": "review",
                    "parent_step_id": "call-sub",
                },
            }
            # Child state (no further pending_child_approval for recursion)
            child_state = {
                "session_id": "child-session",
            }

            tool.session_manager.load_state.side_effect = [
                parent_state,  # First load: parent state
                child_state,  # Second load: child state for message propagation
            ]

            tool._forward_approval("parent-session", tmp_path, message="approved!")

            # set_stage_approval_status called on child session with APPROVED
            tool.session_manager.set_stage_approval_status.assert_called_once_with(
                session_id="child-session",
                project_path=tmp_path,
                stage_name="review",
                status=ApprovalStatus.APPROVED,
                reason="Approved by user",
            )

            # save_state called on child with _approval_message
            save_calls = tool.session_manager.save_state.call_args_list
            child_saves = [c for c in save_calls if c[0][0] == "child-session"]
            assert len(child_saves) >= 1, "Expected save_state called for child session"
            assert any(
                c[0][2].get("_approval_message") == "approved!" for c in child_saves
            ), "_approval_message not propagated to child state"

            # Parent's pending_child_approval metadata should be cleared
            parent_saves = [c for c in save_calls if c[0][0] == "parent-session"]
            assert len(parent_saves) >= 1, (
                "Expected save_state called for parent session"
            )
            assert all("pending_child_approval" not in c[0][2] for c in parent_saves), (
                "pending_child_approval not cleared from parent state"
            )

    def test_forward_denial_returns_early_when_no_pending_child_approval(self):
        """_forward_denial is a no-op when no pending_child_approval exists."""
        import tempfile

        tool = _make_recipes_tool()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # State without pending_child_approval
            tool.session_manager.load_state.return_value = {
                "session_id": "parent-session",
            }

            tool._forward_denial("parent-session", tmp_path)

            # Should not have attempted to deny any child session
            tool.session_manager.set_stage_approval_status.assert_not_called()

    def test_forward_denial_sets_child_denied_and_clears_pending(self):
        """_forward_denial sets DENIED on child stage and clears child's pending approval."""
        import tempfile

        tool = _make_recipes_tool()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            parent_state = {
                "session_id": "parent-session",
                "pending_child_approval": {
                    "child_session_id": "child-session",
                    "child_stage_name": "review",
                    "parent_step_id": "call-sub",
                },
            }
            # Child state (no further pending_child_approval for recursion)
            child_state = {
                "session_id": "child-session",
            }

            tool.session_manager.load_state.side_effect = [
                parent_state,  # First load: parent state
                child_state,  # Second load: child state for recursion check
            ]

            tool._forward_denial("parent-session", tmp_path, reason="User denied")

            # set_stage_approval_status called on child session with DENIED
            tool.session_manager.set_stage_approval_status.assert_called_once_with(
                session_id="child-session",
                project_path=tmp_path,
                stage_name="review",
                status=ApprovalStatus.DENIED,
                reason="User denied",
            )

            # clear_pending_approval called on child session
            tool.session_manager.clear_pending_approval.assert_called_once_with(
                "child-session", tmp_path
            )

            # Parent's pending_child_approval metadata should be cleared
            save_calls = tool.session_manager.save_state.call_args_list
            parent_saves = [c for c in save_calls if c[0][0] == "parent-session"]
            assert len(parent_saves) >= 1, (
                "Expected save_state called for parent session"
            )
            assert all("pending_child_approval" not in c[0][2] for c in parent_saves), (
                "pending_child_approval not cleared from parent state"
            )


# =============================================================================
# _approve_stage / _deny_stage Forwarding Wiring Tests
# =============================================================================


class TestApproveStageForwardsToChild:
    """Tests that _approve_stage and _deny_stage wire into the forwarding helpers."""

    @pytest.mark.asyncio
    async def test_approve_stage_calls_forward_approval_when_pending_child_approval(
        self,
    ):
        """When _approve_stage is called on a session with pending_child_approval,
        _forward_approval is called with correct session_id, project_path, and message.
        """
        import tempfile
        from unittest.mock import patch

        tool = _make_recipes_tool()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Mock _get_working_dir to return tmp_path
            tool.coordinator.get_capability.return_value = str(tmp_path)

            # Session exists
            tool.session_manager.session_exists.return_value = True

            # Pending approval exists for "planning" stage
            tool.session_manager.get_pending_approval.return_value = {
                "stage_name": "planning",
                "approval_prompt": "Approve?",
            }

            # State includes pending_child_approval
            tool.session_manager.load_state.return_value = {
                "session_id": "parent-session",
                "_approval_message": "",
                "pending_child_approval": {
                    "child_session_id": "child-session",
                    "child_stage_name": "review",
                    "parent_step_id": "call-sub",
                },
            }

            with patch.object(tool, "_forward_approval") as mock_forward:
                result = await tool._approve_stage(
                    {
                        "session_id": "parent-session",
                        "stage_name": "planning",
                        "message": "go ahead",
                    }
                )

            assert result.success is True
            mock_forward.assert_called_once_with(
                session_id="parent-session",
                project_path=tmp_path,
                message="go ahead",
            )

    @pytest.mark.asyncio
    async def test_deny_stage_calls_forward_denial_when_pending_child_approval(self):
        """When _deny_stage is called on a session with pending_child_approval,
        _forward_denial is called with correct session_id, project_path, and reason.
        """
        import tempfile
        from unittest.mock import patch

        tool = _make_recipes_tool()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Mock _get_working_dir to return tmp_path
            tool.coordinator.get_capability.return_value = str(tmp_path)

            # Session exists
            tool.session_manager.session_exists.return_value = True

            # Pending approval exists for "planning" stage
            tool.session_manager.get_pending_approval.return_value = {
                "stage_name": "planning",
                "approval_prompt": "Approve?",
            }

            # State includes pending_child_approval
            tool.session_manager.load_state.return_value = {
                "session_id": "parent-session",
                "pending_child_approval": {
                    "child_session_id": "child-session",
                    "child_stage_name": "review",
                    "parent_step_id": "call-sub",
                },
            }

            with patch.object(tool, "_forward_denial") as mock_forward:
                result = await tool._deny_stage(
                    {
                        "session_id": "parent-session",
                        "stage_name": "planning",
                        "reason": "No thanks",
                    }
                )

            assert result.success is True
            mock_forward.assert_called_once_with(
                session_id="parent-session",
                project_path=tmp_path,
                reason="No thanks",
            )
