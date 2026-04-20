"""Tests for foreach iteration checkpointing.

Covers 17 tests as per COE-approved design spec:
  - 4 model validation tests
  - 13 executor behaviour tests
"""

import copy
import logging
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step


# ============================================================================
# Model Validation Tests (4)
# ============================================================================


class TestCheckpointIterationsModelValidation:
    """Validate the checkpoint_iterations field on the Step model."""

    def test_checkpoint_iterations_valid_with_foreach(self):
        """Step with foreach + checkpoint_iterations=True validates without errors."""
        step = Step(
            id="test",
            agent="a",
            prompt="p {{item}}",
            foreach="{{items}}",
            checkpoint_iterations=True,
        )
        errors = step.validate()
        assert not errors

    def test_checkpoint_iterations_invalid_without_foreach(self):
        """checkpoint_iterations without foreach fails validation."""
        step = Step(
            id="test",
            agent="a",
            prompt="p",
            checkpoint_iterations=True,
        )
        errors = step.validate()
        assert any("checkpoint_iterations requires foreach" in e for e in errors)

    def test_checkpoint_iterations_invalid_with_parallel(self):
        """checkpoint_iterations with parallel=True fails validation."""
        step = Step(
            id="test",
            agent="a",
            prompt="p {{item}}",
            foreach="{{items}}",
            checkpoint_iterations=True,
            parallel=True,
        )
        errors = step.validate()
        assert any(
            "checkpoint_iterations cannot be used with parallel" in e for e in errors
        )

    def test_checkpoint_iterations_default_false(self):
        """checkpoint_iterations defaults to False (backward compat)."""
        step = Step(id="test", agent="a", prompt="p")
        assert step.checkpoint_iterations is False


# ============================================================================
# Executor Tests (13)
# ============================================================================


@pytest.fixture
def mock_coordinator():
    """Mock coordinator with async spawn capability."""
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


@pytest.fixture
def mock_session_manager():
    """Mock session manager that returns a fresh state dict on every load_state call."""
    manager = MagicMock()
    manager.create_session.return_value = "test-session-id"
    manager.is_cancellation_requested.return_value = False
    manager.is_immediate_cancellation.return_value = False

    def fresh_state(*args, **kwargs):
        return {
            "current_step_index": 0,
            "context": {},
            "completed_steps": [],
            "started": "2025-01-01T00:00:00",
        }

    manager.load_state.side_effect = fresh_state
    return manager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_foreach_recipe(
    step_id: str = "loop-step",
    items: list | None = None,
    collect: str | None = "results",
    output: str | None = None,
    on_error: str = "fail",
    checkpoint_iterations: bool = True,
    while_steps: list | None = None,
    num_extra_steps: int = 0,
) -> Recipe:
    """Build a minimal foreach recipe for tests."""
    step_kwargs: dict = {
        "id": step_id,
        "foreach": "{{items}}",
        "checkpoint_iterations": checkpoint_iterations,
        "on_error": on_error,
    }
    if while_steps:
        # Compound foreach step — no agent/prompt required
        step_kwargs["while_steps"] = while_steps
    else:
        step_kwargs["agent"] = "a"
        step_kwargs["prompt"] = "p {{item}}"

    if collect:
        step_kwargs["collect"] = collect
    if output:
        step_kwargs["output"] = output

    steps: list[Step] = [Step(**step_kwargs)]
    if num_extra_steps:
        steps.append(
            Step(
                id="after-step",
                agent="a",
                prompt="after {{results}}",
                output="final_result",
            )
        )

    return Recipe(
        name="test",
        description="test",
        version="1.0.0",
        steps=steps,
        context={"items": items if items is not None else ["a", "b", "c"]},
    )


def _capture_saves(mock_session_manager: MagicMock) -> list[dict]:
    """Install a side_effect on save_state that captures deep copies of each saved state."""
    captured: list[dict] = []

    def _capture(sid: str, pp, state: dict) -> None:
        captured.append(copy.deepcopy(state))

    mock_session_manager.save_state.side_effect = _capture
    return captured


