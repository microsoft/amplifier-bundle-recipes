"""Flat stage-level approval keys are refused by name, never dropped (recipes-c9d).

``Stage`` has exactly three fields -- ``name``, ``steps``, ``approval`` -- so
``approval_required`` / ``approval_message`` / ``auto_approve_if`` were dropped
by :meth:`Recipe._parse_stage` without a word. A stage that presented itself,
at length, as a human approval gate ("APPROVE to accept ...") had never gated
anything: the run went straight through it to completion with no prompt.

Measured on the shipped example that surfaced this
(``examples/context-intelligence/verification/adversarial-verification.yaml``)::

    Recipe.from_yaml(<that file>) -> stage.approval is None for every stage

These tests pin BOTH directions: the flat keys are refused by name with a
remedy, and the canonical ``approval:`` block still parses into a real gate.
The schema-v2 library refuses the identical shape at manifest parse -- see
``src/amplifier_recipe_runner/tests/test_flat_stage_approval_keys.py`` -- and
``test_both_engines_agree_on_the_key_list`` pins the two lists together.
"""

from pathlib import Path

import pytest
import yaml
from amplifier_module_tool_recipes.models import FLAT_STAGE_APPROVAL_KEYS
from amplifier_module_tool_recipes.models import Recipe

FLAT_KEY_VALUES = {
    "approval_required": True,
    "approval_message": "Review before continuing.",
    "auto_approve_if": ["{{score}} > 0"],
}

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADVERSARIAL = (
    _REPO_ROOT
    / "examples/context-intelligence/verification/adversarial-verification.yaml"
)


def _stage(**extra):
    return {
        "name": "final_review",
        "steps": [{"id": "look", "type": "bash", "command": "echo looked"}],
        **extra,
    }


# --- reject path ----------------------------------------------------------


@pytest.mark.parametrize("key", sorted(FLAT_KEY_VALUES))
def test_each_flat_key_is_refused_by_name(key):
    """Each key is named, with the stage it was found on and the remedy."""
    with pytest.raises(ValueError) as excinfo:
        Recipe._parse_stage(_stage(**{key: FLAT_KEY_VALUES[key]}))

    message = str(excinfo.value)
    assert f"'{key}'" in message, message
    assert "final_review" in message, message
    assert "approval:" in message, message


def test_all_three_together_are_all_named():
    """The exact shape the defect was found in names every offending key."""
    with pytest.raises(ValueError) as excinfo:
        Recipe._parse_stage(_stage(**FLAT_KEY_VALUES))

    message = str(excinfo.value)
    for key in FLAT_KEY_VALUES:
        assert f"'{key}'" in message, message


def test_auto_approve_if_remedy_does_not_invent_a_field():
    """No engine here evaluates a conditional auto-approval, so none is offered.

    Aliasing it onto something would be worse than refusing: the behaviour
    would still be dropped, but now while claiming to have been honoured.
    """
    with pytest.raises(ValueError) as excinfo:
        Recipe._parse_stage(_stage(auto_approve_if=["{{x}} > 0"]))

    assert "no conditional auto-approval" in str(excinfo.value)


def test_approval_required_false_is_still_refused():
    """The falsey case declared no gate -- but it did declare the key.

    Reading the key only when it is truthy is what let the truthy case ship
    unnoticed for as long as it did.
    """
    with pytest.raises(ValueError, match="approval_required"):
        Recipe._parse_stage(_stage(approval_required=False))


def test_unnamed_stage_is_still_refused():
    stage = _stage(approval_required=True)
    del stage["name"]
    with pytest.raises(ValueError, match="approval_required"):
        Recipe._parse_stage(stage)


def test_a_whole_recipe_carrying_the_flat_keys_fails_to_load(tmp_path):
    """``Recipe.from_yaml`` is the loader every legacy path goes through."""
    recipe = tmp_path / "flat.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "name": "flat-gate",
                "description": "A gate that never gated",
                "version": "1.0.0",
                "stages": [_stage(approval_required=True, approval_message="Go on?")],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        Recipe.from_yaml(recipe)

    assert "approval_required" in str(excinfo.value)


# --- accept path ----------------------------------------------------------


def test_canonical_approval_block_still_builds_a_real_gate():
    stage = Recipe._parse_stage(
        _stage(approval={"required": True, "prompt": "Accept it?"})
    )
    assert stage.approval is not None
    assert stage.approval.required is True
    assert stage.approval.prompt == "Accept it?"
    assert stage.validate() == []


def test_a_stage_with_no_approval_at_all_is_unchanged():
    stage = Recipe._parse_stage(_stage())
    assert stage.approval is None
    assert stage.validate() == []


def test_a_context_variable_of_the_same_name_is_untouched(tmp_path):
    """``approval_required`` is a legitimate CONTEXT variable name.

    ``examples/context-intelligence/synthesis/action-executor.yaml`` declares
    exactly that. The refusal is scoped to stage mappings.
    """
    recipe = tmp_path / "ctx.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "name": "ctx",
                "description": "A context variable, not a stage key",
                "version": "1.0.0",
                "context": {"approval_required": True, "auto_approve_if": ["x"]},
                "steps": [{"id": "look", "type": "bash", "command": "echo hi"}],
            }
        ),
        encoding="utf-8",
    )

    loaded = Recipe.from_yaml(recipe)
    assert loaded.context["approval_required"] is True


# --- the shipped example the defect was found in --------------------------


def test_the_shipped_adversarial_example_now_carries_a_real_gate():
    """Its ``final_review`` stage prompts; the other four declare no gate.

    Before recipes-c9d this file's ``final_review`` stage read as a human
    approval gate and parsed to ``approval is None`` -- it ran through to
    completion without prompting.
    """
    recipe = Recipe.from_yaml(_ADVERSARIAL)
    gates = {stage.name: stage.approval for stage in recipe.stages}

    final = gates["final_review"]
    assert final is not None
    assert final.required is True
    assert "APPROVE" in final.prompt
    assert final.validate() == []

    for name, approval in gates.items():
        if name == "final_review":
            continue
        assert approval is not None and approval.required is False, name


def test_no_shipped_recipe_carries_a_flat_stage_approval_key():
    """A grep the next author cannot forget to run.

    Only stage mappings are checked: a context variable or step field of the
    same name is legitimate and common.
    """
    offenders = []
    for path in sorted(_REPO_ROOT.rglob("*.y*ml")):
        if ".git" in path.parts:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
            continue
        for index, stage in enumerate(data["stages"]):
            if not isinstance(stage, dict):
                continue
            found = [k for k in FLAT_STAGE_APPROVAL_KEYS if k in stage]
            if found:
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:stages[{index}] {found}")

    assert offenders == [], "flat stage approval keys still shipped: " + "; ".join(
        offenders
    )


# --- the two engines must not disagree about the same file ----------------


def test_both_engines_agree_on_the_key_list():
    """The legacy map is duplicated, not imported -- so pin it to the library's.

    The runner library is an optional dependency of this module, so the legacy
    loader cannot import from it. When it IS importable, the two maps must be
    identical or the same recipe would be refused by one engine and accepted
    by the other.
    """
    library_manifest = pytest.importorskip("amplifier_recipe_runner.manifest")
    assert dict(library_manifest.FLAT_STAGE_APPROVAL_KEYS) == dict(
        FLAT_STAGE_APPROVAL_KEYS
    )
