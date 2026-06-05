"""Tests for recipe models - Recipe, Step, YAML parsing."""

from pathlib import Path

import pytest
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step


class TestStep:
    """Tests for Step dataclass."""

    def test_step_creation_minimal(self):
        """Step can be created with required fields only."""
        step = Step(id="test", agent="test-agent", prompt="Do something")
        assert step.id == "test"
        assert step.agent == "test-agent"
        assert step.prompt == "Do something"
        assert step.timeout == 600  # default
        assert step.on_error == "fail"  # default
        assert step.depends_on == []  # default

    def test_step_validation_valid(self, sample_step: Step):
        """Valid step should have no errors."""
        errors = sample_step.validate()
        assert errors == []

    def test_step_validation_missing_id(self):
        """Step without id should fail validation."""
        step = Step(id="", agent="test", prompt="test")
        errors = step.validate()
        assert any("id" in e.lower() for e in errors)

    def test_step_validation_missing_agent(self):
        """Step without agent should fail validation."""
        step = Step(id="test", agent="", prompt="test")
        errors = step.validate()
        assert any("agent" in e.lower() for e in errors)

    def test_step_validation_missing_prompt(self):
        """Step without prompt should fail validation."""
        step = Step(id="test", agent="test", prompt="")
        errors = step.validate()
        assert any("prompt" in e.lower() for e in errors)

    def test_step_validation_negative_timeout(self):
        """Step with negative timeout should fail validation."""
        step = Step(id="test", agent="test", prompt="test", timeout=-1)
        errors = step.validate()
        assert any("timeout" in e.lower() for e in errors)

    def test_step_validation_zero_timeout(self):
        """Step with timeout=0 should fail validation (not positive)."""
        step = Step(id="test", agent="test", prompt="test", timeout=0)
        errors = step.validate()
        assert any("timeout" in e.lower() for e in errors)

    def test_step_timeout_template_passes_validate(self):
        """Step with a templated timeout string passes validation (resolved at execution)."""
        step = Step(
            id="test", agent="test", prompt="test", timeout="{{browser_timeout}}"
        )
        errors = step.validate()
        assert not any("timeout" in e.lower() for e in errors)

    def test_step_timeout_literal_int_validates(self):
        """Literal int timeout still validates and is stored as int."""
        step = Step(id="test", agent="test", prompt="test", timeout=1800)
        assert step.timeout == 1800
        errors = step.validate()
        assert not any("timeout" in e.lower() for e in errors)

    def test_step_timeout_non_templated_string_invalid(self):
        """Non-templated, non-numeric string timeout fails validation."""
        step = Step(id="test", agent="test", prompt="test", timeout="notanumber")  # type: ignore[arg-type]
        errors = step.validate()
        assert any("timeout" in e.lower() for e in errors)

    def test_parse_step_plain_numeric_string_coerced(self):
        """_parse_step coerces a plain numeric string timeout to int."""
        step = Recipe._parse_step(
            {"id": "t", "type": "agent", "agent": "a", "prompt": "p", "timeout": "900"}
        )
        assert step.timeout == 900
        assert isinstance(step.timeout, int)

    def test_parse_step_template_string_preserved(self):
        """_parse_step preserves a templated timeout string verbatim."""
        step = Recipe._parse_step(
            {
                "id": "t",
                "type": "agent",
                "agent": "a",
                "prompt": "p",
                "timeout": "{{my_timeout}}",
            }
        )
        assert step.timeout == "{{my_timeout}}"
        assert isinstance(step.timeout, str)

    def test_step_validation_invalid_on_error(self):
        """Step with invalid on_error should fail validation."""
        step = Step(id="test", agent="test", prompt="test", on_error="invalid")
        errors = step.validate()
        assert any("on_error" in e.lower() for e in errors)

    def test_step_validation_valid_on_error_values(self):
        """Step with valid on_error values should pass."""
        for value in ["fail", "continue", "skip_remaining"]:
            step = Step(id="test", agent="test", prompt="test", on_error=value)
            errors = step.validate()
            assert not any("on_error" in e.lower() for e in errors)

    def test_step_validation_invalid_output_name(self):
        """Step with invalid output name should fail validation."""
        step = Step(id="test", agent="test", prompt="test", output="invalid!name")
        errors = step.validate()
        assert any("output" in e.lower() for e in errors)

    def test_step_validation_reserved_output_name(self):
        """Step with reserved output name should fail validation."""
        for reserved in ["recipe", "session", "step"]:
            step = Step(id="test", agent="test", prompt="test", output=reserved)
            errors = step.validate()
            assert any("reserved" in e.lower() for e in errors)

    def test_step_validation_retry_invalid_max_attempts(self):
        """Step with invalid retry max_attempts should fail."""
        step = Step(id="test", agent="test", prompt="test", retry={"max_attempts": 0})
        errors = step.validate()
        assert any("max_attempts" in e.lower() for e in errors)

    def test_step_validation_retry_invalid_backoff(self):
        """Step with invalid retry backoff should fail."""
        step = Step(
            id="test",
            agent="test",
            prompt="test",
            retry={"max_attempts": 3, "backoff": "invalid"},
        )
        errors = step.validate()
        assert any("backoff" in e.lower() for e in errors)


