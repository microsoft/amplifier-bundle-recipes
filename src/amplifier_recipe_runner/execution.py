"""The recipe-owned execution session, and the spawn adapter that feeds it.

Contracts: ``recipe-dependency-manifest.v1`` Core 3, 4, 5 and
``recipe-runner-lib.v1`` Core 2, 3, 4.

Planning answered *what would this recipe run with?*. This module answers *and
now run it there* -- in a session the recipe owns, whose agent surface is the
frozen :class:`~amplifier_recipe_runner.api.ExecutionPlan` and nothing else.

The load-bearing idea
---------------------
Everything here funnels through one object: :class:`PlanCatalog`, built once
from ``plan.agents`` and never added to. :class:`PlanCatalogSpawnAdapter` --
the runner's own ``session.spawn`` capability -- resolves every agent name
against that catalog:

* **Closed-world resolution (manifest Core 3).** The adapter has no parameter,
  no attribute, and no fallback by which a caller's agent map could be
  consulted. A name outside the catalog raises
  :class:`~amplifier_recipe_runner.errors.UndeclaredAgentError`; it never
  quietly resolves from somewhere else.
* **Isolation by default (manifest Core 4).** The session is composed from the
  declared closure alone. Hosts reach it only through the five ports in
  :mod:`~amplifier_recipe_runner.ports`, none of which carries agents.
* **A colliding host agent changes nothing (manifest Core 5).** The
  ``session.spawn`` capability signature that Amplifier hosts use passes an
  ``agent_configs`` map. This adapter accepts that argument for signature
  compatibility and **discards it**, recording each discarded name on
  :attr:`PlanCatalogSpawnAdapter.ignored_host_agents`. Ignoring it silently
  would be indistinguishable from honouring it, so it is ignored *visibly*.
* **Neutral session abstraction (lib Core 3).** :class:`RecipeExecutionSession`
  implements the library's own
  :class:`~amplifier_recipe_runner.api.ExecutionSession` protocol. Amplifier's
  coordinator never appears in a signature here.

Foundation is imported **lazily**, inside methods, exactly as
:mod:`~amplifier_recipe_runner.resolver` does -- so this module (and the whole
public surface) imports and is testable without it.

Scope
-----
:func:`run` executes agent-bearing steps sequentially against the isolated
session. :func:`resume` is the same execution, minus the steps a recorded run
already completed -- one code path, not a second one, so a resumed step cannot
mean something different from the step ``run`` would have executed. Templating,
foreach/while bodies, approval gates, and per-agent module overlays belong to
the orchestration work, not here: a step this executor cannot run fails loud by
name (:class:`UnsupportedStepError`) rather than being skipped into a
fabricated success (lib Core 8).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from typing import Final
from typing import Protocol
from typing import runtime_checkable

from .api import AgentProvenance
from .api import ExecutionPlan
from .api import RunRequest
from .api import RunResult
from .api import RunStatus
from .engine import RECIPE_INTERNAL_KEYS
from .engine import ApprovalLedger
from .engine import ExecutionError
from .engine import RecipeProgram
from .engine import RecursionState
from .engine import ResumeState
from .engine import StepEngine
from .engine import StepSpec
from .engine import UnsupportedStepError
from .engine import load_program
from .errors import PreflightError
from .errors import UndeclaredAgentError
from .manifest import ManifestError
from .manifest import parse_manifest_file
from .planner import plan as plan_dependencies
from .ports import HostServices
from .ports import RunEvent
from .ports import WorkspacePath
from .resolver import DependencyResolver

__all__ = [
    "CONTRACTS",
    "AmbiguousCompletedStepError",
    "ExecutionError",
    "SPAWN_CAPABILITY",
    "FoundationSessionFactory",
    "FoundationSpawnBackend",
    "PlanCatalog",
    "PlanCatalogSpawnAdapter",
    "RecipeExecutionSession",
    "RunStateStore",
    "SessionBuild",
    "SessionFactory",
    "SpawnBackend",
    "SpawnRequest",
    "UnknownCompletedStepError",
    "UnsupportedStepError",
    "create_execution_session",
    "plan",
    "resume",
    "run",
]

CONTRACTS: Final[tuple[str, ...]] = (
    "recipe-dependency-manifest.v1",
    "recipe-runner-lib.v1",
)

#: The capability name Amplifier hosts register agent spawning under. The
#: runner registers its *own* adapter here so that in-session delegation
#: resolves from the plan catalog too -- not just direct
#: :meth:`RecipeExecutionSession.invoke` calls.
SPAWN_CAPABILITY: Final[str] = "session.spawn"

# --------------------------------------------------------------------------
# Execution-time errors (post-preflight)
# --------------------------------------------------------------------------


#: ``ExecutionError`` and ``UnsupportedStepError`` are defined in
#: :mod:`amplifier_recipe_runner.engine` (which raises them) and re-exported
#: here, where hosts have always imported them from.


class UnknownCompletedStepError(ExecutionError):
    """``resume`` was told a step completed that this recipe does not declare.

    Both silent readings are wrong: ignoring the name would resume against a
    step list the caller does not believe in, and running everything would redo
    work the caller said was already done. So this refuses instead, naming both
    sides (lib Core 8).
    """

    def __init__(self, unknown: Sequence[str], declared: Sequence[str]) -> None:
        self.unknown = tuple(unknown)
        self.declared = tuple(declared)
        named = ", ".join(repr(step) for step in self.unknown)
        declares = ", ".join(repr(step) for step in self.declared) if self.declared else "no steps"
        super().__init__(
            f"resume was told {named} already completed, but this recipe declares {declares}.",
            remedy=(
                "Resume against the recipe the run recorded, or start a fresh run -- a resumed "
                "run never guesses which step an unrecognised id meant."
            ),
        )


class AmbiguousCompletedStepError(ExecutionError):
    """``resume`` was told a step completed that the recipe declares twice.

    Nothing validates step-id uniqueness, so a recipe *can* declare the same id
    more than once. ``run`` merely overwrites an output when that happens;
    ``resume`` would skip real, unfinished work -- so it refuses rather than
    guess which occurrence the recorded id meant (lib Core 8).
    """

    def __init__(self, ambiguous: Sequence[str]) -> None:
        self.ambiguous = tuple(ambiguous)
        named = ", ".join(repr(step) for step in self.ambiguous)
        super().__init__(
            f"resume was told {named} already completed, but this recipe declares that id more than once, "
            "so which occurrence ran cannot be known.",
            remedy=(
                "Give every step a unique `id:` and start a fresh run -- resuming cannot "
                "distinguish two steps that share one name."
            ),
        )


class MissingHostServicesError(ExecutionError):
    """``run`` was called without the five host ports it requires."""

    def __init__(self) -> None:
        super().__init__(
            "run requires host services; `RunRequest.services` was None.",
            remedy="Pass HostServices(provider_access=..., workspace=...) on the RunRequest.",
        )


# --------------------------------------------------------------------------
# The frozen catalog (manifest Core 3, Core 4)
# --------------------------------------------------------------------------


class PlanCatalog:
    """The agent surface of a run: an :class:`ExecutionPlan`, frozen.

    Built once from ``plan.agents`` -- which the planner populated exclusively
    from declared dependencies -- and never mutated. There is deliberately no
    ``add``, no ``update``, and no constructor argument that could carry a
    caller's agents (manifest Core 3, Core 4).

    Keys are what a step may reference: every canonical ``namespace:name`` in
    the closure, plus each alias a step actually used. Anything else is
    undeclared, by definition.
    """

    __slots__ = ("_by_name", "_definitions")

    def __init__(self, agents: Mapping[str, AgentProvenance]) -> None:
        by_name: dict[str, AgentProvenance] = dict(agents)
        self._by_name: Mapping[str, AgentProvenance] = MappingProxyType(by_name)
        self._definitions: Mapping[str, Mapping[str, Any]] = MappingProxyType(
            {name: _definition_for(prov) for name, prov in by_name.items()}
        )

    @classmethod
    def from_plan(cls, plan: ExecutionPlan) -> PlanCatalog:
        """Freeze ``plan``'s agent provenance into a catalog."""
        return cls(plan.agents)

    # -- read-only surface -------------------------------------------------

    @property
    def names(self) -> tuple[str, ...]:
        """Canonical ``namespace:name`` of every agent the closure supplies."""
        return tuple(sorted(name for name, prov in self._by_name.items() if prov.alias is None))

    @property
    def aliases(self) -> tuple[str, ...]:
        """Recipe-declared aliases a step referenced, if any."""
        return tuple(sorted(name for name, prov in self._by_name.items() if prov.alias is not None))

    def __contains__(self, reference: object) -> bool:
        return reference in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._by_name))

    def resolve(self, reference: str, *, step_id: str | None = None) -> AgentProvenance:
        """Resolve ``reference`` within the closure, or refuse it.

        Raises:
            UndeclaredAgentError: ``reference`` is outside the plan. The
                caller's agent map is never consulted as a fallback -- there
                is no code path here that could consult one.
        """
        provenance = self._by_name.get(reference)
        if provenance is None:
            raise UndeclaredAgentError(
                reference,
                step_id=step_id,
                declared_agents=self.names,
                remedy=(
                    f"This run's agents are exactly {', '.join(self.names) or 'none'} -- "
                    f"resolved from the recipe's declared dependency closure. Declare a "
                    f"dependency supplying {reference!r} in the recipe's `dependencies` "
                    "block. The calling session's agents never satisfy a reference."
                ),
            )
        return provenance

    def definition(self, reference: str, *, step_id: str | None = None) -> Mapping[str, Any]:
        """The frozen, plan-derived definition the spawn backend may use."""
        self.resolve(reference, step_id=step_id)
        return self._definitions[reference]

    def agent_configs(self) -> Mapping[str, Mapping[str, Any]]:
        """The **entire** agent map this run has. Read-only.

        Named to mirror the ``agent_configs`` argument Amplifier's spawn
        capability carries, so the substitution is obvious at the call site:
        the run uses *this*, never the host's.
        """
        return self._definitions


