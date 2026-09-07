"""Guardrail pinning the `recipes` tool description.

The description renders into the tool-schema block of **every request of every
session** that mounts this bundle, used or not.  So its size is a per-request
cost paid unconditionally, and it is worth pinning: `g7h3` measured the full
lean head at -13.57% $/task, 95% CI [-22.27%, -4.86%].

What these tests hold:

* **Byte pin** -- the shipped description is byte-identical to a vendored copy
  (`data/recipes_description.v1.txt`).  Any edit to the string is a red test,
  not a silent regrowth.
* **Char budget** -- 1,181 chars, and the arithmetic behind that number is
  stated below rather than asserted as a bare figure.
* **Rule survival** -- every operation the input schema accepts is named in the
  description, and the `@recipes:examples/code-review.yaml` bundle pointer
  survives.  The operation list is *derived from the schema*, so adding an
  operation without documenting it is a red test.  That is the exact drift that
  produced the divergence recorded below.

DIVERGENCE FROM THE UPSTREAM ARTIFACT, STATED ON PURPOSE.
`zc6t` measured this description at 1,581 -> 1,020 chars and shipped
`docs/lanes/zc6t-lean-head-ship/patches/tool-descriptions/recipes.lean.txt`
(vendored here verbatim as `data/recipes_description.zc6t-v1-1020.txt`).  The
`engine_info` operation was added to this tool *after* that measurement, so the
1,020-char artifact does not mention it -- a genuine fidelity loss at today's
head, caught by re-verification rather than inherited from zc6t's table.  It is
restored here at +161 chars.  `test_pin_is_zc6t_v1_plus_one_restoration` holds
that this is the *only* divergence, so neither vendored file can drift quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from amplifier_module_tool_recipes import RecipesTool

DATA = Path(__file__).parent / "data"

#: The pinned text. Byte-for-byte what the tool must ship.
PINNED = (DATA / "recipes_description.v1.txt").read_text(encoding="utf-8")

#: zc6t's shipped artifact, unmodified. The base the pin is derived from.
ZC6T_V1 = (DATA / "recipes_description.zc6t-v1-1020.txt").read_text(encoding="utf-8")

#: 1,020 (zc6t v1) + 161 (the `engine_info` restoration) = 1,181.
CHAR_BUDGET = 1_181

#: Pre-change size of this description at today's head, for the record. The
#: budget must stay strictly under it or the change bought nothing.
STOCK_CHARS = 1_788

#: The one line restored on top of zc6t's artifact.
RESTORED_LINE = (
    "- engine_info - report which tool-recipes engine is running (module file, "
    "version, git sha, bundle-cache/editable-install shadowing) without "
    "executing anything.\n"
)

#: Pointers that must survive compression. `@recipes:examples/my-recipe.yaml`
#: is deliberately NOT here: it was an invented filename inside a second usage
#: example, adjudicated a false positive on `model_performance-d8s3`. The real
#: pointer is the one below.
REQUIRED_POINTERS = ("@recipes:examples/code-review.yaml",)


def _description() -> str:
    """The description exactly as the tool serves it.

    `description` is a plain property over a literal and touches no instance
    state, so it is read off the class -- no coordinator, no executor, and no
    re-derivation of the string from source that could disagree with it.
    """
    return RecipesTool.description.fget(None)  # type: ignore[misc]


def _schema_operations() -> list[str]:
    """Operation names the input schema actually accepts."""
    schema = RecipesTool.input_schema.fget(None)  # type: ignore[misc]
    return list(schema["properties"]["operation"]["enum"])


def test_description_is_byte_identical_to_pin() -> None:
    assert _description() == PINNED, (
        "The `recipes` tool description no longer matches its pinned copy at "
        f"{DATA / 'recipes_description.v1.txt'}. If the change is intended, "
        "update that file, restate the char budget arithmetic, and re-check "
        "rule survival -- do not delete this test."
    )


def test_description_within_char_budget() -> None:
    actual = len(_description())
    assert actual <= CHAR_BUDGET, f"{actual} chars exceeds budget {CHAR_BUDGET}"
    assert CHAR_BUDGET < STOCK_CHARS, "budget must stay under the pre-change size"


def test_char_budget_arithmetic_holds() -> None:
    assert len(ZC6T_V1) == 1_020
    assert len(PINNED) == CHAR_BUDGET
    assert len(PINNED) - len(ZC6T_V1) == 161


def test_pin_is_zc6t_v1_plus_one_restoration() -> None:
    """The pin is zc6t's artifact plus exactly one restored line, nothing else."""
    anchor = (
        "- cancel - cancel a running session (session_id; `immediate: true` "
        "does not wait for the current step).\n"
    )
    assert anchor in ZC6T_V1
    assert ZC6T_V1.replace(anchor, anchor + RESTORED_LINE, 1) == PINNED


@pytest.mark.parametrize("operation", _schema_operations())
def test_every_schema_operation_is_named(operation: str) -> None:
    assert re.search(rf"(?<![\w]){re.escape(operation)}(?![\w])", _description()), (
        f"operation {operation!r} is accepted by the input schema but never "
        "named in the tool description -- callers cannot discover it"
    )


def test_schema_operation_set_has_not_shrunk() -> None:
    """A shrinking enum would silently weaken the test above."""
    assert set(_schema_operations()) >= {
        "execute",
        "resume",
        "list",
        "validate",
        "approvals",
        "approve",
        "deny",
        "cancel",
        "engine_info",
    }


@pytest.mark.parametrize("pointer", REQUIRED_POINTERS)
def test_required_pointer_survives(pointer: str) -> None:
    assert pointer in _description()
