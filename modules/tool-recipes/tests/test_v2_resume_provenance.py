"""A v2 resume verifies the closure the run recorded (recipes-8hr).

``recipe-dependency-manifest.v1`` Core 8: a resume re-resolves, compares that
against what the run recorded, and refuses on a difference. The library's own
``resume`` is explicit that it does NOT do this and that the caller must --
``src/amplifier_recipe_runner/execution.py``'s docstring names
``provenance.check_resume_provenance`` and says the standalone CLI calls it.

No adapter path did. ``_record_v2_run`` wrote ``v2_provenance`` into session
state on every v2 run and nothing ever read it, so a run recorded against one
recipe/dependency closure could be resumed after either moved: the *remaining*
steps then ran against a different closure than the *completed* ones did, with
nothing reporting the divergence.

The end-to-end tests here use the library's REAL planner (these recipes declare
``dependencies: []``, so resolution needs no network) precisely so the recipe
digest is a real digest of the file on disk -- an edited recipe has to be
caught by the same arithmetic that runs in production, not by a stub that was
told to differ.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from amplifier_module_tool_recipes import V2_PROVENANCE_STATE_KEY
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
# A staged recipe that pauses, so a resume has something to continue INTO
# ---------------------------------------------------------------------------

STAGED_RECIPE = """\
schema_version: 2

name: provenance-resume
description: "Pauses at a gate so the remainder runs on a later resume"
version: "1.0.0"

dependencies: []

context:
  out_dir: "OUT_DIR"

stages:
  - name: "first"
    steps:
      - id: "step-one"
        type: "bash"
        command: "echo one >> {{out_dir}}/ran.txt"
        output: "one"

    approval:
      required: true
      prompt: "Continue?"
      timeout: 0
      default: "deny"

  - name: "second"
    steps:
      - id: "step-two"
        type: "bash"
        command: "echo two >> {{out_dir}}/ran.txt"
        output: "two"
