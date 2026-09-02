"""Recipe manifest parsing and strict validation (``schema_version: 2``).

Implements the parse-time half of contract ``recipe-dependency-manifest.v1``:

* **Core 1** -- a portable recipe declares ``schema_version: 2`` and a
  ``dependencies`` block. A recipe declaring neither is a *legacy recipe*
  (returned as a typed :class:`LegacyRecipe` marker, never an error here).
  Unknown manifest keys are a parse ERROR, never silently ignored.
* **Core 2** -- ``dependencies`` entries are source URIs with
  ``kind: bundle`` or ``kind: behavior`` only, each optionally listing
  ``required_agents``.
* **Core 12** -- the historical ``agent_config`` step field is REJECTED at
  parse under schema 2. It is never silently retained inert.

Scope: **parsing only**. No dependency resolution, no network, no Foundation
calls, no lockfile handling. Everything here is pure and offline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from typing import Final
from typing import Literal

import yaml

__all__ = [
    "CONTRACT",
    "DEPENDENCY_KEYS",
    "DEPENDENCY_KINDS",
    "KNOWN_TOP_LEVEL_KEYS",
    "SCHEMA_VERSION",
    "Dependency",
    "DependencyKind",
    "LegacyRecipe",
    "Manifest",
    "ManifestError",
    "ParseResult",
    "parse_manifest",
    "parse_manifest_file",
    "parse_manifest_text",
]

CONTRACT: Final[str] = "recipe-dependency-manifest.v1"

#: The only schema version this parser accepts. Higher values are Reserved.
SCHEMA_VERSION: Final[int] = 2

DependencyKind = Literal["bundle", "behavior"]

#: Core 2 -- v1 permits exactly these two kinds. Anything else is Reserved.
DEPENDENCY_KINDS: Final[tuple[str, ...]] = ("bundle", "behavior")

#: Keys a dependency entry may carry.
DEPENDENCY_KEYS: Final[frozenset[str]] = frozenset({"source", "kind", "required_agents"})

#: Manifest keys introduced by schema 2.
_MANIFEST_KEYS: Final[frozenset[str]] = frozenset({"schema_version", "dependencies", "agents"})

#: Recipe-body keys that predate the manifest and remain valid under schema 2.
_RECIPE_BODY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "description",
        "version",
        "author",
        "created",
        "updated",
        "tags",
        "context",
        "steps",
        "stages",
        "recursion",
        "rate_limiting",
        "orchestrator",
    }
)

#: Every top-level key a schema-2 recipe may declare. Anything else is a
#: parse ERROR naming the offending key (Core 1).
KNOWN_TOP_LEVEL_KEYS: Final[frozenset[str]] = _MANIFEST_KEYS | _RECIPE_BODY_KEYS


class ManifestError(ValueError):
    """A recipe manifest violated the contract at parse time.

    The message always names the offending key (or step id) and the contract
    clause that rejects it, so the failure is actionable without reading this
    module.
    """

    def __init__(self, message: str, *, clause: str | None = None, source: str | None = None) -> None:
        self.detail = message
        self.clause = clause
        self.source = source
        prefix = f"{source}: " if source else ""
        suffix = f" [{CONTRACT} {clause}]" if clause else ""
        super().__init__(f"{prefix}{message}{suffix}")


@dataclass(frozen=True, slots=True)
class Dependency:
    """One declared, Foundation-resolvable dependency (Core 2).

    ``source`` is carried verbatim -- resolution happens elsewhere.
    """

    source: str
    kind: DependencyKind
    required_agents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Manifest:
    """A parsed ``schema_version: 2`` manifest."""

    schema_version: int
    dependencies: tuple[Dependency, ...]
    agents: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    source: str | None = None

    @property
    def is_legacy(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class LegacyRecipe:
    """Typed marker: this recipe declares no manifest (Core 1, Core 10).

    Not an error. Legacy handling lives elsewhere -- this module only reports
    that the recipe is legacy and why.
    """

    reason: str
    source: str | None = None

    @property
    def is_legacy(self) -> bool:
        return True


ParseResult = Manifest | LegacyRecipe


def parse_manifest_file(path: str | Path) -> ParseResult:
    """Parse a recipe YAML file's manifest. See :func:`parse_manifest`."""
    p = Path(path)
    return parse_manifest_text(p.read_text(encoding="utf-8"), source=str(p))