def _definition_for(provenance: AgentProvenance) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "name": provenance.agent,
            "alias": provenance.alias,
            "local_path": provenance.local_path,
            "supplied_by": provenance.supplied_by,
            "resolved_revision": provenance.resolved_revision,
            "dependency_digest": provenance.dependency_digest,
        }
    )


# --------------------------------------------------------------------------
# Spawn seam
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    """One agent invocation, already resolved against the plan catalog.

    A backend receives this and nothing else: by the time it exists, the name
    has been checked against the closure, so no backend can accidentally widen
    resolution.
    """

    agent: str
    """The reference as written -- canonical name or recipe alias."""

    canonical: str
    """The closure's canonical ``namespace:name`` for :attr:`agent`."""

    instruction: str
    run_id: str
    workspace: Path
    provenance: AgentProvenance
    definition: Mapping[str, Any]
    """Plan-derived facts about the agent. Frozen."""

    context: Mapping[str, Any] = MappingProxyType({})
    step_id: str | None = None


@runtime_checkable
class SpawnBackend(Protocol):
    """Performs the actual agent invocation for an already-resolved request.

    The seam exists so resolution policy (this module's whole point) is
    testable without a model call, and so an embedder can supply its own
    execution machinery. A backend never resolves names.
    """

    async def spawn(self, request: SpawnRequest) -> str:
        """Run the agent and return its output text."""
        ...


