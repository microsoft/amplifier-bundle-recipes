"""Tests for recipe validation logic."""

from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step
from amplifier_module_tool_recipes.validator import ValidationResult
from amplifier_module_tool_recipes.validator import check_step_dependencies
from amplifier_module_tool_recipes.validator import check_variable_references
from amplifier_module_tool_recipes.validator import extract_variables
from amplifier_module_tool_recipes.validator import validate_recipe


class TestExtractVariables:
    """Tests for extract_variables function."""

    def test_extract_simple_variable(self):
        """Extract single variable from template."""
        variables = extract_variables("Hello {{name}}")
        assert variables == {"name"}

    def test_extract_multiple_variables(self):
        """Extract multiple variables from template."""
        variables = extract_variables("{{greeting}} {{name}}, welcome to {{place}}")
        assert variables == {"greeting", "name", "place"}

    def test_extract_nested_variable(self):
        """Extract nested variable references."""
        variables = extract_variables("Recipe: {{recipe.name}}, Session: {{session.id}}")
        assert variables == {"recipe.name", "session.id"}

    def test_extract_no_variables(self):
        """Return empty set when no variables present."""
        variables = extract_variables("No variables here")
        assert variables == set()

    def test_extract_duplicate_variables(self):
        """Duplicate variables should be returned once."""
        variables = extract_variables("{{name}} and {{name}} again")
        assert variables == {"name"}

    def test_extract_variable_with_underscores(self):
        """Variables with underscores should be extracted."""
        variables = extract_variables("{{first_result}} and {{second_result}}")
        assert variables == {"first_result", "second_result"}


class TestCheckVariableReferences:
    """Tests for check_variable_references function."""

    def test_valid_context_variable(self):
        """Variables defined in context should be valid."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="Use {{input}}")],
            context={"input": "value"},
        )
        errors = check_variable_references(recipe)
        assert errors == []

    def test_valid_reserved_variables(self):
        """Reserved variables (recipe, session, step) should be valid."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="s1",
                    agent="a",
                    prompt="Recipe: {{recipe.name}}, Session: {{session.id}}, Step: {{step.id}}",
                )
            ],
        )
        errors = check_variable_references(recipe)
        assert errors == []

    def test_undefined_variable(self):
        """Undefined variables should produce errors."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="Use {{undefined}}")],
        )
        errors = check_variable_references(recipe)
        assert len(errors) == 1
        assert "undefined" in errors[0].lower()

    def test_step_output_available_to_later_steps(self):
        """Variables from step outputs should be available to later steps."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="First step", output="first_result"),
                Step(id="s2", agent="b", prompt="Use {{first_result}}"),
            ],
        )
        errors = check_variable_references(recipe)
        assert errors == []

    def test_step_output_not_available_to_earlier_steps(self):
        """Variables from step outputs should not be available to earlier steps."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="Use {{second_result}}"),  # not defined yet
                Step(id="s2", agent="b", prompt="Second step", output="second_result"),
            ],
        )
        errors = check_variable_references(recipe)
        assert len(errors) == 1
        assert "second_result" in errors[0]

    def test_unknown_namespace(self):
        """Unknown namespace in nested reference should produce error."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="Use {{unknown.field}}")],
        )
        errors = check_variable_references(recipe)
        assert len(errors) == 1
        assert "unknown" in errors[0].lower()

    def test_known_namespace_from_step_output(self):
        """Known namespace from step output should be valid for nested field references."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="First step", output="structure"),
                Step(id="s2", agent="b", prompt="Use {{structure.provider_file}} and {{structure.provider_class}}"),
            ],
        )
        errors = check_variable_references(recipe)
        assert len(errors) == 0  # Should be valid - namespace exists from step output

    def test_nested_reference_in_context_with_known_namespace(self):
        """Nested field references in step context should work with known namespaces."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="First", output="config"),
                Step(
                    id="s2",
                    recipe="some-recipe.yaml",
                    step_context={"file": "{{config.main_file}}", "class": "{{config.provider_class}}"},
                    depends_on=["s1"],
                ),
            ],
        )
        errors = check_variable_references(recipe)
        assert len(errors) == 0  # Should be valid

    def test_nested_reference_in_recipe_path_with_known_namespace(self):
        """Nested field references in recipe paths should work with known namespaces."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="Get path", output="paths"),
                Step(id="s2", recipe="{{paths.recipe_dir}}/sub-recipe.yaml", agent="b", prompt="Run"),
            ],
        )
        errors = check_variable_references(recipe)
        assert len(errors) == 0  # Should be valid


class TestCheckStepDependencies:
    """Tests for check_step_dependencies function."""

    def test_valid_dependencies(self, multi_step_recipe: Recipe):
        """Valid dependencies should have no errors."""
        errors = check_step_dependencies(multi_step_recipe)
        assert errors == []

    def test_dependency_on_unknown_step(self):
        """Dependency on unknown step should produce error."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="First"),
                Step(id="s2", agent="b", prompt="Second", depends_on=["nonexistent"]),
            ],
        )
        errors = check_step_dependencies(recipe)
        assert any("nonexistent" in e for e in errors)

    def test_dependency_on_later_step(self):
        """Dependency on step that appears later should produce error."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="s1", agent="a", prompt="First", depends_on=["s2"]),
                Step(id="s2", agent="b", prompt="Second"),
            ],
        )
        errors = check_step_dependencies(recipe)
        assert any("later" in e.lower() for e in errors)

    def test_self_dependency(self):
        """Self-dependency should produce error."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="First", depends_on=["s1"])],
        )
        errors = check_step_dependencies(recipe)
        assert any("itself" in e.lower() for e in errors)


