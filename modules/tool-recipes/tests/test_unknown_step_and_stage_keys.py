"""A step or stage key this loader does not read is refused by name.

Two measured defects, one shape:

* **recipes-dna** -- ``README.md:15`` and ``README.md:219-220`` documented a
  step-level approval gate::

      - id: "plan-changes"
        requires_approval: true          # Pauses here for human review
        approval_message: "Review the upgrade plan before applying"

  Neither is a :class:`Step` field. Measured before this change::

      Recipe._parse_step({'id':'x','agent':'a','prompt':'p','requires_approval':True})
      -> TypeError: Step.__init__() got an unexpected keyword argument 'requires_approval'

  Loud, but naming no remedy and no valid keys -- and describing a shape that
  cannot load at all. Approval gates are a STAGED-mode feature
  (``stages[].approval``).

* **recipes-juc** -- :meth:`Recipe._parse_stage` built its ``Stage`` by hand,
  so a stage's ``condition:`` was dropped without a word::

      Recipe._parse_stage({'name': 'x', 'condition': '{{a}} == 1', 'steps': [...]})
      -> Stage(name='x', steps=[...], approval=None)   # condition gone

  Three stages of
  ``examples/context-intelligence/verification/adversarial-verification.yaml``
  declared exactly that and documented themselves as "SKIPPED when
  continue_from is provided". They ran every time.

``condition:`` is REFUSED rather than honoured: honouring it means a second,
stage-shaped skip path through approval gates, stage state, resume and
``steps.jsonl`` in both engines, where the remedy -- ``condition:`` on the
stage's steps -- already exists, is evaluated by both engines, and is recorded
as skipped. The schema-2 library refuses the identical shapes at manifest
parse (``src/amplifier_recipe_runner/tests/test_unknown_step_and_stage_keys.py``)
and ``test_both_engines_agree_on_the_key_lists`` pins the two together.
"""

from pathlib import Path

import pytest
import yaml
from amplifier_module_tool_recipes.models import FLAT_STEP_APPROVAL_KEYS
from amplifier_module_tool_recipes.models import KNOWN_STAGE_KEYS
from amplifier_module_tool_recipes.models import KNOWN_STEP_KEYS
from amplifier_module_tool_recipes.models import REJECTED_STAGE_KEYS
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADVERSARIAL = (
    _REPO_ROOT
    / "examples/context-intelligence/verification/adversarial-verification.yaml"
)


def _step(**extra):
    return {"id": "plan-changes", "agent": "zen-architect", "prompt": "Plan it", **extra}


def _stage(**extra):
    return {
        "name": "stage_two",
        "steps": [{"id": "look", "type": "bash", "command": "echo looked"}],
        **extra,
    }


# --- steps: reject path ---------------------------------------------------


@pytest.mark.parametrize("key", sorted(FLAT_STEP_APPROVAL_KEYS))
def test_each_step_approval_key_is_refused_by_name(key):
    """The key, the step, the valid keys, and where the gate actually lives."""
    with pytest.raises(ValueError) as excinfo:
        Recipe._parse_step(_step(**{key: True}))

    message = str(excinfo.value)
    assert f"'{key}'" in message, message
    assert "plan-changes" in message, message
    assert "Valid step keys are" in message, message
    assert FLAT_STEP_APPROVAL_KEYS[key] in message, message


def test_the_readme_shape_is_named_and_pointed_at_the_staged_gate():
    """Exactly what README.md:219-220 told authors to write."""
    with pytest.raises(ValueError) as excinfo:
        Recipe._parse_step(
            _step(requires_approval=True, approval_message="Review the plan")
        )

    message = str(excinfo.value)
    assert "'requires_approval'" in message and "'approval_message'" in message, message
    assert "staged-mode" in message, message
    assert "before_stage" in message, message


def test_no_step_level_gate_is_invented():
    """The remedy points at stages. Inventing a step gate would be worse.

    A remedy that named a step-level approval field would leave the author with
    a second key nothing reads -- the very failure this refusal exists to end.
    """
    assert "requires_approval" not in {f.name for f in Step.__dataclass_fields__.values()}
    assert "staged-mode" in FLAT_STEP_APPROVAL_KEYS["requires_approval"]


def test_an_ordinary_unknown_step_key_gets_the_generic_remedy():
    with pytest.raises(ValueError) as excinfo:
        Recipe._parse_step(_step(retires=3))

    message = str(excinfo.value)
    assert "'retires'" in message, message
    assert "remove the key" in message, message


def test_a_step_with_no_id_is_still_refused():
    step = _step(requires_approval=True)
    del step["id"]
    with pytest.raises(ValueError, match="requires_approval"):
        Recipe._parse_step(step)


def test_a_whole_recipe_carrying_the_readme_shape_fails_to_load(tmp_path):
    """``Recipe.from_yaml`` is the loader every legacy path goes through."""
    recipe = tmp_path / "gate.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "name": "step-gate",
                "description": "A gate that cannot load",
                "version": "1.0.0",
                "steps": [_step(requires_approval=True)],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        Recipe.from_yaml(recipe)

    message = str(excinfo.value)
    assert "requires_approval" in message, message
    assert "TypeError" not in message, message


def test_a_nested_loop_body_step_is_refused_too(tmp_path):
    """Compound bodies are parsed by the executor through the same method."""
    with pytest.raises(ValueError, match="requires_approval"):
        Recipe._parse_step(
            {"id": "inner", "agent": "a", "prompt": "p", "requires_approval": True}
        )


# --- steps: accept path ---------------------------------------------------


