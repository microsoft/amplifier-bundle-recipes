"""
Tests for Task 4: Phase 2 Structural Validation step in validate-recipes.yaml.
Tests:
  1. The structural-validation step exists in validate-recipes.yaml
  2. Step has correct attributes: id, type, timeout, on_error, depends_on, output, parse_json
  3. Step command contains python3 heredoc with validate_recipe_structural function
  4. Python logic correctly validates broken-recipe.yaml: finds UNKNOWN_RECIPE_KEY (naem, stesp),
     MISSING_NAME, MISSING_DESCRIPTION errors
  5. Python logic correctly validates valid-recipe.yaml: zero findings
  6. Output structure: phase, validation_mode, results[], summary{...}
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

RECIPE_PATH = Path(__file__).parent.parent / "validate-recipes.yaml"
FIXTURES_PATH = Path(__file__).parent / "fixtures"
REPO_PATH = Path(__file__).parent.parent.parent  # amplifier-bundle-recipes/


@pytest.fixture(scope="module")
def recipe_data():
    content = RECIPE_PATH.read_text()
    return yaml.safe_load(content)


@pytest.fixture(scope="module")
def phase2_step(recipe_data):
    steps = recipe_data.get("steps", [])
    for step in steps:
        if step.get("id") == "structural-validation":
            return step
    return None


# ── Step Existence & Attributes ────────────────────────────────────────────


def test_structural_validation_step_exists(recipe_data):
    """Step with id='structural-validation' must exist in the steps list."""
    steps = recipe_data.get("steps", [])
    ids = [s.get("id") for s in steps]
    assert "structural-validation" in ids, (
        f"'structural-validation' step not found. Steps found: {ids}"
    )
    print(f"  OK: structural-validation step exists (all steps: {ids})")


def test_structural_validation_step_type(phase2_step):
    """Step type must be 'bash'."""
    assert phase2_step is not None, "structural-validation step not found"
    assert phase2_step.get("type") == "bash", (
        f"type must be 'bash', got: {phase2_step.get('type')}"
    )
    print("  OK: type=bash")


def test_structural_validation_step_timeout(phase2_step):
    """Step timeout must be 120."""
    assert phase2_step is not None, "structural-validation step not found"
    assert phase2_step.get("timeout") == 120, (
        f"timeout must be 120, got: {phase2_step.get('timeout')}"
    )
    print("  OK: timeout=120")


def test_structural_validation_step_on_error(phase2_step):
    """Step on_error must be 'continue'."""
    assert phase2_step is not None, "structural-validation step not found"
    assert phase2_step.get("on_error") == "continue", (
        f"on_error must be 'continue', got: {phase2_step.get('on_error')}"
    )
    print("  OK: on_error=continue")


def test_structural_validation_step_output(phase2_step):
    """Step output must be 'structural_results'."""
    assert phase2_step is not None, "structural-validation step not found"
    assert phase2_step.get("output") == "structural_results", (
        f"output must be 'structural_results', got: {phase2_step.get('output')}"
    )
    print("  OK: output=structural_results")


def test_structural_validation_step_parse_json(phase2_step):
    """Step parse_json must be true."""
    assert phase2_step is not None, "structural-validation step not found"
    assert phase2_step.get("parse_json") is True, (
        f"parse_json must be true, got: {phase2_step.get('parse_json')}"
    )
    print("  OK: parse_json=true")


def test_structural_validation_depends_on_recipe_discovery(phase2_step):
    """Step must depend on recipe-discovery."""
    assert phase2_step is not None, "structural-validation step not found"
    depends_on = phase2_step.get("depends_on", [])
    assert "recipe-discovery" in depends_on, (
        f"depends_on must include 'recipe-discovery', got: {depends_on}"
    )
    print(f"  OK: depends_on includes 'recipe-discovery' (got: {depends_on})")


def test_structural_validation_step_ordering(recipe_data):
    """structural-validation must come after recipe-discovery in steps list."""
    steps = recipe_data.get("steps", [])
    ids = [s.get("id") for s in steps]
    assert "recipe-discovery" in ids, "recipe-discovery step missing"
    assert "structural-validation" in ids, "structural-validation step missing"
    disc_idx = ids.index("recipe-discovery")
    struct_idx = ids.index("structural-validation")
    assert struct_idx > disc_idx, (
        f"structural-validation (index {struct_idx}) must come after "
        f"recipe-discovery (index {disc_idx})"
    )
    print(
        f"  OK: step ordering correct (recipe-discovery={disc_idx}, "
        f"structural-validation={struct_idx})"
    )


# ── Command Contents ───────────────────────────────────────────────────────


def test_structural_validation_command_has_python(phase2_step):
    """Step command must contain python3 heredoc with validate_recipe_structural."""
    assert phase2_step is not None, "structural-validation step not found"
    cmd = phase2_step.get("command", "")
    assert "python3" in cmd, "command must contain python3"
    assert "validate_recipe_structural" in cmd, (
        "command must contain validate_recipe_structural function"
    )
    print("  OK: command contains python3 with validate_recipe_structural")


def test_structural_validation_command_has_constants(phase2_step):
    """Step command must define VALID_RECIPE_KEYS, VALID_STEP_KEYS, VALID_STEP_TYPES."""
    assert phase2_step is not None, "structural-validation step not found"
    cmd = phase2_step.get("command", "")
    assert "VALID_RECIPE_KEYS" in cmd, "command must define VALID_RECIPE_KEYS"
    assert "VALID_STEP_KEYS" in cmd, "command must define VALID_STEP_KEYS"
    assert "VALID_STEP_TYPES" in cmd, "command must define VALID_STEP_TYPES"
    print("  OK: command defines VALID_RECIPE_KEYS, VALID_STEP_KEYS, VALID_STEP_TYPES")


def test_structural_validation_command_references_template_vars(phase2_step):
    """Step command must reference {{env_check}} and {{recipe_discovery}}."""
    assert phase2_step is not None, "structural-validation step not found"
    cmd = phase2_step.get("command", "")
    assert "env_check" in cmd, "command must reference env_check"
    assert "recipe_discovery" in cmd, "command must reference recipe_discovery"
    print("  OK: command references env_check and recipe_discovery template vars")


# ── Python Logic Execution ─────────────────────────────────────────────────


def _build_recipe_discovery_for_fixture(yaml_path: Path) -> dict:
    """Build a recipe_discovery dict containing the specified fixture file as a recipe."""
    data = yaml.safe_load(yaml_path.read_text())
    # Build recipe_info matching the discovery step's output format
    version_val = data.get("version") if data else None
    recipe_info = {
        "path": str(yaml_path),
        "relative_path": yaml_path.name,
        "filename": yaml_path.name,
        "name": data.get("name") if data else None,
        "version": str(version_val) if version_val is not None else None,
        "description": str(data.get("description", "") or "")[:200] if data else None,
        "size_bytes": yaml_path.stat().st_size,
        "step_count": len(data.get("steps", [])) if data else 0,
        "is_staged": bool(data.get("stages")) if data else False,
        "has_context": ("context" in data) if data else False,
        "has_tags": bool(data.get("tags")) if data else False,
        "parse_error": False,
        "is_sub_recipe": False,
    }
    return {
        "phase": "discovery",
        "repo_path": str(yaml_path.parent),
        "recipes_dir": ".",
        "recipes": [recipe_info],
        "non_recipe_yaml": [],
        "parse_errors": [],
        "total_count": 1,
        "search_paths": [str(yaml_path.parent)],
        "errors": [],
    }


def _run_structural_validation(
    yaml_path: Path, validation_mode: str = "structural_only"
) -> dict:
    """
    Extract Phase 2 Python code from the recipe's bash heredoc and run it
    against the specified fixture file. Returns the parsed JSON output.
    """
    content = RECIPE_PATH.read_text()
    recipe = yaml.safe_load(content)
    steps = recipe.get("steps", [])

    struct_step = None
    for s in steps:
        if s.get("id") == "structural-validation":
            struct_step = s
            break
    assert struct_step is not None, "structural-validation step not found in recipe"

    recipe_discovery_data = _build_recipe_discovery_for_fixture(yaml_path)
    env_check_data = {
        "phase": "environment",
        "validation_mode": validation_mode,
        "engine_available": validation_mode == "full",
        "yaml_available": True,
        "repo_exists": True,
        "errors": [],
    }

    cmd = struct_step["command"]
    # Replace template variables with JSON-encoded values
    cmd = cmd.replace("{{recipe_discovery}}", json.dumps(recipe_discovery_data))
    cmd = cmd.replace("{{env_check}}", json.dumps(env_check_data))

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
        f"Phase 2 Python failed (returncode={result.returncode}):\n"
        f"stdout: {result.stdout[:2000]}\n"
        f"stderr: {result.stderr[:2000]}"
    )
    output = json.loads(result.stdout.strip())
    return output


def test_phase2_output_structure_broken_recipe():
    """Python code returns JSON with required top-level keys."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_structural_validation(broken_fixture)

    required_keys = ["phase", "validation_mode", "results", "summary"]
    for key in required_keys:
        assert key in output, (
            f"Required key '{key}' missing from output. Keys: {list(output.keys())}"
        )
    assert output["phase"] == "structural", (
        f"phase must be 'structural', got: {output['phase']}"
    )
    assert isinstance(output["results"], list), "results must be a list"

    # summary must have all required sub-keys
    summary = output["summary"]
    for skey in [
        "total",
        "clean",
        "with_errors",
        "with_warnings",
        "total_errors",
        "total_warnings",
    ]:
        assert skey in summary, (
            f"summary key '{skey}' missing. Summary keys: {list(summary.keys())}"
        )
    print(
        f"  OK: output structure correct "
        f"(phase=structural, results={len(output['results'])}, summary={summary})"
    )


