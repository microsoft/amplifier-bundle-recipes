"""Tests for Phase 5 (Quality Classification) and Phase 6 (Report Synthesis) steps.

Tests verify:
- Recipe has exactly 8 steps total
- Correct step IDs in the expected order
- quality-classification depends on all 3 validation phases
- quick-approval and synthesize-report are mutually exclusive via condition
- Both Phase 6 steps output to 'recipe_validation'
- quality-classification outputs to 'quality_classification'
- Phase 5/6 step properties match spec
"""

from pathlib import Path

import pytest
import yaml

RECIPE_PATH = Path(__file__).parent.parent / "validate-recipes.yaml"

EXPECTED_STEP_IDS = [
    "environment-check",
    "recipe-discovery",
    "structural-validation",
    "best-practices-check",
    "semantic-validation",
    "quality-classification",
    "quick-approval",
    "synthesize-report",
]


@pytest.fixture(scope="module")
def recipe_data():
    """Load and parse the validate-recipes.yaml recipe."""
    return yaml.safe_load(RECIPE_PATH.read_text())


@pytest.fixture(scope="module")
def steps(recipe_data):
    """Extract the steps list from the recipe."""
    return recipe_data["steps"]


@pytest.fixture(scope="module")
def steps_by_id(steps):
    """Build a lookup dict of step by id."""
    return {step["id"]: step for step in steps}


# ── Step Count ──────────────────────────────────────────────────────────────


def test_recipe_has_8_steps(steps):
    """Recipe must have exactly 8 steps."""
    assert len(steps) == 8, (
        f"Expected 8 steps, got {len(steps)}. "
        f"Step IDs: {[s['id'] for s in steps]}"
    )


def test_step_ids_are_correct(steps):
    """All 8 step IDs must be present in the correct order."""
    actual_ids = [step["id"] for step in steps]
    assert actual_ids == EXPECTED_STEP_IDS, (
        f"Expected step IDs:\n  {EXPECTED_STEP_IDS}\n"
        f"Got:\n  {actual_ids}"
    )


# ── quality-classification (Phase 5) ────────────────────────────────────────


def test_quality_classification_exists(steps_by_id):
    """quality-classification step must exist."""
    assert "quality-classification" in steps_by_id


def test_quality_classification_is_bash(steps_by_id):
    """quality-classification must be a bash step."""
    step = steps_by_id["quality-classification"]
    assert step.get("type") == "bash"


def test_quality_classification_timeout(steps_by_id):
    """quality-classification must have timeout of 60."""
    step = steps_by_id["quality-classification"]
    assert step.get("timeout") == 60


def test_quality_classification_on_error_continue(steps_by_id):
    """quality-classification must have on_error: continue."""
    step = steps_by_id["quality-classification"]
    assert step.get("on_error") == "continue"


def test_quality_classification_depends_on_all_three_phases(steps_by_id):
    """quality-classification must depend on all 3 validation phases."""
    step = steps_by_id["quality-classification"]
    depends_on = set(step.get("depends_on", []))
    required_deps = {"structural-validation", "best-practices-check", "semantic-validation"}
    assert required_deps == depends_on, (
        f"Expected depends_on: {sorted(required_deps)}\n"
        f"Got: {sorted(depends_on)}"
    )


def test_quality_classification_output_key(steps_by_id):
    """quality-classification must output to 'quality_classification'."""
    step = steps_by_id["quality-classification"]
    assert step.get("output") == "quality_classification"


def test_quality_classification_parse_json(steps_by_id):
    """quality-classification must have parse_json: true."""
    step = steps_by_id["quality-classification"]
    assert step.get("parse_json") is True


def test_quality_classification_has_command(steps_by_id):
    """quality-classification must have a non-empty command."""
    step = steps_by_id["quality-classification"]
    command = step.get("command", "")
    assert command.strip(), "quality-classification command must not be empty"


def test_quality_classification_command_has_classify_recipe_function(steps_by_id):
    """quality-classification command must implement classify_recipe function."""
    step = steps_by_id["quality-classification"]
    command = step.get("command", "")
    assert "classify_recipe" in command, (
        "quality-classification command must implement classify_recipe function"
    )


