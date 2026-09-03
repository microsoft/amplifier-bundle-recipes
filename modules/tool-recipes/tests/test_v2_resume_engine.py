"""v2 ``resume`` runs the SAME engine v2 ``execute`` ran (recipes-5c6).

The defect these tests defend against, reproduced live on
``examples/multi-repo-activity-report.yaml``:

* ``execute`` ran the recipe on the closed-world **legacy step engine**
  (``execution_mode: v2-closed-world-legacy-engine``) -- the engine that
  understands ``bash``, ``foreach``, ``type: recipe`` and staged approval
  gates. It reached the gate and paused, two steps in.
* ``resume`` handed the remainder to the runner library's **sequential
  executor**, which runs agent steps only. It died on ``analyze-repos``
  (``foreach`` + ``type: recipe``) with ``UnsupportedStepError`` -- a step
  shape the very same run had already executed past on the other engine.

Consequence: every shipped v2 recipe combining an approval gate with a
non-agent step was un-completable, which is the entire point of a staged
recipe.

So these tests exercise the real thing end to end -- real ``SessionManager``,
real ``RecipeExecutor``, real ``foreach`` and real ``type: recipe``
sub-recipes -- and assert on observable outcomes: the recipe finishes, the
sub-recipe actually ran per item, completed steps are NOT re-run, and the
label on the resume is the same one execute reported. Only dependency
*resolution* is stubbed (there is no network here); every step really runs.
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
# Recipes: the sweep-C shape, with every agent step replaced by a bash step so
# the test needs no model. The step SHAPES are what the defect was about.
# ---------------------------------------------------------------------------

SUB_RECIPE = """\
name: analyze-one-repo
description: "The sub-recipe a type-recipe step dispatches to"
version: "1.0.0"

context:
  repo: ""
  out_dir: ""

steps:
  - id: "analyze"
    type: "bash"
    command: "echo {{repo}} >> {{out_dir}}/analyzed.txt"
    output: "analysis"
"""

STAGED_RECIPE = """\
schema_version: 2

name: staged-foreach-subrecipe
description: "A staged v2 recipe with an approval gate, a foreach and a sub-recipe"
version: "1.0.0"

dependencies: []

context:
  out_dir: "OUT_DIR"

stages:
  - name: "setup"
    steps:
      - id: "load-repos"
        type: "bash"
        command: "echo '{\\"repos\\": [\\"alpha\\", \\"beta\\"]}'"
        parse_json: true
        output: "repos_data"

      - id: "prepare-analysis-plan"
        type: "bash"
        command: "echo planned"
        output: "analysis_plan"

    approval:
      required: true
      prompt: "Analyze {{repos_data.repos}}?"
      timeout: 0
      default: "deny"

  - name: "analysis"
    steps:
      - id: "analyze-repos"
        foreach: "{{repos_data.repos}}"
        as: "repo"
        collect: "repo_analyses"
        type: "recipe"
        recipe: "sub.yaml"
        context:
          repo: "{{repo}}"
          out_dir: "{{out_dir}}"
        output: "single_repo_result"

  - name: "synthesis"
    steps:
      - id: "report"
        type: "bash"
        command: "echo reported >> {{out_dir}}/report.txt"
        output: "report"
"""

FLAT_RECIPE = """\
schema_version: 2

name: flat-foreach-subrecipe
description: "A flat v2 recipe whose foreach step fails until a guard exists"
version: "1.0.0"

dependencies: []

context:
  out_dir: "OUT_DIR"

steps:
  - id: "load-repos"
    type: "bash"
    command: "echo ran >> {{out_dir}}/load-repos.txt && echo '{\\"repos\\": [\\"alpha\\", \\"beta\\"]}'"
    parse_json: true
    output: "repos_data"

  - id: "analyze-repos"
    foreach: "{{repos_data.repos}}"
    as: "repo"
    collect: "repo_analyses"
    type: "recipe"
    recipe: "guarded-sub.yaml"
    context:
      repo: "{{repo}}"
      out_dir: "{{out_dir}}"
    output: "single_repo_result"

  - id: "report"
    type: "bash"
    command: "echo reported >> {{out_dir}}/report.txt"
    output: "report"
"""

GUARDED_SUB_RECIPE = """\
name: analyze-one-repo-guarded
description: "Fails until the guard file exists"
version: "1.0.0"

context:
  repo: ""
  out_dir: ""

steps:
  - id: "analyze"
    type: "bash"
    command: "test -f {{out_dir}}/guard && echo {{repo}} >> {{out_dir}}/analyzed.txt"
    output: "analysis"
