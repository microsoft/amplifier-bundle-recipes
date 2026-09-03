"""ONE session id survives the whole approval round trip (recipes-3f6).

The defect, reproduced live in sweep C on
``examples/multi-repo-activity-report.yaml``: a staged ``schema_version: 2``
recipe pauses at its gate and ``execute`` reports one session id, but the
caller could not complete the documented "approve ... then resume" workflow
with it.

* ``approve`` accepted the id (recipes-5c6 retargets the write to wherever the
  gate actually lives) but **reported a different one back** -- the engine's
  own session.
* Following that reported id into ``resume`` then conducted the rest of the
  run under the engine's session, leaving the run's OWN session frozen at
  ``status: paused`` forever after the run had finished.
* ``approvals`` listed only the engine id, so the second id was not merely
  unnecessary -- it was the only one the listing ever showed.

The workflow needs exactly one id. These tests pin that end to end, on both
the legacy and the v2 path, with a real ``SessionManager``, a real
``RecipeExecutor`` and real ``bash`` steps: every assertion is about ids a
caller actually receives, plus proof the recipe really ran to completion.
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


# ---------------------------------------------------------------------------
# Two recipes of identical shape -- one legacy, one v2 -- so the round trip is
# asserted on both engines from the same scenario.
# ---------------------------------------------------------------------------

_STAGES = """\
context:
  out_dir: "OUT_DIR"

stages:
  - name: "setup"
    steps:
      - id: "prep"
        type: "bash"
        command: "echo prepped >> {{out_dir}}/prep.txt"
        output: "prep"
    approval:
      required: true
      prompt: "Continue?"
      timeout: 0
      default: "deny"

  - name: "finish"
    steps:
      - id: "report"
        type: "bash"
        command: "echo reported >> {{out_dir}}/report.txt"
        output: "report"
"""

LEGACY_STAGED = (
    'name: legacy-staged\ndescription: "staged legacy recipe with an approval gate"\n'
    'version: "1.0.0"\n\n' + _STAGES
)

V2_STAGED = (
    "schema_version: 2\n\nname: v2-staged\n"
    'description: "staged v2 recipe with an approval gate"\n'
    'version: "1.0.0"\n\ndependencies: []\n\n' + _STAGES
)

STEP_IDS = ("prep", "report")


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
        self._capabilities: dict[str, Any] = {
            "session.working_dir": str(working_dir)
        }

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


async def pause_at_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
) -> tuple[RecipesTool, Path, Path, Any]:
    """execute a staged recipe of the given schema, up to its approval gate."""
    tool, project, out_dir = make_tool(tmp_path)
    if schema == "v2":
        recipe = write_recipe(tmp_path, "v2.yaml", V2_STAGED, out_dir)
        install_plan(monkeypatch)
    else:
        recipe = write_recipe(tmp_path, "legacy.yaml", LEGACY_STAGED, out_dir)

    executed = await tool._execute_recipe({"recipe_path": str(recipe)})
    assert executed.success is True, executed.error
    assert executed.output["status"] == "paused_for_approval"
    assert executed.output["stage_name"] == "setup"
    return tool, project, out_dir, executed


# ---------------------------------------------------------------------------
# The round trip, on both engines
# ---------------------------------------------------------------------------


@requires_runner
@pytest.mark.parametrize("schema", ["legacy", "v2"])
class TestOneSessionIdRoundTrip:
    @pytest.mark.asyncio
    async def test_execute_approve_resume_all_accept_the_same_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
    ):
        """The documented workflow, followed verbatim, with one id.

        Every id a caller is handed at any point of the round trip is the same
        id, and following it to the end really finishes the recipe.
        """
        tool, _project, out_dir, executed = await pause_at_gate(
            tmp_path, monkeypatch, schema
        )
        session_id = executed.output["session_id"]

        approved = await tool._approve_stage(
            {"session_id": session_id, "stage_name": "setup", "message": "merge"}
        )
        assert approved.success is True, approved.error
        # The whole point: approve does not hand back a second id.
        assert approved.output["session_id"] == session_id

        resumed = await tool._resume_recipe(
            {"session_id": approved.output["session_id"]}
        )
        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "completed"
        assert resumed.output["session_id"] == session_id

        # It really ran, and the gated step ran exactly once.
        assert lines(out_dir / "prep.txt") == ["prepped"]
        assert lines(out_dir / "report.txt") == ["reported"]

    @pytest.mark.asyncio
    async def test_approvals_lists_the_id_execute_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
    ):
        """A caller who finds the gate via ``approvals`` gets the same id too.

        Before the fix a v2 run's gate was listed under the engine's session --
        an id the caller had never seen, and the only one the listing offered.
        """
        tool, _project, _out_dir, executed = await pause_at_gate(
            tmp_path, monkeypatch, schema
        )
        session_id = executed.output["session_id"]

        listed = await tool._list_approvals({})
        assert listed.success is True, listed.error
        entries = [
            entry
            for entry in listed.output["pending_approvals"]
            if entry["session_id"] == session_id
        ]
        assert entries, (
            f"{session_id} is not in the approvals listing: "
            f"{listed.output['pending_approvals']}"
        )
        assert entries[0]["stage_name"] == "setup"

        # And that listed id is directly usable, with no translation step.
        approved = await tool._approve_stage(
            {"session_id": entries[0]["session_id"], "stage_name": "setup"}
        )
        assert approved.success is True, approved.error

    @pytest.mark.asyncio
    async def test_deny_reports_the_id_execute_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
    ):
        """``deny`` echoes the caller's own id, exactly as ``approve`` does."""
        tool, _project, out_dir, executed = await pause_at_gate(
            tmp_path, monkeypatch, schema
        )
        session_id = executed.output["session_id"]

        denied = await tool._deny_stage(
            {"session_id": session_id, "stage_name": "setup", "reason": "no"}
        )
        assert denied.success is True, denied.error
        assert denied.output["status"] == "denied"
        assert denied.output["session_id"] == session_id
        # Denial really stopped it: the gated stage never ran.
        assert lines(out_dir / "report.txt") == []


