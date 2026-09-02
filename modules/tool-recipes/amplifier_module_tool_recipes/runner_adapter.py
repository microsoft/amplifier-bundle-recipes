"""Thin adapter binding Amplifier's ``recipes`` tool to the runner library.

Contracts:

* ``recipe-runner-lib.v1`` **Core 1** -- one execution home. Schema-v2 recipes
  are executed by ``amplifier-recipe-runner``; this module carries no workflow,
  resolution, or agent-catalog logic of its own. It maps Amplifier's facilities
  onto the library's ports and translates the result back.
* ``recipe-runner-lib.v1`` **Core 4** -- host ports. There are exactly five
  (``provider_access``, ``approval_callback``, ``event_sink``, ``workspace``,
  ``cancellation``) and *none of them carries an agent map*.
* ``recipe-dependency-manifest.v1`` **Core 10** -- legacy mode is labeled and
  confined. A recipe with no ``schema_version`` keeps its existing caller-bound
  behavior **byte-identically**, labeled :data:`LEGACY_EXECUTION_MODE` and
  accompanied by a deprecation warning naming the remedy.

The defect this module exists to remove
---------------------------------------
The legacy executor hands ``coordinator.config["agents"]`` -- the *calling
session's* entire agent map -- to every spawn (``executor.py``,
``agent_configs=agents``). That is what makes a recipe's meaning depend on who
invoked it. The v2 path must never do this, so:

* :class:`~amplifier_recipe_runner.ports.HostServices` has no field that could
  carry it (the library's own structural guarantee), and
* :func:`build_host_services` and :func:`build_run_request` additionally *scan*
  what they are about to hand over and raise :class:`CallerAgentLeakError` if
  the caller's agent map is reachable through it.

The second check is redundant with the first by design. A silent leak and a
correct run are indistinguishable from the outside, so the leak is made loud.

Nothing here imports the runner library at module import time: the import is
lazy (:func:`load_runner`) so the ``recipes`` tool still mounts, and legacy
recipes still run, on an install that does not have the library yet.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import warnings
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from .session import ApprovalStatus

logger = logging.getLogger(__name__)

__all__ = [
    "ACCEPTED_CONFIG_KEYS",
    "LEGACY_DEPRECATION_REMEDY",
    "LEGACY_EXECUTION_MODE",
    "REJECTED_CONFIG_KEYS",
    "RUNNER_DISTRIBUTION",
    "RUNNER_IMPORT_NAME",
    "V2_EXECUTION_MODE",
    "MODEL_ROLE_RESOLVER_CAPABILITY",
    "PROVIDER_ROLES_FALLBACK",
    "PROVIDER_ROLES_RESOLVER",
    "SESSION_DEFAULT_ROLE",
    "AdapterConfigError",
    "CallerAgentLeakError",
    "CoordinatorEventSink",
    "CoordinatorProviderAccess",
    "RecipeRunnerUnavailableError",
    "SessionApprovalCallback",
    "ModelRoleUnavailableError",
    "SessionCancellationToken",
    "V2ResumeUnavailableError",
    "build_host_services",
    "build_run_request",
    "build_validate_request",
    "check_adapter_config",
    "check_model_roles",
    "declared_model_roles",
    "declared_schema_version",
    "execution_mode_of",
    "find_caller_agent_leak",
    "is_v2_recipe",
    "issue_for",
    "label_execution_mode",
    "legacy_deprecation_message",
    "library_resume",
    "load_runner",
    "manifest_header",
    "provider_roles_label",
    "resume_v2_recipe",
    "run_v2_recipe",
    "validate_v2_recipe",
    "warn_legacy_recipe",
]


# ---------------------------------------------------------------------------
# Labels (manifest.v1 Core 10)
# ---------------------------------------------------------------------------

#: Execution mode of a legacy recipe: agents resolve from the *caller's* map.
LEGACY_EXECUTION_MODE = "legacy-caller-bound"

#: Execution mode of a schema-v2 recipe: agents resolve from the recipe's own
#: declared dependency closure, through the runner library.
V2_EXECUTION_MODE = "runner-isolated"

LEGACY_DEPRECATION_REMEDY = (
    "Migrate the recipe to `schema_version: 2` with a `dependencies:` block so "
    "its agents resolve from its own declared closure instead of the calling "
    "session's agent map (see docs/RECIPE_SCHEMA.md, 'Recipe schema v2'). "
    "Legacy recipes run ONLY through this Amplifier tool adapter "
    "(recipe-dependency-manifest.v1 Core 10); the standalone recipe-runner CLI "
    "rejects them."
)

#: The duck-typed host capability that serves model roles.
MODEL_ROLE_RESOLVER_CAPABILITY = "model_role_resolver"

#: Role name the adapter synthesizes when the host registers no
#: :data:`MODEL_ROLE_RESOLVER_CAPABILITY`. It means exactly what it says: the
#: session's own default provider configuration, with no routing applied.
SESSION_DEFAULT_ROLE = "default"

#: Label for "roles came from the host's model_role_resolver capability".
PROVIDER_ROLES_RESOLVER = "model-role-resolver"

#: Label for "this host resolves no model roles, so the adapter served the
#: session default". Reported on the run's output and logged, never silent.
PROVIDER_ROLES_FALLBACK = "session-default-fallback"

#: Import name and distribution name of the one execution home (lib.v1 Core 1).
RUNNER_IMPORT_NAME = "amplifier_recipe_runner"
RUNNER_DISTRIBUTION = "amplifier-recipe-runner"


# ---------------------------------------------------------------------------
# Adapter configuration (manifest.v1 Core 12's spirit: never silently inert)
# ---------------------------------------------------------------------------

#: Every config key ``mount()`` actually reads. Anything else is refused.
ACCEPTED_CONFIG_KEYS = frozenset({"session_dir", "auto_cleanup_days"})

#: Keys that are refused with a *specific* reason rather than the generic
#: "not read" message, because the obvious reading of them is wrong rather
#: than merely unsupported.
REJECTED_CONFIG_KEYS: Mapping[str, str] = {
    "legacy_mode": (
        "Legacy mode is not a host setting -- it is decided by the recipe's own "
        "manifest (recipe-dependency-manifest.v1 Core 1): a recipe declaring "
        "`schema_version` runs in the runner library, one declaring none runs "
        f"caller-bound as {LEGACY_EXECUTION_MODE!r}. A host able to force "
        "legacy mode on could rebind a schema-v2 recipe's agents to the calling "
        "session while the run still reported success -- the exact silent "
        "failure schema v2 exists to end (Core 3). "
        "`amplifier_recipe_runner.RunRequest.legacy_mode` is therefore always "
        "False from this adapter, and legacy recipes never reach the library at "
        "all: they run on the frozen caller-bound path (Core 10). "
        "Remove this key; to run a recipe caller-bound, remove its "
        "`schema_version` and accept the deprecation warning."
    ),
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RecipeRunnerUnavailableError(ImportError):
    """The runner library is not importable, so no v2 recipe can run.

    Raised instead of silently falling back to the legacy caller-bound path:
    a v2 recipe run with caller-bound resolution would produce a *different*
    agent catalog while reporting success, which is precisely the silent
    failure schema v2 exists to end.
    """

    def __init__(self, cause: BaseException | None = None) -> None:
        self.cause = cause
        detail = f" ({type(cause).__name__}: {cause})" if cause is not None else ""
        super().__init__(
            f"schema_version: 2 recipes execute in the {RUNNER_DISTRIBUTION} "
            f"library, which is not importable here{detail}. "
            f"Install it (`uv pip install {RUNNER_DISTRIBUTION}`, or add it as a "
            "git dependency of amplifier-module-tool-recipes) and retry. "
            "This recipe was NOT run in legacy caller-bound mode: doing so would "
            "resolve its agents from the calling session instead of its declared "
            "dependencies (recipe-dependency-manifest.v1 Core 3)."
        )


class CallerAgentLeakError(RuntimeError):
    """The caller's agent map was reachable from a v2 host handover.

    A structural backstop for ``recipe-dependency-manifest.v1`` Core 3/4 and
    ``recipe-runner-lib.v1`` Core 4: no port may grant the host's ambient agent
    map to the recipe. If this ever raises, the adapter is wrong -- not the
    recipe.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            "The calling session's agent map is reachable from what this "
            f"adapter was about to hand the recipe runner, at {path}. "
            "No host port may carry a caller agent map "
            "(recipe-runner-lib.v1 Core 4; recipe-dependency-manifest.v1 Core 3). "
            "This is an adapter defect; the run was refused rather than "
            "executed with caller-bound resolution."
        )


