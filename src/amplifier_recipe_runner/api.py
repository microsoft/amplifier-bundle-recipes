"""Public API surface of the recipe runner library.

Contract: ``recipe-runner-lib.v1``.

* **Core 2 -- public API surface.** :class:`RecipeRunner` exposes ``validate``
  (manifest + plan checks, no side effects), ``plan`` (resolved dependency
  plan and agent provenance, no execution), ``run``, and ``resume``. All four
  are usable without a UI and without the Amplifier CLI.
* **Core 3 -- neutral session abstraction.** :class:`ExecutionSession` is the
  library's *own* execution-session abstraction. Amplifier's ``coordinator``
  (and any Amplifier-internal session object) is not public API and does not
  appear in any signature here.
* **Core 7 -- run manifest schema.** :class:`ExecutionPlan` is the stable,
  documented shape of the resolved graph.

Interfaces only: this module defines types and protocols. Implementations land
in sibling modules (manifest parsing, planner, trust policy, session).

Manifest types (``schema_version: 2`` parsing) are governed by the separate
``recipe-dependency-manifest.v1`` contract and live in ``manifest.py``. This
module deliberately has **no import-time dependency** on that module: all
annotations are lazy (``from __future__ import annotations``) and the plan
shape carries resolved *facts* (digests, URIs, provenance) rather than parsed
manifest objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Final
from typing import Protocol
from typing import runtime_checkable

from .ports import HostServices
from .ports import WorkspacePath

__all__ = [
    "RUN_MANIFEST_VERSION",
    "AgentProvenance",
    "DependencyKind",
    "EffectivePolicy",
    "ExecutionPlan",
    "ExecutionSession",
    "LockMode",
    "RecipeRunner",
    "ResolvedDependency",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "TrustPolicy",
    "ValidationIssue",
    "ValidationReport",
]

#: Version of the run-manifest shape produced by :meth:`RecipeRunner.plan`.
#: Values above 1 are Reserved in ``recipe-runner-lib.v1``.
RUN_MANIFEST_VERSION: Final[int] = 1


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class LockMode(str, Enum):
    """Lock semantics for a run (``recipe-dependency-manifest.v1`` Core 8).

    Locks are never updated silently on run: rewriting requires
    :attr:`UPDATE_LOCK` explicitly.
    """

    LOCKED = "locked"
    """Require exact lock entries. Default, and mandatory for CI."""

    UPDATE_LOCK = "update-lock"
    """Re-resolve and rewrite the lockfile, explicitly."""

    UNLOCKED = "unlocked"
    """Interactive only; the runner warns."""


class DependencyKind(str, Enum):
    """Dependency kinds permitted in v1 (manifest Core 2)."""

    BUNDLE = "bundle"
    BEHAVIOR = "behavior"


class RunStatus(str, Enum):
    """Terminal or suspended state of a run."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    """Suspended at an approval gate; ``resume`` continues it."""


# --------------------------------------------------------------------------
# Trust policy (caller-supplied input, not a host port)
# --------------------------------------------------------------------------


@runtime_checkable
class TrustPolicy(Protocol):
    """Decides which dependency sources may be fetched or activated.

    Contract lib Core 6: a trust policy is a *required input* for remote
    resolution -- arbitrary explicit URIs are permitted only with one, and CI
    mode requires locked immutable refs. Enforcement happens in preflight,
    before any fetch or module activation.

    A policy is a caller-supplied input carried on :class:`RunRequest`; it is
    not one of the five host ports.
    """

    @property
    def name(self) -> str:
        """Stable identifier recorded in run provenance."""
        ...

    def check_source(self, source: str, *, locked_ref: str | None = None) -> None:
        """Return ``None`` if the source is permitted.

        Raise :class:`~amplifier_recipe_runner.errors.TrustRefusedError`
        otherwise -- refusal is a real result, never a silent skip.
        """
        ...


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Everything a host supplies to ``validate`` / ``plan`` / ``run``.

    ``services`` is optional for ``validate`` and ``plan`` -- both are
    side-effect free and must work with no host wiring at all -- and required
    for ``run``.
    """

    recipe: str | Path
    """Recipe source: a filesystem path or a Foundation-resolvable URI."""

    context: Mapping[str, Any] = field(default_factory=dict)
    """Recipe context variables."""

    services: HostServices | None = None
    """The five host ports. Required for ``run``/``resume``."""

    trust_policy: TrustPolicy | None = None
    """Required for remote resolution (lib Core 6)."""

    lock_mode: LockMode = LockMode.LOCKED
    """Defaults to the CI-safe mode; relaxing it is explicit."""

    run_id: str | None = None
    """Caller-chosen run identifier; generated when omitted."""

    legacy_mode: bool = False
    """Labeled caller-bound legacy mode (manifest Core 10).

    Only the embedded Amplifier tool adapter may set this, and only with a
    deprecation warning. The standalone runner leaves it ``False``, so a legacy
    recipe raises
    :class:`~amplifier_recipe_runner.errors.LegacyRecipeError`.
    """


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One problem found by ``validate``, with the remedy attached."""

    code: str
    message: str
    location: str | None = None
    remedy: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Result of ``validate``: manifest + plan checks, no side effects."""

    ok: bool
    schema_version: int | None = None
    legacy: bool = False
    """True when the recipe has no ``schema_version: 2`` manifest."""

    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()


