"""In-session v2 execution: legacy step engine, closed-world agent catalog.

recipes-lc7: the library's sequential executor runs agent steps only, so a real
migrated recipe (30 steps, 3 of them agent steps) failed at its first ``bash``
step and no v2 recipe could run in-session at all. Execution now routes through
the proven step engine with the *plan's* catalog substituted for the caller's
agent map.

What each test here is defending:

* full step vocabulary really executes (``bash`` + ``parse_json`` + ``agent``),
* an agent in the caller's map but not in the plan is refused by name
  (manifest.v1 Core 3),
* a colliding caller agent cannot alter resolution -- the catalog is the only
  map the spawn ever sees (manifest.v1 Core 5),
* the caller-map leak detector still passes on what is handed over
  (lib.v1 Core 4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from amplifier_module_tool_recipes import _expand_session_dir
from amplifier_module_tool_recipes import closed_world as cw
from amplifier_module_tool_recipes import runner_adapter as ra

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

RUNNER_AVAILABLE = ra.runner_available()
requires_runner = pytest.mark.skipif(
    not RUNNER_AVAILABLE, reason=f"{ra.RUNNER_DISTRIBUTION} is not importable"
)

AGENT_FILE = """\
---
meta:
  name: reviewer
  description: The reviewer the RECIPE declared
---
You are the declared reviewer.
"""

V2_RECIPE = """\
schema_version: 2
name: mixed-step-recipe
description: bash + parse_json + agent, the shapes a real recipe uses
version: "1.0.0"

dependencies:
  - source: "bundles/supplier"
    kind: bundle
    required_agents:
      - "supplier:reviewer"

steps:
  - id: "setup"
    type: "bash"
    command: "echo '{\\"files\\": [\\"a.py\\"]}'"
    parse_json: true
    output: "inventory"

  - id: "review"
    agent: "supplier:reviewer"
    prompt: "Review {{inventory}}"
    output: "review_result"
"""

UNDECLARED_RECIPE = V2_RECIPE.replace('agent: "supplier:reviewer"', 'agent: "caller-only"')


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSpawn:
    """Stands in for the host's ``session.spawn`` capability."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"output": "reviewed", "session_id": "child-1"}


class FakeCoordinator:
    """A caller whose agent map both collides with, and exceeds, the closure."""

    def __init__(self, spawn: Any) -> None:
        self.config: dict[str, Any] = {
            "agents": {
                # Same name as the declared agent -- a different definition.
                "supplier:reviewer": {
                    "name": "reviewer",
                    "description": "the CALLER's impostor",
                    "instruction": "You are the impostor.",
                },
                "caller-only": {"name": "caller-only", "description": "only the caller has this"},
            },
            "providers": [{"module": "provider-anthropic"}],
        }
        self.session = object()
        self._capabilities: dict[str, Any] = {"session.spawn": spawn}

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value

    def get(self, name: str) -> Any:
        return self.config.get(name)