class RecipeCancelledError(RuntimeError):
    """Raised by :class:`SessionCancellationToken` when the host cancelled."""


class V2ResumeUnavailableError(RuntimeError):
    """A mid-run v2 resume was asked for and the library exports no ``resume``.

    ``recipe-runner-lib.v1`` Core 2 names four entry points; this library
    version exports ``plan`` and ``run`` only (:func:`library_resume` returns
    ``None``). Continuing a *partly completed* run means skipping the steps it
    already finished, and only the library can do that -- doing it here would
    re-run completed steps, or make this adapter a second execution home
    (Core 1). So the resume is refused rather than approximated.

    The same refusal shape the standalone CLI uses for the same gap
    (``cli.py``'s ``EXIT_UNSUPPORTED`` branch); both disappear when the
    library's ``resume`` lands.
    """

    def __init__(self, message: str, *, remedy: str) -> None:
        self.message = message
        self.remedy = remedy
        super().__init__(f"{message} Remedy: {remedy}")


class ModelRoleUnavailableError(RuntimeError):
    """A step asked for a model role this session cannot serve.

    The host, not the library, owns provider routing (``ProviderAccess`` is a
    host port), so this refusal lives here. It exists because of the
    session-default fallback (:data:`PROVIDER_ROLES_FALLBACK`): once a lean
    session offers a default role, a step that asked for ``model_role: coding``
    would otherwise run on the default provider and report success -- a silent
    downgrade. The role is named instead, together with what this session
    actually serves.
    """

    def __init__(self, role: str, *, step_id: str | None, served: Sequence[str], label: str) -> None:
        self.role = role
        self.step_id = step_id
        self.served = tuple(served)
        self.label = label
        where = f"Step {step_id!r}" if step_id else "A step"
        super().__init__(
            f"{where} requests model role {role!r}, which this Amplifier session "
            f"does not serve; it serves {', '.join(self.served) or 'no roles at all'} "
            f"(provider_roles={label}). The step was NOT run on another provider: "
            "an unavailable model role is a real failure, never a silent downgrade "
            "(recipe-runner-lib.v1 Core 4). Remedy: activate a bundle registering a "
            f"`{MODEL_ROLE_RESOLVER_CAPABILITY}` capability that serves {role!r}, or "
            "remove the step's `model_role` so it runs on the session default."
        )