"""


# ---------------------------------------------------------------------------
# Fakes: a caller session, deliberately holding an agent map that must never
# reach the recipe (manifest.v1 Core 3) even on the resume path.
# ---------------------------------------------------------------------------


class FakeCoordinator:
    def __init__(self, working_dir: Path) -> None:
        self.config: dict[str, Any] = {
            "agents": {"caller-only": {"description": "only the caller has this"}},
            "providers": [{"module": "provider-anthropic"}],
        }
        self.session = object()
        self._capabilities: dict[str, Any] = {
            "session.working_dir": str(working_dir),
        }

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value

    def get(self, name: str) -> Any:
        return self.config.get(name)


def make_plan(step_ids: tuple[str, ...]) -> Any:
    """An ``ExecutionPlan`` as the library would have resolved it.

    Empty ``agents``: these recipes declare no agent steps, so the closed-world
    catalog is legitimately empty -- and that is exactly the state in which the
    caller's own ``caller-only`` agent must still not be reachable.
    """
    from amplifier_recipe_runner.api import EffectivePolicy
    from amplifier_recipe_runner.api import ExecutionPlan
    from amplifier_recipe_runner.api import LockMode

    return ExecutionPlan(
        recipe_digest="sha256:test",
        schema_version=2,
        dependencies=(),
        agents={},
        step_ids=step_ids,
        policy=EffectivePolicy(lock_mode=LockMode.LOCKED),
    )


def install_plan(
    monkeypatch: pytest.MonkeyPatch, step_ids: tuple[str, ...]
) -> list[Any]:
    """Stub dependency resolution only. Execution stays entirely real."""
    runner = ra.load_runner()
    planned: list[Any] = []

    async def fake_plan(request: Any) -> Any:
        planned.append(request)
        return make_plan(step_ids)

    monkeypatch.setattr(runner, "plan", fake_plan)
    return planned


def forbid_library_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the regression itself impossible to pass silently.

    If the resume ever routes back to the library's own ``run``/``resume``, it
    reaches the sequential executor -- the exact defect. Failing loudly here
    means a green test cannot mean "it quietly went the old way".
    """
    runner = ra.load_runner()

    async def forbidden(request: Any) -> Any:
        raise AssertionError(
            "resume reached the library's sequential executor, which cannot run "
            "`foreach` / `type: recipe` steps (recipes-5c6)"
        )

    monkeypatch.setattr(runner, "run", forbidden)
    monkeypatch.setattr(runner, "resume", forbidden, raising=False)


def make_tool(tmp_path: Path) -> tuple[RecipesTool, Path]:
    project = tmp_path / "project"
    project.mkdir()
    coordinator = FakeCoordinator(project)
    sessions = SessionManager(tmp_path / "amplifier-sessions")
    executor = RecipeExecutor(coordinator, sessions)
    return RecipesTool(executor, sessions, coordinator, {}), project


def write_recipes(tmp_path: Path, out_dir: Path, *bodies: tuple[str, str]) -> None:
    for name, body in bodies:
        (tmp_path / name).write_text(
            body.replace("OUT_DIR", str(out_dir)), encoding="utf-8"
        )


def lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# The sweep-C scenario, end to end
# ---------------------------------------------------------------------------


@requires_runner
class TestApprovalGateResume:
    @pytest.mark.asyncio
    async def test_approve_then_resume_completes_foreach_and_sub_recipe_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The reproduced failure, now run to a terminal status.

        execute -> pauses at the gate; approve; resume -> the ``foreach`` +
        ``type: recipe`` step actually dispatches per item and the recipe
        completes. Before the fix this died on `analyze-repos`.
        """
        tool, project = make_tool(tmp_path)
        out_dir = project / "out"
        out_dir.mkdir()
        write_recipes(
            tmp_path,
            out_dir,
            ("staged.yaml", STAGED_RECIPE),
            ("sub.yaml", SUB_RECIPE),
        )
        install_plan(
            monkeypatch,
            ("load-repos", "prepare-analysis-plan", "analyze-repos", "report"),
        )

        executed = await tool._execute_recipe(
            {"recipe_path": str(tmp_path / "staged.yaml")}
        )

        assert executed.success is True, executed.error
        assert executed.output["status"] == "paused_for_approval"
        assert executed.output["stage_name"] == "setup"
        assert (
            executed.output["execution_mode"] == ra.V2_LEGACY_ENGINE_EXECUTION_MODE
        )
        session_id = executed.output["session_id"]
        assert executed.output["completed_steps"] == [
            "load-repos",
            "prepare-analysis-plan",
        ]

        # The gate is approved through the id the run reported -- the only id
        # the caller was ever given.
        approved = await tool._approve_stage(
            {"session_id": session_id, "stage_name": "setup", "message": "merge"}
        )
        assert approved.success is True, approved.error

        forbid_library_execution(monkeypatch)
        resumed = await tool._resume_recipe({"session_id": session_id})

        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "completed"
        # Same engine label as the execute that paused it.
        assert resumed.output["execution_mode"] == ra.V2_LEGACY_ENGINE_EXECUTION_MODE
        # The sub-recipe really ran, once per foreach item.
        assert lines(out_dir / "analyzed.txt") == ["alpha", "beta"]
        assert lines(out_dir / "report.txt") == ["reported"]

    @pytest.mark.asyncio
    async def test_resume_also_works_addressed_at_the_engines_own_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Two session ids exist; a caller holding either must be able to resume.

        The engine runs in a session of its own making. Both ids were observed
        in the wild (the run reports one, `approvals` lists the other), and
        before the fix BOTH refused -- one on the sequential executor, one with
        "recorded no run outcome".
        """
        tool, project = make_tool(tmp_path)
        out_dir = project / "out"
        out_dir.mkdir()
        write_recipes(
            tmp_path,
            out_dir,
            ("staged.yaml", STAGED_RECIPE),
            ("sub.yaml", SUB_RECIPE),
        )
        install_plan(
            monkeypatch,
            ("load-repos", "prepare-analysis-plan", "analyze-repos", "report"),
        )

        executed = await tool._execute_recipe(
            {"recipe_path": str(tmp_path / "staged.yaml")}
        )
        reported_session = executed.output["session_id"]
        record = tool.session_manager.load_state(reported_session, project)[
            V2_RUN_STATE_KEY
        ]
        engine_session = record["engine_session_id"]
        assert engine_session and engine_session != reported_session

        await tool._approve_stage(
            {"session_id": engine_session, "stage_name": "setup", "message": "merge"}
        )

        forbid_library_execution(monkeypatch)
        resumed = await tool._resume_recipe({"session_id": engine_session})

        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "completed"
        assert lines(out_dir / "analyzed.txt") == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Interrupted mid-foreach, no approval gate involved
