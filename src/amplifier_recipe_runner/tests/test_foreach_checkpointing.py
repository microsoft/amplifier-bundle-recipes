"""``checkpoint_iterations:`` -- a long foreach resumes mid-loop (parity Δ3).

Mirrors ``modules/tool-recipes/tests/test_foreach_checkpointing.py``, the
legacy engine's own coverage of the same field.

The one claim every test here has to earn is that a skipped iteration was
**not re-executed** -- not merely that its result reappeared. A cached result
and a re-run that produced the same string are indistinguishable from the
context alone, and only one of them is the feature. So each loop body appends
to a file on disk, and the assertion is about that file: the side effect is
the evidence, the collected result is only the bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from amplifier_recipe_runner.engine import ResumeState
from amplifier_recipe_runner.engine import StepEngine
from amplifier_recipe_runner.engine import parse_program


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class Recorder:
    """Stands in for the host's state store: keeps every checkpoint handed to it."""

    def __init__(self) -> None:
        self.states: list[ResumeState] = []

    def __call__(self, state: ResumeState) -> None:
        # Round-tripped through the serialised form on purpose: a checkpoint
        # only counts if it survives the trip to disk and back.
        self.states.append(ResumeState.from_mapping(json.loads(json.dumps(state.to_mapping()))))

    @property
    def progresses(self) -> list[dict[str, Any] | None]:
        return [dict(s.foreach_progress) if s.foreach_progress else None for s in self.states]

    @property
    def completed_indices(self) -> list[list[int]]:
        return [list(p["completed_indices"]) for p in self.progresses if p]


def loop_recipe(
    log: Path,
    *,
    fail_on: str | None = None,
    parallel: str = "",
    checkpoint: bool = True,
    on_error: str = "fail",
) -> str:
    """A foreach whose body records, on disk, that it actually ran."""
    guard = f'test "{{{{it}}}}" != "{fail_on}" || exit 7; ' if fail_on else ""
    return f"""
steps:
  - id: loop
    foreach: "{{{{items}}}}"
    as: it
    type: bash
    command: 'echo {{{{it}}}} >> {log}; {guard}echo v-{{{{it}}}}'
    collect: seen
    on_error: {on_error}
    {parallel}
    checkpoint_iterations: {str(checkpoint).lower()}
"""


def execute(
    body: str,
    *,
    workspace: Path,
    context: dict[str, Any],
    resume: ResumeState | None = None,
    checkpoint: Any = None,
) -> tuple[Any, dict[str, Any]]:
    program = parse_program(yaml.safe_load(body))
    engine = StepEngine(
        program,
        invoke_agent=None,  # type: ignore[arg-type] - no agent step in these recipes
        workspace=workspace,
        run_id="run-test",
        checkpoint=checkpoint,
    )
    ctx = dict(context)
    outcome = asyncio.run(engine.execute(ctx, resume=resume))
    return outcome, ctx


