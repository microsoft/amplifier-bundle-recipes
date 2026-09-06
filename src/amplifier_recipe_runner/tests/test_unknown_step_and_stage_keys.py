"""A step or stage key no engine reads is refused by name (recipes-dna, recipes-juc).

Two measured defects, one shape:

* **recipes-dna** -- ``README.md`` documented a step-level approval gate
  (``requires_approval: true`` / ``approval_message:``). Neither is a step
  field in either engine: the legacy loader died on a raw
  ``TypeError: Step.__init__() got an unexpected keyword argument``, and this
  library dropped both without a word.
* **recipes-juc** -- three stages of
  ``examples/context-intelligence/verification/adversarial-verification.yaml``
  declared ``condition: "{{continue_from}} == ''"`` and documented themselves
  as "SKIPPED when continue_from is provided". No parser here read a stage
  ``condition:``, so they ran every time.

``condition:`` on a stage is REFUSED rather than honoured. Honouring it means
a second, stage-shaped skip path threaded through approval gates, stage state,
resume and ``steps.jsonl`` in both engines; the remedy -- ``condition:`` on the
stage's steps -- costs an author one line per step and rides the step-level
condition both engines already evaluate and record as skipped.

Both of the library's parsers reach step and stage mappings and both must
refuse: :func:`~amplifier_recipe_runner.manifest.parse_manifest` (every
schema-2 ``validate``/``plan``/``run``) and
:func:`~amplifier_recipe_runner.engine.parse_program` (the ONLY parser a
LEGACY sub-recipe reached from a v2 parent passes through).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from amplifier_recipe_runner.engine import load_program
from amplifier_recipe_runner.engine import parse_program
from amplifier_recipe_runner.engine import parse_step
from amplifier_recipe_runner.errors import RecipeRunnerError
from amplifier_recipe_runner.manifest import FLAT_STEP_APPROVAL_KEYS
from amplifier_recipe_runner.manifest import KNOWN_STAGE_KEYS
from amplifier_recipe_runner.manifest import KNOWN_STEP_KEYS
from amplifier_recipe_runner.manifest import REJECTED_STAGE_KEYS
from amplifier_recipe_runner.manifest import Manifest
from amplifier_recipe_runner.manifest import ManifestError
from amplifier_recipe_runner.manifest import parse_manifest
from amplifier_recipe_runner.manifest import parse_manifest_file

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADVERSARIAL = _REPO_ROOT / "examples/context-intelligence/verification/adversarial-verification.yaml"


def _flat(step_extra: dict) -> dict:
    return {
        "schema_version": 2,
        "name": "gated",
        "description": "A flat recipe",
        "version": "1.0",
        "dependencies": [],
        "steps": [{"id": "plan-changes", "agent": "supplier:architect", "prompt": "Plan it", **step_extra}],
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
                "name": "stage_two",
                "steps": [{"id": "look", "type": "bash", "command": "echo looked"}],
                **stage_extra,
            }
        ],
    }


# --- steps: the manifest parser -------------------------------------------


@pytest.mark.parametrize("key", sorted(FLAT_STEP_APPROVAL_KEYS))
def test_each_step_approval_key_is_rejected_by_name(key: str) -> None:
    """The README's shape names the key, the step, the valid keys, the remedy."""
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_flat({key: True}))

    message = str(excinfo.value)
    assert repr(key) in message, message
    assert "'plan-changes'" in message, message
    assert "Valid step keys are" in message, message
    assert FLAT_STEP_APPROVAL_KEYS[key] in message, message


def test_the_readme_shape_points_at_the_staged_gate() -> None:
    """`requires_approval` had no remedy at all before -- now it names one.

    The remedy must not invent a step-level gate: approval is a STAGED-mode
    feature, and saying otherwise would be a worse failure than the raw
    TypeError this replaces.
    """
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_flat({"requires_approval": True, "approval_message": "Review the plan"}))

    message = str(excinfo.value)
    assert "'approval_message'" in message and "'requires_approval'" in message, message
    assert "staged-mode" in message, message
    assert "before_stage" in message, message


