"""In-session v2 execution: the proven step engine, a closed-world agent map.

Contracts:

* ``recipe-dependency-manifest.v1`` **Core 3/4/5** -- a schema-v2 recipe's
  ``agent:`` references resolve from its own declared closure, never from the
  calling session's agent map, and a colliding caller agent cannot alter that
  resolution.
* ``recipe-runner-lib.v1`` **Core 1/2** -- manifest parsing, dependency
  resolution, provenance and the closed-world catalog stay in the library.
  Nothing here resolves a dependency, parses a manifest, or invents an agent.

Why this module exists
----------------------
The library's own sequential executor runs *agent* steps. Real recipes are
mostly not agent steps: ``repo-audit.yaml`` is 30 steps of which 3 are agent
steps -- the rest are ``bash``, ``parse_json``, ``foreach``, conditions and
staged approvals. Routing them through the library executor raised
``UnsupportedStepError`` at step 1, so no real v2 recipe could run in-session
(recipes-lc7).

The step vocabulary and the agent catalog are two different things, and only
the second is what the contracts constrain. So in-session v2 execution runs on
the **legacy step engine** (:class:`~amplifier_module_tool_recipes.executor.RecipeExecutor`,
full vocabulary, years of use) with its agent catalog **replaced** by the
catalog the library's ``plan()`` resolved:

    plan()  ->  ClosedWorldAgentCatalog  ->  RecipeExecutor(ClosedWorldCoordinator)

:class:`ClosedWorldCoordinator` is a view of the caller's coordinator with one
thing changed and one thing wrapped:

* ``config["agents"]`` **is** the plan catalog -- the legacy executor reads
  that key (``executor.py``: ``self.coordinator.config.get("agents", {})``)
  for agent-level provider preferences, so it must see the recipe's agents and
  only those.
* ``session.spawn`` is wrapped by :class:`ClosedWorldSpawn`, which refuses a
  name outside the catalog with the library's own ``UndeclaredAgentError`` and
  passes ``agent_configs=<the catalog>`` -- never the caller's map -- to the
  host's real spawn function.

Everything else (providers, tools, hooks, approvals, display, cancellation)
still comes from the caller's session through the host's own spawn machinery,
which is exactly how a spawned child has always obtained providers. That is
what makes an in-session v2 agent step do real model work instead of reporting
"No providers available" (recipes-30w).

One honest boundary
-------------------
The host's spawn capability composes the child session from the *parent*
session (that is what supplies providers) and, in Amplifier's app layer, also
seeds the child's own delegate roster from the parent coordinator's live agent
map. That is host policy about what a *child* may delegate to next; it is not
this recipe's step resolution. Every ``agent:`` a step of this recipe names is
resolved here, against the plan catalog, before any spawn happens.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "V2_LEGACY_ENGINE_EXECUTION_MODE",
    "ClosedWorldAgentCatalog",
    "ClosedWorldCatalogError",
    "ClosedWorldCoordinator",
    "ClosedWorldSpawn",
    "SPAWN_CAPABILITY",
    "agent_provenance_record",
    "build_catalog",
    "host_coordinator_of",
]

#: Execution mode label for a schema-v2 recipe executed in-session: the
#: recipe's own closed-world agent catalog, run on the legacy step engine.
#: Deliberately distinct from ``runner-isolated`` (the library's own executor,
#: which the standalone CLI uses) -- two different engines must never wear one
#: label.
V2_LEGACY_ENGINE_EXECUTION_MODE = "v2-closed-world-legacy-engine"

#: The capability name Amplifier hosts register agent spawning under.
SPAWN_CAPABILITY = "session.spawn"

#: Keys an agent's own definition file may contribute to its spawn overlay.
#: Exactly the set Foundation's agent loading produces; nothing is synthesized.
_OVERLAY_KEYS = (
    "tools",
    "providers",
    "hooks",
    "session",
    "provider_preferences",
    "model_role",
    "agents",
)


class ClosedWorldCatalogError(RuntimeError):
    """An agent in the plan closure has no usable definition to run.

    Raised rather than spawning with an empty overlay: an agent whose
    definition could not be loaded would run as a *generic* session while the
    recipe reported success -- the fabricated success ``recipe-runner-lib.v1``
    Core 8 forbids.
    """

    def __init__(self, agent: str, reason: str) -> None:
        self.agent = agent
        self.reason = reason
        super().__init__(
            f"Agent {agent!r} is in this recipe's declared closure, but its "
            f"definition could not be loaded: {reason}. The run was refused "
            "rather than executed with an empty agent definition."
        )


# ---------------------------------------------------------------------------
# Catalog construction (from the library's plan, never from the caller)
# ---------------------------------------------------------------------------


def _load_agent_config(canonical: str, local_path: str | None) -> dict[str, Any]:
    """One agent's config, loaded from its resolved definition file.

    Uses Foundation's own agent loading (``Bundle.load_agent_metadata``) so the
    dict has exactly the shape ``session.spawn`` expects of an entry in
    ``agent_configs``: ``name``/``description``/``instruction`` plus whatever
    mount-plan sections (``tools``, ``providers``, ``session``, ...) the agent
    file declares.

    A plan agent whose ``local_path`` is its supplying dependency's root rather
    than a standalone file (``AgentProvenance.local_path`` documents both
    cases) contributes what is known and nothing invented.
    """
    simple = canonical.split(":", 1)[-1]
    namespace = canonical.split(":", 1)[0] if ":" in canonical else ""
    config: dict[str, Any] = {"name": simple}

    path = Path(local_path) if local_path else None
    if path is None or path.suffix != ".md" or not path.is_file():
        logger.warning(
            "Agent %r has no standalone definition file (local_path=%r); its "
            "spawn overlay carries only its name.",
            canonical,
            local_path,
        )
        return config

    loaded = _foundation_agent_metadata(canonical, simple, namespace, path)
    for key, value in loaded.items():
        if value not in (None, "", [], {}):
            config[key] = value
    config["name"] = loaded.get("name") or simple
    config["local_path"] = str(path)
    return config


def _foundation_agent_metadata(
    canonical: str, simple: str, namespace: str, path: Path
) -> Mapping[str, Any]:
    """Foundation's own reading of an agent ``.md`` file.

    Primary route: build a one-agent :class:`Bundle` rooted where the file
    lives and let Foundation's ``load_agent_metadata`` fill it in -- the same
    call the resolver and every host make.

    Fallback: the file is not under an ``agents/`` directory, so Foundation's
    path convention cannot reach it. Its frontmatter is then read with
    Foundation's own parser, producing the same keys. The fallback is logged;
    it is never silent.
    """
    try:
        from amplifier_foundation import Bundle  # noqa: PLC0415 -- lazy by design
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ClosedWorldCatalogError(canonical, f"amplifier-foundation is not importable: {exc}") from exc

    bundle = Bundle(name=namespace or simple, agents={simple: {}}, base_path=path.parent.parent)
    resolved = bundle.resolve_agent_path(simple)
    if resolved is not None and Path(resolved) == path:
        bundle.load_agent_metadata()
        loaded = bundle.agents.get(simple) or {}
        if loaded:
            return dict(loaded)

    logger.info(
        "Agent %r at %s is not under Foundation's `agents/` convention; reading "
        "its frontmatter directly.",
        canonical,
        path,
    )
    return _frontmatter_agent_metadata(canonical, simple, path)


def _frontmatter_agent_metadata(canonical: str, simple: str, path: Path) -> dict[str, Any]:
    try:
        from amplifier_foundation import parse_frontmatter  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ClosedWorldCatalogError(canonical, f"amplifier-foundation is not importable: {exc}") from exc

    try:
        frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - an unreadable definition is a real failure
        raise ClosedWorldCatalogError(canonical, f"{type(exc).__name__}: {exc}") from exc

    meta = frontmatter.get("meta") if isinstance(frontmatter.get("meta"), Mapping) else frontmatter
    meta = meta if isinstance(meta, Mapping) else {}
    config: dict[str, Any] = {
        "name": meta.get("name", simple),
        "description": meta.get("description", ""),
    }
    for key in _OVERLAY_KEYS:
        if key in frontmatter:
            config[key] = frontmatter[key]
    if body and body.strip():
        config["instruction"] = body.strip()
    return config


class ClosedWorldAgentCatalog:
    """The whole agent surface of one v2 run: the plan's closure, frozen.

    Built once by :meth:`from_plan` from ``ExecutionPlan.agents`` -- which the
    library populated exclusively from declared dependencies. There is no
    method to add an agent, and no constructor argument that could carry a
    caller's map (manifest Core 3/4).
    """

    __slots__ = ("_configs", "_provenance")

    def __init__(
        self,
        configs: Mapping[str, Mapping[str, Any]],
        provenance: Mapping[str, Any],
    ) -> None:
        self._configs = {name: dict(config) for name, config in configs.items()}
        self._provenance = dict(provenance)

    @classmethod
    def from_plan(cls, plan: Any) -> ClosedWorldAgentCatalog:
        """Load every agent the plan resolved, from its own resolved path."""
        configs: dict[str, dict[str, Any]] = {}
        for reference, provenance in plan.agents.items():
            configs[reference] = _load_agent_config(provenance.agent, provenance.local_path)
        return cls(configs, dict(plan.agents))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._configs))

    def __contains__(self, reference: object) -> bool:
        return reference in self._configs

    def __len__(self) -> int:
        return len(self._configs)

    def agent_configs(self) -> dict[str, dict[str, Any]]:
        """The **entire** agent map this run has, as ``session.spawn`` wants it.

        Named to mirror the ``agent_configs`` argument the spawn capability
        carries, so the substitution is obvious at the call site: the run uses
        *this*, never the host's.
        """
        return {name: dict(config) for name, config in self._configs.items()}

    def resolve(self, reference: str, *, step_id: str | None = None) -> Any:
        """Provenance for ``reference``, or refuse it.

        Raises:
            UndeclaredAgentError: ``reference`` is outside the closure. The
                caller's agent map is never consulted -- there is no code path
                here that could consult one.
        """
        provenance = self._provenance.get(reference)
        if provenance is not None:
            return provenance

        from .runner_adapter import load_runner  # noqa: PLC0415 -- lazy import

        runner = load_runner()
        raise runner.UndeclaredAgentError(
            reference,
            step_id=step_id,
            declared_agents=self.names,
            remedy=(
                f"This run's agents are exactly {', '.join(self.names) or 'none'} -- "
                "resolved from the recipe's declared dependency closure. Declare a "
                f"dependency supplying {reference!r} in the recipe's `dependencies` "
                "block. The calling session's agents never satisfy a reference."
            ),
        )


def build_catalog(plan: Any) -> ClosedWorldAgentCatalog:
    """The plan's closure as a spawn-ready catalog (lib Core 1: plan owns it)."""
    return ClosedWorldAgentCatalog.from_plan(plan)


