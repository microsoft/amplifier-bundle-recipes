"""
Tests for Task 6: Phase 4 Semantic Validation step in validate-recipes.yaml.
Tests:
  1. The semantic-validation step exists in validate-recipes.yaml
  2. Step has correct attributes: id, type, timeout, on_error, depends_on, output, parse_json
  3. Step command contains python3 heredoc with validate_semantic function
  4. Step depends_on recipe-discovery (parallel with structural and best-practices)
  5. Python logic detects BARE_AGENT_NAME for 'bare-agent-name' in broken-recipe.yaml
  6. Python logic allows 'foundation:zen-architect' (no BARE_AGENT_NAME)
  7. Cross-recipe duplicate name detection works across multiple files
  8. Output structure: phase, local_namespace, local_agents_count, known_agent_patterns,
     results[], cross_recipe_findings[], summary{total, with_errors, with_warnings,
     clean, total_errors, total_warnings}
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
def phase4_step(recipe_data):
    steps = recipe_data.get("steps", [])
    for step in steps:
        if step.get("id") == "semantic-validation":
            return step
    return None


# ── Step Existence & Attributes ────────────────────────────────────────────────


def test_semantic_validation_step_exists(recipe_data):
    """Step with id='semantic-validation' must exist in the steps list."""
    steps = recipe_data.get("steps", [])
    ids = [s.get("id") for s in steps]
    assert "semantic-validation" in ids, (
        f"'semantic-validation' step not found. Steps found: {ids}"
    )
    print(f"  OK: semantic-validation step exists (all steps: {ids})")


def test_semantic_validation_step_type(phase4_step):
    """Step type must be 'bash'."""
    assert phase4_step is not None, "semantic-validation step not found"
    assert phase4_step.get("type") == "bash", (
        f"type must be 'bash', got: {phase4_step.get('type')}"
    )
    print("  OK: type=bash")


def test_semantic_validation_step_timeout(phase4_step):
    """Step timeout must be 120."""
    assert phase4_step is not None, "semantic-validation step not found"
    assert phase4_step.get("timeout") == 120, (
        f"timeout must be 120, got: {phase4_step.get('timeout')}"
    )
    print("  OK: timeout=120")


def test_semantic_validation_step_on_error(phase4_step):
    """Step on_error must be 'continue'."""
    assert phase4_step is not None, "semantic-validation step not found"
    assert phase4_step.get("on_error") == "continue", (
        f"on_error must be 'continue', got: {phase4_step.get('on_error')}"
    )
    print("  OK: on_error=continue")


def test_semantic_validation_step_output(phase4_step):
    """Step output must be 'semantic_results'."""
    assert phase4_step is not None, "semantic-validation step not found"
    assert phase4_step.get("output") == "semantic_results", (
        f"output must be 'semantic_results', got: {phase4_step.get('output')}"
    )
    print("  OK: output=semantic_results")


def test_semantic_validation_step_parse_json(phase4_step):
    """Step parse_json must be true."""
    assert phase4_step is not None, "semantic-validation step not found"
    assert phase4_step.get("parse_json") is True, (
        f"parse_json must be true, got: {phase4_step.get('parse_json')}"
    )
    print("  OK: parse_json=true")


def test_semantic_validation_depends_on_recipe_discovery(phase4_step):
    """Step must depend on recipe-discovery."""
    assert phase4_step is not None, "semantic-validation step not found"
    depends_on = phase4_step.get("depends_on", [])
    assert "recipe-discovery" in depends_on, (
        f"depends_on must include 'recipe-discovery', got: {depends_on}"
    )
    print(f"  OK: depends_on includes 'recipe-discovery' (got: {depends_on})")


def test_semantic_validation_step_ordering(recipe_data):
    """semantic-validation must come after recipe-discovery in steps list."""
    steps = recipe_data.get("steps", [])
    ids = [s.get("id") for s in steps]
    assert "recipe-discovery" in ids, "recipe-discovery step missing"
    assert "semantic-validation" in ids, "semantic-validation step missing"
    disc_idx = ids.index("recipe-discovery")
    sem_idx = ids.index("semantic-validation")
    assert sem_idx > disc_idx, (
        f"semantic-validation (index {sem_idx}) must come after "
        f"recipe-discovery (index {disc_idx})"
    )
    print(
        f"  OK: step ordering correct (recipe-discovery={disc_idx}, "
        f"semantic-validation={sem_idx})"
    )


# ── Command Contents ──────────────────────────────────────────────────────────


def test_semantic_validation_command_has_python(phase4_step):
    """Step command must contain python3 heredoc with validate_semantic."""
    assert phase4_step is not None, "semantic-validation step not found"
    cmd = phase4_step.get("command", "")
    assert "python3" in cmd, "command must contain python3"
    assert "validate_semantic" in cmd, "command must contain validate_semantic function"
    print("  OK: command contains python3 with validate_semantic function")


def test_semantic_validation_command_references_template_vars(phase4_step):
    """Step command must reference {{recipe_discovery}} and {{known_agents}}."""
    assert phase4_step is not None, "semantic-validation step not found"
    cmd = phase4_step.get("command", "")
    assert "recipe_discovery" in cmd, "command must reference recipe_discovery"
    assert "known_agents" in cmd, "command must reference known_agents"
    print("  OK: command references recipe_discovery and known_agents template vars")


def test_semantic_validation_command_has_bare_agent_check(phase4_step):
    """Step command must contain BARE_AGENT_NAME check logic."""
    assert phase4_step is not None, "semantic-validation step not found"
    cmd = phase4_step.get("command", "")
    assert "BARE_AGENT_NAME" in cmd, "command must contain BARE_AGENT_NAME check"
    print("  OK: command contains BARE_AGENT_NAME check")


def test_semantic_validation_command_has_cross_recipe_check(phase4_step):
    """Step command must contain DUPLICATE_RECIPE_NAME cross-recipe check."""
    assert phase4_step is not None, "semantic-validation step not found"
    cmd = phase4_step.get("command", "")
    assert "DUPLICATE_RECIPE_NAME" in cmd, (
        "command must contain DUPLICATE_RECIPE_NAME check"
    )
    print("  OK: command contains DUPLICATE_RECIPE_NAME check")


# ── Python Logic Execution ────────────────────────────────────────────────────


def _build_recipe_discovery_for_fixture(yaml_path: Path) -> dict:
    """Build a recipe_discovery dict containing the specified fixture file as a recipe."""
    try:
        data = yaml.safe_load(yaml_path.read_text())
    except Exception:
        data = None
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


def _build_recipe_discovery_for_two_fixtures(
    yaml_path1: Path, yaml_path2: Path
) -> dict:
    """Build a recipe_discovery dict with two recipe files (for cross-recipe testing)."""
    recipes = []
    for yaml_path in [yaml_path1, yaml_path2]:
        try:
            data = yaml.safe_load(yaml_path.read_text())
        except Exception:
            data = None
        version_val = data.get("version") if data else None
        recipe_info = {
            "path": str(yaml_path),
            "relative_path": yaml_path.name,
            "filename": yaml_path.name,
            "name": data.get("name") if data else None,
            "version": str(version_val) if version_val is not None else None,
            "description": str(data.get("description", "") or "")[:200]
            if data
            else None,
            "size_bytes": yaml_path.stat().st_size,
            "step_count": len(data.get("steps", [])) if data else 0,
            "is_staged": bool(data.get("stages")) if data else False,
            "has_context": ("context" in data) if data else False,
            "has_tags": bool(data.get("tags")) if data else False,
            "parse_error": False,
            "is_sub_recipe": False,
        }
        recipes.append(recipe_info)
    return {
        "phase": "discovery",
        "repo_path": str(yaml_path1.parent),
        "recipes_dir": ".",
        "recipes": recipes,
        "non_recipe_yaml": [],
        "parse_errors": [],
        "total_count": 2,
        "search_paths": [str(yaml_path1.parent)],
        "errors": [],
    }


def _run_semantic_validation(
    yaml_path: Path,
    known_agents: str = "[]",
    extra_recipes: list | None = None,
) -> dict:
    """
    Extract Phase 4 Python code from the recipe's bash heredoc and run it
    against the specified fixture file. Returns the parsed JSON output.
    """
    content = RECIPE_PATH.read_text()
    recipe = yaml.safe_load(content)
    steps = recipe.get("steps", [])

    sem_step = None
    for s in steps:
        if s.get("id") == "semantic-validation":
            sem_step = s
            break
    assert sem_step is not None, "semantic-validation step not found in recipe"

    if extra_recipes:
        recipe_discovery_data = _build_recipe_discovery_for_two_fixtures(
            yaml_path, extra_recipes[0]
        )
    else:
        recipe_discovery_data = _build_recipe_discovery_for_fixture(yaml_path)

    cmd = sem_step["command"]
    # Replace template variables with JSON-encoded values
    cmd = cmd.replace("{{recipe_discovery}}", json.dumps(recipe_discovery_data))
    cmd = cmd.replace("{{known_agents}}", known_agents)
    cmd = cmd.replace("{{repo_path}}", str(REPO_PATH))

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
        f"Phase 4 Python failed (returncode={result.returncode}):\n"
        f"stdout: {result.stdout[:2000]}\n"
        f"stderr: {result.stderr[:2000]}"
    )
    output = json.loads(result.stdout.strip())
    return output


def test_phase4_output_structure():
    """Python code returns JSON with required top-level keys."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_semantic_validation(broken_fixture)

    required_keys = [
        "phase",
        "local_namespace",
        "local_agents_count",
        "known_agent_patterns",
        "results",
        "cross_recipe_findings",
        "summary",
    ]
    for key in required_keys:
        assert key in output, (
            f"Required key '{key}' missing from output. Keys: {list(output.keys())}"
        )
    assert output["phase"] == "semantic", (
        f"phase must be 'semantic', got: {output['phase']}"
    )
    assert isinstance(output["results"], list), "results must be a list"
    assert isinstance(output["cross_recipe_findings"], list), (
        "cross_recipe_findings must be a list"
    )

    # summary must have all required sub-keys
    summary = output["summary"]
    for skey in [
        "total",
        "with_errors",
        "with_warnings",
        "clean",
        "total_errors",
        "total_warnings",
    ]:
        assert skey in summary, (
            f"summary key '{skey}' missing. Summary keys: {list(summary.keys())}"
        )
    print(
        f"  OK: output structure correct "
        f"(phase=semantic, results={len(output['results'])}, summary={summary})"
    )


