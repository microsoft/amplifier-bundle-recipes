"""Tests for recipe executor - variable substitution."""

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor


class MockSessionManager:
    """Minimal mock for SessionManager - only substitute_variables is tested."""

    def __init__(self):
        """Initialize mock."""
        self.calls = []


class MockCoordinator:
    """Minimal mock for Coordinator - only substitute_variables is tested."""

    def __init__(self):
        """Initialize mock."""
        self.calls = []


class TestSubstituteVariables:
    """Tests for variable substitution in executor."""

    @pytest.fixture
    def executor(self) -> RecipeExecutor:
        """Create executor with mock dependencies."""
        return RecipeExecutor(MockCoordinator(), MockSessionManager())  # type: ignore[arg-type]

    def test_substitute_simple_variable(self, executor: RecipeExecutor):
        """Simple variable substitution."""
        template = "Hello {{name}}"
        context = {"name": "World"}
        result = executor.substitute_variables(template, context)
        assert result == "Hello World"

    def test_substitute_multiple_variables(self, executor: RecipeExecutor):
        """Multiple variables in same template."""
        template = "{{greeting}} {{name}}, welcome to {{place}}"
        context = {"greeting": "Hello", "name": "User", "place": "Amplifier"}
        result = executor.substitute_variables(template, context)
        assert result == "Hello User, welcome to Amplifier"

    def test_substitute_nested_variable(self, executor: RecipeExecutor):
        """Nested variable reference (e.g., recipe.name)."""
        template = "Recipe: {{recipe.name}}, Version: {{recipe.version}}"
        context = {"recipe": {"name": "test-recipe", "version": "1.0.0"}}
        result = executor.substitute_variables(template, context)
        assert result == "Recipe: test-recipe, Version: 1.0.0"

    def test_substitute_deep_nested_variable(self, executor: RecipeExecutor):
        """Deeper nested variable reference."""
        template = "Session ID: {{session.id}}"
        context = {"session": {"id": "abc123", "started": "2024-01-01"}}
        result = executor.substitute_variables(template, context)
        assert result == "Session ID: abc123"

    def test_substitute_no_variables(self, executor: RecipeExecutor):
        """Template without variables should be unchanged."""
        template = "No variables here"
        context = {"name": "unused"}
        result = executor.substitute_variables(template, context)
        assert result == "No variables here"

    def test_substitute_undefined_variable_raises(self, executor: RecipeExecutor):
        """Undefined variable should raise ValueError."""
        template = "Hello {{undefined}}"
        context = {"name": "World"}
        with pytest.raises(ValueError) as exc_info:
            executor.substitute_variables(template, context)
        assert "undefined" in str(exc_info.value).lower()
        assert "Undefined variable" in str(exc_info.value)

    def test_substitute_undefined_nested_variable_raises(self, executor: RecipeExecutor):
        """Undefined nested variable should raise ValueError."""
        template = "Value: {{data.unknown}}"
        context = {"data": {"known": "value"}}
        with pytest.raises(ValueError) as exc_info:
            executor.substitute_variables(template, context)
        assert "data.unknown" in str(exc_info.value)

    def test_substitute_variable_with_underscore(self, executor: RecipeExecutor):
        """Variables with underscores should work."""
        template = "Result: {{first_result}}"
        context = {"first_result": "success"}
        result = executor.substitute_variables(template, context)
        assert result == "Result: success"

    def test_substitute_numeric_value(self, executor: RecipeExecutor):
        """Numeric values should be converted to string."""
        template = "Count: {{count}}"
        context = {"count": 42}
        result = executor.substitute_variables(template, context)
        assert result == "Count: 42"

    def test_substitute_preserves_surrounding_text(self, executor: RecipeExecutor):
        """Text around variables should be preserved."""
        template = "Before {{var}} after"
        context = {"var": "middle"}
        result = executor.substitute_variables(template, context)
        assert result == "Before middle after"

    def test_substitute_same_variable_multiple_times(self, executor: RecipeExecutor):
        """Same variable used multiple times should be substituted everywhere."""
        template = "{{name}} and {{name}} again"
        context = {"name": "test"}
        result = executor.substitute_variables(template, context)
        assert result == "test and test again"

    def test_substitute_empty_string_value(self, executor: RecipeExecutor):
        """Empty string values should substitute correctly."""
        template = "Value: {{value}}"
        context = {"value": ""}
        result = executor.substitute_variables(template, context)
        assert result == "Value: "

    def test_substitute_multiline_template(self, executor: RecipeExecutor):
        """Multiline templates should work."""
        template = """Line 1: {{var1}}
Line 2: {{var2}}
Line 3: {{var3}}"""
        context = {"var1": "a", "var2": "b", "var3": "c"}
        result = executor.substitute_variables(template, context)
        expected = """Line 1: a
Line 2: b
Line 3: c"""
        assert result == expected

    def test_substitute_list_value_converts_to_string(self, executor: RecipeExecutor):
        """List values should be converted to JSON string representation."""
        template = "Items: {{items}}"
        context = {"items": ["a", "b", "c"]}
        result = executor.substitute_variables(template, context)
        # Implementation uses json.dumps for valid JSON output (double quotes)
        assert result == 'Items: ["a", "b", "c"]'

    def test_substitute_dict_value_converts_to_string(self, executor: RecipeExecutor):
        """Dict values should be converted to JSON string representation."""
        template = "Data: {{data}}"
        context = {"data": {"key": "value"}}
        result = executor.substitute_variables(template, context)
        # Implementation uses json.dumps for valid JSON output (double quotes)
        assert result == 'Data: {"key": "value"}'

    def test_error_message_includes_available_variables(self, executor: RecipeExecutor):
        """Error message should list available variables."""
        template = "Hello {{missing}}"
        context = {"name": "World", "greeting": "Hi"}
        with pytest.raises(ValueError) as exc_info:
            executor.substitute_variables(template, context)
        error_msg = str(exc_info.value)
        assert "Available variables" in error_msg
        # Should list both available variables
        assert "name" in error_msg or "greeting" in error_msg