def ran(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").split() if log.is_file() else []


ITEMS = ["a", "b", "c", "d", "e"]


# --------------------------------------------------------------------------
# sequential -- the acceptance criterion, twice: not re-executed, and identical
# --------------------------------------------------------------------------


class TestSequentialResume:
    def test_resume_does_not_re_execute_the_completed_iterations(self, tmp_path: Path) -> None:
        log = tmp_path / "ran.log"
        recorder = Recorder()

        failed, _ctx = execute(
            loop_recipe(log, fail_on="c"),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=recorder,
        )
        assert failed.status == "failed"
        assert ran(log) == ["a", "b", "c"], "the loop should stop at the failing item"

        log.unlink()
        resumed, ctx = execute(
            loop_recipe(log),
            workspace=tmp_path,
            context=dict(failed.state.context),
            resume=failed.state,
            checkpoint=recorder,
        )

        assert resumed.status == "succeeded"
        # THE claim: a, b never ran again -- only the failed item and what follows.
        assert ran(log) == ["c", "d", "e"]
        assert ctx["seen"] == ["v-a\n", "v-b\n", "v-c\n", "v-d\n", "v-e\n"]

    def test_the_resumed_result_is_identical_to_an_uninterrupted_run(self, tmp_path: Path) -> None:
        interrupted_log = tmp_path / "interrupted.log"
        clean_log = tmp_path / "clean.log"

        failed, _ = execute(
            loop_recipe(interrupted_log, fail_on="c"),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=Recorder(),
        )
        _, resumed_ctx = execute(
            loop_recipe(interrupted_log),
            workspace=tmp_path,
            context=dict(failed.state.context),
            resume=failed.state,
        )
        _, clean_ctx = execute(loop_recipe(clean_log), workspace=tmp_path, context={"items": ITEMS})

        assert resumed_ctx["seen"] == clean_ctx["seen"]
        assert ran(clean_log) == ITEMS, "the uninterrupted run must genuinely have run every item"

    def test_a_checkpoint_lands_at_every_iteration_boundary(self, tmp_path: Path) -> None:
        recorder = Recorder()
        execute(
            loop_recipe(tmp_path / "ran.log"),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=recorder,
        )
        # One per iteration, growing by exactly one index each time -- then a
        # final write with the loop cleared (the step-completion checkpoint).
        assert recorder.completed_indices == [[0], [0, 1], [0, 1, 2], [0, 1, 2, 3], [0, 1, 2, 3, 4]]
        assert recorder.progresses[-1] is None

    def test_results_are_carried_index_aligned_not_re_derived(self, tmp_path: Path) -> None:
        recorder = Recorder()
        failed, _ = execute(
            loop_recipe(tmp_path / "ran.log", fail_on="c"),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=recorder,
        )
        progress = dict(failed.state.foreach_progress or {})
        assert progress["step_id"] == "loop"
        assert progress["total_items"] == 5
        assert progress["completed_iterations"] == 2
        assert progress["completed_indices"] == [0, 1]
        assert progress["results"] == {"0": "v-a\n", "1": "v-b\n"}

    def test_the_step_completion_checkpoint_carries_no_loop_progress(self, tmp_path: Path) -> None:
        """A finished loop must not look resumable, or resume re-enters it."""
        recorder = Recorder()
        outcome, _ = execute(
            loop_recipe(tmp_path / "ran.log"),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=recorder,
        )
        assert outcome.status == "succeeded"
        assert recorder.states[-1].foreach_progress is None
        assert recorder.states[-1].completed_steps == ("loop",)
        assert outcome.state.foreach_progress is None


# --------------------------------------------------------------------------
# parallel -- per completed item, because a batch has no prefix
# --------------------------------------------------------------------------


class TestParallelResume:
    def test_only_the_unfinished_items_run_again(self, tmp_path: Path) -> None:
        log = tmp_path / "ran.log"
        recorder = Recorder()

        failed, _ = execute(
            loop_recipe(log, fail_on="c", parallel="parallel: 2"),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=recorder,
        )
        assert failed.status == "failed"
        assert sorted(ran(log)) == ITEMS, "a parallel loop runs every item; one of them fails"

        progress = dict(failed.state.foreach_progress or {})
        # Non-contiguous on purpose: index 2 failed, 3 and 4 finished anyway.
        assert progress["completed_indices"] == [0, 1, 3, 4]
        assert progress["completed_iterations"] == 2, "the contiguous prefix stops at the gap"

        log.unlink()
        resumed, ctx = execute(
            loop_recipe(log, parallel="parallel: 2"),
            workspace=tmp_path,
            context=dict(failed.state.context),
            resume=failed.state,
            checkpoint=recorder,
        )

        assert resumed.status == "succeeded"
        assert ran(log) == ["c"], "only the item that never finished should run again"
        assert ctx["seen"] == ["v-a\n", "v-b\n", "v-c\n", "v-d\n", "v-e\n"]

    def test_restored_results_keep_input_order_not_completion_order(self, tmp_path: Path) -> None:
        log = tmp_path / "ran.log"
        failed, _ = execute(
            loop_recipe(log, fail_on="a", parallel="parallel: true"),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=Recorder(),
        )
        _, ctx = execute(
            loop_recipe(log, parallel="parallel: true"),
            workspace=tmp_path,
            context=dict(failed.state.context),
            resume=failed.state,
        )
        # The restored slots are 1..4 and the re-run slot is 0; input order wins.
        assert ctx["seen"] == ["v-a\n", "v-b\n", "v-c\n", "v-d\n", "v-e\n"]


# --------------------------------------------------------------------------
# the flag is a flag -- off means off
# --------------------------------------------------------------------------


class TestWithoutTheFlag:
    def test_an_unflagged_loop_records_no_progress_and_restarts_from_item_zero(self, tmp_path: Path) -> None:
        log = tmp_path / "ran.log"
        recorder = Recorder()

        failed, _ = execute(
            loop_recipe(log, fail_on="c", checkpoint=False),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=recorder,
        )
        assert failed.status == "failed"
        assert recorder.states == [], "no flag, no mid-step checkpoint"
        assert failed.state.foreach_progress is None

        log.unlink()
        _, ctx = execute(
            loop_recipe(log, checkpoint=False),
            workspace=tmp_path,
            context=dict(failed.state.context),
            resume=failed.state,
        )
        assert ran(log) == ITEMS, "without the flag the whole loop runs again -- that is the documented cost"
        assert ctx["seen"] == ["v-a\n", "v-b\n", "v-c\n", "v-d\n", "v-e\n"]

    def test_a_run_with_no_checkpoint_hook_still_completes(self, tmp_path: Path) -> None:
        """No ``state_dir`` means nothing is kept -- not that the loop refuses to run."""
        log = tmp_path / "ran.log"
        outcome, ctx = execute(loop_recipe(log), workspace=tmp_path, context={"items": ITEMS}, checkpoint=None)
        assert outcome.status == "succeeded"
        assert ran(log) == ITEMS
        assert ctx["seen"] == ["v-a\n", "v-b\n", "v-c\n", "v-d\n", "v-e\n"]

    def test_a_failing_checkpoint_hook_does_not_fail_the_run(self, tmp_path: Path) -> None:
        def explode(_state: ResumeState) -> None:
            raise OSError("disk full")

        log = tmp_path / "ran.log"
        outcome, ctx = execute(loop_recipe(log), workspace=tmp_path, context={"items": ITEMS}, checkpoint=explode)
        assert outcome.status == "succeeded"
        assert ctx["seen"][-1] == "v-e\n"


# --------------------------------------------------------------------------
# edges: empty list, absorbed failures, a mismatched or stale payload
# --------------------------------------------------------------------------


class TestEdges:
    def test_an_empty_list_writes_no_progress(self, tmp_path: Path) -> None:
        recorder = Recorder()
        outcome, ctx = execute(
            loop_recipe(tmp_path / "ran.log"),
            workspace=tmp_path,
            context={"items": []},
            checkpoint=recorder,
        )
        assert outcome.status == "succeeded"
        assert ctx["seen"] == []
        assert all(p is None for p in recorder.progresses)

    def test_an_absorbed_failure_holds_its_slot_and_is_not_retried(self, tmp_path: Path) -> None:
        """``on_error: continue`` finished that iteration -- and the slot is checkpointed.

        For a bash body the non-zero exit is absorbed *inside* the step, so
        the slot holds its empty stdout rather than ``None`` (see
        ``TestForeach.test_on_error_continue_keeps_a_slot_for_the_failed_iteration``).
        Either way the iteration is over, so re-running it on resume would be
        work already accounted for.
        """
        log = tmp_path / "ran.log"
        recorder = Recorder()
        outcome, ctx = execute(
            loop_recipe(log, fail_on="c", on_error="continue"),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=recorder,
        )
        assert outcome.status == "succeeded"
        assert ctx["seen"] == ["v-a\n", "v-b\n", "", "v-d\n", "v-e\n"]
        # The slot was checkpointed as done, so a resume would not retry it.
        mid = [p for p in recorder.progresses if p and len(p["completed_indices"]) == 3]
        assert mid and mid[0]["results"]["2"] == ""

    def test_progress_recorded_for_another_step_is_ignored(self, tmp_path: Path) -> None:
        log = tmp_path / "ran.log"
        stale = ResumeState(
            context={"items": ITEMS},
            foreach_progress={
                "step_id": "some-other-loop",
                "total_items": 5,
                "completed_iterations": 4,
                "completed_indices": [0, 1, 2, 3],
                "results": {"0": "x", "1": "x", "2": "x", "3": "x"},
            },
        )
        _, ctx = execute(loop_recipe(log), workspace=tmp_path, context={"items": ITEMS}, resume=stale)
        assert ran(log) == ITEMS, "progress belonging to a different step must not skip this one"
        assert ctx["seen"] == ["v-a\n", "v-b\n", "v-c\n", "v-d\n", "v-e\n"]

    def test_an_index_past_the_end_of_a_shortened_list_is_dropped(self, tmp_path: Path) -> None:
        """The list a run resumes with is not necessarily the list it started with."""
        log = tmp_path / "ran.log"
        events: list[tuple[str, dict[str, Any]]] = []
        stale = ResumeState(
            context={"items": ["a", "b"]},
            foreach_progress={
                "step_id": "loop",
                "total_items": 5,
                "completed_iterations": 4,
                "completed_indices": [0, 1, 2, 3],
                "results": {"0": "v-a\n", "1": "v-b\n", "2": "gone", "3": "gone"},
            },
        )
        program = parse_program(yaml.safe_load(loop_recipe(log)))
        engine = StepEngine(
            program,
            invoke_agent=None,  # type: ignore[arg-type]
            workspace=tmp_path,
            run_id="run-test",
            emit=lambda kind, data: events.append((kind, dict(data))),
        )
        ctx: dict[str, Any] = {"items": ["a", "b"]}
        outcome = asyncio.run(engine.execute(ctx, resume=stale))

        assert outcome.status == "succeeded"
        assert ctx["seen"] == ["v-a\n", "v-b\n"], "only slots that still have an item survive"
        assert ran(log) == [], "both surviving slots were already done"
        # Said out loud, not silently absorbed.
        assert any(kind == "foreach:items-changed" for kind, _ in events)

    def test_progress_is_consumed_once(self, tmp_path: Path) -> None:
        """Two loops with the same id are two loops, not one continued."""
        log = tmp_path / "ran.log"
        body = f"""
steps:
  - id: loop
    foreach: "{{{{items}}}}"
    as: it
    type: bash
    command: 'echo first-{{{{it}}}} >> {log}; echo v-{{{{it}}}}'
    collect: first_seen
    checkpoint_iterations: true
  - id: loop
    foreach: "{{{{items}}}}"
    as: it
    type: bash
    command: 'echo second-{{{{it}}}} >> {log}; echo w-{{{{it}}}}'
    collect: second_seen
    checkpoint_iterations: true
"""
        resume = ResumeState(
            context={"items": ["a", "b"]},
            foreach_progress={
                "step_id": "loop",
                "total_items": 2,
                "completed_iterations": 2,
                "completed_indices": [0, 1],
                "results": {"0": "restored-a", "1": "restored-b"},
            },
        )
        _, ctx = execute(body, workspace=tmp_path, context={"items": ["a", "b"]}, resume=resume)

        assert ctx["first_seen"] == ["restored-a", "restored-b"], "the first loop consumed it"
        assert ctx["second_seen"] == ["w-a\n", "w-b\n"], "the second loop ran for real"
        assert ran(log) == ["second-a", "second-b"]


# --------------------------------------------------------------------------
# serialisation -- a checkpoint is only worth having if it survives the disk
# --------------------------------------------------------------------------


class TestResumeStateShape:
    def test_progress_round_trips_through_the_serialised_form(self) -> None:
        progress = {
            "step_id": "loop",
            "total_items": 3,
            "completed_iterations": 2,
            "completed_indices": [0, 1],
            "results": {"0": {"nested": True}, "1": [1, 2]},
        }
        state = ResumeState(completed_steps=("earlier",), context={"k": "v"}, foreach_progress=progress)
        revived = ResumeState.from_mapping(json.loads(json.dumps(state.to_mapping())))
        assert revived.foreach_progress == progress

    def test_absent_progress_serialises_as_null_not_an_empty_shape(self) -> None:
        payload = ResumeState().to_mapping()
        assert payload["foreach_progress"] is None
        assert ResumeState.from_mapping(payload).foreach_progress is None

    def test_a_state_file_written_before_this_field_existed_still_loads(self) -> None:
        """Older state has no `foreach_progress` key at all; that is not a crash."""
        legacy_payload = {"completed_steps": ["a"], "stage_index": 1, "context": {"x": 1}}
        assert ResumeState.from_mapping(legacy_payload).foreach_progress is None


# --------------------------------------------------------------------------
# end to end -- through the real store, in two separate engine lifetimes
# --------------------------------------------------------------------------


class TestThroughTheRunStateStore:
    def test_the_store_holds_mid_loop_progress_between_two_runs(self, tmp_path: Path) -> None:
        """The wiring, not just the engine: a killed process resumes mid-loop.

        The first engine is never asked for its outcome state -- exactly what
        a ``kill -9`` would leave behind. Everything the second engine knows,
        it read off disk.
        """
        from amplifier_recipe_runner.execution import RunStateStore

        log = tmp_path / "ran.log"
        store = RunStateStore(tmp_path / "run-dir")

        def save(state: ResumeState) -> None:
            store.save(engine_state=state, approvals=_EmptyLedger(), status="running")

        first, _ = execute(
            loop_recipe(log, fail_on="c"),
            workspace=tmp_path,
            context={"items": ITEMS},
            checkpoint=save,
        )
        assert first.status == "failed"
        assert store.path.is_file()

        recorded = store.load() or {}
        from_disk = ResumeState.from_mapping(recorded["engine_state"])
        assert (from_disk.foreach_progress or {})["completed_indices"] == [0, 1]

        log.unlink()
        resumed, ctx = execute(
            loop_recipe(log),
            workspace=tmp_path,
            context=dict(from_disk.context),
            resume=from_disk,
        )
        assert resumed.status == "succeeded"
        assert ran(log) == ["c", "d", "e"]
        assert ctx["seen"] == ["v-a\n", "v-b\n", "v-c\n", "v-d\n", "v-e\n"]

    def test_a_sub_recipe_never_writes_the_parents_state(self, tmp_path: Path) -> None:
        """Only the top-level engine owns ``engine-state.json``."""
        from amplifier_recipe_runner import execution

        source = Path(execution.__file__).read_text(encoding="utf-8")
        assert "checkpoint=checkpoint if (owns_state and store is not None) else None" in source
        assert source.count("owns_state=True") == 1, "exactly one call site may own the run's state"


class _EmptyLedger:
    def to_mapping(self) -> dict[str, Any]:
        return {}


@pytest.mark.parametrize("flag,expected", [("true", True), ("false", False), (None, False)])
def test_the_field_is_parsed_rather_than_ignored(flag: str | None, expected: bool) -> None:
    line = f"    checkpoint_iterations: {flag}\n" if flag is not None else ""
    body = f"""
steps:
  - id: loop
    foreach: "{{{{items}}}}"
    type: bash
    command: echo x
{line}"""
    program = parse_program(yaml.safe_load(body))
    assert program.steps[0].checkpoint_iterations is expected