class PlanCatalogSpawnAdapter:
    """The runner-owned ``session.spawn`` capability.

    Resolution source: the frozen :class:`PlanCatalog`, exclusively.

    The call signature mirrors the capability shape Amplifier hosts register
    (``agent_name``, ``instruction``, ``parent_session``, ``agent_configs``,
    plus inheritance keyword arguments) so this adapter can be dropped into a
    real session in that slot. Every host-supplied argument that could widen
    the recipe's world -- ``agent_configs``, ``parent_session``,
    ``parent_messages``, tool/hook inheritance -- is **discarded and
    recorded**, never consulted (manifest Core 4, Core 5).
    """

    __slots__ = (
        "_backend",
        "_catalog",
        "_event_sink",
        "_ignored_arguments",
        "_ignored_host_agents",
        "_run_id",
        "_workspace",
    )

    def __init__(
        self,
        catalog: PlanCatalog,
        backend: SpawnBackend,
        *,
        run_id: str,
        workspace: Path,
        event_sink: Any | None = None,
    ) -> None:
        self._catalog = catalog
        self._backend = backend
        self._run_id = run_id
        self._workspace = workspace
        self._event_sink = event_sink
        self._ignored_host_agents: list[str] = []
        self._ignored_arguments: list[str] = []

    @property
    def catalog(self) -> PlanCatalog:
        return self._catalog

    @property
    def ignored_host_agents(self) -> tuple[str, ...]:
        """Every agent name a host offered and this adapter refused to use.

        Visible refusal: a host that hands over a colliding catalog can see
        that it was ignored, rather than inferring it from behaviour.
        """
        return tuple(self._ignored_host_agents)

    @property
    def ignored_arguments(self) -> tuple[str, ...]:
        """Host inheritance arguments discarded, in call order."""
        return tuple(self._ignored_arguments)

    async def __call__(
        self,
        agent_name: str,
        instruction: str,
        parent_session: Any = None,
        agent_configs: Mapping[str, Any] | None = None,
        *,
        context: Mapping[str, Any] | None = None,
        step_id: str | None = None,
        **host_arguments: Any,
    ) -> Mapping[str, Any]:
        """Resolve ``agent_name`` in the closure and run it."""
        self._record_ignored(parent_session, agent_configs, host_arguments)

        provenance = self._catalog.resolve(agent_name, step_id=step_id)
        request = SpawnRequest(
            agent=agent_name,
            canonical=provenance.agent,
            instruction=instruction,
            run_id=self._run_id,
            workspace=self._workspace,
            provenance=provenance,
            definition=self._catalog.definition(agent_name),
            context=MappingProxyType(dict(context or {})),
            step_id=step_id,
        )
        self._emit("agent:start", {"agent": provenance.agent, "step_id": step_id})
        output = await self._backend.spawn(request)
        self._emit("agent:complete", {"agent": provenance.agent, "step_id": step_id})
        return MappingProxyType(
            {
                "output": output,
                "agent": provenance.agent,
                "supplied_by": provenance.supplied_by,
                "session_id": f"{self._run_id}:{provenance.agent}",
            }
        )

    # -- internals ---------------------------------------------------------

    def _record_ignored(
        self,
        parent_session: Any,
        agent_configs: Mapping[str, Any] | None,
        host_arguments: Mapping[str, Any],
    ) -> None:
        if agent_configs:
            self._ignored_host_agents.extend(str(name) for name in agent_configs)
            self._ignored_arguments.append("agent_configs")
        if parent_session is not None:
            self._ignored_arguments.append("parent_session")
        self._ignored_arguments.extend(name for name, value in host_arguments.items() if value is not None)

    def _emit(self, kind: str, data: Mapping[str, Any]) -> None:
        _emit(self._event_sink, kind, self._run_id, data)


def _emit(sink: Any | None, kind: str, run_id: str, data: Mapping[str, Any]) -> None:
    """Emit on the event-sink port. A sink failure never fails a run."""
    if sink is None:
        return
    try:
        sink.emit(RunEvent(kind=kind, run_id=run_id, data=MappingProxyType(dict(data))))
    except Exception:  # noqa: BLE001 - contract: a sink must not break a run
        pass


# --------------------------------------------------------------------------
# The neutral session (lib Core 3)
# --------------------------------------------------------------------------


class RecipeExecutionSession:
    """The library's own execution session, owned by the recipe.

    Satisfies :class:`~amplifier_recipe_runner.api.ExecutionSession`. Its agent
    surface is :attr:`catalog` and nothing else, and it exposes no method to
    install, inject, or import an agent from anywhere.
    """

    __slots__ = ("_adapter", "_catalog", "_closers", "_closed", "_run_id", "_workspace")

    def __init__(
        self,
        *,
        run_id: str,
        workspace: Path,
        catalog: PlanCatalog,
        adapter: PlanCatalogSpawnAdapter,
        closers: Sequence[Any] = (),
    ) -> None:
        self._run_id = run_id
        self._workspace = workspace
        self._catalog = catalog
        self._adapter = adapter
        self._closers = list(closers)
        self._closed = False

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def workspace(self) -> WorkspacePath:
        return WorkspacePath(self._workspace)

    @property
    def catalog(self) -> PlanCatalog:
        return self._catalog

    @property
    def spawn_adapter(self) -> PlanCatalogSpawnAdapter:
        return self._adapter

    def available_agents(self) -> Sequence[str]:
        """Canonical names from the plan's closure. Read-only, closed-world."""
        return self._catalog.names

    async def invoke(
        self,
        agent: str,
        instruction: str,
        *,
        context: Mapping[str, Any] | None = None,
        step_id: str | None = None,
    ) -> str:
        """Run one agent from the closure.

        Raises:
            UndeclaredAgentError: ``agent`` is outside the plan.
        """
        result = await self._adapter(agent, instruction, context=context, step_id=step_id)
        return str(result["output"])

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for closer in reversed(self._closers):
            try:
                outcome = closer()
                if hasattr(outcome, "__await__"):
                    await outcome
            except Exception:  # noqa: BLE001 - teardown must not mask a result
                pass


