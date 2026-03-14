"""
Shared test utilities for validate-recipes test suite.

Provides build_recipe_discovery_for_fixture() — previously duplicated
identically in test_phase2, test_phase3, and test_phase4.
"""

from pathlib import Path

import pytest
import yaml


def build_recipe_discovery_for_fixture(yaml_path: Path) -> dict:
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


@pytest.fixture
def build_recipe_discovery():
    """Factory fixture returning build_recipe_discovery_for_fixture.

    Use in test functions that need to construct a recipe_discovery dict
    for a single fixture file.

    Example::

        def test_something(build_recipe_discovery):
            data = build_recipe_discovery(FIXTURES_PATH / "valid-recipe.yaml")
            assert data["total_count"] == 1
    """
    return build_recipe_discovery_for_fixture