def test_phase2_broken_recipe_unknown_keys():
    """Broken fixture should have UNKNOWN_RECIPE_KEY findings for 'naem' and 'stesp'."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_structural_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0, "Should have at least one result for broken-recipe.yaml"

    findings = results[0].get("findings", [])
    unknown_key_findings = [f for f in findings if f["code"] == "UNKNOWN_RECIPE_KEY"]

    assert len(unknown_key_findings) >= 2, (
        f"Expected at least 2 UNKNOWN_RECIPE_KEY findings (naem, stesp), "
        f"got {len(unknown_key_findings)}: {unknown_key_findings}"
    )

    # Verify naem and stesp are mentioned in the findings
    all_details = " ".join(
        f.get("detail", "") + " " + f.get("message", "") for f in unknown_key_findings
    )
    assert "naem" in all_details, (
        f"Expected 'naem' mentioned in UNKNOWN_RECIPE_KEY findings. Details: {all_details}"
    )
    assert "stesp" in all_details, (
        f"Expected 'stesp' mentioned in UNKNOWN_RECIPE_KEY findings. Details: {all_details}"
    )
    print("  OK: UNKNOWN_RECIPE_KEY findings for 'naem' and 'stesp' found")


def test_phase2_broken_recipe_missing_name():
    """Broken fixture should have MISSING_NAME finding (uses 'naem' typo instead of 'name')."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_structural_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    finding_codes = [f["code"] for f in findings]

    assert "MISSING_NAME" in finding_codes, (
        f"Expected MISSING_NAME finding, got codes: {finding_codes}"
    )
    print("  OK: MISSING_NAME finding detected for broken-recipe.yaml")


