"""The native runner ships exactly the canonical compiler, with no drift."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "shell_source_sync", ROOT / "scripts/sync-shell-source.py"
)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def test_checked_in_mirror_is_fresh():
    assert sync.synchronize(ROOT, check=True) == [], (
        "Run python scripts/sync-shell-source.py"
    )


def test_mirror_detects_and_repairs_modified_missing_and_stale_files(tmp_path):
    source = tmp_path / sync.SOURCE
    source.mkdir(parents=True)
    (source / "__init__.py").write_bytes(b'"""compiler"""\n')
    (source / "policy.py").write_bytes(b"ANSWER = 42\n")
    assert sync.synchronize(tmp_path) == ["__init__.py", "policy.py"]
    mirror = tmp_path / sync.MIRROR
    (mirror / "policy.py").write_bytes(b"ANSWER = 0\n")
    (mirror / "__init__.py").unlink()
    (mirror / "stale.py").write_bytes(b"old = True\n")
    (mirror / "note.txt").write_text("not generated")
    expected = ["__init__.py", "policy.py", "stale.py"]
    assert sync.synchronize(tmp_path, check=True) == expected
    assert (mirror / "policy.py").read_bytes() == b"ANSWER = 0\n"
    assert sync.synchronize(tmp_path) == expected
    assert sync.synchronize(tmp_path, check=True) == []
    assert (mirror / "policy.py").read_bytes() == (source / "policy.py").read_bytes()
    assert (mirror / "note.txt").read_text() == "not generated"


def test_both_installable_packages_pin_the_verified_parser_profile():
    pins = {"tree-sitter==0.25.2", "tree-sitter-bash==0.25.1"}
    for relative in ["pyproject.toml", "modules/tool-recipes/pyproject.toml"]:
        with (ROOT / relative).open("rb") as stream:
            dependencies = tomllib.load(stream)["project"]["dependencies"]
        assert pins <= set(dependencies)