def parse_manifest_text(text: str, *, source: str | None = None) -> ParseResult:
    """Parse recipe YAML text's manifest. See :func:`parse_manifest`."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough detail
        raise ManifestError(f"recipe is not valid YAML: {exc}", source=source) from exc
    return parse_manifest(data, source=source)


def parse_manifest(data: Any, *, source: str | None = None) -> ParseResult:
    """Parse an already-loaded recipe mapping into a typed manifest.

    Returns a :class:`Manifest` for ``schema_version: 2`` recipes, or a
    :class:`LegacyRecipe` marker for recipes declaring no manifest at all.

    Raises:
        ManifestError: on any contract violation -- unknown top-level or
            dependency key, malformed ``dependencies``/``agents``, an
            unsupported ``schema_version``, or an ``agent_config`` step field.
    """
    if not isinstance(data, Mapping):
        raise ManifestError(
            f"recipe must be a YAML mapping at the top level, got {type(data).__name__}",
            source=source,
        )

    if "schema_version" not in data:
        # Core 1 + Core 10: no manifest declared. Declaring manifest keys
        # WITHOUT the version is not legacy -- it is a version that was
        # forgotten, and silently ignoring the block is exactly what Core 1
        # forbids.
        stray = sorted(k for k in _MANIFEST_KEYS if k in data)
        if stray:
            raise ManifestError(
                f"manifest key(s) {_fmt(stray)} declared without 'schema_version'; "
                f"add 'schema_version: {SCHEMA_VERSION}' or remove them",
                clause="Core 1",
                source=source,
            )
        return LegacyRecipe(
            reason="no 'schema_version' declared; recipe is legacy",
            source=source,
        )

    _check_schema_version(data["schema_version"], source=source)
    _check_top_level_keys(data, source=source)
    _reject_agent_config(data, source=source)

    if "dependencies" not in data:
        raise ManifestError(
            f"schema_version {SCHEMA_VERSION} requires a 'dependencies' block "
            "(use 'dependencies: []' to declare none)",
            clause="Core 1",
            source=source,
        )

    dependencies = _parse_dependencies(data["dependencies"], source=source)
    agents = _parse_agent_aliases(data.get("agents"), source=source)

    return Manifest(
        schema_version=SCHEMA_VERSION,
        dependencies=dependencies,
        agents=agents,
        source=source,
    )


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _fmt(names: list[str]) -> str:
    return ", ".join(repr(n) for n in names)


def _check_schema_version(value: Any, *, source: str | None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(
            f"'schema_version' must be the integer {SCHEMA_VERSION}, got {value!r}",
            clause="Core 1",
            source=source,
        )
    if value > SCHEMA_VERSION:
        raise ManifestError(
            f"'schema_version' {value} is Reserved; this runner supports {SCHEMA_VERSION}",
            clause="Reserved",
            source=source,
        )
    if value != SCHEMA_VERSION:
        raise ManifestError(
            f"'schema_version' {value} is not supported; a portable recipe declares "
            f"'schema_version: {SCHEMA_VERSION}' (omit it entirely for a legacy recipe)",
            clause="Core 1",
            source=source,
        )


def _check_top_level_keys(data: Mapping[str, Any], *, source: str | None) -> None:
    unknown = sorted(str(k) for k in data if k not in KNOWN_TOP_LEVEL_KEYS)
    if unknown:
        raise ManifestError(
            f"unknown top-level manifest key(s): {_fmt(unknown)}; allowed keys are "
            f"{_fmt(sorted(KNOWN_TOP_LEVEL_KEYS))}",
            clause="Core 1",
            source=source,
        )


def _parse_dependencies(value: Any, *, source: str | None) -> tuple[Dependency, ...]:
    if not isinstance(value, list):
        raise ManifestError(
            f"'dependencies' must be a list, got {type(value).__name__}",
            clause="Core 2",
            source=source,
        )

    parsed: list[Dependency] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(value):
        where = f"dependencies[{index}]"
        if not isinstance(entry, Mapping):
            raise ManifestError(
                f"{where} must be a mapping with 'source' and 'kind', got {type(entry).__name__}",
                clause="Core 2",
                source=source,
            )

        unknown = sorted(str(k) for k in entry if k not in DEPENDENCY_KEYS)
        if unknown:
            raise ManifestError(
                f"{where}: unknown dependency key(s): {_fmt(unknown)}; allowed keys are "
                f"{_fmt(sorted(DEPENDENCY_KEYS))}",
                clause="Core 1",
                source=source,
            )

        dep_source = entry.get("source")
        if not isinstance(dep_source, str) or not dep_source.strip():
            raise ManifestError(
                f"{where}: 'source' is required and must be a non-empty string, got {dep_source!r}",
                clause="Core 2",
                source=source,
            )

        kind = entry.get("kind")
        if kind not in DEPENDENCY_KINDS:
            raise ManifestError(
                f"{where}: 'kind' must be one of {_fmt(list(DEPENDENCY_KINDS))}, got {kind!r}",
                clause="Core 2",
                source=source,
            )

        required_agents = _parse_required_agents(entry.get("required_agents"), where=where, source=source)

        if dep_source in seen:
            raise ManifestError(
                f"{where}: duplicate dependency source {dep_source!r} "
                f"(already declared at dependencies[{seen[dep_source]}])",
                clause="Core 2",
                source=source,
            )
        seen[dep_source] = index

        parsed.append(Dependency(source=dep_source, kind=kind, required_agents=required_agents))

    return tuple(parsed)


def _parse_required_agents(value: Any, *, where: str, source: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestError(
            f"{where}: 'required_agents' must be a list of agent names, got {type(value).__name__}",
            clause="Core 2",
            source=source,
        )
    agents: list[str] = []
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(
                f"{where}: 'required_agents' entries must be non-empty strings, got {name!r}",
                clause="Core 2",
                source=source,
            )
        agents.append(name)
    return tuple(agents)


def _parse_agent_aliases(value: Any, *, source: str | None) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ManifestError(
            f"'agents' must be a mapping of alias -> 'namespace:name', got {type(value).__name__}",
            clause="Core 3",
            source=source,
        )

    aliases: dict[str, str] = {}
    for alias, canonical in value.items():
        if not isinstance(alias, str) or not alias.strip():
            raise ManifestError(
                f"'agents' alias must be a non-empty string, got {alias!r}",
                clause="Core 3",
                source=source,
            )
        if ":" in alias:
            raise ManifestError(
                f"'agents' alias {alias!r} must not contain ':' -- an alias is a bare name, "
                "the value carries the canonical 'namespace:name'",
                clause="Core 3",
                source=source,
            )
        if not isinstance(canonical, str) or canonical.count(":") != 1 or not all(canonical.split(":")):
            raise ManifestError(
                f"'agents' alias {alias!r} must map to a canonical 'namespace:name', got {canonical!r}",
                clause="Core 3",
                source=source,
            )
        aliases[alias] = canonical

    return MappingProxyType(aliases)


def _reject_agent_config(data: Mapping[str, Any], *, source: str | None) -> None:
    """Core 12: reject the historical ``agent_config`` step field at parse.

    Walks flat steps, staged steps, and nested foreach/while step bodies.
    Structural step validation is out of scope -- non-mapping entries are
    skipped rather than diagnosed here.
    """
    for step, path in _walk_steps(data):
        if "agent_config" in step:
            step_id = step.get("id")
            named = f"step {step_id!r}" if isinstance(step_id, str) and step_id else f"step at {path}"
            raise ManifestError(
                f"{named} declares 'agent_config', which is rejected under "
                f"schema_version {SCHEMA_VERSION}: it must be resolved, never silently "
                "retained inert. Declare the agent's dependency in 'dependencies' instead",
                clause="Core 12",
                source=source,
            )


def _walk_steps(data: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], str]]:
    found: list[tuple[Mapping[str, Any], str]] = []

    def visit_steps(steps: Any, path: str) -> None:
        if not isinstance(steps, list):
            return
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                continue
            here = f"{path}[{index}]"
            found.append((step, here))
            # foreach / while bodies nest their steps under 'steps'
            visit_steps(step.get("steps"), f"{here}.steps")

    visit_steps(data.get("steps"), "steps")

    stages = data.get("stages")
    if isinstance(stages, list):
        for index, stage in enumerate(stages):
            if isinstance(stage, Mapping):
                visit_steps(stage.get("steps"), f"stages[{index}].steps")

    return found