# --------------------------------------------------------------------------
# Building the session from a plan
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionBuild:
    """What a session factory produced: a backend, plus how to tear it down."""

    backend: SpawnBackend
    closers: tuple[Any, ...] = ()
    session: Any | None = None
    """The host-layer session object, when one was built. Never public API."""


@runtime_checkable
class SessionFactory(Protocol):
    """Builds the execution machinery a :class:`SpawnBackend` needs.

    Injectable for the same reason the resolver is (lib Core 5): the runner's
    resolution policy must be provable without Foundation, a network, or a
    model.
    """

    async def create(
        self,
        plan: ExecutionPlan,
        catalog: PlanCatalog,
        services: HostServices,
        *,
        run_id: str,
    ) -> SessionBuild: ...


async def create_execution_session(
    plan: ExecutionPlan,
    services: HostServices,
    *,
    run_id: str | None = None,
    spawn_backend: SpawnBackend | None = None,
    session_factory: SessionFactory | None = None,
) -> RecipeExecutionSession:
    """Build the recipe-owned session for ``plan``.

    The catalog is frozen from ``plan`` *before* any session exists, so there
    is no window in which the session's agent surface could come from anywhere
    else (manifest Core 3, Core 4).

    Args:
        plan: The resolved plan. Its ``agents`` become the entire agent world.
        services: The five host ports. No port carries agents.
        run_id: Caller-chosen run identifier; generated when omitted.
        spawn_backend: Executes resolved invocations. When supplied it takes
            precedence and **no** host session is built (so ``session_factory``
            is unused); when omitted, one is built by ``session_factory``.
        session_factory: Builds the backend. Defaults to
            :class:`FoundationSessionFactory` (Foundation imported lazily).

    Returns:
        A :class:`RecipeExecutionSession` whose ``available_agents()`` are
        exactly the plan's.
    """
    identifier = run_id or f"run-{uuid.uuid4().hex[:12]}"
    catalog = PlanCatalog.from_plan(plan)
    workspace = Path(services.workspace)

    closers: tuple[Any, ...] = ()
    session: Any | None = None
    if spawn_backend is None:
        if not len(catalog):
            # The plan resolved no agents, so there is nothing an agent
            # session could serve. Composing one anyway would make a recipe of
            # pure `bash`/`foreach` steps require Foundation, a bundle fetch,
            # and a provider it never uses. The empty catalog still refuses
            # every name (`UndeclaredAgentError`), so nothing is loosened --
            # the refusal simply happens without paying for a session first.
            spawn_backend = _NoDeclaredAgentsBackend()
        else:
            factory = session_factory or FoundationSessionFactory()
            build = await factory.create(plan, catalog, services, run_id=identifier)
            spawn_backend, closers, session = build.backend, build.closers, build.session

    adapter = PlanCatalogSpawnAdapter(
        catalog,
        spawn_backend,
        run_id=identifier,
        workspace=workspace,
        event_sink=services.event_sink,
    )
    if session is not None:
        _register_spawn_capability(session, adapter)

    _emit(services.event_sink, "session:ready", identifier, {"agents": catalog.names})
    return RecipeExecutionSession(
        run_id=identifier,
        workspace=workspace,
        catalog=catalog,
        adapter=adapter,
        closers=closers,
    )


def _load_target(dependency: Any) -> str:
    """The path the registry should load for ``dependency``.

    A ``#subdirectory=`` partial that names a FILE (``providers/anthropic-sonnet.yaml``)
    resolves to that file's parent DIRECTORY in ``local_path`` -- which is not
    a bundle, so loading it fails with "missing bundle.md". The declared
    subdirectory says which file was actually asked for, so it is used when it
    names one. Directory-style partials (a folder with its own ``bundle.md``)
    are unaffected: no file candidate exists and the directory is loaded as
    before.
    """
    local_path = getattr(dependency, "local_path", None)
    subdirectory = getattr(dependency, "subdirectory", None)
    if local_path and subdirectory and str(subdirectory).endswith((".yaml", ".yml")):
        base = Path(local_path)
        name = Path(str(subdirectory)).name
        for candidate in (base / name, base.parent / str(subdirectory)):
            if candidate.is_file():
                return str(candidate)
    return str(local_path or dependency.uri)


class _NoDeclaredAgentsBackend:
    """Stand-in backend for a plan that resolved no agents at all.

    Unreachable by construction: :class:`PlanCatalog` refuses every name
    before a backend is consulted, and an empty catalog refuses all of them.
    It exists so that "no agents were declared" is a *stated* fact rather than
    a `None` that something later dereferences.
    """

    async def spawn(self, request: SpawnRequest) -> str:  # pragma: no cover - unreachable
        raise ExecutionError(
            f"Step {request.step_id!r} asked for agent {request.agent!r}, but this recipe declares no agents.",
            remedy="Declare a dependency supplying the agent in the recipe's `dependencies` block.",
        )


def _register_spawn_capability(session: Any, adapter: PlanCatalogSpawnAdapter) -> None:
    """Point the session's own delegation at the plan catalog.

    Without this, an in-session ``delegate``-style tool would spawn through
    whatever capability the host registered -- which resolves from the host's
    agent map. Registering the runner's adapter closes that route.
    """
    coordinator = getattr(session, "coordinator", None)
    register = getattr(coordinator, "register_capability", None)
    if register is None:  # pragma: no cover - depends on the host session shape
        raise ExecutionError(
            "The execution session exposes no capability registry, so recipe "
            "agent spawning could not be bound to the plan catalog.",
            remedy="Supply a spawn_backend, or a session factory whose session registers capabilities.",
        )
    register(SPAWN_CAPABILITY, adapter)


