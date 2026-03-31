"""Tests for amplifier_module_tool_recipes.recipe_to_dot.

Covers both public functions (recipe_to_dot, recipe_dot_hash) and the private
helper _auto_label, plus parametrized smoke-tests over every bundled example.
"""

import glob
import textwrap
from pathlib import Path

import pytest

from amplifier_module_tool_recipes.recipe_to_dot import (
    _auto_label,
    recipe_dot_hash,
    recipe_to_dot,
)

# ---------------------------------------------------------------------------
# Paths to real example / recipe YAML files (collected at import time so
# pytest can parametrize over them without a fixture).
# ---------------------------------------------------------------------------

_BUNDLE_ROOT = Path("/home/bkrabach/dev/recipe-dot-docs/amplifier-bundle-recipes")
_EXAMPLE_YAMLS: list[str] = sorted(
    glob.glob(str(_BUNDLE_ROOT / "examples" / "*.yaml"))
    + glob.glob(str(_BUNDLE_ROOT / "recipes" / "*.yaml"))
)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, content: str) -> Path:
    """Dedent *content* and write it to ``recipe.yaml`` inside *tmp_path*."""
    p = tmp_path / "recipe.yaml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# 1. Simple flat recipe — 3 sequential agent steps
# ---------------------------------------------------------------------------


def test_simple_flat_recipe(tmp_path: Path) -> None:
    """3 sequential agent steps produce the expected graph structure."""
    p = _write(
        tmp_path,
        """
        name: simple-flat
        steps:
          - id: gather-data
            agent: foundation:zen-architect
            prompt: Gather the data.
          - id: analyse-data
            agent: foundation:zen-architect
            prompt: Analyse the gathered data.
          - id: write-report
            agent: foundation:zen-architect
            prompt: Write the final report.
        """,
    )
    dot = recipe_to_dot(p)

    # Graph wrapper
    assert "digraph" in dot

    # Start / done terminal nodes
    assert "start" in dot
    assert "done" in dot

    # All three step nodes
    assert "step_gather_data" in dot
    assert "step_analyse_data" in dot
    assert "step_write_report" in dot

    # Green fill for agent steps
    assert "#c8e6c9" in dot

    # Sequential edge chain
    assert "start -> step_gather_data" in dot
    assert "step_gather_data -> step_analyse_data" in dot
    assert "step_analyse_data -> step_write_report" in dot

    # Legend with agent entry
    assert "AI Agent Step" in dot
    assert "cluster_legend" in dot


# ---------------------------------------------------------------------------
# 2. Staged recipe with approval gate
# ---------------------------------------------------------------------------


def test_staged_recipe_with_approval(tmp_path: Path) -> None:
    """Staged recipe: cluster subgraphs, approval diamond, inter-stage edges."""
    p = _write(
        tmp_path,
        """
        name: staged-recipe
        stages:
          - name: stage-one
            approval:
              required: true
              prompt: Please review and approve before proceeding.
            steps:
              - id: prepare
                agent: foundation:zen-architect
                prompt: Prepare the work for review.
          - name: stage-two
            steps:
              - id: finalize
                agent: foundation:zen-architect
                prompt: Finalize the work after approval.
        """,
    )
    dot = recipe_to_dot(p)

    # Both stage clusters present
    assert "cluster_stage_one" in dot
    assert "cluster_stage_two" in dot

    # Approval gate node (orange diamond)
    assert "gate_stage_one" in dot
    assert "#ffe0b2" in dot

    # start flows into the gate which precedes stage-one
    assert "start -> gate_stage_one" in dot

    # Legend is present
    assert "cluster_legend" in dot


# ---------------------------------------------------------------------------
# 3. Bash step coloring
# ---------------------------------------------------------------------------


def test_bash_step_coloring(tmp_path: Path) -> None:
    """Bash step gets blue fill (#bbdefb) and 'Script Step' legend entry."""
    p = _write(
        tmp_path,
        """
        name: bash-recipe
        steps:
          - id: run-script
            type: bash
            command: echo hello
        """,
    )
    dot = recipe_to_dot(p)

    # Blue fill for bash steps
    assert "#bbdefb" in dot

    # Legend entry for script steps
    assert "Script Step" in dot


# ---------------------------------------------------------------------------
# 4. Sub-recipe styling
# ---------------------------------------------------------------------------


def test_sub_recipe_styling(tmp_path: Path) -> None:
    """Sub-recipe step gets gray dashed styling and '(sub-recipe)' label."""
    p = _write(
        tmp_path,
        """
        name: sub-recipe-test
        steps:
          - id: call-sub
            type: recipe
            recipe: other-recipe.yaml
        """,
    )
    dot = recipe_to_dot(p)

    # Gray fill (shared with start/end)
    assert "#e0e0e0" in dot

    # Dashed style attribute
    assert "dashed" in dot

    # Annotation in node label
    assert "(sub-recipe)" in dot


# ---------------------------------------------------------------------------
# 5. Conditional step → decision diamond
# ---------------------------------------------------------------------------


def test_conditional_step(tmp_path: Path) -> None:
    """A step with a condition produces a yellow decision diamond."""
    p = _write(
        tmp_path,
        """
        name: conditional-test
        steps:
          - id: classify
            agent: foundation:zen-architect
            prompt: Classify the input as yes or no.
            output: classification
          - id: handle-case
            condition: "{{classification}} == 'yes'"
            agent: foundation:zen-architect
            prompt: Handle the affirmative case.
        """,
    )
    dot = recipe_to_dot(p)

    # Yellow fill for condition diamonds
    assert "#fff9c4" in dot

    # Diamond shape
    assert "diamond" in dot

    # Condition node id is cond_ + step node id
    assert "cond_step_handle_case" in dot


