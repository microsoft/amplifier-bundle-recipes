"""One v2 run, ONE session -- and it is the id the caller is handed (recipes-ppu).

The defect, reproduced live on ``examples/simple-analysis-recipe.yaml``:
``execute`` on a ``schema_version: 2`` recipe created *two* Amplifier recipe
sessions and returned the id of the one that held none of the run.

* The tool binds a session so the approval and cancellation ports have real
  state; it then handed the step engine nothing, so the engine created a
  session of its own and every step checkpointed there.
* The returned id's ``state.json`` read ``completed_steps: []``,
  ``context: {}``, ``current_step_index: 0`` -- while the same result reported
  ``status: completed`` and three finished steps.
* ``parent_session_id`` was ``null`` on both, so nothing on disk linked them,
  and ``list`` showed one run as two indistinguishable sessions.

Anything that audits, resumes, garbage-collects or bills against ``state.json``
inherited that inversion -- silently, and looking like a *result* rather than
an error.

The trap in the obvious fix is what these tests also pin: the engine reads
``is_resuming = session_id is not None``, so simply forwarding the bound id
would take the resume branch, load ``state["context"]`` and silently run every
v2 recipe with an EMPTY context. Attaching is a separate request, and
``test_the_callers_context_survives_the_attach`` is what proves it stayed one.

Real ``SessionManager``, real ``RecipeExecutor``, real ``bash`` steps. Only
dependency *resolution* is stubbed; every step really runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplifier_module_tool_recipes import V2_RUN_STATE_KEY
from amplifier_module_tool_recipes import RecipesTool
from amplifier_module_tool_recipes import runner_adapter as ra
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.session import SessionManager

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

RUNNER_AVAILABLE = ra.runner_available()
requires_runner = pytest.mark.skipif(
    not RUNNER_AVAILABLE, reason=f"{ra.RUNNER_DISTRIBUTION} is not importable"
)


# ---------------------------------------------------------------------------
# One recipe shape, written twice: legacy and v2, so "the v2 path now behaves
# like the legacy one" is asserted against the legacy one rather than asserted
# of it.
# ---------------------------------------------------------------------------

_BODY = """\
context:
  out_dir: "OUT_DIR"
  marker: "unset"

steps:
  - id: "write"
    type: "bash"
    command: "echo {{marker}} >> {{out_dir}}/marker.txt"
    output: "written"

  - id: "again"
    type: "bash"
    command: "echo {{marker}}-again >> {{out_dir}}/marker.txt"
    output: "written_again"
"""

LEGACY_FLAT = (
    'name: legacy-flat\ndescription: "flat legacy recipe"\nversion: "1.0.0"\n\n'
    + _BODY
)

V2_FLAT = (
    'schema_version: 2\n\nname: v2-flat\ndescription: "flat v2 recipe"\n'
    'version: "1.0.0"\n\ndependencies: []\n\n' + _BODY
)

STEP_IDS = ("write", "again")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeCoordinator:
    def __init__(self, working_dir: Path) -> None:
        self.config: dict[str, Any] = {
            "agents": {},
            "providers": [{"module": "provider-anthropic"}],
        }
        self.session = object()
        self._capabilities: dict[str, Any] = {"session.working_dir": str(working_dir)}

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value

    def get(self, name: str) -> Any:
        return self.config.get(name)


def make_tool(tmp_path: Path) -> tuple[RecipesTool, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    out_dir = project / "out"
    out_dir.mkdir()
    coordinator = FakeCoordinator(project)
    sessions = SessionManager(tmp_path / "amplifier-sessions")
    executor = RecipeExecutor(coordinator, sessions)
    return RecipesTool(executor, sessions, coordinator, {}), project, out_dir


def write_recipe(tmp_path: Path, name: str, body: str, out_dir: Path) -> Path:
    path = tmp_path / name
    path.write_text(body.replace("OUT_DIR", str(out_dir)), encoding="utf-8")
    return path


def install_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub dependency resolution only. Every step still really runs."""
    from amplifier_recipe_runner.api import EffectivePolicy
    from amplifier_recipe_runner.api import ExecutionPlan
    from amplifier_recipe_runner.api import LockMode

    runner = ra.load_runner()

    async def fake_plan(request: Any) -> Any:
        return ExecutionPlan(
            recipe_digest="sha256:test",
            schema_version=2,
            dependencies=(),
            agents={},
            step_ids=STEP_IDS,
            policy=EffectivePolicy(lock_mode=LockMode.LOCKED),
        )

    monkeypatch.setattr(runner, "plan", fake_plan)


def lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


async def run_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
) -> tuple[RecipesTool, Path, Path, Any]:
    tool, project, out_dir = make_tool(tmp_path)
    if schema == "v2":
        recipe = write_recipe(tmp_path, "v2.yaml", V2_FLAT, out_dir)
        install_plan(monkeypatch)
    else:
        recipe = write_recipe(tmp_path, "legacy.yaml", LEGACY_FLAT, out_dir)

    executed = await tool._execute_recipe(
        {"recipe_path": str(recipe), "context": {"marker": "from-the-caller"}}
    )
    assert executed.success is True, executed.error
    assert executed.output["status"] == "completed"
    return tool, project, out_dir, executed


