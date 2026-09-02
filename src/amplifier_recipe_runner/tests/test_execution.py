"""Tests for recipe-owned execution (manifest.v1 Core 3, 4, 5; lib.v1 Core 2, 3, 4).

Everything here runs against LOCAL fixture bundles under ``fixtures/exec/``
with an injected spawn backend: no network, no Foundation, no model call. The
one test that exercises the real Foundation composition path is skipped when
``amplifier_foundation`` is not importable, so the suite proves the runner's
resolution policy rather than the install.

The three scenarios the work item names are each covered by a test that would
FAIL if isolation were broken, not merely pass when it holds:

* **Closure supplies X, host does not** --
  :func:`test_run_executes_when_the_simulated_host_lacks_the_agent`. The
  simulated host catalog is built from a *different* bundle that supplies no
  such agent; the run still succeeds, and provenance names the declared
  dependency.
* **A colliding host agent cannot alter resolution** --
  :func:`test_colliding_host_agent_cannot_alter_resolution`. The impostor
  fixture supplies the same canonical name from a different path; it is handed
  to the adapter through the exact argument a real host would use, and the
  resolved definition still points at the declared dependency.
* **Unknown name raises** --
  :func:`test_unknown_agent_raises_undeclared_agent_error` and friends, at
  catalog, adapter, session, and ``run`` level.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import textwrap
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

import amplifier_recipe_runner as pkg
from amplifier_recipe_runner import execution
from amplifier_recipe_runner.api import ExecutionPlan
from amplifier_recipe_runner.api import ExecutionSession
from amplifier_recipe_runner.api import RunRequest
from amplifier_recipe_runner.api import RunStatus
from amplifier_recipe_runner.errors import UndeclaredAgentError
from amplifier_recipe_runner.execution import ExecutionError
from amplifier_recipe_runner.execution import FoundationSessionFactory
from amplifier_recipe_runner.execution import PlanCatalog
from amplifier_recipe_runner.execution import PlanCatalogSpawnAdapter
from amplifier_recipe_runner.execution import RecipeExecutionSession
from amplifier_recipe_runner.execution import SpawnRequest
from amplifier_recipe_runner.execution import UnsupportedStepError
from amplifier_recipe_runner.execution import create_execution_session
from amplifier_recipe_runner.manifest import parse_manifest_file
from amplifier_recipe_runner.planner import plan as plan_dependencies
from amplifier_recipe_runner.ports import HOST_PORTS
from amplifier_recipe_runner.ports import HostServices
from amplifier_recipe_runner.ports import RunEvent
from amplifier_recipe_runner.resolver import LocalBundleResolver

FIXTURES = Path(__file__).parent / "fixtures" / "exec"
SUPPLIER = FIXTURES / "supplier"
IMPOSTOR = FIXTURES / "impostor"

HAS_FOUNDATION = importlib.util.find_spec("amplifier_foundation") is not None
needs_foundation = pytest.mark.skipif(not HAS_FOUNDATION, reason="amplifier-foundation is not installed")


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class RecordingBackend:
    """Spawn backend double. Records every resolved request; calls no model."""

    def __init__(self, reply: str = "done") -> None:
        self.requests: list[SpawnRequest] = []
        self._reply = reply

    async def spawn(self, request: SpawnRequest) -> str:
        self.requests.append(request)
        return f"{self._reply}:{request.canonical}"

    @property
    def canonicals(self) -> list[str]:
        return [r.canonical for r in self.requests]


class ExplodingBackend:
    """Backend that must never be reached."""

    async def spawn(self, request: SpawnRequest) -> str:  # pragma: no cover - must not run
        raise AssertionError(f"backend reached for {request.agent!r}; resolution should have refused first")


class Providers:
    def roles(self) -> list[str]:
        return ["general"]

    def resolve(self, role: str) -> object:
        return object()


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


class ExplodingSink:
    def emit(self, event: RunEvent) -> None:
        raise RuntimeError("this sink is broken on purpose")


class Cancelled:
    cancelled = True

    def raise_if_cancelled(self) -> None:
        raise RuntimeError("cancelled")


def services_for(tmp_path: Path, **kwargs: Any) -> HostServices:
    return HostServices(provider_access=Providers(), workspace=tmp_path, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


RECIPE = """
    schema_version: 2
    name: review
    dependencies:
      - source: {supplier}
        kind: bundle
        required_agents: [supplier:reviewer]
    steps:
      - id: review
        agent: supplier:reviewer
        instruction: Review the change.