# ---------------------------------------------------------------------------


@requires_runner
class TestMidRunResume:
    @pytest.mark.asyncio
    async def test_a_run_that_died_in_a_foreach_resumes_without_re_running_earlier_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Resume re-enters the engine's own session, so its checkpoint decides.

        The first step is checkpointed before the ``foreach`` step dies. If the
        resume started a second run instead of re-entering that session, the
        first step would run twice -- which the marker file would show.
        """
        tool, project = make_tool(tmp_path)
        out_dir = project / "out"
        out_dir.mkdir()
        write_recipes(
            tmp_path,
            out_dir,
            ("flat.yaml", FLAT_RECIPE),
            ("guarded-sub.yaml", GUARDED_SUB_RECIPE),
        )
        install_plan(monkeypatch, ("load-repos", "analyze-repos", "report"))

        failed = await tool._execute_recipe({"recipe_path": str(tmp_path / "flat.yaml")})

        assert failed.success is False
        assert failed.output["status"] == "failed"
        assert failed.output["execution_mode"] == ra.V2_LEGACY_ENGINE_EXECUTION_MODE
        # It died IN the foreach step, having finished the one before it.
        assert failed.output["completed_steps"] == ["load-repos"]
        assert lines(out_dir / "load-repos.txt") == ["ran"]
        assert lines(out_dir / "analyzed.txt") == []

        # Clear the interruption and resume.
        (out_dir / "guard").write_text("go", encoding="utf-8")
        forbid_library_execution(monkeypatch)
        resumed = await tool._resume_recipe(
            {"session_id": failed.output["session_id"]}
        )

        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "completed"
        assert resumed.output["execution_mode"] == ra.V2_LEGACY_ENGINE_EXECUTION_MODE
        # The foreach ran per item, and the completed step was NOT re-run.
        assert lines(out_dir / "analyzed.txt") == ["alpha", "beta"]
        assert lines(out_dir / "load-repos.txt") == ["ran"]
        assert lines(out_dir / "report.txt") == ["reported"]

    @pytest.mark.asyncio
    async def test_a_run_naming_an_engine_session_that_is_gone_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A missing engine session is reported, never silently re-run.

        Its checkpoint is the only record of what finished; without it,
        re-entering would redo steps that already ran.
        """
        tool, project = make_tool(tmp_path)
        out_dir = project / "out"
        out_dir.mkdir()
        write_recipes(
            tmp_path,
            out_dir,
            ("flat.yaml", FLAT_RECIPE),
            ("guarded-sub.yaml", GUARDED_SUB_RECIPE),
        )
        install_plan(monkeypatch, ("load-repos", "analyze-repos", "report"))

        failed = await tool._execute_recipe({"recipe_path": str(tmp_path / "flat.yaml")})
        session_id = failed.output["session_id"]
        state = tool.session_manager.load_state(session_id, project)
        state[V2_RUN_STATE_KEY]["engine_session_id"] = "vanished-session"
        tool.session_manager.save_state(session_id, project, state)

        forbid_library_execution(monkeypatch)
        refused = await tool._resume_recipe({"session_id": session_id})

        assert refused.success is False
        assert refused.error["type"] == "V2EngineSessionMissing"
        assert "vanished-session" in refused.error["message"]
        # Nothing ran again.
        assert lines(out_dir / "load-repos.txt") == ["ran"]