class FakeSessionManager:
    """Enough session state for the engine's checkpointing to be real."""

    def __init__(self, tmp_path: Path) -> None:
        self.base = tmp_path / "sessions"
        self.base.mkdir(exist_ok=True)
        self.states: dict[str, dict[str, Any]] = {}

    def create_session(self, *args: Any, **kwargs: Any) -> str:
        session_id = f"recipe_{len(self.states)}"
        self.states[session_id] = {"completed_steps": []}
        return session_id

    def get_session_dir(self, session_id: str, project_path: Path) -> Path:
        path = self.base / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def load_state(self, session_id: str, project_path: Path) -> dict[str, Any]:
        return dict(self.states.setdefault(session_id, {}))

    def save_state(self, session_id: str, project_path: Path, state: dict[str, Any]) -> None:
        self.states[session_id] = dict(state)

    def save_checkpoint(self, *args: Any, **kwargs: Any) -> None:
        return None

    def cleanup_old_sessions(self, project_path: Path) -> int:
        return 0

    def is_cancellation_requested(self, session_id: str, project_path: Path) -> bool:
        return False

    def get_stage_approval_status(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def set_pending_approval(self, *args: Any, **kwargs: Any) -> None:
        return None

    def complete_session(self, *args: Any, **kwargs: Any) -> None:
        return None


def make_plan(agent_path: Path, *, reference: str = "supplier:reviewer") -> Any:
    """An ``ExecutionPlan`` as the library would have resolved it."""
    from amplifier_recipe_runner.api import AgentProvenance
    from amplifier_recipe_runner.api import EffectivePolicy
    from amplifier_recipe_runner.api import ExecutionPlan
    from amplifier_recipe_runner.api import LockMode

    return ExecutionPlan(
        recipe_digest="sha256:test",
        schema_version=2,
        dependencies=(),
        agents={
            reference: AgentProvenance(
                agent=reference,
                supplied_by="bundles/supplier",
                dependency_digest="sha256:dep",
                local_path=str(agent_path),
            )
        },
        step_ids=("setup", "review"),
        policy=EffectivePolicy(lock_mode=LockMode.LOCKED),
    )


def write_agent(tmp_path: Path) -> Path:
    agents_dir = tmp_path / "supplier" / "agents"
    agents_dir.mkdir(parents=True)
    path = agents_dir / "reviewer.md"
    path.write_text(AGENT_FILE, encoding="utf-8")
    return path


def write_recipe(tmp_path: Path, body: str = V2_RECIPE) -> Path:
    path = tmp_path / "mixed.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The catalog itself
# ---------------------------------------------------------------------------


@requires_runner
class TestCatalog:
    def test_catalog_is_built_from_the_plans_resolved_definition(self, tmp_path: Path):
        catalog = cw.build_catalog(make_plan(write_agent(tmp_path)))

        assert catalog.names == ("supplier:reviewer",)
        config = catalog.agent_configs()["supplier:reviewer"]
        assert config["description"] == "The reviewer the RECIPE declared"
        assert "You are the declared reviewer." in config["instruction"]

    def test_a_name_outside_the_plan_is_refused_by_name(self, tmp_path: Path):
        from amplifier_recipe_runner.errors import UndeclaredAgentError

        catalog = cw.build_catalog(make_plan(write_agent(tmp_path)))

        with pytest.raises(UndeclaredAgentError) as excinfo:
            catalog.resolve("caller-only", step_id="review")
        assert "caller-only" in str(excinfo.value)

    def test_the_catalog_never_contains_the_callers_agents(self, tmp_path: Path):
        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn)
        catalog = cw.build_catalog(make_plan(write_agent(tmp_path)))
        scoped = cw.ClosedWorldCoordinator(coordinator, catalog)

        assert set(scoped.config["agents"]) == {"supplier:reviewer"}
        assert "caller-only" not in scoped.config["agents"]
        # The impostor's definition is gone; the declared one is in its place.
        assert scoped.config["agents"]["supplier:reviewer"]["description"] == (
            "The reviewer the RECIPE declared"
        )
        # ... and the caller's own map is untouched.
        assert coordinator.config["agents"]["supplier:reviewer"]["description"] == (
            "the CALLER's impostor"
        )

    def test_what_is_handed_over_passes_the_caller_map_leak_detector(self, tmp_path: Path):
        coordinator = FakeCoordinator(FakeSpawn())
        catalog = cw.build_catalog(make_plan(write_agent(tmp_path)))
        scoped = cw.ClosedWorldCoordinator(coordinator, catalog)

        caller_map = ra.caller_agent_map(coordinator)
        assert caller_map is not None  # premise: there IS a map to leak
        assert ra.find_caller_agent_leak(catalog.agent_configs(), caller_map) is None
        assert ra.find_caller_agent_leak(scoped.config["agents"], caller_map) is None

    @pytest.mark.asyncio
    async def test_spawn_refuses_an_undeclared_name_before_calling_the_host(
        self, tmp_path: Path
    ):
        from amplifier_recipe_runner.errors import UndeclaredAgentError

        spawn = FakeSpawn()
        catalog = cw.build_catalog(make_plan(write_agent(tmp_path)))
        wrapper = cw.ClosedWorldSpawn(spawn, catalog)

        with pytest.raises(UndeclaredAgentError):
            await wrapper("caller-only", "do it")
        assert spawn.calls == []

    @pytest.mark.asyncio
    async def test_a_colliding_caller_map_is_discarded_visibly(self, tmp_path: Path):
        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn)
        catalog = cw.build_catalog(make_plan(write_agent(tmp_path)))
        wrapper = cw.ClosedWorldSpawn(spawn, catalog)

        await wrapper(
            "supplier:reviewer",
            "review it",
            parent_session=coordinator.session,
            agent_configs=coordinator.config["agents"],
        )

        handed_over = spawn.calls[0]["agent_configs"]
        assert set(handed_over) == {"supplier:reviewer"}
        assert handed_over["supplier:reviewer"]["description"] == (
            "The reviewer the RECIPE declared"
        )
        assert "caller-only" in wrapper.ignored_host_agents
        assert "agent_configs" in wrapper.ignored_arguments
        # The caller's session still supplies providers: parent_session rides through.
        assert spawn.calls[0]["parent_session"] is coordinator.session


