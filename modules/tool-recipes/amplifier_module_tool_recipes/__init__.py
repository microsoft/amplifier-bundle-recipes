"""Amplifier tool-recipes module - Execute multi-step AI agent recipes."""

import logging
from pathlib import Path
from typing import Any

from amplifier_core import ModuleCoordinator
from amplifier_core import ToolResult

from .executor import ApprovalGatePausedError
from .executor import RecipeExecutor
from .models import Recipe
from .session import ApprovalStatus
from .session import SessionManager
from .validator import validate_recipe

logger = logging.getLogger(__name__)


async def mount(coordinator: ModuleCoordinator, config: dict[str, Any] | None = None):
    """
    Mount tool-recipes module.

    Args:
        coordinator: Amplifier coordinator
        config: Optional tool configuration
    """
    config = config or {}

    # Initialize session manager
    base_dir = Path(config.get("session_dir", "~/.amplifier/projects")).expanduser()
    auto_cleanup_days = config.get("auto_cleanup_days", 7)
    session_manager = SessionManager(base_dir, auto_cleanup_days)

    # Initialize executor
    executor = RecipeExecutor(coordinator, session_manager)

    # Create tool instance
    tool = RecipesTool(executor, session_manager, coordinator, config)

    # Register tool in mount_points
    coordinator.mount_points["tools"][tool.name] = tool

    logger.info("Mounted tool-recipes")


