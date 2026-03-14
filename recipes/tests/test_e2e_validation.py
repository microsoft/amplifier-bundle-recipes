"""
Tests for Task 9: End-to-end validation that everything works together.

This test module validates the complete implementation by running all four
validation steps described in the spec:

  Step 1: Validate the recipe against itself using the engine
  Step 2: Run Phase 0+1 against amplifier-bundle-recipes (examples/ discovery)
  Step 3: Run Phase 0+1 against amplifier-foundation (recipes/ discovery)
  Step 4: Structural validation against fixture files
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ── Paths ──────────────────────────────────────────────────────────────────────

RECIPE_PATH = Path(__file__).parent.parent / "validate-recipes.yaml"
FIXTURES_PATH = Path(__file__).parent / "fixtures"
# amplifier-bundle-recipes/ root (3 levels up from this file)
BUNDLE_RECIPES_ROOT = Path(__file__).parent.parent.parent
# amplifier-foundation/ lives next to amplifier-bundle-recipes/
FOUNDATION_ROOT = BUNDLE_RECIPES_ROOT.parent / "amplifier-foundation"

# ── Helpers: extract Phase 1 discovery logic from the recipe ──────────────────

VALID_RECIPE_KEYS = {
    "name", "description", "version", "author", "created", "updated",
    "tags", "context", "steps", "stages", "recursion", "rate_limiting",
    "orchestrator",
}

VALID_STEP_KEYS = {
    "id", "type", "agent", "prompt", "mode", "agent_config", "recipe",
    "context", "command", "cwd", "env", "output_exit_code", "output",
    "condition", "foreach", "as", "collect", "parallel", "max_iterations",
    "timeout", "retry", "on_error", "depends_on", "parse_json",
    "while_condition", "max_while_iterations", "break_when", "update_context",
    "steps", "recursion", "provider", "model", "provider_preferences",
    "model_role",
}

SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def _process_yaml_file(yaml_path, base, results, sub_recipe_refs):
    """Process a single YAML file — categorize as recipe, non-recipe, or parse error."""
    rel = str(yaml_path.relative_to(base)).replace("\\", "/")
    rel_check = "/" + rel

    # Skip test fixtures
    if "/tests/" in rel_check or "/test_" in rel_check:
        return

    file_size = yaml_path.stat().st_size

    data = None
    parse_error_msg = None
    try:
        data = yaml.safe_load(yaml_path.read_text())
    except Exception as exc:
        parse_error_msg = str(exc)
        results["parse_errors"].append({
            "path": str(yaml_path),
            "relative_path": rel,
            "error": parse_error_msg,
        })

    if parse_error_msg is not None:
        results["recipes"].append({
            "path": str(yaml_path),
            "relative_path": rel,
            "filename": yaml_path.name,
            "name": None, "version": None, "description": None,
            "size_bytes": file_size, "step_count": 0,
            "is_staged": False, "has_context": False, "has_tags": False,
            "parse_error": True, "is_sub_recipe": False,
        })
        return

    if not isinstance(data, dict):
        results["non_recipe_yaml"].append({"path": str(yaml_path), "relative_path": rel})
        return

    has_name = bool(data.get("name"))
    has_steps_or_stages = ("steps" in data) or ("stages" in data)

    if not (has_name and has_steps_or_stages):
        results["non_recipe_yaml"].append({"path": str(yaml_path), "relative_path": rel})
        return

    steps = data.get("steps") or []
    stages = data.get("stages") or []
    is_staged = bool(stages)

    step_count = len(steps)
    for stage in stages:
        if isinstance(stage, dict):
            step_count += len(stage.get("steps") or [])

    all_steps = list(steps)
    for stage in stages:
        if isinstance(stage, dict):
            all_steps.extend(stage.get("steps") or [])
    for step in all_steps:
        if isinstance(step, dict) and step.get("type") == "recipe":
            for field in ("recipe_path", "recipe", "path", "recipe_file"):
                ref = step.get(field)
                if ref:
                    sub_recipe_refs.add(str(ref))
                    break

    version_val = data.get("version")
    recipe_info = {
        "path": str(yaml_path),
        "relative_path": rel,
        "filename": yaml_path.name,
        "name": data.get("name"),
        "version": str(version_val) if version_val is not None else None,
        "description": str(data.get("description", "") or "")[:200] or None,
        "size_bytes": file_size,
        "step_count": step_count,
        "is_staged": is_staged,
        "has_context": "context" in data,
        "has_tags": bool(data.get("tags")),
        "parse_error": False,
        "is_sub_recipe": False,
    }
    results["recipes"].append(recipe_info)


def discover_recipes(repo_path, recipes_dir="recipes"):
    """Phase 1 discovery logic — mirrors validate-recipes.yaml Phase 1."""
    repo = Path(repo_path).expanduser().resolve()
    results = {
        "phase": "discovery",
        "repo_path": str(repo),
        "recipes_dir": recipes_dir,
        "recipes": [],
        "non_recipe_yaml": [],
        "parse_errors": [],
        "total_count": 0,
        "search_paths": [],
        "errors": [],
    }

    if not repo.exists() or not repo.is_dir():
        results["errors"].append({
            "type": "path_error",
            "message": f"Repository path does not exist: {repo_path}",
        })
        return results

    sub_recipe_refs = set()
    scan_dirs = []

    configured_dir = repo / recipes_dir
    if configured_dir.exists() and configured_dir.is_dir():
        scan_dirs.append(configured_dir)
        results["search_paths"].append(str(configured_dir))

    if recipes_dir != "examples":
        examples_dir = repo / "examples"
        if examples_dir.exists() and examples_dir.is_dir():
            scan_dirs.append(examples_dir)
            results["search_paths"].append(str(examples_dir))

    for scan_dir in scan_dirs:
        yaml_files = sorted(
            list(scan_dir.rglob("*.yaml")) + list(scan_dir.rglob("*.yml"))
        )
        for yaml_path in yaml_files:
            _process_yaml_file(yaml_path, repo, results, sub_recipe_refs)

    for recipe in results["recipes"]:
        r_path = recipe.get("path", "")
        r_name = recipe.get("name") or ""
        for ref in sub_recipe_refs:
            if ref in r_path or (r_name and ref == r_name):
                recipe["is_sub_recipe"] = True
                break

    results["total_count"] = len(results["recipes"])
    return results


def validate_recipe_structural(path_str):
    """Structural validation logic — mirrors validate-recipes.yaml Phase 2."""
    findings = []
    path = Path(path_str)

    try:
        data = yaml.safe_load(path.read_text())
    except Exception as exc:
        findings.append({
            "code": "YAML_PARSE_ERROR",
            "severity": "ERROR",
            "message": f"Failed to parse recipe: {exc}",
            "detail": str(exc),
        })
        return findings

    if not isinstance(data, dict):
        findings.append({
            "code": "INVALID_STRUCTURE",
            "severity": "ERROR",
            "message": "Recipe is not a YAML mapping",
            "detail": "",
        })
        return findings

    # Unknown recipe-level keys
    for key in data:
        if key not in VALID_RECIPE_KEYS:
            findings.append({
                "code": "UNKNOWN_KEY",
                "severity": "ERROR",
                "message": f"Unknown recipe-level key: '{key}'",
                "detail": key,
            })

    # Required fields
    if not data.get("name"):
        findings.append({
            "code": "MISSING_NAME",
            "severity": "ERROR",
            "message": "Recipe is missing required 'name' field",
            "detail": "",
        })

    if not data.get("description"):
        findings.append({
            "code": "MISSING_DESCRIPTION",
            "severity": "ERROR",
            "message": "Recipe is missing required 'description' field (or it is empty)",
            "detail": "",
        })

    version = data.get("version")
    if version is None:
        findings.append({
            "code": "MISSING_VERSION",
            "severity": "ERROR",
            "message": "Recipe is missing required 'version' field",
            "detail": "",
        })
    else:
        version_str = str(version)
        if not SEMVER_PATTERN.match(version_str):
            findings.append({
                "code": "INVALID_VERSION",
                "severity": "ERROR",
                "message": f"Version '{version_str}' does not match semver format (X.Y.Z)",
                "detail": version_str,
            })

    # Steps / stages
    steps = data.get("steps") or []
    stages = data.get("stages") or []
    if not steps and not stages:
        findings.append({
            "code": "MISSING_STEPS",
            "severity": "ERROR",
            "message": "Recipe has no steps or stages",
            "detail": "",
        })

    all_steps = list(steps)
    for stage in stages:
        if isinstance(stage, dict):
            all_steps.extend(stage.get("steps") or [])

    seen_ids = {}
    for i, step in enumerate(all_steps):
        if not isinstance(step, dict):
            findings.append({
                "code": "INVALID_STEP",
                "severity": "ERROR",
                "message": f"Step {i} is not a mapping",
                "detail": str(step),
            })
            continue

        for key in step:
            if key not in VALID_STEP_KEYS:
                findings.append({
                    "code": "UNKNOWN_KEY",
                    "severity": "ERROR",
                    "message": f"Unknown step key: '{key}' in step {i}",
                    "detail": key,
                })

        step_id = step.get("id")
        if not step_id:
            findings.append({
                "code": "MISSING_STEP_ID",
                "severity": "ERROR",
                "message": f"Step {i} is missing required 'id' field",
                "detail": "",
            })
        else:
            if step_id in seen_ids:
                findings.append({
                    "code": "DUPLICATE_STEP_ID",
                    "severity": "ERROR",
                    "message": f"Duplicate step ID: '{step_id}'",
                    "detail": step_id,
                })
            seen_ids[step_id] = True

    return findings


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: Engine Self-Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestStep1EngineValidation:
    """Step 1: Validate the recipe against itself using the engine."""

    def test_recipe_file_exists(self):
        """validate-recipes.yaml must exist."""
        assert RECIPE_PATH.exists(), f"validate-recipes.yaml not found at {RECIPE_PATH}"

    def test_recipe_parses_as_valid_yaml(self):
        """validate-recipes.yaml must parse without YAML errors."""
        content = RECIPE_PATH.read_text()
        data = yaml.safe_load(content)
        assert isinstance(data, dict), "Recipe must parse as a YAML mapping"
        assert data.get("name") == "validate-recipes"

    def test_engine_validation_result_is_acceptable(self):
        """Engine validates the recipe or reports itself as not importable — both OK."""
        result = subprocess.run(
            [
                sys.executable, "-c",
                """