class TestRecipe:
    """Tests for Recipe dataclass."""

    def test_recipe_creation(self, sample_recipe: Recipe):
        """Recipe can be created with valid fields."""
        assert sample_recipe.name == "test-recipe"
        assert sample_recipe.version == "1.0.0"
        assert len(sample_recipe.steps) == 1

    def test_recipe_validation_valid(self, sample_recipe: Recipe):
        """Valid recipe should have no errors."""
        errors = sample_recipe.validate()
        assert errors == []

    def test_recipe_validation_missing_name(self):
        """Recipe without name should fail validation."""
        recipe = Recipe(name="", description="test", version="1.0.0", steps=[])
        errors = recipe.validate()
        assert any("name" in e.lower() for e in errors)

    def test_recipe_validation_missing_description(self):
        """Recipe without description should fail validation."""
        recipe = Recipe(name="test", description="", version="1.0.0", steps=[])
        errors = recipe.validate()
        assert any("description" in e.lower() for e in errors)

    def test_recipe_validation_missing_version(self):
        """Recipe without version should fail validation."""
        recipe = Recipe(name="test", description="test", version="", steps=[])
        errors = recipe.validate()
        assert any("version" in e.lower() for e in errors)

    def test_recipe_validation_invalid_name(self):
        """Recipe with invalid name characters should fail."""
        recipe = Recipe(
            name="test@recipe!", description="test", version="1.0.0", steps=[]
        )
        errors = recipe.validate()
        assert any("name" in e.lower() and "alphanumeric" in e.lower() for e in errors)

    def test_recipe_validation_valid_names(self):
        """Recipe with valid name formats should pass."""
        valid_names = ["test-recipe", "test_recipe", "TestRecipe", "test123"]
        for name in valid_names:
            recipe = Recipe(
                name=name,
                description="test",
                version="1.0.0",
                steps=[Step(id="s1", agent="a", prompt="p")],
            )
            errors = recipe.validate()
            name_errors = [
                e for e in errors if "name" in e.lower() and "alphanumeric" in e.lower()
            ]
            assert not name_errors, f"Name '{name}' should be valid"

    def test_recipe_validation_version_format(self):
        """Recipe version must follow semver format."""
        invalid_versions = ["1.0", "v1.0.0", "1.0.0-beta", "1.a.0"]
        for version in invalid_versions:
            recipe = Recipe(
                name="test",
                description="test",
                version=version,
                steps=[Step(id="s1", agent="a", prompt="p")],
            )
            errors = recipe.validate()
            assert any("version" in e.lower() for e in errors), (
                f"Version '{version}' should be invalid"
            )

    def test_recipe_validation_no_steps(self):
        """Recipe with no steps should fail validation."""
        recipe = Recipe(name="test", description="test", version="1.0.0", steps=[])
        errors = recipe.validate()
        assert any("step" in e.lower() for e in errors)

    def test_recipe_validation_duplicate_step_ids(self):
        """Recipe with duplicate step IDs should fail."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step1", agent="a", prompt="p"),
                Step(id="step1", agent="b", prompt="q"),  # duplicate
            ],
        )
        errors = recipe.validate()
        assert any("duplicate" in e.lower() for e in errors)

    def test_recipe_validation_invalid_depends_on(self):
        """Recipe with invalid depends_on reference should fail."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step1", agent="a", prompt="p"),
                Step(id="step2", agent="b", prompt="q", depends_on=["nonexistent"]),
            ],
        )
        errors = recipe.validate()
        assert any("depends_on" in e.lower() and "unknown" in e.lower() for e in errors)

    def test_recipe_validation_self_dependency(self):
        """Step that depends on itself should fail."""
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(id="step1", agent="a", prompt="p", depends_on=["step1"]),
            ],
        )
        errors = recipe.validate()
        assert any("depend on itself" in e.lower() for e in errors)

    def test_recipe_get_step(self, multi_step_recipe: Recipe):
        """get_step should return correct step by ID."""
        step = multi_step_recipe.get_step("step-2")
        assert step is not None
        assert step.id == "step-2"
        assert step.agent == "agent-b"

    def test_recipe_get_step_not_found(self, sample_recipe: Recipe):
        """get_step should return None for unknown ID."""
        step = sample_recipe.get_step("nonexistent")
        assert step is None