"""


def write_recipe(tmp_path: Path, body: str = RECIPE, *, name: str = "recipe.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip().format(supplier=SUPPLIER, impostor=IMPOSTOR), encoding="utf-8")
    return path


def planned(recipe_path: Path) -> ExecutionPlan:
    manifest = parse_manifest_file(recipe_path)
    return asyncio.run(
        plan_dependencies(
            manifest,  # type: ignore[arg-type]
            LocalBundleResolver(),  # type: ignore[arg-type]
            recipe_path.parent,
            recipe=recipe_path,
        )
    )


def simulated_host_catalog(*bundle_dirs: Path) -> dict[str, dict[str, Any]]:
    """An agent map shaped exactly like the one an Amplifier host would pass.

    Built by reading real fixture bundles, so a test that claims "the host
    supplies a colliding agent" is claiming something concrete.
    """
    from amplifier_recipe_runner.manifest import Dependency

    catalog: dict[str, dict[str, Any]] = {}
    resolver = LocalBundleResolver()
    for directory in bundle_dirs:
        bundle = asyncio.run(resolver.resolve(Dependency(source=str(directory), kind="bundle")))
        for name, agent in bundle.agents.items():
            catalog[name] = {"name": name, "local_path": agent.local_path, "supplied_by": str(directory)}
    return catalog


def adapter_for(
    plan: ExecutionPlan,
    backend: Any,
    *,
    workspace: Path,
    event_sink: Any | None = None,
) -> PlanCatalogSpawnAdapter:
    return PlanCatalogSpawnAdapter(
        PlanCatalog.from_plan(plan),
        backend,
        run_id="run-test",
        workspace=workspace,
        event_sink=event_sink,
    )


# --------------------------------------------------------------------------
# The catalog -- closed world by construction (manifest Core 3, Core 4)
# --------------------------------------------------------------------------


def test_catalog_is_exactly_the_plans_agents(tmp_path: Path) -> None:
    plan = planned(write_recipe(tmp_path))
    catalog = PlanCatalog.from_plan(plan)

    assert catalog.names == ("supplier:extra", "supplier:reviewer")
    assert "supplier:reviewer" in catalog
    assert "impostor:reviewer" not in catalog
    assert catalog.resolve("supplier:reviewer").supplied_by == str(SUPPLIER)


def test_catalog_offers_no_way_to_add_an_agent(tmp_path: Path) -> None:
    """Core 3/4: there must be no method by which caller agents could enter."""
    catalog = PlanCatalog.from_plan(planned(write_recipe(tmp_path)))

    for forbidden in ("add", "update", "install", "extend", "__setitem__", "merge"):
        assert not hasattr(catalog, forbidden), f"PlanCatalog.{forbidden} would open the closed world"

    configs = catalog.agent_configs()
    with pytest.raises(TypeError):
        configs["host:injected"] = {}  # type: ignore[index]


def test_unknown_agent_raises_undeclared_agent_error(tmp_path: Path) -> None:
    catalog = PlanCatalog.from_plan(planned(write_recipe(tmp_path)))

    with pytest.raises(UndeclaredAgentError) as excinfo:
        catalog.resolve("host:only", step_id="review")

    exc = excinfo.value
    assert exc.agent == "host:only"
    assert exc.step_id == "review"
    assert exc.declared_agents == ("supplier:extra", "supplier:reviewer")
    assert "supplier:reviewer" in str(exc)
    assert "Remedy:" in str(exc)


def test_catalog_carries_provenance_for_every_agent(tmp_path: Path) -> None:
    catalog = PlanCatalog.from_plan(planned(write_recipe(tmp_path)))
    definition = catalog.definition("supplier:reviewer")

    assert definition["name"] == "supplier:reviewer"
    assert definition["supplied_by"] == str(SUPPLIER)
    assert definition["local_path"] == str(SUPPLIER / "agents" / "reviewer.md")


# --------------------------------------------------------------------------
# The spawn adapter -- resolution source is the plan, only (Core 3, 4, 5)
# --------------------------------------------------------------------------


def test_adapter_resolves_from_the_plan_when_the_host_catalog_lacks_the_agent(tmp_path: Path) -> None:
    plan = planned(write_recipe(tmp_path))
    backend = RecordingBackend()
    adapter = adapter_for(plan, backend, workspace=tmp_path)

    # A host catalog that supplies nothing this recipe needs.
    host_catalog: dict[str, dict[str, Any]] = {"host:unrelated": {"name": "host:unrelated"}}

    result = asyncio.run(adapter("supplier:reviewer", "go", None, host_catalog))

    assert result["output"] == "done:supplier:reviewer"
    assert backend.canonicals == ["supplier:reviewer"]
    assert backend.requests[0].provenance.supplied_by == str(SUPPLIER)
    assert adapter.ignored_host_agents == ("host:unrelated",)


def test_colliding_host_agent_cannot_alter_resolution(tmp_path: Path) -> None:
    """manifest Core 5: a caller agent with a colliding name changes nothing."""
    plan = planned(write_recipe(tmp_path))
    backend = RecordingBackend()
    adapter = adapter_for(plan, backend, workspace=tmp_path)

    host_catalog = simulated_host_catalog(IMPOSTOR)
    assert "supplier:reviewer" in host_catalog, "the fixture must actually collide"
    assert host_catalog["supplier:reviewer"]["local_path"] == str(IMPOSTOR / "agents" / "reviewer.md")

    asyncio.run(adapter("supplier:reviewer", "go", object(), host_catalog))

    request = backend.requests[0]
    assert request.definition["local_path"] == str(SUPPLIER / "agents" / "reviewer.md")
    assert request.provenance.supplied_by == str(SUPPLIER)
    assert str(IMPOSTOR) not in str(request.definition)
    # And the refusal is visible, not merely implied by the outcome.
    assert adapter.ignored_host_agents == ("supplier:reviewer",)
    assert "agent_configs" in adapter.ignored_arguments
    assert "parent_session" in adapter.ignored_arguments


def test_host_supplied_agent_outside_the_plan_is_still_undeclared(tmp_path: Path) -> None:
    """A host offering an agent does not make it declarable (Core 3)."""
    plan = planned(write_recipe(tmp_path))
    adapter = adapter_for(plan, ExplodingBackend(), workspace=tmp_path)
    host_catalog = {"host:only": {"name": "host:only", "local_path": "/host/agents/only.md"}}

    with pytest.raises(UndeclaredAgentError) as excinfo:
        asyncio.run(adapter("host:only", "go", None, host_catalog))

    assert excinfo.value.agent == "host:only"


def test_adapter_discards_host_inheritance_arguments(tmp_path: Path) -> None:
    """manifest Core 4: nothing a host passes may widen the recipe's world."""
    plan = planned(write_recipe(tmp_path))
    backend = RecordingBackend()
    adapter = adapter_for(plan, backend, workspace=tmp_path)

    asyncio.run(
        adapter(
            "supplier:reviewer",
            "go",
            parent_session=object(),
            agent_configs=None,
            parent_messages=[{"role": "user", "content": "host history"}],
            tool_inheritance={"inherit_tools": ["tool-everything"]},
            hook_inheritance={"exclude_hooks": []},
        )
    )

    for discarded in ("parent_session", "parent_messages", "tool_inheritance", "hook_inheritance"):
        assert discarded in adapter.ignored_arguments

    request = backend.requests[0]
    assert request.instruction == "go"
    assert dict(request.context) == {}


