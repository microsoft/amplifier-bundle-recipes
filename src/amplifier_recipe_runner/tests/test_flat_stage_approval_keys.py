"""Flat stage-level approval keys are refused by name, never dropped (recipes-c9d).

``approval_required`` / ``approval_message`` / ``auto_approve_if`` are not
stage fields -- ``docs/RECIPE_SCHEMA.md`` documents exactly one gate, the
``approval:`` block. Before this, a stage declaring them parsed cleanly and ran
straight through: a human checkpoint that had never gated anything.

Both of the library's parsers reach a stage mapping and both must refuse:

* :func:`amplifier_recipe_runner.manifest.parse_manifest` -- the strict
  schema-2 parse that ``plan``/``run``/the tool's ``validate`` all go through.
* :func:`amplifier_recipe_runner.engine.parse_program` -- the program parser,
  which is the ONLY parser a LEGACY sub-recipe reached from a v2 parent
  through a ``type: recipe`` step passes through.
"""

from __future__ import annotations

import textwrap

import pytest

from amplifier_recipe_runner.engine import parse_program
from amplifier_recipe_runner.errors import RecipeRunnerError
from amplifier_recipe_runner.manifest import CONTRACT
from amplifier_recipe_runner.manifest import FLAT_STAGE_APPROVAL_KEYS
from amplifier_recipe_runner.manifest import Manifest
from amplifier_recipe_runner.manifest import ManifestError
from amplifier_recipe_runner.manifest import parse_manifest
from amplifier_recipe_runner.manifest import parse_manifest_text

FLAT_KEY_VALUES = {
    "approval_required": True,
    "approval_message": "Review before continuing.",
    "auto_approve_if": ["{{score}} > 0"],
}


def _staged(stage_extra: dict) -> dict:
    return {
        "schema_version": 2,
        "name": "gated",
        "description": "A staged recipe",
        "version": "1.0",
        "dependencies": [],
        "stages": [
            {
                "name": "final_review",
                "steps": [{"id": "look", "type": "bash", "command": "echo looked"}],
                **stage_extra,
            }
        ],
    }


# --- reject path: the manifest parser ------------------------------------


@pytest.mark.parametrize("key", sorted(FLAT_KEY_VALUES))
def test_each_flat_stage_approval_key_is_rejected_by_name(key: str) -> None:
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_staged({key: FLAT_KEY_VALUES[key]}))

    message = str(excinfo.value)
    assert repr(key) in message, message
    assert "'final_review'" in message, message
    assert "approval:" in message, message
    assert f"{CONTRACT} Core 1" in message, message


def test_all_three_keys_together_are_all_named() -> None:
    """The shape the defect was found in names every offending key at once."""
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_staged(dict(FLAT_KEY_VALUES)))

    message = str(excinfo.value)
    for key in FLAT_KEY_VALUES:
        assert repr(key) in message, message


def test_auto_approve_if_remedy_does_not_invent_a_field() -> None:
    """``auto_approve_if`` has no equivalent -- the remedy must say so.

    An alias would be worse than a rejection here: no engine in this repo
    evaluates a conditional auto-approval, so "translating" it would still
    drop the behaviour while now claiming to have honoured it.
    """
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_staged({"auto_approve_if": ["{{x}} > 0"]}))

    message = str(excinfo.value)
    assert "no conditional auto-approval" in message, message


def test_rejected_even_when_the_stage_is_unnamed() -> None:
    """A stage with no usable name is located positionally, not skipped."""
    body = _staged({"approval_required": True})
    del body["stages"][0]["name"]

    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(body)

    assert "stages[0]" in str(excinfo.value)


def test_approval_required_false_is_still_rejected() -> None:
    """``approval_required: false`` declared no gate -- but still declared it.

    Accepting the falsey case would mean the same key is sometimes read and
    sometimes not, which is exactly the confusion that let the ``true`` case
    ship unnoticed.
    """
    with pytest.raises(ManifestError, match="approval_required"):
        parse_manifest(_staged({"approval_required": False}))


# --- accept path: the canonical block still parses ------------------------


def test_canonical_approval_block_still_parses() -> None:
    staged = textwrap.dedent(
        """
        schema_version: 2
        name: gated
        description: A staged recipe with a real gate
        version: "1.0"
        dependencies: []
        stages:
          - name: final_review
            approval:
              required: true
              prompt: Accept the investigation?
            steps:
              - id: look
                type: bash
                command: echo looked
        """
    )
    manifest = parse_manifest_text(staged)
    assert isinstance(manifest, Manifest)
    assert manifest.schema_version == 2


def test_a_flat_key_elsewhere_is_not_confused_for_a_stage_key() -> None:
    """``approval_required`` as a CONTEXT variable is a normal, valid name.

    ``examples/context-intelligence/synthesis/action-executor.yaml`` declares
    exactly that. The check is scoped to stage mappings, so a context variable
    or a step field of the same name is untouched by it.
    """
    body = _staged({})
    body["context"] = {"approval_required": True, "auto_approve_if": ["x"]}
    manifest = parse_manifest(body)
    assert isinstance(manifest, Manifest)


def test_a_legacy_recipe_is_not_parsed_by_this_check() -> None:
    """No ``schema_version`` means the manifest parser returns before this.

    The legacy engine's own loader
    (``amplifier_module_tool_recipes.models.Recipe._parse_stage``) is what
    refuses the same shape there -- see that package's
    ``tests/test_flat_stage_approval_keys.py``.
    """
    body = _staged({"approval_required": True})
    del body["schema_version"]
    del body["dependencies"]

    parsed = parse_manifest(body)
    assert not isinstance(parsed, Manifest)


# --- reject path: the program parser (legacy sub-recipe route) ------------


@pytest.mark.parametrize("key", sorted(FLAT_KEY_VALUES))
def test_parse_program_refuses_the_same_shape(key: str) -> None:
    """A legacy sub-recipe reaches ``parse_program`` and nothing else."""
    body = {
        "name": "sub",
        "stages": [
            {
                "name": "final_review",
                key: FLAT_KEY_VALUES[key],
                "steps": [{"id": "look", "type": "bash", "command": "echo hi"}],
            }
        ],
    }
    with pytest.raises(RecipeRunnerError) as excinfo:
        parse_program(body)

    message = str(excinfo.value)
    assert repr(key) in message, message
    assert "'final_review'" in message, message


def test_parse_program_still_accepts_the_canonical_block() -> None:
    program = parse_program(
        {
            "name": "sub",
            "stages": [
                {
                    "name": "final_review",
                    "approval": {"required": True, "prompt": "Go on?"},
                    "steps": [{"id": "look", "type": "bash", "command": "echo hi"}],
                }
            ],
        }
    )
    assert program.is_staged
    assert program.stages[0].approval is not None
    assert program.stages[0].approval.required is True
    assert program.stages[0].approval.prompt == "Go on?"


def test_both_parsers_read_the_same_key_list() -> None:
    """One source of truth: the program parser imports the manifest's map."""
    assert set(FLAT_STAGE_APPROVAL_KEYS) == set(FLAT_KEY_VALUES)
    assert all(remedy.strip() for remedy in FLAT_STAGE_APPROVAL_KEYS.values())