# ---------------------------------------------------------------------------
# The invariant, asserted on BOTH engines from the same scenario
# ---------------------------------------------------------------------------


@requires_runner
@pytest.mark.parametrize("schema", ["legacy", "v2"])
class TestTheReportedIdHoldsTheRun:
    @pytest.mark.asyncio
    async def test_the_returned_sessions_state_answers_what_the_run_did(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
    ):
        """Resolve the returned id, read ``state.json``, see the run.

        This is the documented, obvious thing a caller does, and on the v2 path
        it used to report a run that had apparently done nothing.
        """
        tool, project, _out_dir, executed = await run_flat(
            tmp_path, monkeypatch, schema
        )
        session_id = executed.output["session_id"]

        state = tool.session_manager.load_state(session_id, project)
        assert state["completed_steps"] == list(STEP_IDS)
        assert state["current_step_index"] == len(STEP_IDS)
        # Both steps' outputs are here, and the context names this session as
        # the run's own -- the two records cannot disagree about who ran it.
        assert {"written", "written_again"} <= set(state["context"])
        assert state["context"]["session"]["id"] == session_id

    @pytest.mark.asyncio
    async def test_the_callers_context_survives_the_attach(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
    ):
        """The trap: adopting a session must NOT read as "resume".

        ``is_resuming = session_id is not None``, and the resume branches
        replace the caller's ``context_vars`` with the session's stored
        ``context``. Handing the bound id in as ``session_id`` would therefore
        have run every v2 recipe with an empty context -- silently. The steps
        template ``{{marker}}``, so a discarded context shows up as output.
        """
        tool, project, out_dir, executed = await run_flat(
            tmp_path, monkeypatch, schema
        )
        session_id = executed.output["session_id"]

        assert lines(out_dir / "marker.txt") == [
            "from-the-caller",
            "from-the-caller-again",
        ]
        state = tool.session_manager.load_state(session_id, project)
        assert state["context"]["marker"] == "from-the-caller"

    @pytest.mark.asyncio
    async def test_the_session_listing_shows_the_run_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
    ):
        """One run, one entry.

        An operator reading a two-entry listing cannot tell "one run, reported
        twice" from "two runs, one of which did nothing".
        """
        tool, _project, _out_dir, executed = await run_flat(
            tmp_path, monkeypatch, schema
        )
        listed = await tool._list_sessions({})
        assert listed.success is True, listed.error
        assert [s["session_id"] for s in listed.output["sessions"]] == [
            executed.output["session_id"]
        ]
        assert listed.output["count"] == 1


@requires_runner
class TestV2RunIdentity:
    @pytest.mark.asyncio
    async def test_the_summary_session_is_the_returned_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``summary.session.id`` was the *other* id -- the state-holder.

        The harness that read it was working around the split. There is
        nothing left to work around.
        """
        _tool, _project, _out_dir, executed = await run_flat(tmp_path, monkeypatch, "v2")
        assert (
            executed.output["summary"]["session"]["id"]
            == executed.output["session_id"]
        )

    @pytest.mark.asyncio
    async def test_the_run_record_points_at_the_session_holding_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``engine_session_id`` names this session, not a second one."""
        tool, project, _out_dir, executed = await run_flat(tmp_path, monkeypatch, "v2")
        session_id = executed.output["session_id"]

        # The envelope and the session agree, rather than contradicting.
        assert executed.output["completed_steps"] == list(STEP_IDS)

        record = tool.session_manager.load_state(session_id, project)[V2_RUN_STATE_KEY]
        assert record["session_id"] == session_id
        assert record["engine_session_id"] == session_id
        assert record["status"] == "succeeded"
        assert record["completed_steps"] == list(STEP_IDS)


class TestAttachIsNotResume:
    """``attach_session_id`` and ``session_id`` are different requests."""

    @pytest.mark.asyncio
    async def test_passing_both_refuses_rather_than_letting_one_win(
        self, tmp_path: Path
    ):
        """Silently preferring one would re-run or wipe a run, depending which."""
        project = tmp_path / "project"
        project.mkdir()
        sessions = SessionManager(tmp_path / "amplifier-sessions")
        executor = RecipeExecutor(FakeCoordinator(project), sessions)
        recipe = Recipe(name="r", description="", version="1.0.0", steps=[])

        with pytest.raises(ValueError, match="pass exactly one"):
            await executor.execute_recipe(
                recipe,
                {},
                project,
                session_id="resume-me",
                attach_session_id="attach-me",
            )

    @pytest.mark.asyncio
    async def test_an_adopted_session_keeps_its_own_start_time(self, tmp_path: Path):
        """The run belongs to the session that was bound before it started."""
        project = tmp_path / "project"
        project.mkdir()
        sessions = SessionManager(tmp_path / "amplifier-sessions")
        executor = RecipeExecutor(FakeCoordinator(project), sessions)
        recipe = Recipe(name="r", description="", version="1.0.0", steps=[])

        bound = sessions.create_session(recipe, project)
        started = sessions.load_state(bound, project)["started"]

        context = await executor.execute_recipe(
            recipe, {"kept": "yes"}, project, attach_session_id=bound
        )

        assert context["session"]["id"] == bound
        assert context["session"]["started"] == started
        assert context["kept"] == "yes"