class FoundationSessionFactory:
    """Builds the session from the plan closure, using Foundation.

    ``compose`` -> ``load_agent_metadata`` -> ``prepare`` -> ``create_session``,
    over exactly the dependencies the plan resolved -- never the host's bundle.

    Two narrowings make the isolation structural rather than incidental:

    * The registry is **strict**, so an include that fails to resolve raises
      instead of silently yielding a smaller closure.
    * After composition the bundle's agent map is intersected with the plan
      catalog, and anything outside it is dropped and recorded on
      :attr:`dropped_agents`. Composition can only ever *reach* declared
      dependencies, so this should be empty -- and if it ever is not, the run
      says so rather than quietly widening.

    ``amplifier_foundation`` is imported lazily, inside :meth:`create`.
    """

    __slots__ = ("_dropped_agents", "_install_deps", "_registry", "_strict")

    def __init__(
        self,
        *,
        strict: bool = True,
        install_deps: bool = True,
        registry: Any | None = None,
    ) -> None:
        self._strict = strict
        self._install_deps = install_deps
        self._registry = registry
        self._dropped_agents: tuple[str, ...] = ()

    @property
    def dropped_agents(self) -> tuple[str, ...]:
        """Composed agents that were outside the plan catalog, and dropped."""
        return self._dropped_agents

    async def create(
        self,
        plan: ExecutionPlan,
        catalog: PlanCatalog,
        services: HostServices,
        *,
        run_id: str,
    ) -> SessionBuild:
        if not services.provider_access.roles():
            raise ExecutionError(
                "The host's provider access offers no model roles, so no agent could run.",
                remedy="Return at least one role from ProviderAccess.roles().",
            )

        bundle = await self.compose(plan, catalog)
        prepared = await bundle.prepare(install_deps=self._install_deps)
        workspace = Path(services.workspace)
        session = await prepared.create_session(session_cwd=workspace)

        # Measured off the COMPOSED closure, not off the host's port. The port
        # says which roles a host is willing to serve; this says whether the
        # recipe's own world can actually reach a model. Without it, an agent
        # step returns the sub-session's "No providers available" string AS ITS
        # OUTPUT -- a run that exits 0 with that text written into its report is
        # the fabricated success lib Core 8 forbids.
        #
        # A bundle that does not expose `providers` at all has made no claim,
        # so it is not treated as a denial -- only an explicitly EMPTY list is.
        # Refusing on an un-measurable absence would fail an embedder whose
        # bundle object simply has a different shape.
        declared_providers = getattr(bundle, "providers", None)
        has_providers = True if declared_providers is None else bool(declared_providers)
        backend = FoundationSpawnBackend(prepared, workspace=workspace, has_providers=has_providers)
        return SessionBuild(
            backend=backend,
            closers=(session.cleanup,),
            session=session,
        )

    async def compose(self, plan: ExecutionPlan, catalog: PlanCatalog) -> Any:
        """Compose the plan's dependencies into one bundle, narrowed to ``catalog``.

        Returns Foundation's ``Bundle``. Typed ``Any`` on purpose: Foundation
        objects are not part of this library's public surface (lib Core 3).
        """
        registry = self._get_registry()
        bundles = [await self._load(registry, dependency) for dependency in plan.dependencies]
        if not bundles:
            raise ExecutionError(
                "The plan resolved no dependencies, so there is no bundle to execute in.",
                remedy="Declare at least one dependency in the recipe's `dependencies` block.",
            )

        composed = bundles[0].compose(*bundles[1:])
        composed.load_agent_metadata()
        self._narrow_to_catalog(composed, catalog)
        return composed

    def _narrow_to_catalog(self, bundle: Any, catalog: PlanCatalog) -> None:
        agents = getattr(bundle, "agents", None)
        if not isinstance(agents, dict):  # pragma: no cover - shape guard
            return
        namespace = str(getattr(bundle, "name", "") or "")
        kept: dict[str, Any] = {}
        dropped: list[str] = []
        for name, config in agents.items():
            canonical = name if ":" in name else f"{namespace}:{name}"
            if name in catalog or canonical in catalog:
                kept[name] = config
            else:
                dropped.append(name)
        bundle.agents = kept
        self._dropped_agents = tuple(sorted(dropped))

    async def _load(self, registry: Any, dependency: Any) -> Any:
        target = _load_target(dependency)
        try:
            return await registry.load(target)
        except Exception as exc:  # registry raises its own hierarchy
            raise ExecutionError(
                f"Dependency {dependency.uri!r} could not be loaded for execution: {type(exc).__name__}: {exc}",
                remedy="Re-plan the recipe; the resolved dependency is no longer readable at its recorded path.",
            ) from exc

    def _get_registry(self) -> Any:
        if self._registry is not None:
            return self._registry
        try:
            from amplifier_foundation.paths.resolution import get_amplifier_home
            from amplifier_foundation.registry import BundleRegistry
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ExecutionError(
                f"amplifier-foundation is not importable: {exc}",
                remedy=("Install amplifier-foundation, or pass a spawn_backend / session_factory instead."),
            ) from exc

        from .resolver import DEFAULT_RUNNER_NAMESPACE

        self._registry = BundleRegistry(home=Path(get_amplifier_home()) / DEFAULT_RUNNER_NAMESPACE, strict=self._strict)
        return self._registry


