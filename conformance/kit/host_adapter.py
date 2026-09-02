#!/usr/bin/env python3
"""A second, independent HOST for the runner library -- a standalone process.

``recipe-runner-lib.v1`` Core 1 says every host surface is a thin adapter over
the one library. Its Conformance section demands that the same recipe run
through more than one host yields *identical resolved-graph identity*. This
file is that second host: a separate OS process, sharing no in-memory state
with the kit, that plans a recipe and prints the resolved graph as JSON.

It is deliberately thin -- parse argv, build a ``RunRequest``, call ``plan``,
print. It carries no workflow, resolution, or agent-catalog logic of its own,
which is the property Core 1 is about.

Standing in for the real CLI, honestly
--------------------------------------
``amplifier-recipe-runner``'s ``pyproject.toml`` declares a
``recipe-runner = amplifier_recipe_runner.cli:main`` console script, but the
``cli`` module does not exist yet. When it lands, ``kit.py`` prefers it and this
adapter becomes a fallback; the fixture reports which surface it actually used
either way, and never claims to have exercised a CLI it could not find.

Usage::

    python host_adapter.py --recipe <path> --fixtures <dir> [--expect-error]

Exit codes: ``0`` planned, ``2`` typed preflight refusal (still JSON on stdout,
with ``error``/``message``), ``1`` anything else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import ensure_runner_importable  # noqa: E402
from _bootstrap import graph_identity  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    args = parser.parse_args(argv)

    ensure_runner_importable()

    from amplifier_recipe_runner.api import RunRequest
    from amplifier_recipe_runner.errors import PreflightError
    from amplifier_recipe_runner.execution import plan as plan_recipe
    from amplifier_recipe_runner.manifest import ManifestError
    from amplifier_recipe_runner.resolver import LocalBundleResolver

    resolver = LocalBundleResolver(base_path=args.fixtures.resolve())
    request = RunRequest(recipe=args.recipe.resolve())

    try:
        resolved = asyncio.run(plan_recipe(request, resolver=resolver))
    except (PreflightError, ManifestError) as exc:
        json.dump(
            {"error": type(exc).__name__, "message": str(exc)},
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        return 2

    json.dump(graph_identity(resolved), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
