"""A gate can be placed BEFORE the stage it guards (recipes-vtj).

The defect: ``approval:`` under a stage has always meant "pause **after** this
stage" -- the executor runs every step, appends the stage to
``completed_stages``, and only then looks for a gate. That is a *review*
checkpoint, and it is a sound default. But ``ApprovalConfig`` had no field that
could express the other shape -- "do not run this stage unless a human says so"
-- so an *authorisation* checkpoint could not be written at all, and the
shipped ``dependency-upgrade-staged-recipe.yaml`` was written as if it could:
its gate sat on ``validation``, its prompt rendered the *previous* stage's
outputs, and the operator was asked to approve validating compatibility at a
moment when ``compatibility_check`` was already in context.

``when: before_stage`` is that missing shape. These tests pin it on
observables, not internals: a counting ``session.spawn`` and real ``bash``
steps writing real files, so "the gated stage did not run" and "it ran exactly
once" are measurements. The ``after_stage`` and no-``when`` variants run the
same recipe through the same harness as controls -- the guarantee is that
adding this field changed nothing for a recipe that does not use it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplifier_module_tool_recipes import RecipesTool
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.session import SessionManager

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# One recipe shape, three gate positions
# ---------------------------------------------------------------------------

_RECIPE = """\
name: gate-position
description: "assess, then act under a gate, then wrap up"
version: "1.0.0"

context:
  out_dir: "OUT_DIR"

stages:
  - name: "assess"
    steps:
      - id: "assess-step"
        type: "bash"
        command: "echo assessed >> {{out_dir}}/assess.txt && echo assessment-text"
        output: "assessment"

  - name: "act"
    approval:
      required: true
      prompt: "Act on {{assessment}}?"
      timeout: 0
      default: "deny"
GATE_WHEN
    steps:
      - id: "act-step"
        agent: "tester:doer"
        prompt: "Act on {{assessment}}"
        output: "action"

      - id: "record-act"
        type: "bash"
        command: "echo acted >> {{out_dir}}/act.txt"
        output: "act_record"

  - name: "wrap"
    steps:
      - id: "wrap-step"
        type: "bash"
        command: "echo wrapped >> {{out_dir}}/wrap.txt"
        output: "wrap"
"""

# Both gates before their stage: the second is only ever reached by a resume,
# which is where a gate that suppressed itself for the whole run would show up.
_TWO_BEFORE_GATES = """\
name: two-before-gates
description: "two authorisation gates, back to back"
version: "1.0.0"

context:
  out_dir: "OUT_DIR"

stages:
  - name: "assess"
    steps:
      - id: "assess-step"
        type: "bash"
        command: "echo assessed >> {{out_dir}}/assess.txt"
        output: "assessment"

  - name: "act"
    approval:
      required: true
      prompt: "Run act?"
      when: "before_stage"
    steps:
      - id: "act-step"
        type: "bash"
        command: "echo acted >> {{out_dir}}/act.txt"
        output: "action"

  - name: "wrap"
    approval:
      required: true
      prompt: "Run wrap?"
      when: "before_stage"
    steps:
      - id: "wrap-step"
        type: "bash"
        command: "echo wrapped >> {{out_dir}}/wrap.txt"
        output: "wrap"
"""

ALL_STEP_IDS = ["assess-step", "act-step", "record-act", "wrap-step"]

GATE_WHEN = {
    "before_stage": '      when: "before_stage"',
    "after_stage": '      when: "after_stage"',
    # The field omitted entirely -- every recipe written before it existed.
    "omitted": "",
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class CountingSpawn:
    """The host's ``session.spawn``, counting every agent step that runs.

    A gate that fired too late -- or a resume that re-ran an approved stage --
    shows up here as a call that should not exist.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"output": "acted", "session_id": f"child-{len(self.calls)}"}