class FoundationSpawnBackend:
    """Runs a resolved invocation in a sub-session of the recipe's own bundle.

    Minimal by design: the sub-session is built from the *same* prepared
    closure, so an agent can reach exactly what the recipe declared. Per-agent
    module/tool overlays and inheritance policy are the orchestration work
    item, not this one.

    This backend performs real model calls, so nothing in the test suite
    exercises it; the resolution policy it serves is proved against injected
    backends instead.
    """

    __slots__ = ("_has_providers", "_prepared", "_workspace")

    def __init__(self, prepared: Any, *, workspace: Path, has_providers: bool = True) -> None:
        self._prepared = prepared
        self._workspace = workspace
        self._has_providers = has_providers

    async def spawn(self, request: SpawnRequest) -> str:
        if not self._has_providers:
            # Refused HERE, not at composition: a recipe whose agent steps are
            # all skipped by conditions never needs a provider, and failing it
            # up front would be a false alarm. This fires only when a step
            # really reaches for a model.
            raise ExecutionError(
                f"Step {request.step_id!r} needs agent {request.canonical!r}, but this recipe's "
                "declared closure configures no model provider, so the agent has nothing to run on.",
                remedy=(
                    "Declare one in the recipe's `dependencies:` block -- e.g. a provider partial "
                    "from the same pinned source, `kind: behavior` with "
                    "`#subdirectory=providers/<provider>.yaml`. The calling environment's providers "
                    "are deliberately not borrowed (recipe-dependency-manifest.v1 Core 4)."
                ),
            )
        session = await self._prepared.create_session(session_cwd=self._workspace)
        try:
            return str(await session.execute(request.instruction))
        finally:
            await session.cleanup()


# --------------------------------------------------------------------------
# Public entry points (lib Core 2)
# --------------------------------------------------------------------------


async def plan(request: RunRequest, *, resolver: DependencyResolver | None = None) -> ExecutionPlan:
    """Resolve ``request``'s dependency closure. Executes nothing.

    No fetch beyond what the resolver needs to *read* dependencies, no module
    activation, no session, no step. Preflight failures raise typed
    :class:`~amplifier_recipe_runner.errors.PreflightError` subclasses.
    """
    recipe_path = Path(request.recipe)
    manifest = parse_manifest_file(recipe_path)
    workspace = Path(request.services.workspace) if request.services is not None else recipe_path.parent
    return await plan_dependencies(
        manifest,  # type: ignore[arg-type]
        resolver if resolver is not None else _default_resolver(),
        workspace,
        recipe=recipe_path,
        trust_policy=request.trust_policy,
        lock_mode=request.lock_mode,
    )


async def run(
    request: RunRequest,
    *,
    resolver: DependencyResolver | None = None,
    spawn_backend: SpawnBackend | None = None,
    session_factory: SessionFactory | None = None,
) -> RunResult:
    """Plan ``request``, then execute it in a recipe-owned session.

    Agent-bearing steps run sequentially against the isolated session. The
    result is honest in both directions: a preflight refusal comes back as
    :attr:`~amplifier_recipe_runner.api.RunStatus.FAILED` with the typed error
    attached and no step run, and a step failure never reports success (lib
    Core 8).
    """
    return await _plan_and_execute(
        request,
        completed_steps=(),
        resolver=resolver,
        spawn_backend=spawn_backend,
        session_factory=session_factory,
    )


async def resume(
    request: RunRequest,
    *,
    completed_steps: Sequence[str] = (),
    resolver: DependencyResolver | None = None,
    spawn_backend: SpawnBackend | None = None,
    session_factory: SessionFactory | None = None,
) -> RunResult:
    """Continue ``request``'s run, skipping the steps it already completed.

    Resuming is :func:`run` with a shorter step list -- the same preflight, the
    same recipe-owned session, the same executor -- so a resumed step can never
    mean something different from the step ``run`` would have executed.

    ``completed_steps`` are matched by step id, not by position, so a run that
    stopped with a gap never re-runs a step it already finished. Each recorded
    id must name exactly one step, and both ways that can fail are refused
    *before* a session is built rather than quietly ignored: a name the recipe
    does not declare raises :class:`UnknownCompletedStepError`, and a name the
    recipe declares twice raises :class:`AmbiguousCompletedStepError`.

    Two things this deliberately does NOT do, because pretending otherwise
    would be the fabricated success lib Core 8 forbids:

    * It does not verify provenance. That is the caller's job and it must
      happen first (manifest Core 8); the standalone CLI does it via
      :func:`~amplifier_recipe_runner.provenance.check_resume_provenance`.
    * It does not reconstruct a skipped step's output. Nothing is re-derived
      and nothing is invented, so ``outputs`` carries only the steps this call
      actually ran. This executor passes no step output into a later step, so
      no remaining step can depend on one; an executor that later threads
      outputs would need them recorded, not guessed.

    The returned ``completed_steps`` covers the whole run -- the steps handed
    in plus the ones this call ran -- so a host that records it verbatim can
    resume again without losing what earlier attempts finished. That holds on
    every failure path too: a refusal reports back at least what it was given,
    because failing to resume never un-completes a step that already ran.
    """
    return await _plan_and_execute(
        request,
        completed_steps=completed_steps,
        resolver=resolver,
        spawn_backend=spawn_backend,
        session_factory=session_factory,
    )