"""


class FakeCoordinator:
    def __init__(self, working_dir: Path) -> None:
        self.config: dict[str, Any] = {
            "agents": {"caller-only": {"description": "only the caller has this"}},
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


def make_tool(tmp_path: Path) -> tuple[RecipesTool, Path]:
    project = tmp_path / "project"
    project.mkdir()
    coordinator = FakeCoordinator(project)
    sessions = SessionManager(tmp_path / "amplifier-sessions")
    executor = RecipeExecutor(coordinator, sessions)
    return RecipesTool(executor, sessions, coordinator, {}), project


def write_recipe(tmp_path: Path, out_dir: Path, body: str = STAGED_RECIPE) -> Path:
    path = tmp_path / "staged.yaml"
    path.write_text(body.replace("OUT_DIR", str(out_dir)), encoding="utf-8")
    return path


def lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


async def run_to_the_gate(tool: RecipesTool, recipe: Path) -> str:
    """Execute until the approval gate pauses it, then approve. Returns the id."""
    executed = await tool._execute_recipe({"recipe_path": str(recipe)})
    assert executed.success is True, executed.error
    assert executed.output["status"] == "paused_for_approval"
    assert executed.output["completed_steps"] == ["step-one"]
    session_id = executed.output["session_id"]
    approved = await tool._approve_stage(
        {"session_id": session_id, "stage_name": "first", "message": "go"}
    )
    assert approved.success is True, approved.error
    return session_id


# ---------------------------------------------------------------------------
# End to end: the recorded closure decides whether the remainder runs
# ---------------------------------------------------------------------------


@requires_runner
class TestResumeVerifiesTheRecordedClosure:
    @pytest.mark.asyncio
    async def test_an_unchanged_closure_resumes_exactly_as_before(self, tmp_path: Path):
        """The regression fence: verification must not cost a working resume."""
        tool, project = make_tool(tmp_path)
        out_dir = project / "out"
        out_dir.mkdir()
        recipe = write_recipe(tmp_path, out_dir)

        session_id = await run_to_the_gate(tool, recipe)
        resumed = await tool._resume_recipe({"session_id": session_id})

        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "completed"
        assert resumed.output["execution_mode"] == ra.V2_LEGACY_ENGINE_EXECUTION_MODE
        # The remaining step ran, and the completed one was not re-run.
        assert lines(out_dir / "ran.txt") == ["one", "two"]
        # Verified, so nothing to warn about.
        assert ra.provenance_warning_of(resumed) is None

    @pytest.mark.asyncio
    async def test_editing_the_recipe_refuses_the_resume_and_names_the_digest(
        self, tmp_path: Path
    ):
        """The defect: the remaining steps must not run against a different recipe.

        Before this check the edited step simply ran, and the result reported
        ``completed`` -- indistinguishable from a run whose recipe never moved.
        """
        tool, project = make_tool(tmp_path)
        out_dir = project / "out"
        out_dir.mkdir()
        recipe = write_recipe(tmp_path, out_dir)

        session_id = await run_to_the_gate(tool, recipe)

        # The recipe moves under the paused run: step-two is now a different
        # step than the one the recorded plan resolved.
        recipe.write_text(
            recipe.read_text(encoding="utf-8").replace("echo two", "echo EDITED"),
            encoding="utf-8",
        )

        resumed = await tool._resume_recipe({"session_id": session_id})

        assert resumed.success is False
        assert resumed.error["type"] == "V2ProvenanceMismatchError"
        assert resumed.error["diverged"]["source"] == "<recipe>"
        assert "digest" in resumed.error["diverged"]["what"]
        assert resumed.error["diverged"]["expected"] != resumed.error["diverged"]["actual"]
        assert "execute" in resumed.error["remedy"]
        assert resumed.error["completed_steps"] == ["step-one"]
        # NOT resumed: the edited step never ran.
        assert lines(out_dir / "ran.txt") == ["one"]

    @pytest.mark.asyncio
    async def test_a_refused_resume_leaves_the_run_resumable_once_restored(
        self, tmp_path: Path
    ):
        """The refusal is a refusal, not a corruption -- restore and continue."""
        tool, project = make_tool(tmp_path)
        out_dir = project / "out"
        out_dir.mkdir()
        recipe = write_recipe(tmp_path, out_dir)
        recorded_body = recipe.read_text(encoding="utf-8")

        session_id = await run_to_the_gate(tool, recipe)
        recipe.write_text(recorded_body.replace("echo two", "echo EDITED"), encoding="utf-8")
        refused = await tool._resume_recipe({"session_id": session_id})
        assert refused.success is False

        recipe.write_text(recorded_body, encoding="utf-8")
        resumed = await tool._resume_recipe({"session_id": session_id})

        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "completed"
        assert lines(out_dir / "ran.txt") == ["one", "two"]

    @pytest.mark.asyncio
    async def test_a_session_recorded_without_provenance_warns_and_still_resumes(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Older builds recorded no ``v2_provenance``. Those sessions still resume.

        Refusing them would strand every run recorded before the record
        existed -- the check is there to stop silent drift, not to make
        yesterday's paused runs unfinishable. It says so instead of pretending
        it verified something.
        """
        tool, project = make_tool(tmp_path)
        out_dir = project / "out"
        out_dir.mkdir()
        recipe = write_recipe(tmp_path, out_dir)

        session_id = await run_to_the_gate(tool, recipe)

        # Reconstruct a pre-provenance session on disk.
        state = tool.session_manager.load_state(session_id, project)
        assert state.pop(V2_PROVENANCE_STATE_KEY, None) is not None
        tool.session_manager.save_state(session_id, project, state)

        with caplog.at_level(logging.WARNING, logger="amplifier_module_tool_recipes"):
            resumed = await tool._resume_recipe({"session_id": session_id})

        assert resumed.success is True, resumed.error
        assert resumed.output["status"] == "completed"
        assert lines(out_dir / "ran.txt") == ["one", "two"]

        warning = ra.provenance_warning_of(resumed)
        assert warning is not None
        assert "recorded no dependency provenance" in warning
        assert any("recorded no dependency provenance" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_edit_is_undetectable_when_the_session_recorded_no_provenance(
        self, tmp_path: Path
    ):
        """A pre-provenance session that was ALSO edited: warned, not silently refused.

        This is the honest cost of not stranding old sessions, and it is
        recorded here rather than left for someone to discover: with nothing
        recorded there is nothing to compare, so the edit is not detectable
        at all. The warning is the only signal, and it is present.
        """
        tool, project = make_tool(tmp_path)
        out_dir = project / "out"
        out_dir.mkdir()
        recipe = write_recipe(tmp_path, out_dir)

        session_id = await run_to_the_gate(tool, recipe)
        state = tool.session_manager.load_state(session_id, project)
        state.pop(V2_PROVENANCE_STATE_KEY, None)
        tool.session_manager.save_state(session_id, project, state)
        recipe.write_text(
            recipe.read_text(encoding="utf-8").replace("echo two", "echo EDITED"),
            encoding="utf-8",
        )

        resumed = await tool._resume_recipe({"session_id": session_id})

        assert resumed.success is True, resumed.error
        assert lines(out_dir / "ran.txt") == ["one", "EDITED"]
        assert ra.provenance_warning_of(resumed) is not None


# ---------------------------------------------------------------------------
# The library route (`execution_mode: runner-isolated`) is gated too
# ---------------------------------------------------------------------------


V2_RECIPE = """\
schema_version: 2
name: library-route
description: "Recorded as having run on the library's own executor"
version: "1.0.0"
dependencies: []
steps:
  - id: "review"
    type: "bash"
    command: "echo review"
    output: "reviewed"
"""


@requires_runner
class TestLibraryRouteIsGatedBeforeHandoff:
    @pytest.mark.asyncio
    async def test_a_mismatch_refuses_before_the_library_resume_is_called(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """"Before handing off" is the whole requirement: the library must not run.

        The library's ``resume`` does not check provenance and is documented
        not to, so a check that ran after the handover would be checking a run
        that had already continued.
        """
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "recipe.yaml").write_text(V2_RECIPE, encoding="utf-8")
        original = tmp_path / "v2.yaml"
        original.write_text(V2_RECIPE, encoding="utf-8")

        state = {
            "recipe_path": str(original),
            V2_RUN_STATE_KEY: {
                "status": "failed",
                "run_id": "run-1",
                "completed_steps": [],
                "step_ids": ["review"],
                "recipe_path": str(original),
                "execution_mode": ra.V2_EXECUTION_MODE,
            },
            # Recorded against a recipe body that is not the one on disk.
            V2_PROVENANCE_STATE_KEY: {
                "manifest_version": 1,
                "run_id": "run-1",
                "recipe_digest": "sha256:recorded-something-else",
                "schema_version": 2,
                "dependencies": [],
                "agents": {},
                "step_ids": ["review"],
            },
        }
        session_manager = MagicMock()
        session_manager.session_exists.return_value = True
        session_manager.load_state.return_value = state
        session_manager.get_session_dir.return_value = session_dir

        coordinator = FakeCoordinator(tmp_path)
        executor = MagicMock()
        tool = RecipesTool(executor, session_manager, coordinator, {})

        async def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the library was handed a run whose closure moved")

        runner = ra.load_runner()
        monkeypatch.setattr(runner, "run", forbidden)
        monkeypatch.setattr(
            "amplifier_module_tool_recipes.runner_adapter.library_resume",
            lambda: forbidden,
        )

        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert result.success is False
        assert result.error["type"] == "V2ProvenanceMismatchError"
        assert result.error["diverged"]["expected"] == "sha256:recorded-something-else"


# ---------------------------------------------------------------------------
# The comparison itself, against plans built by hand
# ---------------------------------------------------------------------------


def make_plan(
    *,
    digest: str = "sha256:recorded",
    dependencies: tuple[Any, ...] = (),
    agents: dict[str, Any] | None = None,
) -> Any:
    from amplifier_recipe_runner.api import EffectivePolicy
    from amplifier_recipe_runner.api import ExecutionPlan
    from amplifier_recipe_runner.api import LockMode

    return ExecutionPlan(
        recipe_digest=digest,
        schema_version=2,
        dependencies=dependencies,
        agents=agents or {},
        step_ids=("a", "b"),
        policy=EffectivePolicy(lock_mode=LockMode.LOCKED),
    )


def dependency(uri: str, revision: str) -> Any:
    from amplifier_recipe_runner.api import DependencyKind
    from amplifier_recipe_runner.api import ResolvedDependency

    return ResolvedDependency(
        uri=uri,
        kind=DependencyKind.BUNDLE,
        requested_ref="main",
        resolved_revision=revision,
    )


def agent(name: str, supplied_by: str, declared_by: str | None = None) -> Any:
    from amplifier_recipe_runner.api import AgentProvenance

    return AgentProvenance(agent=name, supplied_by=supplied_by, declared_by=declared_by)


def recorded_from(plan: Any, run_id: str = "run-1") -> dict[str, Any]:
    from amplifier_recipe_runner.provenance import run_manifest_from_plan

    return run_manifest_from_plan(plan, run_id=run_id).to_mapping()


async def check(recorded: Any, fresh_plan: Any, tmp_path: Path) -> str | None:
    recipe = tmp_path / "r.yaml"
    if not recipe.exists():
        recipe.write_text(V2_RECIPE, encoding="utf-8")

    async def fake_plan(request: Any) -> Any:
        return fresh_plan

    return await ra.check_recorded_provenance(
        FakeCoordinator(tmp_path),
        MagicMock(),
        recipe,
        tmp_path,
        recorded,
        session_id="sess-1",
        run_id="run-1",
        plan=fake_plan,
    )


@requires_runner
class TestWhatCountsAsDrift:
    @pytest.mark.asyncio
    async def test_an_identical_re_resolution_passes_silently(self, tmp_path: Path):
        plan = make_plan(dependencies=(dependency("git+https://x/b", "abc123"),))
        assert await check(recorded_from(plan), plan, tmp_path) is None

    @pytest.mark.asyncio
    async def test_a_moved_dependency_names_the_dependency_and_both_revisions(
        self, tmp_path: Path
    ):
        recorded = recorded_from(
            make_plan(dependencies=(dependency("git+https://x/b", "abc123"),))
        )
        moved = make_plan(dependencies=(dependency("git+https://x/b", "def456"),))

        with pytest.raises(ra.V2ProvenanceMismatchError) as excinfo:
            await check(recorded, moved, tmp_path)

        diverged = excinfo.value.diverged
        assert diverged["source"] == "git+https://x/b"
        assert "resolved revision" in diverged["what"]
        assert diverged["expected"] == "abc123"
        assert diverged["actual"] == "def456"
        assert "execute" in excinfo.value.remedy

    @pytest.mark.asyncio
    async def test_an_agent_now_defined_by_another_tree_is_drift(self, tmp_path: Path):
        """The gap ``check_resume_provenance`` alone leaves.

        Both sides declare the same dependency at the same identity, so the
        library's own comparison is satisfied -- and the agent the recipe
        actually spawns is defined somewhere else. Core 7 records the map so
        that this is checkable; unchecked, it is exactly the silent
        substitution Core 8 exists to forbid.
        """
        deps = (dependency("git+https://x/b", "abc123"),)
        recorded = recorded_from(
            make_plan(
                dependencies=deps,
                agents={"reviewer": agent("reviewer", "git+https://x/b", "git+https://x/b")},
            )
        )
        fresh = make_plan(
            dependencies=deps,
            agents={"reviewer": agent("reviewer", "git+https://x/other", "git+https://x/b")},
        )

        with pytest.raises(ra.V2ProvenanceMismatchError) as excinfo:
            await check(recorded, fresh, tmp_path)

        assert excinfo.value.diverged["source"] == "reviewer"
        assert "agent 'reviewer'" in excinfo.value.diverged["what"]
        assert excinfo.value.diverged["actual"] == "git+https://x/other"

    @pytest.mark.asyncio
    async def test_an_agent_that_vanished_from_the_closure_is_drift(self, tmp_path: Path):
        deps = (dependency("git+https://x/b", "abc123"),)
        recorded = recorded_from(
            make_plan(dependencies=deps, agents={"reviewer": agent("reviewer", "git+https://x/b")})
        )
        fresh = make_plan(dependencies=deps, agents={})

        with pytest.raises(ra.V2ProvenanceMismatchError) as excinfo:
            await check(recorded, fresh, tmp_path)

        assert excinfo.value.diverged["actual"] == "<not supplied>"

    @pytest.mark.asyncio
    async def test_a_record_written_before_declared_by_existed_is_not_drift(
        self, tmp_path: Path
    ):
        """The compatibility carve-out, from ``AgentProvenance``'s own note.

        Such a record put the DECLARED dependency in ``supplied_by``. Comparing
        that against a newer plan's DEFINING tree would report drift that is
        only a library-version difference -- and strand exactly the old
        sessions this check is careful not to strand.
        """
        deps = (dependency("git+https://x/b", "abc123"),)
        recorded = recorded_from(
            make_plan(
                dependencies=deps,
                # declared_by absent: the older field meaning.
                agents={"reviewer": agent("reviewer", "git+https://x/b")},
            )
        )
        fresh = make_plan(
            dependencies=deps,
            agents={
                "reviewer": agent("reviewer", "git+https://x/included", "git+https://x/b")
            },
        )

        assert await check(recorded, fresh, tmp_path) is None

    @pytest.mark.asyncio
    async def test_a_re_plan_that_fails_warns_rather_than_claiming_drift(
        self, tmp_path: Path
    ):
        """A failed re-resolution is not a mismatch, and must not be named one.

        Both resume routes re-plan for themselves moments later and report the
        real failure typed; calling it drift here would name the wrong defect.
        """
        recipe = tmp_path / "r.yaml"
        recipe.write_text(V2_RECIPE, encoding="utf-8")

        async def exploding_plan(request: Any) -> Any:
            raise RuntimeError("dependency source unreachable")

        warning = await ra.check_recorded_provenance(
            FakeCoordinator(tmp_path),
            MagicMock(),
            recipe,
            tmp_path,
            recorded_from(make_plan()),
            run_id="run-1",
            plan=exploding_plan,
        )

        assert warning is not None
        assert "could not be re-resolved" in warning
        assert "dependency source unreachable" in warning

    @pytest.mark.asyncio
    async def test_an_unreadable_record_warns_rather_than_stranding(self, tmp_path: Path):
        warning = await check({"dependencies": "not-a-list-of-mappings"}, make_plan(), tmp_path)

        assert warning is not None
        assert "cannot read" in warning

    @pytest.mark.asyncio
    async def test_no_record_at_all_warns(self, tmp_path: Path):
        warning = await check(None, make_plan(), tmp_path)

        assert warning is not None
        assert "recorded no dependency provenance" in warning