# --------------------------------------------------------------------------
# Plan (the run manifest -- lib Core 7)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedDependency:
    """One declared dependency, resolved to an immutable identity."""

    uri: str
    """Canonical source URI as declared."""

    kind: DependencyKind
    requested_ref: str | None = None
    resolved_revision: str | None = None
    """Immutable revision (e.g. commit sha) actually resolved."""

    content_digest: str | None = None
    subdirectory: str | None = None
    """Set for behavior partials declared via ``#subdirectory=``."""

    required_agents: tuple[str, ...] = ()
    version: str | None = None
    local_path: str | None = None
    """Where the resolver placed (or found) this dependency on disk.

    Part of the Core 7 provenance record: the declared URI says what was
    asked for, this says what was actually read.
    """

    namespace: str | None = None
    """Bundle namespace this dependency contributes agents under."""


@dataclass(frozen=True, slots=True)
class AgentProvenance:
    """Which dependency supplies an agent, recorded per run (manifest Core 7)."""

    agent: str
    """Canonical ``namespace:name``."""

    supplied_by: str
    """URI of the supplying dependency, or ``"runner-baseline"``."""

    dependency_digest: str | None = None
    alias: str | None = None
    """Alias used in the recipe, when the step referenced one."""

    local_path: str | None = None
    """Resolved local path of the agent definition, or of its supplying
    dependency when the agent has no standalone file (manifest Core 7)."""

    resolved_revision: str | None = None
    """Immutable revision of the supplying dependency, for git sources.

    ``None`` for local file/path sources -- those record
    :attr:`dependency_digest` (a content digest) instead.
    """

    defined_in: str | None = None
    """The source tree that actually holds this agent's definition, when that
    is NOT :attr:`supplied_by`'s own tree.

    A declared dependency may *reach* an agent through its own ``includes``
    without defining it. :attr:`supplied_by` still names the declared
    dependency -- that is the source the recipe asked for, and the one a
    resume re-resolves -- but stamping only that would claim the agent lives
    in a tree it does not, which makes the Core 7 map non-discriminating.
    ``None`` means the agent is defined inside :attr:`supplied_by`'s own tree.
    """

    via_includes: bool = False
    """True when :attr:`supplied_by` supplies this agent *transitively*.

    Always paired with :attr:`defined_in`; never set for an agent the declared
    dependency defines itself.
    """


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """Policy actually in force, after intersection (manifest Core 9)."""

    lock_mode: LockMode
    trust_policy: str | None = None
    capabilities: tuple[str, ...] = ()
    """host policy ∩ runner policy ∩ manifest-declared needs."""

    isolated: bool = True
    """False only in labeled caller-bound legacy mode."""


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """The resolved graph: what ``run`` *would* do, without doing any of it.

    This is the stable, documented run-manifest shape required by lib Core 7.
    """

    recipe_digest: str
    schema_version: int
    dependencies: tuple[ResolvedDependency, ...] = ()
    agents: Mapping[str, AgentProvenance] = field(default_factory=dict)
    """Agent name -> provenance, covering every agent any step references."""

    step_ids: tuple[str, ...] = ()
    policy: EffectivePolicy | None = None
    runner_version: str | None = None
    foundation_version: str | None = None
    manifest_version: int = RUN_MANIFEST_VERSION


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of ``run`` / ``resume``.

    A refused dependency or missing artifact surfaces here as a non-success
    status with ``error`` set -- never as a fabricated success (lib Core 8).
    """

    run_id: str
    status: RunStatus
    plan: ExecutionPlan | None = None
    outputs: Mapping[str, Any] = field(default_factory=dict)
    completed_steps: tuple[str, ...] = ()
    error: BaseException | None = None
    pending_approval: str | None = None
    """Stage awaiting approval when ``status`` is :attr:`RunStatus.PAUSED`."""

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.SUCCEEDED


# --------------------------------------------------------------------------
# Neutral execution session (lib Core 3)
# --------------------------------------------------------------------------


@runtime_checkable
class ExecutionSession(Protocol):
    """The library's own execution-session abstraction.

    Deliberately *not* Amplifier's coordinator: hosts and recipes see this
    protocol and nothing of Amplifier's internals. Its agent surface is built
    exclusively from the declared dependency closure plus runner baseline
    (manifest Core 3, Core 4), which is why there is no method to install,
    inject, or import agents from a caller.
    """

    @property
    def run_id(self) -> str: ...

    @property
    def workspace(self) -> WorkspacePath: ...

    def available_agents(self) -> Sequence[str]:
        """Agents resolved from the plan's closure. Read-only, closed-world."""
        ...

    async def invoke(
        self,
        agent: str,
        instruction: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> str:
        """Run one agent from the closure.

        Raise :class:`~amplifier_recipe_runner.errors.UndeclaredAgentError`
        if ``agent`` is outside it.
        """
        ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------


@runtime_checkable
class RecipeRunner(Protocol):
    """The library's public entry point (lib Core 2).

    Every method is usable without a UI and without the Amplifier CLI.
    ``validate`` and ``plan`` have no side effects: no fetch, no activation,
    no step execution.
    """

    async def validate(self, request: RunRequest) -> ValidationReport:
        """Check manifest and plan. Never fetches, activates, or executes."""
        ...

    async def plan(self, request: RunRequest) -> ExecutionPlan:
        """Resolve the dependency closure and agent provenance; execute nothing.

        Preflight failures raise typed
        :class:`~amplifier_recipe_runner.errors.PreflightError` subclasses
        before any side effect.
        """
        ...

    async def run(self, request: RunRequest) -> RunResult:
        """Preflight, then execute. Requires ``request.services``."""
        ...

    async def resume(self, run_id: str, services: HostServices) -> RunResult:
        """Continue a paused or interrupted run from recorded provenance.

        A provenance mismatch raises
        :class:`~amplifier_recipe_runner.errors.ProvenanceMismatchError`
        rather than silently re-resolving (manifest Core 8).
        """
        ...
