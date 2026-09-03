"""Amplifier tool-recipes module - Execute multi-step AI agent recipes.

Two execution modes live behind the single ``recipes`` tool, chosen by the
recipe's own manifest (``recipe-dependency-manifest.v1`` Core 1):

* A recipe declaring ``schema_version`` is executed by the
  ``amplifier-recipe-runner`` library -- the one execution home
  (``recipe-runner-lib.v1`` Core 1). Amplifier reaches it through five named
  ports only, none of which carries this session's agent map.
* A recipe declaring none is a **legacy recipe**: it keeps its existing
  caller-bound behavior, byte-identically, labeled
  ``execution_mode="legacy-caller-bound"`` and accompanied by a deprecation
  warning (Core 10).

The split is per *operation*, not just per run: ``execute``, ``validate`` and
``resume`` each route on the manifest. A v2 recipe is validated by the
library's plan preflight and resumed through the library, never by the legacy
validator or the legacy executor -- both of which would answer while knowing
nothing about the ``dependencies`` block that decides what the recipe actually
resolves to.

See ``runner_adapter.py`` for the port mapping, the ``resume`` seam, and why
the label rides beside the result payload rather than inside it.
"""

import json
import logging
from pathlib import Path
from typing import Any

from amplifier_core import ModuleCoordinator
from amplifier_core import ToolResult

from .executor import ApprovalGatePausedError
from .executor import RecipeExecutor
from .models import Recipe
from .runner_adapter import LEGACY_EXECUTION_MODE
from .runner_adapter import V2_EXECUTION_MODE
from .runner_adapter import RecipeRunnerUnavailableError
from .runner_adapter import V2ResumeUnavailableError
from .runner_adapter import check_adapter_config
from .runner_adapter import check_legacy_agents_available
from .runner_adapter import declared_schema_version
from .runner_adapter import is_v2_recipe
from .runner_adapter import label_execution_mode
from .runner_adapter import load_runner
from .runner_adapter import recipe_display_name
from .runner_adapter import resume_v2_recipe
from .closed_world import agent_provenance_record
from .runner_adapter import provider_roles_label
from .runner_adapter import run_v2_recipe
from .runner_adapter import run_v2_recipe_in_session
from .runner_adapter import V2_LEGACY_ENGINE_EXECUTION_MODE
from .runner_adapter import validate_v2_recipe
from .runner_adapter import warn_legacy_recipe
from .session import ApprovalStatus
from .session import SessionManager
from .validator import validate_recipe

logger = logging.getLogger(__name__)

# Maximum size (in bytes) for output values returned in tool result
# Prevents oversized tool results that break session resumption
# ~10KB is roughly 2.5k tokens, leaving room for other content
MAX_OUTPUT_SIZE_BYTES = 10_000


def _truncate_value(value: Any, max_bytes: int = MAX_OUTPUT_SIZE_BYTES) -> Any:
    """
    Truncate large values to prevent context overflow.

    Handles strings, dicts, and lists differently:
    - Strings: Truncate with message
    - Dicts/Lists: Return truncation marker with preview

    Args:
        value: Value to potentially truncate
        max_bytes: Maximum size in bytes

    Returns:
        Original value if small enough, truncated version otherwise
    """
    if isinstance(value, str):
        if len(value) > max_bytes:
            return (
                value[:max_bytes] + "\n\n[... truncated, see session for full output]"
            )
        return value

    if isinstance(value, (dict, list)):
        try:
            serialized = json.dumps(value)
            if len(serialized) > max_bytes:
                # For structured data, return a truncation marker with preview
                preview = (
                    serialized[:500] + "..." if len(serialized) > 500 else serialized
                )
                return {
                    "_truncated": True,
                    "_type": type(value).__name__,
                    "_full_size_bytes": len(serialized),
                    "_preview": preview,
                    "_message": "See session files for full output",
                }
        except (TypeError, ValueError):
            pass  # Can't serialize, return as-is
        return value

    return value


def _extract_result_summary(
    context: dict[str, Any],
    recipe: Recipe | None = None,
) -> dict[str, Any]:
    """
    Extract a compact summary from recipe context for tool result.

    Instead of returning the entire accumulated context (which can be 1MB+
    for complex workflows), return only essential information.

    Output Priority (following "mechanism not policy" principle):
    1. Explicit `final_output` key in context (documented contract)
    2. Last step's output variable (if recipe provided)
    3. List of available outputs for discovery

    Recipes should use `final_output` as their context key for the primary
    result they want returned to the caller.

    Args:
        context: Full recipe execution context
        recipe: Recipe object (optional, enables last-step fallback)

    Returns:
        Compact summary suitable for tool result
    """
    summary: dict[str, Any] = {}

    # === Metadata (always small, always include) ===

    if "session" in context:
        summary["session"] = context["session"]

    if "recipe" in context:
        summary["recipe_metadata"] = context["recipe"]

    # Completion info for staged recipes
    if "stage" in context:
        summary["last_stage"] = context["stage"]

    if "step" in context:
        summary["last_step"] = context["step"]

    if "_skipped_steps" in context:
        summary["skipped_steps"] = context["_skipped_steps"]

    # === Final Output (explicit contract, no guessing) ===

    # Priority 1: Explicit `final_output` key (documented contract)
    # Recipes should use this key if they want to return specific output
    if "final_output" in context:
        summary["final_output"] = _truncate_value(context["final_output"])

    # Priority 2: Last step's output variable (if recipe provided)
    # This is often the "real" final output of the workflow
    elif recipe is not None:
        last_step_output = _get_last_step_output_key(recipe)
        if last_step_output and last_step_output in context:
            summary["final_output"] = _truncate_value(context[last_step_output])
            summary["final_output_key"] = last_step_output

    # === Discovery: what outputs are available ===

    output_keys = [
        k
        for k in context.keys()
        if not k.startswith("_") and k not in ("session", "recipe", "stage", "step")
    ]
    summary["available_outputs"] = output_keys

    # === Reference to full results ===

    if "session" in context:
        session_id = context["session"].get("id", "unknown")
        summary["full_results_location"] = (
            f"Full results saved in recipe session: {session_id}. "
            "Use 'recipes list' to see session details."
        )

    return summary