def test_every_step_field_is_still_accepted():
    """Derived from the dataclass, so a new Step field is valid on arrival.

    Both spellings of the three renamed keys load today and still do:
    ``steps:``/``while_steps:``, ``as:``/``as_var:``,
    ``context:``/``step_context:``.
    """
    for name in ("as", "context", "steps", "as_var", "step_context", "while_steps"):
        assert name in KNOWN_STEP_KEYS, name
    for field_name in Step.__dataclass_fields__:
        assert field_name in KNOWN_STEP_KEYS, field_name


def test_a_fully_loaded_step_still_parses():
    step = Recipe._parse_step(
        {
            "id": "everything",
            "agent": "zen-architect",
            "prompt": "go",
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
            "spawn_mode": "subprocess",
        }
    )
    assert step.as_var == "item"
    assert step.parallel == 2


def test_a_context_variable_named_like_a_step_key_is_untouched(tmp_path):
    recipe = tmp_path / "ctx.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "name": "ctx",
                "description": "A context variable, not a step key",
                "version": "1.0.0",
                "context": {"requires_approval": True},
                "steps": [{"id": "look", "type": "bash", "command": "echo hi"}],
            }
        ),
        encoding="utf-8",
    )

    loaded = Recipe.from_yaml(recipe)
    assert loaded.context["requires_approval"] is True


# --- stages ---------------------------------------------------------------


def test_stage_condition_is_refused_by_name_with_the_step_level_remedy():
    with pytest.raises(ValueError) as excinfo:
        Recipe._parse_stage(_stage(condition="{{continue_from}} == ''"))

    message = str(excinfo.value)
    assert "'condition'" in message, message
    assert "stage_two" in message, message
    assert "Valid stage keys are" in message, message
    assert REJECTED_STAGE_KEYS["condition"] in message, message


def test_a_stage_condition_never_parses_to_a_silently_dropped_stage():
    """The measured defect: a clean parse with the condition gone."""
    with pytest.raises(ValueError):
        Recipe._parse_stage(_stage(condition="false"))


def test_stage_description_is_a_real_key_and_is_recorded():
    stage = Recipe._parse_stage(_stage(description="What this stage is for."))
    assert stage.description == "What this stage is for."
    assert stage.validate() == []


def test_an_ordinary_unknown_stage_key_is_refused():
    with pytest.raises(ValueError, match="retry_stage"):
        Recipe._parse_stage(_stage(retry_stage=True))


def test_the_flat_approval_keys_keep_their_own_message():
    """The specific refusal runs first; the generic one never shadows it."""
    with pytest.raises(ValueError) as excinfo:
        Recipe._parse_stage(_stage(approval_required=True))

    assert "approval gate is its 'approval:' block" in str(excinfo.value)


# --- the shipped example the stage defect was found in --------------------


def test_the_adversarial_example_carries_its_conditions_on_steps():
    """Continuation mode is now real, and every stage still loads."""
    recipe = Recipe.from_yaml(_ADVERSARIAL)
    conditioned = {"pre_check", "initial_investigation", "synthesis"}

    for stage in recipe.stages:
        if stage.name not in conditioned:
            continue
        assert stage.steps, stage.name
        for step in stage.steps:
            assert step.condition == "{{continue_from}} == ''", (stage.name, step.id)


def test_no_shipped_recipe_carries_an_unknown_step_or_stage_key():
    """A grep the next author cannot forget to run."""
    offenders = []
    for path in sorted(_REPO_ROOT.rglob("*.y*ml")):
        if ".git" in path.parts:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or not (data.get("steps") or data.get("stages")):
            continue

        def visit(steps, where):
            if not isinstance(steps, list):
                return
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                # `instruction:` / `message:` are library-only prompt aliases:
                # valid there, refused here. Only keys unknown to BOTH engines
                # count as shipped offenders.
                unknown = sorted(
                    str(k)
                    for k in step
                    if str(k) not in KNOWN_STEP_KEYS
                    and str(k) not in ("instruction", "message")
                )
                if unknown:
                    rel = path.relative_to(_REPO_ROOT)
                    offenders.append(f"{rel}:{where}[{index}] step key(s) {unknown}")
                visit(step.get("steps"), f"{where}[{index}].steps")

        visit(data.get("steps"), "steps")
        for index, stage in enumerate(data.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            unknown = sorted(str(k) for k in stage if str(k) not in KNOWN_STAGE_KEYS)
            if unknown:
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:stages[{index}] stage key(s) {unknown}")
            visit(stage.get("steps"), f"stages[{index}].steps")

    assert offenders == [], "unknown keys still shipped: " + "; ".join(offenders)


# --- the two engines must not disagree about the same file ----------------


def test_both_engines_agree_on_the_key_lists():
    """The sets are duplicated, not imported -- so pin them to the library's.

    The runner library is an optional dependency of this module. When it IS
    importable, the sets must match, difference included: ``instruction`` and
    ``message`` are prompt aliases only the library reads.
    """
    library_manifest = pytest.importorskip("amplifier_recipe_runner.manifest")

    assert library_manifest.KNOWN_STEP_KEYS == KNOWN_STEP_KEYS | {
        "instruction",
        "message",
    }
    assert library_manifest.KNOWN_STAGE_KEYS == KNOWN_STAGE_KEYS
    assert dict(library_manifest.FLAT_STEP_APPROVAL_KEYS) == FLAT_STEP_APPROVAL_KEYS
    assert dict(library_manifest.REJECTED_STAGE_KEYS) == REJECTED_STAGE_KEYS
