"""Tests for approval gates functionality (Phase 3).

Tests cover:
- ApprovalConfig model validation
- Stage model validation
- Recipe staged mode parsing and validation
- SessionManager approval methods
- ApprovalGatePausedError exception
"""

import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from amplifier_module_tool_recipes import RecipesTool
from amplifier_module_tool_recipes.executor import ApprovalGatePausedError
from amplifier_module_tool_recipes.models import ApprovalConfig
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Stage
from amplifier_module_tool_recipes.models import Step
from amplifier_module_tool_recipes.session import ApprovalStatus
from amplifier_module_tool_recipes.session import SessionManager

# =============================================================================
# ApprovalConfig Model Tests
# =============================================================================


class TestApprovalConfig:
    """Tests for ApprovalConfig dataclass."""

    def test_default_values(self):
        """Default values should be sensible."""
        config = ApprovalConfig()
        assert config.required is False
        assert config.prompt == ""
        assert config.timeout == 0  # Default: wait forever (no timeout)
        assert config.default == "deny"

    def test_valid_configuration(self):
        """Valid configuration should have no errors."""
        config = ApprovalConfig(
            required=True,
            prompt="Do you want to proceed?",
            timeout=600,
            default="deny",
        )
        errors = config.validate()
        assert errors == []

    def test_valid_approve_default(self):
        """'approve' is a valid default value."""
        config = ApprovalConfig(
            required=True,
            prompt="Auto-approve on timeout?",
            timeout=60,
            default="approve",
        )
        errors = config.validate()
        assert errors == []

    def test_negative_timeout_invalid(self):
        """Negative timeout should fail validation."""
        config = ApprovalConfig(timeout=-1)
        errors = config.validate()
        assert any("timeout" in e and "non-negative" in e for e in errors)

    def test_zero_timeout_valid(self):
        """Zero timeout (wait forever) should be valid."""
        config = ApprovalConfig(timeout=0)
        errors = config.validate()
        assert not any("timeout" in e for e in errors)

    def test_invalid_default_value(self):
        """Invalid default value should fail validation."""
        config = ApprovalConfig(default="invalid")  # type: ignore
        errors = config.validate()
        assert any("default" in e for e in errors)

    def test_required_without_prompt_invalid(self):
        """Required approval without prompt should fail validation."""
        config = ApprovalConfig(required=True, prompt="")
        errors = config.validate()
        assert any("prompt is required" in e for e in errors)

    def test_not_required_without_prompt_valid(self):
        """Non-required approval without prompt is valid."""
        config = ApprovalConfig(required=False, prompt="")
        errors = config.validate()
        assert errors == []


# =============================================================================
# Stage Model Tests
# =============================================================================


class TestStage:
    """Tests for Stage dataclass."""

    def test_valid_stage(self):
        """Valid stage should pass validation."""
        stage = Stage(
            name="planning",
            steps=[Step(id="analyze", agent="planner", prompt="Plan the work")],
        )
        errors = stage.validate()
        assert errors == []

    def test_stage_with_approval(self):
        """Stage with approval config should validate both."""
        stage = Stage(
            name="implementation",
            steps=[Step(id="code", agent="developer", prompt="Write code")],
            approval=ApprovalConfig(
                required=True, prompt="Approve implementation plan?"
            ),
        )
        errors = stage.validate()
        assert errors == []

    def test_missing_name(self):
        """Stage without name should fail validation."""
        stage = Stage(
            name="",
            steps=[Step(id="s1", agent="a", prompt="p")],
        )
        errors = stage.validate()
        assert any("missing required field: name" in e for e in errors)

    def test_invalid_name_characters(self):
        """Stage name with invalid characters should fail."""
        stage = Stage(
            name="stage@#$",
            steps=[Step(id="s1", agent="a", prompt="p")],
        )
        errors = stage.validate()
        assert any("alphanumeric" in e for e in errors)

    def test_valid_name_with_hyphens_underscores(self):
        """Stage name with hyphens and underscores should be valid."""
        stage = Stage(
            name="my-stage_name",
            steps=[Step(id="s1", agent="a", prompt="p")],
        )
        errors = stage.validate()
        assert errors == []

    def test_name_with_spaces_valid(self):
        """Stage name with spaces should be valid."""
        stage = Stage(
            name="Planning Phase",
            steps=[Step(id="s1", agent="a", prompt="p")],
        )
        errors = stage.validate()
        assert not any("alphanumeric" in e for e in errors)

    def test_empty_steps(self):
        """Stage without steps should fail validation."""
        stage = Stage(name="empty", steps=[])
        errors = stage.validate()
        assert any("at least one step" in e for e in errors)

    def test_duplicate_step_ids_in_stage(self):
        """Duplicate step IDs within stage should fail."""
        stage = Stage(
            name="test",
            steps=[
                Step(id="same", agent="a", prompt="p1"),
                Step(id="same", agent="b", prompt="p2"),
            ],
        )
        errors = stage.validate()
        assert any("duplicate step IDs" in e for e in errors)

    def test_stage_validates_steps(self):
        """Stage should validate its steps."""
        stage = Stage(
            name="test",
            steps=[Step(id="invalid", agent="", prompt="")],  # Missing agent and prompt
        )
        errors = stage.validate()
        assert any("agent" in e for e in errors)

    def test_stage_validates_approval_config(self):
        """Stage should validate its approval config."""
        stage = Stage(
            name="test",
            steps=[Step(id="s1", agent="a", prompt="p")],
            approval=ApprovalConfig(
                required=True, prompt=""
            ),  # Invalid: required but no prompt
        )
        errors = stage.validate()
        assert any("prompt is required" in e for e in errors)