def _get_last_step_output_key(recipe: Recipe) -> str | None:
    """
    Get the output key from the recipe's last step.

    For flat recipes: last step in steps list
    For staged recipes: last step of last stage

    Args:
        recipe: Recipe object

    Returns:
        Output key name, or None if not found
    """
    # Flat recipe
    if recipe.steps:
        last_step = recipe.steps[-1]
        return getattr(last_step, "output", None)

    # Staged recipe
    if recipe.stages:
        last_stage = recipe.stages[-1]
        if last_stage.steps:
            last_step = last_stage.steps[-1]
            return getattr(last_step, "output", None)

    return None


def _validation_issue_dict(issue: Any) -> dict[str, Any]:
    """A library ``ValidationIssue`` as a JSON-serializable finding.

    The typed error stays legible: ``code`` is the real exception class name
    (``UndeclaredAgentError``, ``AgentCollisionError``, ...), not a flattened
    string, and ``remedy`` survives to the caller.
    """
    return {
        "code": issue.code,
        "message": issue.message,
        "location": issue.location,
        "remedy": issue.remedy,
    }


#: Session-state key holding what a schema-v2 run recorded, so `resume` can
#: tell "nothing ran" from "some steps ran" from "it finished" without
#: guessing. Written by `_record_v2_run`; read by `_resume_v2_recipe`.
V2_RUN_STATE_KEY = "v2_run"

#: Session-state key holding the plan's dependency identity and per-agent
#: provenance for a v2 run, so this surface's identity is comparable against
#: `recipe-runner plan --json` on any other surface (lib.v1 Core 7).
V2_PROVENANCE_STATE_KEY = "v2_provenance"


def _expand_session_dir(raw: Any, coordinator: Any) -> Path:
    """Resolve the configured ``session_dir``, expanding ``{project}`` and ``~``.

    ``{project}`` is the project the session belongs to -- the name of the
    host's working directory. Left unexpanded it becomes a *literal* directory
    called ``{project}`` on disk, which silently collects every project's
    recipe sessions in one place under a path nobody chose.
    """
    text = str(raw)
    if "{project}" in text:
        working = None
        getter = getattr(coordinator, "get_capability", None)
        if callable(getter):
            working = getter("session.working_dir")
        project = Path(working).name if working else Path.cwd().name
        text = text.replace("{project}", project)
    return Path(text).expanduser()


async def mount(coordinator: ModuleCoordinator, config: dict[str, Any] | None = None):
    """
    Mount tool-recipes module.

    Args:
        coordinator: Amplifier coordinator
        config: Optional tool configuration

    Raises:
        AdapterConfigError: the config carries a key this module does not read
            (notably ``legacy_mode``). Refused rather than retained inert, in
            the spirit of ``recipe-dependency-manifest.v1`` Core 12: a setting
            that looks honoured and changes nothing is indistinguishable, from
            the outside, from one that works.
    """
    config = config or {}
    check_adapter_config(config)

    # Initialize session manager
    base_dir = _expand_session_dir(
        config.get("session_dir", "~/.amplifier/projects"), coordinator
    )
    auto_cleanup_days = config.get("auto_cleanup_days", 7)
    session_manager = SessionManager(base_dir, auto_cleanup_days)

    # Declare observable lifecycle events for this module
    # (hooks-logging will auto-discover and log these)
    obs_events = list(coordinator.get_capability("observability.events") or [])
    obs_events.extend(
        [
            "recipe:start",
            "recipe:step",
            "recipe:complete",
            "recipe:approval",
            "recipe:loop_iteration",
            "recipe:loop_complete",
        ]
    )
    coordinator.register_capability("observability.events", obs_events)

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

    def _get_working_dir(self) -> Path:
        """Get working directory from coordinator capability.

        Returns session.working_dir capability if available, falls back to
        Path.cwd() for backward compatibility with CLI and older deployments.
        """
        working_dir = self.coordinator.get_capability("session.working_dir")
        return Path(working_dir) if working_dir else Path.cwd()

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
- validate: Validate recipe structure (schema-v2 recipes are checked by the
  runner library: manifest parse + dependency plan preflight, nothing executed)
- approvals: List pending approvals across sessions
- approve: Approve a stage to continue execution
- deny: Deny a stage to stop execution
- cancel: Cancel a running recipe session (graceful or immediate)