def test_phase4_bare_agent_name_detection():
    """
    Broken fixture has 'bare-agent-name' (no colon) — should find BARE_AGENT_NAME.
    This validates: bare agent name detection correctly identifies missing namespace.
    """
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_semantic_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0, "Should have at least one result for broken-recipe.yaml"

    findings = results[0].get("findings", [])
    finding_codes = [f["code"] for f in findings]

    assert "BARE_AGENT_NAME" in finding_codes, (
        f"Expected BARE_AGENT_NAME finding for 'bare-agent-name', "
        f"got codes: {finding_codes}"
    )
    # Verify the agent name is mentioned
    bare_findings = [f for f in findings if f["code"] == "BARE_AGENT_NAME"]
    all_detail = " ".join(
        f.get("detail", "") + " " + f.get("message", "") for f in bare_findings
    )
    assert "bare-agent-name" in all_detail, (
        f"Expected 'bare-agent-name' in BARE_AGENT_NAME finding detail. "
        f"Detail: {all_detail}"
    )
    print("  OK: BARE_AGENT_NAME finding detected for 'bare-agent-name'")


def test_phase4_valid_agent_with_colon_no_bare_warning():
    """
    'foundation:zen-architect' has a colon — should NOT trigger BARE_AGENT_NAME.
    Tests that namespace:name format passes agent reference validation.
    """
    valid_fixture = FIXTURES_PATH / "valid-recipe.yaml"
    # Pass foundation:* as a known pattern so the agent is recognized
    output = _run_semantic_validation(valid_fixture, known_agents='["foundation:*"]')
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    bare_findings = [f for f in findings if f["code"] == "BARE_AGENT_NAME"]

    assert len(bare_findings) == 0, (
        f"'foundation:zen-architect' has a colon and should not trigger "
        f"BARE_AGENT_NAME. Got: {bare_findings}"
    )
    print(
        "  OK: 'foundation:zen-architect' (with colon) does NOT trigger BARE_AGENT_NAME"
    )


