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


class TestCompoundSteps:
    """Tests for compound steps: foreach/while with nested 'steps' bodies.

    Regression tests for the 'Step.__init__() got an unexpected keyword
    argument steps' error that occurred when recipes used the natural
    YAML 'steps' key inside foreach or while_condition blocks.
    """

    def test_parse_step_remaps_steps_to_while_steps(self):
        """'steps' in YAML data is remapped to 'while_steps' during parsing."""
        step_data = {
            "id": "loop",
            "foreach": "{{items}}",
            "as": "item",
            "steps": [
                {"id": "inner", "agent": "test", "prompt": "do {{item}}"},
            ],
        }
        step = Recipe._parse_step(step_data)
        assert step.while_steps is not None
        assert len(step.while_steps) == 1
        assert step.while_steps[0]["id"] == "inner"

    def test_parse_step_remaps_steps_for_while_condition(self):
        """'steps' in while_condition blocks also remapped to 'while_steps'."""
        step_data = {
            "id": "converge",
            "while_condition": "true",
            "break_when": "{{done}} == true",
            "max_iterations": 3,
            "steps": [
                {"id": "check", "agent": "test", "prompt": "check"},
                {
                    "id": "fix",
                    "agent": "test",
                    "prompt": "fix",
                    "condition": "{{needs_fix}} == true",
                },
            ],
        }
        step = Recipe._parse_step(step_data)
        assert step.while_steps is not None
        assert len(step.while_steps) == 2

    def test_foreach_compound_step_validates(self):
        """Foreach with nested steps validates without agent/prompt."""
        step = Step(
            id="pipeline",
            foreach="{{tasks}}",
            as_var="task",
            while_steps=[
                {"id": "impl", "agent": "a", "prompt": "p"},
            ],
        )
        errors = step.validate()
        assert errors == [], f"Unexpected validation errors: {errors}"

    def test_while_compound_step_validates(self):
        """While with nested steps validates without agent/prompt."""
        step = Step(
            id="loop",
            while_condition="{{keep_going}}",
            break_when="{{done}} contains APPROVED",
            max_while_iterations=3,
            while_steps=[
                {"id": "review", "agent": "a", "prompt": "p"},
            ],
        )
        errors = step.validate()
        assert errors == [], f"Unexpected validation errors: {errors}"

    def test_while_steps_without_loop_context_is_invalid(self):
        """while_steps without foreach or while_condition should fail."""
        step = Step(
            id="orphan",
            agent="test",
            prompt="test",
            while_steps=[{"id": "sub", "agent": "a", "prompt": "p"}],
        )
        errors = step.validate()
        assert any("requires" in e for e in errors)

    def test_subagent_driven_development_structure_parses(self, temp_dir: Path):
        """The subagent-driven-development recipe structure parses and validates.

        This is the exact structure that triggered the original bug:
        foreach with nested steps, where sub-steps include while_condition
        blocks with their own nested steps.
        """
        recipe_yaml = temp_dir / "sdd.yaml"
        recipe_yaml.write_text("""\
name: "subagent-driven-development"
description: "Test the nested foreach/while structure"
version: "1.0.0"

context:
  plan_path: ""

stages:
  - name: "task-execution"
    steps:
      - id: "load-plan"
        agent: "test:plan-writer"
        prompt: "Load plan from {{plan_path}}"
        output: "tasks"
        parse_json: true

      - id: "per-task-pipeline"
        foreach: "{{tasks}}"
        as: "current_task"
        steps:
          - id: "implement"
            agent: "test:implementer"
            prompt: "Implement {{current_task}}"
            output: "task_implementation"

          - id: "spec-review-loop"
            while_condition: "true"
            break_when: "{{spec_verdict}} contains APPROVED"
            max_iterations: 3
            steps:
              - id: "spec-review"
                agent: "test:spec-reviewer"
                prompt: "Review: {{current_task}}"
                output: "spec_verdict"

              - id: "spec-fix"
                condition: "{{spec_verdict}} contains NEEDS CHANGES"
                agent: "test:implementer"
                prompt: "Fix: {{spec_verdict}}"
                output: "task_implementation"

          - id: "quality-review-loop"
            while_condition: "true"
            break_when: "{{quality_verdict}} contains APPROVED"
            max_iterations: 3
            steps:
              - id: "quality-review"
                agent: "test:quality-reviewer"
                prompt: "Review quality"
                output: "quality_verdict"

              - id: "quality-fix"
                condition: "{{quality_verdict}} contains NEEDS CHANGES"
                agent: "test:implementer"
                prompt: "Fix quality: {{quality_verdict}}"
                output: "task_implementation"

        collect: "completed_tasks"

  - name: "final-review"
    steps:
      - id: "full-review"
        agent: "test:reviewer"
        prompt: "Final review of {{completed_tasks}}"
        output: "final_review"

    approval:
      required: true
      prompt: "Approve the implementation?"
""")
        recipe = Recipe.from_yaml(recipe_yaml)

        # Recipe parsed without error
        assert recipe.name == "subagent-driven-development"
        assert recipe.is_staged
        assert len(recipe.stages) == 2

        # The foreach step has nested sub-steps stored as while_steps
        task_stage = recipe.stages[0]
        pipeline_step = next(s for s in task_stage.steps if s.id == "per-task-pipeline")
        assert pipeline_step.foreach == "{{tasks}}"
        assert pipeline_step.while_steps is not None
        assert len(pipeline_step.while_steps) == 3  # implement, spec-loop, quality-loop

        # Nested while sub-steps also have their own steps
        spec_loop_data = pipeline_step.while_steps[1]
        assert spec_loop_data["id"] == "spec-review-loop"
        assert "steps" in spec_loop_data  # still raw dict, not yet parsed
        assert len(spec_loop_data["steps"]) == 2

        # Full validation passes
        errors = recipe.validate()
        assert errors == [], f"Validation errors: {errors}"