class AdapterConfigError(ValueError):
    """The ``recipes`` tool was configured with a key it does not read.

    Mirrors ``recipe-dependency-manifest.v1`` Core 12's rule for
    ``agent_config`` -- a setting is implemented or rejected, never silently
    retained inert. A config key that looks honoured but changes nothing is
    indistinguishable, from the outside, from one that works.
    """

    def __init__(self, key: str, detail: str) -> None:
        self.key = key
        self.detail = detail
        super().__init__(
            f"The recipes tool does not read config key {key!r}. {detail} "
            f"Keys this module reads: {', '.join(sorted(ACCEPTED_CONFIG_KEYS))}."
        )


# ---------------------------------------------------------------------------
# Lazy import of the one execution home (lib.v1 Core 1)
# ---------------------------------------------------------------------------


def _in_bundle_library_src() -> Path | None:
    """Locate the runner library shipped in this same bundle, if present.

    The bundle tree always ships this module and the library together:

        <bundle-root>/modules/tool-recipes/amplifier_module_tool_recipes/  (here)
        <bundle-root>/src/amplifier_recipe_runner/                          (library)

    Module activation installs only this module (`uv pip install -e <module>
    --no-sources`), so the library is not on sys.path even though it sits two
    directories up. Returns the `src` directory to add, or None.
    """
    bundle_src = Path(__file__).resolve().parents[3] / "src"
    if (bundle_src / "amplifier_recipe_runner" / "__init__.py").is_file():
        return bundle_src
    return None


def load_runner() -> ModuleType:
    """Import and return the runner library, or fail loud.

    Tries the installed package first, then the library shipped in this same
    bundle tree (see :func:`_in_bundle_library_src`).

    Raises:
        RecipeRunnerUnavailableError: the library is not installed and not
            found in the bundle tree. Never falls back to the legacy path --
            see the class docstring.
    """
    try:
        import amplifier_recipe_runner  # noqa: PLC0415 -- deliberately lazy
    except ImportError as exc:
        bundle_src = _in_bundle_library_src()
        if bundle_src is None:
            raise RecipeRunnerUnavailableError(exc) from exc
        if str(bundle_src) not in sys.path:
            sys.path.insert(0, str(bundle_src))
        try:
            import amplifier_recipe_runner  # noqa: PLC0415
        except ImportError as retry_exc:
            raise RecipeRunnerUnavailableError(retry_exc) from retry_exc
    return amplifier_recipe_runner


def runner_available() -> bool:
    """True when the runner library can be imported. Never raises."""
    try:
        load_runner()
    except RecipeRunnerUnavailableError:
        return False
    return True


def library_resume() -> Callable[..., Awaitable[Any]] | None:
    """The library's ``resume`` entry point, or ``None`` if it exports none.

    **The seam.** ``recipe-runner-lib.v1`` Core 2 names four entry points --
    ``validate``, ``plan``, ``run``, ``resume``. The shipped library exports
    ``plan`` and ``run``; ``resume`` is declared on the
    :class:`~amplifier_recipe_runner.api.RecipeRunner` protocol with no
    concrete implementation (tracked as recipes-4qf, superseding recipes-10s).

    This is a lookup rather than a hard import so the moment that entry point
    lands, :func:`resume_v2_recipe` routes to it with no change here -- and
    until it does, the absence is reported as itself instead of being
    approximated on a path that would re-run completed steps.
    """
    try:
        runner = load_runner()
    except RecipeRunnerUnavailableError:
        return None
    entry = getattr(runner, "resume", None)
    return entry if callable(entry) else None


def check_adapter_config(config: Mapping[str, Any] | None) -> None:
    """Refuse a ``recipes`` tool config key this module does not read.

    Raises:
        AdapterConfigError: on the first unread key, named. Silence would make
            a mis-spelled or unsupported setting indistinguishable from an
            honoured one -- the failure mode ``recipe-dependency-manifest.v1``
            Core 12 forbids for ``agent_config`` and Core 1 forbids for unknown
            manifest keys. This module applies the same rule to itself.
    """
    for key in config or {}:
        if key in ACCEPTED_CONFIG_KEYS:
            continue
        detail = REJECTED_CONFIG_KEYS.get(key, "It is ignored, so it would silently do nothing.")
        raise AdapterConfigError(key, detail)


# ---------------------------------------------------------------------------
# Manifest routing (manifest.v1 Core 1)
# ---------------------------------------------------------------------------

_SCHEMA_VERSION_KEY = "schema_version"


def manifest_header(recipe_path: Path) -> Mapping[str, Any] | None:
    """Top-level mapping of a recipe file, or ``None`` if it is not one.

    Deliberately forgiving: an unreadable or malformed recipe returns ``None``
    so routing falls through to the legacy path, which then raises its own,
    unchanged error. Routing must never invent a new error text for a file the
    legacy path already reports on (manifest.v1 Core 10 byte-identity).
    """
    try:
        data = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, Mapping) else None


def is_v2_recipe(recipe_path: Path) -> bool:
    """True when the recipe declares a ``schema_version`` key.

    Presence, not value, is the routing predicate. A present-but-invalid value
    (``schema_version: 3``, ``schema_version: "two"``) is the *library's* error
    to report, because the library owns manifest parsing (lib.v1 Core 1). This
    adapter must not grow a second opinion about manifest validity.
    """
    header = manifest_header(recipe_path)
    return header is not None and _SCHEMA_VERSION_KEY in header