def test_phase4_bare_finding_has_warning_severity():
    """BARE_AGENT_NAME finding must have WARNING severity."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_semantic_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    bare_findings = [f for f in findings if f["code"] == "BARE_AGENT_NAME"]
    assert len(bare_findings) > 0, "Expected BARE_AGENT_NAME finding"

    for finding in bare_findings:
        assert finding.get("severity") == "WARNING", (
            f"BARE_AGENT_NAME must have severity=WARNING, got: {finding.get('severity')}"
        )
    print("  OK: BARE_AGENT_NAME findings have WARNING severity")


def test_phase4_duplicate_recipe_name_detection():
    """
    When two recipes have the same name, DUPLICATE_RECIPE_NAME should be in
    cross_recipe_findings.
    """
    # Use valid-recipe and broken-recipe but override names to be the same
    # We'll create a temp fixture with a duplicate name
    import tempfile

    dup_content = """
name: "valid-test-recipe"
description: "A duplicate name recipe for cross-recipe testing."
version: "1.0.0"
steps:
  - id: "step-one"
    type: bash
    command: "echo hello"
    timeout: 30
    on_error: continue
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", dir=str(FIXTURES_PATH), delete=False
    ) as f:
        f.write(dup_content)
        dup_path = Path(f.name)

    try:
        valid_fixture = FIXTURES_PATH / "valid-recipe.yaml"
        output = _run_semantic_validation(valid_fixture, extra_recipes=[dup_path])

        cross_findings = output.get("cross_recipe_findings", [])
        cross_codes = [f["code"] for f in cross_findings]

        assert "DUPLICATE_RECIPE_NAME" in cross_codes, (
            f"Expected DUPLICATE_RECIPE_NAME in cross_recipe_findings "
            f"when two recipes share the name 'valid-test-recipe'. "
            f"Got codes: {cross_codes}. "
            f"Cross findings: {cross_findings}"
        )
        print(
            f"  OK: DUPLICATE_RECIPE_NAME detected in cross_recipe_findings: "
            f"{cross_findings}"
        )
    finally:
        dup_path.unlink(missing_ok=True)


def test_phase4_result_entry_has_required_fields():
    """Each result entry must have required fields."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_semantic_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0

    entry = results[0]
    for field in ["path", "findings", "error_count", "warning_count"]:
        assert field in entry, (
            f"Result entry missing required field '{field}'. "
            f"Fields present: {list(entry.keys())}"
        )
    print(f"  OK: result entry has required fields: {list(entry.keys())}")


def test_phase4_finding_has_required_fields():
    """Each finding must have code, severity, message fields."""
    broken_fixture = FIXTURES_PATH / "broken-recipe.yaml"
    output = _run_semantic_validation(broken_fixture)
    results = output["results"]
    assert len(results) > 0

    findings = results[0].get("findings", [])
    assert len(findings) > 0, "Expected at least one finding for broken-recipe.yaml"

    for finding in findings[:5]:
        for field in ["code", "severity", "message"]:
            assert field in finding, (
                f"Finding missing required field '{field}'. Finding: {finding}"
            )
    print("  OK: findings have required fields (code, severity, message)")