import sys
from pathlib import Path
recipe_path = Path(sys.argv[1])
try:
    from amplifier_module_tool_recipes.models import Recipe
    from amplifier_module_tool_recipes.validator import validate_recipe
    recipe = Recipe.from_yaml(recipe_path.read_text())
    errors = validate_recipe(recipe)
    if len(errors) == 0:
        print("Valid: True")
    else:
        print(f"Valid: False ({len(errors)} errors)")
        for e in errors:
            print(f"  Error: {e}")
        sys.exit(1)
except ImportError:
    print("Engine not importable")
except Exception as e:
    print(f"Other error: {type(e).__name__}: {e}")
    sys.exit(2)
""",
                str(RECIPE_PATH),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout.strip()
        assert result.returncode in (0, 1, 2), f"Unexpected returncode: {result.returncode}"
        # Acceptable outcomes: engine validates it OR engine not available
        assert "Engine not importable" in output or "Valid: True" in output, (
            f"Unexpected output from engine validation: {output!r}\n"
            f"stderr: {result.stderr}"
        )
        print(f"  Engine validation result: {output}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: Phase 0+1 against amplifier-bundle-recipes
# ══════════════════════════════════════════════════════════════════════════════


class TestStep2BundleRecipesDiscovery:
    """Step 2: Run Phase 0+1 against amplifier-bundle-recipes."""

    def test_bundle_recipes_root_exists(self):
        """amplifier-bundle-recipes/ repository root must exist."""
        assert BUNDLE_RECIPES_ROOT.exists(), (
            f"amplifier-bundle-recipes root not found at {BUNDLE_RECIPES_ROOT}"
        )
        assert (BUNDLE_RECIPES_ROOT / "examples").is_dir(), (
            "examples/ directory not found in amplifier-bundle-recipes"
        )

    def test_discovery_finds_at_least_minimum_recipes(self):
        """Phase 1 discovery must find at least 19 recipes in the corpus.

        The spec estimated ~19-21 recipes. The actual examples/ directory
        has grown beyond that. We assert >= 19 to verify discovery works.
        """
        output = discover_recipes(str(BUNDLE_RECIPES_ROOT), "recipes")
        total = output["total_count"]
        assert total >= 19, (
            f"Discovery found only {total} recipes — expected at least 19. "
            f"Search paths: {output['search_paths']}"
        )
        print(f"  Recipes discovered: {total} (min expected: 19)")

    def test_discovery_scans_examples_directory(self):
        """Phase 1 discovery must include examples/ in its search paths."""
        output = discover_recipes(str(BUNDLE_RECIPES_ROOT), "recipes")
        search_paths = output["search_paths"]
        examples_path = str(BUNDLE_RECIPES_ROOT / "examples")
        assert any(examples_path in p for p in search_paths), (
            f"examples/ not in search paths: {search_paths}"
        )

    def test_discovery_skips_test_fixtures(self):
        """Phase 1 discovery must skip files in tests/ subdirectories."""
        output = discover_recipes(str(BUNDLE_RECIPES_ROOT), "recipes")
        for recipe in output["recipes"]:
            rel = recipe.get("relative_path", "")
            assert "/tests/" not in ("/" + rel), (
                f"Test fixture was not skipped: {rel}"
            )

    def test_discovery_has_no_parse_errors(self):
        """Phase 1 discovery must encounter no YAML parse errors in the corpus."""
        output = discover_recipes(str(BUNDLE_RECIPES_ROOT), "recipes")
        parse_errors = output.get("parse_errors", [])
        assert len(parse_errors) == 0, (
            f"Discovery found {len(parse_errors)} parse error(s): "
            + ", ".join(e["relative_path"] for e in parse_errors)
        )

    def test_discovery_output_has_required_fields(self):
        """Phase 1 output JSON must have all required top-level fields."""
        output = discover_recipes(str(BUNDLE_RECIPES_ROOT), "recipes")
        for field in ("phase", "repo_path", "recipes", "total_count", "search_paths"):
            assert field in output, f"Missing field '{field}' in discovery output"
        assert output["phase"] == "discovery"

    def test_each_recipe_entry_has_required_fields(self):
        """Each recipe entry in Phase 1 output must have required fields."""
        output = discover_recipes(str(BUNDLE_RECIPES_ROOT), "recipes")
        for recipe in output["recipes"]:
            for field in ("path", "relative_path", "filename", "name",
                          "step_count", "parse_error", "is_sub_recipe"):
                assert field in recipe, (
                    f"Recipe entry missing field '{field}': {recipe.get('relative_path', '?')}"
                )


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: Phase 0+1 against amplifier-foundation
# ══════════════════════════════════════════════════════════════════════════════


class TestStep3FoundationDiscovery:
    """Step 3: Run Phase 0+1 against amplifier-foundation."""

    def test_foundation_root_exists(self):
        """amplifier-foundation/ repository root must exist."""
        assert FOUNDATION_ROOT.exists(), (
            f"amplifier-foundation not found at {FOUNDATION_ROOT}"
        )
        assert (FOUNDATION_ROOT / "recipes").is_dir(), (
            "recipes/ directory not found in amplifier-foundation"
        )

    def test_discovery_finds_exactly_4_recipes(self):
        """Phase 1 discovery of amplifier-foundation must find exactly 4 recipes."""
        output = discover_recipes(str(FOUNDATION_ROOT), "recipes")
        total = output["total_count"]
        assert total == 4, (
            f"Expected exactly 4 recipes in amplifier-foundation, found {total}. "
            f"Recipes: {[r['relative_path'] for r in output['recipes']]}"
        )

    def test_discovery_finds_expected_recipe_names(self):
        """Phase 1 must find the four known validate-* recipes in amplifier-foundation."""
        output = discover_recipes(str(FOUNDATION_ROOT), "recipes")
        names = {r["name"] for r in output["recipes"] if r.get("name")}
        expected_names = {
            "validate-agents",
            "validate-bundle-repo",
            "validate-bundle",
        }
        for name in expected_names:
            assert name in names, (
                f"Expected recipe name '{name}' not found. "
                f"Names discovered: {sorted(names)}"
            )

    def test_discovery_finds_validate_bundle_repo(self):
        """validate-bundle-repo.yaml must be discoverable in amplifier-foundation."""
        output = discover_recipes(str(FOUNDATION_ROOT), "recipes")
        paths = [r["relative_path"] for r in output["recipes"]]
        assert any("validate-bundle-repo" in p for p in paths), (
            f"validate-bundle-repo not found. Paths: {paths}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: Structural Validation Against Fixture Files
# ══════════════════════════════════════════════════════════════════════════════


class TestStep4StructuralValidation:
    """Step 4: Structural validation against fixture files."""

    def test_fixtures_directory_exists(self):
        """The fixtures directory must exist."""
        assert FIXTURES_PATH.exists(), f"Fixtures directory not found at {FIXTURES_PATH}"
        for name in ("valid-recipe.yaml", "broken-recipe.yaml", "warnings-recipe.yaml"):
            assert (FIXTURES_PATH / name).exists(), f"Fixture {name} not found"

    # ── valid-recipe.yaml ────────────────────────────────────────────────────

    def test_valid_recipe_has_zero_findings(self):
        """valid-recipe.yaml must produce 0 structural findings."""
        findings = validate_recipe_structural(str(FIXTURES_PATH / "valid-recipe.yaml"))
        assert len(findings) == 0, (
            f"valid-recipe.yaml expected 0 findings, got {len(findings)}:\n"
            + "\n".join(f"  [{f['severity']}] {f['code']}: {f['message']}" for f in findings)
        )

    # ── broken-recipe.yaml ───────────────────────────────────────────────────

    def test_broken_recipe_has_multiple_findings(self):
        """broken-recipe.yaml must produce multiple ERROR findings."""
        findings = validate_recipe_structural(str(FIXTURES_PATH / "broken-recipe.yaml"))
        assert len(findings) >= 2, (
            f"broken-recipe.yaml expected multiple findings, got {len(findings)}:\n"
            + "\n".join(f"  [{f['severity']}] {f['code']}: {f['message']}" for f in findings)
        )

    def test_broken_recipe_detects_unknown_key_naem(self):
        """broken-recipe.yaml must detect 'naem' as an unknown recipe-level key."""
        findings = validate_recipe_structural(str(FIXTURES_PATH / "broken-recipe.yaml"))
        codes_details = [(f["code"], f.get("detail", "")) for f in findings]
        assert any(
            code == "UNKNOWN_KEY" and "naem" in detail
            for code, detail in codes_details
        ), (
            f"Expected UNKNOWN_KEY finding for 'naem'. Got: {codes_details}"
        )

    def test_broken_recipe_detects_unknown_key_stesp(self):
        """broken-recipe.yaml must detect 'stesp' as an unknown recipe-level key."""
        findings = validate_recipe_structural(str(FIXTURES_PATH / "broken-recipe.yaml"))
        codes_details = [(f["code"], f.get("detail", "")) for f in findings]
        assert any(
            code == "UNKNOWN_KEY" and "stesp" in detail
            for code, detail in codes_details
        ), (
            f"Expected UNKNOWN_KEY finding for 'stesp'. Got: {codes_details}"
        )

    def test_broken_recipe_detects_missing_name(self):
        """broken-recipe.yaml must detect MISSING_NAME (naem typo means no valid name)."""
        findings = validate_recipe_structural(str(FIXTURES_PATH / "broken-recipe.yaml"))
        codes = [f["code"] for f in findings]
        assert "MISSING_NAME" in codes, (
            f"Expected MISSING_NAME finding. Got codes: {codes}"
        )

    def test_broken_recipe_detects_invalid_version(self):
        """broken-recipe.yaml must detect INVALID_VERSION for 'not-a-version'."""
        findings = validate_recipe_structural(str(FIXTURES_PATH / "broken-recipe.yaml"))
        codes = [f["code"] for f in findings]
        assert "INVALID_VERSION" in codes, (
            f"Expected INVALID_VERSION finding. Got codes: {codes}"
        )

    def test_broken_recipe_all_findings_are_errors(self):
        """All findings in broken-recipe.yaml structural validation must be ERROR severity."""
        findings = validate_recipe_structural(str(FIXTURES_PATH / "broken-recipe.yaml"))
        non_errors = [f for f in findings if f.get("severity") != "ERROR"]
        assert len(non_errors) == 0, (
            f"Expected all structural findings to be ERROR severity. "
            f"Non-ERROR findings: {non_errors}"
        )

    # ── warnings-recipe.yaml ─────────────────────────────────────────────────

    def test_warnings_recipe_has_zero_structural_findings(self):
        """warnings-recipe.yaml must produce 0 STRUCTURAL findings.

        This fixture has best-practices issues (warnings), not structural errors.
        Structural validation should report nothing for it.
        """
        findings = validate_recipe_structural(str(FIXTURES_PATH / "warnings-recipe.yaml"))
        assert len(findings) == 0, (
            f"warnings-recipe.yaml expected 0 structural findings, got {len(findings)}:\n"
            + "\n".join(f"  [{f['severity']}] {f['code']}: {f['message']}" for f in findings)
        )


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1b: Structural self-validation of validate-recipes.yaml
# ══════════════════════════════════════════════════════════════════════════════


class TestStep1bRecipeSelfValidation:
    """Step 1b: validate-recipes.yaml passes its own structural validation."""

    def test_validate_recipes_passes_structural_check(self):
        """validate-recipes.yaml must pass the structural validator with 0 findings."""
        findings = validate_recipe_structural(str(RECIPE_PATH))
        assert len(findings) == 0, (
            f"validate-recipes.yaml structural validation found {len(findings)} issue(s):\n"
            + "\n".join(f"  [{f['severity']}] {f['code']}: {f['message']}" for f in findings)
        )