def agent_provenance_record(plan: Any, *, run_id: str = "") -> dict[str, Any]:
    """The plan's dependency identity and per-agent provenance, as plain data.

    Persisted beside the recipe session so a later reader can compare this
    run's identity against ``recipe-runner plan --json`` on any other surface
    (lib Core 7: two hosts must produce the same provenance for the same
    locked recipe).

    Deliberately **not** hand-rolled here. This delegates to the library's own
    :func:`~amplifier_recipe_runner.provenance.run_manifest_from_plan` and its
    ``to_mapping()`` -- the identical call the standalone CLI's
    ``plan --json`` makes (``cli.py``'s ``_plan_mapping``). So the two surfaces
    are comparable key for key rather than approximately: a second, similar
    serializer here would drift, and a drifted field looks exactly like a
    genuine identity mismatch. ``defined_in``/``via_includes`` ride along
    because that mapping already carries them (PR #86) -- an agent reached
    through a dependency's own includes is defined somewhere other than the
    dependency that supplied it.

    Two fields are *recorded but not comparable* by construction: ``run_id``
    (this run's own) and ``created_at`` (a timestamp, which
    :class:`~amplifier_recipe_runner.provenance.RunManifest` documents as
    "recorded, never compared").
    """
    from .runner_adapter import load_runner  # noqa: PLC0415 -- lazy import

    load_runner()  # puts the library on sys.path if it is not already
    from amplifier_recipe_runner.provenance import run_manifest_from_plan  # noqa: PLC0415

    return run_manifest_from_plan(plan, run_id=run_id).to_mapping()


