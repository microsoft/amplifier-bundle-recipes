"""Engine provenance -- *which* tool-recipes code actually ran.

A recipe run used to say what it did and never say who did it.  On a machine
with one installed engine that distinction is invisible.  On a machine with
several worktrees of this repo it is the whole story: ``recipes-669`` and
``recipes-ecd`` both record a lane "proving" an engine change live and
actually exercising a *different lane's* code, because the shared bundle cache
had been repointed and the CLI's editable ``.pth`` had been rewritten behind
everyone's back.  Neither run failed.  Both looked like clean passes.

So this module answers one question, from inside the running engine, with no
run required:

    Which file is this code, where did that file come from, and is anything
    about that path lying to me?

What it collects
----------------

* ``module_file`` / ``module_dir`` -- ``__file__``, exactly as imported, and
  ``module_dir_real`` beside it, because those two differing *is* the symlink
  substitution that made recipes-669 silent.
* ``package_version`` -- the installed distribution version, when there is one.
* ``git_sha`` / ``git_ref`` / ``git_root`` -- resolved by reading ``.git``
  upward from ``__file__``.  Deliberately file-reading, not ``git`` in a
  subprocess: this runs on the result path of every operation, and a worktree's
  ``.git`` is a *file* (``gitdir: ...``) whose refs live in the main repo's
  common dir, which a naive subprocess-free reader gets wrong.
* ``bundle_cache_*`` -- when the code was imported out of the Amplifier bundle
  cache, the cache directory, whether it is a symlink, what it resolves to, and
  the ``commit``/``ref`` its ``.amplifier_cache_meta.json`` claims.
* ``editable_pth`` -- every editable-install ``.pth`` on this interpreter that
  points at this module, and where each one points.  This is the recipes-ecd
  failure made visible: restoring the cache directory is not enough if the
  ``.pth`` still names a lane worktree.
* ``warnings`` -- the two conditions above, rendered as sentences naming *both*
  paths, so a reader never has to reconstruct the substitution themselves.

How it rides on a result
------------------------

Beside the payload, never inside it -- the same mechanism, and the same
reason, as :func:`runner_adapter.label_execution_mode` and
:func:`shutdown.attach_shutdown_warning`: ``ToolResult``'s serialized payload
is what the legacy-compat baselines pin byte-for-byte, and a diagnostic that
silently rewrites a frozen contract is a worse bug than the one it reports.

The caller-facing, CLI-visible answer is therefore a *new* operation --
``engine_info`` -- whose output is the provenance itself and which no baseline
pins.  ``scripts/live-proof.sh`` asks exactly that, which is how a lane proves
its own engine ran without touching the cache or the CLI's venv.

Nothing here may raise.  Every collector is individually guarded: provenance
that cannot be gathered degrades to ``None`` plus a note, because a run that
dies reporting its own identity is strictly worse than a run that admits it
does not know.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODULE_NAME = "amplifier_module_tool_recipes"
DISTRIBUTION_NAME = "amplifier-module-tool-recipes"

#: Marker the Amplifier bundle cache drops beside a cached bundle checkout.
CACHE_META_FILENAME = ".amplifier_cache_meta.json"

#: Set to ``0``/``false``/``no``/``off`` to silence the import-time guard.
ENV_GUARD = "AMPLIFIER_RECIPES_ENGINE_GUARD"

_FALSEY = {"0", "false", "no", "off"}

# How far up from ``__file__`` to look for a ``.git`` or a cache marker.  A
# bundle checkout is shallow; an unbounded walk on a deep path is wasted work.
_MAX_WALK_UP = 12

_CACHED: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Small guarded readers
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _ancestors(start: Path) -> list[Path]:
    chain = [start, *start.parents]
    return chain[:_MAX_WALK_UP]


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def find_git_dir(start: Path) -> tuple[Path, Path] | None:
    """Return ``(git_dir, work_tree_root)`` for the first ``.git`` above *start*.

    Handles both shapes: a real ``.git`` directory, and the ``gitdir: <path>``
    *file* a linked worktree carries (whose target may be relative to the
    worktree root).
    """
    for directory in _ancestors(start):
        candidate = directory / ".git"
        try:
            if candidate.is_dir():
                return candidate, directory
            if candidate.is_file():
                content = _read_text(candidate) or ""
                if not content.startswith("gitdir:"):
                    continue
                target = Path(content.split(":", 1)[1].strip())
                if not target.is_absolute():
                    target = directory / target
                if target.exists():
                    return target, directory
        except OSError:  # pragma: no cover - defensive
            continue
    return None


def _git_common_dir(git_dir: Path) -> Path:
    """The repository's shared dir -- ``git_dir`` itself unless it is a worktree."""
    commondir = _read_text(git_dir / "commondir")
    if not commondir:
        return git_dir
    common = Path(commondir)
    if not common.is_absolute():
        common = git_dir / common
    try:
        return common.resolve()
    except OSError:  # pragma: no cover - defensive
        return git_dir


