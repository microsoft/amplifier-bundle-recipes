"""Tests for RecipesTool._resolve_path() tilde expansion."""

from pathlib import Path
from unittest.mock import MagicMock

from amplifier_module_tool_recipes import RecipesTool


def _make_tool() -> RecipesTool:
    """Create a minimally-mocked RecipesTool for unit testing _resolve_path."""
    return RecipesTool(
        executor=MagicMock(),
        session_manager=MagicMock(),
        coordinator=MagicMock(),
        config={},
    )


class TestResolvePathExpandUser:
    """_resolve_path must expand ~ in non-@mention paths."""

    def test_tilde_path_is_expanded(self):
        """Path starting with ~ should be expanded to the user's home directory."""
        tool = _make_tool()
        result = tool._resolve_path("~/recipes/my-recipe.yaml")

        assert result is not None
        # The ~ must be resolved to an absolute path, not left as literal ~
        assert "~" not in str(result)
        expected = Path("~/recipes/my-recipe.yaml").expanduser()
        assert result == expected

    def test_absolute_path_unchanged(self):
        """Absolute paths should pass through unchanged."""
        tool = _make_tool()
        result = tool._resolve_path("/tmp/recipe.yaml")

        assert result == Path("/tmp/recipe.yaml")

    def test_relative_path_unchanged(self):
        """Relative paths without ~ should pass through unchanged."""
        tool = _make_tool()
        result = tool._resolve_path("recipes/local.yaml")

        assert result == Path("recipes/local.yaml")