# ---------------------------------------------------------------------------
# The spawn wrapper and the coordinator view
# ---------------------------------------------------------------------------


class ClosedWorldSpawn:
    """The host's own spawn, with the recipe's catalog as its only agent map.

    Resolution happens *here*, before the host is called, so a name outside the
    closure never reaches a spawn at all. What the host receives is the
    catalog; what it offered is recorded on :attr:`ignored_host_agents` and
    discarded (manifest Core 5 -- ignoring it silently would be
    indistinguishable from honouring it).
    """

    __slots__ = ("_catalog", "_host_spawn", "_ignored_arguments", "_ignored_host_agents")

    def __init__(self, host_spawn: Any, catalog: ClosedWorldAgentCatalog) -> None:
        self._host_spawn = host_spawn
        self._catalog = catalog
        self._ignored_host_agents: list[str] = []
        self._ignored_arguments: list[str] = []

    @property
    def catalog(self) -> ClosedWorldAgentCatalog:
        return self._catalog

    @property
    def ignored_host_agents(self) -> tuple[str, ...]:
        """Every agent name a caller offered and this wrapper refused to use."""
        return tuple(self._ignored_host_agents)

    @property
    def ignored_arguments(self) -> tuple[str, ...]:
        return tuple(self._ignored_arguments)

    async def __call__(
        self,
        agent_name: str,
        instruction: str,
        parent_session: Any = None,
        agent_configs: Mapping[str, Any] | None = None,
        **host_arguments: Any,
    ) -> Any:
        step_id = None
        metadata = host_arguments.get("session_metadata")
        if isinstance(metadata, Mapping):
            step_id = metadata.get("recipe_step")

        self._catalog.resolve(agent_name, step_id=step_id)

        if agent_configs:
            self._ignored_host_agents.extend(str(name) for name in agent_configs)
            self._ignored_arguments.append("agent_configs")

        # The catalog carries each agent's definition file verbatim -- including
        # a `provider_preferences` block written in provider MODULE names, which
        # the host merges into the child's session config and the child's own
        # routing re-assert then re-pins priority from (hooks-routing
        # `role_pin._declared_pins`). This wrapper is the last hop that decides
        # the outgoing overlay in a v2 run -- the engine's own aligned map is
        # discarded above, by design -- so the alignment is applied here too,
        # against the preference chain this very spawn is promoting. See
        # `executor.align_overlay_preferences` for the measured defect.
        from .executor import align_overlay_preferences  # noqa: PLC0415 -- lazy by design

        outgoing_agents = align_overlay_preferences(
            self._catalog.agent_configs(),
            agent_name,
            host_arguments.get("provider_preferences"),
        )

        return await self._host_spawn(
            agent_name=agent_name,
            instruction=instruction,
            parent_session=parent_session,
            agent_configs=outgoing_agents,
            **host_arguments,
        )