# ---------------------------------------------------------------------------
# v2-only: the second session exists, and must stay an implementation detail
# ---------------------------------------------------------------------------


@requires_runner
class TestV2EngineSessionStaysInternal:
    @pytest.mark.asyncio
    async def test_the_two_ids_really_are_distinct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Guard the guard: a v2 run does have two sessions.

        Without this, every "same id" assertion above would still pass if the
        engine session quietly stopped existing, and the round trip would be
        proven on a scenario that no longer contains the hazard.
        """
        tool, project, _out_dir, executed = await pause_at_gate(
            tmp_path, monkeypatch, "v2"
        )
        session_id = executed.output["session_id"]
        record = tool.session_manager.load_state(session_id, project)[V2_RUN_STATE_KEY]

        engine_session_id = record["engine_session_id"]
        assert engine_session_id and engine_session_id != session_id
        # The gate physically lives over there -- which is why it had to be
        # translated rather than simply reported.
        assert tool.session_manager.get_pending_approval(engine_session_id, project)
        assert not tool.session_manager.get_pending_approval(session_id, project)

    @pytest.mark.asyncio
    async def test_approving_the_engine_id_reports_the_run_id_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Either id may be given; only the run's own id comes back.

        A caller holding the engine id (from an older ``approvals`` listing, or
        a log) is steered onto the one id that works, instead of being left to
        discover the difference from a refusal.
        """
        tool, project, out_dir, executed = await pause_at_gate(
            tmp_path, monkeypatch, "v2"
        )
        session_id = executed.output["session_id"]
        engine_session_id = tool.session_manager.load_state(session_id, project)[
            V2_RUN_STATE_KEY
        ]["engine_session_id"]

        approved = await tool._approve_stage(
            {"session_id": engine_session_id, "stage_name": "setup"}
        )
        assert approved.success is True, approved.error
        assert approved.output["session_id"] == session_id
        # Named, not hidden.
        assert approved.output["gate_session_id"] == engine_session_id
        assert session_id in approved.output["message"]

        resumed = await tool._resume_recipe(
            {"session_id": approved.output["session_id"]}
        )
        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "completed"
        assert lines(out_dir / "report.txt") == ["reported"]

    @pytest.mark.asyncio
    async def test_resuming_the_engine_id_keeps_the_run_record_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Resuming under the engine id left the run's own session stale.

        Observed before the fix: the run finished, but its own session still
        read ``status: paused`` with only the pre-gate step completed, so a
        later ``resume`` of the id the caller was originally given re-entered a
        finished run instead of reporting there was nothing to do.
        """
        tool, project, out_dir, executed = await pause_at_gate(
            tmp_path, monkeypatch, "v2"
        )
        session_id = executed.output["session_id"]
        engine_session_id = tool.session_manager.load_state(session_id, project)[
            V2_RUN_STATE_KEY
        ]["engine_session_id"]

        await tool._approve_stage(
            {"session_id": engine_session_id, "stage_name": "setup"}
        )
        resumed = await tool._resume_recipe({"session_id": engine_session_id})
        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "completed"
        # Reported under the run's own id, whichever id was handed in.
        assert resumed.output["session_id"] == session_id

        record = tool.session_manager.load_state(session_id, project)[V2_RUN_STATE_KEY]
        assert record["status"] == "succeeded"
        assert record["completed_steps"] == list(STEP_IDS)

        # Consequently the id the caller has left says the honest thing.
        again = await tool._resume_recipe({"session_id": session_id})
        assert again.success is True, again.error
        assert again.output["status"] == "nothing_to_resume"
        # And nothing ran a second time.
        assert lines(out_dir / "report.txt") == ["reported"]
