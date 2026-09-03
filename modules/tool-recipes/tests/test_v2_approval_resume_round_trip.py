"""A gated schema-v2 recipe can actually be finished (recipes-zyp / recipes-o4k).

The defect, measured live on ``amplifier-ecosystem-audit.yaml`` and
``document-generation.yaml``: a ``schema_version: 2`` staged recipe ran up to
its first ``approval.required: true`` gate, reported
``status: paused_for_approval``, accepted an ``approve`` -- and then refused
every ``resume``::

    Session <id> holds a schema_version 2 recipe but recorded no run outcome,
    so which of its steps completed is unknown. It was NOT resumed ...

Re-running with ``execute`` just re-hit the same gate, so a gated v2 recipe
could execute exactly one stage and no further, forever. Two things had to be
true for the documented "approve ... then resume" workflow to close:

* the pause has to leave a run outcome behind -- ours, recorded against the
  session the caller was handed, or failing that the step engine's own
  checkpoint (:meth:`RecipesTool._record_from_engine_checkpoint`), so that
  "which steps completed" is answerable rather than guessed;
* ``approvals`` has to stop reporting a gate the caller has already approved
  (facet b), or the caller's own approval reads as if it never took.

These tests drive the whole round trip through the tool's public operations,
with a real ``SessionManager``, a real ``RecipeExecutor``, real ``bash`` steps
and a counting spawn for the agent steps -- so "stage 1 did not run twice" is
an observation, not an assertion about internals. Only dependency *resolution*
is stubbed; every step really runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplifier_module_tool_recipes import V2_RUN_STATE_KEY
from amplifier_module_tool_recipes import RecipesTool
from amplifier_module_tool_recipes import runner_adapter as ra
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.session import SessionManager

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

RUNNER_AVAILABLE = ra.runner_available()
requires_runner = pytest.mark.skipif(
    not RUNNER_AVAILABLE, reason=f"{ra.RUNNER_DISTRIBUTION} is not importable"
)

LEGACY_ENGINE = ra.V2_LEGACY_ENGINE_EXECUTION_MODE

AGENT_FILE = """\
---
meta:
  name: auditor
  description: The auditor the RECIPE declared
---
You are the declared auditor.
"""

# Three stages, the first TWO gated -- the shape amplifier-ecosystem-audit.yaml
# has, and the one a single-gate test cannot exercise: the second gate is only
# ever reached by a resume.
TWO_GATE_RECIPE = """\
schema_version: 2

name: two-gate-audit
description: "A staged v2 recipe with two approval gates"
version: "1.0.0"

dependencies:
  - source: "bundles/supplier"
    kind: bundle
    required_agents:
      - "supplier:auditor"

context:
  out_dir: "OUT_DIR"

stages:
  - name: "discovery"
    steps:
      - id: "list-repos"
        type: "bash"
        command: "echo listed >> {{out_dir}}/discovery.txt && echo '{\\"repos\\": [\\"alpha\\"]}'"
        parse_json: true
        output: "repos_data"

      - id: "plan-audit"
        agent: "supplier:auditor"
        prompt: "Plan an audit of {{repos_data.repos}}"
        output: "audit_plan"
    approval:
      required: true
      prompt: "Audit {{repos_data.repos}}?"
      timeout: 0
      default: "deny"

  - name: "audits"
    steps:
      - id: "run-audit"
        agent: "supplier:auditor"
        prompt: "Audit it: {{audit_plan}}"
        output: "audit_result"
    approval:
      required: true
      prompt: "Publish the audit?"
      timeout: 0
      default: "deny"

  - name: "reporting"
    steps:
      - id: "report"
        type: "bash"
        command: "echo reported >> {{out_dir}}/report.txt"
        output: "report"
"""

STEP_IDS = ("list-repos", "plan-audit", "run-audit", "report")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class CountingSpawn:
    """The host's ``session.spawn``, counting every agent step that runs.

    A resume that re-ran stage 1 would show up here as a second
    ``plan-audit`` call -- the observable form of "it re-executed steps that
    had already run", which is exactly what the refusal existed to prevent
    and what a wrong fix would cause.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"output": "audited", "session_id": f"child-{len(self.calls)}"}

    @property
    def prompts_by_agent(self) -> list[str]:
        return [call.get("prompt", "") for call in self.calls]


