"""
Tests for Task 5: Phase 3 Best Practices Check step in validate-recipes.yaml.
Tests:
  1. The best-practices-check step exists in validate-recipes.yaml
  2. Step has correct attributes: id, type, timeout, on_error, depends_on, output, parse_json
  3. Step command contains python3 heredoc with check_best_practices function
  4. Step depends_on recipe-discovery (runs parallel with structural-validation)
  5. Python logic correctly validates warnings-recipe.yaml: finds expected findings
     including NAME_USES_UNDERSCORES, SHORT_DESCRIPTION, NO_TAGS, VERSION_PLACEHOLDER,
     CONTEXT_NOT_SNAKE_CASE, SHORT_PROMPT, GENERIC_STEP_ID
  6. Python logic correctly validates valid-recipe.yaml: zero or minimal findings
  7. Output structure: phase, results[], summary{total, with_warnings, with_suggestions,
     clean, total_warnings, total_suggestions}
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from conftest import build_recipe_discovery_for_fixture

RECIPE_PATH = Path(__file__).parent.parent / "validate-recipes.yaml"
FIXTURES_PATH = Path(__file__).parent / "fixtures"
REPO_PATH = Path(__file__).parent.parent.parent  # amplifier-bundle-recipes/


@pytest.fixture(scope="module")
def recipe_data():
    content = RECIPE_PATH.read_text()
    return yaml.safe_load(content)


@pytest.fixture(scope="module")
def phase3_step(recipe_data):
    steps = recipe_data.get("steps", [])
    for step in steps:
        if step.get("id") == "best-practices-check":
            return step
    return None


# ── Step Existence & Attributes ────────────────────────────────────────────────


def test_best_practices_step_exists(recipe_data):
    """Step with id='best-practices-check' must exist in the steps list."""
    steps = recipe_data.get("steps", [])
    ids = [s.get("id") for s in steps]
    assert "best-practices-check" in ids, (
        f"'best-practices-check' step not found. Steps found: {ids}"
    )
    print(f"  OK: best-practices-check step exists (all steps: {ids})")


def test_best_practices_step_type(phase3_step):
    """Step type must be 'bash'."""
    assert phase3_step is not None, "best-practices-check step not found"
    assert phase3_step.get("type") == "bash", (
        f"type must be 'bash', got: {phase3_step.get('type')}"
    )
    print("  OK: type=bash")


def test_best_practices_step_timeout(phase3_step):
    """Step timeout must be 60."""
    assert phase3_step is not None, "best-practices-check step not found"
    assert phase3_step.get("timeout") == 60, (
        f"timeout must be 60, got: {phase3_step.get('timeout')}"
    )
    print("  OK: timeout=60")


def test_best_practices_step_on_error(phase3_step):
    """Step on_error must be 'continue'."""
    assert phase3_step is not None, "best-practices-check step not found"
    assert phase3_step.get("on_error") == "continue", (
        f"on_error must be 'continue', got: {phase3_step.get('on_error')}"
    )
    print("  OK: on_error=continue")


def test_best_practices_step_output(phase3_step):
    """Step output must be 'practice_results'."""
    assert phase3_step is not None, "best-practices-check step not found"
    assert phase3_step.get("output") == "practice_results", (
        f"output must be 'practice_results', got: {phase3_step.get('output')}"
    )
    print("  OK: output=practice_results")


def test_best_practices_step_parse_json(phase3_step):
    """Step parse_json must be true."""
    assert phase3_step is not None, "best-practices-check step not found"
    assert phase3_step.get("parse_json") is True, (
        f"parse_json must be true, got: {phase3_step.get('parse_json')}"
    )
    print("  OK: parse_json=true")


def test_best_practices_depends_on_recipe_discovery(phase3_step):
    """Step must depend on recipe-discovery (runs parallel with structural-validation)."""
    assert phase3_step is not None, "best-practices-check step not found"
    depends_on = phase3_step.get("depends_on", [])
    assert "recipe-discovery" in depends_on, (
        f"depends_on must include 'recipe-discovery', got: {depends_on}"
    )
    print(f"  OK: depends_on includes 'recipe-discovery' (got: {depends_on})")


def test_best_practices_step_ordering(recipe_data):
    """best-practices-check must come after recipe-discovery in steps list."""
    steps = recipe_data.get("steps", [])
    ids = [s.get("id") for s in steps]
    assert "recipe-discovery" in ids, "recipe-discovery step missing"
    assert "best-practices-check" in ids, "best-practices-check step missing"
    disc_idx = ids.index("recipe-discovery")
    bp_idx = ids.index("best-practices-check")
    assert bp_idx > disc_idx, (
        f"best-practices-check (index {bp_idx}) must come after "
        f"recipe-discovery (index {disc_idx})"
    )
    print(
        f"  OK: step ordering correct (recipe-discovery={disc_idx}, "
        f"best-practices-check={bp_idx})"
    )


# ── Command Contents ────────────────────────────────────────────────────────────


def test_best_practices_command_has_python(phase3_step):
    """Step command must contain python3 heredoc with check_best_practices."""
    assert phase3_step is not None, "best-practices-check step not found"
    cmd = phase3_step.get("command", "")
    assert "python3" in cmd, "command must contain python3"
    assert "check_best_practices" in cmd, (
        "command must contain check_best_practices function"
    )
    print("  OK: command contains python3 with check_best_practices function")


def test_best_practices_command_references_recipe_discovery(phase3_step):
    """Step command must reference {{recipe_discovery}}."""
    assert phase3_step is not None, "best-practices-check step not found"
    cmd = phase3_step.get("command", "")
    assert "recipe_discovery" in cmd, "command must reference recipe_discovery"
    print("  OK: command references recipe_discovery template var")


# ── Python Logic Execution ──────────────────────────────────────────────────────


def _run_best_practices_check(yaml_path: Path) -> dict:
    """
    Extract Phase 3 Python code from the recipe's bash heredoc and run it
    against the specified fixture file. Returns the parsed JSON output.
    """
    content = RECIPE_PATH.read_text()
    recipe = yaml.safe_load(content)
    steps = recipe.get("steps", [])

    bp_step = None
    for s in steps:
        if s.get("id") == "best-practices-check":
            bp_step = s
            break
    assert bp_step is not None, "best-practices-check step not found in recipe"

    recipe_discovery_data = build_recipe_discovery_for_fixture(yaml_path)

    cmd = bp_step["command"]
    # Replace template variables with JSON-encoded values
    cmd = cmd.replace("{{recipe_discovery}}", json.dumps(recipe_discovery_data))

    # Extract python code between << 'EOF' and EOF
    lines = cmd.split("\n")
    in_python = False
    python_lines = []
    for line in lines:
        if "python3 << 'EOF'" in line or "python3 << EOF" in line:
            in_python = True
            continue
        if in_python and line.strip() == "EOF":
            break
        if in_python:
            python_lines.append(line)

    python_code = "\n".join(python_lines)
    python_code = textwrap.dedent(python_code)

    result = subprocess.run(
        [sys.executable, "-c", python_code],
        capture_output=True,
        text=True,
        cwd=str(REPO_PATH),
    )
    assert result.returncode == 0, (
        f"Phase 3 Python failed (returncode={result.returncode}):\n"
        f"stdout: {result.stdout[:2000]}\n"
        f"stderr: {result.stderr[:2000]}"
    )
    output = json.loads(result.stdout.strip())
    return output


def test_phase3_output_structure():
    """Python code returns JSON with required top-level keys."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)

    required_keys = ["phase", "results", "summary"]
    for key in required_keys:
        assert key in output, (
            f"Required key '{key}' missing from output. Keys: {list(output.keys())}"
        )
    assert output["phase"] == "best_practices", (
        f"phase must be 'best_practices', got: {output['phase']}"
    )
    assert isinstance(output["results"], list), "results must be a list"

    # summary must have all required sub-keys
    summary = output["summary"]
    for skey in [
        "total",
        "with_warnings",
        "with_suggestions",
        "clean",
        "total_warnings",
        "total_suggestions",
    ]:
        assert skey in summary, (
            f"summary key '{skey}' missing. Summary keys: {list(summary.keys())}"
        )
    print(
        f"  OK: output structure correct "
        f"(phase=best_practices, results={len(output['results'])}, summary={summary})"
    )