def declared_schema_version(recipe_path: Path) -> Any | None:
    """Raw value of ``schema_version``, or ``None`` when the key is absent."""
    header = manifest_header(recipe_path)
    if header is None:
        return None
    return header.get(_SCHEMA_VERSION_KEY)


def recipe_display_name(recipe_path: Path) -> str:
    """The recipe's declared ``name``, falling back to its file stem."""
    header = manifest_header(recipe_path)
    if header is not None:
        name = header.get("name")
        if isinstance(name, str) and name.strip():
            return name
    return recipe_path.stem


# ---------------------------------------------------------------------------
# Legacy labeling + deprecation (manifest.v1 Core 10)
# ---------------------------------------------------------------------------


def legacy_deprecation_message(recipe_path: Path | str) -> str:
    """The deprecation text emitted for a legacy recipe, remedy included."""
    return (
        f"Recipe {recipe_path} declares no `schema_version` and is running in "
        f"{LEGACY_EXECUTION_MODE!r} mode: its `agent:` references resolve from "
        f"the calling session's agent map, not from the recipe's own declared "
        f"dependencies. {LEGACY_DEPRECATION_REMEDY}"
    )


def warn_legacy_recipe(recipe_path: Path | str) -> str:
    """Emit the Core 10 deprecation warning and return the message.

    The warning rides ``warnings`` and ``logging`` **only** -- never the tool
    result payload, hook events, or display messages. Those three surfaces are
    pinned byte-for-byte by ``conformance/legacy-compat`` as the evidence that
    legacy behavior did not change; announcing the deprecation on them would
    itself be the behavior change Core 10 forbids.
    """
    message = legacy_deprecation_message(recipe_path)
    warnings.warn(message, DeprecationWarning, stacklevel=3)
    logger.warning("%s", message)
    return message


def label_execution_mode(result: Any, mode: str) -> Any:
    """Attach ``execution_mode`` to a tool result and return it.

    ``ToolResult`` is a pydantic model whose serialized payload
    (``success``/``output``/``error``) is exactly what the legacy-compat
    baselines pin. The label is therefore attached *beside* that payload rather
    than inside it: ``result.execution_mode`` is readable by any caller, while
    ``model_dump()`` -- and so every recorded baseline -- is unchanged.
    """
    try:
        object.__setattr__(result, "execution_mode", mode)
    except (AttributeError, TypeError):  # pragma: no cover - exotic result types
        logger.debug("Could not label execution_mode on %r", type(result))
    return result


def execution_mode_of(result: Any) -> str | None:
    """Read back the label set by :func:`label_execution_mode`."""
    mode = getattr(result, "execution_mode", None)
    return mode if isinstance(mode, str) else None


# ---------------------------------------------------------------------------
# Caller-agent-map leak detection (lib.v1 Core 4)
# ---------------------------------------------------------------------------

_SCAN_MAX_DEPTH = 8


def caller_agent_map(coordinator: Any) -> Mapping[str, Any] | None:
    """The caller's agent map, exactly as the legacy executor reads it.

    Mirrors ``executor.py``'s ``self.coordinator.config.get("agents", {})`` so
    the leak check is aimed at the *same object* the legacy path passes to
    ``spawn(agent_configs=...)``.
    """
    config = getattr(coordinator, "config", None)
    if not isinstance(config, Mapping):
        return None
    agents = config.get("agents")
    return agents if isinstance(agents, Mapping) else None


def find_caller_agent_leak(payload: Any, agents: Mapping[str, Any] | None) -> str | None:
    """Return a path to the caller agent map inside ``payload``, or ``None``.

    Identity-based: it looks for *the caller's own* map object, or any of its
    per-agent config objects. A recipe legitimately naming an agent string is
    not a leak; handing over the caller's catalog is.

    The scan follows dataclass/instance attributes, mappings and sequences to a
    bounded depth. Bound methods terminate it, which is why every port below
    holds narrow callables rather than the coordinator itself.
    """
    if agents is None:
        return None

    targets: dict[int, str] = {id(agents): "caller agent map"}
    for name, config in agents.items():
        if config is not None and not isinstance(config, (str, int, float, bool)):
            targets[id(config)] = f"caller agent config {name!r}"

    seen: set[int] = set()

    def walk(node: Any, path: str, depth: int) -> str | None:
        if depth > _SCAN_MAX_DEPTH or node is None:
            return None
        node_id = id(node)
        if node_id in targets:
            return f"{path} ({targets[node_id]})"
        if isinstance(node, (str, bytes, int, float, bool, Path)):
            return None
        if node_id in seen:
            return None
        seen.add(node_id)

        if isinstance(node, Mapping):
            for key, value in node.items():
                found = walk(value, f"{path}[{key!r}]", depth + 1)
                if found:
                    return found
            return None
        if isinstance(node, (list, tuple, set, frozenset)):
            for index, value in enumerate(node):
                found = walk(value, f"{path}[{index}]", depth + 1)
                if found:
                    return found
            return None

        attributes = getattr(node, "__dict__", None)
        if isinstance(attributes, Mapping):
            for name, value in attributes.items():
                found = walk(value, f"{path}.{name}", depth + 1)
                if found:
                    return found
        for name in getattr(type(node), "__slots__", ()) or ():
            if not isinstance(name, str):
                continue
            found = walk(getattr(node, name, None), f"{path}.{name}", depth + 1)
            if found:
                return found
        return None

    return walk(payload, type(payload).__name__, 0)


