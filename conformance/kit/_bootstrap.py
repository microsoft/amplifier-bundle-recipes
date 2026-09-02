"""Locate the ``amplifier-recipe-runner`` library and expose the shared
resolved-graph serializer both hosts in the kit use.

The kit deliberately does **not** vendor, stub, or re-implement any runner
behavior. It imports the real library and asserts against it. If the library is
not importable the kit says so and stops -- it never degrades into asserting
against a double, because a kit that passes without the implementation present
would prove nothing.

Search order for the library source:

1. Already importable (installed, or already on ``sys.path``).
2. ``$AMPLIFIER_RECIPE_RUNNER_SRC`` -- explicit override, checked first among
   paths so a caller can always point the kit at a specific checkout.
3. Conventional sibling checkouts, relative to this repo.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "RUNNER_PACKAGE",
    "RunnerUnavailable",
    "ensure_runner_importable",
    "graph_identity",
    "runner_source_path",
]

RUNNER_PACKAGE = "amplifier_recipe_runner"

#: Where the library is expected to live when it is not installed. Relative to
#: the repo root (three parents up from this file: kit -> conformance -> repo).
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CANDIDATES: tuple[Path, ...] = (
    # hw-recipes lane layout: lanes/<lane>/amplifier-bundle-recipes
    _REPO_ROOT.parents[2] / "amplifier-recipe-runner" / "src",
    # flat sibling checkouts
    _REPO_ROOT.parent / "amplifier-recipe-runner" / "src",
    _REPO_ROOT.parent.parent / "amplifier-recipe-runner" / "src",
)


class RunnerUnavailable(RuntimeError):
    """The runner library could not be located. Reported, never worked around."""

    def __init__(self, tried: tuple[str, ...]) -> None:
        listed = "\n  ".join(tried) or "(none)"
        super().__init__(
            f"{RUNNER_PACKAGE} is not importable, and the conformance kit refuses to "
            f"assert against a stand-in. Tried:\n  {listed}\n"
            "Set AMPLIFIER_RECIPE_RUNNER_SRC to the library's `src` directory, or "
            "`pip install -e` the amplifier-recipe-runner checkout."
        )
        self.tried = tried


def _explicit_candidate() -> Path | None:
    raw = os.environ.get("AMPLIFIER_RECIPE_RUNNER_SRC")
    return Path(raw).expanduser() if raw else None


def runner_source_path() -> str | None:
    """The path the kit added to ``sys.path``, or ``None`` if already importable."""
    return _ADDED[0]


_ADDED: list[str | None] = [None]


def ensure_runner_importable() -> str:
    """Make the runner importable, or raise :class:`RunnerUnavailable`.

    Returns a human-readable description of how it was found, so every kit run
    records which implementation it actually exercised.
    """
    if importlib.util.find_spec(RUNNER_PACKAGE) is not None:
        module = __import__(RUNNER_PACKAGE)
        return f"already importable: {module.__file__}"

    tried: list[str] = []
    explicit = _explicit_candidate()
    for candidate in ([explicit] if explicit else []) + list(_CANDIDATES):
        if candidate is None:
            continue
        resolved = candidate.resolve()
        tried.append(str(resolved))
        if (resolved / RUNNER_PACKAGE / "__init__.py").is_file():
            sys.path.insert(0, str(resolved))
            _ADDED[0] = str(resolved)
            module = __import__(RUNNER_PACKAGE)
            return f"loaded from {resolved} ({module.__file__})"

    raise RunnerUnavailable(tuple(tried))


# --------------------------------------------------------------------------
# The shared resolved-graph identity
# --------------------------------------------------------------------------

#: Fields that describe WHERE a dependency landed on this machine, not WHAT it
#: is. Two hosts on different machines legitimately differ here, so they are
#: excluded from identity comparison -- and named, rather than quietly dropped.
PLACEMENT_FIELDS: tuple[str, ...] = ("local_path",)


def graph_identity(plan: Any) -> dict[str, Any]:
    """Canonical, host-neutral identity of a resolved graph.

    Built from the library's own documented run-manifest shape
    (``recipe-runner-lib.v1`` Core 7) rather than a shape the kit invents, then
    stripped of the two per-run fields (``run_id``, ``created_at``) and of the
    placement fields above.
    """
    from amplifier_recipe_runner.provenance import run_manifest_from_plan

    manifest = run_manifest_from_plan(plan, run_id="fixed", created_at="fixed")
    data = manifest.to_mapping()
    data.pop("run_id", None)
    data.pop("created_at", None)
    for dependency in data.get("dependencies", []):
        for key in PLACEMENT_FIELDS:
            dependency.pop(key, None)
    for agent in (data.get("agents") or {}).values():
        for key in PLACEMENT_FIELDS:
            agent.pop(key, None)
    return data