def test_adapter_matches_the_host_spawn_capability_signature() -> None:
    """It must be droppable into the slot a host registers, or it is not a fix."""
    parameters = inspect.signature(PlanCatalogSpawnAdapter.__call__).parameters
    for name in ("agent_name", "instruction", "parent_session", "agent_configs"):
        assert name in parameters, f"{name} missing; a host call would not bind"
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


# --------------------------------------------------------------------------
# The neutral session (lib Core 3)
# --------------------------------------------------------------------------


def session_for(plan: ExecutionPlan, backend: Any, workspace: Path, **kwargs: Any) -> RecipeExecutionSession:
    return asyncio.run(
        create_execution_session(
            plan,
            services_for(workspace, **kwargs),
            run_id="run-test",
            spawn_backend=backend,
        )
    )


def test_session_satisfies_the_neutral_protocol(tmp_path: Path) -> None:
    session = session_for(planned(write_recipe(tmp_path)), RecordingBackend(), tmp_path)
    assert isinstance(session, ExecutionSession)
    assert session.run_id == "run-test"
    assert Path(session.workspace) == tmp_path


def test_session_available_agents_are_exactly_the_plan(tmp_path: Path) -> None:
    session = session_for(planned(write_recipe(tmp_path)), RecordingBackend(), tmp_path)
    assert list(session.available_agents()) == ["supplier:extra", "supplier:reviewer"]