def test_an_ordinary_unknown_step_key_gets_the_generic_remedy() -> None:
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_flat({"retires": 3}))

    message = str(excinfo.value)
    assert "'retires'" in message, message
    assert "remove the key" in message, message


def test_an_unknown_key_in_a_nested_loop_body_is_caught_too() -> None:
    body = _flat({})
    body["steps"] = [
        {
            "id": "outer",
            "foreach": "{{items}}",
            "steps": [{"id": "inner", "agent": "supplier:a", "prompt": "p", "requires_approval": True}],
        }
    ]
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(body)

    assert "'inner'" in str(excinfo.value)


def test_a_step_with_no_id_is_still_refused_and_located() -> None:
    body = _flat({})
    body["steps"] = [{"agent": "supplier:a", "prompt": "p", "requires_approval": True}]
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(body)

    assert "steps[0]" in str(excinfo.value)


def test_every_documented_step_key_still_parses() -> None:
    """The refusal narrows nothing that already loaded.

    Both spellings of the three renamed keys are accepted, because both load:
    ``steps:``/``while_steps:``, ``as:``/``as_var:``, ``context:``/``step_context:``.
    """
    body = _flat({})
    body["steps"] = [
        {
            "id": "everything",
            "agent": "supplier:a",
            "instruction": "go",
            "condition": "{{ok}} == 'yes'",
            "foreach": "{{items}}",
            "as": "item",
            "collect": "results",
            "parallel": 2,
            "timeout": 30,
            "on_error": "continue",
            "parse_json": True,
            "output": "out",
            "depends_on": [],
            "steps": [{"id": "body", "agent": "supplier:a", "prompt": "p"}],
        },
        {"id": "loop", "while_condition": "{{n}} < 3", "while_steps": [{"id": "b2", "agent": "supplier:a", "prompt": "p"}]},
    ]
    assert isinstance(parse_manifest(body), Manifest)


def test_a_context_variable_named_like_a_step_key_is_untouched() -> None:
    body = _flat({})
    body["context"] = {"requires_approval": True}
    assert isinstance(parse_manifest(body), Manifest)


# --- steps: the program parser (legacy sub-recipe route) -------------------


def test_parse_step_refuses_the_same_shape() -> None:
    with pytest.raises(RecipeRunnerError) as excinfo:
        parse_step({"id": "plan-changes", "agent": "a", "prompt": "p", "requires_approval": True})

    message = str(excinfo.value)
    assert "'requires_approval'" in message, message
    assert "'plan-changes'" in message, message
    assert "staged-mode" in message, message


def test_parse_program_refuses_a_legacy_recipe_carrying_the_readme_shape() -> None:
    """A legacy sub-recipe reaches ``parse_program`` and nothing else."""
    with pytest.raises(RecipeRunnerError) as excinfo:
        parse_program(
            {
                "name": "sub",
                "steps": [{"id": "plan-changes", "agent": "a", "prompt": "p", "approval_message": "ok?"}],
            }
        )

    assert "'approval_message'" in str(excinfo.value)


# --- stages ---------------------------------------------------------------


def test_stage_condition_is_rejected_by_name_with_the_step_level_remedy() -> None:
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_staged({"condition": "{{continue_from}} == ''"}))

    message = str(excinfo.value)
    assert "'condition'" in message, message
    assert "'stage_two'" in message, message
    assert "Valid stage keys are" in message, message
    assert REJECTED_STAGE_KEYS["condition"] in message, message


def test_stage_condition_remedy_does_not_invent_a_stage_field() -> None:
    """The remedy points at the steps, which really are evaluated."""
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_staged({"condition": "false"}))

    assert "put 'condition:' on each of the stage's steps" in str(excinfo.value)


def test_parse_program_refuses_a_stage_condition_too() -> None:
    with pytest.raises(RecipeRunnerError) as excinfo:
        parse_program(
            {
                "name": "sub",
                "stages": [
                    {
                        "name": "stage_two",
                        "condition": "false",
                        "steps": [{"id": "look", "type": "bash", "command": "echo hi"}],
                    }
                ],
            }
        )

    assert "'condition'" in str(excinfo.value)