class ClosedWorldCoordinator:
    """The caller's coordinator, with the recipe's catalog in place of its agents.

    Everything the step engine needs from a coordinator -- providers, hooks,
    display, working directory, capabilities -- still comes from the caller.
    Exactly two things differ, and both are the point:

    * :attr:`config` is a *copy* whose ``agents`` key is the plan catalog. The
      caller's own map is not reachable through it.
    * ``get_capability("session.spawn")`` returns :class:`ClosedWorldSpawn`.
    """

    def __init__(self, coordinator: Any, catalog: ClosedWorldAgentCatalog) -> None:
        self._coordinator = coordinator
        self._catalog = catalog
        self._spawn: ClosedWorldSpawn | None = None
        host_config = getattr(coordinator, "config", None)
        base = dict(host_config) if isinstance(host_config, Mapping) else {}
        base["agents"] = catalog.agent_configs()
        self.config: dict[str, Any] = base

    @property
    def catalog(self) -> ClosedWorldAgentCatalog:
        return self._catalog

    @property
    def spawn(self) -> ClosedWorldSpawn | None:
        """The wrapper actually handed to the engine, once it asked for one."""
        return self._spawn

    def get_capability(self, name: str) -> Any:
        capability = self._coordinator.get_capability(name)
        if name != SPAWN_CAPABILITY or capability is None:
            return capability
        if self._spawn is None or self._spawn._host_spawn is not capability:
            self._spawn = ClosedWorldSpawn(capability, self._catalog)
        return self._spawn

    def register_capability(self, name: str, value: Any) -> Any:
        return self._coordinator.register_capability(name, value)

    def get(self, name: str) -> Any:
        getter = getattr(self._coordinator, "get", None)
        return getter(name) if callable(getter) else None

    @property
    def host_coordinator(self) -> Any:
        """The coordinator this view was built over -- one layer down."""
        return self._coordinator

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes this view does not define itself, so
        # `config` and the capability methods above always win.
        return getattr(self._coordinator, name)


def host_coordinator_of(coordinator: Any) -> Any:
    """The real host coordinator underneath any number of scoped views.

    A v2 sub-recipe of a v2 parent must be scoped over the *host*, not over
    the parent's scope. Nesting the views would leave the parent's
    :class:`ClosedWorldSpawn` innermost, so it -- not the sub-recipe's own
    catalog -- would decide which agent names are admissible, and every agent
    the sub-recipe declared but the parent did not would be refused. Each
    recipe's closure is its own (manifest Core 3/4); it is neither inherited
    nor intersected.

    A plain host coordinator is returned unchanged, so this is safe to call
    unconditionally.
    """
    seen: list[int] = []
    while isinstance(coordinator, ClosedWorldCoordinator):
        # Defensive: a self-referential view would otherwise spin forever.
        if id(coordinator) in seen:  # pragma: no cover - not constructible today
            break
        seen.append(id(coordinator))
        coordinator = coordinator.host_coordinator
    return coordinator
