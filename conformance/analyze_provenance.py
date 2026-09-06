#!/usr/bin/env python3
"""Check that a plan's Core 7 provenance map actually discriminates.

Contract: ``recipe-dependency-manifest.v1`` Core 7 -- "agent-to-dependency
provenance map". A map that stamps every agent with the same declared
dependency satisfies the *shape* of Core 7 while saying nothing: measured on a
real migrated recipe, 39 agents, one declared dependency, and 26 of those
agents defined in entirely different checkouts at different revisions -- all 39
stamped with the same URI, revision and digest.

So this checks the claim rather than the field's presence. For every agent:

* ``local_path`` (the agent's own definition file) must lie inside
  ``defined_in`` (the tree the record says defines it). A definition outside
  the tree claimed for it is a MISMATCH -- the exact defect above.
* ``declared_by`` must be one of the recipe's own declared dependencies. An
  agent attributed to something the recipe never declared is a MISMATCH.
* An agent with a definition file but no ``defined_in`` is UNATTRIBUTED:
  honest (nothing is claimed) but unproven, so it is reported separately and
  never counted as a pass.

Usage::

    PYTHONPATH=src python3 conformance/analyze_provenance.py --assert
    PYTHONPATH=src python3 conformance/analyze_provenance.py recipes/*.yaml
    python3 conformance/analyze_provenance.py plan.json

A ``.yaml``/``.yml`` argument is planned through the library (which resolves
its dependencies, so it needs them fetchable or cached); a ``.json`` argument
is read as an already-emitted ``plan --json`` / run-manifest payload. With no
arguments, every recipe under ``recipes/`` is checked.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def plan_payload(recipe: Path) -> dict[str, Any]:
    """Plan ``recipe`` through the library and return its run-manifest mapping."""
    import asyncio

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from amplifier_recipe_runner.manifest import parse_manifest_file
    from amplifier_recipe_runner.planner import plan
    from amplifier_recipe_runner.provenance import run_manifest_from_plan
    from amplifier_recipe_runner.resolver import FoundationResolver

    manifest = parse_manifest_file(recipe)
    resolved = asyncio.run(plan(manifest, FoundationResolver(), recipe.parent))
    return run_manifest_from_plan(resolved, run_id="analyze").to_mapping()


def read_payload(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if "agents" in doc:
        return doc
    for key in ("plan", "manifest", "provenance"):
        nested = doc.get(key)
        if isinstance(nested, dict) and "agents" in nested:
            return nested
    return doc


def load(path: Path) -> dict[str, Any]:
    return plan_payload(path) if path.suffix in {".yaml", ".yml"} else read_payload(path)


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------


def inside(child: str | None, parent: str | None) -> bool:
    if not child or not parent:
        return False
    try:
        target = Path(child).resolve()
        root = Path(parent).resolve()
    except (OSError, ValueError):
        return False
    return target == root or target.is_relative_to(root)


def check(payload: dict[str, Any], label: str) -> tuple[int, int, int]:
    """Print one payload's report. Returns ``(ok, mismatched, unattributed)``."""
    agents: dict[str, Any] = payload.get("agents") or {}
    dependencies: list[dict[str, Any]] = payload.get("dependencies") or []
    declared = {dep.get("uri") for dep in dependencies}

    print(f"file: {label}")
    print(f"declared dependencies: {len(dependencies)}")
    for dep in dependencies:
        print(f"  {dep.get('uri')}")
        print(f"    resolved_revision={dep.get('resolved_revision')}")
        print(f"    local_path={dep.get('local_path')}")

    print(f"total agents in map: {len(agents)}")
    for field in ("supplied_by", "declared_by", "resolved_revision"):
        counts = Counter(agent.get(field) for agent in agents.values())
        print(f"distinct {field} values: {len(counts)}")
        for value, count in counts.most_common():
            print(f"  {count:3d}  {value}")

    ok: list[str] = []
    mismatched: list[tuple[str, str]] = []
    unattributed: list[str] = []

    for name, agent in sorted(agents.items()):
        local_path = agent.get("local_path")
        defined_in = agent.get("defined_in")
        declared_by = agent.get("declared_by")

        if declared and declared_by not in declared:
            mismatched.append((name, f"declared_by={declared_by!r} is not a declared dependency"))
            continue
        if not local_path:
            unattributed.append(f"{name}: no definition file recorded")
            continue
        if not defined_in:
            unattributed.append(f"{name}: no defining tree recorded for {local_path}")
            continue
        if not inside(local_path, defined_in):
            mismatched.append((name, f"{local_path} is not inside claimed tree {defined_in}"))
            continue
        ok.append(name)

    print()
    print(f"agents whose definition IS inside the tree claimed for it: {len(ok)}")
    print(f"agents with no defining tree recorded (unattributed): {len(unattributed)}")
    for line in unattributed:
        print(f"  UNATTRIBUTED {line}")
    print(f"agents attributed to a tree they are NOT in: {len(mismatched)}")
    for name, reason in mismatched:
        print(f"  MISMATCH {name}")
        print(f"           {reason}")
    print()
    return len(ok), len(mismatched), len(unattributed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="recipe .yaml or plan .json paths")
    parser.add_argument(
        "--assert",
        dest="assert_clean",
        action="store_true",
        help="exit non-zero unless every agent is attributed to a tree that really holds it",
    )
    args = parser.parse_args(argv)

    paths = args.paths or sorted((REPO_ROOT / "recipes").glob("*.yaml"))
    if not paths:
        print("no recipes or plans to check", file=sys.stderr)
        return 2

    total_ok = total_mismatch = total_unattributed = 0
    for path in paths:
        try:
            payload = load(path)
        except Exception as exc:  # noqa: BLE001 - report, never hide, a load failure
            print(f"file: {path}\n  FAILED TO LOAD: {type(exc).__name__}: {exc}\n")
            total_mismatch += 1
            continue
        ok, mismatched, unattributed = check(payload, str(path))
        total_ok += ok
        total_mismatch += mismatched
        total_unattributed += unattributed

    print(f"TOTAL: {total_ok} attributed, {total_mismatch} mismatched, {total_unattributed} unattributed")
    if args.assert_clean and (total_mismatch or total_unattributed):
        print("FAIL: provenance does not discriminate", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