def test_phase2_broken_recipe_missing_description():
    """Broken fixture should have MISSING_DESCRIPTION finding (empty description string)."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_structural_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    finding_codes = [f["code"] for f in findings]

    assert "MISSING_DESCRIPTION" in finding_codes, (
        f"Expected MISSING_DESCRIPTION finding, got codes: {finding_codes}"
    )
    print("  OK: MISSING_DESCRIPTION finding detected for broken-recipe.yaml")


def test_phase2_broken_recipe_has_errors():
    """Broken fixture should produce ERROR-level findings in the summary."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_structural_validation(broken_fixture)
    summary = output["summary"]

    assert summary["with_errors"] > 0, (
        f"Expected with_errors > 0 for broken-recipe.yaml, got: {summary}"
    )
    assert summary["total_errors"] > 0, (
        f"Expected total_errors > 0 for broken-recipe.yaml, got: {summary}"
    )
    print(f"  OK: broken-recipe.yaml summary has errors: {summary}")


def test_phase2_valid_recipe_zero_findings():
    """Valid fixture should produce zero findings."""
    valid_fixture = FIXTURES_PATH / "valid-recipe.yaml"
    output = _run_structural_validation(valid_fixture)
    results = output["results"]
    assert len(results) > 0, "Should have at least one result for valid-recipe.yaml"

    findings = results[0].get("findings", [])
    assert len(findings) == 0, (
        f"Expected zero findings for valid-recipe.yaml, got {len(findings)}: {findings}"
    )
    print("  OK: valid-recipe.yaml has zero findings")


def test_phase2_valid_recipe_summary_clean():
    """Valid fixture should show clean=1 and zero errors in summary."""
    valid_fixture = FIXTURES_PATH / "valid-recipe.yaml"
    output = _run_structural_validation(valid_fixture)
    summary = output["summary"]

    assert summary["clean"] == 1, (
        f"Expected clean=1 for valid-recipe.yaml, got: {summary}"
    )
    assert summary["with_errors"] == 0, (
        f"Expected with_errors=0 for valid-recipe.yaml, got: {summary}"
    )
    assert summary["total_errors"] == 0, (
        f"Expected total_errors=0 for valid-recipe.yaml, got: {summary}"
    )
    print(f"  OK: valid-recipe.yaml summary is clean: {summary}")


def test_phase2_result_entry_has_required_fields():
    """Each result entry must have required fields."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_structural_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0

    entry = results[0]
    for field in ["path", "findings", "error_count", "warning_count"]:
        assert field in entry, (
            f"Result entry missing required field '{field}'. "
            f"Fields present: {list(entry.keys())}"
        )
    print(f"  OK: result entry has required fields: {list(entry.keys())}")


def test_phase2_finding_has_required_fields():
    """Each finding must have code, severity, message fields."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_structural_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    assert len(findings) > 0, "Expected at least one finding for broken-recipe.yaml"

    for finding in findings[:3]:  # check first 3
        for field in ["code", "severity", "message"]:
            assert field in finding, (
                f"Finding missing required field '{field}'. Finding: {finding}"
            )
    print("  OK: findings have required fields (code, severity, message)")


def test_phase2_invalid_version_finding():
    """Broken fixture has 'not-a-version' — should produce INVALID_VERSION finding."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_structural_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    finding_codes = [f["code"] for f in findings]

    assert "INVALID_VERSION" in finding_codes, (
        f"Expected INVALID_VERSION finding for 'not-a-version', got codes: {finding_codes}"
    )
    print("  OK: INVALID_VERSION finding detected for 'not-a-version'")