# =============================================================================
# Recipe Staged Mode Tests
# =============================================================================


class TestRecipeStagedMode:
    """Tests for Recipe staged mode parsing and validation."""

    def test_is_staged_property(self):
        """is_staged should return True for staged recipes."""
        staged_recipe = Recipe(
            name="staged",
            description="test",
            version="1.0.0",
            stages=[
                Stage(name="s1", steps=[Step(id="step1", agent="a", prompt="p")]),
            ],
        )
        assert staged_recipe.is_staged is True

        flat_recipe = Recipe(
            name="flat",
            description="test",
            version="1.0.0",
            steps=[Step(id="step1", agent="a", prompt="p")],
        )
        assert flat_recipe.is_staged is False

    def test_get_all_steps_staged(self):
        """get_all_steps should return all steps from all stages."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            stages=[
                Stage(name="s1", steps=[Step(id="a", agent="a", prompt="p")]),
                Stage(name="s2", steps=[Step(id="b", agent="b", prompt="p")]),
            ],
        )
        steps = recipe.get_all_steps()
        assert len(steps) == 2
        assert [s.id for s in steps] == ["a", "b"]

    def test_get_stage(self):
        """get_stage should find stage by name."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            stages=[
                Stage(name="planning", steps=[Step(id="a", agent="a", prompt="p")]),
                Stage(name="execution", steps=[Step(id="b", agent="b", prompt="p")]),
            ],
        )
        stage = recipe.get_stage("execution")
        assert stage is not None
        assert stage.name == "execution"

        assert recipe.get_stage("nonexistent") is None

    def test_validate_staged_mode(self):
        """Staged mode validation should work."""
        recipe = Recipe(
            name="valid-staged",
            description="A valid staged recipe",
            version="1.0.0",
            stages=[
                Stage(
                    name="planning",
                    steps=[Step(id="plan", agent="planner", prompt="Create plan")],
                    approval=ApprovalConfig(required=True, prompt="Approve plan?"),
                ),
                Stage(
                    name="execution",
                    steps=[Step(id="execute", agent="executor", prompt="Execute plan")],
                ),
            ],
        )
        errors = recipe.validate()
        assert errors == []

    def test_duplicate_stage_names(self):
        """Duplicate stage names should fail validation."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            stages=[
                Stage(name="same", steps=[Step(id="a", agent="a", prompt="p")]),
                Stage(name="same", steps=[Step(id="b", agent="b", prompt="p")]),
            ],
        )
        errors = recipe.validate()
        assert any("Duplicate stage names" in e for e in errors)

    def test_duplicate_step_ids_across_stages(self):
        """Duplicate step IDs across stages should fail."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            stages=[
                Stage(name="s1", steps=[Step(id="same", agent="a", prompt="p")]),
                Stage(name="s2", steps=[Step(id="same", agent="b", prompt="p")]),
            ],
        )
        errors = recipe.validate()
        assert any("Duplicate step IDs across stages" in e for e in errors)

    def test_from_yaml_staged_recipe(self, temp_dir: Path):
        """Recipe.from_yaml should parse staged recipes."""
        yaml_content = """
name: staged-recipe
description: A recipe with stages
version: 1.0.0

stages:
  - name: planning
    steps:
      - id: analyze
        agent: analyzer
        prompt: Analyze the task
    approval:
      required: true
      prompt: Approve the analysis?
      timeout: 300

  - name: execution
    steps:
      - id: execute
        agent: executor
        prompt: Execute the plan
"""
        recipe_file = temp_dir / "staged.yaml"
        recipe_file.write_text(yaml_content)

        recipe = Recipe.from_yaml(recipe_file)
        assert recipe.is_staged
        assert len(recipe.stages) == 2
        assert recipe.stages[0].name == "planning"
        assert recipe.stages[0].approval is not None
        assert recipe.stages[0].approval.required is True
        assert recipe.stages[0].approval.timeout == 300
        assert recipe.stages[1].name == "execution"
        assert recipe.stages[1].approval is None

    def test_from_yaml_rejects_both_steps_and_stages(self, temp_dir: Path):
        """Recipe cannot have both steps and stages."""
        yaml_content = """
name: invalid
description: Has both steps and stages
version: 1.0.0

steps:
  - id: s1
    agent: a
    prompt: p

stages:
  - name: stage1
    steps:
      - id: s2
        agent: b
        prompt: p
"""
        recipe_file = temp_dir / "invalid.yaml"
        recipe_file.write_text(yaml_content)

        with pytest.raises(ValueError, match="cannot have both"):
            Recipe.from_yaml(recipe_file)

    def test_must_have_steps_or_stages(self):
        """Recipe must have at least one step or stage."""
        recipe = Recipe(
            name="empty",
            description="test",
            version="1.0.0",
            steps=[],
            stages=[],
        )
        errors = recipe.validate()
        assert any("at least one step or stage" in e for e in errors)


