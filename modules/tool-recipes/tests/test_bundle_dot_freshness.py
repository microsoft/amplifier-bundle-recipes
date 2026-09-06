"""The checked-in ``bundle.dot`` must still describe the repo at HEAD.

The rule, in one sentence:

    Regenerating ``bundle.dot`` from the repo as it stands must reproduce the
    file that is checked in -- same ``source_hash``, same bytes.

Why this exists
---------------
``bundle.dot`` / ``bundle.png`` are generated artifacts: they are produced by
``amplifier_foundation.bundle_docs.bundle_to_dot.bundle_repo_dot(repo_root)``,
which walks the repo and annotates every node with a token-cost estimate. Edit
``bundle.md``, an agent description, or any counted context file and the
diagram is immediately wrong -- but nothing said so, because no gate ever
regenerated it.

It rotted exactly that way (recipes-qok). At the point this test was written
the checked-in diagram still claimed ``v1.0.0`` and

===============================  ==========  ===========
label                            checked in  regenerated
===============================  ==========  ===========
``recipe-author`` description      ~408 tok     ~140 tok
``result-validator`` description   ~396 tok     ~144 tok
``recipes-behavior``              ~1393 tok     ~873 tok
``recipe-instructions.md``        ~2001 tok    ~2771 tok
===============================  ==========  ===========

-- roughly a 3x error on the agent descriptions, silent for as long as nobody
happened to regenerate. Worse, it made the staleness *contagious*: the next
lane to legitimately touch ``bundle.md`` had the choice of leaving the diagram
wrong or dragging ~50 lines of somebody else's drift into its own diff.

Two assertions, not one
-----------------------
``source_hash`` is a SHA-256 over the graph **body only** (nodes + edges) --
see ``bundle_to_dot.py``'s "Source hash" section, which hashes ``body_str``
before the header is assembled. The graph ``label`` -- which carries the bundle
**version** -- sits in the header and is therefore *outside* the hash. A
version bump alone moves the visible title and not the hash, which is the exact
drift observed here (``v1.0.0`` vs ``v1.0.1``). So the hash is checked because
it is the artifact's own declared identity, and the full bytes are checked
because the hash alone would have let this one through.

Remediation is the same for both: run the command in ``_REGEN_COMMAND`` from
the repo root and commit the two regenerated files. That command is also
documented in ``AGENTS.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from amplifier_foundation.bundle_docs.bundle_to_dot import bundle_repo_dot

# <repo-root>/modules/tool-recipes/tests/this_file.py -> <repo-root>
_REPO_ROOT = Path(__file__).resolve().parents[3]

_BUNDLE_DOT = _REPO_ROOT / "bundle.dot"
_BUNDLE_PNG = _REPO_ROOT / "bundle.png"

#: The one way to bring both artifacts back in sync. Kept as a single constant
#: so the failure message and ``AGENTS.md`` cannot drift apart.
_REGEN_COMMAND = (
    '"${AMPLIFIER_PYTHON:-python3}" -c "from amplifier_foundation.bundle_docs'
    ".bundle_to_dot import bundle_repo_dot; "
    "open('bundle.dot','w').write(bundle_repo_dot('.'))\"\n"
    "    dot -Tpng bundle.dot -o bundle.png"
)

_SOURCE_HASH_RE = re.compile(r'^\s*source_hash="([0-9a-f]{64})"\s*$', re.MULTILINE)


def _declared_source_hash(dot_text: str) -> str | None:
    """Return the ``source_hash`` graph attribute, or ``None`` if absent."""
    match = _SOURCE_HASH_RE.search(dot_text)
    return match.group(1) if match else None


@pytest.fixture(scope="module")
def checked_in_dot() -> str:
    assert _BUNDLE_DOT.is_file(), f"{_BUNDLE_DOT} is missing"
    return _BUNDLE_DOT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def regenerated_dot() -> str:
    """``bundle.dot`` as the generator produces it from the repo right now.

    ``bundle_repo_dot`` is a pure function -- it reads the repo and returns a
    string, writing nothing -- so regenerating here cannot disturb the working
    tree. The tests below write the result into pytest's ``tmp_path`` when they
    need a file to point the reader at.
    """
    return bundle_repo_dot(_REPO_ROOT)


def test_checked_in_bundle_dot_declares_a_source_hash(checked_in_dot: str) -> None:
    """Without the attribute there is nothing to compare, so say so loudly."""
    assert _declared_source_hash(checked_in_dot) is not None, (
        f"{_BUNDLE_DOT.name} has no source_hash= graph attribute. It was not "
        "produced by bundle_repo_dot, or was hand-edited afterwards. "
        f"Regenerate it from the repo root:\n\n    {_REGEN_COMMAND}\n"
    )


def test_bundle_dot_source_hash_matches_regenerated(
    checked_in_dot: str, regenerated_dot: str, tmp_path: Path
) -> None:
    """The checked-in hash equals the hash of a fresh regeneration."""
    checked_in_hash = _declared_source_hash(checked_in_dot)
    regenerated_hash = _declared_source_hash(regenerated_dot)

    if checked_in_hash == regenerated_hash:
        return

    written = tmp_path / "bundle.dot"
    written.write_text(regenerated_dot, encoding="utf-8")

    pytest.fail(
        "bundle.dot is stale -- it no longer describes this repo.\n\n"
        f"  checked in  : {checked_in_hash}\n"
        f"  regenerated : {regenerated_hash}\n\n"
        f"A fresh regeneration was written to {written} for comparison.\n\n"
        "Something counted by the diagram changed (bundle.md, an agent\n"
        "description, a context file, a module's tool schema) without the\n"
        "diagram being regenerated -- or amplifier-foundation's generator\n"
        "itself changed. Either way the fix is the same. From the repo root:\n\n"
        f"    {_REGEN_COMMAND}\n\n"
        "then commit both bundle.dot and bundle.png."
    )


def test_bundle_dot_is_byte_identical_to_regenerated(
    checked_in_dot: str, regenerated_dot: str, tmp_path: Path
) -> None:
    """Bytes, not just the hash -- the header (and so the version) is unhashed.

    ``source_hash`` covers the body only. The graph ``label`` carrying the
    bundle version lives in the header, so a version bump moves the title and
    leaves the hash untouched. This is the assertion that catches it.
    """
    if checked_in_dot == regenerated_dot:
        return

    written = tmp_path / "bundle.dot"
    written.write_text(regenerated_dot, encoding="utf-8")

    checked_in_lines = checked_in_dot.splitlines()
    regenerated_lines = regenerated_dot.splitlines()
    differing = [
        f"  line {n}:\n    checked in  : {old}\n    regenerated : {new}"
        for n, (old, new) in enumerate(zip(checked_in_lines, regenerated_lines), 1)
        if old != new
    ]
    if len(checked_in_lines) != len(regenerated_lines):
        differing.append(
            f"  line count: checked in {len(checked_in_lines)}, "
            f"regenerated {len(regenerated_lines)}"
        )

    pytest.fail(
        "bundle.dot differs from a fresh regeneration.\n\n"
        f"  checked in  source_hash: {_declared_source_hash(checked_in_dot)}\n"
        f"  regenerated source_hash: {_declared_source_hash(regenerated_dot)}\n\n"
        + "\n".join(differing[:20])
        + (f"\n  ... and {len(differing) - 20} more" if len(differing) > 20 else "")
        + f"\n\nA fresh regeneration was written to {written}.\n"
        "From the repo root:\n\n"
        f"    {_REGEN_COMMAND}\n\n"
        "then commit both bundle.dot and bundle.png."
    )


def test_bundle_png_is_present_and_is_a_png() -> None:
    """``bundle.png`` is rendered from ``bundle.dot`` and ships beside it.

    The PNG's *bytes* are not asserted: graphviz output is not stable across
    versions, so pinning them would fail on a different host rather than on a
    real drift. What is asserted is that the rendered artifact exists at all --
    the failure mode where ``bundle.dot`` is regenerated and the PNG is
    forgotten.
    """
    assert _BUNDLE_PNG.is_file(), (
        f"{_BUNDLE_PNG} is missing. Render it from bundle.dot:\n\n"
        f"    {_REGEN_COMMAND}\n"
    )
    header = _BUNDLE_PNG.read_bytes()[:8]
    assert header == b"\x89PNG\r\n\x1a\n", (
        f"{_BUNDLE_PNG} is not a PNG (leading bytes: {header!r}). Re-render it:"
        f"\n\n    {_REGEN_COMMAND}\n"
    )