class TestRecipeFromYaml:
    """Tests for Recipe.from_yaml loading."""

    def test_from_yaml_valid_file(self, yaml_recipe_file: Path):
        """Recipe can be loaded from valid YAML file."""
        recipe = Recipe.from_yaml(yaml_recipe_file)
        assert recipe.name == "yaml-test-recipe"
        assert recipe.version == "2.0.0"
        assert len(recipe.steps) == 2
        assert recipe.context["file_path"] == "/path/to/file"

    def test_from_yaml_file_not_found(self, temp_dir: Path):
        """Loading nonexistent file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            Recipe.from_yaml(temp_dir / "nonexistent.yaml")

    def test_from_yaml_invalid_yaml(self, temp_dir: Path):
        """Loading invalid YAML should raise error."""
        import yaml

        bad_file = temp_dir / "bad.yaml"
        bad_file.write_text("not: valid: yaml: [")
        with pytest.raises(yaml.YAMLError):
            Recipe.from_yaml(bad_file)

    def test_from_yaml_not_dict(self, temp_dir: Path):
        """YAML that's not a dict should raise ValueError."""
        bad_file = temp_dir / "list.yaml"
        bad_file.write_text("- item1\n- item2")
        with pytest.raises(ValueError, match="must be a dictionary"):
            Recipe.from_yaml(bad_file)

    def test_from_yaml_steps_not_list(self, temp_dir: Path):
        """YAML where steps is not a list should raise ValueError."""
        bad_file = temp_dir / "bad_steps.yaml"
        bad_file.write_text(
            "name: test\ndescription: test\nversion: 1.0.0\nsteps: not-a-list"
        )
        with pytest.raises(ValueError, match="steps.*must be a list"):
            Recipe.from_yaml(bad_file)

    def test_from_yaml_step_not_dict(self, temp_dir: Path):
        """YAML where step is not a dict should raise ValueError."""
        bad_file = temp_dir / "bad_step.yaml"
        bad_file.write_text(
            "name: test\ndescription: test\nversion: 1.0.0\nsteps:\n  - just-a-string"
        )
        with pytest.raises(ValueError, match="step must be a dictionary"):
            Recipe.from_yaml(bad_file)

    def test_from_yaml_preserves_step_dependencies(self, yaml_recipe_file: Path):
        """depends_on should be preserved when loading from YAML."""
        recipe = Recipe.from_yaml(yaml_recipe_file)
        report_step = recipe.get_step("report")
        assert report_step is not None
        assert "analyze" in report_step.depends_on