def resolve_head(git_dir: Path) -> tuple[str | None, str | None]:
    """Return ``(sha, ref)`` for a git dir's ``HEAD``.

    ``ref`` is ``None`` for a detached HEAD; ``sha`` is ``None`` when the ref
    exists but could not be resolved (which is reported, never guessed).
    """
    head = _read_text(git_dir / "HEAD")
    if not head:
        return None, None
    if not head.startswith("ref:"):
        return head.split()[0], None

    ref = head.split(":", 1)[1].strip()
    common = _git_common_dir(git_dir)
    for base in (git_dir, common):
        sha = _read_text(base / ref)
        if sha:
            return sha.split()[0], ref

    packed = _read_text(common / "packed-refs")
    if packed:
        for line in packed.splitlines():
            if not line or line[0] in "#^":
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0], ref
    return None, ref


# ---------------------------------------------------------------------------
# bundle cache
# ---------------------------------------------------------------------------


def find_cache_meta(start: Path) -> tuple[Path, dict[str, Any]] | None:
    """Return ``(cache_dir, meta)`` if *start* sits inside a cached bundle."""
    for directory in _ancestors(start):
        marker = directory / CACHE_META_FILENAME
        try:
            if not marker.is_file():
                continue
        except OSError:  # pragma: no cover - defensive
            continue
        raw = _read_text(marker)
        if raw is None:
            return directory, {}
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return directory, {}
        return directory, meta if isinstance(meta, dict) else {}
    return None


# ---------------------------------------------------------------------------
# editable installs
# ---------------------------------------------------------------------------


def editable_pth_targets(module_name: str = MODULE_NAME) -> list[dict[str, str]]:
    """Every editable-install ``.pth`` on this interpreter naming *module_name*.

    uv/pip write these two shapes; only the first carries a plain path (and so
    loses to ``PYTHONPATH``), which is precisely why the live-proof procedure
    can work without touching the venv at all::

        _editable_impl_<module>.pth        -> one line, a directory
        __editable__.<dist>-<ver>.pth      -> may instead install a finder
    """
    stem = module_name.replace("-", "_")
    seen: set[str] = set()
    found: list[dict[str, str]] = []
    for entry in sys.path:
        if not entry or not entry.endswith("site-packages"):
            continue
        directory = Path(entry)
        try:
            candidates = sorted(directory.glob("*.pth"))
        except OSError:  # pragma: no cover - defensive
            continue
        for pth in candidates:
            name = pth.name
            if stem not in name.replace("-", "_"):
                continue
            key = str(pth)
            if key in seen:
                continue
            seen.add(key)
            first = (_read_text(pth) or "").splitlines()
            target = first[0].strip() if first else ""
            record = {"pth": key, "target": target}
            if target and not target.startswith("import "):
                record["target_real"] = os.path.realpath(target)
            found.append(record)
    return found


# ---------------------------------------------------------------------------
# the provenance record
# ---------------------------------------------------------------------------


def _package_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version

        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("engine provenance: version lookup failed: %s", exc)
        return None


def _collect() -> dict[str, Any]:
    module_file = Path(__file__).parent / "__init__.py"
    module_dir = module_file.parent
    module_dir_real = os.path.realpath(module_dir)

    record: dict[str, Any] = {
        "module": MODULE_NAME,
        "module_file": str(module_file),
        "module_dir": str(module_dir),
        "module_dir_real": module_dir_real,
        "module_dir_is_symlink": str(module_dir) != module_dir_real,
        "package_version": _package_version(),
        "git_sha": None,
        "git_ref": None,
        "git_root": None,
        "bundle_cache_dir": None,
        "bundle_cache_is_symlink": False,
        "bundle_cache_resolves_to": None,
        "bundle_cache_commit": None,
        "bundle_cache_ref": None,
        "editable_pth": [],
        "python_executable": sys.executable,
        "warnings": [],
    }

    try:
        git = find_git_dir(module_dir)
        if git is not None:
            git_dir, root = git
            sha, ref = resolve_head(git_dir)
            record["git_sha"] = sha
            record["git_ref"] = ref
            record["git_root"] = str(root)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("engine provenance: git lookup failed: %s", exc)

    try:
        cache = find_cache_meta(module_dir)
        if cache is not None:
            cache_dir, meta = cache
            resolved = os.path.realpath(cache_dir)
            record["bundle_cache_dir"] = str(cache_dir)
            record["bundle_cache_resolves_to"] = resolved
            record["bundle_cache_is_symlink"] = str(cache_dir) != resolved
            record["bundle_cache_commit"] = meta.get("commit")
            record["bundle_cache_ref"] = meta.get("ref")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("engine provenance: cache lookup failed: %s", exc)

    try:
        record["editable_pth"] = editable_pth_targets()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("engine provenance: .pth scan failed: %s", exc)

    record["warnings"] = shadow_warnings(record)
    return record