Example:
  Execute recipe: {{"operation": "execute", "recipe_path": "@recipes:examples/code-review.yaml", "context": {{"file_path": "src/auth.py"}}}}
  Resume session: {{"operation": "resume", "session_id": "recipe_20251118_143022_a3f2"}}
  List sessions: {{"operation": "list"}}
  Validate recipe: {{"operation": "validate", "recipe_path": "@recipes:examples/my-recipe.yaml"}}
  List approvals: {{"operation": "approvals"}}
  Approve stage: {{"operation": "approve", "session_id": "...", "stage_name": "planning", "message": "merge"}}
  Deny stage: {{"operation": "deny", "session_id": "...", "stage_name": "planning", "reason": "needs revision"}}
  Cancel recipe: {{"operation": "cancel", "session_id": "...", "immediate": false}}"""

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "execute",
                        "resume",
                        "list",
                        "validate",
                        "approvals",
                        "approve",
                        "deny",
                        "cancel",
                    ],
                    "description": "Operation to perform",
                },
                "recipe_path": {
                    "type": "string",
                    "description": "Path to recipe YAML file. Supports @bundle:path format (e.g., @recipes:examples/code-review.yaml) to reference recipes within bundles. Required for 'execute' and 'validate' operations.",
                },
                "context": {
                    "type": "object",
                    "description": "Context variables for recipe execution (for 'execute' operation)",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID (required for 'resume', 'approve', 'deny', 'cancel' operations)",
                },
                "stage_name": {
                    "type": "string",
                    "description": "Stage name to approve or deny (required for 'approve' and 'deny' operations)",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for denial (optional for 'deny' operation)",
                },
                "message": {
                    "type": "string",
                    "description": "Optional message from the user when approving (e.g., 'merge', 'pr'). Made available to subsequent steps as {{_approval_message}}.",
                },
                "immediate": {
                    "type": "boolean",
                    "description": "If true, request immediate cancellation (don't wait for current step). For 'cancel' operation.",
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
            if operation == "cancel":
                return await self._cancel_recipe(input)
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
        if path_str.startswith("@"):
            # Get mention resolver from coordinator capabilities
            mention_resolver = self.coordinator.get_capability("mention_resolver")
            if mention_resolver is None:
                return None
            return mention_resolver.resolve(path_str)
        return Path(path_str).expanduser()

    async def _execute_recipe(self, input: dict[str, Any]) -> ToolResult:
        """Route a recipe to the runner library (v2) or the legacy path.

        The manifest decides (``recipe-dependency-manifest.v1`` Core 1): a
        recipe declaring ``schema_version`` executes in the runner library; one
        that does not is a legacy recipe and runs exactly as it always has.

        There is deliberately no fallback from v2 to legacy. If the library is
        unavailable the run fails loud -- running a v2 recipe caller-bound would
        resolve a *different* agent catalog while reporting success.
        """
        recipe_path_str = input.get("recipe_path")
        if not recipe_path_str:
            return ToolResult(
                success=False,
                error={"message": "recipe_path is required for execute operation"},
            )

        # Resolve @mention paths (e.g., @recipes:examples/code-review.yaml)
        recipe_path = self._resolve_path(recipe_path_str)
        if recipe_path is None:
            return ToolResult(
                success=False,
                error={
                    "message": f"Could not resolve @mention path: {recipe_path_str}"
                },
            )
        context_vars = input.get("context", {})

        # Determine project path (from coordinator capability or cwd)
        project_path = self._get_working_dir()

        if is_v2_recipe(recipe_path):
            return await self._execute_v2_recipe(
                recipe_path, context_vars, project_path
            )

        # Legacy recipe: labeled and confined (manifest.v1 Core 10). The warning
        # rides `warnings`/`logging` only -- see runner_adapter.warn_legacy_recipe.
        warn_legacy_recipe(recipe_path)
        result = await self._execute_legacy_recipe(
            recipe_path, context_vars, project_path
        )
        return label_execution_mode(result, LEGACY_EXECUTION_MODE)

    async def _execute_v2_recipe(
        self,
        recipe_path: Path,
        context_vars: dict[str, Any],
        project_path: Path,
    ) -> ToolResult:
        """Execute a schema-v2 recipe in the runner library.

        Amplifier's approvals, cancellation, events/display, workspace and
        provider access are mapped onto the library's five ports; the caller's
        agent map is not among them, and the adapter refuses the run if it ever
        becomes reachable (``runner_adapter.CallerAgentLeakError``).
        """
        try:
            runner = load_runner()
        except RecipeRunnerUnavailableError as exc:
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={
                        "message": str(exc),
                        "type": type(exc).__name__,
                    },
                ),
                V2_EXECUTION_MODE,
            )

        recipe_name = recipe_display_name(recipe_path)

        # Bind an Amplifier recipe session so the approval and cancellation
        # ports have real state to read, and so `approvals`/`cancel`/`list`
        # work for a v2 run exactly as they do for a legacy one.
        session_id: str | None = None
        try:
            session_id = self.session_manager.create_session(
                Recipe(
                    name=recipe_name,
                    description=f"schema_version {declared_schema_version(recipe_path)!r} recipe",
                    version="",
                ),
                project_path,
                recipe_path=recipe_path,
            )
        except Exception as exc:
            logger.warning(
                "Could not bind an Amplifier session to this v2 run (%s); its "
                "approval and cancellation ports will have no state to read.",
                exc,
            )

        try:
            result = await run_v2_recipe_in_session(
                self.coordinator,
                self.session_manager,
                recipe_path,
                context_vars,
                project_path,
                session_id=session_id,
            )
        except Exception as exc:
            logger.error("v2 recipe execution failed: %s", exc, exc_info=True)
            # completed_steps is left unknown, not assumed empty: the run raised
            # before reporting what it finished, and a later `resume` must be
            # able to tell "nothing ran" from "we do not know".
            self._record_v2_run(
                session_id,
                project_path,
                recipe_path,
                run_id=None,
                status="errored",
                completed_steps=None,
                step_ids=None,
                execution_mode=V2_LEGACY_ENGINE_EXECUTION_MODE,
            )
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={
                        "message": f"Recipe execution failed: {exc}",
                        "type": type(exc).__name__,
                    },
                ),
                V2_LEGACY_ENGINE_EXECUTION_MODE,
            )

        plan = result.plan
        self._record_v2_run(
            session_id,
            project_path,
            recipe_path,
            run_id=result.run_id,
            status=getattr(result.status, "name", str(result.status)).lower(),
            completed_steps=list(result.completed_steps),
            step_ids=list(plan.step_ids) if plan is not None else None,
            execution_mode=V2_LEGACY_ENGINE_EXECUTION_MODE,
            provenance=(
                agent_provenance_record(plan, run_id=result.run_id) if plan is not None else None
            ),
        )

        return label_execution_mode(
            self._v2_tool_result(
                result,
                runner,
                recipe_path,
                recipe_name,
                session_id,
                execution_mode=V2_LEGACY_ENGINE_EXECUTION_MODE,
            ),
            V2_LEGACY_ENGINE_EXECUTION_MODE,
        )

    def _record_v2_run(
        self,
        session_id: str | None,
        project_path: Path,
        recipe_path: Path,
        *,
        run_id: str | None,
        status: str,
        completed_steps: list[str] | None,
        step_ids: list[str] | None,
        execution_mode: str = V2_EXECUTION_MODE,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        """Record what a v2 run reported, into the bound Amplifier session.

        The library returns ``completed_steps`` and never persists them
        itself. Without this, a later ``resume`` would have to guess whether
        any step had already run -- and guessing "none" would re-run completed
        steps behind the caller's back. Recorded under
        :data:`V2_RUN_STATE_KEY` so it cannot collide with the legacy
        executor's own ``completed_steps`` bookkeeping.

        Never raises: a bookkeeping failure must not fail a run that already
        happened. It is logged loudly instead, and its absence is what makes
        ``resume`` refuse rather than assume (see ``_resume_v2_recipe``).
        """
        if session_id is None:
            return
        record = {
            "execution_mode": execution_mode,
            "recipe_path": str(recipe_path),
            "schema_version": declared_schema_version(recipe_path),
            "run_id": run_id,
            "status": status,
            "completed_steps": completed_steps,
            "step_ids": step_ids,
        }
        try:
            state = self.session_manager.load_state(session_id, project_path)
            state[V2_RUN_STATE_KEY] = record
            # Cross-surface identity (lib.v1 Core 7): the plan's dependency
            # identity and per-agent provenance, persisted verbatim so this
            # run is comparable against `recipe-runner plan --json`.
            if provenance is not None:
                state[V2_PROVENANCE_STATE_KEY] = provenance
            state["recipe_path"] = str(recipe_path)
            self.session_manager.save_state(session_id, project_path, state)
        except Exception as exc:
            logger.error(
                "Could not record the v2 run outcome for session %s (%s); a later "
                "`resume` of it will refuse rather than guess which steps completed.",
                session_id,
                exc,
                exc_info=True,
            )

    def _v2_tool_result(
        self,
        result: Any,
        runner: Any,
        recipe_path: Path,
        recipe_name: str,
        session_id: str | None,
        execution_mode: str = V2_EXECUTION_MODE,
    ) -> ToolResult:
        """Translate the library's ``RunResult`` into a tool result.

        Every non-success status is reported as itself; a paused or failed run
        never reads as a completed one (``recipe-runner-lib.v1`` Core 8).
        """
        status = result.status
        plan = result.plan
        output: dict[str, Any] = {
            "recipe": recipe_name,
            "execution_mode": execution_mode,
            "schema_version": declared_schema_version(recipe_path),
            "run_id": result.run_id,
            "session_id": session_id,
            "completed_steps": list(result.completed_steps),
            # Which source served this run's model roles. Named on every v2
            # run so the session-default fallback is visible rather than
            # inferred from behaviour.
            "provider_roles": provider_roles_label(self.coordinator),
        }
        if plan is not None:
            # Core 7 provenance: which dependency supplied each agent.
            output["agent_provenance"] = {
                name: provenance.supplied_by for name, provenance in plan.agents.items()
            }
            output["dependencies"] = [
                {
                    "uri": dependency.uri,
                    "resolved_revision": dependency.resolved_revision,
                    "content_digest": dependency.content_digest,
                }
                for dependency in plan.dependencies
            ]

        if status == runner.RunStatus.SUCCEEDED:
            output["status"] = "completed"
            output["summary"] = {
                key: _truncate_value(value) for key, value in result.outputs.items()
            }
            return ToolResult(success=True, output=output)

        if status == runner.RunStatus.PAUSED:
            output["status"] = "paused_for_approval"
            output["stage_name"] = result.pending_approval
            output["message"] = (
                f"Recipe paused at stage '{result.pending_approval}'. "
                "Use 'approve' or 'deny' to continue, then 'resume'."
            )
            return ToolResult(success=True, output=output)

        if status == runner.RunStatus.CANCELLED:
            output["status"] = "cancelled"
            output["message"] = "Recipe run was cancelled by the host."
            return ToolResult(success=True, output=output)

        output["status"] = "failed"
        error = result.error
        return ToolResult(
            success=False,
            output=output,
            error={
                "message": f"Recipe execution failed: {error}",
                "type": type(error).__name__ if error is not None else "RunFailed",
                "remedy": getattr(error, "remedy", None),
            },
        )

    async def _execute_legacy_recipe(
        self,
        recipe_path: Path,
        context_vars: dict[str, Any],
        project_path: Path,
    ) -> ToolResult:
        """Execute a legacy (no ``schema_version``) recipe.

        Behavior here is frozen: ``conformance/legacy-compat`` pins this path's
        outcome and agent provenance byte-for-byte (manifest.v1 Core 10). Agents
        resolve from the caller's map, which is exactly what schema v2 exists to
        end -- but changing it here would break every recipe that works today.
        """
        # Load recipe
        try:
            recipe = Recipe.from_yaml(recipe_path)
        except Exception as e:
            return ToolResult(
                success=False, error={"message": f"Failed to load recipe: {str(e)}"}
            )

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

        # Plan-time agent preflight. A legacy recipe binds `agent:` to the
        # CALLER's map, so one referencing an agent this bundle does not mount
        # is already doomed -- it just does not find out until the first agent
        # step, where it dies on a bare "not found" that names neither the
        # bundle nor a remedy. Fail here instead, before any step runs.
        #
        # This changes behavior ONLY for runs that were going to fail anyway:
        # when every referenced agent is present (or the host exposes no
        # readable registry) the preflight is a no-op, which is what keeps
        # `conformance/legacy-compat` byte-identical.
        preflight = check_legacy_agents_available(recipe, self.coordinator)
        if preflight is not None:
            missing, message = preflight
            return ToolResult(
                success=False,
                error={
                    "message": message,
                    "type": "LegacyAgentsUnavailable",
                    "missing_agents": missing,
                },
            )

        # Execute recipe (pass recipe_path for sub-recipe resolution)
        try:
            final_context = await self.executor.execute_recipe(
                recipe, context_vars, project_path, recipe_path=recipe_path
            )

            # Extract compact summary instead of returning full context
            # Full context is saved in session files and can be massive (1MB+)
            result_summary = _extract_result_summary(final_context, recipe=recipe)

            return ToolResult(
                success=True,
                output={
                    "status": "completed",
                    "recipe": recipe.name,
                    "session_id": final_context["session"]["id"],
                    "summary": result_summary,
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
        """Resume an interrupted recipe session, on the engine that ran it.

        The recipe recorded in the session decides, exactly as it does for
        ``execute``: a session holding a ``schema_version`` recipe resumes
        through the runner library (``_resume_v2_recipe``), one holding a
        legacy recipe resumes on the legacy caller-bound path. A v2 session is
        never resumed on the legacy path -- that would re-bind its agents to
        this caller (``recipe-dependency-manifest.v1`` Core 3).
        """
        session_id = input.get("session_id")
        if not session_id:
            return ToolResult(
                success=False,
                error={"message": "session_id is required for resume operation"},
            )

        project_path = self._get_working_dir()

        # Check session exists
        if not self.session_manager.session_exists(session_id, project_path):
            return ToolResult(
                success=False,
                error={"message": f"Session not found: {session_id}"},
            )

        # Validate session exists and recover recipe_path for sub-recipe resolution
        try:
            state = self.session_manager.load_state(session_id, project_path)
        except Exception as e:
            return ToolResult(
                success=False, error={"message": f"Failed to load session: {str(e)}"}
            )

        recipe_path_str = state.get("recipe_path")
        original_recipe_path = Path(recipe_path_str) if recipe_path_str else None

        # Load recipe from session
        session_dir = self.session_manager.get_session_dir(session_id, project_path)
        recipe_file = session_dir / "recipe.yaml"

        if not recipe_file.exists():
            return ToolResult(
                success=False,
                error={"message": f"Recipe file not found in session: {session_id}"},
            )

        if is_v2_recipe(recipe_file):
            return await self._resume_v2_recipe(
                session_id, project_path, recipe_file, original_recipe_path, state
            )

        warn_legacy_recipe(original_recipe_path or recipe_file)

        try:
            recipe = Recipe.from_yaml(recipe_file)
        except Exception as e:
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={"message": f"Failed to load recipe from session: {str(e)}"},
                ),
                LEGACY_EXECUTION_MODE,
            )

        # Resume execution (recipe_path recovered from state for sub-recipe resolution)
        try:
            final_context = await self.executor.execute_recipe(
                recipe,
                context_vars={},
                project_path=project_path,
                session_id=session_id,
                recipe_path=original_recipe_path,
            )

            # Extract compact summary instead of returning full context
            # Full context is saved in session files and can be massive (1MB+)
            result_summary = _extract_result_summary(final_context, recipe=recipe)

            return label_execution_mode(
                ToolResult(
                    success=True,
                    output={
                        "status": "completed",
                        "recipe": recipe.name,
                        "session_id": session_id,
                        "summary": result_summary,
                    },
                ),
                LEGACY_EXECUTION_MODE,
            )
        except ApprovalGatePausedError as e:
            # Recipe paused at another approval gate
            return label_execution_mode(
                ToolResult(
                    success=True,
                    output={
                        "status": "paused_for_approval",
                        "recipe": recipe.name,
                        "session_id": e.session_id,
                        "stage_name": e.stage_name,
                        "approval_prompt": e.approval_prompt,
                        "message": f"Recipe paused at stage '{e.stage_name}'. Use 'approve' or 'deny' to continue.",
                    },
                ),
                LEGACY_EXECUTION_MODE,
            )
        except Exception as e:
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={
                        "message": f"Failed to resume recipe: {str(e)}",
                        "type": type(e).__name__,
                    },
                ),
                LEGACY_EXECUTION_MODE,
            )

    async def _resume_v2_recipe(
        self,
        session_id: str,
        project_path: Path,
        recipe_file: Path,
        original_recipe_path: Path | None,
        state: dict[str, Any],
    ) -> ToolResult:
        """Resume a schema-v2 run, through the runner library and only it.

        Resuming a v2 recipe on the legacy path would re-bind its agents to
        *this* session instead of its declared dependencies
        (``recipe-dependency-manifest.v1`` Core 3), so that never happens here:
        every outcome below is either a library call or a refusal.

        What the recorded run reported decides which:

        * it finished -- nothing to resume, and saying so is the right answer,
          not a failure;
        * nothing completed -- resuming *is* running from the start, which is
          one library call against the recorded ``run_id``;
        * some steps completed -- skipping them needs the library's ``resume``
          entry point (:func:`runner_adapter.library_resume`); without it the
          resume is refused, naming the missing seam;
        * the run recorded nothing usable -- refused, because assuming "no
          step ran" would re-run steps that did.
        """
        try:
            load_runner()
        except RecipeRunnerUnavailableError as exc:
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={"message": str(exc), "type": type(exc).__name__},
                ),
                V2_EXECUTION_MODE,
            )

        schema_version = declared_schema_version(recipe_file)
        record = state.get(V2_RUN_STATE_KEY)
        if not isinstance(record, dict):
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={
                        "message": (
                            f"Session {session_id} holds a schema_version "
                            f"{schema_version!r} recipe but recorded no run outcome, so "
                            "which of its steps completed is unknown. It was NOT resumed: "
                            "assuming none had run would re-execute steps that did. Re-run "
                            "the recipe with the `execute` operation to start a fresh run."
                        ),
                        "type": "V2RunNotRecorded",
                    },
                ),
                V2_EXECUTION_MODE,
            )

        run_id = record.get("run_id")
        completed_steps = record.get("completed_steps")
        step_ids = record.get("step_ids") or []

        if record.get("status") == "succeeded":
            return label_execution_mode(
                ToolResult(
                    success=True,
                    output={
                        "status": "nothing_to_resume",
                        "recipe": recipe_display_name(recipe_file),
                        "execution_mode": V2_EXECUTION_MODE,
                        "schema_version": schema_version,
                        "session_id": session_id,
                        "run_id": run_id,
                        "completed_steps": completed_steps or [],
                        "message": (
                            "This run already completed every recorded step; there is "
                            "nothing to resume."
                        ),
                    },
                ),
                V2_EXECUTION_MODE,
            )

        if completed_steps is None:
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={
                        "message": (
                            f"Run {run_id or '(unrecorded)'} in session {session_id} "
                            f"ended with status {record.get('status')!r} without recording "
                            "which steps completed, so it cannot be resumed without "
                            "risking re-running steps that already ran. Re-run the recipe "
                            "with the `execute` operation to start a fresh run."
                        ),
                        "type": "V2CompletedStepsUnknown",
                    },
                ),
                V2_EXECUTION_MODE,
            )

        # Prefer the recipe the run actually recorded: a library `resume` checks
        # recorded provenance against a fresh plan of it, and handing over the
        # session's frozen copy instead would make that check unable to ever
        # detect an edited recipe (Core 8).
        recorded_path = record.get("recipe_path")
        resume_path = Path(recorded_path) if recorded_path else original_recipe_path
        if resume_path is None or not resume_path.exists():
            resume_path = recipe_file

        try:
            result = await resume_v2_recipe(
                self.coordinator,
                self.session_manager,
                resume_path,
                {},
                project_path,
                session_id=session_id,
                run_id=run_id,
                completed_steps=tuple(completed_steps),
            )
        except V2ResumeUnavailableError as exc:
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={
                        "message": exc.message,
                        "type": type(exc).__name__,
                        "remedy": exc.remedy,
                        "completed_steps": list(completed_steps),
                        "step_ids": list(step_ids),
                    },
                ),
                V2_EXECUTION_MODE,
            )
        except Exception as exc:
            logger.error("v2 recipe resume failed: %s", exc, exc_info=True)
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={
                        "message": f"Failed to resume recipe: {exc}",
                        "type": type(exc).__name__,
                    },
                ),
                V2_EXECUTION_MODE,
            )

        plan = result.plan
        self._record_v2_run(
            session_id,
            project_path,
            resume_path,
            run_id=result.run_id,
            status=getattr(result.status, "name", str(result.status)).lower(),
            completed_steps=list(result.completed_steps),
            step_ids=list(plan.step_ids) if plan is not None else step_ids or None,
        )

        runner = load_runner()
        return label_execution_mode(
            self._v2_tool_result(
                result,
                runner,
                resume_path,
                recipe_display_name(recipe_file),
                session_id,
            ),
            V2_EXECUTION_MODE,
        )

    async def _list_sessions(self, input: dict[str, Any]) -> ToolResult:
        """List active recipe sessions."""
        project_path = self._get_working_dir()

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
            return ToolResult(
                success=False,
                error={"message": "recipe_path is required for validate operation"},
            )

        # Resolve @mention paths (e.g., @recipes:examples/code-review.yaml)
        recipe_path = self._resolve_path(recipe_path_str)
        if recipe_path is None:
            return ToolResult(
                success=False,
                error={
                    "message": f"Could not resolve @mention path: {recipe_path_str}"
                },
            )

        if is_v2_recipe(recipe_path):
            return await self._validate_v2_recipe(recipe_path)

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

    async def _validate_v2_recipe(self, recipe_path: Path) -> ToolResult:
        """Validate a schema-v2 recipe through the runner library.

        The library owns manifest parsing and dependency resolution
        (``recipe-runner-lib.v1`` Core 1), so validation asks it rather than
        growing a second opinion here: manifest parse plus plan preflight,
        executing nothing and carrying no host services -- and therefore no
        caller agent map. The legacy validator is never consulted for a v2
        recipe; it ignores the `dependencies` manifest entirely and would call
        such a recipe "valid" while knowing nothing about what it resolves to.

        The result keeps the legacy validate operation's shape -- ``status``
        and ``warnings`` on success, ``errors``/``warnings`` in ``error`` on
        failure -- so a caller does not have to branch on schema version to
        read it. Each finding additionally carries the library's typed
        ``code`` and ``remedy``.
        """
        try:
            report = await validate_v2_recipe(recipe_path)
        except RecipeRunnerUnavailableError as exc:
            return label_execution_mode(
                ToolResult(
                    success=False,
                    error={"message": str(exc), "type": type(exc).__name__},
                ),
                V2_EXECUTION_MODE,
            )

        schema_version = (
            report.schema_version
            if report.schema_version is not None
            else declared_schema_version(recipe_path)
        )
        warnings = [_validation_issue_dict(issue) for issue in report.warnings]

        if report.ok:
            return label_execution_mode(
                ToolResult(
                    success=True,
                    output={
                        "status": "valid",
                        "recipe": recipe_display_name(recipe_path),
                        "execution_mode": V2_EXECUTION_MODE,
                        "schema_version": schema_version,
                        "warnings": warnings,
                    },
                ),
                V2_EXECUTION_MODE,
            )

        errors = [_validation_issue_dict(issue) for issue in report.errors]
        return label_execution_mode(
            ToolResult(
                success=False,
                error={
                    "message": "Recipe validation failed",
                    "type": errors[0]["code"] if errors else "ValidationFailed",
                    "schema_version": schema_version,
                    "errors": errors,
                    "warnings": warnings,
                },
            ),
            V2_EXECUTION_MODE,
        )

    async def _list_approvals(self, input: dict[str, Any]) -> ToolResult:
        """List pending approvals across all sessions."""
        project_path = self._get_working_dir()

        try:
            pending_approvals = self.session_manager.list_pending_approvals(
                project_path
            )

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
        message = input.get("message", "")

        if not session_id:
            return ToolResult(
                success=False,
                error={"message": "session_id is required for approve operation"},
            )
        if not stage_name:
            return ToolResult(
                success=False,
                error={"message": "stage_name is required for approve operation"},
            )

        project_path = self._get_working_dir()

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

            # Store the approval message in session state so the executor
            # can inject it into the recipe context on resume
            state = self.session_manager.load_state(session_id, project_path)
            state["_approval_message"] = message
            self.session_manager.save_state(session_id, project_path, state)

            # Forward approval to child session if one is pending
            if state.get("pending_child_approval"):
                self._forward_approval(
                    session_id=session_id,
                    project_path=project_path,
                    message=message,
                )

            return ToolResult(
                success=True,
                output={
                    "status": "approved",
                    "session_id": session_id,
                    "stage_name": stage_name,
                    "approval_message": message,
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
            return ToolResult(
                success=False,
                error={"message": "session_id is required for deny operation"},
            )
        if not stage_name:
            return ToolResult(
                success=False,
                error={"message": "stage_name is required for deny operation"},
            )

        project_path = self._get_working_dir()

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

            # Forward denial to child session if one is pending
            state = self.session_manager.load_state(session_id, project_path)
            if state.get("pending_child_approval"):
                self._forward_denial(
                    session_id=session_id,
                    project_path=project_path,
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

    def _forward_approval(
        self, session_id: str, project_path: Path, message: str = ""
    ) -> None:
        """Forward approval from a parent session to its pending child session.

        Loads parent state and extracts ``pending_child_approval``.  Returns
        early (idempotent) when no such metadata is present.  Otherwise sets
        the child stage to APPROVED, propagates ``_approval_message`` into the
        child state, and recursively forwards if the child itself has a
        ``pending_child_approval`` (grandchild scenario).  Finally clears the
        parent's ``pending_child_approval`` metadata.

        Args:
            session_id: Parent session identifier.
            project_path: Project directory.
            message: Approval message to propagate (default: ``""``).
        """
        state = self.session_manager.load_state(session_id, project_path)
        pca = state.get("pending_child_approval")
        if not pca:
            return

        child_session_id = pca["child_session_id"]
        child_stage_name = pca["child_stage_name"]

        # Approve the child stage
        self.session_manager.set_stage_approval_status(
            session_id=child_session_id,
            project_path=project_path,
            stage_name=child_stage_name,
            status=ApprovalStatus.APPROVED,
            reason="Approved by user",
        )

        # Propagate _approval_message into child state
        child_state = self.session_manager.load_state(child_session_id, project_path)
        child_state["_approval_message"] = message
        self.session_manager.save_state(child_session_id, project_path, child_state)

        # Recursive: forward to grandchild if the child also has pending_child_approval
        if child_state.get("pending_child_approval"):
            self._forward_approval(child_session_id, project_path, message=message)

        # Clear parent's pending_child_approval metadata
        state.pop("pending_child_approval", None)
        self.session_manager.save_state(session_id, project_path, state)

    def _forward_denial(
        self,
        session_id: str,
        project_path: Path,
        reason: str = "Denied by user",
    ) -> None:
        """Forward denial from a parent session to its pending child session.

        Loads parent state and extracts ``pending_child_approval``.  Returns
        early (idempotent) when no such metadata is present.  Otherwise sets
        the child stage to DENIED, clears the child's pending approval, and
        recursively forwards if the child itself has a
        ``pending_child_approval`` (grandchild scenario).  Finally clears the
        parent's ``pending_child_approval`` metadata.

        Args:
            session_id: Parent session identifier.
            project_path: Project directory.
            reason: Denial reason to propagate (default: ``"Denied by user"``).
        """
        state = self.session_manager.load_state(session_id, project_path)
        pca = state.get("pending_child_approval")
        if not pca:
            return

        child_session_id = pca["child_session_id"]
        child_stage_name = pca["child_stage_name"]

        # Deny the child stage
        self.session_manager.set_stage_approval_status(
            session_id=child_session_id,
            project_path=project_path,
            stage_name=child_stage_name,
            status=ApprovalStatus.DENIED,
            reason=reason,
        )

        # Clear the child's pending approval
        self.session_manager.clear_pending_approval(child_session_id, project_path)

        # Recursive: forward to grandchild if the child also has pending_child_approval
        child_state = self.session_manager.load_state(child_session_id, project_path)
        if child_state.get("pending_child_approval"):
            self._forward_denial(child_session_id, project_path, reason=reason)

        # Clear parent's pending_child_approval metadata
        state.pop("pending_child_approval", None)
        self.session_manager.save_state(session_id, project_path, state)

    async def _cancel_recipe(self, input: dict[str, Any]) -> ToolResult:
        """Cancel a running recipe session.

        First cancellation request triggers graceful cancellation (complete current step).
        Second request (or immediate=True) triggers immediate cancellation.
        Cancelled sessions can be resumed later.
        """
        session_id = input.get("session_id")
        immediate = input.get("immediate", False)

        if not session_id:
            return ToolResult(
                success=False,
                error={"message": "session_id is required for cancel operation"},
            )

        project_path = self._get_working_dir()

        # Verify session exists
        if not self.session_manager.session_exists(session_id, project_path):
            return ToolResult(
                success=False,
                error={"message": f"Session not found: {session_id}"},
            )

        # Check current cancellation status
        from .session import CancellationStatus

        current_status = self.session_manager.get_cancellation_status(
            session_id, project_path
        )

        if current_status == CancellationStatus.CANCELLED:
            return ToolResult(
                success=False,
                error={
                    "message": f"Session already cancelled: {session_id}. Use 'resume' to restart.",
                },
            )

        # Request cancellation
        success, message = self.session_manager.request_cancellation(
            session_id, project_path, immediate=immediate
        )

        if not success:
            return ToolResult(
                success=False,
                error={"message": message},
            )

        # Determine the cancellation level
        new_status = self.session_manager.get_cancellation_status(
            session_id, project_path
        )
        level = (
            "immediate" if new_status == CancellationStatus.IMMEDIATE else "graceful"
        )

        return ToolResult(
            success=True,
            output={
                "status": "cancellation_requested",
                "session_id": session_id,
                "level": level,
                "message": message,
                "next_steps": (
                    "Recipe will stop immediately."
                    if level == "immediate"
                    else "Recipe will stop after current step completes. "
                    "Send another cancel request (or use immediate=true) for immediate cancellation."
                ),
                "resume_info": "Use 'resume' operation to restart the recipe from where it stopped.",
            },
        )