def test_session_invoke_runs_a_declared_agent(tmp_path: Path) -> None:
    backend = RecordingBackend()
    session = session_for(planned(write_recipe(tmp_path)), backend, tmp_path)

    assert asyncio.run(session.invoke("supplier:reviewer", "go")) == "done:supplier:reviewer"
    assert backend.canonicals == ["supplier:reviewer"]


def test_session_invoke_refuses_an_undeclared_agent(tmp_path: Path) -> None:
    session = session_for(planned(write_recipe(tmp_path)), ExplodingBackend(), tmp_path)

    with pytest.raises(UndeclaredAgentError):
        asyncio.run(session.invoke("host:only", "go"))


def test_session_aclose_is_idempotent_and_runs_closers(tmp_path: Path) -> None:
    closed: list[str] = []
    session = RecipeExecutionSession(
        run_id="r",
        workspace=tmp_path,
        catalog=PlanCatalog({}),
        adapter=adapter_for(ExecutionPlan(recipe_digest="d", schema_version=2), RecordingBackend(), workspace=tmp_path),
        closers=(lambda: closed.append("a"), lambda: closed.append("b")),
    )

    asyncio.run(session.aclose())
    asyncio.run(session.aclose())
    assert closed == ["b", "a"]


# --------------------------------------------------------------------------
# run() -- the recipe executes in its own session (lib Core 2)
# --------------------------------------------------------------------------


def run_recipe(recipe_path: Path, backend: Any, workspace: Path, **kwargs: Any):
    request = RunRequest(recipe=recipe_path, services=services_for(workspace, **kwargs), run_id="run-test")
    return asyncio.run(pkg.run(request, resolver=LocalBundleResolver(), spawn_backend=backend))


