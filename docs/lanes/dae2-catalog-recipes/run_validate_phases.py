#!/usr/bin/env python3
"""Run foundation's `validate-agents` DETERMINISTIC phases against a checkout, at $0.

The recipe's `environment-check`, `agent-discovery`, `structural-validation` and
`quality-classification` steps are pure `type: bash` python heredocs -- no LLM,
no spend. This runner executes those four steps VERBATIM out of the recipe YAML
(it re-implements nothing, so it cannot silently diverge from the recipe) and
prints the discovered agent count, the structural summary, the quality level and
the per-agent classification.

Everything downstream of `quality-classification` in that recipe IS an LLM step
(`description-quality-check`, `tool-access-analysis`, `synthesize-report`); this
lane's spend authority is $0, so those are not run and this file says so rather
than implying a full-recipe verdict. Precedent: the kp79 lane on
amplifier-bundle-attractor did exactly this.

    usage: run_validate_phases.py <validate-agents.yaml> <repo-path>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

DETERMINISTIC = [
    "environment-check",
    "agent-discovery",
    "structural-validation",
    "quality-classification",
]


def substitute(command: str, ctx: dict) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        if key not in ctx:
            return match.group(0)
        value = ctx[key]
        return value if isinstance(value, str) else json.dumps(value)

    return re.sub(r"\{\{([^}]+)\}\}", repl, command)


def main(recipe_path: str, repo_path: str) -> int:
    recipe = yaml.safe_load(Path(recipe_path).read_text(encoding="utf-8"))
    steps = {s["id"]: s for s in recipe["steps"]}
    ctx: dict = {"repo_path": repo_path}

    for step_id in DETERMINISTIC:
        step = steps[step_id]
        proc = subprocess.run(
            ["bash", "-c", substitute(step["command"], ctx)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            print(f"STEP {step_id} FAILED rc={proc.returncode}", file=sys.stderr)
            print(proc.stderr[-2000:], file=sys.stderr)
            return proc.returncode
        try:
            ctx[step["output"]] = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            print(f"STEP {step_id}: non-JSON output", file=sys.stderr)
            print(proc.stdout[-2000:], file=sys.stderr)
            return 1

    struct = ctx["structural_results"]
    qual = ctx["quality_classification"]
    print(f'validate-agents v{recipe["version"]} -- DETERMINISTIC phases 0-3 only, $0 (no LLM)')
    print(f"recipe: {recipe_path}")
    print(f"repo: {repo_path}")
    print(f'agents discovered: {ctx["discovery_results"]["total_count"]}')
    print(f'structural summary: {struct["summary"]}')
    print(f'quality_level: {qual["quality_level"]}')
    print(f'quality summary: {qual["summary"]}')
    for agent in struct["agents"]:
        errors = [e.get("code") for e in agent.get("errors", [])]
        warnings = [w.get("code") for w in agent.get("warnings", [])]
        print(
            f'  {agent["name"]:20s} chars={agent["description_length"]:5d} '
            f'examples={agent["example_count"]} commentary={agent["commentary_count"]} '
            f"errors={errors} warnings={warnings}"
        )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
