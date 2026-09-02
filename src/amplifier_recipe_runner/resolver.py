"""Dependency resolution -- the injectable seam between the planner and disk.

Contract: ``recipe-runner-lib.v1`` Core 5::

    The library owns a resolver interface with injectable policy. The default
    implementation uses Foundation's ``BundleRegistry`` under a runner
    namespace within the standard Amplifier cache root. Cache location is NOT
    part of the public semantic contract; embedders may inject registry/cache
    policy (mirrors, offline, isolation).

The planner never constructs a registry and never touches the network: it
receives a :class:`DependencyResolver` and asks it, once per *declared*
dependency, "what does this source contain, and what exactly did you read?".
That question -- and its provenance-carrying answer -- is the whole seam.

Two implementations ship here:

* :class:`FoundationResolver` -- the default. Wraps Foundation's
  ``BundleRegistry``, composing includes exactly as Amplifier would.
  Foundation is imported *lazily*, inside methods, so this module (and the
  planner, and the tests) import cleanly without it.
* :class:`LocalBundleResolver` -- an offline resolver for local path /
  ``file://`` sources. It reads one bundle file and follows no includes; a
  local bundle that *declares* includes is refused by name rather than
  silently under-reported (see :class:`DependencyResolutionError`).

Neither resolver activates modules, starts sessions, or executes anything.
Resolution reads bundle definitions; that is all.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from typing import Final
from typing import Protocol
from typing import runtime_checkable

import yaml

from .errors import PreflightError
from .manifest import Dependency

__all__ = [
    "DEFAULT_RUNNER_NAMESPACE",
    "DependencyResolutionError",
    "DependencyResolver",
    "FoundationResolver",
    "LocalBundleResolver",
    "ResolvedAgent",
    "ResolvedBundle",
    "canonical_agent_name",
    "split_source",
]

#: Sub-directory of the Amplifier home the default resolver caches under.
#: Cache location is explicitly NOT part of the public semantic contract
#: (lib Core 5) -- this constant exists so embedders can *see* the default,
#: not so they must match it.
DEFAULT_RUNNER_NAMESPACE: Final[str] = "recipe-runner"

_BUNDLE_FILENAMES: Final[tuple[str, ...]] = ("bundle.md", "bundle.yaml", "bundle.yml")
_BEHAVIOR_SUFFIXES: Final[tuple[str, ...]] = (".yaml", ".yml", ".md")

#: ``git+https://host/org/repo@ref`` -- the ``@ref`` is the *requested* ref,
#: never the resolved revision.
_REF_RE: Final[re.Pattern[str]] = re.compile(r"@(?P<ref>[^@/#]+)$")


class DependencyResolutionError(PreflightError):
    """A declared dependency source could not be resolved to a bundle.

    A :class:`PreflightError` because resolution happens in preflight: a
    source that cannot be read is a real failure surfaced *before* any step
    runs, never a silently empty closure (lib Core 8).
    """

    def __init__(
        self,
        source: str,
        reason: str,
        *,
        remedy: str | None = None,
    ) -> None:
        self.source = source
        self.reason = reason
        super().__init__(
            f"Dependency {source!r} could not be resolved: {reason}",
            remedy=remedy or "Check the dependency's `source` in the recipe's `dependencies` block.",
        )


# --------------------------------------------------------------------------
# Resolution results -- the provenance-carrying answer
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedAgent:
    """One agent a dependency supplies, with where it was read from."""

    name: str
    """Canonical ``namespace:name``."""

    local_path: str | None = None
    """Path to the agent definition file, when it has one."""

    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    """Whatever the resolver could read about the agent. Advisory."""


@dataclass(frozen=True, slots=True)
class ResolvedBundle:
    """What a resolver read for one declared dependency.

    Every field except :attr:`agents` is provenance (manifest Core 7): the
    declared URI, what ref was asked for, what immutable revision or content
    digest was actually obtained, and where it landed on disk.
    """

    source: str
    """The declared source URI, verbatim -- never normalised or rewritten."""

    kind: str
    """``"bundle"`` or ``"behavior"``, from the declaration."""

    namespace: str
    """Bundle name; the namespace its agents are canonicalised under."""

    agents: Mapping[str, ResolvedAgent] = field(default_factory=lambda: MappingProxyType({}))
    """Canonical ``namespace:name`` -> agent. ONLY this dependency's
    contribution -- a behavior partial contributes only what it declares."""

    local_path: str | None = None
    resolved_revision: str | None = None
    """Immutable revision (e.g. a commit sha) for git sources; ``None`` for
    local sources, which record :attr:`content_digest` instead."""

    content_digest: str | None = None
    requested_ref: str | None = None
    subdirectory: str | None = None
    version: str | None = None


@runtime_checkable
class DependencyResolver(Protocol):
    """Turns one declared dependency into a :class:`ResolvedBundle`.

    This is the injection point named by lib Core 5. The planner depends on
    this protocol and nothing else, so an embedder can supply an offline
    cache, a mirror, or a test double without the planner changing.

    Implementations must not execute recipe steps or activate modules.
    """

    async def resolve(
        self,
        dependency: Dependency,
        *,
        workspace: Path | None = None,
    ) -> ResolvedBundle:
        """Read ``dependency``'s source and report what it contains.

        Raise :class:`DependencyResolutionError` when the source cannot be
        read -- an unreadable dependency is a real failure, never an empty
        closure.
        """
        ...


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def canonical_agent_name(name: str, namespace: str) -> str:
    """Return ``namespace:name`` for a bare name; pass through a namespaced one.

    Bundles usually declare agents already namespaced (``recipes:recipe-author``);
    a bare name belongs to the declaring bundle's own namespace.
    """
    return name if ":" in name else f"{namespace}:{name}"


def split_source(source: str) -> tuple[str, str | None, str | None]:
    """Split ``source`` into ``(base, subdirectory, requested_ref)``.

    ``base`` keeps its ``@ref`` suffix so it stays a usable URI; the ref is
    reported separately because Core 7 records the *requested* ref alongside
    the *resolved* revision.
    """
    base, _, fragment = source.partition("#")
    subdirectory: str | None = None
    if fragment:
        for part in fragment.split("&"):
            key, _, value = part.partition("=")
            if key == "subdirectory" and value:
                subdirectory = value
    match = _REF_RE.search(base)
    ref = match.group("ref") if match else None
    return base, subdirectory, ref


def _digest_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_file(path: Path) -> str | None:
    try:
        return _digest_text(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _parse_bundle_file(path: Path) -> dict[str, Any]:
    """Load a bundle definition from ``.md`` frontmatter or a ``.yaml`` body."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DependencyResolutionError(str(path), f"cannot read bundle file: {exc}") from exc

    if path.suffix == ".md":
        if not text.lstrip().startswith("---"):
            raise DependencyResolutionError(
                str(path),
                "markdown bundle has no YAML frontmatter block",
                remedy="Add a `---` delimited frontmatter block declaring `bundle:`.",
            )
        stripped = text.lstrip()
        end = stripped.find("\n---", 3)
        if end == -1:
            raise DependencyResolutionError(str(path), "unterminated YAML frontmatter block")
        text = stripped[3:end]

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DependencyResolutionError(str(path), f"bundle file is not valid YAML: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise DependencyResolutionError(str(path), f"bundle definition must be a mapping, got {type(data).__name__}")
    return dict(data)


def _declared_agent_names(data: Mapping[str, Any]) -> list[str]:
    """Agent names a bundle definition declares, in declaration order.

    Mirrors Foundation's roster parsing: an ``include:`` list, plus inline
    mapping entries. The ``agents: all | none | [...]`` access-control form is
    not a roster and contributes nothing.
    """
    agents = data.get("agents")
    if not isinstance(agents, Mapping):
        return []

    names: list[str] = []
    include = agents.get("include")
    if isinstance(include, list):
        names.extend(str(n) for n in include if isinstance(n, str))
    for key, value in agents.items():
        if key != "include" and isinstance(value, Mapping):
            names.append(str(key))
    return names


def _agent_file(resource_root: Path, canonical: str) -> Path | None:
    simple = canonical.split(":", 1)[-1]
    candidate = resource_root / "agents" / f"{simple}.md"
    return candidate if candidate.exists() else None


def _agent_metadata(path: Path | None, name: str) -> Mapping[str, Any]:
    """Best-effort description for an agent file. Advisory, never load-bearing."""
    meta: dict[str, Any] = {"name": name}
    if path is None or path.suffix != ".md":
        return MappingProxyType(meta)
    try:
        data = _parse_bundle_file(path)
    except DependencyResolutionError:
        return MappingProxyType(meta)
    block = data.get("meta") if isinstance(data.get("meta"), Mapping) else {}
    description = block.get("description") or data.get("description")
    if isinstance(description, str):
        meta["description"] = description
    return MappingProxyType(meta)


async def _git_revision(path: Path) -> str | None:
    """Resolved commit sha of a git checkout, or ``None`` if it is not one."""
    if not (path / ".git").exists() and not any((p / ".git").exists() for p in path.parents):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(path),
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except (OSError, ValueError):  # pragma: no cover - git absent
        return None
    if proc.returncode != 0:
        return None
    revision = stdout.decode("utf-8", "replace").strip()
    return revision or None


def _local_candidate(source: str, workspace: Path | None, base_path: Path | None) -> Path | None:
    """Interpret ``source`` as a local path, or return ``None``."""
    base, _, _ = split_source(source)
    if base.startswith("file://"):
        base = base[len("file://") :]
    elif "://" in base or base.startswith("git+"):
        return None
    path = Path(os.path.expanduser(base))
    if path.is_absolute():
        return path
    for root in (base_path, workspace, Path.cwd()):
        if root is not None:
            candidate = (root / path).resolve()
            if candidate.exists():
                return candidate
    return None


def _bundle_file_for(root: Path, subdirectory: str | None, source: str) -> Path:
    """Locate the bundle/behavior definition file under ``root``."""
    target = root if subdirectory is None else root / subdirectory

    if target.is_file():
        return target
    if not target.exists():
        for suffix in _BEHAVIOR_SUFFIXES:
            with_suffix = target.with_name(target.name + suffix)
            if with_suffix.is_file():
                return with_suffix
        raise DependencyResolutionError(
            source,
            f"no such path: {target}",
            remedy="Point `source` at an existing bundle file or directory.",
        )
    for name in _BUNDLE_FILENAMES:
        candidate = target / name
        if candidate.is_file():
            return candidate
    raise DependencyResolutionError(
        source,
        f"no bundle file ({', '.join(_BUNDLE_FILENAMES)}) under {target}",
        remedy="Add a bundle.md/bundle.yaml, or point `source` directly at the behavior file.",
    )


# --------------------------------------------------------------------------
# Offline resolver
# --------------------------------------------------------------------------


class LocalBundleResolver:
    """Reads one local bundle or behavior file. No network, no composition.

    Useful as an embedder-injected offline policy (lib Core 5's "offline,
    isolation" case) and as the resolver the planner's own tests run against.

    **Includes are refused, not ignored.** Following ``includes:`` is
    Foundation's job; a local bundle that declares them would resolve to a
    *smaller* closure here than it would in production, so this resolver
    fails loud and names them rather than under-reporting.
    """

    def __init__(self, *, base_path: Path | None = None, allow_includes: bool = False) -> None:
        self._base_path = base_path
        self._allow_includes = allow_includes

    async def resolve(
        self,
        dependency: Dependency,
        *,
        workspace: Path | None = None,
    ) -> ResolvedBundle:
        source = dependency.source
        _, subdirectory, requested_ref = split_source(source)
        root = _local_candidate(source, workspace, self._base_path)
        if root is None:
            raise DependencyResolutionError(
                source,
                "not a local path; this resolver is offline",
                remedy="Inject FoundationResolver (the default) to resolve remote sources.",
            )

        bundle_file = _bundle_file_for(root, subdirectory, source)
        data = _parse_bundle_file(bundle_file)
        meta = data.get("bundle") if isinstance(data.get("bundle"), Mapping) else {}

        includes = data.get("includes")
        if includes and not self._allow_includes:
            raise DependencyResolutionError(
                source,
                f"declares {len(includes)} include(s) this offline resolver does not compose",
                remedy=("Resolve this dependency with FoundationResolver, which composes includes as Amplifier does."),
            )

        namespace = str(meta.get("name") or bundle_file.parent.name)
        namespace_root = meta.get("namespace_root")
        resource_root = bundle_file.parent
        if isinstance(namespace_root, str) and namespace_root:
            resource_root = (bundle_file.parent / namespace_root).resolve()

        agents: dict[str, ResolvedAgent] = {}
        for declared in _declared_agent_names(data):
            canonical = canonical_agent_name(declared, namespace)
            agent_path = _agent_file(resource_root, canonical)
            agents[canonical] = ResolvedAgent(
                name=canonical,
                local_path=str(agent_path) if agent_path else None,
                metadata=_agent_metadata(agent_path, canonical),
            )

        return ResolvedBundle(
            source=source,
            kind=dependency.kind,
            namespace=namespace,
            agents=MappingProxyType(agents),
            local_path=str(resource_root),
            resolved_revision=await _git_revision(resource_root),
            content_digest=_digest_file(bundle_file),
            requested_ref=requested_ref,
            subdirectory=subdirectory,
            version=str(meta["version"]) if isinstance(meta.get("version"), (str, int, float)) else None,
        )


# --------------------------------------------------------------------------
# Default resolver (Foundation-backed)
# --------------------------------------------------------------------------


class FoundationResolver:
    """The default resolver: Foundation's ``BundleRegistry`` (lib Core 5).

    Composes includes exactly as Amplifier does, so a recipe's closure is the
    same closure a bundle would produce in a session. The registry lives under
    a runner namespace inside the Amplifier home
    (``$AMPLIFIER_HOME/recipe-runner``) so recipe resolution never disturbs the
    host's own bundle state -- but the location is a default, not a promise
    (lib Core 5 excludes cache location from the semantic contract).

    ``amplifier_foundation`` is imported lazily, inside :meth:`resolve`, so the
    library remains importable (and the planner testable) without it.
    """

    def __init__(
        self,
        *,
        home: Path | None = None,
        namespace: str = DEFAULT_RUNNER_NAMESPACE,
        registry: Any | None = None,
    ) -> None:
        self._home = home
        self._namespace = namespace
        self._registry = registry

    def _get_registry(self) -> Any:
        if self._registry is not None:
            return self._registry
        try:
            from amplifier_foundation.paths.resolution import get_amplifier_home
            from amplifier_foundation.registry import BundleRegistry
        except ImportError as exc:  # pragma: no cover - depends on install
            raise DependencyResolutionError(
                "<amplifier-foundation>",
                f"amplifier-foundation is not importable: {exc}",
                remedy=("Install amplifier-foundation, or inject a resolver (e.g. LocalBundleResolver) instead."),
            ) from exc

        home = self._home if self._home is not None else Path(get_amplifier_home()) / self._namespace
        self._registry = BundleRegistry(home=home)
        return self._registry

    async def resolve(
        self,
        dependency: Dependency,
        *,
        workspace: Path | None = None,
    ) -> ResolvedBundle:
        source = dependency.source
        _, subdirectory, requested_ref = split_source(source)
        registry = self._get_registry()

        target = source
        local = _local_candidate(source, workspace, None)
        if local is not None:
            # A local path with a ``#subdirectory=`` fragment is not something
            # Foundation's source resolver interprets -- it only means "inside
            # a fetched repo". Resolve it to the concrete bundle file here so
            # a local behavior partial loads exactly like a remote one.
            target = str(_bundle_file_for(local, subdirectory, source))

        try:
            bundle = await registry.load(target)
        except Exception as exc:  # registry raises its own exception hierarchy
            raise DependencyResolutionError(source, f"{type(exc).__name__}: {exc}") from exc

        if isinstance(bundle, dict):  # pragma: no cover - only for a None argument
            raise DependencyResolutionError(source, "registry returned a bundle set, not a single bundle")

        # Fill in descriptions/metadata from each agent's own file.
        bundle.load_agent_metadata()

        namespace = str(getattr(bundle, "name", "") or "")
        base_path = getattr(bundle, "base_path", None)
        source_base_paths = getattr(bundle, "source_base_paths", {}) or {}

        agents: dict[str, ResolvedAgent] = {}
        for declared, config in (getattr(bundle, "agents", {}) or {}).items():
            canonical = canonical_agent_name(str(declared), namespace)
            resolved_path = None
            try:
                resolved_path = bundle.resolve_agent_path(str(declared))
            except Exception:  # noqa: BLE001 - path resolution is best effort
                resolved_path = None
            agents[canonical] = ResolvedAgent(
                name=canonical,
                local_path=str(resolved_path) if resolved_path else None,
                metadata=MappingProxyType(dict(config) if isinstance(config, Mapping) else {"name": canonical}),
            )

        resource_root = Path(source_base_paths.get(namespace, base_path)) if (base_path or source_base_paths) else None
        revision = await _git_revision(resource_root) if resource_root else None
        digest = None
        if resource_root is not None:
            for name in _BUNDLE_FILENAMES:
                candidate = resource_root / name
                if candidate.is_file():
                    digest = _digest_file(candidate)
                    break

        return ResolvedBundle(
            source=source,
            kind=dependency.kind,
            namespace=namespace,
            agents=MappingProxyType(agents),
            local_path=str(resource_root) if resource_root else None,
            resolved_revision=revision,
            content_digest=digest,
            requested_ref=requested_ref,
            subdirectory=subdirectory,
            version=str(getattr(bundle, "version", "") or "") or None,
        )