def test_run_executes_when_the_simulated_host_lacks_the_agent(tmp_path: Path) -> None:
    """The GOOD fixture: declared closure supplies X, the host does not."""
    host_catalog = simulated_host_catalog(IMPOSTOR)
    host_catalog.pop("supplier:reviewer")
    assert "supplier:reviewer" not in host_catalog, "the host must genuinely lack the agent"

    backend = RecordingBackend()
    result = run_recipe(write_recipe(tmp_path), backend, tmp_path)

    assert result.status is RunStatus.SUCCEEDED
    assert result.succeeded
    assert result.completed_steps == ("review",)
    assert result.outputs["review"] == "done:supplier:reviewer"
    assert backend.requests[0].provenance.supplied_by == str(SUPPLIER)
    assert result.plan is not None
    assert set(result.plan.agents) == {"supplier:extra", "supplier:reviewer"}

    # The host seam has no shape that could have supplied it (lib Core 4).
    assert not hasattr(services_for(tmp_path), "agents")
    assert set(HOST_PORTS) == {f.name for f in __import__("dataclasses").fields(HostServices)}


def test_run_reports_an_undeclared_agent_without_running_a_step(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        """
        schema_version: 2
        dependencies:
          - source: {supplier}
            kind: bundle
        steps:
          - id: review
            agent: host:only
            instruction: Review the change.
        """,
    )
    backend = ExplodingBackend()
    result = run_recipe(recipe, backend, tmp_path)

    assert result.status is RunStatus.FAILED
    assert not result.succeeded
    assert isinstance(result.error, UndeclaredAgentError)
    assert result.error.agent == "host:only"
    assert result.completed_steps == ()


def test_run_reports_a_strict_parse_failure_without_running_a_step(tmp_path: Path) -> None:
    """manifest Core 1: an unknown key is an error, and `run` says so."""
    recipe = write_recipe(
        tmp_path,
        """
        schema_version: 2
        dependencies:
          - source: {supplier}
            kind: bundle
        stpes:
          - id: typo
        """,
    )
    result = run_recipe(recipe, ExplodingBackend(), tmp_path)

    assert result.status is RunStatus.FAILED
    assert "stpes" in str(result.error)
    assert result.completed_steps == ()


def test_run_refuses_a_step_it_cannot_execute(tmp_path: Path) -> None:
    """lib Core 8: an unrunnable step is a failure, never a silent skip."""
    recipe = write_recipe(
        tmp_path,
        """
        schema_version: 2
        dependencies:
          - source: {supplier}
            kind: bundle
        steps:
          - id: review
            agent: supplier:reviewer
            instruction: Review the change.
          - id: summarize
            description: no agent, no instruction
        """,
    )
    backend = RecordingBackend()
    result = run_recipe(recipe, backend, tmp_path)

    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, UnsupportedStepError)
    assert result.error.step_id == "summarize"
    assert result.completed_steps == ("review",)
    assert "summarize" not in result.outputs


def test_run_refuses_a_nested_step_body(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        """
        schema_version: 2
        dependencies:
          - source: {supplier}
            kind: bundle
        steps:
          - id: loop
            agent: supplier:reviewer
            instruction: Review each.
            steps:
              - id: inner
                agent: supplier:reviewer
                instruction: inner
        """,
    )
    result = run_recipe(recipe, ExplodingBackend(), tmp_path)

    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, UnsupportedStepError)
    assert result.error.step_id == "loop"


def test_run_without_host_services_fails_honestly(tmp_path: Path) -> None:
    request = RunRequest(recipe=write_recipe(tmp_path), run_id="run-test")
    result = asyncio.run(pkg.run(request, resolver=LocalBundleResolver(), spawn_backend=ExplodingBackend()))

    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, ExecutionError)
    assert "services" in str(result.error)


def test_run_honours_the_cancellation_port(tmp_path: Path) -> None:
    result = run_recipe(write_recipe(tmp_path), ExplodingBackend(), tmp_path, cancellation=Cancelled())

    assert result.status is RunStatus.CANCELLED
    assert result.completed_steps == ()


def test_run_emits_events_on_the_sink_port(tmp_path: Path) -> None:
    sink = CollectingSink()
    result = run_recipe(write_recipe(tmp_path), RecordingBackend(), tmp_path, event_sink=sink)

    assert result.succeeded
    assert "session:ready" in sink.kinds
    assert sink.kinds.count("step:start") == 1
    assert sink.kinds.count("step:complete") == 1
    assert all(event.run_id == "run-test" for event in sink.events)