def _refuse_agent_leak(payload: Any, coordinator: Any) -> None:
    leak = find_caller_agent_leak(payload, caller_agent_map(coordinator))
    if leak is not None:
        raise CallerAgentLeakError(leak)


# ---------------------------------------------------------------------------
# Port 1: provider access
# ---------------------------------------------------------------------------


class CoordinatorProviderAccess:
    """Amplifier's model-role routing, as the library's ``ProviderAccess``.

    Amplifier serves providers through the duck-typed ``model_role_resolver``
    capability: ``known_roles`` enumerates the roles, ``await resolve(role)``
    returns that role's provider-preference chain. The library's port is
    synchronous, so every known role is resolved once at build time and the
    resolved chain becomes the opaque ``ProviderHandle`` -- the runner passes it
    through and never introspects it.

    Carries no coordinator reference and no agent map: only role names and the
    provider preferences they resolved to.
    """

    __slots__ = ("_handles", "_role_source")

    def __init__(self, handles: Mapping[str, Any], *, role_source: str = PROVIDER_ROLES_RESOLVER) -> None:
        self._handles = dict(handles)
        self._role_source = role_source

    @classmethod
    async def create(cls, coordinator: Any) -> CoordinatorProviderAccess:
        """Pre-resolve every role the host's resolver capability enumerates.

        A host with no :data:`MODEL_ROLE_RESOLVER_CAPABILITY` at all -- a lean
        bundle such as ``anchors``, which routes nothing -- is not a host with
        no providers: it runs its own agents on its configured default. So one
        role is synthesized here, :data:`SESSION_DEFAULT_ROLE`, backed by that
        default, and it is labeled :data:`PROVIDER_ROLES_FALLBACK` wherever it
        is used.

        This is deliberately a HOST-side fallback. The library's precondition
        ("no roles, so no agent could run") stays exactly as strict: it is a
        true statement about a host that offers nothing, and weakening it would
        let a genuinely provider-less host fabricate a run.

        A host that *does* register the capability is taken at its word,
        including when it enumerates nothing: routing is configured and broken,
        which is a real failure to report rather than one to paper over.
        """
        resolver = None
        if hasattr(coordinator, "get_capability"):
            resolver = coordinator.get_capability(MODEL_ROLE_RESOLVER_CAPABILITY)
        if resolver is None:
            providers = _session_provider_names(coordinator)
            logger.warning(
                "No %s capability is registered, so this session routes no model "
                "roles; the recipe runner is given one synthesized %r role backed "
                "by the session's default provider configuration (%s) "
                "[provider_roles=%s]. A step naming an explicit `model_role` still "
                "fails rather than running on it.",
                MODEL_ROLE_RESOLVER_CAPABILITY,
                SESSION_DEFAULT_ROLE,
                ", ".join(providers) or "no named providers",
                PROVIDER_ROLES_FALLBACK,
            )
            return cls(
                {
                    SESSION_DEFAULT_ROLE: {
                        "source": PROVIDER_ROLES_FALLBACK,
                        # No preference chain: "whatever the session is
                        # configured to use", which is what the host's own
                        # agent work already runs on.
                        "provider_preferences": (),
                        "session_providers": providers,
                    }
                },
                role_source=PROVIDER_ROLES_FALLBACK,
            )

        roles = getattr(resolver, "known_roles", None)
        if not isinstance(roles, (list, tuple)):
            roles = ()

        handles: dict[str, Any] = {}
        for role in roles:
            if not isinstance(role, str):
                continue
            try:
                resolved = await resolver.resolve(role)
            except Exception as exc:  # a third-party resolver may raise
                logger.warning("model role %r did not resolve: %s", role, exc)
                continue
            if resolved:
                handles[role] = list(resolved)
        return cls(handles)

    @property
    def role_source(self) -> str:
        """Where these roles came from: the host's resolver, or the fallback."""
        return self._role_source

    @property
    def is_session_default_fallback(self) -> bool:
        """True when this session serves only the synthesized default role."""
        return self._role_source == PROVIDER_ROLES_FALLBACK

    def roles(self) -> Sequence[str]:
        return tuple(sorted(self._handles))

    def resolve(self, role: str) -> Any:
        """Return the provider-preference chain for ``role``.

        Raises:
            KeyError: this host does not serve ``role``. An unavailable provider
                is a real failure, never a silent downgrade (lib.v1 Core 4) --
                including under the session-default fallback, which serves
                exactly one role and never stands in for a named one.
        """
        try:
            return self._handles[role]
        except KeyError:
            raise KeyError(
                f"This Amplifier session serves no provider for model role {role!r}; "
                f"it serves {', '.join(self.roles()) or 'no roles at all'} "
                f"(provider_roles={self._role_source})."
            ) from None