# ---------------------------------------------------------------------------
# 6. Foreach + parallel annotation
# ---------------------------------------------------------------------------


def test_foreach_parallel(tmp_path: Path) -> None:
    """A step with parallel:true (no foreach) gets a '(parallel)' label annotation."""
    p = _write(
        tmp_path,
        """
        name: parallel-test
        steps:
          - id: parallel-step
            parallel: true
            agent: foundation:zen-architect
            prompt: Do this step in parallel across items.
        """,
    )
    dot = recipe_to_dot(p)

    assert "(parallel)" in dot


# ---------------------------------------------------------------------------
# 7. While loop → self-edge
# ---------------------------------------------------------------------------


def test_while_loop(tmp_path: Path) -> None:
    """A step with while_condition emits a loop-back self-edge."""
    p = _write(
        tmp_path,
        """
        name: while-test
        steps:
          - id: loop-step
            while_condition: "{{done}} != 'yes'"
            agent: foundation:zen-architect
            prompt: Keep processing until the done flag is set to yes.
        """,
    )
    dot = recipe_to_dot(p)

    # Self-loop: node_id -> same node_id
    assert "step_loop_step -> step_loop_step" in dot

    # Loop edge is labelled "loop"
    assert 'label="loop"' in dot


# ---------------------------------------------------------------------------
# 8. Deterministic output
# ---------------------------------------------------------------------------


def test_deterministic(tmp_path: Path) -> None:
    """Calling recipe_to_dot / recipe_dot_hash twice returns identical results."""
    p = _write(
        tmp_path,
        """
        name: deterministic-test
        steps:
          - id: step-one
            agent: foundation:zen-architect
            prompt: Do something deterministic.
        """,
    )

    dot1 = recipe_to_dot(p)
    dot2 = recipe_to_dot(p)
    assert dot1 == dot2

    hash1 = recipe_dot_hash(p)
    hash2 = recipe_dot_hash(p)
    assert hash1 == hash2

    # SHA-256 hex digest is always 64 characters
    assert len(hash1) == 64
    assert hash1 == hash1.lower()  # lowercase hex


# ---------------------------------------------------------------------------
# 9. Hash changes when content changes
# ---------------------------------------------------------------------------


def test_hash_changes_on_content(tmp_path: Path) -> None:
    """Different YAML content produces different SHA-256 hashes."""
    p_alpha = tmp_path / "alpha.yaml"
    p_alpha.write_text(
        textwrap.dedent("""
            name: recipe-alpha
            steps:
              - id: step-one
                agent: foundation:zen-architect
                prompt: Do alpha.
        """)
    )

    p_beta = tmp_path / "beta.yaml"
    p_beta.write_text(
        textwrap.dedent("""
            name: recipe-beta
            steps:
              - id: step-one
                agent: foundation:zen-architect
                prompt: Do beta.
        """)
    )

    assert recipe_dot_hash(p_alpha) != recipe_dot_hash(p_beta)


# ---------------------------------------------------------------------------
# 10. Missing file → FileNotFoundError
# ---------------------------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    """recipe_to_dot raises FileNotFoundError for a path that does not exist."""
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError):
        recipe_to_dot(missing)


# ---------------------------------------------------------------------------
# 11. Invalid YAML (no 'name' or 'steps'/'stages') → ValueError
# ---------------------------------------------------------------------------


def test_invalid_yaml(tmp_path: Path) -> None:
    """recipe_to_dot raises ValueError for YAML missing required recipe keys."""
    p = tmp_path / "invalid.yaml"
    p.write_text("some_key: some_value\nanother_key: 42\n")
    with pytest.raises(ValueError):
        recipe_to_dot(p)


# ---------------------------------------------------------------------------
# 12. _auto_label
# ---------------------------------------------------------------------------


def test_auto_label() -> None:
    """_auto_label converts hyphenated IDs to readable multi-line labels."""
    # Two words → joined with a newline
    assert _auto_label("audit-dependencies") == "Audit\nDependencies"

    # Single word → just title-cased, no newline
    assert _auto_label("a") == "A"

    # Four words (> 3) → split roughly in half across exactly 2 lines
    result = _auto_label("very-long-step-name")
    # "very", "long", "step", "name" → mid = (4+1)//2 = 2
    # → "Very Long\nStep Name"
    assert "\n" in result
    lines = result.split("\n")
    assert len(lines) == 2
    # All four words must be present (case-insensitive check via title-case)
    assert "Very" in result
    assert "Long" in result
    assert "Step" in result
    assert "Name" in result


# ---------------------------------------------------------------------------
# 13. All bundled example / recipe YAMLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "yaml_file",
    _EXAMPLE_YAMLS,
    ids=lambda p: Path(p).name,
)
def test_all_example_recipes(yaml_file: str) -> None:
    """Every bundled example/recipe YAML produces a valid DOT diagram."""
    dot = recipe_to_dot(yaml_file)

    assert dot.startswith("digraph"), (
        f"DOT should start with 'digraph' for {yaml_file!r}"
    )
    assert "start" in dot, f"DOT should contain 'start' node for {yaml_file!r}"
    assert "done" in dot, f"DOT should contain 'done' node for {yaml_file!r}"
    assert "cluster_legend" in dot, (
        f"DOT should contain legend cluster for {yaml_file!r}"
    )