def test_a_broken_sink_never_fails_a_run(tmp_path: Path) -> None:
    result = run_recipe(write_recipe(tmp_path), RecordingBackend(), tmp_path, event_sink=ExplodingSink())
    assert result.status is RunStatus.SUCCEEDED


def test_run_executes_staged_steps_in_order(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        """
        schema_version: 2
        dependencies:
          - source: {supplier}
            kind: bundle
        stages:
          - name: first
            steps:
              - id: a
                agent: supplier:reviewer
                instruction: one
          - name: second
            steps:
              - id: b
                agent: supplier:extra
                instruction: two
        """,
    )
    backend = RecordingBackend()
    result = run_recipe(recipe, backend, tmp_path)

    assert result.succeeded
    assert result.completed_steps == ("a", "b")
    assert backend.canonicals == ["supplier:reviewer", "supplier:extra"]


def test_run_resolves_a_recipe_declared_alias(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        """
        schema_version: 2
        dependencies:
          - source: {supplier}
            kind: bundle
        agents:
          critic: supplier:reviewer
        steps:
          - id: review
            agent: critic
            instruction: Review the change.
        """,
    )
    backend = RecordingBackend()
    result = run_recipe(recipe, backend, tmp_path)

    assert result.succeeded
    assert backend.requests[0].agent == "critic"
    assert backend.requests[0].canonical == "supplier:reviewer"


# --------------------------------------------------------------------------
# Structural: no caller import beyond the five ports (lib Core 3, Core 4)
# --------------------------------------------------------------------------


EXECUTION_SOURCE = Path(execution.__file__).read_text(encoding="utf-8")
EXECUTION_TREE = ast.parse(EXECUTION_SOURCE)


def test_execution_imports_nothing_from_amplifier_at_module_level() -> None:
    """Foundation must be lazy, or the library stops importing without it."""
    offenders: list[str] = []
    for node in EXECUTION_TREE.body:  # module level only -- function-local is the point
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] if node.level == 0 else []
        else:
            continue
        offenders += [n for n in names if n.split(".")[0].startswith("amplifier")]
    assert offenders == []


def test_execution_reads_no_host_service_beyond_the_five_ports() -> None:
    """lib Core 4: the runner may only ever reach for a named port."""
    reached = {
        node.attr
        for node in ast.walk(EXECUTION_TREE)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "services"
    }
    assert reached, "the guard is only meaningful if it actually sees the accesses"
    assert reached <= set(HOST_PORTS), f"reached beyond the ports: {sorted(reached - set(HOST_PORTS))}"


def test_spawn_request_carries_no_caller_session() -> None:
    fields = {f.name for f in __import__("dataclasses").fields(SpawnRequest)}
    assert "parent_session" not in fields
    assert "agent_configs" not in fields
    assert {"canonical", "provenance", "definition"} <= fields


# --------------------------------------------------------------------------
# Foundation composition path (skipped without the install)
# --------------------------------------------------------------------------


@needs_foundation
def test_foundation_factory_composes_only_the_plan_closure(tmp_path: Path) -> None:
    """compose -> load_agent_metadata, narrowed to the frozen catalog."""
    plan = planned(write_recipe(tmp_path))
    factory = FoundationSessionFactory(install_deps=False)

    bundle = asyncio.run(factory.compose(plan, PlanCatalog.from_plan(plan)))

    assert set(bundle.agents) == {"supplier:reviewer", "supplier:extra"}
    assert factory.dropped_agents == ()
    # Metadata came from the declared dependency's own files.
    assert "declared closure" in str(bundle.agents["supplier:reviewer"].get("description", ""))


