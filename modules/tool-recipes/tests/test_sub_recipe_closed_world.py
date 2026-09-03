"""A v2 sub-recipe keeps its own closure, whatever the parent is.

recipes-ykj: ``repo-audit.yaml`` (``schema_version: 2``, declaring
``foundation:zen-architect`` in its own dependency manifest) ran clean when
invoked directly under ``-b anchors-amp-dev`` and died with

    Agent 'foundation:zen-architect' not found in configuration

when reached as a ``type: recipe`` step of ``ecosystem-audit-batch.yaml``
(legacy, no ``schema_version``). Same recipe, same host, same bundle -- two
different agent maps, because a sub-recipe simply ran on the parent's
executor, bound to the parent's coordinator.

``schema_version`` is a property of the RECIPE, not of how the recipe was
reached (recipe-dependency-manifest.v1 Core 3/4). What each test here defends:

* a v2 sub-recipe of a legacy parent resolves from its own declared closure,
  even when the caller's map has no such agent at all (the reported bug),
* a colliding caller definition still loses to the declared one (Core 5),
* the legacy parent's OWN agent steps stay caller-bound -- the fix reaches
  the sub-recipe boundary and nothing else,
* a legacy sub-recipe is untouched: no plan, no scope, caller-bound as
  ``conformance/legacy-compat`` pins it,
* a v2 sub-recipe of a v2 parent is scoped over the HOST, not over the
  parent's scope -- closures are not inherited or intersected,
* a closure that cannot be resolved fails the step loudly; there is no
  fallback to the caller-bound path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplifier_module_tool_recipes import closed_world as cw
from amplifier_module_tool_recipes import runner_adapter as ra
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

RUNNER_AVAILABLE = ra.runner_available()
requires_runner = pytest.mark.skipif(
    not RUNNER_AVAILABLE, reason=f"{ra.RUNNER_DISTRIBUTION} is not importable"
)

DECLARED_AGENT_FILE = """\
---
meta:
  name: zen-architect
  description: The architect the SUB-RECIPE declared
---
You are the declared architect.
"""

PARENT_AGENT_FILE = """\
---
meta:
  name: planner
  description: The planner the PARENT declared
---
You are the declared planner.
"""

# The parent from the bug report, reduced: legacy, one `type: recipe` step.
LEGACY_PARENT = """\
name: legacy-parent
description: No schema_version -- caller-bound, exactly as before
version: "1.0.0"

steps:
  - id: "audit"
    type: "recipe"
    recipe: "sub.yaml"
    context:
      subject: "{{subject}}"
    output: "audit_result"
"""

# The parent's own agent step must stay caller-bound; only the boundary moves.
LEGACY_PARENT_WITH_OWN_AGENT = """\
name: legacy-parent-with-agent
description: A legacy agent step of its own, plus a v2 sub-recipe
version: "1.0.0"

steps:
  - id: "plan"
    agent: "caller-only"
    prompt: "Plan it"
    output: "plan_result"

  - id: "audit"
    type: "recipe"
    recipe: "sub.yaml"
    context:
      subject: "{{subject}}"
    output: "audit_result"
"""

V2_SUB = """\
schema_version: 2

name: v2-sub
description: Carries its own agents, wherever it is reached from
version: "1.0.0"

dependencies:
  - source: "bundles/foundation"
    kind: bundle
    required_agents:
      - "foundation:zen-architect"

steps:
  - id: "review"
    agent: "foundation:zen-architect"
    prompt: "Review {{subject}}"
    output: "review_result"
"""

LEGACY_SUB = """\
name: legacy-sub
description: No schema_version -- resolves from the caller, as it always has
version: "1.0.0"

steps:
  - id: "review"
    agent: "caller-only"
    prompt: "Review {{subject}}"
    output: "review_result"
"""

V2_PARENT = """\
schema_version: 2

name: v2-parent
description: Declares ONLY its own agent, not the sub-recipe's
version: "1.0.0"

dependencies:
  - source: "bundles/parent-supplier"
    kind: bundle
    required_agents:
      - "parent:planner"

steps:
  - id: "plan"
    agent: "parent:planner"
    prompt: "Plan {{subject}}"
    output: "plan_result"

  - id: "audit"
    type: "recipe"
    recipe: "sub.yaml"
    context:
      subject: "{{subject}}"
    output: "audit_result"