def test_phase3_warnings_recipe_name_uses_underscores():
    """Warnings fixture has underscores in name — should find NAME_USES_UNDERSCORES."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    finding_codes = [f["code"] for f in findings]

    assert "NAME_USES_UNDERSCORES" in finding_codes, (
        f"Expected NAME_USES_UNDERSCORES finding for 'warnings_test_recipe', "
        f"got codes: {finding_codes}"
    )
    print("  OK: NAME_USES_UNDERSCORES finding detected")


def test_phase3_warnings_recipe_short_description():
    """Warnings fixture has short description — should find SHORT_DESCRIPTION."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    finding_codes = [f["code"] for f in findings]

    assert "SHORT_DESCRIPTION" in finding_codes, (
        f"Expected SHORT_DESCRIPTION finding, got codes: {finding_codes}"
    )
    print("  OK: SHORT_DESCRIPTION finding detected")


def test_phase3_warnings_recipe_no_tags():
    """Warnings fixture has no tags — should find NO_TAGS."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    finding_codes = [f["code"] for f in findings]

    assert "NO_TAGS" in finding_codes, (
        f"Expected NO_TAGS finding, got codes: {finding_codes}"
    )
    print("  OK: NO_TAGS finding detected")


def test_phase3_warnings_recipe_version_placeholder():
    """Warnings fixture has version 0.0.0 — should find VERSION_PLACEHOLDER."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    finding_codes = [f["code"] for f in findings]

    assert "VERSION_PLACEHOLDER" in finding_codes, (
        f"Expected VERSION_PLACEHOLDER finding for version '0.0.0', "
        f"got codes: {finding_codes}"
    )
    print("  OK: VERSION_PLACEHOLDER finding detected")


