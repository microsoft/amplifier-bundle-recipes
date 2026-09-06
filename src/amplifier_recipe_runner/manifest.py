"""Recipe manifest parsing and strict validation (``schema_version: 2``).

Implements the parse-time half of contract ``recipe-dependency-manifest.v1``:

* **Core 1** -- a portable recipe declares ``schema_version: 2`` and a
  ``dependencies`` block. A recipe declaring neither is a *legacy recipe*
  (returned as a typed :class:`LegacyRecipe` marker, never an error here).
  Unknown manifest keys are a parse ERROR, never silently ignored.
* **Core 2** -- ``dependencies`` entries are source URIs with
  ``kind: bundle`` or ``kind: behavior`` only, each optionally listing
  ``required_agents``.
* **Core 9** -- an optional top-level ``capabilities`` list is the recipe's own
  term of the effective-capability intersection (host policy ∩ runner policy ∩
  manifest-declared needs). Absent means the recipe declares *none*: an
  intersection can never add, so a recipe that asks for nothing is granted
  nothing. That is the same empty-term semantics
  :func:`~amplifier_recipe_runner.trust.intersect_capabilities` already
  implements, and it is deliberately distinct from "unconstrained" -- which
  only the *host* and *runner* terms can express, via ``None``.
* **Core 12** -- the historical ``agent_config`` step field is REJECTED at
  parse under schema 2. It is never silently retained inert.
* **Core 1, applied to stages** -- the flat stage keys ``approval_required`` /
  ``approval_message`` / ``auto_approve_if`` are REJECTED at parse by name.
  They read as a human checkpoint and are not stage fields, so accepting them
  quietly runs a declared approval gate ungated.
* **Core 1, applied to every step and stage key** -- a key no engine reads is
  REJECTED at parse, naming the offending key, the step or stage it sits on,
  and the valid keys. An unread key is not inert: ``condition:`` on a STAGE
  read as "skip this stage" and was dropped, so the stage ran unconditionally
  (recipes-juc); ``requires_approval:`` on a STEP read as a human checkpoint
  and is not a step field at all (recipes-dna).

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
    "CONTEXT_DECLARATION_KEYS",
    "CONTEXT_DECLARED_TYPES",
    "CONTRACT",
    "DEPENDENCY_KEYS",
    "DEPENDENCY_KINDS",
    "FLAT_STAGE_APPROVAL_KEYS",
    "FLAT_STEP_APPROVAL_KEYS",
    "KNOWN_STAGE_KEYS",
    "KNOWN_STEP_KEYS",
    "KNOWN_TOP_LEVEL_KEYS",
    "REJECTED_STAGE_KEYS",
    "SCHEMA_VERSION",
    "Dependency",
    "DependencyKind",
    "LegacyRecipe",
    "Manifest",
    "ManifestError",
    "ParseResult",
    "ResolvedContext",
    "check_context_block",
    "is_context_declaration",
    "parse_manifest",
    "parse_manifest_file",
    "parse_manifest_text",
    "resolve_context_block",
    "unknown_stage_key_error",
    "unknown_step_key_error",
]

CONTRACT: Final[str] = "recipe-dependency-manifest.v1"

#: The only schema version this parser accepts. Higher values are Reserved.
SCHEMA_VERSION: Final[int] = 2

DependencyKind = Literal["bundle", "behavior"]

#: Core 2 -- v1 permits exactly these two kinds. Anything else is Reserved.
DEPENDENCY_KINDS: Final[tuple[str, ...]] = ("bundle", "behavior")

#: Keys a dependency entry may carry.
DEPENDENCY_KEYS: Final[frozenset[str]] = frozenset({"source", "kind", "required_agents"})

#: Keys a schema-form ``context:`` entry may carry. A non-empty mapping whose
#: keys are all drawn from this set is a DECLARATION, not a value: the runner
#: binds its ``default:``, never the mapping itself. See
#: :func:`resolve_context_block`.
CONTEXT_DECLARATION_KEYS: Final[frozenset[str]] = frozenset({"type", "required", "default", "description", "enum"})

#: Accepted values of a declaration's ``type:``. Documentation only -- nothing
#: coerces a value -- but a typo is still worth naming.
CONTEXT_DECLARED_TYPES: Final[frozenset[str]] = frozenset(
    {"string", "number", "integer", "boolean", "array", "object", "any"}
)

#: Manifest keys introduced by schema 2.
_MANIFEST_KEYS: Final[frozenset[str]] = frozenset({"schema_version", "dependencies", "agents", "capabilities"})

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

#: Flat stage-level approval keys that read like a human checkpoint and are
#: not stage fields. ``docs/RECIPE_SCHEMA.md`` ("Stage Object") documents
#: exactly one gate -- the ``approval:`` block -- so each of these was parsed
#: by nobody and dropped without a word. Each maps to the remedy that actually
#: works; ``auto_approve_if`` has no equivalent at all, so its remedy says so
#: rather than inventing a field no engine here can evaluate.
FLAT_STAGE_APPROVAL_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "approval_required": "use 'approval: {required: <bool>, prompt: <text>}'",
        "approval_message": "use 'approval: {required: true, prompt: <text>}'",
        "auto_approve_if": (
            "there is no conditional auto-approval in this schema -- remove it, "
            "and gate the stage with 'approval: {required: true, prompt: <text>}' "
            "if the checkpoint is real"
        ),
    }
)


#: Every YAML key a STEP may carry. A step key outside this set is a parse
#: ERROR naming the offending key, the step, and this list (Core 1).
#:
#: The set is the legacy ``Step`` dataclass's own YAML surface -- every field
#: name, plus the three YAML spellings of the renamed ones (``as`` ->
#: ``as_var``, ``context`` -> ``step_context``, ``steps`` -> ``while_steps``;
#: both spellings load, and ``while_steps:`` is documented as a step key) --
#: PLUS the two extra instruction aliases this library reads
#: (:data:`.engine.INSTRUCTION_KEYS`). ``modules/tool-recipes``'s
#: ``KNOWN_STEP_KEYS`` derives the same set from the dataclass directly, and
#: ``tests/test_unknown_step_and_stage_keys.py`` pins the two together,
#: difference included: a key is unknown to both engines or to neither.
KNOWN_STEP_KEYS: Final[frozenset[str]] = frozenset(
    {
        # identity / dispatch
        "id",
        "type",
        # agent steps
        "agent",
        "prompt",
        "instruction",  # library-only alias for 'prompt'
        "message",  # library-only alias for 'prompt'
        "mode",
        "agent_config",
        "provider",
        "model",
        "provider_preferences",
        "model_role",
        "spawn_mode",
        # recipe steps
        "recipe",
        "context",
        "step_context",  # the dataclass spelling of 'context'
        "recursion",
        # bash steps
        "command",
        "cwd",
        "env",
        "output_exit_code",
        # common
        "output",
        "condition",
        "foreach",
        "as",
        "as_var",  # the dataclass spelling of 'as'
        "collect",
        "parallel",
        "checkpoint_iterations",
        "max_iterations",
        "timeout",
        "retry",
        "on_error",
        "depends_on",
        "parse_json",
        # loops
        "while_condition",
        "max_while_iterations",
        "break_when",
        "update_context",
        "steps",
        "while_steps",  # the dataclass spelling of a loop body's 'steps'
    }
)

#: Step-level approval keys -> the remedy that actually works. Approval gates
#: are a STAGED-mode feature (``stages[].approval``); none of these is a step
#: field, so a step declaring one has never gated anything. ``README.md``
#: documented exactly this shape (recipes-dna), which is why each is named
#: individually rather than folded into the generic unknown-key message.
FLAT_STEP_APPROVAL_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "requires_approval": (
            "approval gates are a staged-mode feature -- put the step in a "
            "'stages:' block and gate the stage with "
            "'approval: {required: true, prompt: <text>, when: before_stage}'"
        ),
        "approval_message": (
            "the gate's text is the stage's 'approval: {prompt: <text>}' -- "
            "a step has no approval prompt"
        ),
        "approval": (
            "'approval:' belongs to a STAGE, not a step -- move it up one "
            "level onto the stage that contains this step"
        ),
    }
)

#: Every YAML key a STAGE may carry. ``description`` is documentation the
#: parsers record and no engine executes; the other three are structural.
#: Anything else is a parse ERROR (Core 1).
KNOWN_STAGE_KEYS: Final[frozenset[str]] = frozenset({"name", "steps", "approval", "description"})

#: Stage keys that read as behaviour, are read by nobody, and therefore get a
#: named remedy of their own rather than the generic unknown-key message.
#: ``condition:`` is the measured one (recipes-juc): three stages of
#: ``examples/context-intelligence/verification/adversarial-verification.yaml``
#: declared it and documented themselves as "SKIPPED when continue_from is
#: provided" -- and ran every time.
REJECTED_STAGE_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "condition": (
            "a stage has no condition -- put 'condition:' on each of the "
            "stage's steps, which both engines evaluate and record as skipped"
        ),
    }
)


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
    capabilities: tuple[str, ...] = ()
    """Capabilities the recipe declares it needs -- the manifest term of the
    Core 9 intersection, in declaration order.

    Empty means the recipe declared none, which is the same thing as declaring
    ``capabilities: []``: nothing is granted. A manifest has no way to say
    "unconstrained", by design -- an intersection term that could widen the
    host's or runner's grant would not be an intersection.
    """

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


@dataclass(frozen=True, slots=True)
class ResolvedContext:
    """A recipe's ``context:`` block read into values plus diagnostics."""

    values: Mapping[str, Any]
    """Variable -> value, every declaration replaced by its ``default:``.
    A declaration with no default is ABSENT, not ``None``."""

    required: tuple[str, ...] = ()
    """Variables declared ``required: true`` with no ``default:``. The caller
    must supply each one; :mod:`.execution` refuses the run otherwise."""

    errors: tuple[str, ...] = ()
    """Malformed declarations, each message naming its variable."""

    warnings: tuple[str, ...] = ()
    """Non-fatal oddities, each message naming its variable."""


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
            dependency key, malformed
            ``dependencies``/``capabilities``/``agents``, an unsupported
            ``schema_version``, an ``agent_config`` step field, a flat
            stage-level approval key (``approval_required`` /
            ``approval_message`` / ``auto_approve_if``), or any other step or
            stage key no engine reads (see :data:`KNOWN_STEP_KEYS` /
            :data:`KNOWN_STAGE_KEYS`).
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
    _reject_flat_stage_approval_keys(data, source=source)
    _reject_unknown_stage_keys(data, source=source)
    _reject_unknown_step_keys(data, source=source)
    check_context_block(data.get("context"), source=source)

    if "dependencies" not in data:
        raise ManifestError(
            f"schema_version {SCHEMA_VERSION} requires a 'dependencies' block "
            "(use 'dependencies: []' to declare none)",
            clause="Core 1",
            source=source,
        )

    dependencies = _parse_dependencies(data["dependencies"], source=source)
    capabilities = _parse_capabilities(data.get("capabilities"), source=source)
    agents = _parse_agent_aliases(data.get("agents"), source=source)

    return Manifest(
        schema_version=SCHEMA_VERSION,
        dependencies=dependencies,
        capabilities=capabilities,
        agents=agents,
        source=source,
    )