# ---------------------------------------------------------------------------
# End-to-end, on the real step engine
# ---------------------------------------------------------------------------


@requires_runner
class TestInSessionExecution:
    @pytest.mark.asyncio
    async def test_bash_parse_json_and_agent_steps_all_execute(self, tmp_path: Path):
        from amplifier_recipe_runner.api import RunStatus

        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn)
        sessions = FakeSessionManager(tmp_path)
        plan = make_plan(write_agent(tmp_path))
        recipe = write_recipe(tmp_path)

        result = await ra.run_v2_recipe_in_session(
            coordinator,
            sessions,
            recipe,
            {},
            tmp_path,
            plan=lambda request: _resolved(plan),
        )

        assert result.status is RunStatus.SUCCEEDED, result.error
        assert result.plan is plan
        # The bash step ran and its JSON output was parsed, not stringified.
        assert result.outputs["inventory"] == {"files": ["a.py"]}
        # The agent step ran through the host's spawn, with the plan's catalog.
        assert len(spawn.calls) == 1
        assert spawn.calls[0]["agent_name"] == "supplier:reviewer"
        assert set(spawn.calls[0]["agent_configs"]) == {"supplier:reviewer"}
        assert spawn.calls[0]["agent_configs"]["supplier:reviewer"]["description"] == (
            "The reviewer the RECIPE declared"
        )

    @pytest.mark.asyncio
    async def test_a_step_naming_a_caller_only_agent_fails_the_run(self, tmp_path: Path):
        from amplifier_recipe_runner.api import RunStatus

        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn)
        plan = make_plan(write_agent(tmp_path))

        result = await ra.run_v2_recipe_in_session(
            coordinator,
            FakeSessionManager(tmp_path),
            write_recipe(tmp_path, UNDECLARED_RECIPE),
            {},
            tmp_path,
            plan=lambda request: _resolved(plan),
        )

        assert result.status is RunStatus.FAILED
        assert "caller-only" in str(result.error)
        assert spawn.calls == []


async def _resolved(plan: Any) -> Any:
    """The injected ``plan`` seam: already-resolved, nothing re-resolved."""
    return plan


# ---------------------------------------------------------------------------
# session_dir placeholder (recipes-30w, incidental defect)
# ---------------------------------------------------------------------------


class TestSessionDirPlaceholder:
    def test_project_placeholder_expands_to_the_working_directory_name(self, tmp_path: Path):
        class WorkingDirCoordinator:
            def get_capability(self, name: str) -> Any:
                return str(tmp_path / "my-project") if name == "session.working_dir" else None

        resolved = _expand_session_dir(
            "~/.amplifier/projects/{project}/recipes", WorkingDirCoordinator()
        )

        assert "{project}" not in str(resolved)
        assert resolved.name == "recipes"
        assert "my-project" in str(resolved)
        assert not str(resolved).startswith("~")

    def test_a_configured_path_without_the_placeholder_is_unchanged(self, tmp_path: Path):
        class BareCoordinator:
            def get_capability(self, name: str) -> Any:
                return None

        assert _expand_session_dir(str(tmp_path), BareCoordinator()) == tmp_path