def test_quality_classification_command_consumes_all_phase_results(steps_by_id):
    """quality-classification command must reference all three phase outputs."""
    step = steps_by_id["quality-classification"]
    command = step.get("command", "")
    for var in ("structural_results", "practice_results", "semantic_results"):
        assert var in command, (
            f"quality-classification command must consume '{var}'"
        )


def test_quality_classification_command_has_quality_levels(steps_by_id):
    """quality-classification command must classify to 4 quality levels."""
    step = steps_by_id["quality-classification"]
    command = step.get("command", "")
    for level in ("critical", "needs_work", "polish", "good"):
        assert level in command, (
            f"quality-classification command must reference quality level '{level}'"
        )


def test_quality_classification_command_has_requires_llm_analysis(steps_by_id):
    """quality-classification command must set requires_llm_analysis flag."""
    step = steps_by_id["quality-classification"]
    command = step.get("command", "")
    assert "requires_llm_analysis" in command


def test_quality_classification_command_has_summary_keys(steps_by_id):
    """quality-classification command must build summary with expected keys."""
    step = steps_by_id["quality-classification"]
    command = step.get("command", "")
    for key in ("total", "good", "polish", "needs_work", "critical"):
        assert key in command, (
            f"quality-classification summary must include key '{key}'"
        )


# ── quick-approval (Phase 6a) ────────────────────────────────────────────────


def test_quick_approval_exists(steps_by_id):
    """quick-approval step must exist."""
    assert "quick-approval" in steps_by_id


def test_quick_approval_is_bash(steps_by_id):
    """quick-approval must be a bash step."""
    step = steps_by_id["quick-approval"]
    assert step.get("type") == "bash"


def test_quick_approval_condition(steps_by_id):
    """quick-approval must have the correct condition for fast path."""
    step = steps_by_id["quick-approval"]
    condition = step.get("condition", "")
    assert "quality_classification.requires_llm_analysis" in condition, (
        f"quick-approval condition must reference quality_classification.requires_llm_analysis, got: {condition!r}"
    )
    assert "false" in condition.lower(), (
        f"quick-approval condition must check for false, got: {condition!r}"
    )


def test_quick_approval_timeout(steps_by_id):
    """quick-approval must have timeout of 10."""
    step = steps_by_id["quick-approval"]
    assert step.get("timeout") == 10


def test_quick_approval_depends_on_quality_classification(steps_by_id):
    """quick-approval must depend on quality-classification."""
    step = steps_by_id["quick-approval"]
    depends_on = step.get("depends_on", [])
    assert "quality-classification" in depends_on


def test_quick_approval_output_key(steps_by_id):
    """quick-approval must output to 'recipe_validation'."""
    step = steps_by_id["quick-approval"]
    assert step.get("output") == "recipe_validation"


def test_quick_approval_parse_json(steps_by_id):
    """quick-approval must have parse_json: true."""
    step = steps_by_id["quick-approval"]
    assert step.get("parse_json") is True


def test_quick_approval_command_generates_pass_report(steps_by_id):
    """quick-approval command must generate a PASS report with expected keys."""
    step = steps_by_id["quick-approval"]
    command = step.get("command", "")
    for key in ("quality_level", "summary", "recipes_validated", "recommendations"):
        assert key in command, (
            f"quick-approval command must reference key '{key}'"
        )


def test_quick_approval_command_has_good_quality_level(steps_by_id):
    """quick-approval command must set quality_level to 'good'."""
    step = steps_by_id["quick-approval"]
    command = step.get("command", "")
    assert "good" in command, "quick-approval must set quality_level='good'"


# ── synthesize-report (Phase 6b) ─────────────────────────────────────────────


def test_synthesize_report_exists(steps_by_id):
    """synthesize-report step must exist."""
    assert "synthesize-report" in steps_by_id


def test_synthesize_report_is_agent_step(steps_by_id):
    """synthesize-report must be an agent step (type: agent or agent field set)."""
    step = steps_by_id["synthesize-report"]
    # Agent steps can be implicit (no type) or explicit type: agent
    # The agent field must be set
    assert step.get("agent") == "foundation:zen-architect", (
        f"synthesize-report must use agent 'foundation:zen-architect', got: {step.get('agent')!r}"
    )