# --------------------------------------------------------------------------
# The declarative `context:` entry
# --------------------------------------------------------------------------


def is_context_declaration(value: Any) -> bool:
    """True when a ``context:`` value is a schema-form declaration.

    A recipe's ``context:`` is a variable -> VALUE mapping, but authors reach
    for the shape every other tool's input schema uses::

        context:
          topic:
            type: string
            required: true

    Nothing used to recognise that, so the whole mapping was bound as the
    variable's value and ``{{topic}}`` substituted ``{'type': 'string',
    'required': True}`` into prompts and conditions (recipes-u2f). A non-empty
    mapping whose every key is drawn from :data:`CONTEXT_DECLARATION_KEYS` is
    a declaration; anything else -- including ``{}`` and any mapping carrying
    one other key -- is an ordinary literal value, bound unchanged.
    """
    if not isinstance(value, Mapping) or not value:
        return False
    return all(isinstance(key, str) and key in CONTEXT_DECLARATION_KEYS for key in value)


def _declaration_issues(name: str, declaration: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for one declaration, every message naming ``name``."""
    errors: list[str] = []
    warnings: list[str] = []
    where = f"context variable {name!r}"

    if "required" in declaration and not isinstance(declaration["required"], bool):
        errors.append(f"{where}: 'required' must be true or false, got {declaration['required']!r}")

    if "type" in declaration:
        declared = declaration["type"]
        if not isinstance(declared, str) or declared not in CONTEXT_DECLARED_TYPES:
            errors.append(
                f"{where}: 'type' must be one of {', '.join(sorted(CONTEXT_DECLARED_TYPES))}, got {declared!r}"
            )

    if "description" in declaration and not isinstance(declaration["description"], str):
        errors.append(f"{where}: 'description' must be a string, got {type(declaration['description']).__name__}")

    if "enum" in declaration:
        choices = declaration["enum"]
        if not isinstance(choices, list) or not choices:
            errors.append(f"{where}: 'enum' must be a non-empty list, got {choices!r}")
        elif "default" in declaration and declaration["default"] not in choices:
            errors.append(f"{where}: default {declaration['default']!r} is not one of its 'enum' values {choices!r}")

    if declaration.get("required") is True and "default" in declaration:
        warnings.append(
            f"{where} declares both 'required: true' and a default; the default is bound, "
            "so the variable is never missing (drop one)"
        )

    return errors, warnings


def resolve_context_block(context: Any) -> ResolvedContext:
    """Read a ``context:`` block into values plus diagnostics.

    Never raises and never binds a declaration mapping: a malformed
    declaration contributes an error and is left unbound. ``default:`` binds;
    ``required: true`` without a default defers to the caller; a declaration
    that is neither is deliberately UNBOUND, so referencing it fails loudly
    through the engine's own "variable not found" path.
    """
    if not isinstance(context, Mapping):
        return ResolvedContext(values=MappingProxyType({}))

    values: dict[str, Any] = {}
    required: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []

    for name, value in context.items():
        if not is_context_declaration(value):
            values[name] = value
            continue

        key = str(name)
        entry_errors, entry_warnings = _declaration_issues(key, value)
        errors.extend(entry_errors)
        warnings.extend(entry_warnings)
        if entry_errors:
            continue

        if "default" in value:
            values[name] = value["default"]
        elif value.get("required") is True:
            required.append(key)

    return ResolvedContext(
        values=values,
        required=tuple(required),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def check_context_block(context: Any, *, source: str | None = None) -> ResolvedContext:
    """Resolve a ``context:`` block, raising on a malformed declaration.

    Raises:
        ManifestError: naming every offending variable. Binding the schema
            mapping as the value is the one outcome this refuses.
    """
    resolved = resolve_context_block(context)
    if resolved.errors:
        raise ManifestError(
            "malformed 'context' declaration(s): " + "; ".join(resolved.errors),
            clause="Core 1",
            source=source,
        )
    return resolved


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


def _parse_capabilities(value: Any, *, source: str | None) -> tuple[str, ...]:
    """Parse the optional top-level ``capabilities`` list (Core 9).

    Absent (``None``) and ``[]`` mean the same thing -- the recipe declares no
    needs -- so both yield an empty tuple. A duplicate entry is an ERROR rather
    than a quiet de-dupe: silently collapsing a list the author wrote twice is
    the same class of silent edit Core 1 forbids for unknown keys.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestError(
            f"'capabilities' must be a list of capability names, got {type(value).__name__} "
            "(omit the key, or use 'capabilities: []', to declare none)",
            clause="Core 9",
            source=source,
        )

    capabilities: list[str] = []
    for index, name in enumerate(value):
        if isinstance(name, bool) or not isinstance(name, str) or not name.strip():
            raise ManifestError(
                f"capabilities[{index}] must be a non-empty string, got {name!r}",
                clause="Core 9",
                source=source,
            )
        if name in capabilities:
            raise ManifestError(
                f"capabilities[{index}]: duplicate capability {name!r} "
                f"(already declared at capabilities[{capabilities.index(name)}])",
                clause="Core 9",
                source=source,
            )
        capabilities.append(name)

    return tuple(capabilities)


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


def _reject_flat_stage_approval_keys(data: Mapping[str, Any], *, source: str | None) -> None:
    """Core 1, applied to a STAGE key: an approval gate is never dropped silently.

    ``approval_required`` / ``approval_message`` / ``auto_approve_if`` are not
    stage fields -- the only gate a stage has is its ``approval:`` block. A
    stage carrying them presents itself, often at length, as a human
    checkpoint and has never gated anything: the run goes straight through.
    That is the same silent edit Core 1 forbids for an unknown top-level key,
    and the same class as Core 12's parsed-but-ignored ``agent_config``, so it
    is rejected here by name with the remedy rather than honoured as an alias
    (``auto_approve_if`` has no honourable reading at all -- no engine here
    can evaluate it).
    """
    stages = data.get("stages")
    if not isinstance(stages, list):
        return
    for index, stage in enumerate(stages):
        if not isinstance(stage, Mapping):
            continue
        offending = [key for key in FLAT_STAGE_APPROVAL_KEYS if key in stage]
        if not offending:
            continue
        name = stage.get("name")
        named = f"stage {name!r}" if isinstance(name, str) and name else f"stage at stages[{index}]"
        remedies = "; ".join(f"{key!r}: {FLAT_STAGE_APPROVAL_KEYS[key]}" for key in offending)
        one = len(offending) == 1
        raise ManifestError(
            f"{named} declares {_fmt(offending)}, which "
            f"{'is not a stage key' if one else 'are not stage keys'}: a stage's only "
            "approval gate is its 'approval:' block, so "
            f"{'this' if one else 'these'} would be read by nobody and the declared "
            f"human checkpoint would run ungated. {remedies}",
            clause="Core 1",
            source=source,
        )


def unknown_step_key_error(step: Mapping[str, Any], *, where: str) -> tuple[str, str] | None:
    """``(message, remedy)`` for a step carrying unread keys, else ``None``.

    Shared by this module and :mod:`.engine` so the two parse paths cannot
    describe the same file differently. ``where`` is the positional path used
    when the step has no usable ``id``.
    """
    offending = sorted(str(key) for key in step if key not in KNOWN_STEP_KEYS)
    if not offending:
        return None

    step_id = step.get("id")
    named = f"step {step_id!r}" if isinstance(step_id, str) and step_id else f"step at {where}"
    one = len(offending) == 1
    message = (
        f"{named} declares {_fmt(offending)}, which "
        f"{'is not a step key' if one else 'are not step keys'}: "
        f"{'it' if one else 'they'} would be read by nobody, so whatever "
        f"{'it declares' if one else 'they declare'} never happens. Valid step keys are "
        f"{_fmt(sorted(KNOWN_STEP_KEYS))}"
    )
    named_remedies = [f"{key!r}: {FLAT_STEP_APPROVAL_KEYS[key]}" for key in offending if key in FLAT_STEP_APPROVAL_KEYS]
    remedy = (
        "; ".join(named_remedies)
        if named_remedies
        else "remove the key, or correct it to one of the valid step keys named above"
    )
    return message, remedy


def unknown_stage_key_error(stage: Mapping[str, Any], *, index: int) -> tuple[str, str] | None:
    """``(message, remedy)`` for a stage carrying unread keys, else ``None``.

    The flat approval keys are handled first, by
    :func:`_reject_flat_stage_approval_keys` and its engine twin, so they keep
    their own remedies; anything else left over lands here.
    """
    offending = sorted(str(key) for key in stage if key not in KNOWN_STAGE_KEYS)
    if not offending:
        return None

    name = stage.get("name")
    named = f"stage {name!r}" if isinstance(name, str) and name else f"stage at stages[{index}]"
    one = len(offending) == 1
    message = (
        f"{named} declares {_fmt(offending)}, which "
        f"{'is not a stage key' if one else 'are not stage keys'}: "
        f"{'it' if one else 'they'} would be read by nobody, so whatever "
        f"{'it declares' if one else 'they declare'} never happens. Valid stage keys are "
        f"{_fmt(sorted(KNOWN_STAGE_KEYS))}"
    )
    named_remedies = [f"{key!r}: {REJECTED_STAGE_KEYS[key]}" for key in offending if key in REJECTED_STAGE_KEYS]
    remedy = (
        "; ".join(named_remedies)
        if named_remedies
        else "remove the key, or correct it to one of the valid stage keys named above"
    )
    return message, remedy


def _reject_unknown_step_keys(data: Mapping[str, Any], *, source: str | None) -> None:
    """Core 1, applied to a STEP key: an unread step key is an ERROR.

    Walks flat steps, staged steps, and nested foreach/while bodies -- the
    same population :func:`_reject_agent_config` walks.
    """
    for step, path in _walk_steps(data):
        failure = unknown_step_key_error(step, where=path)
        if failure is None:
            continue
        message, remedy = failure
        raise ManifestError(f"{message}. {remedy}", clause="Core 1", source=source)


def _reject_unknown_stage_keys(data: Mapping[str, Any], *, source: str | None) -> None:
    """Core 1, applied to a STAGE key: an unread stage key is an ERROR."""
    stages = data.get("stages")
    if not isinstance(stages, list):
        return
    for index, stage in enumerate(stages):
        if not isinstance(stage, Mapping):
            continue
        failure = unknown_stage_key_error(stage, index=index)
        if failure is None:
            continue
        message, remedy = failure
        raise ManifestError(f"{message}. {remedy}", clause="Core 1", source=source)


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
