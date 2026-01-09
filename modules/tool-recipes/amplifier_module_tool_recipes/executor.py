"""Recipe execution engine."""

import asyncio
import datetime
import json
import os
import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from .expression_evaluator import ExpressionError
from .expression_evaluator import evaluate_condition
from .models import BackoffConfig
from .models import OrchestratorConfig
from .models import RateLimitingConfig
from .models import Recipe
from .models import RecursionConfig
from .models import Step
from .session import ApprovalStatus
from .session import SessionManager


@dataclass
class BashResult:
    """Result of a bash command execution."""

    stdout: str
    stderr: str
    exit_code: int


class SkipRemainingError(Exception):
    """Raised when step fails with on_error='skip_remaining'."""

    pass


class ApprovalGatePausedError(Exception):
    """Raised when execution pauses at an approval gate.

    This is not a failure - it signals that the recipe has paused
    waiting for human approval before continuing to the next stage.
    Callers should catch this and handle it appropriately (e.g., notify user).
    """

    def __init__(self, session_id: str, stage_name: str, approval_prompt: str):
        self.session_id = session_id
        self.stage_name = stage_name
        self.approval_prompt = approval_prompt
        super().__init__(f"Execution paused at stage '{stage_name}' awaiting approval")


class CancellationRequestedError(Exception):
    """Raised when cancellation is requested and execution should stop.

    This is similar to ApprovalGatePausedError - it signals that execution
    has been interrupted, but in this case due to a cancellation request.
    The recipe can be resumed later from the last checkpoint.
    """

    def __init__(
        self,
        session_id: str,
        is_immediate: bool,
        current_step: str | None = None,
        message: str | None = None,
    ):
        self.session_id = session_id
        self.is_immediate = is_immediate
        self.current_step = current_step
        level = "immediate" if is_immediate else "graceful"
        step_info = f" at step '{current_step}'" if current_step else ""
        self.message = message or f"Recipe {session_id} cancellation ({level}){step_info}"
        super().__init__(self.message)


@dataclass
class RecursionState:
    """Track recursion across nested recipe executions."""

    current_depth: int = 0
    total_steps: int = 0
    max_depth: int = 5
    max_total_steps: int = 100
    recipe_stack: list[str] = field(default_factory=list)

    def check_depth(self, recipe_name: str) -> None:
        """Raise if depth limit exceeded."""
        if self.current_depth >= self.max_depth:
            raise ValueError(
                f"Recipe recursion depth {self.current_depth} exceeds limit {self.max_depth}. "
                f"Stack: {' -> '.join(self.recipe_stack)}"
            )

    def check_total_steps(self) -> None:
        """Raise if total steps limit exceeded."""
        if self.total_steps >= self.max_total_steps:
            raise ValueError(f"Total steps {self.total_steps} exceeds limit {self.max_total_steps}")

    def increment_steps(self) -> None:
        """Increment total steps counter and check limit."""
        self.total_steps += 1
        self.check_total_steps()

    def enter_recipe(self, recipe_name: str, override_config: RecursionConfig | None = None) -> "RecursionState":
        """
        Create child state for sub-recipe.

        Args:
            recipe_name: Name of recipe being entered
            override_config: Optional per-step recursion config override
        """
        # Use override config if provided, otherwise inherit current limits
        max_depth = override_config.max_depth if override_config else self.max_depth
        max_total_steps = override_config.max_total_steps if override_config else self.max_total_steps

        return RecursionState(
            current_depth=self.current_depth + 1,
            total_steps=self.total_steps,
            max_depth=max_depth,
            max_total_steps=max_total_steps,
            recipe_stack=[*self.recipe_stack, recipe_name],
        )


@dataclass
class BackoffState:
    """Tracks current backoff state for rate limiting."""

    config: BackoffConfig
    current_delay_ms: int = 0
    consecutive_successes: int = 0

    def increase(self) -> None:
        """Increase backoff delay after rate limit hit."""
        if not self.config.enabled:
            return
        if self.current_delay_ms == 0:
            self.current_delay_ms = self.config.initial_delay_ms
        else:
            self.current_delay_ms = min(
                int(self.current_delay_ms * self.config.multiplier),
                self.config.max_delay_ms,
            )
        self.consecutive_successes = 0

    def record_success(self) -> None:
        """Record successful call, potentially reset backoff."""
        if not self.config.enabled:
            return
        self.consecutive_successes += 1
        if self.consecutive_successes >= self.config.reset_after_success:
            self.current_delay_ms = 0
            self.consecutive_successes = 0


class RateLimiter:
    """Global rate limiter shared across recipe tree.

    Controls concurrency and pacing of LLM calls to prevent overwhelming
    provider APIs. Sub-recipes inherit the parent's rate limiter.
    """

    def __init__(self, config: RateLimitingConfig):
        self.config = config
        # Semaphore for concurrency control (high value if None/unlimited)
        max_concurrent = config.max_concurrent_llm or 999999
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.min_delay_ms = config.min_delay_ms
        self.backoff = BackoffState(config=config.backoff)
        self._last_completion: float = 0.0
        self._lock = asyncio.Lock()
        # Stats for observability
        self.stats = {
            "total_acquisitions": 0,
            "total_wait_time_ms": 0,
            "rate_limit_hits": 0,
        }

    async def acquire(self) -> None:
        """Acquire a slot before making LLM call."""
        start = asyncio.get_event_loop().time()
        await self.semaphore.acquire()
        await self._apply_pacing()
        await self._apply_backoff()
        elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000
        self.stats["total_acquisitions"] += 1
        self.stats["total_wait_time_ms"] += elapsed_ms

    def release(self) -> None:
        """Release slot after LLM call completes."""
        self._last_completion = asyncio.get_event_loop().time()
        self.semaphore.release()

    def record_rate_limit(self) -> None:
        """Called when 429 received - increase backoff."""
        self.stats["rate_limit_hits"] += 1
        self.backoff.increase()

    def record_success(self) -> None:
        """Called on success - potentially decrease backoff."""
        self.backoff.record_success()

    async def _apply_pacing(self) -> None:
        """Ensure min_delay_ms between completions."""
        if self.min_delay_ms <= 0:
            return
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed_ms = (now - self._last_completion) * 1000
            if elapsed_ms < self.min_delay_ms:
                await asyncio.sleep((self.min_delay_ms - elapsed_ms) / 1000)

    async def _apply_backoff(self) -> None:
        """Apply current backoff delay if any."""
        delay = self.backoff.current_delay_ms
        if delay > 0:
            await asyncio.sleep(delay / 1000)