# =============================================================================
# SessionManager Approval Methods Tests
# =============================================================================


class TestSessionManagerApprovals:
    """Tests for SessionManager approval tracking methods."""

    @pytest.fixture
    def session_manager(self, temp_dir: Path) -> SessionManager:
        """Create session manager with temp directory."""
        return SessionManager(base_dir=temp_dir, auto_cleanup_days=7)

    @pytest.fixture
    def sample_recipe(self) -> Recipe:
        """Create a sample staged recipe."""
        return Recipe(
            name="test-recipe",
            description="Test recipe",
            version="1.0.0",
            stages=[
                Stage(
                    name="planning",
                    steps=[Step(id="plan", agent="planner", prompt="Plan")],
                    approval=ApprovalConfig(required=True, prompt="Approve?"),
                ),
            ],
        )

    def test_get_stage_approval_status_default(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """Default approval status should be NOT_REQUIRED."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)
        status = session_manager.get_stage_approval_status(
            session_id, temp_dir, "planning"
        )
        assert status == ApprovalStatus.NOT_REQUIRED

    def test_set_and_get_approval_status(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """Should be able to set and get approval status."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)

        session_manager.set_stage_approval_status(
            session_id,
            temp_dir,
            "planning",
            ApprovalStatus.APPROVED,
            reason="Looks good",
        )

        status = session_manager.get_stage_approval_status(
            session_id, temp_dir, "planning"
        )
        assert status == ApprovalStatus.APPROVED

    def test_set_approval_status_records_history(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """Setting approval status should record in history."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)

        session_manager.set_stage_approval_status(
            session_id,
            temp_dir,
            "planning",
            ApprovalStatus.DENIED,
            reason="Needs revision",
        )

        state = session_manager.load_state(session_id, temp_dir)
        assert "approval_history" in state
        assert len(state["approval_history"]) == 1
        assert state["approval_history"][0]["stage"] == "planning"
        assert state["approval_history"][0]["status"] == "denied"
        assert state["approval_history"][0]["reason"] == "Needs revision"
        assert "timestamp" in state["approval_history"][0]

    def test_set_pending_approval(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """Should be able to set pending approval."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)

        session_manager.set_pending_approval(
            session_id,
            temp_dir,
            "planning",
            "Approve the plan?",
            timeout=600,
            default="deny",
        )

        pending = session_manager.get_pending_approval(session_id, temp_dir)
        assert pending is not None
        assert pending["stage_name"] == "planning"
        assert pending["approval_prompt"] == "Approve the plan?"
        assert pending["approval_timeout"] == 600
        assert pending["approval_default"] == "deny"
        assert "approval_requested_at" in pending

    def test_get_pending_approval_none_when_not_set(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """get_pending_approval should return None when no pending approval."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)
        pending = session_manager.get_pending_approval(session_id, temp_dir)
        assert pending is None

    def test_clear_pending_approval(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """clear_pending_approval should remove pending approval data."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)

        session_manager.set_pending_approval(
            session_id, temp_dir, "planning", "Approve?", 600, "deny"
        )
        assert session_manager.get_pending_approval(session_id, temp_dir) is not None

        session_manager.clear_pending_approval(session_id, temp_dir)
        assert session_manager.get_pending_approval(session_id, temp_dir) is None

    def test_list_pending_approvals(
        self, session_manager: SessionManager, temp_dir: Path
    ):
        """list_pending_approvals should find sessions with pending approvals."""
        recipe1 = Recipe(
            name="recipe1",
            description="test",
            version="1.0.0",
            stages=[Stage(name="s1", steps=[Step(id="a", agent="a", prompt="p")])],
        )
        recipe2 = Recipe(
            name="recipe2",
            description="test",
            version="1.0.0",
            stages=[Stage(name="s1", steps=[Step(id="a", agent="a", prompt="p")])],
        )

        session1 = session_manager.create_session(recipe1, temp_dir)
        # Create second session without pending approval (to verify filtering)
        session_manager.create_session(recipe2, temp_dir)

        # Set pending approval on session1 only
        session_manager.set_pending_approval(
            session1, temp_dir, "s1", "Approve?", 600, "deny"
        )

        pending = session_manager.list_pending_approvals(temp_dir)
        assert len(pending) == 1
        assert pending[0]["session_id"] == session1

    def test_list_pending_approvals_empty(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """list_pending_approvals should return empty list when no pending."""
        session_manager.create_session(sample_recipe, temp_dir)
        pending = session_manager.list_pending_approvals(temp_dir)
        assert pending == []


# =============================================================================
# Approval Timeout Tests
# =============================================================================


class TestApprovalTimeout:
    """Tests for approval timeout checking."""

    @pytest.fixture
    def session_manager(self, temp_dir: Path) -> SessionManager:
        """Create session manager with temp directory."""
        return SessionManager(base_dir=temp_dir, auto_cleanup_days=7)

    @pytest.fixture
    def sample_recipe(self) -> Recipe:
        """Create a sample staged recipe."""
        return Recipe(
            name="test-recipe",
            description="Test recipe",
            version="1.0.0",
            stages=[
                Stage(
                    name="planning",
                    steps=[Step(id="plan", agent="planner", prompt="Plan")],
                ),
            ],
        )

    def test_check_timeout_no_pending(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """check_approval_timeout returns None when no pending approval."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)
        result = session_manager.check_approval_timeout(session_id, temp_dir)
        assert result is None

    def test_check_timeout_not_expired(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """check_approval_timeout returns None when timeout not reached."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)
        session_manager.set_pending_approval(
            session_id, temp_dir, "planning", "Approve?", 3600, "deny"
        )

        result = session_manager.check_approval_timeout(session_id, temp_dir)
        assert result is None

    def test_check_timeout_expired_deny(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """check_approval_timeout should apply TIMEOUT status when expired with deny default."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)
        session_manager.set_pending_approval(
            session_id, temp_dir, "planning", "Approve?", 1, "deny"
        )

        # Manually set requested_at to past
        state = session_manager.load_state(session_id, temp_dir)
        state["pending_approval_requested_at"] = (
            datetime.datetime.now() - datetime.timedelta(seconds=10)
        ).isoformat()
        session_manager.save_state(session_id, temp_dir, state)

        result = session_manager.check_approval_timeout(session_id, temp_dir)
        assert result == ApprovalStatus.TIMEOUT

        # Verify status was set
        status = session_manager.get_stage_approval_status(
            session_id, temp_dir, "planning"
        )
        assert status == ApprovalStatus.TIMEOUT

        # Verify pending was cleared
        assert session_manager.get_pending_approval(session_id, temp_dir) is None

    def test_check_timeout_expired_approve(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """check_approval_timeout should auto-approve when expired with approve default."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)
        session_manager.set_pending_approval(
            session_id, temp_dir, "planning", "Approve?", 1, "approve"
        )

        # Manually set requested_at to past
        state = session_manager.load_state(session_id, temp_dir)
        state["pending_approval_requested_at"] = (
            datetime.datetime.now() - datetime.timedelta(seconds=10)
        ).isoformat()
        session_manager.save_state(session_id, temp_dir, state)

        result = session_manager.check_approval_timeout(session_id, temp_dir)
        assert result == ApprovalStatus.APPROVED

        # Verify status was set
        status = session_manager.get_stage_approval_status(
            session_id, temp_dir, "planning"
        )
        assert status == ApprovalStatus.APPROVED

    def test_check_timeout_zero_never_expires(
        self, session_manager: SessionManager, sample_recipe: Recipe, temp_dir: Path
    ):
        """Zero timeout (wait forever) should never expire."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)
        session_manager.set_pending_approval(
            session_id, temp_dir, "planning", "Approve?", 0, "deny"
        )

        # Even with old timestamp, should not timeout
        state = session_manager.load_state(session_id, temp_dir)
        state["pending_approval_requested_at"] = (
            datetime.datetime.now() - datetime.timedelta(days=365)
        ).isoformat()
        session_manager.save_state(session_id, temp_dir, state)

        result = session_manager.check_approval_timeout(session_id, temp_dir)
        assert result is None


# =============================================================================
# ApprovalGatePausedError Tests
# =============================================================================


class TestApprovalGatePausedError:
    """Tests for ApprovalGatePausedError exception."""

    def test_exception_attributes(self):
        """Exception should store session_id, stage_name, and approval_prompt."""
        error = ApprovalGatePausedError(
            session_id="test-123",
            stage_name="planning",
            approval_prompt="Do you approve?",
        )
        assert error.session_id == "test-123"
        assert error.stage_name == "planning"
        assert error.approval_prompt == "Do you approve?"

    def test_exception_message(self):
        """Exception message should be informative."""
        error = ApprovalGatePausedError(
            session_id="test-123",
            stage_name="planning",
            approval_prompt="Do you approve?",
        )
        assert "planning" in str(error)
        assert (
            "paused" in str(error).lower() or "awaiting approval" in str(error).lower()
        )

    def test_exception_can_be_caught(self):
        """Exception should be catchable."""
        with pytest.raises(ApprovalGatePausedError) as exc_info:
            raise ApprovalGatePausedError("s1", "stage1", "Approve?")

        assert exc_info.value.session_id == "s1"

    def test_exception_is_not_generic_exception_subclass(self):
        """ApprovalGatePausedError should be distinguishable from generic errors."""
        error = ApprovalGatePausedError("s1", "stage1", "Approve?")
        assert isinstance(error, Exception)
        assert type(error).__name__ == "ApprovalGatePausedError"


# =============================================================================
# ApprovalStatus Enum Tests
# =============================================================================


class TestApprovalStatus:
    """Tests for ApprovalStatus enum."""

    def test_all_statuses_exist(self):
        """All expected statuses should exist."""
        assert ApprovalStatus.PENDING == "pending"
        assert ApprovalStatus.APPROVED == "approved"
        assert ApprovalStatus.DENIED == "denied"
        assert ApprovalStatus.NOT_REQUIRED == "not_required"
        assert ApprovalStatus.TIMEOUT == "timeout"

    def test_status_is_string(self):
        """Status values should be strings for JSON serialization."""
        assert isinstance(ApprovalStatus.PENDING.value, str)
        assert isinstance(ApprovalStatus.APPROVED.value, str)

    def test_status_from_string(self):
        """Should be able to create status from string."""
        status = ApprovalStatus("approved")
        assert status == ApprovalStatus.APPROVED

    def test_invalid_status_raises(self):
        """Invalid status string should raise ValueError."""
        with pytest.raises(ValueError):
            ApprovalStatus("invalid")


# =============================================================================
# Approval Message Context Tests
# =============================================================================


class TestApprovalMessageContext:
    """Tests for _approval_message flowing through approval gates into recipe context."""

    @pytest.fixture
    def session_manager(self, temp_dir: Path) -> SessionManager:
        """Create session manager with temp directory."""
        return SessionManager(base_dir=temp_dir, auto_cleanup_days=7)

    @pytest.fixture
    def sample_recipe(self) -> Recipe:
        """Create a sample staged recipe."""
        return Recipe(
            name="test-recipe",
            description="Test recipe",
            version="1.0.0",
            stages=[
                Stage(
                    name="planning",
                    steps=[Step(id="plan", agent="planner", prompt="Plan")],
                    approval=ApprovalConfig(required=True, prompt="Approve?"),
                ),
                Stage(
                    name="execution",
                    steps=[
                        Step(
                            id="execute",
                            agent="executor",
                            prompt="Do {{_approval_message}}",
                        )
                    ],
                ),
            ],
        )

    @pytest.fixture
    def recipes_tool(
        self, session_manager: SessionManager, temp_dir: Path
    ) -> RecipesTool:
        """Create a RecipesTool with mocked coordinator pointing at temp_dir."""
        mock_coordinator = MagicMock()
        mock_coordinator.get_capability.return_value = str(temp_dir)
        mock_executor = MagicMock()
        return RecipesTool(
            executor=mock_executor,
            session_manager=session_manager,
            coordinator=mock_coordinator,
            config={},
        )

    def test_input_schema_includes_message(
        self,
        recipes_tool: RecipesTool,
    ):
        """The tool's input_schema should include a 'message' property for approve operations."""
        schema = recipes_tool.input_schema
        assert "message" in schema["properties"]
        assert schema["properties"]["message"]["type"] == "string"

    @pytest.mark.asyncio
    async def test_approve_stage_stores_message_in_state(
        self,
        recipes_tool: RecipesTool,
        session_manager: SessionManager,
        sample_recipe: Recipe,
        temp_dir: Path,
    ):
        """_approve_stage should store the user's message in session state as _approval_message."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)

        # Set up a pending approval (as the executor would)
        session_manager.set_pending_approval(
            session_id, temp_dir, "planning", "Approve?", 600, "deny"
        )

        # Call _approve_stage with a message
        result = await recipes_tool._approve_stage(
            {"session_id": session_id, "stage_name": "planning", "message": "merge"}
        )
        assert result.success is True

        # Verify _approval_message was stored in session state
        state = session_manager.load_state(session_id, temp_dir)
        assert "_approval_message" in state
        assert state["_approval_message"] == "merge"

    @pytest.mark.asyncio
    async def test_approve_stage_defaults_message_to_empty(
        self,
        recipes_tool: RecipesTool,
        session_manager: SessionManager,
        sample_recipe: Recipe,
        temp_dir: Path,
    ):
        """_approve_stage without message should store empty string as _approval_message."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)

        session_manager.set_pending_approval(
            session_id, temp_dir, "planning", "Approve?", 600, "deny"
        )

        # Call _approve_stage WITHOUT a message
        result = await recipes_tool._approve_stage(
            {"session_id": session_id, "stage_name": "planning"}
        )
        assert result.success is True

        # Verify _approval_message was stored as empty string
        state = session_manager.load_state(session_id, temp_dir)
        assert "_approval_message" in state
        assert state["_approval_message"] == ""

    @pytest.mark.asyncio
    async def test_approval_message_in_tool_result(
        self,
        recipes_tool: RecipesTool,
        session_manager: SessionManager,
        sample_recipe: Recipe,
        temp_dir: Path,
    ):
        """_approve_stage should include the message in the ToolResult output dict."""
        session_id = session_manager.create_session(sample_recipe, temp_dir)

        session_manager.set_pending_approval(
            session_id, temp_dir, "planning", "Approve?", 600, "deny"
        )

        result = await recipes_tool._approve_stage(
            {"session_id": session_id, "stage_name": "planning", "message": "pr"}
        )
        assert result.success is True
        assert result.output["approval_message"] == "pr"