def engine_provenance(*, refresh: bool = False) -> dict[str, Any]:
    """The running engine's identity.  Collected once, then cached.

    Never raises: a collector that fails leaves its fields ``None`` rather than
    taking the run down with it.
    """
    global _CACHED
    if _CACHED is None or refresh:
        try:
            _CACHED = _collect()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("engine provenance: collection failed: %s", exc)
            _CACHED = {
                "module": MODULE_NAME,
                "module_file": str(Path(__file__).parent / "__init__.py"),
                "warnings": [f"engine provenance unavailable: {exc}"],
            }
    return dict(_CACHED)


def reset_cache() -> None:
    """Drop the cached record (tests; nothing in a run needs this)."""
    global _CACHED
    _CACHED = None


# ---------------------------------------------------------------------------
# The guard (recipes-669 / recipes-ecd)
# ---------------------------------------------------------------------------


def shadow_warnings(record: dict[str, Any]) -> list[str]:
    """Sentences naming *both* paths whenever the import path is shadowed.

    Two conditions, both of which produced a silent wrong-engine run in the
    field, and neither of which is visible from a run's output:

    1. The engine was imported from the bundle cache, but the cache directory
       is a symlink -- so "the cached bundle" is really some worktree.
    2. The engine was imported from the bundle cache, but an editable ``.pth``
       on this interpreter points somewhere else -- so the *next* import in
       another process may not be this code at all.
    """
    cache_dir = record.get("bundle_cache_dir")
    if not cache_dir:
        return []

    warnings: list[str] = []
    if record.get("bundle_cache_is_symlink"):
        warnings.append(
            "tool-recipes engine: bundle cache "
            f"{cache_dir} is a SYMLINK resolving to "
            f"{record.get('bundle_cache_resolves_to')} -- every `amplifier` run "
            "on this machine imports that tree, not the cached bundle."
        )

    # A ``.pth`` names the directory *containing* the package, so compare it
    # against the parent of the package dir that actually got imported -- and
    # compare resolved, so a symlinked cache is caught by the clause above
    # rather than reported twice here.
    module_dir = str(record.get("module_dir") or "")
    package_parent_real = (
        os.path.realpath(os.path.dirname(module_dir)) if module_dir else ""
    )
    for entry in record.get("editable_pth") or []:
        target_real = entry.get("target_real")
        if not target_real:
            continue
        if package_parent_real and os.path.realpath(target_real) == package_parent_real:
            continue
        warnings.append(
            "tool-recipes engine: editable install "
            f"{entry.get('pth')} points at {target_real}, "
            f"but this engine was imported from {record.get('module_dir')} "
            f"(bundle cache {cache_dir}) -- the two disagree about whose code runs."
        )
    return warnings


def guard_enabled() -> bool:
    raw = os.environ.get(ENV_GUARD)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def warn_if_shadowed() -> list[str]:
    """Log the shadow warnings once, at import.  Never fails a run."""
    if not guard_enabled():
        return []
    try:
        warnings = engine_provenance().get("warnings") or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("engine provenance: guard failed: %s", exc)
        return []
    for message in warnings:
        logger.warning("%s", message)
    return list(warnings)


# ---------------------------------------------------------------------------
# Riding beside a tool result
# ---------------------------------------------------------------------------


def label_engine_provenance(result: Any) -> Any:
    """Attach ``engine`` beside a tool result's payload and return it.

    Beside, not inside: see this module's docstring, and the identical
    mechanism in :func:`runner_adapter.label_execution_mode`.
    """
    try:
        object.__setattr__(result, "engine", engine_provenance())
    except (AttributeError, TypeError):  # pragma: no cover - exotic result types
        logger.debug("Could not label engine provenance on %r", type(result))
    return result


def engine_provenance_of(result: Any) -> dict[str, Any] | None:
    """Read back the record set by :func:`label_engine_provenance`."""
    record = getattr(result, "engine", None)
    return record if isinstance(record, dict) else None