def _session_provider_names(coordinator: Any) -> tuple[str, ...]:
    """Names of the providers mounted in the calling session. Advisory only.

    Recorded on the fallback handle so a reader can see *what* "the session
    default" meant. Strings only -- nothing here holds a live provider object,
    and nothing here can reach the caller's agent map.
    """
    providers: Any = None
    getter = getattr(coordinator, "get", None)
    if callable(getter):
        try:
            providers = getter("providers")
        except Exception:  # noqa: BLE001 - a host that cannot answer is a fact
            providers = None
    if not isinstance(providers, Mapping):
        mounts = getattr(coordinator, "mount_points", None)
        providers = mounts.get("providers") if isinstance(mounts, Mapping) else None
    if isinstance(providers, Mapping):
        return tuple(sorted(str(name) for name in providers))
    return ()


def provider_roles_label(coordinator: Any) -> str:
    """Which source serves this session's model roles, as a label.

    The same predicate :meth:`CoordinatorProviderAccess.create` uses, exposed
    synchronously so a caller can report it on a run's output without
    re-resolving anything.
    """
    resolver = None
    if hasattr(coordinator, "get_capability"):
        resolver = coordinator.get_capability(MODEL_ROLE_RESOLVER_CAPABILITY)
    return PROVIDER_ROLES_RESOLVER if resolver is not None else PROVIDER_ROLES_FALLBACK


# ---------------------------------------------------------------------------
# Explicit model roles a recipe asks for (host-side preflight)
# ---------------------------------------------------------------------------


def declared_model_roles(recipe_path: Path) -> tuple[tuple[str | None, tuple[str, ...]], ...]:
    """Every explicit ``model_role`` a recipe's steps request.

    Returns ``(step_id, roles)`` pairs -- a step may name a chain, and a step
    that names none contributes nothing. Nested and staged step bodies are
    walked, so a role buried in a ``foreach`` body is not missed.
    """
    header = manifest_header(recipe_path)
    if header is None:
        return ()

    found: list[tuple[str | None, tuple[str, ...]]] = []

    def visit(steps: Any) -> None:
        if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
            return
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            step_id = step.get("id") if isinstance(step.get("id"), str) else None
            declared = step.get("model_role")
            roles: tuple[str, ...] = ()
            if isinstance(declared, str) and declared.strip():
                roles = (declared.strip(),)
            elif isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
                roles = tuple(r.strip() for r in declared if isinstance(r, str) and r.strip())
            if roles:
                found.append((step_id, roles))
            for key in ("steps", "while_steps"):
                visit(step.get(key))

    visit(header.get("steps"))
    for stage in header.get("stages") or ():
        if isinstance(stage, Mapping):
            visit(stage.get("steps"))
    return tuple(found)


def check_model_roles(recipe_path: Path, provider_access: Any) -> None:
    """Refuse a run whose steps name a model role this session cannot serve.

    Raises:
        ModelRoleUnavailableError: naming the first unserved role and the step
            that asked for it. A step naming a *chain* passes when any entry in
            it resolves -- that is what a chain means -- and fails naming its
            first entry when none does.
    """
    served = set(provider_access.roles())
    label = getattr(provider_access, "role_source", PROVIDER_ROLES_RESOLVER)
    for step_id, roles in declared_model_roles(recipe_path):
        if any(role in served for role in roles):
            continue
        raise ModelRoleUnavailableError(
            roles[0],
            step_id=step_id,
            served=tuple(sorted(served)),
            label=label,
        )


# ---------------------------------------------------------------------------
# Port 2: approval callback
# ---------------------------------------------------------------------------


class SessionApprovalCallback:
    """Amplifier's recipe-session approval gates, as the library's callback.

    Approvals in the ``recipes`` tool are out-of-band: a gate records a pending
    approval in session state, the caller answers with the tool's ``approve`` /
    ``deny`` operation, and the run continues on ``resume``. That is mapped
    faithfully here -- an unanswered gate returns ``approved=False`` (which the
    library turns into a paused run), never a fabricated approval.
    """

    __slots__ = ("_project_path", "_session_id", "_session_manager")

    def __init__(
        self,
        session_manager: Any,
        project_path: Path,
        session_id: str | None,
    ) -> None:
        self._session_manager = session_manager
        self._project_path = project_path
        self._session_id = session_id

    async def __call__(self, request: Any) -> Any:
        runner = load_runner()
        decision = runner.ApprovalDecision

        if self._session_id is None:
            return decision(
                approved=False,
                message=(
                    "No Amplifier recipe session is bound to this run, so its "
                    f"approval gate {request.stage!r} cannot be answered. "
                    "Nothing was approved."
                ),
            )

        status = self._session_manager.get_stage_approval_status(
            self._session_id, self._project_path, request.stage
        )
        if status == ApprovalStatus.APPROVED:
            state = self._session_manager.load_state(self._session_id, self._project_path)
            return decision(approved=True, message=state.get("_approval_message", ""))
        if status in (ApprovalStatus.DENIED, ApprovalStatus.TIMEOUT):
            return decision(approved=False, message=f"Stage {request.stage!r} was {status.value}.")

        self._session_manager.set_pending_approval(
            session_id=self._session_id,
            project_path=self._project_path,
            stage_name=request.stage,
            prompt=request.prompt,
            timeout=int(request.details.get("timeout", 3600)),
            default="deny",
        )
        return decision(
            approved=False,
            message=(
                f"Stage {request.stage!r} is awaiting approval. Answer it with the "
                "recipes tool's `approve` (or `deny`) operation, then `resume`."
            ),
        )


# ---------------------------------------------------------------------------
# Port 3: event sink
# ---------------------------------------------------------------------------


