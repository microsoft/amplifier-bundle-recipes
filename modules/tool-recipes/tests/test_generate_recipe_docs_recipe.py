"""Tests for content of recipes/generate-recipe-docs.yaml.

Verifies two enhancements:
1. The enhance-labels step prompt includes a rule instructing the LLM to
   add the recipe filename (without extension) to sub-recipe call nodes.
2. The discover-and-generate step's steps_info builder includes the
   ``recipe`` field so the LLM can see which recipe file is being called.
"""

from pathlib import Path

import yaml

# Path to the recipe under test
_RECIPE_PATH = (
    Path(__file__).parent.parent.parent.parent / "recipes" / "generate-recipe-docs.yaml"
)


def _load_recipe() -> dict:
    with _RECIPE_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _find_step(recipe: dict, step_id: str) -> dict:
    for step in recipe.get("steps", []):
        if step.get("id") == step_id:
            return step
    raise KeyError(f"Step {step_id!r} not found in recipe")


# ---------------------------------------------------------------------------
# 1. enhance-labels prompt includes sub-recipe filename rule
# ---------------------------------------------------------------------------


def test_enhance_labels_prompt_contains_sub_recipe_filename_rule() -> None:
    """The enhance-labels prompt must instruct the LLM to include the
    recipe filename (without extension) in sub-recipe node labels."""
    recipe = _load_recipe()
    step = _find_step(recipe, "enhance-labels")
    prompt: str = step.get("prompt", "")

    # The prompt should tell the LLM to include the recipe filename
    assert "recipe filename" in prompt.lower() or "filename" in prompt.lower(), (
        "enhance-labels prompt should mention including the recipe filename "
        f"on sub-recipe nodes. Prompt: {prompt[:500]!r}"
    )

    # The prompt should explain the parentheses format, e.g. "(dotfiles-prescan)"
    assert "parenthes" in prompt.lower() or "(dotfiles-" in prompt.lower(), (
        "enhance-labels prompt should show the '(recipe-name)' parentheses format. "
        f"Prompt: {prompt[:500]!r}"
    )


def test_enhance_labels_prompt_references_tooltip_for_recipe_name() -> None:
    """The rule should tell the LLM where to find the recipe name — in the
    tooltip attribute or the steps_info recipe field."""
    recipe = _load_recipe()
    step = _find_step(recipe, "enhance-labels")
    prompt: str = step.get("prompt", "")

    # The rule should reference either tooltip or the recipe field in steps_info
    assert "tooltip" in prompt.lower() or '"recipe"' in prompt or "recipe field" in prompt.lower(), (
        "enhance-labels prompt should direct the LLM to the tooltip or recipe "
        "field to find the sub-recipe filename. "
        f"Prompt: {prompt[:500]!r}"
    )


# ---------------------------------------------------------------------------
# 2. discover-and-generate includes recipe field in step_info
# ---------------------------------------------------------------------------


def test_discover_step_includes_recipe_field_in_step_info() -> None:
    """The discover-and-generate bash step must include the ``recipe`` field
    in the step_info dict so the LLM receives the sub-recipe filename."""
    recipe = _load_recipe()
    step = _find_step(recipe, "discover-and-generate")
    command: str = step.get("command", "")

    # The command should include logic to add recipe field to step_info
    assert 'step_info["recipe"]' in command or "step_info['recipe']" in command, (
        "discover-and-generate command should add step['recipe'] to step_info. "
        f"Command excerpt: {command[100:400]!r}"
    )
