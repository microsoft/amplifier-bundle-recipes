"""Tests proving on_error contract correctness in foreach and bash steps.

RED phase: 4 new-behaviour tests FAIL on unmodified main.
GREEN phase: all 8 tests pass after the two fixes are applied.

Fix 1 — _execute_loop_sequential respects step.on_error (continue / skip_remaining).
  The buggy except-block unconditionally raises ValueError, bypassing on_error entirely.
  For agent steps, execute_step_with_retry catches exceptions first, so the failure
  manifests only when an exception escapes the step body directly (e.g. while_steps,
  or testing _execute_loop_sequential directly with a mocked body).  We test the
  method under isolation to prove the specific code path is broken.

Fix 2 — _execute_bash_step raises SkipRemainingError when on_error='skip_remaining'.
  The existing code silently falls through and returns the BashResult.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amplifier_module_tool_recipes.executor import (
    BashResult,
    RecipeExecutor,
    RecursionState,
    SkipRemainingError,
)
from amplifier_module_tool_recipes.models import Recipe, Step


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_coordinator():
    """Mock coordinator with async spawn capability."""
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    # Prevent MagicMock hooks from being awaited in _show_progress
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


@pytest.fixture
def mock_session_manager():
    """Mock session manager."""
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


@pytest.fixture
def project_path(tmp_path: Path) -> Path:
    """Temporary project directory for bash step execution."""
    return tmp_path


@pytest.fixture
def executor(mock_coordinator, mock_session_manager) -> RecipeExecutor:
    """RecipeExecutor with mock dependencies."""
    return RecipeExecutor(mock_coordinator, mock_session_manager)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helper: build a minimal Step for foreach testing
# ---------------------------------------------------------------------------


def _foreach_step(on_error: str, collect: str = "results") -> Step:
    """Build a foreach agent step with the given on_error policy."""
    return Step(
        id="loop-step",
        agent="test-agent",
        prompt="Process {{item}}",
        foreach="{{items}}",
        collect=collect,
        on_error=on_error,
    )


# ===========================================================================
# Class 1: Sequential foreach on_error behaviour
# ===========================================================================


class TestSequentialForeachOnError:
    """Sequential foreach respects step.on_error (Fix 1).

    We call _execute_loop_sequential directly with a mocked _execute_single_step_body
    so that exceptions reach the buggy except-block regardless of the agent layer.
    This is the precise code path the fix targets.
    """

    # -----------------------------------------------------------------------
    # 1. continue → None placeholder appended, warning logged — NEW BEHAVIOUR
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sequential_foreach_on_error_continue_appends_none_on_failure(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """on_error=continue: failed iteration → None appended, warning logged.

        RED on unmodified code: ValueError is raised from the except-block.
        GREEN after Fix 1: None is appended and logger.warning called once.
        """
        step = _foreach_step(on_error="continue")

        call_count = 0

        async def mock_body(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # idx=1 (second item)
                raise RuntimeError("simulated failure on iteration 1")
            return f"result_{call_count}"

        executor._execute_single_step_body = mock_body  # type: ignore[method-assign]

        with patch("amplifier_module_tool_recipes.executor.logger") as mock_logger:
            results = await executor._execute_loop_sequential(
                step=step,
                context={},
                items=["a", "b", "c"],
                loop_var="item",
                project_path=project_path,
                recursion_state=RecursionState(),
            )

        # Iteration 0 and 2 succeeded; iteration 1 → None
        assert results[0] == "result_1"
        assert results[1] is None
        assert results[2] == "result_3"
        assert len(results) == 3
        # One warning must be logged for the failed iteration
        mock_logger.warning.assert_called_once()

    # -----------------------------------------------------------------------
    # 2. skip_remaining → SkipRemainingError raised — NEW BEHAVIOUR
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sequential_foreach_on_error_skip_remaining_raises(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """on_error=skip_remaining: failed iteration → SkipRemainingError raised.

        RED on unmodified code: ValueError raised (wrong exception type).
        GREEN after Fix 1: SkipRemainingError raised.
        """
        step = _foreach_step(on_error="skip_remaining")

        call_count = 0

        async def mock_body(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # idx=1
                raise RuntimeError("simulated iteration failure")
            return f"result_{call_count}"

        executor._execute_single_step_body = mock_body  # type: ignore[method-assign]

        with pytest.raises(SkipRemainingError):
            await executor._execute_loop_sequential(
                step=step,
                context={},
                items=["a", "b", "c"],
                loop_var="item",
                project_path=project_path,
                recursion_state=RecursionState(),
            )

        # First iteration succeeded; second raised → body called exactly twice
        assert call_count == 2

    # -----------------------------------------------------------------------
    # 3. fail (default) → ValueError raised — REGRESSION GUARD
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sequential_foreach_on_error_fail_unchanged(
        self, executor: RecipeExecutor, mock_coordinator, temp_dir: Path
    ):
        """on_error=fail: failed iteration → ValueError with iteration index.

        Must PASS before and after Fix 1 (regression guard).
        """
        mock_spawn = mock_coordinator.get_capability.return_value
        # First iteration ok, second raises, third never runs
        mock_spawn.side_effect = ["ok", Exception("boom"), "ok"]

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop-step",
                    agent="test-agent",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    collect="results",
                    on_error="fail",
                ),
            ],
            context={"items": ["a", "b", "c"]},
        )

        with pytest.raises(ValueError, match="iteration 1 failed"):
            await executor.execute_recipe(recipe, {}, temp_dir)

    # -----------------------------------------------------------------------
    # 4. continue with collect → context variable correct — REGRESSION GUARD
    #    (execute_step_with_retry already returns None for on_error=continue;
    #     this test guards that collect stores [ok, None, ok] end-to-end)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sequential_foreach_on_error_continue_with_collect(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """on_error=continue with collect: None is stored at failed index.

        Tests the collect wiring after _execute_loop_sequential returns.
        Uses direct _execute_loop_sequential call for determinism.
        Must PASS before and after Fix 1 (regression guard for collect path).
        """
        step = _foreach_step(on_error="continue", collect="results_var")
        context: dict = {}

        call_count = 0

        async def mock_body(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("fail on idx 1")
            return "ok"

        executor._execute_single_step_body = mock_body  # type: ignore[method-assign]

        # _execute_loop_sequential returns the list; simulate what _execute_foreach_step does:
        # On unmodified code this would raise ValueError → test also confirms regression.
        # We DON'T wrap in pytest.raises because GREEN path should return normally.
        try:
            results = await executor._execute_loop_sequential(
                step=step,
                context=context,
                items=["x", "y", "z"],
                loop_var="item",
                project_path=project_path,
                recursion_state=RecursionState(),
            )
            # Simulate collect storage (done by _execute_foreach_step)
            context["results_var"] = results
        except ValueError:
            # On unmodified code a ValueError is raised — mark the collect key absent
            pass

        # After fix: results_var must contain [ok, None, ok]
        assert "results_var" in context, (
            "results_var not written to context — Fix 1 likely not applied"
        )
        assert context["results_var"] == ["ok", None, "ok"]


# ===========================================================================
# Class 2: Bash step on_error=skip_remaining
# ===========================================================================


class TestBashOnErrorSkipRemaining:
    """Bash step raises SkipRemainingError when on_error='skip_remaining' (Fix 2)."""

    # -----------------------------------------------------------------------
    # 5. skip_remaining raises SkipRemainingError on non-zero exit — NEW BEHAVIOUR
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_bash_on_error_skip_remaining_raises_on_nonzero_exit(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """on_error=skip_remaining: non-zero exit → SkipRemainingError raised.

        RED on unmodified code: no exception raised (silent fall-through returns BashResult).
        GREEN after Fix 2: SkipRemainingError raised.
        """
        step = Step(
            id="bash-test",
            type="bash",
            command="exit 1",
            on_error="skip_remaining",
        )

        with pytest.raises(SkipRemainingError) as exc_info:
            await executor._execute_bash_step(step, {}, project_path)

        # Error message should identify the step and exit code
        err = str(exc_info.value)
        assert "bash-test" in err or "exit code" in err.lower()

    # -----------------------------------------------------------------------
    # 6. skip_remaining propagates: step after failing bash never executes — NEW BEHAVIOUR
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_bash_on_error_skip_remaining_propagates_in_recipe(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """on_error=skip_remaining on step-b: step-c is never executed.

        RED on unmodified code: step-b silently returns BashResult → step-c runs →
          step_c_out appears in context.
        GREEN after Fix 2: SkipRemainingError propagates → step-c skipped →
          step_c_out absent from context.
        """
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="step-a",
                    type="bash",
                    command="echo hello",
                    output="step_a_out",
                ),
                Step(
                    id="step-b",
                    type="bash",
                    command="exit 1",
                    on_error="skip_remaining",
                ),
                Step(
                    id="step-c",
                    type="bash",
                    command="echo world",
                    output="step_c_out",
                ),
            ],
            context={},
        )

        result = await executor.execute_recipe(recipe, {}, project_path)

        # step-a ran and captured output
        assert "step_a_out" in result
        assert result["step_a_out"].strip() == "hello"
        # step-c was never reached — its output must be absent
        assert "step_c_out" not in result

    # -----------------------------------------------------------------------
    # 7. continue → returns BashResult with non-zero exit — REGRESSION GUARD
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_bash_on_error_continue_returns_result(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """on_error=continue: non-zero exit → BashResult returned, no exception.

        Must PASS before and after Fix 2 (regression guard).
        """
        step = Step(
            id="bash-test",
            type="bash",
            command="exit 42",
            on_error="continue",
        )

        result = await executor._execute_bash_step(step, {}, project_path)

        assert isinstance(result, BashResult)
        assert result.exit_code == 42

    # -----------------------------------------------------------------------
    # 8. fail (default) → ValueError raised — REGRESSION GUARD
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_bash_on_error_fail_raises(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """on_error=fail: non-zero exit → ValueError raised.

        Must PASS before and after Fix 2 (regression guard).
        """
        step = Step(
            id="bash-test",
            type="bash",
            command="exit 1",
            on_error="fail",
        )

        with pytest.raises(ValueError, match="exit code"):
            await executor._execute_bash_step(step, {}, project_path)


# ===========================================================================
# Class 3: Integration — bash step on_error=skip_remaining inside foreach
# ===========================================================================


class TestBashForeachSkipRemainingIntegration:
    """Integration: bash step on_error=skip_remaining nested inside a foreach loop.

    Tests the joint execution path where both fixes interact:

    Fix 2 (_execute_bash_step raises SkipRemainingError on non-zero exit) produces
    the exception that Fix 1's handler (_execute_loop_sequential's
    ``except SkipRemainingError: raise`` block) must propagate without swallowing.

    Neither fix tested in isolation guarantees the combined path works correctly;
    this class provides the missing end-to-end coverage COE flagged on PR #63.
    """

    # -----------------------------------------------------------------------
    # 9. bash skip_remaining inside foreach → SkipRemainingError propagates
    #    and later iterations never execute — INTEGRATION TEST (COE concern)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_foreach_with_bash_step_skip_remaining_propagates(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Integration: bash step on_error=skip_remaining inside foreach propagates.

        Recipe structure (via _execute_loop_sequential directly):
          - foreach over [item1, item2, item3]
          - body: bash step that fails on item2 (on_error=skip_remaining)

        Expected behaviour:
          - iteration 0 (item1): bash succeeds, marker file written
          - iteration 1 (item2): bash exits non-zero →
              _execute_bash_step raises SkipRemainingError (Fix 2) →
              _execute_loop_sequential's ``except SkipRemainingError: raise``
              propagates it (Fix 1)
          - iteration 2 (item3): never executes

        RED without Fix 2: bash silently returns BashResult (no exception),
          all three iterations run, no SkipRemainingError raised.
        RED without Fix 1: SkipRemainingError is caught and re-raised as
          ValueError, wrong exception type propagates.
        GREEN after both fixes: SkipRemainingError raised, item3 never touched.
        """
        marker_dir = project_path  # fresh tmp_path per test

        step = Step(
            id="bash-foreach",
            type="bash",
            # Fail on item2; on success write a per-item marker file so we
            # can prove which iterations actually ran after the exception.
            command=(
                "[ '{{item}}' = 'item2' ] && exit 1 "
                "|| touch " + str(marker_dir) + "/{{item}}.done"
            ),
            foreach="{{items}}",
            on_error="skip_remaining",
        )

        with pytest.raises(SkipRemainingError):
            await executor._execute_loop_sequential(
                step=step,
                context={},
                items=["item1", "item2", "item3"],
                loop_var="item",
                project_path=project_path,
                recursion_state=RecursionState(),
            )

        # iteration 0 (item1) ran and completed successfully
        assert (marker_dir / "item1.done").exists(), (
            "item1 iteration should have completed — marker file missing"
        )
        # iteration 2 (item3) was never reached
        assert not (marker_dir / "item3.done").exists(), (
            "item3 iteration should never have executed — "
            "SkipRemainingError must halt the loop after item2 fails"
        )