class CoordinatorEventSink:
    """Runner events forwarded to Amplifier's hooks and display.

    Holds two narrow callables, never the coordinator: an async hook emitter and
    a synchronous display writer. ``emit`` must not raise -- a sink failure never
    fails a run -- so every forward is guarded.
    """

    __slots__ = ("_hook_emit", "_show_message", "_tasks")

    def __init__(
        self,
        hook_emit: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
        show_message: Callable[..., Any] | None = None,
    ) -> None:
        self._hook_emit = hook_emit
        self._show_message = show_message
        self._tasks: set[asyncio.Task[Any]] = set()

    @classmethod
    def from_coordinator(cls, coordinator: Any) -> CoordinatorEventSink:
        hooks = getattr(coordinator, "hooks", None)
        display = getattr(coordinator, "display_system", None)
        return cls(
            hook_emit=getattr(hooks, "emit", None),
            show_message=getattr(display, "show_message", None),
        )

    def emit(self, event: Any) -> None:
        kind = getattr(event, "kind", "")
        data = dict(getattr(event, "data", {}) or {})
        data.setdefault("run_id", getattr(event, "run_id", None))
        name = f"recipe:runner:{kind}"
        # Guarded independently: one broken sink must not silence the other,
        # and neither may fail the run.
        for forward in (self._forward_display, self._forward_hook):
            try:
                forward(name, data)
            except Exception as exc:
                logger.debug("recipe runner event %r not forwarded: %s", kind, exc)

    def _forward_display(self, name: str, data: dict[str, Any]) -> None:
        if self._show_message is not None:
            self._show_message(f"{name} {data}", level="info", source="recipes")

    def _forward_hook(self, name: str, data: dict[str, Any]) -> None:
        if self._hook_emit is None:
            return
        coro = self._hook_emit(name, data)
        if not asyncio.iscoroutine(coro):
            return
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no running loop -- nothing to schedule onto
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


# ---------------------------------------------------------------------------
# Port 5: cancellation  (port 4, workspace, is a bare path)
# ---------------------------------------------------------------------------


class SessionCancellationToken:
    """Amplifier's recipe-session cancellation, as the library's token.

    Backed by the same session state the ``recipes`` tool's ``cancel`` operation
    writes, so cancelling a v2 run uses the operation callers already know.
    """

    __slots__ = ("_project_path", "_session_id", "_session_manager")

    def __init__(
        self,
        session_manager: Any,
        project_path: Path,
        session_id: str | None,
    ) -> None:
        self._session_manager = session_manager
        self._project_path = project_path
        self._session_id = session_id

    @property
    def cancelled(self) -> bool:
        if self._session_id is None:
            return False
        return bool(
            self._session_manager.is_cancellation_requested(self._session_id, self._project_path)
        )

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RecipeCancelledError(
                f"Recipe session {self._session_id} was cancelled by the host."
            )


# ---------------------------------------------------------------------------
# Assembling the handover
# ---------------------------------------------------------------------------


async def build_host_services(
    coordinator: Any,
    session_manager: Any,
    project_path: Path,
    *,
    session_id: str | None = None,
) -> Any:
    """Map Amplifier onto the library's five ports, and prove nothing leaked.

    Raises:
        CallerAgentLeakError: the caller's agent map is reachable through the
            assembled ports. Structurally impossible via ``HostServices``'
            fields; checked anyway because a leak would be invisible otherwise.
    """
    runner = load_runner()
    services = runner.HostServices(
        provider_access=await CoordinatorProviderAccess.create(coordinator),
        workspace=runner.WorkspacePath(Path(project_path)),
        approval_callback=SessionApprovalCallback(session_manager, project_path, session_id),
        event_sink=CoordinatorEventSink.from_coordinator(coordinator),
        cancellation=SessionCancellationToken(session_manager, project_path, session_id),
    )
    _refuse_agent_leak(services, coordinator)
    return services


def build_validate_request(recipe_path: Path) -> Any:
    """A services-free ``RunRequest`` for ``validate`` / ``plan``.

    ``services`` is ``None`` deliberately. Both entry points are side-effect
    free and documented to work with no host wiring at all (``RunRequest``);
    handing them the five ports would give a *validation* reach into the
    calling session it has no reason to have. With no services the library
    workspaces the plan at the recipe's own directory.

    ``legacy_mode`` stays ``False`` for the same reason it does in
    :func:`build_run_request`, and no host config can change it -- see
    :data:`REJECTED_CONFIG_KEYS`.
    """
    runner = load_runner()
    return runner.RunRequest(
        recipe=Path(recipe_path),
        context={},
        services=None,
        legacy_mode=False,
    )


def issue_for(exc: BaseException) -> Any:
    """A library error as the library's own ``ValidationIssue``.

    Mirrors the standalone CLI's ``_issue_for`` (``cli.py``) field for field,
    so the same recipe validated through the tool and through the CLI reports
    the same code, message, location and remedy. The typed error stays typed:
    ``code`` is the exception class name, never a flattened string.
    """
    runner = load_runner()
    return runner.ValidationIssue(
        code=type(exc).__name__,
        message=str(getattr(exc, "message", None) or exc),
        location=str(getattr(exc, "location", None) or getattr(exc, "source", None) or "") or None,
        remedy=getattr(exc, "remedy", None),
    )