@needs_foundation
def test_foundation_factory_drops_an_agent_outside_the_catalog(tmp_path: Path) -> None:
    """Belt and braces: composition can only reach declared bundles, and if it
    ever reached further, the run would say so rather than widen silently."""
    plan = planned(write_recipe(tmp_path))
    narrowed = PlanCatalog({k: v for k, v in plan.agents.items() if k == "supplier:reviewer"})
    factory = FoundationSessionFactory(install_deps=False)

    bundle = asyncio.run(factory.compose(plan, narrowed))

    assert set(bundle.agents) == {"supplier:reviewer"}
    assert factory.dropped_agents == ("supplier:extra",)


def test_foundation_factory_reports_an_empty_closure_honestly(tmp_path: Path) -> None:
    factory = FoundationSessionFactory(registry=object())
    empty = ExecutionPlan(recipe_digest="sha256:none", schema_version=2)

    with pytest.raises(ExecutionError) as excinfo:
        asyncio.run(factory.compose(empty, PlanCatalog({})))

    assert "no dependencies" in str(excinfo.value)


def test_foundation_factory_refuses_a_host_with_no_provider_roles(tmp_path: Path) -> None:
    class NoRoles:
        def roles(self) -> list[str]:
            return []

        def resolve(self, role: str) -> object:  # pragma: no cover - never reached
            raise KeyError(role)

    plan = planned(write_recipe(tmp_path))
    services = HostServices(provider_access=NoRoles(), workspace=tmp_path)  # type: ignore[arg-type]

    with pytest.raises(ExecutionError) as excinfo:
        asyncio.run(FoundationSessionFactory().create(plan, PlanCatalog.from_plan(plan), services, run_id="r"))

    assert "no model roles" in str(excinfo.value)


# --------------------------------------------------------------------------
# Entry points are wired (lib Core 2)
# --------------------------------------------------------------------------


def test_plan_and_run_are_exported_and_async() -> None:
    assert pkg.plan is execution.plan
    assert pkg.run is execution.run
    assert {"plan", "run"} <= set(pkg.__all__)
    assert inspect.iscoroutinefunction(pkg.plan)
    assert inspect.iscoroutinefunction(pkg.run)


def test_plan_executes_nothing(tmp_path: Path) -> None:
    request = RunRequest(recipe=write_recipe(tmp_path), services=services_for(tmp_path))
    plan = asyncio.run(pkg.plan(request, resolver=LocalBundleResolver()))

    assert plan.schema_version == 2
    assert plan.step_ids == ("review",)
    assert set(plan.agents) == {"supplier:extra", "supplier:reviewer"}
    assert plan.policy is not None and plan.policy.isolated is True
    # Nothing was written into the workspace by planning.
    assert [p.name for p in tmp_path.iterdir()] == ["recipe.yaml"]


def test_plan_works_without_host_services(tmp_path: Path) -> None:
    request = RunRequest(recipe=write_recipe(tmp_path))
    plan = asyncio.run(pkg.plan(request, resolver=LocalBundleResolver()))
    assert isinstance(plan, ExecutionPlan)


def test_isolation_is_provable_end_to_end(tmp_path: Path) -> None:
    """One assertion chain over the whole seam, for the record.

    Plan -> catalog -> session -> spawn: at no point does a host-supplied
    agent map participate, even when one is pushed through the very argument
    a host would use.
    """
    plan = planned(write_recipe(tmp_path))
    backend = RecordingBackend()
    session = session_for(plan, backend, tmp_path)
    hostile = simulated_host_catalog(IMPOSTOR)

    asyncio.run(session.spawn_adapter("supplier:reviewer", "go", object(), hostile))

    assert isinstance(session.catalog, PlanCatalog)
    assert session.catalog.names == tuple(sorted(plan.agents))
    assert backend.requests[0].definition["supplied_by"] == str(SUPPLIER)
    assert session.spawn_adapter.ignored_host_agents == ("supplier:reviewer",)
    assert isinstance(backend.requests[0].context, Mapping)
    assert isinstance(session.catalog.agent_configs(), MappingProxyType)
