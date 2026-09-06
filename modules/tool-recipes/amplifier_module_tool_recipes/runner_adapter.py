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
import hashlib
import importlib
import logging
import re
import sys
import uuid
import warnings
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from .closed_world import V2_LEGACY_ENGINE_EXECUTION_MODE
from .session import ApprovalStatus

logger = logging.getLogger(__name__)

__all__ = [
    "ACCEPTED_CONFIG_KEYS",
    "LEGACY_DEPRECATION_REMEDY",
    "LEGACY_EXECUTION_MODE",
    "LIBRARY_SOURCE_INSTALLED",
    "LIBRARY_SOURCE_IN_BUNDLE",
    "LIBRARY_SOURCE_PREIMPORTED",
    "REJECTED_CONFIG_KEYS",
    "UNKNOWN_LIBRARY_VERSION",
    "SELF_AGENT",
    "RUNNER_DISTRIBUTION",
    "RUNNER_IMPORT_NAME",
    "V2_EXECUTION_MODE",
    "V2_LEGACY_ENGINE_EXECUTION_MODE",
    "MODEL_ROLE_RESOLVER_CAPABILITY",
    "PROVIDER_ROLES_FALLBACK",
    "PROVIDER_ROLES_RESOLVER",
    "SESSION_DEFAULT_ROLE",
    "AdapterConfigError",
    "CallerAgentLeakError",
    "CoordinatorEventSink",
    "CoordinatorProviderAccess",
    "EngineSessionRecorder",
    "RecipeRunnerUnavailableError",
    "RunnerLibrary",
    "SessionApprovalCallback",
    "ModelRoleUnavailableError",
    "SessionCancellationToken",
    "V2ResumeUnavailableError",
    "build_host_services",
    "build_run_request",
    "build_validate_request",
    "check_adapter_config",
    "check_legacy_agents_available",
    "check_model_roles",
    "collect_agent_references",
    "declared_model_roles",
    "declared_schema_version",
    "execution_mode_of",
    "find_caller_agent_leak",
    "is_v2_recipe",
    "issue_for",
    "label_execution_mode",
    "legacy_deprecation_message",
    "legacy_missing_agents_message",
    "library_resume",
    "load_runner",
    "manifest_header",
    "provider_roles_label",
    "resume_v2_recipe",
    "run_v2_recipe",
    "run_v2_recipe_in_session",
    "runner_provenance",
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
#:
#: ``shutdown_drain_timeout`` is the ceiling, in seconds, on the post-run wait
#: for background work the run left behind (``shutdown.py``, recipes-8sr).
ACCEPTED_CONFIG_KEYS = frozenset(
    {"session_dir", "auto_cleanup_days", "shutdown_drain_timeout"}
)

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

    ``recipe-runner-lib.v1`` Core 2 names four entry points. The library this
    repository ships exports all four -- ``resume`` landed with recipes-4qf --
    so this refusal is reached only against a copy that predates it, one where
    :func:`library_resume` still returns ``None``. Continuing a *partly
    completed* run means skipping the steps it already finished, and only the
    library can do that -- doing it here would re-run completed steps, or make
    this adapter a second execution home (Core 1). So the resume is refused
    rather than approximated.

    The same refusal shape the standalone CLI uses for the same gap
    (``cli.py``'s ``EXIT_UNSUPPORTED`` branch).
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
    if (bundle_src / RUNNER_IMPORT_NAME / "__init__.py").is_file():
        return bundle_src
    return None


#: How this process came by the library copy it is actually using.
LIBRARY_SOURCE_IN_BUNDLE = "in-bundle"
LIBRARY_SOURCE_INSTALLED = "installed"
LIBRARY_SOURCE_PREIMPORTED = "already-imported"

#: Reported when the library's own ``__version__`` could not be read from a
#: copy's source. Never guessed and never blank: an unknown version that
#: printed as ``0.1.0`` would make a real skew look like a match.
UNKNOWN_LIBRARY_VERSION = "unknown"

_VERSION_PATTERN = re.compile(r"^__version__\s*[:=]\s*[\"']([^\"']+)[\"']", re.MULTILINE)


@dataclass(frozen=True)
class RunnerLibrary:
    """Identity of one copy of the runner library on this machine.

    ``version`` alone does not identify a copy. The measured defect
    (recipes-4g5) was two clones of *this same repository* at different
    commits, both declaring ``__version__ = "0.1.0"`` -- so a version-only
    comparison would have reported a match while one copy was missing the
    provenance fields the other emitted. ``fingerprint`` is therefore a digest
    of the package's own sources, and two copies are "the same library" only
    when it agrees.
    """

    package_dir: str
    module_file: str
    version: str
    fingerprint: str
    source: str

    def to_mapping(self) -> dict[str, str]:
        """Plain data, for the run record and the tool output."""
        return {
            "package_dir": self.package_dir,
            "module_file": self.module_file,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "source": self.source,
        }

    def same_library_as(self, other: RunnerLibrary) -> bool:
        """True when both copies would execute identical code.

        Compares content, not location: two clones of the same commit in two
        cache directories are the same library and must not warn.
        """
        return self.version == other.version and self.fingerprint == other.fingerprint


def _library_version(package_dir: Path) -> str:
    """The ``__version__`` a copy declares, read WITHOUT importing it.

    The whole point is to describe a copy this process did *not* load; importing
    it to ask would put a second copy of the library in ``sys.modules``.
    """
    try:
        source = (package_dir / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return UNKNOWN_LIBRARY_VERSION
    match = _VERSION_PATTERN.search(source)
    return match.group(1) if match else UNKNOWN_LIBRARY_VERSION


def _library_fingerprint(package_dir: Path) -> str:
    """A digest over a copy's own ``*.py`` sources (tests excluded).

    Excludes ``tests/`` and ``__pycache__`` so a test-only difference -- which
    cannot change what a run does -- is not reported as a skew.
    """
    digest = hashlib.sha256()
    try:
        paths = sorted(package_dir.rglob("*.py"))
    except OSError:
        return UNKNOWN_LIBRARY_VERSION
    for path in paths:
        try:
            relative = path.relative_to(package_dir)
        except ValueError:  # pragma: no cover -- rglob results are relative
            continue
        parts = relative.parts
        if "tests" in parts or "__pycache__" in parts:
            continue
        try:
            body = path.read_bytes()
        except OSError:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest()[:16]


def _identify(package_dir: Path, module_file: str, source: str) -> RunnerLibrary:
    return RunnerLibrary(
        package_dir=str(package_dir),
        module_file=module_file,
        version=_library_version(package_dir),
        fingerprint=_library_fingerprint(package_dir),
        source=source,
    )


def _identify_copy_at(bundle_src: Path) -> RunnerLibrary:
    """Identify the bundle-local copy from its files alone (no import)."""
    package_dir = bundle_src / RUNNER_IMPORT_NAME
    return _identify(
        package_dir,
        str(package_dir / "__init__.py"),
        LIBRARY_SOURCE_IN_BUNDLE,
    )


def _identify_imported(module: ModuleType, bundle_src: Path | None) -> RunnerLibrary:
    """Identify the copy this process actually imported.

    Reads the identity off the *imported module's* ``__file__`` rather than
    off whatever was intended, so the record describes what ran.
    """
    module_file = getattr(module, "__file__", None)
    if not module_file:  # namespace package or a stub in a test
        return RunnerLibrary(
            package_dir="",
            module_file="",
            version=str(getattr(module, "__version__", UNKNOWN_LIBRARY_VERSION)),
            fingerprint=UNKNOWN_LIBRARY_VERSION,
            source=LIBRARY_SOURCE_PREIMPORTED,
        )
    package_dir = Path(module_file).resolve().parent
    source = LIBRARY_SOURCE_INSTALLED
    if bundle_src is not None and package_dir == (bundle_src / RUNNER_IMPORT_NAME):
        source = LIBRARY_SOURCE_IN_BUNDLE
    return _identify(package_dir, str(Path(module_file).resolve()), source)


def _skew_warning(used: RunnerLibrary, in_bundle: RunnerLibrary) -> str:
    return (
        "Recipe runner library SKEW: this run will execute "
        f"{RUNNER_DISTRIBUTION} from {used.module_file} "
        f"(version {used.version}, fingerprint {used.fingerprint}), but the copy "
        f"shipped beside this tool module is {in_bundle.module_file} "
        f"(version {in_bundle.version}, fingerprint {in_bundle.fingerprint}). "
        "These are different libraries, so this run's provenance record may be "
        "missing fields the bundle's own library emits -- the shadowing copy was "
        "already imported (or installed through a sys.meta_path finder) before "
        "this adapter could put the bundle-local one first. Remedy: refresh or "
        f"uninstall the shadowing copy (`uv pip uninstall {RUNNER_DISTRIBUTION}`), "
        "or re-point its editable install at this bundle tree. Reported rather "
        "than run silently: a degraded provenance record is indistinguishable "
        "from a correct one once written."
    )


#: Cache of :func:`runner_provenance`, keyed by the module object it described.
#: Keyed on the object (not just presence) so a test that swaps
#: ``sys.modules`` recomputes -- and re-warns -- instead of reading a stale
#: answer for a library that is no longer loaded.
_PROVENANCE_CACHE: tuple[int, RunnerLibrary] | None = None


def _describe_loaded(module: ModuleType) -> RunnerLibrary:
    """Identify ``module``, warning once per loaded copy if it is skewed."""
    global _PROVENANCE_CACHE
    cached = _PROVENANCE_CACHE
    if cached is not None and cached[0] == id(module):
        return cached[1]

    bundle_src = _in_bundle_library_src()
    used = _identify_imported(module, bundle_src)
    if bundle_src is not None and used.source != LIBRARY_SOURCE_IN_BUNDLE:
        in_bundle = _identify_copy_at(bundle_src)
        if used.same_library_as(in_bundle):
            logger.debug(
                "%s resolved to %s rather than the bundle-local copy at %s; their "
                "contents are identical, so there is no skew.",
                RUNNER_DISTRIBUTION,
                used.module_file,
                in_bundle.module_file,
            )
        else:
            logger.warning("%s", _skew_warning(used, in_bundle))

    _PROVENANCE_CACHE = (id(module), used)
    return used


def runner_provenance() -> dict[str, Any] | None:
    """Which copy of the runner library this process is using, as plain data.

    Recorded on every v2 run (``runner_library`` on the run record and on the
    tool output) so "which library produced this provenance?" is answerable
    from the record itself instead of by re-deriving it from an environment
    that has since changed.

    Returns ``None`` only when the library is not importable at all -- in which
    case no v2 recipe ran, and :class:`RecipeRunnerUnavailableError` already
    said so.
    """
    try:
        module = load_runner()
    except RecipeRunnerUnavailableError:
        return None
    return _describe_loaded(module).to_mapping()


def load_runner() -> ModuleType:
    """Import and return the runner library, or fail loud.

    **Prefers the copy shipped in this same bundle tree.** The library and this
    module are versioned together in one checkout, so the library sitting two
    directories up (:func:`_in_bundle_library_src`) is by definition the one
    this module was written against. Its path goes on the *front* of
    ``sys.path`` before the first import, ahead of any installed copy.

    That ordering was the defect (recipes-4g5): the Amplifier venv had
    ``amplifier_recipe_runner`` editable-installed from a *second* cache clone
    pinned at an older commit, while this module executed from a refreshed
    bundle cache. A plain import bound the stale copy, and a v2 run emitted a
    structurally degraded provenance record -- no error, no warning.

    Two cases ``sys.path`` order cannot fix are detected instead of assumed:
    a copy already in ``sys.modules`` before this ran, and an editable install
    served by a ``sys.meta_path`` finder (which outranks ``sys.path``). In both
    the imported copy is identified from its own ``__file__``, compared against
    the bundle-local one by version *and* content digest, and a mismatch is
    logged as a WARNING naming both paths and versions
    (:func:`runner_provenance`).

    Raises:
        RecipeRunnerUnavailableError: the library is neither installed nor
            present in the bundle tree. Never falls back to the legacy path --
            see the class docstring.
    """
    already_loaded = sys.modules.get(RUNNER_IMPORT_NAME)
    if already_loaded is not None:
        _describe_loaded(already_loaded)
        return already_loaded

    bundle_src = _in_bundle_library_src()
    if bundle_src is not None:
        # Moved to the FRONT, not merely ensured present: an installed copy's
        # `.pth` entry (or a plainly-prepended path) already on `sys.path`
        # ahead of ours is exactly the shadowing this exists to prevent, and
        # "it is somewhere on the path" would not fix it.
        entry = str(bundle_src)
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)

    try:
        module = importlib.import_module(RUNNER_IMPORT_NAME)
    except ImportError as exc:
        raise RecipeRunnerUnavailableError(exc) from exc

    _describe_loaded(module)
    return module


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
    all four: ``resume`` landed with recipes-4qf (superseding recipes-10s) as
    ``amplifier_recipe_runner.execution.resume``, so this lookup now finds one
    and :func:`resume_v2_recipe` routes to it.

    It stays a lookup rather than a hard import because the version actually
    importable on a host is not this repository's to assume: against a copy
    that predates recipes-4qf this still returns ``None`` and the absence is
    reported as itself (:class:`V2ResumeUnavailableError`) instead of being
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


# ---------------------------------------------------------------------------
# Legacy plan-time agent preflight
# ---------------------------------------------------------------------------

#: Pseudo-agent meaning "spawn the current agent". It is never looked up in an
#: agent registry, so the LEGACY preflight exempts it (same rule the
#: availability warning already applies).
#:
#: The exemption is legacy-only, and deliberately so. Under ``schema_version:
#: 2`` the same name is REFUSED -- see
#: :class:`~amplifier_recipe_runner.errors.SelfAgentUnsupportedError` and the
#: "Why ``agent: self`` is refused" section of :mod:`.closed_world`. In a
#: caller-bound run "the calling session's own agent" is a coherent, labeled
#: thing; in a closed world it is the caller's whole config smuggled back in.
#: Same string, two schema versions, two answers.
SELF_AGENT = "self"


def collect_agent_references(recipe: Any) -> set[str]:
    """Every ``agent:`` a legacy recipe would resolve, ``self`` excluded.

    Covers flat and staged steps (via ``Recipe.get_all_steps``) *and* the
    nested bodies of compound steps. A ``foreach``/``while`` body is stored as
    ``while_steps`` -- a list of **raw dicts** parsed on the fly by the
    executor, so it never appears in ``get_all_steps()`` and its ``agent:``
    references would otherwise be invisible to any plan-time check. Bodies
    nest, so the walk recurses.

    Deliberately out of scope: ``type: recipe`` sub-recipes. Their agent
    references live in a *different* file resolved at execution time; reading
    them here would mean resolving sub-recipe paths at plan time, which is a
    behavior change of its own rather than a diagnostic.
    """
    references: set[str] = set()

    def _add(agent: Any) -> None:
        if isinstance(agent, str) and agent and agent != SELF_AGENT:
            references.add(agent)

    def _walk_raw(bodies: Any) -> None:
        if not isinstance(bodies, (list, tuple)):
            return
        for entry in bodies:
            if not isinstance(entry, Mapping):
                continue
            _add(entry.get("agent"))
            # Raw YAML uses `steps` for a nested body; the parsed model renames
            # it to `while_steps`. A raw dict can carry either.
            _walk_raw(entry.get("steps"))
            _walk_raw(entry.get("while_steps"))

    try:
        steps = recipe.get_all_steps()
    except Exception:  # pragma: no cover - defensive; models always provide it
        return references

    for step in steps:
        _add(getattr(step, "agent", None))
        _walk_raw(getattr(step, "while_steps", None))

    return references


def _caller_bundle_name(coordinator: Any) -> str | None:
    """Best-effort name of the bundle whose agent map a legacy recipe binds to.

    The CLI writes ``bundle_name`` into the session config, which is the same
    mapping the coordinator exposes as ``config``. Returns ``None`` when the
    host does not publish it, so the message can say less rather than guess.
    """
    for holder in (coordinator, getattr(coordinator, "session", None)):
        try:
            config = getattr(holder, "config", None)
            if isinstance(config, Mapping):
                name = config.get("bundle_name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        except Exception:
            continue
    return None


def _example_bundle(missing: Sequence[str]) -> str:
    """A concrete ``-b`` value for the remedy, taken from a missing reference.

    ``foundation:zen-architect`` names its bundle already; a bare ``reviewer``
    does not, so the example falls back to a placeholder rather than inventing
    a bundle that may not exist.
    """
    for agent in missing:
        namespace, sep, _ = agent.partition(":")
        if sep and namespace:
            return namespace
    return "<bundle>"


def legacy_missing_agents_message(
    missing: Sequence[str], bundle_name: str | None = None
) -> str:
    """The plan-time diagnostic for a legacy recipe the caller cannot serve.

    Names the agents, the bundle, and **both** remedies. Without this the run
    reaches its first agent step and dies on a bare "agent not found", which
    says nothing about why the recipe worked elsewhere or what to do next.
    """
    agents = ", ".join(f"'{agent}'" for agent in missing)
    bundle_clause = (
        f"the calling bundle '{bundle_name}'" if bundle_name else "the calling bundle"
    )
    return (
        f"This legacy recipe references agent(s) {agents} which {bundle_clause} "
        f"does not mount. A legacy recipe resolves `agent:` from the calling "
        f"session's agent map, so this run would fail at its first agent step. "
        f"Remedy 1: run it from a bundle that includes them (e.g. "
        f"`amplifier tool invoke -b {_example_bundle(missing)} recipes ...`). "
        f"Remedy 2: migrate the recipe to `schema_version: 2` with a "
        f"`dependencies:` block so it carries its own agents "
        f"(see docs/RECIPE_SCHEMA.md, 'Recipe schema v2')."
    )


def check_legacy_agents_available(
    recipe: Any, coordinator: Any
) -> tuple[list[str], str] | None:
    """Plan-time preflight for the legacy path: can the caller serve this recipe?

    Returns ``None`` when the run may proceed, or ``(missing, message)`` when it
    cannot. Enumeration is best-effort and *skips* -- returning ``None`` -- when
    the host exposes no readable agent registry, exactly as
    :func:`~amplifier_module_tool_recipes.validator.check_agent_availability`
    does. Refusing a runnable recipe because we could not read a registry would
    be strictly worse than the mid-run failure this replaces.

    This only fires where the run was already doomed: every agent the recipe
    names is missing from the map the executor would resolve against. Recipes
    whose agents ARE present are unaffected, which is what keeps
    ``conformance/legacy-compat`` byte-identical.
    """
    from .validator import _enumerate_available_agents

    available = _enumerate_available_agents(coordinator)
    if available is None:
        return None

    missing = sorted(
        agent for agent in collect_agent_references(recipe) if agent not in available
    )
    if not missing:
        return None

    return missing, legacy_missing_agents_message(
        missing, _caller_bundle_name(coordinator)
    )


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


class EngineSessionRecorder:
    """A session-manager view that remembers the FIRST session the engine makes.

    The fallback for a run with no session to attach to. When the caller bound
    one, :func:`run_v2_recipe_in_session` hands it to the engine and the engine
    creates none -- the bound session holds the approval gate, the checkpoint
    and the completed-step list, and this recorder sees only later sub-recipe
    children, which are not the run's identity. When the caller bound nothing
    (session binding failed), the engine creates its own inside
    ``execute_recipe`` and this is the only report of it: a run that stops at a
    gate raises from inside the engine, and a run that fails may not reach a
    context at all.

    Capturing it is what makes a later ``resume`` able to re-enter the run that
    was actually interrupted instead of starting a second one beside it.

    Only the first creation is kept. Sub-recipe steps create their own child
    sessions later; the top-level one is the run's identity.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.session_id: str | None = None

    def create_session(self, *args: Any, **kwargs: Any) -> str:
        created = self._inner.create_session(*args, **kwargs)
        if self.session_id is None:
            self.session_id = created
        return created

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def closed_world_scope(
    resolved_plan: Any, coordinator: Any, recipe_path: Path | str
) -> Any:
    """The coordinator view a schema-v2 recipe executes against, labelled.

    One home for the three things that turn a resolved plan into a runnable
    scope: build the catalog from the plan's closure, wrap the caller's
    coordinator in it, and say so at INFO with the ``execution_mode`` label.
    Both v2 entry points -- a top-level run (:func:`run_v2_recipe_in_session`)
    and a v2 sub-recipe reached from another recipe
    (:func:`build_sub_recipe_scope`) -- go through here, so neither can
    silently acquire a different catalog or wear a different label.

    ``coordinator`` must be the *host's* coordinator, never an already-scoped
    view -- see :func:`~.closed_world.host_coordinator_of`.
    """
    from .closed_world import ClosedWorldCoordinator  # noqa: PLC0415 -- lazy
    from .closed_world import build_catalog  # noqa: PLC0415

    catalog = build_catalog(resolved_plan)
    scoped = ClosedWorldCoordinator(coordinator, catalog)
    logger.info(
        "Executing %s on the legacy step engine with the plan's closed-world "
        "catalog (%d agent(s): %s) [execution_mode=%s]",
        recipe_path,
        len(catalog),
        ", ".join(catalog.names) or "none",
        V2_LEGACY_ENGINE_EXECUTION_MODE,
    )
    return scoped


async def build_sub_recipe_scope(
    coordinator: Any,
    session_manager: Any,
    recipe_path: Path,
    context_vars: Mapping[str, Any] | None,
    project_path: Path,
    *,
    session_id: str | None = None,
    plan: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    """The closed-world coordinator for a schema-v2 recipe reached as a *step*.

    A recipe's ``schema_version`` is a property of the recipe, not of how it
    was reached (manifest.v1 Core 3/4). Invoked directly, ``repo-audit.yaml``
    resolves ``foundation:zen-architect`` from its own declared closure;
    invoked as a ``type: recipe`` step of a legacy parent it used to inherit
    the parent's caller-bound coordinator and die on "Agent
    'foundation:zen-architect' not found in configuration" -- the *same*
    recipe, the same host, two different agent maps (recipes-ykj).

    This resolves the sub-recipe exactly as a direct invocation would: the
    library's ``plan()`` over the sub-recipe's own manifest, then the same
    catalog and the same label via :func:`closed_world_scope`.

    Unlike :func:`run_v2_recipe_in_session`, a refusal here **raises** rather
    than becoming a ``RunResult``: the caller is a step of another recipe, and
    a step reports failure by failing. There is deliberately no fallback to
    the caller-bound path -- running a v2 sub-recipe against the parent's map
    would resolve a different agent catalog while reporting success.

    Args:
        coordinator: the *host's* coordinator. Unwrapping an already-scoped
            parent view is the caller's job (:func:`build_sub_recipe_scope`
            is handed one by the executor, which unwraps first).
        plan: injection seam for tests; defaults to the library's ``plan``.

    Raises:
        RecipeRunnerUnavailableError: the runner library is not importable, so
            the declared closure cannot be resolved at all.
    """
    runner = load_runner()
    services = await build_host_services(
        coordinator, session_manager, project_path, session_id=session_id
    )
    check_model_roles(recipe_path, services.provider_access)
    request = build_run_request(recipe_path, context_vars, services, coordinator)
    resolved = await (plan or runner.plan)(request)
    return closed_world_scope(resolved, coordinator, recipe_path)


async def run_v2_recipe_in_session(
    coordinator: Any,
    session_manager: Any,
    recipe_path: Path,
    context_vars: Mapping[str, Any] | None,
    project_path: Path,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    resume_engine_session_id: str | None = None,
    on_engine_session: Callable[[str | None], None] | None = None,
    plan: Callable[..., Awaitable[Any]] | None = None,
    engine: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    """Execute a schema-v2 recipe **in this session**, closed-world.

    The split this function embodies:

    * **Resolution** is the library's, unchanged. ``plan()`` parses the
      manifest, applies trust policy, resolves the declared closure, detects
      collisions and refuses an undeclared agent -- all before anything runs
      (lib Core 1, Core 2, Core 8).
    * **Step machinery** is the legacy engine's: ``bash``, ``parse_json``,
      ``foreach``, ``while``, conditions, staged approvals, sub-recipes. The
      library's own sequential executor runs agent steps only, so routing real
      recipes through it made them fail at their first ``bash`` step
      (recipes-lc7). The contracts constrain which *agents* a recipe may
      resolve, not which step types an engine understands.

    The bridge between the two is :class:`~.closed_world.ClosedWorldCoordinator`:
    the engine runs against a view of this session whose ``agents`` map **is**
    the plan's catalog and whose ``session.spawn`` refuses any name outside it.
    Providers, tools, hooks and approvals still come from the caller's session
    through its own spawn machinery -- which is how a spawned child has always
    obtained providers, and why an in-session v2 agent step does real model
    work rather than reporting "No providers available" (recipes-30w).

    Resuming uses this same function, and that is the point: a run that
    stopped at an approval gate, mid-``foreach`` or mid-sub-recipe must
    continue on the engine that ran it, not on a second one that never saw
    those step shapes (recipes-5c6). ``resume_engine_session_id`` names the
    engine session to re-enter; the engine skips what it checkpointed there.

    One run, one session. On a first run the engine is *attached* to the
    session the caller bound rather than left to create a second one, so the
    id the caller is handed is the id whose ``state.json`` answers "what did
    this run do?", and ``list`` shows the run once (recipes-ppu). Attaching is
    not resuming -- see ``executor.RecipeExecutor._open_run_session``.

    Args:
        session_id: the Amplifier session this run belongs to. On a first run
            the engine adopts it; when ``resume_engine_session_id`` is given
            it is the addressed session and the engine re-enters that one.
        run_id: the recorded run id to continue under, when resuming.
        resume_engine_session_id: re-enter this engine session instead of
            creating one. Its own checkpoint decides what is skipped.
        on_engine_session: called once with the engine session id (or None if
            no session was ever created), whatever the outcome. This is the
            only report of it -- see :class:`EngineSessionRecorder`.
        plan/engine: injection seams for tests. ``engine`` receives
            ``(scoped_coordinator, recipe_path, context, project_path,
            session_manager)`` plus keywords ``session_id`` (resume) and
            ``attach_session_id`` (adopt); at most one is ever non-None.

    Returns:
        The library's ``RunResult``, so every caller translates one shape. A
        preflight refusal, a paused approval gate and a failed step are each
        reported as themselves; nothing reports SUCCEEDED that did not succeed.
    """
    runner = load_runner()

    services = await build_host_services(
        coordinator, session_manager, project_path, session_id=session_id
    )
    check_model_roles(recipe_path, services.provider_access)
    request = build_run_request(
        recipe_path, context_vars, services, coordinator, run_id=run_id
    )
    run_id = request.run_id or f"run-{uuid.uuid4().hex[:12]}"

    recorder = EngineSessionRecorder(session_manager)

    # A first run checkpoints into the session the caller already bound, so the
    # run has ONE session and the id the caller was handed is the id holding
    # its state (recipes-ppu). This is an *attach*, never a resume: the engine
    # still starts at step 0 with `context_vars`. Resuming addresses the
    # engine's session directly and never attaches.
    attach_session_id = None if resume_engine_session_id else session_id

    def report_engine_session() -> str | None:
        # Order matters: when we attached, the bound session IS the engine's,
        # and `recorder` would otherwise report the first SUB-recipe session
        # the run created -- a child, not the run's identity.
        engine_session_id = (
            resume_engine_session_id or attach_session_id or recorder.session_id
        )
        if on_engine_session is not None:
            on_engine_session(engine_session_id)
        return engine_session_id

    try:
        resolved = await (plan or runner.plan)(request)
    except Exception as exc:  # noqa: BLE001 -- preflight refusals are results
        report_engine_session()
        return runner.RunResult(run_id=run_id, status=runner.RunStatus.FAILED, error=exc)

    scoped = closed_world_scope(resolved, coordinator, recipe_path)

    execute = engine or _legacy_engine_run
    try:
        final_context = await execute(
            scoped,
            recipe_path,
            dict(context_vars or {}),
            project_path,
            recorder,
            session_id=resume_engine_session_id,
            attach_session_id=attach_session_id,
        )
    except Exception as exc:  # noqa: BLE001 -- one place turns any failure into a result
        engine_session_id = report_engine_session()
        stage = getattr(exc, "stage_name", None)
        if stage is not None and type(exc).__name__ == "ApprovalGatePausedError":
            return runner.RunResult(
                run_id=run_id,
                status=runner.RunStatus.PAUSED,
                plan=resolved,
                completed_steps=_engine_completed_steps(
                    session_manager,
                    getattr(exc, "session_id", None) or engine_session_id,
                    project_path,
                ),
                pending_approval=stage,
            )
        logger.error("v2 recipe %s failed on the legacy engine: %s", recipe_path, exc)
        return runner.RunResult(
            run_id=run_id,
            status=runner.RunStatus.FAILED,
            plan=resolved,
            # What the engine checkpointed before it died -- never assumed
            # empty, or a resume would redo every step that did run.
            completed_steps=_engine_completed_steps(
                session_manager, engine_session_id, project_path
            ),
            error=exc,
        )

    engine_session_id = report_engine_session()
    engine_session = (final_context or {}).get("session") or {}
    return runner.RunResult(
        run_id=run_id,
        status=runner.RunStatus.SUCCEEDED,
        plan=resolved,
        outputs=dict(final_context or {}),
        completed_steps=_engine_completed_steps(
            session_manager, engine_session.get("id") or engine_session_id, project_path
        ),
    )


async def _legacy_engine_run(
    scoped_coordinator: Any,
    recipe_path: Path,
    context_vars: Mapping[str, Any],
    project_path: Path,
    session_manager: Any,
    *,
    session_id: str | None = None,
    attach_session_id: str | None = None,
) -> Mapping[str, Any]:
    """Run the recipe on the legacy step engine, against the scoped coordinator.

    ``session_id`` re-enters an existing engine session -- the engine's own
    resumption path, which skips what that session checkpointed.

    ``attach_session_id`` is the *other* request, and deliberately a separate
    parameter: run this recipe from the start, but checkpoint into a session
    that already exists. That is what makes a v2 run have ONE session instead
    of two (recipes-ppu). Reusing ``session_id`` for it would read as resume
    and discard the caller's context.

    With neither, the engine creates a fresh session of its own.

    Imported lazily so this module keeps importing without the engine's own
    dependencies, exactly as the library import is lazy.
    """
    from .executor import RecipeExecutor  # noqa: PLC0415
    from .models import Recipe  # noqa: PLC0415

    recipe = Recipe.from_yaml(recipe_path)
    executor = RecipeExecutor(scoped_coordinator, session_manager)
    return await executor.execute_recipe(
        recipe,
        dict(context_vars),
        project_path,
        session_id=session_id,
        recipe_path=recipe_path,
        attach_session_id=attach_session_id,
    )


def _engine_completed_steps(
    session_manager: Any, engine_session_id: str | None, project_path: Path
) -> tuple[str, ...]:
    """What the step engine *recorded* finishing -- never what we assume it did.

    The engine checkpoints completed steps into its own session state. Reading
    that back is the only honest source: inferring "all of them" from a
    successful return would claim steps that a condition skipped.
    """
    if not engine_session_id:
        return ()
    try:
        state = session_manager.load_state(engine_session_id, project_path)
    except Exception as exc:  # noqa: BLE001 - unreadable state is reported, not guessed
        logger.warning(
            "Could not read completed steps from engine session %s: %s",
            engine_session_id,
            exc,
        )
        return ()
    completed = (state or {}).get("completed_steps")
    if isinstance(completed, Sequence) and not isinstance(completed, (str, bytes)):
        return tuple(str(step) for step in completed)
    return ()


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
       (:func:`library_resume`), handed ``completed_steps`` so that it skips
       the steps the recorded run already finished --
       ``recipe-dependency-manifest.v1`` Core 8. Handing them over is the
       whole point of the route: the keyword was dropped once, and every
       completed step ran a second time under a SUCCEEDED result
       (recipes-bpx).
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
        # The steps the recorded run finished are the whole reason this route
        # exists, so they are passed explicitly rather than left to the
        # library's `completed_steps=()` default. Dropping them made a
        # partly-completed run re-execute every step it had already done,
        # reporting success -- the silent re-run `recipe-dependency-manifest.v1`
        # Core 8 forbids and this function's own docstring promises against
        # (recipes-bpx). A `resume` that does not accept the keyword fails
        # loudly here for the same reason: contract Core 2 names it, and
        # swallowing the TypeError would put the silence straight back.
        return await entry(request, completed_steps=tuple(completed_steps))

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