def test_synthesize_report_mode(steps_by_id):
    """synthesize-report must use mode: ANALYZE."""
    step = steps_by_id["synthesize-report"]
    assert step.get("mode") == "ANALYZE"


def test_synthesize_report_condition(steps_by_id):
    """synthesize-report must have the correct condition for LLM path."""
    step = steps_by_id["synthesize-report"]
    condition = step.get("condition", "")
    assert "quality_classification.requires_llm_analysis" in condition, (
        f"synthesize-report condition must reference quality_classification.requires_llm_analysis, got: {condition!r}"
    )
    assert "true" in condition.lower(), (
        f"synthesize-report condition must check for true, got: {condition!r}"
    )


def test_synthesize_report_timeout(steps_by_id):
    """synthesize-report must have timeout of 300."""
    step = steps_by_id["synthesize-report"]
    assert step.get("timeout") == 300


def test_synthesize_report_on_error_continue(steps_by_id):
    """synthesize-report must have on_error: continue."""
    step = steps_by_id["synthesize-report"]
    assert step.get("on_error") == "continue"


def test_synthesize_report_depends_on_quality_classification(steps_by_id):
    """synthesize-report must depend on quality-classification."""
    step = steps_by_id["synthesize-report"]
    depends_on = step.get("depends_on", [])
    assert "quality-classification" in depends_on


def test_synthesize_report_output_key(steps_by_id):
    """synthesize-report must output to 'recipe_validation'."""
    step = steps_by_id["synthesize-report"]
    assert step.get("output") == "recipe_validation"


def test_synthesize_report_parse_json(steps_by_id):
    """synthesize-report must have parse_json: true."""
    step = steps_by_id["synthesize-report"]
    assert step.get("parse_json") is True


def test_synthesize_report_prompt_references_all_phases(steps_by_id):
    """synthesize-report prompt must reference all three phase result contexts."""
    step = steps_by_id["synthesize-report"]
    prompt = step.get("prompt", "")
    for var in ("structural_results", "practice_results", "semantic_results"):
        assert var in prompt, (
            f"synthesize-report prompt must reference '{var}'"
        )


def test_synthesize_report_prompt_has_output_json_keys(steps_by_id):
    """synthesize-report prompt must specify required output JSON keys."""
    step = steps_by_id["synthesize-report"]
    prompt = step.get("prompt", "")
    for key in ("quality_level", "summary", "recipes_validated", "findings_by_severity",
                "per_recipe_verdicts", "recommendations", "report_text"):
        assert key in prompt, (
            f"synthesize-report prompt must reference output key '{key}'"
        )


def test_synthesize_report_prompt_has_verdict_logic(steps_by_id):
    """synthesize-report prompt must instruct on verdict logic per quality level."""
    step = steps_by_id["synthesize-report"]
    prompt = step.get("prompt", "")
    for verdict in ("PASS WITH SUGGESTIONS", "PASS WITH WARNINGS", "FAIL"):
        assert verdict in prompt, (
            f"synthesize-report prompt must reference verdict '{verdict}'"
        )


# ── Mutual Exclusion ─────────────────────────────────────────────────────────


def test_phase6_conditions_are_mutually_exclusive(steps_by_id):
    """quick-approval and synthesize-report conditions must be mutually exclusive."""
    qa_condition = steps_by_id["quick-approval"].get("condition", "")
    sr_condition = steps_by_id["synthesize-report"].get("condition", "")

    # One checks for false, the other for true
    assert "false" in qa_condition.lower(), (
        "quick-approval condition must check for 'false'"
    )
    assert "true" in sr_condition.lower(), (
        "synthesize-report condition must check for 'true'"
    )

    # Both must reference the same variable
    assert "quality_classification.requires_llm_analysis" in qa_condition
    assert "quality_classification.requires_llm_analysis" in sr_condition


def test_both_phase6_output_to_recipe_validation(steps_by_id):
    """Both Phase 6 steps must output to 'recipe_validation'."""
    assert steps_by_id["quick-approval"].get("output") == "recipe_validation"
    assert steps_by_id["synthesize-report"].get("output") == "recipe_validation"