# ---------------------------------------------------------------------------
# Executor test class
# ---------------------------------------------------------------------------


class TestCheckpointIterationsExecutor:
    # ------------------------------------------------------------------
    # 5. saves state per iteration
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_saves_state_per_iteration(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """3 iterations = 3 save_state calls that each have incrementing completed_iterations."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", "r2", "r3"]
        captured = _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(items=["a", "b", "c"])
        await executor.execute_recipe(recipe, {}, temp_dir)

        checkpoint_saves = [s for s in captured if "foreach_progress" in s]
        assert len(checkpoint_saves) == 3
        counts = [
            s["foreach_progress"]["completed_iterations"] for s in checkpoint_saves
        ]
        assert counts == [1, 2, 3]

    # ------------------------------------------------------------------
    # 6. resume skips completed iterations
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_resume_skips_completed(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Resume with completed_iterations=2 executes only the 2 remaining of a 4-item foreach."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r3", "r4"]

        mock_session_manager.load_state.side_effect = lambda *a, **k: {
            "current_step_index": 0,
            "context": {},
            "completed_steps": [],
            "started": "2025-01-01T00:00:00",
            "foreach_progress": {
                "step_id": "loop-step",
                "completed_iterations": 2,
                "total_items": 4,
                "collected_results": ["r1", "r2"],
            },
        }

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(items=["a", "b", "c", "d"])
        await executor.execute_recipe(recipe, {}, temp_dir)

        # Only 2 iterations should have been executed (items 2 and 3)
        assert mock_spawn.call_count == 2

    # ------------------------------------------------------------------
    # 7. resume restores collected results
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_resume_restores_collected_results(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Pre-populated collected_results from checkpoint are merged into the final collect variable."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r3", "r4"]

        mock_session_manager.load_state.side_effect = lambda *a, **k: {
            "current_step_index": 0,
            "context": {},
            "completed_steps": [],
            "started": "2025-01-01T00:00:00",
            "foreach_progress": {
                "step_id": "loop-step",
                "completed_iterations": 2,
                "total_items": 4,
                "collected_results": ["r1", "r2"],
            },
        }

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(items=["a", "b", "c", "d"])
        result = await executor.execute_recipe(recipe, {}, temp_dir)

        assert result["results"] == ["r1", "r2", "r3", "r4"]

    # ------------------------------------------------------------------
    # 8. checkpoint_iterations=False — no extra saves (regression guard)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_false_no_extra_saves(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Default checkpoint_iterations=False leaves save_state behaviour unchanged."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", "r2", "r3"]
        captured = _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(
            items=["a", "b", "c"], checkpoint_iterations=False
        )
        await executor.execute_recipe(recipe, {}, temp_dir)

        # No foreach_progress in any save
        assert all("foreach_progress" not in s for s in captured)
        # Only the one step-completion save
        assert len(captured) == 1

    # ------------------------------------------------------------------
    # 9. empty list — no foreach_progress written
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_empty_list_no_progress(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Empty items list returns early; no foreach_progress is ever saved."""
        mock_spawn = mock_coordinator.get_capability.return_value
        captured = _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(items=[])
        await executor.execute_recipe(recipe, {}, temp_dir)

        assert mock_spawn.call_count == 0
        assert all("foreach_progress" not in s for s in captured)

    # ------------------------------------------------------------------
    # 10. on_error=continue — failed iteration saves None; checkpoint written
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_on_error_continue(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """Failed iteration with on_error=continue saves None in results; checkpoint is still written."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", Exception("boom"), "r3"]
        captured = _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(items=["a", "b", "c"], on_error="continue")
        result = await executor.execute_recipe(recipe, {}, temp_dir)

        checkpoint_saves = [s for s in captured if "foreach_progress" in s]
        # Three checkpoints: success, fail (None), success
        assert len(checkpoint_saves) == 3

        # Second checkpoint records None for the failed iteration
        assert checkpoint_saves[1]["foreach_progress"]["collected_results"] == [
            "r1",
            None,
        ]

        # Final output preserves the None
        assert result["results"] == ["r1", None, "r3"]

    # ------------------------------------------------------------------
    # 11. item count mismatch — warning logged
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_item_count_mismatch_warns(
        self, mock_coordinator, mock_session_manager, temp_dir, caplog
    ):
        """Warning is emitted when the checkpoint's total_items differs from len(items)."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r3", "r4", "r5"]

        mock_session_manager.load_state.side_effect = lambda *a, **k: {
            "current_step_index": 0,
            "context": {},
            "completed_steps": [],
            "started": "2025-01-01T00:00:00",
            "foreach_progress": {
                "step_id": "loop-step",
                "completed_iterations": 2,
                "total_items": 4,  # saved with 4 items
                "collected_results": ["r1", "r2"],
            },
        }

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        # Now there are 5 items — mismatch with saved total_items=4
        recipe = _make_foreach_recipe(items=["a", "b", "c", "d", "e"])

        with caplog.at_level(logging.WARNING):
            await executor.execute_recipe(recipe, {}, temp_dir)

        assert "items count changed" in caplog.text

    # ------------------------------------------------------------------
    # 12. all iterations already done on resume
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_all_done_on_resume(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """When completed_iterations >= len(items), no execution occurs; pre-results are returned."""
        mock_spawn = mock_coordinator.get_capability.return_value

        mock_session_manager.load_state.side_effect = lambda *a, **k: {
            "current_step_index": 0,
            "context": {},
            "completed_steps": [],
            "started": "2025-01-01T00:00:00",
            "foreach_progress": {
                "step_id": "loop-step",
                "completed_iterations": 4,
                "total_items": 4,
                "collected_results": ["r1", "r2", "r3", "r4"],
            },
        }

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(items=["a", "b", "c", "d"])
        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # Zero new executions
        assert mock_spawn.call_count == 0
        # Pre-populated results intact
        assert result["results"] == ["r1", "r2", "r3", "r4"]

    # ------------------------------------------------------------------
    # 13. foreach_progress absent from step-completion checkpoint
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_clears_on_step_completion(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """The foreach-step's own completion save — and any subsequent step saves — omit foreach_progress."""
        mock_spawn = mock_coordinator.get_capability.return_value
        # 2 foreach iterations + 1 call for the subsequent step
        mock_spawn.side_effect = ["r1", "r2", "after_result"]
        captured = _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(items=["a", "b"], num_extra_steps=1)
        await executor.execute_recipe(recipe, {}, temp_dir)

        # The final save (after-step completion) must NOT have foreach_progress
        assert "foreach_progress" not in captured[-1]
        # Every non-checkpoint save must also be free of foreach_progress
        step_completion_saves = [s for s in captured if "foreach_progress" not in s]
        assert len(step_completion_saves) >= 1

    # ------------------------------------------------------------------
    # 14. with collect — collected_results present in foreach_progress
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_with_collect(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """When collect is set, each checkpoint includes collected_results."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1"]
        captured = _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(items=["a"], collect="results")
        await executor.execute_recipe(recipe, {}, temp_dir)

        checkpoint_saves = [s for s in captured if "foreach_progress" in s]
        assert len(checkpoint_saves) == 1
        assert "collected_results" in checkpoint_saves[0]["foreach_progress"]
        assert checkpoint_saves[0]["foreach_progress"]["collected_results"] == ["r1"]

    # ------------------------------------------------------------------
    # 15. without collect — collected_results absent from foreach_progress
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_without_collect(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """When collect is not set, foreach_progress omits collected_results."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1"]
        captured = _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        # output (not collect) — progress should have no collected_results key
        recipe = _make_foreach_recipe(items=["a"], collect=None, output="result")
        await executor.execute_recipe(recipe, {}, temp_dir)

        checkpoint_saves = [s for s in captured if "foreach_progress" in s]
        assert len(checkpoint_saves) == 1
        assert "collected_results" not in checkpoint_saves[0]["foreach_progress"]

    # ------------------------------------------------------------------
    # 16. multi-step body (while_steps)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_with_multi_step_body(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """checkpoint_iterations works when the foreach step has a while_steps body."""
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["sub_r1", "sub_r2"]
        captured = _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(
            items=["x", "y"],
            collect="results",
            while_steps=[{"id": "sub-step", "agent": "a", "prompt": "p {{item}}"}],
        )
        await executor.execute_recipe(recipe, {}, temp_dir)

        checkpoint_saves = [s for s in captured if "foreach_progress" in s]
        # One checkpoint per iteration of the outer foreach
        assert len(checkpoint_saves) == 2
        assert checkpoint_saves[0]["foreach_progress"]["completed_iterations"] == 1
        assert checkpoint_saves[1]["foreach_progress"]["completed_iterations"] == 2

    # ------------------------------------------------------------------
    # 17. full resume cycle (integration)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_iterations_full_resume_cycle(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """INTEGRATION: start recipe, mock crash mid-foreach, verify state, resume, verify completion.

        Phase 1 runs 3 of 5 iterations successfully.  The 4th call to spawn
        exhausts the side-effect list; mock converts StopIteration to
        StopAsyncIteration and the executor turns it into ValueError —
        simulating a mid-loop crash.  The three checkpoints saved before the
        crash are captured and used to drive the Phase 2 resume that completes
        the remaining two iterations.
        """
        mock_spawn = mock_coordinator.get_capability.return_value
        recipe = _make_foreach_recipe(items=["a", "b", "c", "d", "e"])

        # ── Phase 1: run first 3 of 5 iterations; 4th call crashes ──────
        mock_spawn.side_effect = ["r1", "r2", "r3"]  # side-effect exhausted on idx=3
        phase1_saves: list[dict] = []

        def _phase1_capture(sid: str, pp, state: dict) -> None:
            phase1_saves.append(copy.deepcopy(state))

        mock_session_manager.save_state.side_effect = _phase1_capture

        executor1 = RecipeExecutor(mock_coordinator, mock_session_manager)
        # 4th call raises StopAsyncIteration → executor wraps as ValueError
        with pytest.raises(ValueError, match="iteration 3 failed"):
            await executor1.execute_recipe(recipe, {}, temp_dir)

        # Three successful iterations each wrote a checkpoint before the crash
        cp_saves_1 = [s for s in phase1_saves if "foreach_progress" in s]
        assert len(cp_saves_1) == 3, (
            "Phase 1 should produce 3 per-iteration checkpoints"
        )

        last_checkpoint = cp_saves_1[2]
        assert last_checkpoint["foreach_progress"]["completed_iterations"] == 3
        assert last_checkpoint["foreach_progress"]["collected_results"] == [
            "r1",
            "r2",
            "r3",
        ]

        # ── Phase 2: simulate resume from the last checkpoint ──────────
        mock_spawn.reset_mock()
        mock_spawn.side_effect = ["r4", "r5"]

        # Every load_state call returns a fresh deep copy of the crash checkpoint
        mock_session_manager.load_state.side_effect = lambda *a, **k: copy.deepcopy(
            last_checkpoint
        )

        phase2_saves: list[dict] = []

        def _phase2_capture(sid: str, pp, state: dict) -> None:
            phase2_saves.append(copy.deepcopy(state))

        mock_session_manager.save_state.side_effect = _phase2_capture

        executor2 = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor2.execute_recipe(recipe, {}, temp_dir)

        # Only 2 new spawn calls in the resumed run (items at index 3 and 4)
        assert mock_spawn.call_count == 2

        # Final result contains all 5 elements — pre-results merged with new ones
        assert result["results"] == ["r1", "r2", "r3", "r4", "r5"]

        # Step-completion checkpoint in phase 2 must not contain foreach_progress
        step_completion_saves = [s for s in phase2_saves if "foreach_progress" not in s]
        assert len(step_completion_saves) >= 1

    # ------------------------------------------------------------------
    # 18. large accumulated results trigger write-amplification warning
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_large_results_logs_warning(
        self, mock_coordinator, mock_session_manager, temp_dir, caplog
    ):
        """Warning is logged when foreach_progress exceeds 10 MB."""
        import logging

        mock_spawn = mock_coordinator.get_capability.return_value
        big_result = "x" * (1024 * 1024)  # ~1 MB string per iteration
        mock_spawn.side_effect = [
            big_result
        ] * 12  # 12 iterations -> >10 MB accumulated

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(
            items=[str(i) for i in range(12)],
            collect="results",
            checkpoint_iterations=True,
        )

        with caplog.at_level(logging.WARNING):
            await executor.execute_recipe(recipe, {}, temp_dir)

        assert "write amplification" in caplog.text

    # ------------------------------------------------------------------
    # 19. small results do NOT trigger the warning (regression guard)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_checkpoint_small_results_no_warning(
        self, mock_coordinator, mock_session_manager, temp_dir, caplog
    ):
        """No warning is logged when foreach_progress is well under 10 MB."""
        import logging

        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r1", "r2", "r3"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = _make_foreach_recipe(
            items=["a", "b", "c"],
            collect="results",
            checkpoint_iterations=True,
        )

        with caplog.at_level(logging.WARNING):
            await executor.execute_recipe(recipe, {}, temp_dir)

        assert "write amplification" not in caplog.text


# ============================================================================
# Composition Tests (3): session_id resume + foreach_progress
# ============================================================================


class TestCheckpointIterationsResumeComposition:
    """Integration tests for session_id resume + foreach_progress composition.

    PR #64's full_resume_cycle test simulated a crash but did NOT pass
    session_id to execute_recipe, so the is_resuming=True path was never
    exercised in combination with foreach_progress skipping.  These tests
    close that gap.

    The composition path requires BOTH mechanisms to cooperate:
      - current_step_index skip (via is_resuming=True in execute_recipe)
      - foreach_progress skip    (via checkpoint in _execute_loop)

    Each test passes session_id="resume-session-*" to execute_recipe to
    trigger is_resuming=True and verify the two mechanisms compose correctly.
    """

    @staticmethod
    def _make_composition_recipe() -> Recipe:
        """3-step recipe: agent / foreach-with-checkpoint / agent."""
        return Recipe(
            name="test-composition",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step-0", agent="a", prompt="first", output="step0_result"),
                Step(
                    id="loop-step",
                    foreach="{{items}}",
                    agent="a",
                    prompt="p {{item}}",
                    collect="results",
                    checkpoint_iterations=True,
                ),
                Step(
                    id="step-2", agent="a", prompt="after {{results}}", output="final"
                ),
            ],
            context={"items": ["a", "b", "c", "d"]},
        )

    # ------------------------------------------------------------------
    # PRIMARY: mid-foreach crash, resume with session_id
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_resume_with_session_id_and_foreach_progress(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """PRIMARY: 3-step recipe, crash mid-foreach (after 2 of 4), resume with session_id.

        Verifies that current_step_index skip AND foreach_progress skip compose
        correctly when session_id is passed to execute_recipe (is_resuming=True).

        Step-0 is skipped (current_step_index=1).
        Foreach items 0-1 are skipped (foreach_progress.completed_iterations=2).
        Items 2-3 execute (spawn calls: "r2", "r3").
        Step-2 executes (spawn call: "step2_final").
        Total spawn calls: 3 (NOT 4+1).
        """
        recipe = self._make_composition_recipe()

        # Simulated crash state: crashed after completing 2 of 4 foreach iterations.
        # Note: when is_resuming=True the executor uses state["context"] as-is (does
        # NOT merge recipe.context), so "items" must be present in the saved context.
        crash_state = {
            "current_step_index": 1,  # step-0 done; resume at loop-step (index 1)
            "context": {
                "step0_result": "done",
                "items": [
                    "a",
                    "b",
                    "c",
                    "d",
                ],  # required for foreach variable resolution
            },
            "completed_steps": ["step-0"],
            "started": "2025-01-01T00:00:00",
            "foreach_progress": {
                "step_id": "loop-step",
                "completed_iterations": 2,
                "total_items": 4,
                "collected_results": ["r0", "r1"],
            },
        }

        # Every load_state call returns a fresh deep copy of the crash state
        mock_session_manager.load_state.side_effect = lambda *a, **k: copy.deepcopy(
            crash_state
        )

        # 2 foreach remaining (items c, d) + 1 step-2 = 3 total
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["r2", "r3", "step2_final"]

        captured = _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor.execute_recipe(
            recipe, {}, temp_dir, session_id="resume-session-123"
        )

        # We resumed — create_session must NOT have been called
        mock_session_manager.create_session.assert_not_called()

        # Only 2 foreach iterations + 1 step-2 (NOT 4 foreach + 1 step-2)
        assert mock_spawn.call_count == 3

        # Pre-populated results merged with the 2 new results
        assert result["results"] == ["r0", "r1", "r2", "r3"]

        # Step-2 ran after the foreach completed
        assert result["final"] == "step2_final"

        # At least one save must not contain foreach_progress (step completion save)
        assert any("foreach_progress" not in s for s in captured)

    # ------------------------------------------------------------------
    # EDGE 1: foreach already completed on resume
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_resume_after_foreach_completed(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """EDGE: Resume from state where foreach already completed (4 of 4 iterations done).

        The entire foreach step should be skipped (zero new spawns); only step-2
        executes.  The pre-populated collected_results must be propagated to the
        context so step-2 can reference {{results}}.
        """
        recipe = self._make_composition_recipe()

        crash_state = {
            "current_step_index": 1,
            "context": {
                "step0_result": "done",
                "items": ["a", "b", "c", "d"],
            },
            "completed_steps": ["step-0"],
            "started": "2025-01-01T00:00:00",
            "foreach_progress": {
                "step_id": "loop-step",
                "completed_iterations": 4,
                "total_items": 4,
                "collected_results": ["r0", "r1", "r2", "r3"],
            },
        }

        mock_session_manager.load_state.side_effect = lambda *a, **k: copy.deepcopy(
            crash_state
        )

        # Only step-2 should execute
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["step2_final"]

        _capture_saves(mock_session_manager)

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor.execute_recipe(
            recipe, {}, temp_dir, session_id="resume-session-456"
        )

        # Foreach entirely skipped — only the step-2 agent was spawned
        assert mock_spawn.call_count == 1

        # Step-2 ran
        assert result["final"] == "step2_final"

        # Pre-populated results are intact in the final context
        assert result["results"] == ["r0", "r1", "r2", "r3"]

    # ------------------------------------------------------------------
    # EDGE 2: resume at step after foreach (no foreach_progress key)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_resume_at_step_after_foreach(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """EDGE: Resume from state where foreach completed in a prior run and was cleared.

        current_step_index=2 means both step-0 and loop-step are skipped by the
        step index check (not by foreach_progress).  No foreach_progress key is
        present because it is cleared when the foreach step completes normally.
        Only step-2 executes.
        """
        recipe = self._make_composition_recipe()

        crash_state = {
            "current_step_index": 2,
            "context": {
                "step0_result": "done",
                "results": ["r0", "r1", "r2", "r3"],
            },
            "completed_steps": ["step-0", "loop-step"],
            "started": "2025-01-01T00:00:00",
            # No foreach_progress key — cleared on loop-step completion
        }

        mock_session_manager.load_state.side_effect = lambda *a, **k: copy.deepcopy(
            crash_state
        )

        # Only step-2 should execute
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["step2_final"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        result = await executor.execute_recipe(
            recipe, {}, temp_dir, session_id="resume-session-789"
        )

        # Only step-2 — step-0 and loop-step both skipped by current_step_index
        assert mock_spawn.call_count == 1

        # Step-2 ran with the pre-restored results in context
        assert result["final"] == "step2_final"
