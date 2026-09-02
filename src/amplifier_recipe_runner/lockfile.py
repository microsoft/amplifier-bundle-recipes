"""Sidecar lockfile: read, write, and the three lock modes.

Contracts: ``recipe-dependency-manifest.v1`` Core 8 (lock semantics) and the
recording half of Core 7 (declared URI/ref -> resolved immutable
revision/content digest).

A lock is a *sidecar* of the recipe -- ``pipeline.yaml`` locks to
``pipeline.lock.yaml`` (:func:`lock_path_for`) -- and is optional: it is
generated, never hand-required. It records ``lock_version: 1``, the recipe
digest at generation time, and one entry per declared dependency carrying:

* ``declared_source`` -- exactly what the manifest asked for, ``@ref`` and
  ``#subdirectory=`` fragment included. This is the entry's identity.
* ``canonical_source`` -- the same source with its ``@ref`` suffix and
  fragment stripped: the stable identity of *where* it comes from, so the
  same repository pinned at two refs shares a canonical source and differs
  only in the recorded ref/revision.
* the resolved immutable identity -- ``resolved_revision`` for git sources,
  ``content_digest`` for local ones.

The three modes (Core 8), and the one invariant that binds them
-------------------------------------------------------------
* :attr:`~amplifier_recipe_runner.api.LockMode.LOCKED` -- default, and
  mandatory for CI. Requires an exact entry for **every** declared dependency:
  a missing entry, an extra entry, or an entry whose resolved identity differs
  is an error raised before anything runs. **A plain run in locked mode never
  writes the lock** -- :func:`apply_lock_mode` has no write path on this
  branch at all, which is what makes "locks are never updated silently on run"
  structural rather than a promise.
* :attr:`~amplifier_recipe_runner.api.LockMode.UPDATE_LOCK` -- the only mode
  that rewrites, and it does so because it was asked to, explicitly.
* :attr:`~amplifier_recipe_runner.api.LockMode.UNLOCKED` -- interactive only.
  Resolves with no lock consulted and **returns a warning**; the warning is a
  real result the host must surface, not a log line.

A resolved identity that differs from the recorded one raises
:class:`~amplifier_recipe_runner.errors.ProvenanceMismatchError` -- the same
typed failure a mismatched resume raises (see :mod:`.provenance`), because it
is the same fact: what was recorded is not what resolved, and re-resolving
silently is never the answer.

This module imports nothing from Amplifier and executes nothing: it reads and
writes YAML, and compares an :class:`~amplifier_recipe_runner.api.ExecutionPlan`
against what was recorded.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Final

import yaml

from .api import DependencyKind
from .api import ExecutionPlan
from .api import LockMode
from .api import ResolvedDependency
from .errors import PreflightError
from .errors import ProvenanceMismatchError

__all__ = [
    "LOCKFILE_SUFFIX",
    "LOCK_VERSION",
    "LockEntry",
    "LockError",
    "LockEntryMissingError",
    "LockEntryUnexpectedError",
    "LockVersionError",
    "Lockfile",
    "LockfileMissingError",
    "LockResult",
    "apply_lock_mode",
    "canonical_source",
    "lock_from_plan",
    "lock_path_for",
    "read_lock",
    "verify_lock",
    "write_lock",
]

#: Lockfile format version. Values above 1 are Reserved in
#: ``recipe-dependency-manifest.v1``.
LOCK_VERSION: Final[int] = 1

#: Sidecar suffix: ``pipeline.yaml`` -> ``pipeline.lock.yaml``.
LOCKFILE_SUFFIX: Final[str] = ".lock.yaml"

_RECIPE_SUFFIXES: Final[tuple[str, ...]] = (".yaml", ".yml")

_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "declared_source",
        "canonical_source",
        "kind",
        "requested_ref",
        "resolved_revision",
        "content_digest",
        "subdirectory",
        "version",
    }
)

_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({"lock_version", "recipe_digest", "dependencies"})


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class LockError(PreflightError):
    """A lockfile problem detected before any recipe step runs.

    A :class:`~amplifier_recipe_runner.errors.PreflightError`, so a host
    catching that base class already knows nothing ran.
    """


class LockfileMissingError(LockError):
    """Locked mode was requested but no lockfile exists."""

    def __init__(self, path: str | Path, *, remedy: str | None = None) -> None:
        self.path = str(path)
        super().__init__(
            f"Locked mode requires a lockfile, but {self.path!r} does not exist.",
            remedy=remedy or ("Generate one with `update-lock`, or run in `unlocked` mode interactively."),
        )


class LockVersionError(LockError):
    """The lockfile declares an unsupported ``lock_version``."""

    def __init__(self, found: Any, *, path: str | None = None, remedy: str | None = None) -> None:
        self.found = found
        self.path = path
        where = f" in {path!r}" if path else ""
        super().__init__(
            f"Unsupported lock_version {found!r}{where}; this runner writes and reads version {LOCK_VERSION}.",
            remedy=remedy
            or (f"Regenerate the lock with `update-lock` on a runner writing version {LOCK_VERSION}."),
        )


class LockEntryMissingError(LockError):
    """A declared dependency has no entry in the lock (Core 8, locked mode)."""

    def __init__(self, source: str, *, path: str | None = None, remedy: str | None = None) -> None:
        self.source = source
        self.path = path
        where = f" in {path!r}" if path else ""
        super().__init__(
            f"Declared dependency {source!r} has no lock entry{where}; locked mode requires an "
            "exact entry for every declared dependency.",
            remedy=remedy or "Re-run with `update-lock` to record it, after reviewing what it resolves to.",
        )


class LockEntryUnexpectedError(LockError):
    """The lock carries an entry no declared dependency asked for."""

    def __init__(self, source: str, *, path: str | None = None, remedy: str | None = None) -> None:
        self.source = source
        self.path = path
        where = f" in {path!r}" if path else ""
        super().__init__(
            f"Lock entry {source!r}{where} matches no declared dependency; locked mode requires "
            "the lock and the manifest to agree exactly.",
            remedy=remedy
            or ("Re-run with `update-lock` to drop the stale entry, or restore the declaration."),
        )


# --------------------------------------------------------------------------
# Lock shape
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LockEntry:
    """One declared dependency, pinned to the identity it resolved to."""

    declared_source: str
    """Exactly as declared, ``@ref`` and ``#subdirectory=`` included."""

    canonical_source: str
    """``declared_source`` with its ``@ref`` suffix and fragment stripped."""

    kind: str = DependencyKind.BUNDLE.value
    requested_ref: str | None = None
    resolved_revision: str | None = None
    """Immutable revision (git sha) actually resolved, when there is one."""

    content_digest: str | None = None
    """Content digest, recorded for sources that have no revision."""

    subdirectory: str | None = None
    """Set for behavior partials declared via ``#subdirectory=``."""

    version: str | None = None

    @property
    def identity(self) -> str:
        """The immutable identity this entry pins, for mismatch reporting.

        A git source pins its revision; a local source has none, so it pins a
        content digest instead. Reported as a single string so a mismatch can
        name *both* sides without the caller guessing which field applied.
        """
        if self.resolved_revision is not None:
            return self.resolved_revision
        if self.content_digest is not None:
            return self.content_digest
        return "<unresolved>"

    def to_mapping(self) -> dict[str, Any]:
        """Serializable form. ``None`` fields are omitted, never written null."""
        data: dict[str, Any] = {
            "declared_source": self.declared_source,
            "canonical_source": self.canonical_source,
            "kind": self.kind,
        }
        for key in ("requested_ref", "resolved_revision", "content_digest", "subdirectory", "version"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_mapping(cls, data: Any, *, path: str | None = None) -> LockEntry:
        """Parse one entry strictly: an unknown key is an error, not ignored."""
        if not isinstance(data, Mapping):
            raise LockError(
                f"Lock entry must be a mapping, got {type(data).__name__}.",
                remedy="Regenerate the lock with `update-lock`.",
            )
        unknown = sorted(set(data) - _ENTRY_KEYS)
        if unknown:
            where = f" in {path!r}" if path else ""
            raise LockError(
                f"Unknown lock entry key(s) {', '.join(repr(k) for k in unknown)}{where}.",
                remedy="Remove the unknown key(s), or regenerate the lock with `update-lock`.",
            )
        declared = data.get("declared_source")
        if not isinstance(declared, str) or not declared:
            raise LockError(
                "Lock entry is missing a non-empty 'declared_source'.",
                remedy="Regenerate the lock with `update-lock`.",
            )
        canonical = data.get("canonical_source")
        return cls(
            declared_source=declared,
            canonical_source=canonical if isinstance(canonical, str) and canonical else canonical_source(declared),
            kind=str(data.get("kind") or DependencyKind.BUNDLE.value),
            requested_ref=_opt_str(data.get("requested_ref")),
            resolved_revision=_opt_str(data.get("resolved_revision")),
            content_digest=_opt_str(data.get("content_digest")),
            subdirectory=_opt_str(data.get("subdirectory")),
            version=_opt_str(data.get("version")),
        )

    @classmethod
    def from_dependency(cls, dependency: ResolvedDependency) -> LockEntry:
        """Pin one resolved dependency exactly as the plan resolved it."""
        return cls(
            declared_source=dependency.uri,
            canonical_source=canonical_source(dependency.uri),
            kind=_kind_value(dependency.kind),
            requested_ref=dependency.requested_ref,
            resolved_revision=dependency.resolved_revision,
            content_digest=dependency.content_digest,
            subdirectory=dependency.subdirectory,
            version=dependency.version,
        )


@dataclass(frozen=True, slots=True)
class Lockfile:
    """A parsed sidecar lock: version, recipe digest, and pinned entries."""

    recipe_digest: str | None = None
    entries: tuple[LockEntry, ...] = ()
    lock_version: int = LOCK_VERSION

    def entry_for(self, declared_source: str) -> LockEntry | None:
        """The entry pinning ``declared_source``, or ``None``."""
        for entry in self.entries:
            if entry.declared_source == declared_source:
                return entry
        return None

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(entry.declared_source for entry in self.entries)

    def to_mapping(self) -> dict[str, Any]:
        data: dict[str, Any] = {"lock_version": self.lock_version}
        if self.recipe_digest is not None:
            data["recipe_digest"] = self.recipe_digest
        data["dependencies"] = [entry.to_mapping() for entry in self.entries]
        return data

    @classmethod
    def from_mapping(cls, data: Any, *, path: str | None = None) -> Lockfile:
        """Parse a lock document strictly (unknown top-level keys error)."""
        if not isinstance(data, Mapping):
            raise LockError(
                f"Lockfile must be a mapping, got {type(data).__name__}.",
                remedy="Regenerate the lock with `update-lock`.",
            )
        unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
        if unknown:
            where = f" in {path!r}" if path else ""
            raise LockError(
                f"Unknown lockfile key(s) {', '.join(repr(k) for k in unknown)}{where}.",
                remedy="Remove the unknown key(s), or regenerate the lock with `update-lock`.",
            )
        version = data.get("lock_version")
        if version != LOCK_VERSION:
            raise LockVersionError(version, path=path)
        raw = data.get("dependencies") or []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise LockError(
                "Lockfile 'dependencies' must be a list.",
                remedy="Regenerate the lock with `update-lock`.",
            )
        return cls(
            recipe_digest=_opt_str(data.get("recipe_digest")),
            entries=tuple(LockEntry.from_mapping(item, path=path) for item in raw),
            lock_version=LOCK_VERSION,
        )


@dataclass(frozen=True, slots=True)
class LockResult:
    """What :func:`apply_lock_mode` did, stated rather than implied."""

    mode: LockMode
    lock: Lockfile | None = None
    path: Path | None = None
    rewritten: bool = False
    """True only under :attr:`LockMode.UPDATE_LOCK`."""

    warnings: tuple[str, ...] = ()
    """Real results a host must surface (Core 8: unlocked warns)."""


# --------------------------------------------------------------------------
# Paths and canonicalisation
# --------------------------------------------------------------------------


def lock_path_for(recipe: str | Path) -> Path:
    """Sidecar lock path for ``recipe``: ``x.yaml`` -> ``x.lock.yaml``."""
    path = Path(recipe)
    if path.suffix.lower() in _RECIPE_SUFFIXES:
        return path.with_suffix(LOCKFILE_SUFFIX)
    return path.with_name(path.name + LOCKFILE_SUFFIX)


def canonical_source(source: str) -> str:
    """Strip the ``@ref`` suffix and ``#fragment`` from a declared source.

    The result is the stable identity of *where* a dependency comes from. Two
    declarations of the same repository at different refs share it; that is
    the point -- the ref and the resolved revision are recorded separately.
    """
    base, _, _ = source.partition("#")
    scheme, sep, rest = base.partition("://")
    tail = rest if sep else base
    at = tail.rfind("@")
    slash = tail.rfind("/")
    if at > slash:
        tail = tail[:at]
    return f"{scheme}{sep}{tail}" if sep else tail


# --------------------------------------------------------------------------
# Read / write
# --------------------------------------------------------------------------


def read_lock(path: str | Path) -> Lockfile:
    """Read and strictly parse a lockfile.

    Raises:
        LockfileMissingError: no file at ``path``.
        LockError / LockVersionError: the file is malformed or a version this
            runner does not read.
    """
    lock_path = Path(path)
    try:
        text = lock_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LockfileMissingError(lock_path) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LockError(
            f"Lockfile {str(lock_path)!r} is not valid YAML: {exc}",
            remedy="Regenerate the lock with `update-lock`.",
        ) from exc
    return Lockfile.from_mapping(data, path=str(lock_path))


def write_lock(path: str | Path, lock: Lockfile) -> Path:
    """Write ``lock`` to ``path`` atomically, deterministically.

    Key order is fixed by :meth:`Lockfile.to_mapping` and entry order follows
    declaration order, so re-running ``update-lock`` on an unchanged graph
    produces a byte-identical file.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(lock.to_mapping(), sort_keys=False, default_flow_style=False)
    handle, tmp_name = tempfile.mkstemp(dir=str(lock_path.parent), prefix=lock_path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, lock_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return lock_path


def lock_from_plan(plan: ExecutionPlan) -> Lockfile:
    """Build a lock pinning exactly what ``plan`` resolved."""
    return Lockfile(
        recipe_digest=plan.recipe_digest,
        entries=tuple(LockEntry.from_dependency(dep) for dep in plan.dependencies),
        lock_version=LOCK_VERSION,
    )


# --------------------------------------------------------------------------
# Verification (locked mode)
# --------------------------------------------------------------------------


def verify_lock(lock: Lockfile, plan: ExecutionPlan, *, path: str | Path | None = None) -> tuple[str, ...]:
    """Check ``lock`` describes ``plan`` exactly (Core 8, locked mode).

    Every declared dependency must have an entry, every entry must match a
    declared dependency, and each entry's recorded revision/content digest,
    requested ref and subdirectory must equal what the plan resolved.

    Returns:
        Warnings that are not failures. A recipe digest recorded before an
        edit to the recipe *body* is reported here rather than raised: locked
        mode pins the dependency graph, and a step's prompt changing does not
        change what was fetched.

    Raises:
        LockEntryMissingError: a declared dependency has no entry.
        LockEntryUnexpectedError: an entry matches no declared dependency.
        ProvenanceMismatchError: an entry's recorded identity is not what
            resolved -- named on both sides, never silently re-resolved.
    """
    where = str(path) if path is not None else None
    declared = tuple(dep.uri for dep in plan.dependencies)

    for dependency in plan.dependencies:
        entry = lock.entry_for(dependency.uri)
        if entry is None:
            raise LockEntryMissingError(dependency.uri, path=where)
        _compare_entry(entry, dependency)

    for entry in lock.entries:
        if entry.declared_source not in declared:
            raise LockEntryUnexpectedError(entry.declared_source, path=where)

    warnings: list[str] = []
    if lock.recipe_digest is not None and lock.recipe_digest != plan.recipe_digest:
        warnings.append(
            f"Lock records recipe digest {lock.recipe_digest!r} but the recipe now digests to "
            f"{plan.recipe_digest!r}; the dependency graph still matches. Re-run with `update-lock` "
            "to refresh the recorded digest."
        )
    return tuple(warnings)


def _compare_entry(entry: LockEntry, dependency: ResolvedDependency) -> None:
    """Raise if ``dependency`` did not resolve to what ``entry`` pinned."""
    resolved = LockEntry.from_dependency(dependency)
    if entry.identity != resolved.identity:
        raise ProvenanceMismatchError(
            dependency.uri,
            expected=entry.identity,
            actual=resolved.identity,
        )
    for field_name in ("requested_ref", "subdirectory"):
        recorded = getattr(entry, field_name)
        current = getattr(resolved, field_name)
        if recorded != current:
            raise ProvenanceMismatchError(
                dependency.uri,
                expected=f"{field_name}={recorded!r}",
                actual=f"{field_name}={current!r}",
            )


# --------------------------------------------------------------------------
# The three modes
# --------------------------------------------------------------------------


def apply_lock_mode(
    plan: ExecutionPlan,
    *,
    path: str | Path,
    mode: LockMode | None = None,
) -> LockResult:
    """Apply lock semantics for one run (Core 8).

    Args:
        plan: The resolved graph to check against, or to pin.
        path: Sidecar lock path -- see :func:`lock_path_for`.
        mode: Overrides ``plan.policy.lock_mode``; defaults to it, and to
            :attr:`LockMode.LOCKED` when the plan carries no policy. A bare
            string (``"locked"``) is accepted -- hosts read this from a flag
            or a config file -- and an unrecognised one is an error, never a
            silent fallback to a laxer mode.

    Returns:
        A :class:`LockResult` stating whether the lock was rewritten and
        carrying any warnings.

    Raises:
        LockError subclasses / ProvenanceMismatchError: in locked mode, when
            the lock is absent, disagrees with the declared dependencies, or
            pins a different resolved identity.

    Locked mode has **no write path in this function**: it reads, verifies,
    and returns. That is the structural form of "locks are never updated
    silently on run".
    """
    requested = mode or (plan.policy.lock_mode if plan.policy is not None else LockMode.LOCKED)
    try:
        effective = LockMode(requested)
    except ValueError as exc:
        raise LockError(
            f"Unsupported lock mode {requested!r}.",
            remedy="Use one of: locked, update-lock, unlocked.",
        ) from exc
    lock_path = Path(path)

    if effective is LockMode.UNLOCKED:
        return LockResult(
            mode=effective,
            lock=None,
            path=lock_path,
            rewritten=False,
            warnings=(
                "Running in `unlocked` mode: dependency revisions are not pinned and the lockfile "
                f"({lock_path}) was neither read nor written. Unlocked mode is interactive only -- "
                "CI must run `locked`.",
            ),
        )

    if effective is LockMode.UPDATE_LOCK:
        lock = lock_from_plan(plan)
        write_lock(lock_path, lock)
        return LockResult(mode=effective, lock=lock, path=lock_path, rewritten=True)

    lock = read_lock(lock_path)
    warnings = verify_lock(lock, plan, path=lock_path)
    return LockResult(mode=effective, lock=lock, path=lock_path, rewritten=False, warnings=warnings)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _kind_value(kind: Any) -> str:
    return kind.value if isinstance(kind, DependencyKind) else str(kind)