async def _plan_and_execute(
    request: RunRequest,
    *,
    completed_steps: Sequence[str],
    resolver: DependencyResolver | None,
    spawn_backend: SpawnBackend | None,
    session_factory: SessionFactory | None,
) -> RunResult:
    """The one execution path behind :func:`run` and :func:`resume`."""
    run_id = request.run_id or f"run-{uuid.uuid4().hex[:12]}"
    # Carried on every early return: a resume that refuses must not report
    # FEWER completed steps than it was handed, or a host recording the result
    # would erase the very history it resumed from. Nothing is invented -- this
    # is the caller's own list, echoed back unchanged.
    already = tuple(dict.fromkeys(str(step) for step in completed_steps))

    if request.services is None:
        return RunResult(
            run_id=run_id,
            status=RunStatus.FAILED,
            completed_steps=already,
            error=MissingHostServicesError(),
        )

    services = request.services
    recipe_path = Path(request.recipe)
    try:
        resolved = await plan(request, resolver=resolver)
    except (PreflightError, ManifestError) as exc:
        # ManifestError is the parser's own strict-parse failure; it belongs in
        # the same bucket as a typed preflight refusal -- nothing ran either way.
        return RunResult(run_id=run_id, status=RunStatus.FAILED, completed_steps=already, error=exc)

    try:
        program = load_program(recipe_path)
    except ExecutionError as exc:
        return RunResult(run_id=run_id, status=RunStatus.FAILED, plan=resolved, completed_steps=already, error=exc)

    step_ids = tuple(step.id for step in program.all_steps)
    # Refuse before a session exists: nothing should be composed for a request
    # whose recorded step list cannot be honoured. Each recorded id must name
    # exactly one step -- no match and two matches are different defects, so
    # they are reported as different errors.
    unknown = tuple(step for step in already if step not in set(step_ids))
    if unknown:
        return RunResult(
            run_id=run_id,
            status=RunStatus.FAILED,
            plan=resolved,
            completed_steps=already,
            error=UnknownCompletedStepError(unknown, step_ids),
        )
    ambiguous = tuple(step for step in already if step_ids.count(step) > 1)
    if ambiguous:
        return RunResult(
            run_id=run_id,
            status=RunStatus.FAILED,
            plan=resolved,
            completed_steps=already,
            error=AmbiguousCompletedStepError(ambiguous),
        )

    # A run that was cancelled before its first step must not compose a session
    # to discover that. The engine re-checks between every step.
    if _cancelled(services):
        return RunResult(
            run_id=run_id,
            status=RunStatus.CANCELLED,
            plan=resolved,
            completed_steps=already,
        )

    store = RunStateStore.for_request(request, run_id=run_id)
    session = await create_execution_session(
        resolved,
        services,
        run_id=run_id,
        spawn_backend=spawn_backend,
        session_factory=session_factory,
    )
    try:
        return await _execute_program(
            session,
            resolved,
            request,
            services,
            program,
            already,
            store=store,
        )
    finally:
        await session.aclose()


async def _execute_program(
    session: RecipeExecutionSession,
    resolved: ExecutionPlan,
    request: RunRequest,
    services: HostServices,
    program: RecipeProgram,
    already_completed: Sequence[str],
    *,
    store: RunStateStore | None = None,
) -> RunResult:
    """Run ``program``'s full step vocabulary in the recipe-owned session."""
    recorded = store.load() if store is not None else None
    approvals = ApprovalLedger.from_mapping(recorded.get("approvals") if recorded else None)

    resume_state: ResumeState | None = None
    if recorded and recorded.get("engine_state"):
        resume_state = ResumeState.from_mapping(recorded["engine_state"])
    elif already_completed:
        # No persisted state: a resume driven purely by the recorded completed
        # list. Skipping by id (not by position) is what keeps a run that
        # stopped with a gap from re-running a step it already finished.
        resume_state = ResumeState(completed_steps=tuple(already_completed))

    context: dict[str, Any] = {}
    if resume_state is not None and resume_state.context:
        context.update(resume_state.context)
    else:
        context.update(program.context)
        context.update(request.context)
    context["recipe"] = {
        "name": program.name,
        "version": program.version,
        "description": program.description,
        "path": str(Path(request.recipe).expanduser().resolve()),
    }
    context["session"] = {
        "id": session.run_id,
        "started": None,
        "project": str(Path(services.workspace).resolve()),
    }

    engine = _build_engine(
        program,
        session=session,
        request=request,
        services=services,
        approvals=approvals,
        store=store,
    )

    outcome = await engine.execute(context, resume=resume_state)

    if store is not None:
        store.save(
            engine_state=outcome.state,
            approvals=engine.approvals,
            status=outcome.status,
            pending_approval=outcome.pending_approval,
            approval_prompt=outcome.approval_prompt,
        )

    completed = tuple(outcome.completed_steps)
    if outcome.status == "paused":
        return RunResult(
            run_id=session.run_id,
            status=RunStatus.PAUSED,
            plan=resolved,
            outputs=MappingProxyType(dict(outcome.outputs)),
            context=MappingProxyType(dict(outcome.context)),
            completed_steps=completed,
            pending_approval=outcome.pending_approval,
        )
    if outcome.status == "cancelled":
        return RunResult(
            run_id=session.run_id,
            status=RunStatus.CANCELLED,
            plan=resolved,
            outputs=MappingProxyType(dict(outcome.outputs)),
            context=MappingProxyType(dict(outcome.context)),
            completed_steps=completed,
        )
    if outcome.status == "failed":
        return RunResult(
            run_id=session.run_id,
            status=RunStatus.FAILED,
            plan=resolved,
            outputs=MappingProxyType(dict(outcome.outputs)),
            context=MappingProxyType(dict(outcome.context)),
            completed_steps=completed,
            error=outcome.error,
        )
    return RunResult(
        run_id=session.run_id,
        status=RunStatus.SUCCEEDED,
        plan=resolved,
        outputs=MappingProxyType(dict(outcome.outputs)),
        context=MappingProxyType(dict(outcome.context)),
        completed_steps=completed,
    )


def _build_engine(
    program: RecipeProgram,
    *,
    session: RecipeExecutionSession,
    request: RunRequest,
    services: HostServices,
    approvals: ApprovalLedger,
    store: RunStateStore | None,
    recursion: RecursionState | None = None,
) -> StepEngine:
    """Assemble the step engine over one recipe-owned session.

    The engine never resolves an agent name: it hands the reference to
    ``session.invoke``, which resolves it against the frozen plan catalog and
    nothing else (manifest Core 3, Core 4).
    """
    workspace = Path(services.workspace)

    async def invoke_agent(step: StepSpec, instruction: str, context: Mapping[str, Any]) -> Any:
        assert step.agent is not None
        return await session.invoke(step.agent, instruction, context=context, step_id=step.id)

    async def run_sub_recipe(
        path: Path,
        sub_context: dict[str, Any],
        step: StepSpec,
        child_recursion: RecursionState,
    ) -> Mapping[str, Any]:
        return await _run_sub_recipe(
            path,
            sub_context,
            step,
            child_recursion,
            parent_session=session,
            request=request,
            services=services,
            store=store,
        )

    def emit(kind: str, data: Mapping[str, Any]) -> None:
        _emit(services.event_sink, kind, session.run_id, data)

    return StepEngine(
        program,
        invoke_agent=invoke_agent,
        workspace=workspace,
        run_id=session.run_id,
        recipe_path=program.path or Path(request.recipe),
        emit=emit,
        cancellation=services.cancellation,
        approval_callback=services.approval_callback,
        approvals=approvals,
        sub_recipe_runner=run_sub_recipe,
        recursion=recursion,
        scratch_dir=(store.scratch_dir if store is not None else None),
    )