class TestValidateRecipe:
    """Tests for validate_recipe function."""

    def test_valid_recipe(self, sample_recipe: Recipe):
        """Valid recipe should pass validation."""
        result = validate_recipe(sample_recipe)
        assert result.is_valid
        assert result.errors == []

    def test_invalid_recipe_structure(self):
        """Recipe with structural errors should fail."""
        recipe = Recipe(name="", description="", version="", steps=[])
        result = validate_recipe(recipe)
        assert not result.is_valid
        assert len(result.errors) > 0

    def test_validation_result_type(self, sample_recipe: Recipe):
        """validate_recipe should return ValidationResult."""
        result = validate_recipe(sample_recipe)
        assert isinstance(result, ValidationResult)

    def test_agent_availability_warning(self, sample_recipe: Recipe, mock_coordinator):
        """Agent not in available_agents should produce warning."""
        # Change agent to one not in mock_coordinator
        sample_recipe.steps[0].agent = "unavailable-agent"
        result = validate_recipe(sample_recipe, mock_coordinator)
        # Should still be valid (warnings don't fail validation)
        assert result.is_valid
        # But should have warning about unavailable agent
        assert any("unavailable-agent" in w for w in result.warnings)

    def test_agent_availability_no_warning_for_available(self, sample_recipe: Recipe, mock_coordinator):
        """Available agent should not produce warning."""
        # sample_recipe has "test-agent" which is in mock_coordinator
        result = validate_recipe(sample_recipe, mock_coordinator)
        assert not any("test-agent" in w for w in result.warnings)

    def test_validation_without_coordinator(self, sample_recipe: Recipe):
        """Validation without coordinator should skip agent checks."""
        result = validate_recipe(sample_recipe, coordinator=None)
        assert result.is_valid

    def test_multi_step_recipe_validation(self, multi_step_recipe: Recipe):
        """Multi-step recipe with proper dependencies should pass."""
        result = validate_recipe(multi_step_recipe)
        assert result.is_valid


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_validation_result_valid(self):
        """ValidationResult with no errors should be valid."""
        result = ValidationResult(is_valid=True, errors=[], warnings=[])
        assert result.is_valid
        assert result.errors == []

    def test_validation_result_invalid(self):
        """ValidationResult with errors should be invalid."""
        result = ValidationResult(is_valid=False, errors=["Some error"], warnings=[])
        assert not result.is_valid
        assert len(result.errors) == 1

    def test_validation_result_with_warnings(self):
        """ValidationResult can have warnings without being invalid."""
        result = ValidationResult(is_valid=True, errors=[], warnings=["Some warning"])
        assert result.is_valid
        assert len(result.warnings) == 1