class RecipeExecutor:
    """Executes recipe workflows with checkpointing and resumption."""

    def __init__(self, coordinator: Any, session_manager: SessionManager):
        """
        Initialize executor.

        Args:
            coordinator: Amplifier coordinator for agent spawning
            session_manager: Session persistence manager
        """
        self.coordinator = coordinator
        self.session_manager = session_manager

    def _show_progress(self, message: str, level: str = "info") -> None:
        """
        Show progress message to user via display system.

        Args:
            message: Progress message to display
            level: Message level (info, warning, error)
        """
        display_system = getattr(self.coordinator, "display_system", None)
        if display_system is not None:
            display_system.show_message(message=message, level=level, source="recipe")

    def _check_cancellation(
        self,
        session_id: str,
        project_path: Path,
        current_step: str | None = None,
        allow_graceful_completion: bool = False,
    ) -> None:
        """Check if cancellation requested and raise if so.

        This method should be called at loop boundaries (before each step,
        before each loop iteration, etc.) to enable responsive cancellation.

        Args:
            session_id: Current session identifier
            project_path: Project path for session lookup
            current_step: Current step ID for error context
            allow_graceful_completion: If True, only raise on IMMEDIATE cancellation.
                                       Use this when a step is in progress and should
                                       be allowed to complete for graceful cancellation.

        Raises:
            CancellationRequestedError: If cancellation has been requested
        """
        if not self.session_manager.is_cancellation_requested(session_id, project_path):
            return

        is_immediate = self.session_manager.is_immediate_cancellation(session_id, project_path)

        # Graceful cancellation allows current step to complete
        if allow_graceful_completion and not is_immediate:
            return

        raise CancellationRequestedError(
            session_id=session_id,
            is_immediate=is_immediate,
            current_step=current_step,
        )

    def _check_coordinator_cancellation(
        self,
        session_id: str,
        project_path: Path,
    ) -> None:
        """Check if coordinator has cancellation requested (e.g., from SIGINT).

        This integrates with amplifier-core's CancellationToken, allowing
        cancellation signals from the CLI (Ctrl+C) to propagate to recipes.

        Args:
            session_id: Current session identifier
            project_path: Project path for session lookup
        """
        # Check if coordinator has a cancellation token
        cancellation = getattr(self.coordinator, "cancellation", None)
        if cancellation is None:
            return

        if not cancellation.is_cancelled:
            return

        # Propagate coordinator cancellation to session state
        is_immediate = cancellation.is_immediate
        self.session_manager.request_cancellation(session_id, project_path, immediate=is_immediate)

    async def execute_recipe(
        self,
        recipe: Recipe,
        context_vars: dict[str, Any],
        project_path: Path,
        session_id: str | None = None,
        recipe_path: Path | None = None,
        recursion_state: RecursionState | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        parent_session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute recipe with checkpointing and resumption.

        Args:
            recipe: Recipe to execute
            context_vars: Initial context variables (merged with recipe.context)
            project_path: Current project directory
            session_id: Optional session ID to resume
            recipe_path: Optional path to recipe file (saved to session)
            recursion_state: Optional recursion tracking state (for nested recipes)
            rate_limiter: Optional rate limiter (inherited from parent recipe)
            orchestrator_config: Optional orchestrator config (inherited from parent recipe)
            parent_session_id: Parent session ID for cancellation checks in sub-recipes

        Returns:
            Final context dict with all step outputs
        """
        # Initialize or inherit recursion state
        if recursion_state is None:
            # Top-level recipe: create initial state from recipe config
            config = recipe.recursion or RecursionConfig()
            recursion_state = RecursionState(
                current_depth=0,
                total_steps=0,
                max_depth=config.max_depth,
                max_total_steps=config.max_total_steps,
                recipe_stack=[recipe.name],
            )
        else:
            # Sub-recipe: check depth before entering
            recursion_state.check_depth(recipe.name)

        # Initialize or inherit rate limiter
        # Rate limiter is created at root recipe and inherited by sub-recipes
        # Sub-recipes CANNOT override parent's rate limits (parent wins)
        if rate_limiter is None and recipe.rate_limiting:
            rate_limiter = RateLimiter(recipe.rate_limiting)

        # Initialize or inherit orchestrator config
        # Like rate_limiter, created at root recipe and inherited by sub-recipes
        if orchestrator_config is None and recipe.orchestrator:
            orchestrator_config = recipe.orchestrator

        # Create or resume session
        is_resuming = session_id is not None

        # Route to staged execution EARLY (staged recipes have different state structure)
        if recipe.is_staged:
            # For staged recipes, load minimal state for metadata, let _execute_staged_recipe handle the rest
            if is_resuming:
                state = self.session_manager.load_state(session_id, project_path)
                context = state["context"]
                session_started = state["started"]
            else:
                session_id = self.session_manager.create_session(recipe, project_path, recipe_path)
                context = {**recipe.context, **context_vars}
                session_started = datetime.datetime.now().isoformat()

            # Add metadata to context
            context["recipe"] = {
                "name": recipe.name,
                "version": recipe.version,
                "description": recipe.description,
            }
            context["session"] = {
                "id": session_id,
                "started": session_started,
                "project": str(project_path.resolve()),
            }

            return await self._execute_staged_recipe(
                recipe=recipe,
                context=context,
                project_path=project_path,
                session_id=session_id,
                recipe_path=recipe_path,
                recursion_state=recursion_state,
                is_resuming=is_resuming,
                rate_limiter=rate_limiter,
                orchestrator_config=orchestrator_config,
            )

        # Flat recipe state loading (uses current_step_index)
        if is_resuming:
            state = self.session_manager.load_state(session_id, project_path)
            current_step_index = state["current_step_index"]
            context = state["context"]
            completed_steps = state.get("completed_steps", [])
            session_started = state["started"]
        else:
            session_id = self.session_manager.create_session(recipe, project_path, recipe_path)
            current_step_index = 0
            context = {**recipe.context, **context_vars}
            completed_steps = []
            session_started = datetime.datetime.now().isoformat()

        # Effective session ID for cancellation checks
        # For sub-recipes (session_id=None), use parent_session_id to inherit cancellation state
        cancellation_session_id = session_id or parent_session_id

        # Show recipe start progress
        total_steps = len(recipe.steps)
        self._show_progress(f"📋 Starting recipe: {recipe.name} ({total_steps} steps)")

        # Add metadata to context
        context["recipe"] = {
            "name": recipe.name,
            "version": recipe.version,
            "description": recipe.description,
        }
        context["session"] = {
            "id": session_id,
            "started": session_started,
            "project": str(project_path.resolve()),
        }

        # Initialize state for exception handler (will be set during execution)
        state: dict[str, Any] | None = None

        # Flat mode execution (staged recipes already returned above)
        try:
            # Execute remaining steps
            for i in range(current_step_index, len(recipe.steps)):
                step = recipe.steps[i]

                # Check for cancellation before starting each step
                # Use cancellation_session_id to support both root recipes and sub-recipes
                if cancellation_session_id:
                    self._check_coordinator_cancellation(cancellation_session_id, project_path)
                    self._check_cancellation(cancellation_session_id, project_path, current_step=step.id)

                # Add step metadata to context
                context["step"] = {"id": step.id, "index": i}

                # Show step progress
                step_num = i + 1
                step_type = step.type or "agent"
                self._show_progress(f"  [{step_num}/{total_steps}] {step.id} ({step_type})")

                # Check condition if present
                if step.condition:
                    try:
                        condition_result = evaluate_condition(step.condition, context)
                    except ExpressionError as e:
                        raise ValueError(f"Step '{step.id}': condition error: {e}") from e

                    if not condition_result:
                        # Skip this step - record in state but don't execute
                        skipped_steps = context.get("_skipped_steps", [])
                        skipped_steps.append(step.id)
                        context["_skipped_steps"] = skipped_steps
                        continue

                # Handle foreach loops
                if step.foreach:
                    try:
                        await self._execute_loop(
                            step, context, project_path, recursion_state, recipe_path,
                            rate_limiter, orchestrator_config, session_id=cancellation_session_id
                        )
                        # Update completed steps and session state after loop completes
                        completed_steps.append(step.id)
                        state = {
                            "session_id": session_id,
                            "recipe_name": recipe.name,
                            "recipe_version": recipe.version,
                            "started": context["session"]["started"],
                            "current_step_index": i + 1,
                            "context": context,
                            "completed_steps": completed_steps,
                            "project_path": str(project_path.resolve()),
                        }
                        self.session_manager.save_state(session_id, project_path, state)
                        continue
                    except SkipRemainingError:
                        break

                # Execute step based on type (agent, recipe, or bash)
                try:
                    if step.type == "recipe":
                        result = await self._execute_recipe_step(
                            step, context, project_path, recursion_state, recipe_path, rate_limiter, orchestrator_config,
                            parent_session_id=cancellation_session_id
                        )
                    elif step.type == "bash":
                        # Bash steps don't count against agent recursion limits
                        bash_result = await self._execute_bash_step(step, context, project_path)
                        # Store exit code if requested
                        if step.output_exit_code:
                            context[step.output_exit_code] = str(bash_result.exit_code)
                        result = bash_result.stdout
                    else:
                        # Agent step - track for recursion limits
                        recursion_state.increment_steps()
                        result = await self.execute_step_with_retry(
                            step, context, rate_limiter, orchestrator_config,
                            session_id=cancellation_session_id, project_path=project_path
                        )

                    # Process result: unwrap spawn() output and optionally parse JSON
                    result = self._process_step_result(result, step)

                    # Store output if specified
                    if step.output:
                        context[step.output] = result

                    # Update completed steps and session state
                    completed_steps.append(step.id)

                    state = {
                        "session_id": session_id,
                        "recipe_name": recipe.name,
                        "recipe_version": recipe.version,
                        "started": context["session"]["started"],
                        "current_step_index": i + 1,
                        "context": context,
                        "completed_steps": completed_steps,
                        "project_path": str(project_path.resolve()),
                    }

                    # Checkpoint after each step
                    self.session_manager.save_state(session_id, project_path, state)

                except SkipRemainingError:
                    # Skip remaining steps
                    break
                except CancellationRequestedError:
                    # Cancellation requested - save state and re-raise
                    raise

        except CancellationRequestedError as e:
            # Mark session as cancelled and save state for later resumption
            self.session_manager.mark_cancelled(
                session_id, project_path,
                cancelled_at_step=e.current_step,
            )
            if state is not None:
                self.session_manager.save_state(session_id, project_path, state)
            self._show_progress(f"⚠️ Recipe cancelled at step: {e.current_step or 'unknown'}", level="warning")
            raise

        except Exception:
            # Save state even on error for resumption
            if state is not None:
                self.session_manager.save_state(session_id, project_path, state)
            raise

        # Cleanup old sessions
        self.session_manager.cleanup_old_sessions(project_path)

        # Show completion
        self._show_progress(f"✅ Recipe completed: {recipe.name}")

        return context

    async def _execute_staged_recipe(
        self,
        recipe: Recipe,
        context: dict[str, Any],
        project_path: Path,
        session_id: str,
        recipe_path: Path | None,
        recursion_state: RecursionState,
        is_resuming: bool,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
    ) -> dict[str, Any]:
        """
        Execute a staged recipe with approval gates.

        Args:
            recipe: Staged recipe to execute
            context: Current context variables
            project_path: Current project directory
            session_id: Session identifier
            recipe_path: Optional path to recipe file
            recursion_state: Recursion tracking state
            is_resuming: Whether resuming an existing session

        Returns:
            Final context dict with all step outputs

        Raises:
            ApprovalGatePausedError: When execution pauses at an approval gate
        """
        # Load state for resumption
        if is_resuming:
            state = self.session_manager.load_state(session_id, project_path)
            current_stage_index = state.get("current_stage_index", 0)
            current_step_in_stage = state.get("current_step_in_stage", 0)
            completed_stages = state.get("completed_stages", [])
            completed_steps = state.get("completed_steps", [])

            # Check if we're resuming from a pending approval
            pending = self.session_manager.get_pending_approval(session_id, project_path)
            if pending:
                stage_name = pending["stage_name"]
                approval_status = self.session_manager.get_stage_approval_status(session_id, project_path, stage_name)

                # Check for timeout
                timeout_result = self.session_manager.check_approval_timeout(session_id, project_path)
                if timeout_result == ApprovalStatus.TIMEOUT:
                    raise ValueError(f"Approval for stage '{stage_name}' timed out and was denied")
                if timeout_result == ApprovalStatus.APPROVED:
                    # Auto-approved on timeout, clear and continue
                    self.session_manager.clear_pending_approval(session_id, project_path)
                elif approval_status == ApprovalStatus.PENDING:
                    # Still pending - raise to indicate waiting
                    raise ApprovalGatePausedError(
                        session_id=session_id,
                        stage_name=stage_name,
                        approval_prompt=pending["approval_prompt"],
                    )
                elif approval_status == ApprovalStatus.DENIED:
                    raise ValueError(f"Execution denied at stage '{stage_name}'")
                elif approval_status == ApprovalStatus.APPROVED:
                    # Approved, clear pending and continue
                    self.session_manager.clear_pending_approval(session_id, project_path)
        else:
            current_stage_index = 0
            current_step_in_stage = 0
            completed_stages = []
            completed_steps = []

        try:
            # Execute stages
            total_stages = len(recipe.stages)
            for stage_idx in range(current_stage_index, len(recipe.stages)):
                stage = recipe.stages[stage_idx]

                # Check for cancellation before starting each stage
                self._check_coordinator_cancellation(session_id, project_path)
                self._check_cancellation(session_id, project_path, current_step=f"stage:{stage.name}")

                # Show stage progress
                self._show_progress(f"📦 Stage {stage_idx + 1}/{total_stages}: {stage.name}")

                # Add stage metadata to context
                context["stage"] = {
                    "name": stage.name,
                    "index": stage_idx,
                }

                # Determine starting step within this stage
                start_step = current_step_in_stage if stage_idx == current_stage_index else 0

                # Execute steps within this stage
                for step_idx in range(start_step, len(stage.steps)):
                    step = stage.steps[step_idx]

                    # Check for cancellation before starting each step
                    self._check_coordinator_cancellation(session_id, project_path)
                    self._check_cancellation(session_id, project_path, current_step=step.id)

                    # Add step metadata to context
                    context["step"] = {"id": step.id, "index": step_idx, "stage": stage.name}

                    # Check condition if present
                    if step.condition:
                        try:
                            condition_result = evaluate_condition(step.condition, context)
                        except ExpressionError as e:
                            raise ValueError(f"Step '{step.id}': condition error: {e}") from e

                        if not condition_result:
                            skipped_steps = context.get("_skipped_steps", [])
                            skipped_steps.append(step.id)
                            context["_skipped_steps"] = skipped_steps
                            continue

                    # Handle foreach loops
                    if step.foreach:
                        try:
                            await self._execute_loop(
                                step, context, project_path, recursion_state, recipe_path,
                                rate_limiter, orchestrator_config, session_id=session_id
                            )
                            completed_steps.append(step.id)
                            self._save_staged_state(
                                session_id,
                                project_path,
                                recipe,
                                context,
                                stage_idx,
                                step_idx + 1,
                                completed_stages,
                                completed_steps,
                            )
                            continue
                        except SkipRemainingError:
                            break

                    # Execute step based on type (agent, recipe, or bash)
                    try:
                        if step.type == "recipe":
                            result = await self._execute_recipe_step(
                                step, context, project_path, recursion_state, recipe_path, rate_limiter, orchestrator_config,
                                parent_session_id=session_id
                            )
                        elif step.type == "bash":
                            # Bash steps don't count against agent recursion limits
                            bash_result = await self._execute_bash_step(step, context, project_path)
                            # Store exit code if requested
                            if step.output_exit_code:
                                context[step.output_exit_code] = str(bash_result.exit_code)
                            result = bash_result.stdout
                        else:
                            # Agent step - track for recursion limits
                            recursion_state.increment_steps()
                            result = await self.execute_step_with_retry(
                                step, context, rate_limiter, orchestrator_config,
                                session_id=session_id, project_path=project_path
                            )

                        # Process result: unwrap spawn() output and optionally parse JSON
                        result = self._process_step_result(result, step)

                        if step.output:
                            context[step.output] = result

                        completed_steps.append(step.id)
                        self._save_staged_state(
                            session_id,
                            project_path,
                            recipe,
                            context,
                            stage_idx,
                            step_idx + 1,
                            completed_stages,
                            completed_steps,
                        )

                    except SkipRemainingError:
                        break
                    except CancellationRequestedError:
                        # Cancellation requested - re-raise to outer handler
                        raise

                # Stage completed - check for approval gate
                completed_stages.append(stage.name)

                if stage.approval and stage.approval.required:
                    # Save state with next stage as target FIRST
                    # (set_pending_approval will load, add approval fields, and save)
                    self._save_staged_state(
                        session_id, project_path, recipe, context, stage_idx + 1, 0, completed_stages, completed_steps
                    )

                    # Set pending approval AFTER saving state (this loads, modifies, saves)
                    self.session_manager.set_pending_approval(
                        session_id=session_id,
                        project_path=project_path,
                        stage_name=stage.name,
                        prompt=stage.approval.prompt or f"Approve completion of stage '{stage.name}'?",
                        timeout=stage.approval.timeout,
                        default=stage.approval.default,
                    )

                    # Raise to indicate paused state
                    raise ApprovalGatePausedError(
                        session_id=session_id,
                        stage_name=stage.name,
                        approval_prompt=stage.approval.prompt or f"Approve completion of stage '{stage.name}'?",
                    )

                # No approval needed - save progress and continue
                self._save_staged_state(
                    session_id, project_path, recipe, context, stage_idx + 1, 0, completed_stages, completed_steps
                )

        except ApprovalGatePausedError:
            # Re-raise approval pause (not an error)
            raise
        except CancellationRequestedError as e:
            # Mark session as cancelled and save state for later resumption
            self.session_manager.mark_cancelled(
                session_id, project_path,
                cancelled_at_step=e.current_step,
            )
            self._save_staged_state(
                session_id,
                project_path,
                recipe,
                context,
                current_stage_index,
                current_step_in_stage,
                completed_stages,
                completed_steps,
            )
            self._show_progress(f"⚠️ Recipe cancelled at step: {e.current_step or 'unknown'}", level="warning")
            raise
        except Exception:
            # Save state for resumption on error
            self._save_staged_state(
                session_id,
                project_path,
                recipe,
                context,
                current_stage_index,
                current_step_in_stage,
                completed_stages,
                completed_steps,
            )
            raise

        # Cleanup old sessions
        self.session_manager.cleanup_old_sessions(project_path)

        # Show completion
        self._show_progress(f"✅ Recipe completed: {recipe.name}")

        return context

    def _save_staged_state(
        self,
        session_id: str,
        project_path: Path,
        recipe: Recipe,
        context: dict[str, Any],
        stage_index: int,
        step_in_stage: int,
        completed_stages: list[str],
        completed_steps: list[str],
    ) -> None:
        """Save state for staged recipe execution."""
        state = {
            "session_id": session_id,
            "recipe_name": recipe.name,
            "recipe_version": recipe.version,
            "started": context["session"]["started"],
            "current_stage_index": stage_index,
            "current_step_in_stage": step_in_stage,
            "context": context,
            "completed_stages": completed_stages,
            "completed_steps": completed_steps,
            "project_path": str(project_path.resolve()),
            "is_staged": True,
        }
        self.session_manager.save_state(session_id, project_path, state)

    async def execute_step_with_retry(
        self,
        step: Step,
        context: dict[str, Any],
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
        project_path: Path | None = None,
    ) -> Any:
        """
        Execute step with retry logic.

        Args:
            step: Step to execute
            context: Current context variables
            rate_limiter: Optional rate limiter for pacing
            orchestrator_config: Optional orchestrator config for spawned sessions
            session_id: Session identifier for cancellation checks
            project_path: Project path for cancellation checks

        Returns:
            Step result

        Raises:
            Exception if all retries fail and on_error='fail'
            SkipRemainingError if on_error='skip_remaining'
            CancellationRequestedError if cancellation requested
        """
        retry_config = step.retry or {}
        max_attempts = retry_config.get("max_attempts", 1)
        backoff = retry_config.get("backoff", "exponential")
        delay = retry_config.get("initial_delay", 5)
        max_delay = retry_config.get("max_delay", 300)

        last_error = None

        for attempt in range(max_attempts):
            # Check for cancellation before each attempt
            if session_id and project_path:
                self._check_coordinator_cancellation(session_id, project_path)
                self._check_cancellation(session_id, project_path, current_step=step.id)
            try:
                # Acquire rate limiter slot if configured
                if rate_limiter:
                    await rate_limiter.acquire()

                try:
                    result = await self.execute_step(step, context, orchestrator_config)
                    # Record success for backoff tracking
                    if rate_limiter:
                        rate_limiter.record_success()
                    return result
                finally:
                    # Always release rate limiter slot
                    if rate_limiter:
                        rate_limiter.release()

            except Exception as e:
                last_error = e

                # Check if this is a rate limit error (429)
                error_str = str(e).lower()
                is_rate_limit = "429" in error_str or "rate limit" in error_str
                if is_rate_limit and rate_limiter:
                    rate_limiter.record_rate_limit()

                # If final attempt or not retryable
                if attempt == max_attempts - 1:
                    # Handle based on on_error strategy
                    if step.on_error == "fail":
                        raise
                    if step.on_error == "continue":
                        return None  # Continue with None result
                    if step.on_error == "skip_remaining":
                        raise SkipRemainingError() from e

                # Wait before retry
                await asyncio.sleep(min(delay, max_delay))

                # Adjust delay for next attempt
                if backoff == "exponential":
                    delay *= 2
                # Linear backoff keeps same delay

        # Shouldn't reach here, but handle just in case
        if step.on_error == "fail" and last_error:
            raise last_error
        return None

    def _extract_json_aggressively(self, output: str) -> Any:
        """
        Aggressively extract JSON from output using multiple strategies.
        
        Only called when parse_json: true is set on the step.
        
        Strategies (in order):
        1. Entire string is valid JSON
        2. Extract from markdown code block (```json ... ```)
        3. Find JSON object/array embedded in text
        
        Args:
            output: String output from agent
            
        Returns:
            Parsed JSON object/array, or original string if no JSON found
        """
        output_stripped = output.strip()
        
        if not output_stripped:
            return output
        
        # Strategy 1: Entire string is valid JSON
        try:
            return json.loads(output_stripped)
        except (json.JSONDecodeError, ValueError):
            pass
        
        # Strategy 2: Extract from markdown code block
        json_match = re.search(
            r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```',
            output_stripped,
            re.DOTALL
        )
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
        
        # Strategy 3: Find JSON embedded in text
        decoder = json.JSONDecoder()
        for start_char in ['{', '[']:
            idx = output_stripped.find(start_char)
            while idx != -1:
                try:
                    parsed, end_idx = decoder.raw_decode(output_stripped, idx)
                    return parsed
                except (json.JSONDecodeError, ValueError):
                    pass
                idx = output_stripped.find(start_char, idx + 1)
        
        # All strategies failed - return as-is
        return output

    def _process_step_result(self, result: Any, step: Step) -> Any:
        """
        Process step result: unwrap spawn() output and optionally parse JSON.
        
        By default, preserves output as-is (prose, markdown, formatting).
        Only parses JSON if:
        - The ENTIRE output is clean JSON (no markdown, no prose), OR
        - The step has parse_json: true set (aggressive extraction)
        
        Args:
            result: Raw result from step execution
            step: Step configuration (to check parse_json flag)
        
        Returns:
            Processed result (unwrapped and/or parsed)
        """
        # Step 1: Unwrap spawn() result if it's a dict with "output" key
        if isinstance(result, dict) and "output" in result:
            output = result["output"]
        else:
            output = result
        
        # Step 2: Parse JSON if requested
        if isinstance(output, str) and step.parse_json:
            # Opt-in aggressive JSON extraction
            return self._extract_json_aggressively(output)
        
        # Step 3: Conservative default - only parse clean JSON
        if isinstance(output, str):
            output_stripped = output.strip()
            if output_stripped:
                try:
                    return json.loads(output_stripped)
                except (json.JSONDecodeError, ValueError):
                    # Step 4: For bash steps, try aggressive parsing as fallback
                    # Bash commands often print status messages before JSON output
                    if step.type == "bash":
                        extracted = self._extract_json_aggressively(output)
                        if extracted != output:  # Successfully extracted JSON
                            return extracted
        
        return output

    async def execute_step(
        self,
        step: Step,
        context: dict[str, Any],
        orchestrator_config: OrchestratorConfig | None = None,
    ) -> Any:
        """
        Execute single step by spawning sub-agent.

        Args:
            step: Step to execute
            context: Current context variables
            orchestrator_config: Optional orchestrator config for spawned sessions

        Returns:
            Step result from agent
        """
        # Get spawn capability from coordinator (registered by app layer)
        # This follows kernel philosophy: modules request capabilities, apps provide them
        spawn_fn = self.coordinator.get_capability("session.spawn")
        if spawn_fn is None:
            raise RuntimeError(
                f"Step '{step.id}' requires agent spawning but 'session.spawn' capability not registered. "
                "Ensure the app layer registers session spawning capabilities."
            )

        # Agent steps must have prompt and agent (validated by models)
        if not step.prompt or not step.agent:
            raise ValueError(f"Step '{step.id}' is an agent step but missing prompt or agent")

        # Substitute variables in prompt
        instruction = self.substitute_variables(step.prompt, context)

        # Add mode if specified
        if step.mode:
            mode_instruction = f"MODE: {step.mode}\n\n"
            instruction = mode_instruction + instruction

        # Add JSON output instruction if parse_json is enabled
        if step.parse_json:
            json_instruction = """

---

**CRITICAL: JSON OUTPUT REQUIRED**

Your response MUST end with a valid JSON object. The recipe system will parse your final JSON output.

Requirements:
1. Your response MUST contain a JSON code block or raw JSON object
2. The JSON must be valid (proper quotes, no trailing commas, etc.)
3. If you include explanation, put the JSON block LAST in your response
4. Use ```json fences or return raw JSON - both work

Example valid endings:
```json
{"key": "value", "count": 5}
```

Or raw JSON at the end:
{"key": "value", "count": 5}

DO NOT return the JSON as a string or with escape characters. Return actual JSON structure.
"""
            instruction = instruction + json_instruction

        # Get parent session and agents config from coordinator
        parent_session = self.coordinator.session
        agents = self.coordinator.config.get("agents", {})

        # Build orchestrator config dict for spawn if present
        orchestrator_dict = orchestrator_config.config if orchestrator_config else None

        # Spawn sub-session with agent via capability
        result = await spawn_fn(
            agent_name=step.agent,
            instruction=instruction,
            parent_session=parent_session,
            agent_configs=agents,
            sub_session_id=None,  # Let spawner generate ID
            orchestrator_config=orchestrator_dict,
        )

        return result

    async def _execute_loop(
        self,
        step: Step,
        context: dict[str, Any],
        project_path: Path,
        recursion_state: RecursionState,
        recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
    ) -> None:
        """
        Execute a step with foreach iteration.

        Simple, fail-fast implementation per philosophy:
        - No checkpointing (restart on failure)
        - No partial completion (fail-fast)
        - Minimal state tracking
        - Optional parallel execution (all iterations concurrently)

        Args:
            step: Step with foreach field
            context: Current context variables
            project_path: Current project directory
            recursion_state: Recursion tracking state
            orchestrator_config: Optional orchestrator config for spawned sessions
            session_id: Session identifier for cancellation checks

        Raises:
            ValueError: If foreach variable invalid or iteration fails
            SkipRemainingError: If on_error='skip_remaining' and iteration fails
            CancellationRequestedError: If cancellation requested
        """
        # Resolve foreach variable (step.foreach is guaranteed non-None by caller)
        assert step.foreach is not None
        items = self._resolve_foreach_variable(step.foreach, context)

        if not isinstance(items, list):
            raise ValueError(f"Step '{step.id}': foreach variable must be a list, got {type(items).__name__}")

        if not items:
            # Empty list - skip step execution but still set output variables
            # This prevents "undefined variable" errors in downstream steps
            skipped_steps = context.get("_skipped_steps", [])
            skipped_steps.append(step.id)
            context["_skipped_steps"] = skipped_steps
            
            # Set collect variable to empty array so downstream steps can check length
            if step.collect:
                context[step.collect] = []
            
            return

        if len(items) > step.max_iterations:
            raise ValueError(f"Step '{step.id}': foreach exceeds max_iterations ({len(items)} > {step.max_iterations})")

        # Get loop variable name
        loop_var = step.as_var or "item"

        if step.parallel:
            # Parallel execution: run all iterations concurrently
            results = await self._execute_loop_parallel(
                step, context, items, loop_var, project_path, recursion_state, recipe_path, rate_limiter, orchestrator_config, session_id
            )
        else:
            # Sequential execution: run iterations one at a time
            results = await self._execute_loop_sequential(
                step, context, items, loop_var, project_path, recursion_state, recipe_path, rate_limiter, orchestrator_config, session_id
            )

        # Store results
        if step.collect:
            context[step.collect] = results
        elif step.output and results:
            context[step.output] = results[-1]  # Last iteration result

    async def _execute_loop_sequential(
        self,
        step: Step,
        context: dict[str, Any],
        items: list[Any],
        loop_var: str,
        project_path: Path,
        recursion_state: RecursionState,
        recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
    ) -> list[Any]:
        """Execute loop iterations sequentially."""
        results = []

        for idx, item in enumerate(items):
            # Check for cancellation before each iteration
            if session_id and project_path:
                self._check_coordinator_cancellation(session_id, project_path)
                self._check_cancellation(session_id, project_path, current_step=f"{step.id}[{idx}]")

            # Set loop variable in context
            context[loop_var] = item

            try:
                # Execute based on step type (agent, recipe, or bash)
                if step.type == "recipe":
                    result = await self._execute_recipe_step(
                        step, context, project_path, recursion_state, recipe_path, rate_limiter, orchestrator_config,
                        parent_session_id=session_id
                    )
                elif step.type == "bash":
                    # Bash steps don't count against agent recursion limits
                    bash_result = await self._execute_bash_step(step, context, project_path)
                    # Store exit code if requested
                    if step.output_exit_code:
                        context[step.output_exit_code] = str(bash_result.exit_code)
                    result = bash_result.stdout
                else:
                    # Agent step - track for recursion limits
                    recursion_state.increment_steps()
                    result = await self.execute_step_with_retry(
                        step, context, rate_limiter, orchestrator_config,
                        session_id=session_id, project_path=project_path
                    )
                
                # Process result: unwrap spawn() output and optionally parse JSON
                result = self._process_step_result(result, step)
                results.append(result)
            except SkipRemainingError:
                # Propagate skip_remaining
                raise
            except CancellationRequestedError:
                # Propagate cancellation
                raise
            except Exception as e:
                # Fail fast - no partial completion in MVP
                raise ValueError(f"Step '{step.id}' iteration {idx} failed: {e}") from e
            finally:
                # Clean up loop variable (scoped to loop only)
                if loop_var in context:
                    del context[loop_var]

        return results

    async def _execute_loop_parallel(
        self,
        step: Step,
        context: dict[str, Any],
        items: list[Any],
        loop_var: str,
        project_path: Path,
        recursion_state: RecursionState,
        recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
    ) -> list[Any]:
        """
        Execute loop iterations in parallel using asyncio.gather.

        Each iteration gets its own context copy to avoid conflicts.
        Results are returned in the same order as input items.
        Fail-fast: if any iteration fails, the entire step fails.

        Supports bounded parallelism:
        - parallel: true -> unbounded (all at once)
        - parallel: N (int) -> max N concurrent iterations

        Rate limiting is applied via the rate_limiter if configured.
        """
        # Check for cancellation before starting parallel execution
        if session_id and project_path:
            self._check_coordinator_cancellation(session_id, project_path)
            self._check_cancellation(session_id, project_path, current_step=f"{step.id}[parallel]")

        # For agent steps, pre-check total steps limit (all will run in parallel)
        if step.type == "agent":
            if recursion_state.total_steps + len(items) > recursion_state.max_total_steps:
                raise ValueError(
                    f"Parallel loop would exceed max_total_steps "
                    f"({recursion_state.total_steps} + {len(items)} > {recursion_state.max_total_steps})"
                )
            # Pre-increment for all iterations
            recursion_state.total_steps += len(items)

        # Determine concurrency limit
        # parallel: true -> None (unbounded)
        # parallel: N (int) -> N concurrent
        if step.parallel is True:
            max_concurrent = None
        elif isinstance(step.parallel, int):
            max_concurrent = step.parallel
        else:
            max_concurrent = None  # Shouldn't reach here after validation

        # Create semaphore for bounded concurrency (None = unbounded)
        semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent else None

        async def execute_iteration(idx: int, item: Any) -> Any:
            """Execute a single iteration with isolated context."""
            # Copy context and set loop variable for this iteration
            iter_context = {**context, loop_var: item}

            try:
                # Execute based on step type (agent, recipe, or bash)
                if step.type == "recipe":
                    result = await self._execute_recipe_step(
                        step, iter_context, project_path, recursion_state, recipe_path, rate_limiter, orchestrator_config,
                        parent_session_id=session_id
                    )
                elif step.type == "bash":
                    # Bash steps don't count against agent recursion limits
                    bash_result = await self._execute_bash_step(step, iter_context, project_path)
                    # Store exit code if requested (in iteration context)
                    if step.output_exit_code:
                        iter_context[step.output_exit_code] = str(bash_result.exit_code)
                    result = bash_result.stdout
                else:
                    # Agent step - rate limiting handled inside execute_step_with_retry
                    result = await self.execute_step_with_retry(
                        step, iter_context, rate_limiter, orchestrator_config,
                        session_id=session_id, project_path=project_path
                    )
                
                # Process result: unwrap spawn() output and optionally parse JSON
                return self._process_step_result(result, step)
            except SkipRemainingError:
                raise
            except CancellationRequestedError:
                raise
            except Exception as e:
                raise ValueError(f"Step '{step.id}' iteration {idx} failed: {e}") from e

        async def bounded_iteration(idx: int, item: Any) -> Any:
            """Execute iteration with optional semaphore for bounded concurrency."""
            if semaphore:
                async with semaphore:
                    return await execute_iteration(idx, item)
            return await execute_iteration(idx, item)

        # Create tasks for all iterations (semaphore controls actual concurrency)
        tasks = [bounded_iteration(idx, item) for idx, item in enumerate(items)]

        # Run all tasks concurrently, fail-fast on any error
        # asyncio.gather preserves order of results
        results = await asyncio.gather(*tasks)

        return list(results)

    async def _execute_recipe_step(
        self,
        step: Step,
        context: dict[str, Any],
        project_path: Path,
        recursion_state: RecursionState,
        parent_recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        parent_session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute a recipe composition step by loading and running a sub-recipe.

        Args:
            step: Step with type="recipe" and recipe path
            context: Current context variables
            project_path: Current project directory
            recursion_state: Recursion tracking state
            parent_recipe_path: Path to parent recipe file (for relative resolution)
            rate_limiter: Optional rate limiter (inherited from parent recipe)
            orchestrator_config: Optional orchestrator config (inherited from parent recipe)
            parent_session_id: Parent's session ID for cancellation checks

        Returns:
            Sub-recipe's final context dict
        """
        assert step.recipe is not None, "Recipe step must have recipe path"

        # Substitute variables in recipe path (e.g., {{test_recipe}} in foreach loops)
        recipe_path_str = self.substitute_variables(step.recipe, context)

        # Handle @mention paths (e.g., @recipes:examples/code-review.yaml)
        if recipe_path_str.startswith("@"):
            mention_resolver = self.coordinator.get_capability("mention_resolver")
            if mention_resolver is None:
                raise FileNotFoundError(
                    f"Cannot resolve @mention path '{recipe_path_str}': mention_resolver capability not available"
                )
            sub_recipe_path = mention_resolver.resolve(recipe_path_str)
            if sub_recipe_path is None:
                raise FileNotFoundError(f"Sub-recipe @mention not found: {recipe_path_str}")
        else:
            # Resolve sub-recipe path relative to parent recipe's directory (not project_path)
            # This allows recipes to reference sibling recipes naturally
            if parent_recipe_path is not None:
                base_dir = parent_recipe_path.parent
            else:
                base_dir = project_path

            sub_recipe_path = base_dir / recipe_path_str
            if not sub_recipe_path.exists():
                raise FileNotFoundError(f"Sub-recipe not found: {sub_recipe_path}")

        # Load sub-recipe
        sub_recipe = Recipe.from_yaml(sub_recipe_path)

        # Build sub-recipe context from step's context field (with variable substitution)
        # Context isolation: sub-recipe gets ONLY explicitly passed context
        sub_context: dict[str, Any] = {}
        if step.step_context:
            for key, value in step.step_context.items():
                # Recursively substitute variables in all values (strings, dicts, lists)
                sub_context[key] = self._substitute_variables_recursive(value, context)

        # Create child recursion state (with step-level override if present)
        child_state = recursion_state.enter_recipe(sub_recipe.name, step.recursion)

        # Execute sub-recipe recursively
        # Note: rate_limiter and orchestrator_config are inherited from parent (sub-recipes cannot override)
        # parent_session_id is passed so sub-recipes can check for cancellation
        result = await self.execute_recipe(
            recipe=sub_recipe,
            context_vars=sub_context,
            project_path=project_path,
            session_id=None,  # Sub-recipes don't get separate session files
            recipe_path=sub_recipe_path,
            recursion_state=child_state,
            rate_limiter=rate_limiter,  # Inherit parent's rate limiter
            orchestrator_config=orchestrator_config,  # Inherit parent's orchestrator config
            parent_session_id=parent_session_id,  # For cancellation checks
        )

        # Propagate total steps back to parent state
        recursion_state.total_steps = child_state.total_steps

        return result

    def _resolve_foreach_variable(self, foreach: str, context: dict[str, Any]) -> Any:
        """
        Resolve {{variable}} to its value.

        Args:
            foreach: String containing {{variable}} reference
            context: Current context variables

        Returns:
            The resolved value

        Raises:
            ValueError: If variable syntax invalid or undefined
        """
        pattern = r"\{\{(\w+(?:\.\w+)*)\}\}"
        match = re.match(pattern, foreach.strip())
        if not match:
            raise ValueError(f"Invalid foreach syntax: {foreach}")

        var_path = match.group(1)
        parts = var_path.split(".")
        value = context
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise ValueError(f"Undefined variable in foreach: {foreach}")
        return value

    def _substitute_variables_recursive(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Recursively substitute {{variable}} references in nested structures.

        Handles:
        - Strings: Direct variable substitution
        - Dicts: Recursively process all values
        - Lists: Recursively process all items
        - Other types: Pass through unchanged

        Args:
            value: Value to process (string, dict, list, or other)
            context: Dict with variable values

        Returns:
            Value with all variables substituted
        """
        if isinstance(value, str):
            return self.substitute_variables(value, context)
        elif isinstance(value, dict):
            return {k: self._substitute_variables_recursive(v, context) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._substitute_variables_recursive(item, context) for item in value]
        else:
            # Numbers, booleans, None, etc. - pass through unchanged
            return value

    def substitute_variables(self, template: str, context: dict[str, Any]) -> str:
        """
        Replace {{variable}} references with context values.

        Args:
            template: String with {{variable}} placeholders
            context: Dict with variable values

        Returns:
            String with variables substituted

        Raises:
            ValueError if variable undefined
        """
        # Support multi-level access: {{a.b.c.d}} - use * not ? for unlimited depth
        pattern = r"\{\{(\w+(?:\.\w+)*)\}\}"

        def replace(match: re.Match) -> str:
            var_ref = match.group(1)

            # Handle nested references (recipe.name, session.id, etc.)
            if "." in var_ref:
                parts = var_ref.split(".")
                value = context
                path_so_far = []
                for part in parts:
                    path_so_far.append(part)
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    elif isinstance(value, dict):
                        # Key doesn't exist in dict
                        raise ValueError(
                            f"Undefined variable: {{{{{var_ref}}}}}. "
                            f"Key '{part}' not found. "
                            f"Available keys at '{'.'.join(path_so_far[:-1]) or 'root'}': {', '.join(sorted(value.keys()))}"
                        )
                    else:
                        # Parent is not a dict (likely a string from failed JSON parsing)
                        parent_path = ".".join(path_so_far[:-1])
                        raise ValueError(
                            f"Cannot access '{part}' on {{{{{parent_path}}}}} - "
                            f"it's a {type(value).__name__}, not a dict. "
                            f"Hint: The step producing '{parent_path}' may have failed to parse JSON. "
                            f"Check that the bash command outputs clean JSON or add 'parse_json: true'."
                        )
                # Use json.dumps for dict/list to produce valid JSON, not Python repr
                if isinstance(value, (dict, list)):
                    return json.dumps(value)
                return str(value)

            # Handle direct references
            if var_ref not in context:
                available = ", ".join(sorted(context.keys()))
                raise ValueError(f"Undefined variable: {{{{{var_ref}}}}}. Available variables: {available}")

            # Use json.dumps for dict/list to produce valid JSON, not Python repr
            value = context[var_ref]
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return str(value)

        return re.sub(pattern, replace, template)

    async def _execute_bash_step(
        self,
        step: Step,
        context: dict[str, Any],
        project_path: Path,
    ) -> BashResult:
        """
        Execute a bash step by running shell command directly.

        No LLM overhead - command is executed via subprocess.

        Args:
            step: Step with type="bash" and command
            context: Current context variables
            project_path: Current project directory

        Returns:
            BashResult with stdout, stderr, and exit_code

        Raises:
            ValueError: If command fails and on_error="fail"
            asyncio.TimeoutError: If command exceeds timeout
        """
        assert step.command is not None, "Bash step must have command"

        # Substitute variables in command
        command = self.substitute_variables(step.command, context)

        # Determine working directory
        if step.cwd:
            cwd = Path(self.substitute_variables(step.cwd, context))
            if not cwd.is_absolute():
                cwd = project_path / cwd
            if not cwd.exists():
                raise ValueError(f"Step '{step.id}': cwd does not exist: {cwd}")
            if not cwd.is_dir():
                raise ValueError(f"Step '{step.id}': cwd is not a directory: {cwd}")
        else:
            cwd = project_path

        # Build environment variables
        env = os.environ.copy()
        if step.env:
            for key, value in step.env.items():
                # Substitute variables in env values
                env[key] = self.substitute_variables(str(value), context)

        # Execute command with timeout
        # Use /bin/bash explicitly since recipe bash steps may use bash-specific
        # features like pipefail, &> redirects, brace expansion, arrays, etc.
        # The default shell (/bin/sh) is often dash on Ubuntu which lacks these.
        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/bash", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=step.timeout,
                )
            except asyncio.TimeoutError:
                # Kill the process on timeout
                process.kill()
                await process.wait()
                raise ValueError(
                    f"Step '{step.id}': command timed out after {step.timeout}s"
                ) from None

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = process.returncode or 0

            result = BashResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

            # Check for non-zero exit code
            if exit_code != 0:
                error_msg = f"Step '{step.id}': command failed with exit code {exit_code}"
                if stderr.strip():
                    error_msg += f"\nstderr: {stderr.strip()}"

                if step.on_error == "fail":
                    raise ValueError(error_msg)
                # For "continue" and "skip_remaining", we return the result
                # and let the caller handle it

            return result

        except OSError as e:
            raise ValueError(f"Step '{step.id}': failed to execute command: {e}") from e