class FakeCoordinator:
    def __init__(self, working_dir: Path, spawn: Any) -> None:
        self.config: dict[str, Any] = {
            "agents": {"tester:doer": {"name": "doer", "description": "the doer"}},
            "providers": [{"module": "provider-anthropic"}],
        }
        self.session = object()
        self._capabilities: dict[str, Any] = {
            "session.working_dir": str(working_dir),
            "session.spawn": spawn,
        }

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value

    def get(self, name: str) -> Any:
        return self.config.get(name)


def make_tool(tmp_path: Path) -> tuple[RecipesTool, Path, Path, CountingSpawn]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    out_dir = project / "out"
    out_dir.mkdir()
    spawn = CountingSpawn()
    coordinator = FakeCoordinator(project, spawn)
    sessions = SessionManager(tmp_path / "amplifier-sessions")
    executor = RecipeExecutor(coordinator, sessions)
    tool = RecipesTool(executor, sessions, coordinator, {})
    return tool, project, out_dir, spawn


def write_recipe(tmp_path: Path, body: str, out_dir: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text(body.replace("OUT_DIR", str(out_dir)), encoding="utf-8")
    return path


def lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


async def run_to_gate(
    tmp_path: Path, position: str
) -> tuple[RecipesTool, Path, Path, CountingSpawn, Any]:
    """Execute the shared recipe with the gate in the given position."""
    tool, project, out_dir, spawn = make_tool(tmp_path)
    recipe = write_recipe(
        tmp_path,
        _RECIPE.replace("GATE_WHEN", GATE_WHEN[position]),
        out_dir,
        f"gate-{position}.yaml",
    )
    executed = await tool._execute_recipe({"recipe_path": str(recipe)})
    assert executed.success is True, executed.error
    return tool, project, out_dir, spawn, executed


def gate_state(tool: RecipesTool, session_id: str, project: Path) -> dict[str, Any]:
    """The engine checkpoint a caller's own tooling would read at the pause."""
    state = tool.session_manager.load_state(session_id, project)
    return {
        "pending_approval_stage": state.get("pending_approval_stage"),
        "completed_steps": list(state.get("completed_steps") or []),
        "completed_stages": list(state.get("completed_stages") or []),
        "current_stage_index": state.get("current_stage_index"),
        "current_step_in_stage": state.get("current_step_in_stage"),
    }


# ---------------------------------------------------------------------------
# before_stage: the shape that could not be written
# ---------------------------------------------------------------------------


class TestBeforeStageGate:
    @pytest.mark.asyncio
    async def test_it_pauses_before_the_gated_stages_first_step(
        self, tmp_path: Path
    ):
        """Nothing in the gated stage has run when the operator is asked.

        This is the whole point: at an ``after_stage`` gate the answer can no
        longer prevent anything. Measured on the side effects -- zero spawns,
        no ``act.txt`` -- not on internal bookkeeping alone.
        """
        tool, project, out_dir, spawn, executed = await run_to_gate(
            tmp_path, "before_stage"
        )

        assert executed.output["status"] == "paused_for_approval"
        assert executed.output["stage_name"] == "act"

        # The stage's agent step never spawned, and its bash step never wrote.
        assert spawn.calls == []
        assert lines(out_dir / "act.txt") == []
        # The stage before it did run -- so this is a gate, not a failure.
        assert lines(out_dir / "assess.txt") == ["assessed"]

        state = gate_state(tool, executed.output["session_id"], project)
        assert state["pending_approval_stage"] == "act"
        # The gated stage's steps are absent from completed_steps ...
        assert state["completed_steps"] == ["assess-step"]
        # ... and the stage itself is NOT closed, which is the stronger claim.
        assert state["completed_stages"] == ["assess"]
        # The run is parked ON the gated stage (index 1), not past it.
        assert state["current_stage_index"] == 1
        assert state["current_step_in_stage"] == 0

    @pytest.mark.asyncio
    async def test_the_prompt_renders_what_is_known_at_that_moment(
        self, tmp_path: Path
    ):
        """A pre-stage prompt can only speak of earlier stages' outputs.

        ``{{assessment}}`` comes from the stage *before* the gate, so it is
        real at the pause -- the shipped example's prompts were written for
        exactly this position and only make sense here.
        """
        _tool, _project, _out_dir, _spawn, executed = await run_to_gate(
            tmp_path, "before_stage"
        )
        assert "assessment-text" in executed.output["approval_prompt"]

    @pytest.mark.asyncio
    async def test_approve_then_resume_runs_the_stage_exactly_once(
        self, tmp_path: Path
    ):
        """The documented round trip closes, and nothing runs twice.

        A before-stage gate parks the run on its OWN stage, so a naive fix
        would either re-park at the same gate forever or replay the stage.
        Counted at the spawn and at the file: one of each.
        """
        tool, _project, out_dir, spawn, executed = await run_to_gate(
            tmp_path, "before_stage"
        )
        session_id = executed.output["session_id"]

        approved = await tool._approve_stage(
            {"session_id": session_id, "stage_name": "act"}
        )
        assert approved.success is True, approved.error

        finished = await tool._resume_recipe({"session_id": session_id})
        assert finished.success is True, finished.error
        assert finished.output["status"] == "completed"
        assert finished.output["session_id"] == session_id

        assert len(spawn.calls) == 1
        assert spawn.calls[0]["agent_name"] == "tester:doer"
        assert lines(out_dir / "assess.txt") == ["assessed"]
        assert lines(out_dir / "act.txt") == ["acted"]
        assert lines(out_dir / "wrap.txt") == ["wrapped"]

    @pytest.mark.asyncio
    async def test_denying_it_means_the_stage_never_ran(self, tmp_path: Path):
        """The property an after-stage gate cannot offer.

        Denying a post-stage gate stops the *following* stages; the stage's own
        work is already done. Denying this one leaves it undone.
        """
        tool, _project, out_dir, spawn, executed = await run_to_gate(
            tmp_path, "before_stage"
        )
        session_id = executed.output["session_id"]

        denied = await tool._deny_stage(
            {"session_id": session_id, "stage_name": "act", "reason": "not now"}
        )
        assert denied.success is True, denied.error

        resumed = await tool._resume_recipe({"session_id": session_id})
        assert resumed.success is False
        assert "denied" in resumed.error["message"].lower()

        assert spawn.calls == []
        assert lines(out_dir / "act.txt") == []
        assert lines(out_dir / "wrap.txt") == []

    @pytest.mark.asyncio
    async def test_a_later_gate_still_fires_after_an_earlier_one_is_approved(
        self, tmp_path: Path
    ):
        """Passing gate 1 does not disarm gate 2.

        The resume that carries a run through a before-stage gate has to
        suppress that gate for exactly one stage. This is the guard on
        "exactly one": the next gated stage still stops.
        """
        tool, project, out_dir, _spawn = make_tool(tmp_path)
        recipe = write_recipe(tmp_path, _TWO_BEFORE_GATES, out_dir, "two-gates.yaml")

        executed = await tool._execute_recipe({"recipe_path": str(recipe)})
        assert executed.output["status"] == "paused_for_approval"
        assert executed.output["stage_name"] == "act"
        session_id = executed.output["session_id"]

        await tool._approve_stage({"session_id": session_id, "stage_name": "act"})
        at_gate_two = await tool._resume_recipe({"session_id": session_id})
        assert at_gate_two.success is True, at_gate_two.error
        assert at_gate_two.output["status"] == "paused_for_approval"
        assert at_gate_two.output["stage_name"] == "wrap"

        # Stage 'act' ran on that resume; 'wrap' has not run at all.
        assert lines(out_dir / "act.txt") == ["acted"]
        assert lines(out_dir / "wrap.txt") == []
        state = gate_state(tool, session_id, project)
        assert state["completed_stages"] == ["assess", "act"]
        assert state["current_stage_index"] == 2

        await tool._approve_stage({"session_id": session_id, "stage_name": "wrap"})
        finished = await tool._resume_recipe({"session_id": session_id})
        assert finished.success is True, finished.error
        assert finished.output["status"] == "completed"

        # Every stage ran, each exactly once.
        assert lines(out_dir / "assess.txt") == ["assessed"]
        assert lines(out_dir / "act.txt") == ["acted"]
        assert lines(out_dir / "wrap.txt") == ["wrapped"]

    @pytest.mark.asyncio
    async def test_the_approval_message_still_reaches_the_gated_stage(
        self, tmp_path: Path
    ):
        """Same protocol as an after-stage gate, including ``_approval_message``."""
        tool, project, _out_dir, _spawn, executed = await run_to_gate(
            tmp_path, "before_stage"
        )
        session_id = executed.output["session_id"]

        await tool._approve_stage(
            {"session_id": session_id, "stage_name": "act", "message": "ship it"}
        )
        await tool._resume_recipe({"session_id": session_id})

        context = tool.session_manager.load_state(session_id, project)["context"]
        assert context["_approval_message"] == "ship it"


# ---------------------------------------------------------------------------
# after_stage: the default, unchanged
# ---------------------------------------------------------------------------


class TestAfterStageIsUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("position", ["after_stage", "omitted"])
    async def test_the_gate_still_fires_with_its_stage_already_done(
        self, tmp_path: Path, position: str
    ):
        """Today's behaviour, stated as a fact rather than assumed.

        Written explicitly and omitted entirely must be the same thing, or the
        new field would have changed the meaning of every existing recipe.
        """
        tool, project, out_dir, spawn, executed = await run_to_gate(
            tmp_path, position
        )

        assert executed.output["status"] == "paused_for_approval"
        assert executed.output["stage_name"] == "act"

        # The gated stage HAS run by the time the operator is asked.
        assert len(spawn.calls) == 1
        assert lines(out_dir / "act.txt") == ["acted"]
        # ... and the stage is closed, with the run parked on the NEXT one.
        state = gate_state(tool, executed.output["session_id"], project)
        assert state["completed_steps"] == ["assess-step", "act-step", "record-act"]
        assert state["completed_stages"] == ["assess", "act"]
        assert state["current_stage_index"] == 2
        assert state["current_step_in_stage"] == 0

    @pytest.mark.asyncio
    async def test_omitting_when_is_byte_for_byte_the_after_stage_behaviour(
        self, tmp_path: Path
    ):
        """The two variants produce the same observable checkpoint."""
        tool_a, project_a, out_a, spawn_a, executed_a = await run_to_gate(
            tmp_path / "explicit", "after_stage"
        )
        tool_b, project_b, out_b, spawn_b, executed_b = await run_to_gate(
            tmp_path / "omitted", "omitted"
        )

        assert gate_state(tool_a, executed_a.output["session_id"], project_a) == (
            gate_state(tool_b, executed_b.output["session_id"], project_b)
        )
        assert len(spawn_a.calls) == len(spawn_b.calls) == 1
        assert lines(out_a / "act.txt") == lines(out_b / "act.txt") == ["acted"]
        assert executed_a.output["approval_prompt"] == (
            executed_b.output["approval_prompt"]
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("position", ["after_stage", "omitted"])
    async def test_approve_then_resume_still_finishes_without_replay(
        self, tmp_path: Path, position: str
    ):
        """The existing round trip, unaffected by the new field."""
        tool, _project, out_dir, spawn, executed = await run_to_gate(
            tmp_path, position
        )
        session_id = executed.output["session_id"]

        await tool._approve_stage({"session_id": session_id, "stage_name": "act"})
        finished = await tool._resume_recipe({"session_id": session_id})

        assert finished.success is True, finished.error
        assert finished.output["status"] == "completed"
        assert len(spawn.calls) == 1
        assert lines(out_dir / "act.txt") == ["acted"]
        assert lines(out_dir / "wrap.txt") == ["wrapped"]
