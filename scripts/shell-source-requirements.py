#!/usr/bin/env python3
"""Expose declared parser prerequisites to no-project test environments."""

from pathlib import Path
import tomllib


root = Path(__file__).resolve().parents[1]
requirements = []
for path in (root / "pyproject.toml", root / "modules/tool-recipes/pyproject.toml"):
    dependencies = tomllib.loads(path.read_text())["project"]["dependencies"]
    selected = sorted(item for item in dependencies if item.startswith("tree-sitter"))
    names = {item.split("==", 1)[0] for item in selected}
    if names != {"tree-sitter", "tree-sitter-bash"} or len(selected) != 2:
        raise SystemExit(f"{path}: expected exactly two pinned parser requirements")
    requirements.append(selected)
if requirements[0] != requirements[1]:
    raise SystemExit("runner and tool module parser requirements differ")
print("\n".join(requirements[0]))