class RecipesTool:
    """Tool for executing, resuming, and managing recipe workflows."""

    def __init__(
        self,
        executor: RecipeExecutor,
        session_manager: SessionManager,
        coordinator: ModuleCoordinator,
        config: dict[str, Any],
    ):
        """Initialize tool."""
        self.executor = executor
        self.session_manager = session_manager
        self.coordinator = coordinator
        self.config = config

    @property
    def name(self) -> str:
        return "recipes"

    @property
    def description(self) -> str:
        return """Execute multi-step AI agent recipes (workflows).

Recipes are declarative YAML specifications that define multi-step agent workflows with:
- Sequential execution with state persistence
- Agent delegation with context accumulation
- Automatic checkpointing for resumability
- Error handling and retry logic
- Approval gates for human-in-loop workflows (staged recipes)

Operations:
- execute: Run a recipe from YAML file
- resume: Resume interrupted session
- list: List active sessions
- validate: Validate recipe structure
- approvals: List pending approvals across sessions
- approve: Approve a stage to continue execution
- deny: Deny a stage to stop execution

Example:
  Execute recipe: {{"operation": "execute", "recipe_path": "examples/code-review.yaml", "context": {{"file_path": "src/auth.py"}}}}
  Resume session: {{"operation": "resume", "session_id": "recipe_20251118_143022_a3f2"}}
  List sessions: {{"operation": "list"}}
  Validate recipe: {{"operation": "validate", "recipe_path": "my-recipe.yaml"}}
  List approvals: {{"operation": "approvals"}}
  Approve stage: {{"operation": "approve", "session_id": "...", "stage_name": "planning"}}
  Deny stage: {{"operation": "deny", "session_id": "...", "stage_name": "planning", "reason": "needs revision"}}"""

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["execute", "resume", "list", "validate", "approvals", "approve", "deny"],
                    "description": "Operation to perform",
                },
                "recipe_path": {
                    "type": "string",
                    "description": "Path to recipe YAML file (required for 'execute' and 'validate' operations)",
                },
                "context": {
                    "type": "object",
                    "description": "Context variables for recipe execution (for 'execute' operation)",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID (required for 'resume', 'approve', 'deny' operations)",
                },
                "stage_name": {
                    "type": "string",
                    "description": "Stage name to approve or deny (required for 'approve' and 'deny' operations)",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for denial (optional for 'deny' operation)",
                },
            },
            "required": ["operation"],
        }

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        """
        Execute tool operation.

        Args:
            input: Tool input with 'operation' field

        Returns:
            ToolResult with operation results
        """
        operation = input.get("operation")

        try:
            if operation == "execute":
                return await self._execute_recipe(input)
            if operation == "resume":
                return await self._resume_recipe(input)
            if operation == "list":
                return await self._list_sessions(input)
            if operation == "validate":
                return await self._validate_recipe(input)
            if operation == "approvals":
                return await self._list_approvals(input)
            if operation == "approve":
                return await self._approve_stage(input)
            if operation == "deny":
                return await self._deny_stage(input)
            return ToolResult(
                success=False,
                error={"message": f"Unknown operation: {operation}"},
            )
        except Exception as e:
            logger.error(f"Recipe tool error: {e}", exc_info=True)
            return ToolResult(
                success=False,
                error={"message": str(e), "type": type(e).__name__},
            )

    def _resolve_path(self, path_str: str) -> Path | None:
        """Resolve a path string, handling @mention syntax.

        Args:
            path_str: Path string, possibly with @bundle:path syntax

        Returns:
            Resolved Path, or None if @mention couldn't be resolved
        """
        import sys
        if path_str.startswith("@"):
            # Get mention resolver from coordinator capabilities
            mention_resolver = self.coordinator.get_capability("mention_resolver")
            if mention_resolver is None:
                print(f"[DEBUG] mention_resolver capability NOT AVAILABLE for {path_str}", file=sys.stderr)
                caps = list(self.coordinator._capabilities.keys()) if hasattr(self.coordinator, '_capabilities') else 'unknown'
                print(f"[DEBUG] Available capabilities: {caps}", file=sys.stderr)
                return None
            # Debug: check what type of resolver we have
            print(f"[DEBUG recipes] coordinator id: {id(self.coordinator)}", file=sys.stderr)
            print(f"[DEBUG recipes] mention_resolver type: {type(mention_resolver).__name__}", file=sys.stderr)
            print(f"[DEBUG recipes] mention_resolver id: {id(mention_resolver)}", file=sys.stderr)
            # Check all capabilities
            caps = list(self.coordinator._capabilities.keys()) if hasattr(self.coordinator, '_capabilities') else []
            print(f"[DEBUG recipes] all capabilities: {caps}", file=sys.stderr)
            if hasattr(mention_resolver, 'foundation_resolver'):
                print(f"[DEBUG recipes] foundation_resolver: {mention_resolver.foundation_resolver}", file=sys.stderr)
                print(f"[DEBUG recipes] foundation_resolver is not None: {mention_resolver.foundation_resolver is not None}", file=sys.stderr)
                if mention_resolver.foundation_resolver and hasattr(mention_resolver.foundation_resolver, 'bundles'):
                    print(f"[DEBUG recipes] foundation bundles: {list(mention_resolver.foundation_resolver.bundles.keys())}", file=sys.stderr)
            result = mention_resolver.resolve(path_str)
            if result is None:
                print(f"[DEBUG] mention_resolver returned None for {path_str}", file=sys.stderr)
            else:
                print(f"[DEBUG] resolved {path_str} -> {result}", file=sys.stderr)
            return result
        return Path(path_str)

    async def _execute_recipe(self, input: dict[str, Any]) -> ToolResult:
        """Execute recipe from YAML file."""
        recipe_path_str = input.get("recipe_path")
        if not recipe_path_str:
            return ToolResult(success=False, error={"message": "recipe_path is required for execute operation"})

        # Resolve @mention paths (e.g., @recipes:examples/code-review.yaml)
        recipe_path = self._resolve_path(recipe_path_str)
        if recipe_path is None:
            return ToolResult(
                success=False,
                error={"message": f"Could not resolve @mention path: {recipe_path_str}"},
            )
        context_vars = input.get("context", {})

        # Determine project path (current working directory)
        project_path = Path.cwd()

        # Load recipe
        try:
            recipe = Recipe.from_yaml(recipe_path)
        except Exception as e:
            return ToolResult(success=False, error={"message": f"Failed to load recipe: {str(e)}"})

        # Validate recipe
        validation = validate_recipe(recipe, self.coordinator)
        if not validation.is_valid:
            return ToolResult(
                success=False,
                error={
                    "message": "Recipe validation failed",
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                },
            )

        # Execute recipe (pass recipe_path for sub-recipe resolution)
        try:
            final_context = await self.executor.execute_recipe(
                recipe, context_vars, project_path, recipe_path=recipe_path
            )

            return ToolResult(
                success=True,
                output={
                    "status": "completed",
                    "recipe": recipe.name,
                    "session_id": final_context["session"]["id"],
                    "context": final_context,
                },
            )
        except ApprovalGatePausedError as e:
            # Recipe paused at approval gate - not an error
            return ToolResult(
                success=True,
                output={
                    "status": "paused_for_approval",
                    "recipe": recipe.name,
                    "session_id": e.session_id,
                    "stage_name": e.stage_name,
                    "approval_prompt": e.approval_prompt,
                    "message": f"Recipe paused at stage '{e.stage_name}'. Use 'approve' or 'deny' to continue.",
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error={
                    "message": f"Recipe execution failed: {str(e)}",
                    "type": type(e).__name__,
                },
            )

    async def _resume_recipe(self, input: dict[str, Any]) -> ToolResult:
        """Resume interrupted recipe session."""
        session_id = input.get("session_id")
        if not session_id:
            return ToolResult(success=False, error={"message": "session_id is required for resume operation"})

        project_path = Path.cwd()

        # Check session exists
        if not self.session_manager.session_exists(session_id, project_path):
            return ToolResult(
                success=False,
                error={"message": f"Session not found: {session_id}"},
            )

        # Validate session exists
        try:
            _ = self.session_manager.load_state(session_id, project_path)
        except Exception as e:
            return ToolResult(success=False, error={"message": f"Failed to load session: {str(e)}"})

        # Load recipe from session
        session_dir = self.session_manager.get_session_dir(session_id, project_path)
        recipe_file = session_dir / "recipe.yaml"

        if not recipe_file.exists():
            return ToolResult(
                success=False,
                error={"message": f"Recipe file not found in session: {session_id}"},
            )

        try:
            recipe = Recipe.from_yaml(recipe_file)
        except Exception as e:
            return ToolResult(success=False, error={"message": f"Failed to load recipe from session: {str(e)}"})

        # Resume execution
        try:
            final_context = await self.executor.execute_recipe(
                recipe, context_vars={}, project_path=project_path, session_id=session_id
            )

            return ToolResult(
                success=True,
                output={
                    "status": "completed",
                    "recipe": recipe.name,
                    "session_id": session_id,
                    "context": final_context,
                },
            )
        except ApprovalGatePausedError as e:
            # Recipe paused at another approval gate
            return ToolResult(
                success=True,
                output={
                    "status": "paused_for_approval",
                    "recipe": recipe.name,
                    "session_id": e.session_id,
                    "stage_name": e.stage_name,
                    "approval_prompt": e.approval_prompt,
                    "message": f"Recipe paused at stage '{e.stage_name}'. Use 'approve' or 'deny' to continue.",
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error={
                    "message": f"Failed to resume recipe: {str(e)}",
                    "type": type(e).__name__,
                },
            )

    async def _list_sessions(self, input: dict[str, Any]) -> ToolResult:
        """List active recipe sessions."""
        project_path = Path.cwd()

        try:
            sessions = self.session_manager.list_sessions(project_path)

            return ToolResult(
                success=True,
                output={
                    "sessions": sessions,
                    "count": len(sessions),
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error={"message": f"Failed to list sessions: {str(e)}"},
            )

    async def _validate_recipe(self, input: dict[str, Any]) -> ToolResult:
        """Validate recipe without executing."""
        recipe_path_str = input.get("recipe_path")
        if not recipe_path_str:
            return ToolResult(success=False, error={"message": "recipe_path is required for validate operation"})

        # Resolve @mention paths (e.g., @recipes:examples/code-review.yaml)
        recipe_path = self._resolve_path(recipe_path_str)
        if recipe_path is None:
            return ToolResult(
                success=False,
                error={"message": f"Could not resolve @mention path: {recipe_path_str}"},
            )

        try:
            # Load recipe
            recipe = Recipe.from_yaml(recipe_path)

            # Validate
            validation = validate_recipe(recipe, self.coordinator)

            if validation.is_valid:
                return ToolResult(
                    success=True,
                    output={
                        "status": "valid",
                        "recipe": recipe.name,
                        "version": recipe.version,
                        "warnings": validation.warnings,
                    },
                )
            return ToolResult(
                success=False,
                error={
                    "message": "Recipe validation failed",
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                error={"message": f"Failed to validate recipe: {str(e)}"},
            )

    async def _list_approvals(self, input: dict[str, Any]) -> ToolResult:
        """List pending approvals across all sessions."""
        project_path = Path.cwd()

        try:
            pending_approvals = self.session_manager.list_pending_approvals(project_path)

            return ToolResult(
                success=True,
                output={
                    "pending_approvals": pending_approvals,
                    "count": len(pending_approvals),
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error={"message": f"Failed to list approvals: {str(e)}"},
            )

    async def _approve_stage(self, input: dict[str, Any]) -> ToolResult:
        """Approve a stage to continue execution."""
        session_id = input.get("session_id")
        stage_name = input.get("stage_name")

        if not session_id:
            return ToolResult(success=False, error={"message": "session_id is required for approve operation"})
        if not stage_name:
            return ToolResult(success=False, error={"message": "stage_name is required for approve operation"})

        project_path = Path.cwd()

        # Verify session exists
        if not self.session_manager.session_exists(session_id, project_path):
            return ToolResult(
                success=False,
                error={"message": f"Session not found: {session_id}"},
            )

        # Check if there's a pending approval for this stage
        pending = self.session_manager.get_pending_approval(session_id, project_path)
        if not pending:
            return ToolResult(
                success=False,
                error={"message": f"No pending approval for session: {session_id}"},
            )

        if pending["stage_name"] != stage_name:
            return ToolResult(
                success=False,
                error={
                    "message": f"Stage mismatch: pending approval is for '{pending['stage_name']}', not '{stage_name}'"
                },
            )

        try:
            # Set approval status
            self.session_manager.set_stage_approval_status(
                session_id=session_id,
                project_path=project_path,
                stage_name=stage_name,
                status=ApprovalStatus.APPROVED,
                reason="Approved by user",
            )

            return ToolResult(
                success=True,
                output={
                    "status": "approved",
                    "session_id": session_id,
                    "stage_name": stage_name,
                    "message": f"Stage '{stage_name}' approved. Use 'resume' operation to continue execution.",
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error={"message": f"Failed to approve stage: {str(e)}"},
            )

    async def _deny_stage(self, input: dict[str, Any]) -> ToolResult:
        """Deny a stage to stop execution."""
        session_id = input.get("session_id")
        stage_name = input.get("stage_name")
        reason = input.get("reason", "Denied by user")

        if not session_id:
            return ToolResult(success=False, error={"message": "session_id is required for deny operation"})
        if not stage_name:
            return ToolResult(success=False, error={"message": "stage_name is required for deny operation"})

        project_path = Path.cwd()

        # Verify session exists
        if not self.session_manager.session_exists(session_id, project_path):
            return ToolResult(
                success=False,
                error={"message": f"Session not found: {session_id}"},
            )

        # Check if there's a pending approval for this stage
        pending = self.session_manager.get_pending_approval(session_id, project_path)
        if not pending:
            return ToolResult(
                success=False,
                error={"message": f"No pending approval for session: {session_id}"},
            )

        if pending["stage_name"] != stage_name:
            return ToolResult(
                success=False,
                error={
                    "message": f"Stage mismatch: pending approval is for '{pending['stage_name']}', not '{stage_name}'"
                },
            )

        try:
            # Set denial status
            self.session_manager.set_stage_approval_status(
                session_id=session_id,
                project_path=project_path,
                stage_name=stage_name,
                status=ApprovalStatus.DENIED,
                reason=reason,
            )

            # Clear the pending approval
            self.session_manager.clear_pending_approval(session_id, project_path)

            return ToolResult(
                success=True,
                output={
                    "status": "denied",
                    "session_id": session_id,
                    "stage_name": stage_name,
                    "reason": reason,
                    "message": f"Stage '{stage_name}' denied. Recipe execution will not continue.",
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error={"message": f"Failed to deny stage: {str(e)}"},
            )