class FakeCoordinator:
    def __init__(self, working_dir: Path, spawn: Any) -> None:
        self.config: dict[str, Any] = {
            # The caller's own map is deliberately non-empty and colliding:
            # a resume that fell back to the caller-bound path would resolve
            # THIS definition instead of the recipe's declared one.
            "agents": {
                "supplier:auditor": {
                    "name": "auditor",
                    "description": "the CALLER's impostor",
                }
            },
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
    project.mkdir()
    out_dir = project / "out"
    out_dir.mkdir()
    spawn = CountingSpawn()
    coordinator = FakeCoordinator(project, spawn)
    sessions = SessionManager(tmp_path / "amplifier-sessions")
    executor = RecipeExecutor(coordinator, sessions)
    tool = RecipesTool(executor, sessions, coordinator, {})
    return tool, project, out_dir, spawn


def write_agent(tmp_path: Path) -> Path:
    agents_dir = tmp_path / "supplier" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / "auditor.md"
    path.write_text(AGENT_FILE, encoding="utf-8")
    return path


def write_recipe(tmp_path: Path, out_dir: Path) -> Path:
    path = tmp_path / "two-gate.yaml"
    path.write_text(TWO_GATE_RECIPE.replace("OUT_DIR", str(out_dir)), encoding="utf-8")
    return path


def install_plan(monkeypatch: pytest.MonkeyPatch, agent_path: Path) -> None:
    """Stub dependency resolution only. Every step still really runs."""
    from amplifier_recipe_runner.api import AgentProvenance
    from amplifier_recipe_runner.api import EffectivePolicy
    from amplifier_recipe_runner.api import ExecutionPlan
    from amplifier_recipe_runner.api import LockMode

    runner = ra.load_runner()

    async def fake_plan(request: Any) -> Any:
        return ExecutionPlan(
            recipe_digest="sha256:test",
            schema_version=2,
            dependencies=(),
            agents={
                "supplier:auditor": AgentProvenance(
                    agent="supplier:auditor",
                    supplied_by="bundles/supplier",
                    dependency_digest="sha256:dep",
                    local_path=str(agent_path),
                )
            },
            step_ids=STEP_IDS,
            policy=EffectivePolicy(lock_mode=LockMode.LOCKED),
        )

    monkeypatch.setattr(runner, "plan", fake_plan)


def lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


async def pause_at_first_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RecipesTool, Path, Path, CountingSpawn, Any]:
    tool, project, out_dir, spawn = make_tool(tmp_path)
    install_plan(monkeypatch, write_agent(tmp_path))
    recipe = write_recipe(tmp_path, out_dir)

    executed = await tool._execute_recipe({"recipe_path": str(recipe)})
    assert executed.success is True, executed.error
    assert executed.output["status"] == "paused_for_approval"
    assert executed.output["stage_name"] == "discovery"
    return tool, project, out_dir, spawn, executed


# ---------------------------------------------------------------------------
# The round trip the defect made impossible
# ---------------------------------------------------------------------------


