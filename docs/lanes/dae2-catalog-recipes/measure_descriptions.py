#!/usr/bin/env python3
"""Static A/B of agent `meta.description` between two checkouts of this repo, at $0.

Reads the description string straight out of each agent file's YAML frontmatter --
no `amplifier` process, no scratch AMPLIFIER_HOME, no session construction, no LLM
call. (GOAL.md's CENSUS SAFETY rule: running `amplifier` on this host with a
scratch AMPLIFIER_HOME silently rewrites the real install's editable `.pth`
files. Static reads cannot do that.)

Reports, per agent: description char count, `<example>`/`<commentary>` block
counts, whether the description opens trigger-first, whether it carries an
explicit USE WHEN / DO NOT USE WHEN, and the md5 of the BODY (everything after
the closing frontmatter fence) so a body-touching edit cannot hide.

    usage: measure_descriptions.py <stock-dir> <lean-dir>

where each dir is a checkout root containing agents/*.md. Exits non-zero if any
body md5 differs or any lean description breaks the standard.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import yaml

TRIGGER_OPENERS = ("USE WHEN", "USE PROACTIVELY WHEN")
CAP = 600


def load(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise SystemExit(f"{path}: no frontmatter fence")
    _, _, rest = text.partition("---")
    front, fence, body = rest.partition("\n---")
    if not fence:
        raise SystemExit(f"{path}: frontmatter never closed")
    meta = yaml.safe_load(front)
    return meta["meta"]["description"], body


def report(root: Path, label: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    total = 0
    print(f"===== {label}: {root} =====")
    for path in sorted((root / "agents").glob("*.md")):
        desc, body = load(path)
        md5 = hashlib.md5(body.encode("utf-8")).hexdigest()
        total += len(desc)
        print(
            f"  {path.stem:20s} chars={len(desc):4d} "
            f"examples={desc.count('<example>')} commentary={desc.count('<commentary>')} "
            f"trigger_first={desc.startswith(TRIGGER_OPENERS)} "
            f"use_when={'USE WHEN' in desc} do_not={'DO NOT USE WHEN' in desc} "
            f"body_md5={md5}"
        )
        out[path.stem] = (desc, md5)
    print(f"  TOTAL description chars: {total}")
    return out


def main(stock_dir: str, lean_dir: str) -> int:
    stock = report(Path(stock_dir), "STOCK")
    lean = report(Path(lean_dir), "LEAN")

    failures: list[str] = []
    if set(stock) != set(lean):
        failures.append(f"agent set differs: {sorted(stock)} vs {sorted(lean)}")

    print("\n===== VERDICT =====")
    for name in sorted(set(stock) & set(lean)):
        s_desc, s_md5 = stock[name]
        l_desc, l_md5 = lean[name]
        if s_md5 != l_md5:
            failures.append(f"{name}: BODY CHANGED {s_md5} -> {l_md5}")
        if len(l_desc) > CAP:
            failures.append(f"{name}: description {len(l_desc)} > {CAP} chars")
        if not l_desc.startswith(TRIGGER_OPENERS):
            failures.append(f"{name}: description is not trigger-first")
        if "DO NOT USE WHEN" not in l_desc:
            failures.append(f"{name}: no explicit DO NOT USE WHEN")
        if l_desc.count("<example>") or l_desc.count("<commentary>"):
            failures.append(f"{name}: example/commentary block present")
        print(
            f"  {name:20s} {len(s_desc):4d} -> {len(l_desc):4d} chars "
            f"({len(l_desc) - len(s_desc):+d})  body md5 {s_md5} == {l_md5} "
            f"{'OK' if s_md5 == l_md5 else 'MISMATCH'}"
        )
    s_total = sum(len(d) for d, _ in stock.values())
    l_total = sum(len(d) for d, _ in lean.values())
    print(f"  {'REPO TOTAL':20s} {s_total:4d} -> {l_total:4d} chars ({l_total - s_total:+d})")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS: bodies byte-identical, every description trigger-first, <=600, USE WHEN + DO NOT USE WHEN, zero example/commentary blocks.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