async def validate_v2_recipe(
    recipe_path: Path,
    *,
    plan: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    """Validate a schema-v2 recipe: manifest parse + plan preflight, no run.

    ``recipe-runner-lib.v1`` Core 1 puts manifest parsing and dependency
    resolution in the library, so this asks the library rather than growing a
    second opinion: :func:`amplifier_recipe_runner.plan` parses the manifest,
    resolves the declared closure, and raises the typed preflight errors. It
    executes nothing and never sees a caller agent map -- the request carries
    no services at all.

    Args:
        plan: injection seam for tests; defaults to the library's own ``plan``.

    Returns:
        The library's ``ValidationReport``. Every failure -- a manifest parse
        error, a typed preflight refusal, or an environmental failure such as
        an unreachable dependency source -- comes back as a finding whose
        ``code`` is the real exception type, never as a fabricated ``ok``.
    """
    runner = load_runner()
    request = build_validate_request(recipe_path)
    try:
        resolved = await (plan or runner.plan)(request)
    except Exception as exc:  # noqa: BLE001 -- one place turns any failure into a finding
        return runner.ValidationReport(
            ok=False,
            schema_version=None,
            legacy=isinstance(exc, runner.LegacyRecipeError),
            errors=(issue_for(exc),),
        )
    return runner.ValidationReport(ok=True, schema_version=resolved.schema_version, legacy=False)


def build_run_request(
    recipe_path: Path,
    context_vars: Mapping[str, Any] | None,
    services: Any,
    coordinator: Any,
    *,
    run_id: str | None = None,
) -> Any:
    """Build the library's ``RunRequest``, and prove nothing leaked.

    ``legacy_mode`` stays ``False``: this adapter routes here only for recipes
    that declare ``schema_version``, and a v2 recipe is never caller-bound. No
    host config can flip it -- ``legacy_mode`` as a tool config key is refused
    at mount (:data:`REJECTED_CONFIG_KEYS`), because a host able to set it
    could rebind a v2 recipe's agents to the caller and still report success.
    """
    runner = load_runner()
    request = runner.RunRequest(
        recipe=Path(recipe_path),
        context=dict(context_vars or {}),
        services=services,
        run_id=run_id,
        legacy_mode=False,
    )
    _refuse_agent_leak(request, coordinator)
    return request


async def run_v2_recipe(
    coordinator: Any,
    session_manager: Any,
    recipe_path: Path,
    context_vars: Mapping[str, Any] | None,
    project_path: Path,
    *,
    session_id: str | None = None,
    run: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    """Execute a schema-v2 recipe in the runner library.

    Before handing over, the steps' explicit ``model_role`` requests are
    checked against what this session actually serves
    (:func:`check_model_roles`) -- so the session-default fallback can never
    stand in for a named role.

    Args:
        run: injection seam for tests; defaults to the library's own ``run``.

    Returns:
        The library's ``RunResult``, untranslated. Translation to a tool result
        belongs to the caller, so this stays a pure port-mapping function.
    """
    runner = load_runner()
    services = await build_host_services(
        coordinator, session_manager, project_path, session_id=session_id
    )
    check_model_roles(recipe_path, services.provider_access)
    request = build_run_request(recipe_path, context_vars, services, coordinator)
    return await (run or runner.run)(request)


async def resume_v2_recipe(
    coordinator: Any,
    session_manager: Any,
    recipe_path: Path,
    context_vars: Mapping[str, Any] | None,
    project_path: Path,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    completed_steps: Sequence[str] = (),
    resume: Callable[..., Awaitable[Any]] | None = None,
    run: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    """Continue a schema-v2 run, through the library and only the library.

    Two routes, in this order:

    1. The library's ``resume`` entry point, when it exports one
       (:func:`library_resume`). It replays recorded provenance and skips
       completed steps -- ``recipe-dependency-manifest.v1`` Core 8.
    2. Nothing completed, so resuming *is* running from the start: one
       ``run`` call, against the recorded ``run_id``. This is the standalone
       CLI's own reading of the same case (``cli.py``'s ``resume_command``),
       and it re-runs nothing that already ran.

    Anything else -- a partly completed run with no library ``resume`` -- is
    refused with :class:`V2ResumeUnavailableError`. It is never resumed on the
    legacy caller-bound path: that would resolve the recipe's agents from this
    session instead of its declared dependencies (Core 3).

    Args:
        completed_steps: steps the recorded run reported finishing.
        resume/run: injection seams for tests.
    """
    runner = load_runner()
    services = await build_host_services(
        coordinator, session_manager, project_path, session_id=session_id
    )
    check_model_roles(recipe_path, services.provider_access)
    request = build_run_request(
        recipe_path, context_vars, services, coordinator, run_id=run_id
    )

    entry = resume or library_resume()
    if entry is not None:
        return await entry(request)

    if completed_steps:
        raise V2ResumeUnavailableError(
            f"Run {run_id or '(unrecorded)'} stopped after "
            f"{len(completed_steps)} completed step(s) "
            f"({', '.join(completed_steps)}), and continuing mid-run needs the "
            f"{RUNNER_DISTRIBUTION} library's `resume` entry point, which this "
            "version does not export -- so the completed steps cannot be "
            "skipped.",
            remedy=(
                "Re-run the recipe with the `execute` operation to redo every step "
                "(the recorded run is left untouched), or upgrade to a runner "
                "version whose library exposes `resume`. It was NOT resumed on the "
                "legacy caller-bound path: that would resolve its agents from this "
                "session instead of its declared dependencies."
            ),
        )

    return await (run or runner.run)(request)
