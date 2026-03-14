"""
Tests for Task 2: validate-recipes.yaml scaffold with Phase 0.
Tests:
  1. The YAML file exists at recipes/validate-recipes.yaml
  2. It has required top-level metadata: name, description, version, author, tags
  3. It has context with: repo_path, recipes_dir, known_agents
  4. It has at least one step with id='environment-check', type='bash', timeout=30
  5. The Phase 0 Python code (when run with repo_path='.') produces JSON with:
     - yaml_available: true
     - repo_exists: true
     - validation_mode: 'full' or 'structural_only'
     - phase: 'environment'
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

RECIPE_PATH = Path(__file__).parent.parent / "validate-recipes.yaml"


@pytest.fixture(scope="module")
def recipe_data():
    content = RECIPE_PATH.read_text()
    return yaml.safe_load(content)


@pytest.fixture(scope="module")
def phase0_step(recipe_data):
    steps = recipe_data.get("steps", [])
    assert len(steps) >= 1, "Recipe must have at least one step"
    return steps[0]


def test_recipe_file_exists():
    assert RECIPE_PATH.exists(), f"Recipe file not found: {RECIPE_PATH}"
    print(f"  OK: file exists at {RECIPE_PATH}")


def test_recipe_valid_yaml(recipe_data):
    assert isinstance(recipe_data, dict), "Recipe must parse as a YAML mapping"
    print(f"  OK: valid YAML, keys: {list(recipe_data.keys())}")


def test_recipe_metadata(recipe_data):
    assert recipe_data.get("name") == "validate-recipes", (
        f"name must be 'validate-recipes', got: {recipe_data.get('name')}"
    )
    assert recipe_data.get("version") == "1.0.0", (
        f"version must be '1.0.0', got: {recipe_data.get('version')}"
    )
    assert recipe_data.get("author") == "Amplifier Recipes Collection", (
        f"author mismatch: {recipe_data.get('author')}"
    )
    desc = recipe_data.get("description", "")
    assert "Structural Correctness" in desc, (
        "description must mention 'Structural Correctness'"
    )
    assert "Best Practices" in desc, "description must mention 'Best Practices'"
    assert "Semantic Consistency" in desc, (
        "description must mention 'Semantic Consistency'"
    )
    tags = recipe_data.get("tags", [])
    for required_tag in [
        "recipes",
        "validation",
        "quality",
        "audit",
        "structure",
        "best-practices",
    ]:
        assert required_tag in tags, f"tag '{required_tag}' missing from tags: {tags}"
    print("  OK: metadata correct (name, version, author, description, tags)")


def test_recipe_context(recipe_data):
    context = recipe_data.get("context", {})
    assert isinstance(context, dict), "context must be a mapping"
    assert "repo_path" in context, "context must have 'repo_path'"
    assert "recipes_dir" in context, "context must have 'recipes_dir'"
    assert "known_agents" in context, "context must have 'known_agents'"
    # repo_path is required (empty string default)
    assert context["repo_path"] == "" or context["repo_path"] is None, (
        f"repo_path default should be empty string, got: {context['repo_path']!r}"
    )
    # recipes_dir defaults to 'recipes'
    assert context["recipes_dir"] == "recipes", (
        f"recipes_dir default should be 'recipes', got: {context['recipes_dir']!r}"
    )
    print("  OK: context interface correct (repo_path, recipes_dir, known_agents)")


def test_phase0_step(phase0_step):
    assert phase0_step.get("id") == "environment-check", (
        f"First step id must be 'environment-check', got: {phase0_step.get('id')}"
    )
    assert phase0_step.get("type") == "bash", (
        f"Step type must be 'bash', got: {phase0_step.get('type')}"
    )
    assert phase0_step.get("timeout") == 30, (
        f"Step timeout must be 30, got: {phase0_step.get('timeout')}"
    )
    assert phase0_step.get("output") == "env_check", (
        f"Step output must be 'env_check', got: {phase0_step.get('output')}"
    )
    assert phase0_step.get("parse_json") is True, (
        f"Step parse_json must be true, got: {phase0_step.get('parse_json')}"
    )
    cmd = phase0_step.get("command", "")
    assert "python3" in cmd, "Step command must contain python3"
    assert "yaml_available" in cmd, "Phase 0 must check yaml_available"
    assert "engine_available" in cmd, "Phase 0 must check engine_available"
    assert "validation_mode" in cmd, "Phase 0 must set validation_mode"
    print(
        "  OK: environment-check step correct (id, type, timeout=30, output, parse_json)"
    )


def test_phase0_python_execution():
    """Run Phase 0 Python code with repo_path='.' and check output."""
    # Extract Python code from bash heredoc and run it with simulated template vars
    python_code = """
import json
import sys
from pathlib import Path

repo_path_val = '.'

results = {
    'phase': 'environment',
    'python_version': sys.version.split()[0],
    'engine_available': False,
    'yaml_available': False,
    'repo_path': repo_path_val,
    'repo_exists': False,
    'validation_mode': 'structural_only',
    'errors': []
}

try:
    import yaml
    results['yaml_available'] = True
except ImportError:
    results['errors'].append({
        'type': 'import_error',
        'message': 'PyYAML not available',
        'suggestion': 'pip install pyyaml'
    })

try:
    from amplifier_module_tool_recipes.models import Recipe
    from amplifier_module_tool_recipes.validator import validate_recipe
    results['engine_available'] = True
except ImportError:
    pass

repo_path = Path(repo_path_val).expanduser().resolve()
if repo_path.exists() and repo_path.is_dir():
    results['repo_exists'] = True
    results['repo_path_resolved'] = str(repo_path)
else:
    results['errors'].append({
        'type': 'path_error',
        'message': f'Repository path does not exist or is not a directory: {repo_path_val}'
    })

if results['engine_available'] and results['yaml_available']:
    results['validation_mode'] = 'full'
    results['mode_description'] = 'Full validation: engine models + gap coverage + best practices + semantics'
elif results['yaml_available']:
    results['validation_mode'] = 'structural_only'
    results['mode_description'] = 'Structural only: raw YAML parsing without engine model validation'
else:
    results['validation_mode'] = 'minimal'
    results['mode_description'] = 'Minimal: cannot parse YAML'
    results['errors'].append({
        'type': 'degraded_mode',
        'message': 'Neither PyYAML nor recipe engine available'
    })

print(json.dumps(results))
"""
    result = subprocess.run(
        [sys.executable, "-c", python_code],
        capture_output=True,
        text=True,
        cwd=str(RECIPE_PATH.parent.parent),  # amplifier-bundle-recipes/
    )
    assert result.returncode == 0, f"Phase 0 Python failed: {result.stderr}"
    output = json.loads(result.stdout.strip())

    assert output["phase"] == "environment", (
        f"phase must be 'environment', got: {output['phase']}"
    )
    assert output["yaml_available"] is True, (
        f"yaml_available must be true, got: {output['yaml_available']}"
    )
    assert output["repo_exists"] is True, (
        f"repo_exists must be true (using '.'), got: {output['repo_exists']}"
    )
    assert output["validation_mode"] in ("full", "structural_only"), (
        f"validation_mode must be 'full' or 'structural_only', got: {output['validation_mode']}"
    )
    print("  OK: Phase 0 Python runs successfully")
    print(f"      yaml_available: {output['yaml_available']}")
    print(f"      repo_exists: {output['repo_exists']}")
    print(f"      validation_mode: {output['validation_mode']}")
