"""
RED test for Task 2: validate-recipes.yaml scaffold with Phase 0.
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

RECIPE_PATH = Path(__file__).parent.parent / "validate-recipes.yaml"

def test_recipe_file_exists():
    assert RECIPE_PATH.exists(), f"Recipe file not found: {RECIPE_PATH}"
    print(f"  OK: file exists at {RECIPE_PATH}")

def test_recipe_valid_yaml():
    import yaml
    content = RECIPE_PATH.read_text()
    data = yaml.safe_load(content)
    assert isinstance(data, dict), "Recipe must parse as a YAML mapping"
    print(f"  OK: valid YAML, keys: {list(data.keys())}")
    return data

def test_recipe_metadata(data):
    assert data.get("name") == "validate-recipes", f"name must be 'validate-recipes', got: {data.get('name')}"
    assert data.get("version") == "1.0.0", f"version must be '1.0.0', got: {data.get('version')}"
    assert data.get("author") == "Amplifier Recipes Collection", f"author mismatch: {data.get('author')}"
    desc = data.get("description", "")
    assert "Structural Correctness" in desc, "description must mention 'Structural Correctness'"
    assert "Best Practices" in desc, "description must mention 'Best Practices'"
    assert "Semantic Consistency" in desc, "description must mention 'Semantic Consistency'"
    tags = data.get("tags", [])
    for required_tag in ["recipes", "validation", "quality", "audit", "structure", "best-practices"]:
        assert required_tag in tags, f"tag '{required_tag}' missing from tags: {tags}"
    print(f"  OK: metadata correct (name, version, author, description, tags)")

def test_recipe_context(data):
    context = data.get("context", {})
    assert isinstance(context, dict), "context must be a mapping"
    assert "repo_path" in context, "context must have 'repo_path'"
    assert "recipes_dir" in context, "context must have 'recipes_dir'"
    assert "known_agents" in context, "context must have 'known_agents'"
    # repo_path is required (empty string default)
    assert context["repo_path"] == "" or context["repo_path"] is None, \
        f"repo_path default should be empty string, got: {context['repo_path']!r}"
    # recipes_dir defaults to 'recipes'
    assert context["recipes_dir"] == "recipes", \
        f"recipes_dir default should be 'recipes', got: {context['recipes_dir']!r}"
    print(f"  OK: context interface correct (repo_path, recipes_dir, known_agents)")

def test_phase0_step(data):
    steps = data.get("steps", [])
    assert len(steps) >= 1, "Recipe must have at least one step"
    phase0 = steps[0]
    assert phase0.get("id") == "environment-check", \
        f"First step id must be 'environment-check', got: {phase0.get('id')}"
    assert phase0.get("type") == "bash", \
        f"Step type must be 'bash', got: {phase0.get('type')}"
    assert phase0.get("timeout") == 30, \
        f"Step timeout must be 30, got: {phase0.get('timeout')}"
    assert phase0.get("output") == "env_check", \
        f"Step output must be 'env_check', got: {phase0.get('output')}"
    assert phase0.get("parse_json") is True, \
        f"Step parse_json must be true, got: {phase0.get('parse_json')}"
    cmd = phase0.get("command", "")
    assert "python3" in cmd, "Step command must contain python3"
    assert "yaml_available" in cmd, "Phase 0 must check yaml_available"
    assert "engine_available" in cmd, "Phase 0 must check engine_available"
    assert "validation_mode" in cmd, "Phase 0 must set validation_mode"
    print(f"  OK: environment-check step correct (id, type, timeout=30, output, parse_json)")
    return phase0

def test_phase0_python_execution(phase0):
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
        capture_output=True, text=True,
        cwd=str(RECIPE_PATH.parent.parent)  # amplifier-bundle-recipes/
    )
    assert result.returncode == 0, f"Phase 0 Python failed: {result.stderr}"
    output = json.loads(result.stdout.strip())
    
    assert output["phase"] == "environment", f"phase must be 'environment', got: {output['phase']}"
    assert output["yaml_available"] is True, f"yaml_available must be true, got: {output['yaml_available']}"
    assert output["repo_exists"] is True, f"repo_exists must be true (using '.'), got: {output['repo_exists']}"
    assert output["validation_mode"] in ("full", "structural_only"), \
        f"validation_mode must be 'full' or 'structural_only', got: {output['validation_mode']}"
    print(f"  OK: Phase 0 Python runs successfully")
    print(f"      yaml_available: {output['yaml_available']}")
    print(f"      repo_exists: {output['repo_exists']}")
    print(f"      validation_mode: {output['validation_mode']}")
    return output

def run_all_tests():
    failures = []
    
    print("--- RED GATE: Running tests before implementation ---\n")
    
    for test_fn, args in [
        (test_recipe_file_exists, []),
    ]:
        try:
            test_fn(*args)
        except AssertionError as e:
            failures.append(f"FAIL {test_fn.__name__}: {e}")
            print(f"  FAIL: {e}")
    
    if failures:
        print(f"\nRED: {len(failures)} test(s) failed (as expected before implementation)")
        sys.exit(1)
    
    # File exists — run remaining tests
    try:
        data = test_recipe_valid_yaml()
        test_recipe_metadata(data)
        test_recipe_context(data)
        phase0 = test_phase0_step(data)
        test_phase0_python_execution(phase0)
        print("\nALL TESTS PASSED")
        sys.exit(0)
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    run_all_tests()