def test_stage_description_is_a_real_key_and_is_recorded() -> None:
    """Documentation, parsed rather than dropped -- and never executed."""
    program = parse_program(
        {
            "name": "sub",
            "stages": [
                {
                    "name": "stage_two",
                    "description": "What this stage is for.",
                    "steps": [{"id": "look", "type": "bash", "command": "echo hi"}],
                }
            ],
        }
    )
    assert program.stages[0].description == "What this stage is for."
    assert isinstance(parse_manifest(_staged({"description": "doc"})), Manifest)


def test_an_ordinary_unknown_stage_key_is_refused() -> None:
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_staged({"retry_stage": True}))

    assert "'retry_stage'" in str(excinfo.value)


def test_the_flat_approval_keys_keep_their_own_message() -> None:
    """The specific refusal runs first; the generic one never shadows it."""
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(_staged({"approval_required": True}))

    assert "approval gate is its 'approval:' block" in str(excinfo.value)


# --- the two engines agree ------------------------------------------------


def test_both_engines_agree_on_the_key_lists() -> None:
    """The legacy module's sets and this one's differ by exactly two aliases.

    ``instruction`` / ``message`` are read only here
    (:data:`~amplifier_recipe_runner.engine.INSTRUCTION_KEYS`). Everything else
    must match, or a recipe could be accepted by one engine and refused by the
    other -- which is the divergence this whole refusal exists to close.
    """
    models = pytest.importorskip("amplifier_module_tool_recipes.models")

    assert KNOWN_STEP_KEYS == models.KNOWN_STEP_KEYS | {"instruction", "message"}
    assert KNOWN_STAGE_KEYS == models.KNOWN_STAGE_KEYS
    assert dict(FLAT_STEP_APPROVAL_KEYS) == models.FLAT_STEP_APPROVAL_KEYS
    assert dict(REJECTED_STAGE_KEYS) == models.REJECTED_STAGE_KEYS


# --- the shipped example the stage defect was found in ---------------------


def test_the_adversarial_example_carries_its_conditions_on_steps() -> None:
    """Continuation mode is now real: the steps carry the condition.

    Before recipes-juc the three stages below declared it and it was dropped,
    so `--context continue_from=<doc>` re-ran the whole investigation.
    """
    assert isinstance(parse_manifest_file(_ADVERSARIAL), Manifest)

    program = load_program(_ADVERSARIAL)
    conditioned = {"pre_check", "initial_investigation", "synthesis"}
    for stage in program.stages:
        if stage.name not in conditioned:
            continue
        assert stage.steps, stage.name
        for step in stage.steps:
            assert step.condition == "{{continue_from}} == ''", (stage.name, step.id)


def test_no_shipped_recipe_carries_an_unknown_step_or_stage_key() -> None:
    """A grep the next author cannot forget to run."""
    offenders: list[str] = []
    for path in sorted(_REPO_ROOT.rglob("*.y*ml")):
        if ".git" in path.parts:
            continue
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - not every YAML here is a recipe
            continue
        if not isinstance(body, dict) or not (body.get("steps") or body.get("stages")):
            continue

        def visit(steps: object, where: str) -> None:
            if not isinstance(steps, list):
                return
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                unknown = sorted(str(k) for k in step if k not in KNOWN_STEP_KEYS)
                if unknown:
                    offenders.append(f"{path}: {where}[{index}] step key(s) {unknown}")
                visit(step.get("steps"), f"{where}[{index}].steps")

        visit(body.get("steps"), "steps")
        for index, stage in enumerate(body.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            unknown = sorted(str(k) for k in stage if k not in KNOWN_STAGE_KEYS)
            if unknown:
                offenders.append(f"{path}: stages[{index}] stage key(s) {unknown}")
            visit(stage.get("steps"), f"stages[{index}].steps")

    assert offenders == [], "\n".join(offenders)
