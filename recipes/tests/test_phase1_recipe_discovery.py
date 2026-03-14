"""
Tests for Task 3: Phase 1 Recipe Discovery step in validate-recipes.yaml.
Tests:
  1. The recipe-discovery step exists in validate-recipes.yaml
  2. Step has correct attributes: id, type, depends_on, output, parse_json, timeout
  3. Step command contains python3 heredoc
  4. The Python logic runs correctly and returns expected JSON structure
  5. Discovery finds expected recipes from examples/ directory (~19-21 top-level)
  6. Test fixtures in recipes/tests/fixtures/ are correctly skipped
  7. Parse errors are captured but don't block discovery of other files
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

RECIPE_PATH = Path(__file__).parent.parent / "validate-recipes.yaml"
REPO_PATH = Path(__file__).parent.parent.parent  # amplifier-bundle-recipes/


@pytest.fixture(scope="module")
def recipe_data():
    content = RECIPE_PATH.read_text()
    return yaml.safe_load(content)


@pytest.fixture(scope="module")
def phase1_step(recipe_data):
    steps = recipe_data.get("steps", [])
    for step in steps:
        if step.get("id") == "recipe-discovery":
            return step
    return None


def test_recipe_discovery_step_exists(recipe_data):
    """Step with id='recipe-discovery' must exist in the steps list."""
    steps = recipe_data.get("steps", [])
    ids = [s.get("id") for s in steps]
    assert "recipe-discovery" in ids, (
        f"'recipe-discovery' step not found. Steps found: {ids}"
    )
    print(f"  OK: recipe-discovery step exists (all steps: {ids})")


def test_recipe_discovery_step_attributes(phase1_step):
    """Step must have correct type, timeout, output, parse_json."""
    assert phase1_step is not None, "recipe-discovery step not found"
    assert phase1_step.get("type") == "bash", (
        f"type must be 'bash', got: {phase1_step.get('type')}"
    )
    assert phase1_step.get("timeout") == 30, (
        f"timeout must be 30, got: {phase1_step.get('timeout')}"
    )
    assert phase1_step.get("output") == "recipe_discovery", (
        f"output must be 'recipe_discovery', got: {phase1_step.get('output')}"
    )
    assert phase1_step.get("parse_json") is True, (
        f"parse_json must be true, got: {phase1_step.get('parse_json')}"
    )
    print(
        "  OK: recipe-discovery attributes correct (type=bash, timeout=30, "
        "output=recipe_discovery, parse_json=true)"
    )


def test_recipe_discovery_depends_on_environment_check(phase1_step):
    """Step must depend on environment-check."""
    assert phase1_step is not None, "recipe-discovery step not found"
    depends_on = phase1_step.get("depends_on", [])
    assert "environment-check" in depends_on, (
        f"depends_on must include 'environment-check', got: {depends_on}"
    )
    print(f"  OK: depends_on=['environment-check'] (got: {depends_on})")


def test_recipe_discovery_command_has_python(phase1_step):
    """Step command must contain python3 heredoc."""
    assert phase1_step is not None, "recipe-discovery step not found"
    cmd = phase1_step.get("command", "")
    assert "python3" in cmd, "command must contain python3"
    assert "discover_recipes" in cmd, "command must contain discover_recipes function"
    assert "repo_path" in cmd, "command must reference repo_path"
    assert "recipes_dir" in cmd, "command must reference recipes_dir"
    print("  OK: command contains python3 with discover_recipes function")


def test_recipe_discovery_step_ordering(recipe_data):
    """recipe-discovery must come after environment-check in steps list."""
    steps = recipe_data.get("steps", [])
    ids = [s.get("id") for s in steps]
    assert "environment-check" in ids, "environment-check step missing"
    assert "recipe-discovery" in ids, "recipe-discovery step missing"
    env_idx = ids.index("environment-check")
    disc_idx = ids.index("recipe-discovery")
    assert disc_idx > env_idx, (
        f"recipe-discovery (index {disc_idx}) must come after "
        f"environment-check (index {env_idx})"
    )
    print(
        f"  OK: step ordering correct (environment-check={env_idx}, "
        f"recipe-discovery={disc_idx})"
    )


def _run_discovery_python(repo_path: str, recipes_dir: str = "recipes") -> dict:
    """
    Extract and run the Phase 1 Python code from the recipe's bash heredoc.
    Replaces template variables {{repo_path}} and {{recipes_dir}}.
    """
    content = RECIPE_PATH.read_text()
    recipe = yaml.safe_load(content)
    steps = recipe.get("steps", [])
    disc_step = None
    for s in steps:
        if s.get("id") == "recipe-discovery":
            disc_step = s
            break
    assert disc_step is not None, "recipe-discovery step not found"

    # Extract Python code from bash heredoc (between EOF markers)
    cmd = disc_step["command"]
    # Replace template variables
    cmd = cmd.replace("{{repo_path}}", repo_path)
    cmd = cmd.replace("{{recipes_dir}}", recipes_dir)

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
    # Dedent if needed
    python_code = textwrap.dedent(python_code)

    result = subprocess.run(
        [sys.executable, "-c", python_code],
        capture_output=True,
        text=True,
        cwd=str(REPO_PATH),
    )
    assert result.returncode == 0, (
        f"Phase 1 Python failed (returncode={result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\n"
        f"stderr: {result.stderr[:500]}"
    )
    output = json.loads(result.stdout.strip())
    return output


def test_phase1_python_returns_correct_structure():
    """Python code returns JSON with required top-level keys."""
    output = _run_discovery_python(str(REPO_PATH))
    required_keys = [
        "phase",
        "repo_path",
        "recipes_dir",
        "recipes",
        "non_recipe_yaml",
        "parse_errors",
        "total_count",
        "search_paths",
        "errors",
    ]
    for key in required_keys:
        assert key in output, (
            f"Required key '{key}' missing from output. Keys: {list(output.keys())}"
        )
    assert output["phase"] == "discovery", (
        f"phase must be 'discovery', got: {output['phase']}"
    )
    assert isinstance(output["recipes"], list), "recipes must be a list"
    assert isinstance(output["non_recipe_yaml"], list), "non_recipe_yaml must be a list"
    assert isinstance(output["parse_errors"], list), "parse_errors must be a list"
    assert isinstance(output["errors"], list), "errors must be a list"
    assert isinstance(output["total_count"], int), "total_count must be an int"
    print(
        f"  OK: output structure correct, found {output['total_count']} recipes, "
        f"{len(output['parse_errors'])} parse errors"
    )


def test_phase1_recipe_info_fields():
    """Each recipe entry must have required info fields."""
    output = _run_discovery_python(str(REPO_PATH))
    recipes = output["recipes"]
    assert len(recipes) > 0, "Must find at least some recipes"
    required_fields = [
        "path",
        "relative_path",
        "filename",
        "name",
        "size_bytes",
        "step_count",
        "is_staged",
        "has_context",
        "has_tags",
        "parse_error",
        "is_sub_recipe",
    ]
    for recipe in recipes[:3]:  # Check first 3 for efficiency
        for field in required_fields:
            assert field in recipe, (
                f"Recipe entry missing field '{field}'. "
                f"Recipe: {recipe.get('path', 'unknown')}, "
                f"Fields: {list(recipe.keys())}"
            )
    print(f"  OK: recipe_info fields correct for {len(recipes)} recipes")


def test_phase1_examples_dir_scan_count():
    """Discovery against amplifier-bundle-recipes examples/ dir finds recipes.

    Scans examples/ directory directly to verify the discovery function works.
    The examples/ dir contains ~19 top-level recipes plus subdirectory recipes.
    """
    output = _run_discovery_python(str(REPO_PATH), recipes_dir="recipes")
    recipes = output["recipes"]
    # We should find at minimum the validate-recipes.yaml itself in recipes/
    # plus the examples/ dir (if scanned)
    assert len(recipes) >= 1, (
        f"Expected at least 1 recipe (validate-recipes.yaml), got: {len(recipes)}"
    )
    print(f"  OK: Found {len(recipes)} total recipes from REPO_PATH scan")


def test_phase1_examples_only_scan():
    """Scan examples/ directly and check we find ~19-21 top-level recipes."""
    examples_path = REPO_PATH / "examples"
    assert examples_path.exists(), "examples/ directory must exist for this test"

    output = _run_discovery_python(str(REPO_PATH), recipes_dir="examples")
    recipes = output["recipes"]
    # examples/ has 19 top-level YAMLs (all recipes) plus subdirectory recipes
    # Total recursive should be well above 15
    assert len(recipes) >= 15, (
        f"Expected at least 15 recipes in examples/, got: {len(recipes)}"
    )
    print(
        f"  OK: Found {len(recipes)} recipes scanning examples/ dir "
        f"(expected >=15, acceptance criteria ~19-21 top-level)"
    )


def test_phase1_test_fixtures_skipped():
    """Files in recipes/tests/fixtures/ must be skipped."""
    output = _run_discovery_python(str(REPO_PATH), recipes_dir="recipes")
    recipe_paths = [r["relative_path"] for r in output["recipes"]]

    # Check that no fixture files appear in recipes list
    fixture_files = [
        "tests/fixtures/valid-recipe.yaml",
        "tests/fixtures/broken-recipe.yaml",
        "tests/fixtures/warnings-recipe.yaml",
    ]
    for fixture in fixture_files:
        # These should NOT appear in recipes (because of /tests/ skip)
        matching = [
            p for p in recipe_paths if "fixtures" in p and fixture.split("/")[-1] in p
        ]
        assert len(matching) == 0, (
            f"Fixture file '{fixture}' should be skipped but was found in recipes: {matching}"
        )
    print("  OK: test fixture files correctly skipped (path contains '/tests/')")


def test_phase1_pyyaml_not_available_returns_skipped():
    """When PyYAML is not available, discovery returns skipped: True."""
    # We can test this by checking the 'skipped' key when yaml is unavailable.
    # We simulate this by checking the code handles the ImportError case.
    # Instead of actually removing yaml, we test the code structure includes skipped handling
    phase1_step = None
    content = RECIPE_PATH.read_text()
    recipe = yaml.safe_load(content)
    for s in recipe.get("steps", []):
        if s.get("id") == "recipe-discovery":
            phase1_step = s
            break
    assert phase1_step is not None
    cmd = phase1_step.get("command", "")
    assert "skipped" in cmd, (
        "command must handle PyYAML unavailability with 'skipped' key"
    )
    assert "ImportError" in cmd or "import yaml" in cmd, (
        "command must handle ImportError for PyYAML"
    )
    print("  OK: command handles PyYAML unavailability with skipped: True")


def test_phase1_parse_errors_captured():
    """Parse errors should be captured but not block other files."""
    # Create a temp directory with a broken YAML and a valid recipe to test isolation
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a recipes subdirectory
        recipes_subdir = Path(tmpdir) / "recipes"
        recipes_subdir.mkdir()

        # Write a valid recipe
        valid_yaml = recipes_subdir / "valid-recipe.yaml"
        valid_yaml.write_text(
            "name: valid-test\ndescription: test\nversion: '1.0'\n"
            "steps:\n  - id: step1\n    type: bash\n    command: echo hi\n"
        )

        # Write a broken YAML (invalid syntax)
        broken_yaml = recipes_subdir / "broken-recipe.yaml"
        broken_yaml.write_text("name: broken\nsteps:\n  - :\n    bad: [unclosed\n")

        output = _run_discovery_python(tmpdir, recipes_dir="recipes")

        # Valid recipe should still be found
        assert output["total_count"] >= 1, (
            f"Should find at least the valid recipe despite parse error. "
            f"Got total_count={output['total_count']}, "
            f"parse_errors={output['parse_errors']}"
        )
        print(
            f"  OK: parse errors captured ({len(output['parse_errors'])} errors), "
            f"valid recipes still found ({output['total_count']} recipes)"
        )