def test_phase3_warnings_recipe_context_not_snake_case():
    """Warnings fixture has 'InputData' context var — should find CONTEXT_NOT_SNAKE_CASE."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    context_findings = [f for f in findings if f["code"] == "CONTEXT_NOT_SNAKE_CASE"]

    assert len(context_findings) >= 1, (
        f"Expected at least 1 CONTEXT_NOT_SNAKE_CASE finding for 'InputData', "
        f"got: {findings}"
    )
    # Check that InputData is mentioned
    all_detail = " ".join(
        f.get("detail", "") + " " + f.get("message", "") for f in context_findings
    )
    assert "InputData" in all_detail, (
        f"Expected 'InputData' mentioned in CONTEXT_NOT_SNAKE_CASE findings. "
        f"Detail: {all_detail}"
    )
    print("  OK: CONTEXT_NOT_SNAKE_CASE(InputData) finding detected")


def test_phase3_warnings_recipe_short_prompt():
    """Warnings fixture has short prompts — should find multiple SHORT_PROMPT."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    short_prompt_findings = [f for f in findings if f["code"] == "SHORT_PROMPT"]

    assert len(short_prompt_findings) >= 2, (
        f"Expected at least 2 SHORT_PROMPT findings (multiple short prompts), "
        f"got {len(short_prompt_findings)}: {short_prompt_findings}"
    )
    print(f"  OK: {len(short_prompt_findings)} SHORT_PROMPT findings detected")


def test_phase3_warnings_recipe_generic_step_id():
    """Warnings fixture has step1..step4 IDs — should find multiple GENERIC_STEP_ID."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    generic_id_findings = [f for f in findings if f["code"] == "GENERIC_STEP_ID"]

    assert len(generic_id_findings) >= 2, (
        f"Expected at least 2 GENERIC_STEP_ID findings (step1, step2, etc.), "
        f"got {len(generic_id_findings)}: {generic_id_findings}"
    )
    print(f"  OK: {len(generic_id_findings)} GENERIC_STEP_ID findings detected")


def test_phase3_valid_recipe_minimal_findings():
    """Valid fixture should produce zero or minimal findings (no warnings)."""
    valid_fixture = FIXTURES_PATH / "valid-recipe.yaml"
    output = _run_best_practices_check(valid_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    warning_findings = [f for f in findings if f.get("severity") == "WARNING"]

    assert len(warning_findings) == 0, (
        f"Expected zero WARNING findings for valid-recipe.yaml, "
        f"got {len(warning_findings)}: {warning_findings}"
    )
    summary = output["summary"]
    assert summary["total_warnings"] == 0, (
        f"Expected total_warnings=0 for valid-recipe.yaml, got: {summary}"
    )
    print(
        f"  OK: valid-recipe.yaml has no warnings "
        f"(total findings: {len(findings)}, summary: {summary})"
    )


def test_phase3_result_entry_has_required_fields():
    """Each result entry must have required fields."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)
    results = output["results"]
    assert len(results) > 0

    entry = results[0]
    for field in ["path", "findings", "warning_count", "suggestion_count"]:
        assert field in entry, (
            f"Result entry missing required field '{field}'. "
            f"Fields present: {list(entry.keys())}"
        )
    print(f"  OK: result entry has required fields: {list(entry.keys())}")


def test_phase3_finding_has_required_fields():
    """Each finding must have code, severity, message fields."""
    warnings_fixture = FIXTURES_PATH / "warnings-recipe.yaml"
    output = _run_best_practices_check(warnings_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    assert len(findings) > 0, "Expected at least one finding for warnings-recipe.yaml"

    for finding in findings[:5]:  # check first 5
        for field in ["code", "severity", "message"]:
            assert field in finding, (
                f"Finding missing required field '{field}'. Finding: {finding}"
            )
    print("  OK: findings have required fields (code, severity, message)")