@requires_runner
class TestGatedV2RecipeCanBeFinished:
    @pytest.mark.asyncio
    async def test_execute_approve_resume_twice_reaches_the_last_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Two gates, two approvals, and the recipe actually finishes.

        This is the o4k/zyp repro end to end: before the fix the first
        ``resume`` was refused outright, so the third stage was unreachable by
        any sequence of operations.
        """
        tool, project, out_dir, spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]

        # Gate 1: stage one ran, nothing beyond it did.
        assert lines(out_dir / "discovery.txt") == ["listed"]
        assert lines(out_dir / "report.txt") == []
        assert len(spawn.calls) == 1

        approved = await tool._approve_stage(
            {"session_id": session_id, "stage_name": "discovery"}
        )
        assert approved.success is True, approved.error

        resumed = await tool._resume_recipe({"session_id": session_id})
        assert resumed.success is True, resumed.error
        # It continued into stage 2 and stopped at THAT gate -- not refused,
        # and not restarted from the top.
        assert resumed.output["status"] == "paused_for_approval"
        assert resumed.output["stage_name"] == "audits"
        assert resumed.output["session_id"] == session_id

        approved2 = await tool._approve_stage(
            {"session_id": session_id, "stage_name": "audits"}
        )
        assert approved2.success is True, approved2.error

        finished = await tool._resume_recipe({"session_id": session_id})
        assert finished.success is True, finished.error
        assert finished.output["status"] == "completed"
        assert finished.output["session_id"] == session_id

        # Every stage really ran, each exactly once.
        assert lines(out_dir / "discovery.txt") == ["listed"]
        assert lines(out_dir / "report.txt") == ["reported"]
        assert list(finished.output["completed_steps"]) == list(STEP_IDS)

        record = tool.session_manager.load_state(session_id, project)[V2_RUN_STATE_KEY]
        assert record["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_the_pause_records_a_run_outcome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The pause leaves behind what resume needs, under the caller's id.

        The refusal fired because an approval pause recorded nothing, so the
        run's own session held a paused recipe and no statement of what had
        completed. It records one now -- against the id ``execute`` reported,
        which is the id the caller has.
        """
        tool, project, _out_dir, _spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]

        state = tool.session_manager.load_state(session_id, project)
        record = state[V2_RUN_STATE_KEY]

        assert record["status"] == "paused"
        assert record["execution_mode"] == LEGACY_ENGINE
        # What completed is stated, not guessed: stage one's steps and no more.
        assert record["completed_steps"] == ["list-repos", "plan-audit"]
        assert record["engine_session_id"]

    @pytest.mark.asyncio
    async def test_the_paused_result_carries_the_id_approve_accepts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """One id, taken straight from the pause, drives the whole workflow."""
        tool, _project, _out_dir, _spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]

        listed = await tool._list_approvals({})
        assert [entry["session_id"] for entry in listed.output["pending_approvals"]] == [
            session_id
        ]

        approved = await tool._approve_stage(
            {"session_id": session_id, "stage_name": "discovery"}
        )
        assert approved.success is True, approved.error
        assert approved.output["session_id"] == session_id

    @pytest.mark.asyncio
    async def test_approvals_is_empty_after_a_successful_approve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """recipes-o4k facet (b): an approved gate stops being pending.

        Before the fix ``approvals`` kept listing the same session and stage
        after ``approve`` returned ``{'status': 'approved'}``, so the caller
        had no way to tell an approval that took from one that did not.
        """
        tool, _project, _out_dir, _spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]

        before = await tool._list_approvals({})
        assert before.output["count"] == 1
        assert before.output["pending_approvals"][0]["stage_name"] == "discovery"

        await tool._approve_stage({"session_id": session_id, "stage_name": "discovery"})

        after = await tool._list_approvals({})
        assert after.output["pending_approvals"] == []
        assert after.output["count"] == 0

        # ... and the NEXT gate appears in its place once resume reaches it,
        # so the listing is tracking the run rather than merely going quiet.
        await tool._resume_recipe({"session_id": session_id})
        at_gate_two = await tool._list_approvals({})
        assert [
            (entry["session_id"], entry["stage_name"])
            for entry in at_gate_two.output["pending_approvals"]
        ] == [(session_id, "audits")]

    @pytest.mark.asyncio
    async def test_resume_stays_on_the_closed_world_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Every leg of the round trip reports the engine that ran it.

        recipes-5c6: handing a paused v2 run's remainder to the library's
        sequential executor made it die on step shapes the same run had
        already executed past. The label is how that stays visible.
        """
        tool, _project, _out_dir, _spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]
        assert executed.output["execution_mode"] == LEGACY_ENGINE

        await tool._approve_stage({"session_id": session_id, "stage_name": "discovery"})
        resumed = await tool._resume_recipe({"session_id": session_id})
        assert resumed.output["execution_mode"] == LEGACY_ENGINE

        await tool._approve_stage({"session_id": session_id, "stage_name": "audits"})
        finished = await tool._resume_recipe({"session_id": session_id})
        assert finished.output["execution_mode"] == LEGACY_ENGINE

    @pytest.mark.asyncio
    async def test_resume_does_not_re_run_the_approved_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Counted at the spawn: stage 1's agent step runs exactly once.

        The refusal this fix narrows was protecting something real -- resuming
        as if nothing had run would re-execute completed steps. So the fix has
        to be proven not to do that, on the side effects themselves.
        """
        tool, _project, out_dir, spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]
        assert len(spawn.calls) == 1
        assert spawn.calls[0]["agent_name"] == "supplier:auditor"

        await tool._approve_stage({"session_id": session_id, "stage_name": "discovery"})
        await tool._resume_recipe({"session_id": session_id})
        await tool._approve_stage({"session_id": session_id, "stage_name": "audits"})
        await tool._resume_recipe({"session_id": session_id})

        # Two agent steps in the recipe, one spawn each -- never three.
        assert len(spawn.calls) == 2
        # The bash step of stage 1 appended once, not twice.
        assert lines(out_dir / "discovery.txt") == ["listed"]
        assert lines(out_dir / "report.txt") == ["reported"]

        # And each spawn resolved the RECIPE's auditor, never the caller's
        # colliding impostor -- resume never leaves the closed world.
        for call in spawn.calls:
            assert set(call["agent_configs"]) == {"supplier:auditor"}
            assert call["agent_configs"]["supplier:auditor"]["description"] == (
                "The auditor the RECIPE declared"
            )

    @pytest.mark.asyncio
    async def test_approval_message_still_reaches_the_next_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The gate record outlives ``approve`` for a reason.

        The message a caller approves with is injected into the recipe context
        by the *executor*, on the next resume, out of the still-present gate
        record. Dropping that record to make ``approvals`` honest would have
        silently emptied ``_approval_message`` -- so the listing is filtered by
        the stage's status instead, and this is the guard on that choice.
        """
        tool, project, _out_dir, _spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]

        await tool._approve_stage(
            {
                "session_id": session_id,
                "stage_name": "discovery",
                "message": "ship it",
            }
        )
        await tool._resume_recipe({"session_id": session_id})

        engine_session_id = tool.session_manager.load_state(session_id, project)[
            V2_RUN_STATE_KEY
        ]["engine_session_id"]
        context = tool.session_manager.load_state(engine_session_id, project)["context"]
        assert context["_approval_message"] == "ship it"


# ---------------------------------------------------------------------------
# The refusal: narrowed, not removed
# ---------------------------------------------------------------------------


@requires_runner
class TestOutcomelessSessionsAreStillRefused:
    @pytest.mark.asyncio
    async def test_a_v2_session_with_no_outcome_at_all_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """No record, no checkpoint -- still refused, in the same words.

        This is the safety the fix must keep: a v2 session that never reported
        what ran cannot be resumed by assuming nothing did.
        """
        tool, project, out_dir, _spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]

        # Strip every trace of an outcome from the session the caller holds,
        # and from the engine session it points at.
        state = tool.session_manager.load_state(session_id, project)
        engine_session_id = state[V2_RUN_STATE_KEY]["engine_session_id"]
        for target in (session_id, engine_session_id):
            target_state = tool.session_manager.load_state(target, project)
            target_state.pop(V2_RUN_STATE_KEY, None)
            target_state.pop("current_stage_index", None)
            target_state.pop("current_step_index", None)
            target_state.pop("completed_steps", None)
            tool.session_manager.save_state(target, project, target_state)

        refused = await tool._resume_recipe({"session_id": session_id})

        assert refused.success is False
        assert refused.error["type"] == "V2RunNotRecorded"
        assert "recorded no run outcome" in refused.error["message"]
        # It really did refuse: nothing ran a second time.
        assert lines(out_dir / "report.txt") == []

    @pytest.mark.asyncio
    async def test_a_session_that_never_reached_a_step_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A freshly created session is not a checkpoint.

        Every session is born with ``current_step_index: 0`` and
        ``completed_steps: []``. Reading those as "the engine was here" would
        turn the refusal into a silent restart for any session at all, so the
        narrowing has to reject exactly this shape.
        """
        tool, project, _out_dir, _spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]

        state = tool.session_manager.load_state(session_id, project)
        engine_session_id = state[V2_RUN_STATE_KEY]["engine_session_id"]
        for target in (session_id, engine_session_id):
            target_state = tool.session_manager.load_state(target, project)
            target_state.pop(V2_RUN_STATE_KEY, None)
            target_state.pop("current_stage_index", None)
            target_state["current_step_index"] = 0
            target_state["completed_steps"] = []
            tool.session_manager.save_state(target, project, target_state)

        refused = await tool._resume_recipe({"session_id": session_id})

        assert refused.success is False
        assert refused.error["type"] == "V2RunNotRecorded"

    @pytest.mark.asyncio
    async def test_the_engines_own_checkpoint_is_enough_to_resume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A run whose bookkeeping was lost, but whose engine checkpointed.

        This is the shape the o4k reporter was stuck with: the session holding
        the gate and the checkpoint carried no run record of ours, so resume
        refused for lack of an outcome that was, in fact, written down right
        there. It continues on the engine's own checkpoint now -- and still
        does not re-run what that checkpoint says finished.
        """
        tool, project, out_dir, spawn, executed = await pause_at_first_gate(
            tmp_path, monkeypatch
        )
        session_id = executed.output["session_id"]
        engine_session_id = tool.session_manager.load_state(session_id, project)[
            V2_RUN_STATE_KEY
        ]["engine_session_id"]

        await tool._approve_stage({"session_id": session_id, "stage_name": "discovery"})

        # Lose our record everywhere; keep only what the engine wrote.
        for target in (session_id, engine_session_id):
            target_state = tool.session_manager.load_state(target, project)
            target_state.pop(V2_RUN_STATE_KEY, None)
            tool.session_manager.save_state(target, project, target_state)

        resumed = await tool._resume_recipe({"session_id": engine_session_id})

        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "paused_for_approval"
        assert resumed.output["stage_name"] == "audits"
        assert resumed.output["execution_mode"] == LEGACY_ENGINE
        # Stage one was NOT redone: one spawn for plan-audit, one for
        # run-audit, and discovery.txt still has a single line.
        assert len(spawn.calls) == 2
        assert lines(out_dir / "discovery.txt") == ["listed"]