"""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class HostSpawn:
    """The host's spawn, refusing an agent it was not handed -- as hosts do.

    The real failure in the bug report ("Agent 'foundation:zen-architect' not
    found in configuration") happens inside the host's spawn, not in this
    package, so a fake that accepts anything cannot observe the defect at all.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs.get("agent_name")
        configs = kwargs.get("agent_configs") or {}
        if name not in configs:
            raise ValueError(f"Agent '{name}' not found in configuration")
        self.calls.append(kwargs)
        return {"output": f"ran {name}", "session_id": f"child-{len(self.calls)}"}

    @property
    def agent_names(self) -> list[Any]:
        return [call["agent_name"] for call in self.calls]


class HostCoordinator:
    """A caller whose map lacks the sub-recipe's agent entirely."""

    def __init__(self, spawn: Any, agents: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = {
            "agents": (
                {"caller-only": {"name": "caller-only", "description": "only the caller has this"}}
                if agents is None
                else agents
            ),
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


# ---------------------------------------------------------------------------
# Fixtures on disk
# ---------------------------------------------------------------------------


def write_agent(tmp_path: Path, bundle: str, name: str, body: str) -> Path:
    agents_dir = tmp_path / bundle / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def make_plan(reference: str, agent_path: Path, *, step_ids: tuple[str, ...]) -> Any:
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
        step_ids=step_ids,
        policy=EffectivePolicy(lock_mode=LockMode.LOCKED),
    )


class PlanStub:
    """Stands in for the library's ``plan()``, keyed by the recipe asked for."""

    def __init__(self, plans: dict[str, Any]) -> None:
        self._plans = plans
        self.requested: list[str] = []

    async def __call__(self, request: Any) -> Any:
        name = Path(request.recipe).name
        self.requested.append(name)
        try:
            return self._plans[name]
        except KeyError:  # pragma: no cover - a test wiring mistake, not a path
            raise AssertionError(f"no stub plan for {name}") from None


@pytest.fixture
def stub_plan(monkeypatch: pytest.MonkeyPatch):
    """Install a ``plan`` stub on the library the adapter actually loads."""

    def install(plans: dict[str, Any]) -> PlanStub:
        stub = PlanStub(plans)
        monkeypatch.setattr(ra.load_runner(), "plan", stub)
        return stub

    return install


async def run_parent(tmp_path: Path, coordinator: Any, parent: Path) -> dict[str, Any]:
    executor = RecipeExecutor(coordinator, FakeSessionManager(tmp_path))
    return await executor.execute_recipe(
        Recipe.from_yaml(parent),
        {"subject": "the repo"},
        tmp_path,
        recipe_path=parent,
    )


# ---------------------------------------------------------------------------
# The unwrap rule
# ---------------------------------------------------------------------------


@requires_runner
class TestHostCoordinatorOf:
    def test_a_plain_coordinator_is_returned_unchanged(self):
        host = HostCoordinator(HostSpawn())
        assert cw.host_coordinator_of(host) is host

    def test_a_scoped_view_unwraps_to_its_host(self, tmp_path: Path):
        host = HostCoordinator(HostSpawn())
        agent = write_agent(tmp_path, "foundation", "zen-architect", DECLARED_AGENT_FILE)
        catalog = cw.build_catalog(
            make_plan("foundation:zen-architect", agent, step_ids=("review",))
        )
        scoped = cw.ClosedWorldCoordinator(host, catalog)

        assert cw.host_coordinator_of(scoped) is host

    def test_nested_views_unwrap_all_the_way_down(self, tmp_path: Path):
        host = HostCoordinator(HostSpawn())
        agent = write_agent(tmp_path, "foundation", "zen-architect", DECLARED_AGENT_FILE)
        catalog = cw.build_catalog(
            make_plan("foundation:zen-architect", agent, step_ids=("review",))
        )
        nested = cw.ClosedWorldCoordinator(
            cw.ClosedWorldCoordinator(host, catalog), catalog
        )

        assert cw.host_coordinator_of(nested) is host


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


@requires_runner
class TestLegacyParentV2SubRecipe:
    @pytest.mark.asyncio
    async def test_the_sub_recipe_resolves_an_agent_the_caller_never_had(
        self, tmp_path: Path, stub_plan
    ):
        """The reported failure, exactly: the caller's map has no such agent."""
        agent = write_agent(tmp_path, "foundation", "zen-architect", DECLARED_AGENT_FILE)
        stub_plan(
            {"sub.yaml": make_plan("foundation:zen-architect", agent, step_ids=("review",))}
        )
        write(tmp_path, "sub.yaml", V2_SUB)
        parent = write(tmp_path, "parent.yaml", LEGACY_PARENT)

        spawn = HostSpawn()
        coordinator = HostCoordinator(spawn)
        assert "foundation:zen-architect" not in coordinator.config["agents"]

        await run_parent(tmp_path, coordinator, parent)

        assert spawn.agent_names == ["foundation:zen-architect"]
        handed_over = spawn.calls[0]["agent_configs"]
        assert set(handed_over) == {"foundation:zen-architect"}
        assert handed_over["foundation:zen-architect"]["description"] == (
            "The architect the SUB-RECIPE declared"
        )

    @pytest.mark.asyncio
    async def test_a_colliding_caller_definition_still_loses(
        self, tmp_path: Path, stub_plan
    ):
        """Core 5: the caller's same-named agent cannot alter resolution."""
        agent = write_agent(tmp_path, "foundation", "zen-architect", DECLARED_AGENT_FILE)
        stub_plan(
            {"sub.yaml": make_plan("foundation:zen-architect", agent, step_ids=("review",))}
        )
        write(tmp_path, "sub.yaml", V2_SUB)
        parent = write(tmp_path, "parent.yaml", LEGACY_PARENT)

        spawn = HostSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={
                "foundation:zen-architect": {
                    "name": "zen-architect",
                    "description": "the CALLER's impostor",
                },
                "caller-only": {"name": "caller-only"},
            },
        )

        await run_parent(tmp_path, coordinator, parent)

        handed_over = spawn.calls[0]["agent_configs"]
        assert handed_over["foundation:zen-architect"]["description"] == (
            "The architect the SUB-RECIPE declared"
        )
        assert "caller-only" not in handed_over
        # ... and the caller's own map is untouched.
        assert coordinator.config["agents"]["foundation:zen-architect"]["description"] == (
            "the CALLER's impostor"
        )

    @pytest.mark.asyncio
    async def test_the_sub_recipe_is_labelled_with_the_v2_execution_mode(
        self, tmp_path: Path, stub_plan, caplog: pytest.LogCaptureFixture
    ):
        """The boundary says which engine and which catalog it chose.

        A legacy parent's tool result is labelled ``legacy-caller-bound`` and
        stays that way -- it IS caller-bound. The sub-recipe is not, and the
        only place that fact can be observed at run time is here.
        """
        agent = write_agent(tmp_path, "foundation", "zen-architect", DECLARED_AGENT_FILE)
        stub_plan(
            {"sub.yaml": make_plan("foundation:zen-architect", agent, step_ids=("review",))}
        )
        write(tmp_path, "sub.yaml", V2_SUB)
        parent = write(tmp_path, "parent.yaml", LEGACY_PARENT)

        with caplog.at_level("INFO", logger="amplifier_module_tool_recipes.runner_adapter"):
            await run_parent(tmp_path, HostCoordinator(HostSpawn()), parent)

        labelled = [
            record.getMessage()
            for record in caplog.records
            if cw.V2_LEGACY_ENGINE_EXECUTION_MODE in record.getMessage()
        ]
        assert len(labelled) == 1, caplog.text
        # It names the SUB-recipe -- the parent was never planned at all.
        assert "sub.yaml" in labelled[0]
        assert "parent.yaml" not in labelled[0]
        assert "foundation:zen-architect" in labelled[0]

    @pytest.mark.asyncio
    async def test_the_parents_own_agent_step_stays_caller_bound(
        self, tmp_path: Path, stub_plan
    ):
        """The fix reaches the sub-recipe boundary and nothing else."""
        agent = write_agent(tmp_path, "foundation", "zen-architect", DECLARED_AGENT_FILE)
        stub_plan(
            {"sub.yaml": make_plan("foundation:zen-architect", agent, step_ids=("review",))}
        )
        write(tmp_path, "sub.yaml", V2_SUB)
        parent = write(tmp_path, "parent.yaml", LEGACY_PARENT_WITH_OWN_AGENT)

        spawn = HostSpawn()
        coordinator = HostCoordinator(spawn)

        await run_parent(tmp_path, coordinator, parent)

        assert spawn.agent_names == ["caller-only", "foundation:zen-architect"]
        # The parent's step saw the CALLER's map -- unchanged, caller-bound.
        assert "caller-only" in spawn.calls[0]["agent_configs"]
        # The sub-recipe's step saw only its own closure.
        assert set(spawn.calls[1]["agent_configs"]) == {"foundation:zen-architect"}


@requires_runner
class TestLegacySubRecipeIsUntouched:
    @pytest.mark.asyncio
    async def test_no_plan_is_resolved_and_the_caller_map_is_used(
        self, tmp_path: Path, stub_plan
    ):
        stub = stub_plan({})
        write(tmp_path, "sub.yaml", LEGACY_SUB)
        parent = write(tmp_path, "parent.yaml", LEGACY_PARENT)

        spawn = HostSpawn()
        coordinator = HostCoordinator(spawn)

        await run_parent(tmp_path, coordinator, parent)

        assert stub.requested == []  # nothing was planned at all
        assert spawn.agent_names == ["caller-only"]
        assert "caller-only" in spawn.calls[0]["agent_configs"]


@requires_runner
class TestV2ParentV2SubRecipe:
    @pytest.mark.asyncio
    async def test_the_sub_recipes_closure_is_its_own_not_the_parents(
        self, tmp_path: Path, stub_plan
    ):
        """Scoped over the HOST: closures are not inherited or intersected.

        The parent declares ``parent:planner`` and nothing else. Were the
        sub-recipe's scope built over the parent's scope, the parent's
        ``ClosedWorldSpawn`` would sit innermost and refuse
        ``foundation:zen-architect`` -- an agent the sub-recipe declared and
        the parent never did.
        """
        sub_agent = write_agent(
            tmp_path, "foundation", "zen-architect", DECLARED_AGENT_FILE
        )
        parent_agent = write_agent(tmp_path, "parent", "planner", PARENT_AGENT_FILE)
        stub_plan(
            {
                "parent.yaml": make_plan(
                    "parent:planner", parent_agent, step_ids=("plan", "audit")
                ),
                "sub.yaml": make_plan(
                    "foundation:zen-architect", sub_agent, step_ids=("review",)
                ),
            }
        )
        write(tmp_path, "sub.yaml", V2_SUB)
        parent = write(tmp_path, "parent.yaml", V2_PARENT)

        spawn = HostSpawn()
        coordinator = HostCoordinator(spawn)
        result = await ra.run_v2_recipe_in_session(
            coordinator,
            FakeSessionManager(tmp_path),
            parent,
            {"subject": "the repo"},
            tmp_path,
        )

        from amplifier_recipe_runner.api import RunStatus

        assert result.status is RunStatus.SUCCEEDED, result.error
        assert spawn.agent_names == ["parent:planner", "foundation:zen-architect"]
        assert set(spawn.calls[0]["agent_configs"]) == {"parent:planner"}
        assert set(spawn.calls[1]["agent_configs"]) == {"foundation:zen-architect"}


@requires_runner
class TestNoSilentFallback:
    @pytest.mark.asyncio
    async def test_an_unresolvable_closure_fails_the_step(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A v2 sub-recipe never falls back to the parent's caller-bound map."""

        async def refuse(request: Any) -> Any:
            raise RuntimeError("dependency 'bundles/foundation' could not be fetched")

        monkeypatch.setattr(ra.load_runner(), "plan", refuse)
        write(tmp_path, "sub.yaml", V2_SUB)
        parent = write(tmp_path, "parent.yaml", LEGACY_PARENT)

        spawn = HostSpawn()
        with pytest.raises(Exception) as excinfo:
            await run_parent(tmp_path, HostCoordinator(spawn), parent)

        assert "could not be fetched" in str(excinfo.value)
        assert spawn.calls == []