async def _run_sub_recipe(
    path: Path,
    sub_context: dict[str, Any],
    step: StepSpec,
    child_recursion: RecursionState,
    *,
    parent_session: RecipeExecutionSession,
    request: RunRequest,
    services: HostServices,
    store: RunStateStore | None,
) -> Mapping[str, Any]:
    """Run a ``type: recipe`` step's sub-recipe and return its output delta.

    ``schema_version`` is a property of the RECIPE, not of how the recipe was
    reached (manifest Core 3/4). So a v2 sub-recipe is planned and executed
    against **its own** declared closure -- a fresh session -- while a legacy
    sub-recipe runs inside the parent's closure, exactly as the legacy engine
    resolves it. Neither borrows the host's agent map.

    Only the keys the sub-recipe *added* come back. Returning its entire
    context would accumulate every intermediate variable in the parent, which
    is unbounded growth across a foreach.
    """
    sub_program = load_program(path)
    input_keys = set(sub_context) | RECIPE_INTERNAL_KEYS

    context = dict(sub_program.context)
    context.update(sub_context)
    context["recipe"] = {
        "name": sub_program.name,
        "version": sub_program.version,
        "description": sub_program.description,
        "path": str(path),
    }
    context["session"] = {
        "id": f"{parent_session.run_id}:{step.id}",
        "started": None,
        "project": str(Path(services.workspace).resolve()),
    }

    if sub_program.schema_version is not None and sub_program.schema_version >= 2:
        sub_request = RunRequest(
            recipe=path,
            context=dict(sub_context),
            services=services,
            trust_policy=request.trust_policy,
            lock_mode=request.lock_mode,
            run_id=f"{parent_session.run_id}:{step.id}",
            legacy_mode=request.legacy_mode,
            state_dir=None,
        )
        sub_plan = await plan(sub_request)
        sub_session = await create_execution_session(sub_plan, services, run_id=sub_request.run_id or "")
        try:
            engine = _build_engine(
                sub_program,
                session=sub_session,
                request=sub_request,
                services=services,
                approvals=ApprovalLedger(),
                store=None,
                recursion=child_recursion,
            )
            outcome = await engine.execute(context)
        finally:
            await sub_session.aclose()
    else:
        engine = _build_engine(
            sub_program,
            session=parent_session,
            request=request,
            services=services,
            approvals=ApprovalLedger(),
            store=store,
            recursion=child_recursion,
        )
        outcome = await engine.execute(context)

    if outcome.status == "failed" and outcome.error is not None:
        raise outcome.error
    if outcome.status == "paused":
        raise ExecutionError(
            f"Sub-recipe {path.name!r} paused at approval gate {outcome.pending_approval!r}, "
            "which the standalone runner cannot mirror onto the parent run.",
            remedy="Run the staged sub-recipe directly, or move its approval gate to the parent recipe.",
        )

    return {
        key: value
        for key, value in outcome.context.items()
        if key not in input_keys and not key.startswith("_child_session_")
    }


class RunStateStore:
    """Where a run's resumable state lives: ``<run dir>/engine-state.json``.

    Separate from the run manifest on purpose. The manifest records *what the
    run resolved* and is verified on resume (manifest Core 8); this records
    *how far the run got*, which is a different fact with a different
    lifetime. A run with no state directory simply has no store, and a run
    that never pauses never needs one.
    """

    FILENAME = "engine-state.json"

    __slots__ = ("_path", "_root")

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._path = self._root / self.FILENAME

    @classmethod
    def for_request(cls, request: RunRequest, *, run_id: str) -> RunStateStore | None:
        if request.state_dir is None:
            return None
        return cls(Path(request.state_dir))

    @property
    def path(self) -> Path:
        return self._path

    @property
    def scratch_dir(self) -> Path:
        """Run-scoped scratch a bash step can spill an oversized payload into."""
        return self._root / "scratch"

    def load(self) -> dict[str, Any] | None:
        if not self._path.is_file():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def save(
        self,
        *,
        engine_state: ResumeState | None,
        approvals: ApprovalLedger,
        status: str,
        pending_approval: str | None = None,
        approval_prompt: str | None = None,
    ) -> None:
        payload = {
            "status": status,
            "pending_approval": pending_approval,
            "approval_prompt": approval_prompt,
            "approvals": approvals.to_mapping(),
            "engine_state": engine_state.to_mapping() if engine_state is not None else None,
        }
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            # A state write that fails must not turn a completed run into a
            # failed one; the run's own result is still reported honestly.
            pass

    def record_decision(self, stage: str, *, approved: bool, message: str | None = None) -> None:
        """Record a human verdict for ``stage``, for a later process to read."""
        data = self.load() or {}
        approvals = ApprovalLedger.from_mapping(data.get("approvals"))
        approvals.record(stage, approved=approved, message=message)
        data["approvals"] = approvals.to_mapping()
        self._root.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _cancelled(services: HostServices) -> bool:
    token = services.cancellation
    return token is not None and bool(token.cancelled)


def _default_resolver() -> DependencyResolver:
    from .resolver import FoundationResolver

    return FoundationResolver()
