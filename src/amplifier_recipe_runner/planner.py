"""Dependency planning: manifest in, resolved :class:`ExecutionPlan` out.

Contracts: ``recipe-dependency-manifest.v1`` Core 3, 5, 6, 7 (the planning
half) and ``recipe-runner-lib.v1`` Core 5, 7.

:func:`plan` answers one question -- *what would this recipe run with?* -- and
nothing else:

* **Closed-world closure (manifest Core 3).** The agent catalog is built
  exclusively from declared dependencies. There is no parameter, no fallback,
  and no code path by which a caller's ambient agents could enter it.
* **Collision is an error (manifest Core 5).** See "Reading of Core 5" below.
* **Undeclared reference fails, naming the remedy (manifest Core 6).** A step
  referencing an agent outside the closure raises
  :class:`~amplifier_recipe_runner.errors.UndeclaredAgentError`.
* **Provenance per agent (manifest Core 7).** Every agent in the plan records
  its supplying dependency's declared source, the resolved local path, and --
  for git sources -- the resolved immutable revision. Local sources record a
  content digest instead, because they have no revision to record.
* **No namespace inference (manifest Core 11).** The resolver is asked for
  exactly the declared sources, in declaration order, and never for a source
  guessed from an agent name's namespace.

**Planning performs no execution and activates no modules.** It reads bundle
definitions through the injected resolver (lib Core 5) and computes. Nothing
is mounted, no session is created, no step runs.

Reading of Core 5 (deliberate, not an oversight)
------------------------------------------------
"Duplicate agent names across the dependency closure are a preflight ERROR."
An agent's name in the closed world is its canonical ``namespace:name`` -- that
is what a step references (Core 3), and that is what silently overrides during
bundle composition, where a later bundle's agent map wins. So:

* Two dependencies supplying the **same canonical name** -> always an error,
  raised during closure construction. The single exception is the same agent
  arriving twice via a shared include -- identical source path *and* digest --
  which is one agent, not two, and is recorded once.
* Two dependencies supplying the **same bare name in different namespaces**
  (``a:reviewer`` and ``b:reviewer``) is not an override, so it is not an
  error by itself. It becomes one the moment a step references the bare name
  ``reviewer``: that reference is ambiguous and is raised as a collision
  naming both sources. It is never resolved by precedence, and never guessed.

Trust policy
------------
Enforcement belongs to the trust-policy work (``trust.py``), not here. What
lives here is the *hook point* it needs: when a ``trust_policy`` is passed,
``check_source`` is called for every declared source **before any source
reaches the resolver** -- so a refusal on the last dependency still cannot
follow a fetch of the first (manifest Core 6, lib Core 6).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from typing import Final

import yaml

from .api import AgentProvenance
from .api import DependencyKind
from .api import EffectivePolicy
from .api import ExecutionPlan
from .api import LockMode
from .api import ResolvedDependency
from .api import TrustPolicy
from .errors import AgentCollisionError
from .errors import LegacyRecipeError
from .errors import UndeclaredAgentError
from .manifest import LegacyRecipe
from .manifest import Manifest
from .ports import WorkspacePath
from .resolver import DependencyResolver
from .resolver import ResolvedAgent
from .resolver import ResolvedBundle

__all__ = [
    "CONTRACTS",
    "AgentCatalog",
    "plan",
]

CONTRACTS: Final[tuple[str, ...]] = (
    "recipe-dependency-manifest.v1",
    "recipe-runner-lib.v1",
)

#: Keys under which a step may nest further steps. ``while_steps`` carries the
#: body of both ``foreach`` and ``while`` containers.
_NESTED_STEP_KEYS: Final[tuple[str, ...]] = ("steps", "while_steps")


@dataclass(frozen=True, slots=True)
class _CatalogEntry:
    """One agent in the closure, with the dependency that supplied it."""

    agent: ResolvedAgent
    dependency: ResolvedBundle

    @property
    def identity(self) -> tuple[str | None, str | None, str | None]:
        """What makes two arrivals of a name *the same agent*, not a conflict."""
        return (
            self.agent.local_path,
            self.dependency.resolved_revision,
            self.dependency.content_digest,
        )


class AgentCatalog:
    """The closed-world agent catalog built from declared dependencies.

    Read-only by construction: it is populated once, during planning, from
    resolver results. There is deliberately no method to add an agent from a
    caller session (manifest Core 3, Core 4).
    """

    def __init__(self) -> None:
        self._by_canonical: dict[str, _CatalogEntry] = {}
        self._by_bare: dict[str, list[str]] = {}

    def __contains__(self, canonical: str) -> bool:
        return canonical in self._by_canonical

    def __len__(self) -> int:
        return len(self._by_canonical)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_canonical))

    def entries(self) -> Iterator[tuple[str, _CatalogEntry]]:
        yield from sorted(self._by_canonical.items())

    def add(self, agent: ResolvedAgent, dependency: ResolvedBundle) -> None:
        """Add one agent, refusing a genuine duplicate (manifest Core 5)."""
        canonical = agent.name
        existing = self._by_canonical.get(canonical)
        candidate = _CatalogEntry(agent=agent, dependency=dependency)

        if existing is not None:
            if existing.dependency.source == dependency.source or existing.identity == candidate.identity:
                # Same agent reached twice (a shared include, or the same
                # dependency listed once and resolved once). One agent.
                return
            raise AgentCollisionError(
                canonical,
                sources=(existing.dependency.source, dependency.source),
            )

        self._by_canonical[canonical] = candidate
        bare = canonical.split(":", 1)[-1]
        self._by_bare.setdefault(bare, []).append(canonical)

    def entry(self, canonical: str) -> _CatalogEntry | None:
        return self._by_canonical.get(canonical)

    def resolve_reference(self, reference: str) -> _CatalogEntry | None:
        """Resolve a canonical or bare reference. Ambiguity is an error.

        Returns ``None`` when nothing in the closure supplies ``reference`` --
        the caller turns that into
        :class:`~amplifier_recipe_runner.errors.UndeclaredAgentError` with the
        step that referenced it.
        """
        direct = self._by_canonical.get(reference)
        if direct is not None:
            return direct
        if ":" in reference:
            return None

        candidates = self._by_bare.get(reference, [])
        if not candidates:
            return None
        if len(candidates) > 1:
            sources = tuple(sorted({self._by_canonical[c].dependency.source for c in candidates}))
            raise AgentCollisionError(
                reference,
                sources=sources,
                remedy=(
                    f"Reference one of {', '.join(sorted(candidates))} by its canonical "
                    "'namespace:name', or declare an alias in the recipe's `agents` map. "
                    "A bare name supplied by more than one dependency is never resolved "
                    "by precedence."
                ),
            )
        return self._by_canonical[candidates[0]]


# --------------------------------------------------------------------------
# The planner
# --------------------------------------------------------------------------


async def plan(
    manifest: Manifest,
    resolver: DependencyResolver,
    workspace: WorkspacePath | Path | None = None,
    *,
    recipe: Mapping[str, Any] | str | Path | None = None,
    trust_policy: TrustPolicy | None = None,
    lock_mode: LockMode = LockMode.LOCKED,
) -> ExecutionPlan:
    """Resolve ``manifest``'s dependency closure into an :class:`ExecutionPlan`.

    Args:
        manifest: A parsed ``schema_version: 2`` manifest. A
            :class:`~amplifier_recipe_runner.manifest.LegacyRecipe` is refused
            here -- legacy recipes run only through the labeled caller-bound
            adapter (manifest Core 10).
        resolver: The injected resolver (lib Core 5). The planner never builds
            a registry itself.
        workspace: Base directory for relative local sources, and the only
            filesystem location a later ``run`` is entitled to touch.
        recipe: The recipe body -- an already-loaded mapping, or a path to the
            recipe file. When omitted, ``manifest.source`` is read if it points
            at an existing file. Steps supply the agent references checked
            against the closure and the ``step_ids`` recorded in the plan.
        trust_policy: When supplied, ``check_source`` is called for every
            declared source *before* it reaches the resolver.
        lock_mode: Recorded in the plan's effective policy.

    Returns:
        An :class:`ExecutionPlan`: the resolved graph, with per-agent
        provenance. Nothing has been executed and no module has been activated.

    Raises:
        LegacyRecipeError: ``manifest`` is a legacy recipe.
        AgentCollisionError: two dependencies supply the same agent name, or a
            step's bare reference is ambiguous (manifest Core 5).
        UndeclaredAgentError: a step (or a dependency's ``required_agents``)
            references an agent no declared dependency supplies (Core 6).
        DependencyResolutionError: a declared source could not be read.
        TrustRefusedError: raised by ``trust_policy`` before any fetch.
    """
    if isinstance(manifest, LegacyRecipe):
        raise LegacyRecipeError(
            recipe=manifest.source or "<recipe>",
            reason=manifest.reason,
        )

    workspace_path = Path(workspace) if workspace is not None else None
    body, recipe_text = _load_recipe_body(recipe, manifest)

    catalog = AgentCatalog()
    resolved: list[ResolvedDependency] = []

    # Trust hook: EVERY declared source is checked before the resolution loop
    # begins, not one-by-one inside it. Checking in-loop would let dependency
    # one be fetched before dependency two is refused -- a side effect ahead of
    # a refusal, which is exactly what manifest Core 6 / lib Core 6 forbid.
    if trust_policy is not None:
        for dependency in manifest.dependencies:
            trust_policy.check_source(dependency.source)

    for dependency in manifest.dependencies:
        bundle = await resolver.resolve(dependency, workspace=workspace_path)

        for agent in bundle.agents.values():
            catalog.add(agent, bundle)

        _check_required_agents(dependency.required_agents, bundle, catalog)
        resolved.append(_record_dependency(dependency, bundle))

    step_ids, references = _walk_recipe(body)
    provenance = _build_provenance(catalog, references, manifest.agents)

    return ExecutionPlan(
        recipe_digest=_recipe_digest(recipe_text, body, manifest),
        schema_version=manifest.schema_version,
        dependencies=tuple(resolved),
        agents=MappingProxyType(provenance),
        step_ids=step_ids,
        policy=EffectivePolicy(
            lock_mode=lock_mode,
            trust_policy=getattr(trust_policy, "name", None),
            capabilities=(),
            isolated=True,
        ),
        runner_version=_runner_version(),
        foundation_version=_foundation_version(),
    )


# --------------------------------------------------------------------------
# Closure -> plan records
# --------------------------------------------------------------------------


def _record_dependency(dependency: Any, bundle: ResolvedBundle) -> ResolvedDependency:
    return ResolvedDependency(
        uri=dependency.source,
        kind=DependencyKind(dependency.kind),
        requested_ref=bundle.requested_ref,
        resolved_revision=bundle.resolved_revision,
        content_digest=bundle.content_digest,
        subdirectory=bundle.subdirectory,
        required_agents=tuple(dependency.required_agents),
        version=bundle.version,
        local_path=bundle.local_path,
        namespace=bundle.namespace or None,
    )


def _check_required_agents(
    required: tuple[str, ...],
    bundle: ResolvedBundle,
    catalog: AgentCatalog,
) -> None:
    """A declared ``required_agents`` entry the dependency does not supply is
    a preflight failure, not a warning (manifest Core 2 + Core 6)."""
    for name in required:
        canonical = name if ":" in name else f"{bundle.namespace}:{name}"
        if canonical in bundle.agents or name in bundle.agents:
            continue
        raise UndeclaredAgentError(
            name,
            declared_agents=catalog.names,
            remedy=(
                f"Dependency {bundle.source!r} lists {name!r} under `required_agents` "
                f"but supplies {', '.join(sorted(bundle.agents)) or 'no agents'}. "
                "Fix the declaration or the dependency's source."
            ),
        )


def _build_provenance(
    catalog: AgentCatalog,
    references: tuple[tuple[str, str | None], ...],
    aliases: Mapping[str, str],
) -> dict[str, AgentProvenance]:
    """Provenance for the whole closure, plus an entry per referenced alias.

    Keyed by canonical name for every agent the closure supplies (Core 3: only
    those), and additionally by alias for each alias a step actually used, so a
    host can look up either form.
    """
    provenance: dict[str, AgentProvenance] = {
        canonical: _provenance_for(entry, alias=None) for canonical, entry in catalog.entries()
    }

    for reference, step_id in references:
        canonical = aliases.get(reference, reference)
        entry = catalog.resolve_reference(canonical)
        if entry is None:
            raise UndeclaredAgentError(
                reference,
                step_id=step_id,
                declared_agents=catalog.names,
                remedy=(
                    f"Declare a dependency supplying {canonical!r} in the recipe's "
                    "`dependencies` block (and list it under `required_agents`). "
                    "A recipe's agents resolve only from its declared closure -- "
                    "never from the calling session."
                ),
            )
        if reference != entry.agent.name:
            provenance[reference] = _provenance_for(entry, alias=reference)

    return provenance


def _provenance_for(entry: _CatalogEntry, *, alias: str | None) -> AgentProvenance:
    dependency = entry.dependency
    return AgentProvenance(
        agent=entry.agent.name,
        supplied_by=dependency.source,
        dependency_digest=dependency.content_digest,
        alias=alias,
        local_path=entry.agent.local_path or dependency.local_path,
        resolved_revision=dependency.resolved_revision,
    )


# --------------------------------------------------------------------------
# Recipe body
# --------------------------------------------------------------------------


def _load_recipe_body(
    recipe: Mapping[str, Any] | str | Path | None,
    manifest: Manifest,
) -> tuple[Mapping[str, Any], str | None]:
    """Return ``(body, text)``. ``text`` is present only when a file was read."""
    if isinstance(recipe, Mapping):
        return recipe, None

    path: Path | None = None
    if isinstance(recipe, (str, Path)):
        path = Path(recipe)
    elif manifest.source:
        candidate = Path(manifest.source)
        path = candidate if candidate.is_file() else None

    if path is None:
        return {}, None
    if not path.is_file():
        # An explicitly passed path that does not exist is a caller error --
        # silently planning an empty step list would hide every undeclared
        # agent reference the recipe contains.
        raise FileNotFoundError(f"recipe file not found: {path}")

    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return (data if isinstance(data, Mapping) else {}), text


def _walk_recipe(body: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[tuple[str, str | None], ...]]:
    """Collect ``(step_ids, [(agent_reference, step_id)])`` from a recipe body.

    Walks flat steps, staged steps, and nested ``foreach``/``while`` bodies.
    A templated reference (``{{...}}``) cannot be resolved at plan time and is
    skipped here -- it is checked when the step runs.
    """
    step_ids: list[str] = []
    references: list[tuple[str, str | None]] = []

    def visit(steps: Any) -> None:
        if not isinstance(steps, list):
            return
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            step_id = step.get("id") if isinstance(step.get("id"), str) else None
            if step_id:
                step_ids.append(step_id)
            agent = step.get("agent")
            if isinstance(agent, str) and agent.strip() and "{{" not in agent:
                references.append((agent.strip(), step_id))
            for key in _NESTED_STEP_KEYS:
                visit(step.get(key))

    visit(body.get("steps"))
    stages = body.get("stages")
    if isinstance(stages, list):
        for stage in stages:
            if isinstance(stage, Mapping):
                visit(stage.get("steps"))

    return tuple(step_ids), tuple(references)


def _recipe_digest(text: str | None, body: Mapping[str, Any], manifest: Manifest) -> str:
    """Digest the recipe as read.

    Precedence: the file's exact bytes when a file was read; else a canonical
    JSON rendering of the body; else -- when there is no body at all -- the
    manifest's own declared facts. The digest always says what it covered, so
    a plan can never claim to have digested a recipe body it never saw.
    """
    if text is not None:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    if body:
        rendered = json.dumps(body, sort_keys=True, default=str)
        return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    rendered = json.dumps(
        {
            "schema_version": manifest.schema_version,
            "dependencies": [
                {"source": d.source, "kind": d.kind, "required_agents": list(d.required_agents)}
                for d in manifest.dependencies
            ],
            "agents": dict(manifest.agents),
        },
        sort_keys=True,
    )
    return "sha256:manifest-only:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Versions (recorded, never inferred)
# --------------------------------------------------------------------------


def _runner_version() -> str | None:
    from . import __version__

    return __version__


def _foundation_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("amplifier-foundation")
    except Exception:  # noqa: BLE001 - absent Foundation is a fact, not a failure
        return None
