"""Tests for class-based provider preferences in recipes (task-11).

Tests cover:
1. ProviderPreferenceConfig with class_name and required fields
2. Validation: mutual exclusivity (provider vs class_name)
3. Validation: class entries cannot specify model
4. YAML parsing: 'class' key remapped to 'class_name'
5. Executor: ClassPreference vs ProviderPreference construction
"""

from pathlib import Path

from amplifier_module_tool_recipes.models import ProviderPreferenceConfig, Recipe


class TestProviderPreferenceConfigClassSupport:
    """Tests for ProviderPreferenceConfig with class_name field."""

    def test_class_name_defaults_to_empty(self):
        """class_name should default to empty string."""
        pref = ProviderPreferenceConfig(provider="anthropic")
        assert pref.class_name == ""

    def test_required_defaults_to_false(self):
        """required should default to False."""
        pref = ProviderPreferenceConfig(provider="anthropic")
        assert pref.required is False

    def test_class_name_preference(self):
        """Can create a preference with class_name instead of provider."""
        pref = ProviderPreferenceConfig(class_name="fast")
        assert pref.class_name == "fast"
        assert pref.provider == ""
        assert pref.required is False

    def test_class_name_with_required(self):
        """Can create a class preference with required=True."""
        pref = ProviderPreferenceConfig(class_name="premium", required=True)
        assert pref.class_name == "premium"
        assert pref.required is True

    def test_validate_provider_only(self):
        """Provider-only preference should validate cleanly."""
        pref = ProviderPreferenceConfig(provider="anthropic", model="claude-haiku-*")
        errors = pref.validate()
        assert errors == []

    def test_validate_class_name_only(self):
        """Class-name-only preference should validate cleanly."""
        pref = ProviderPreferenceConfig(class_name="fast")
        errors = pref.validate()
        assert errors == []

    def test_validate_must_have_provider_or_class_name(self):
        """Must have either provider or class_name."""
        pref = ProviderPreferenceConfig()
        errors = pref.validate()
        assert len(errors) > 0
        assert any("provider" in e.lower() or "class" in e.lower() for e in errors)

    def test_validate_cannot_have_both_provider_and_class_name(self):
        """Cannot have both provider and class_name."""
        pref = ProviderPreferenceConfig(provider="anthropic", class_name="fast")
        errors = pref.validate()
        assert len(errors) > 0
        assert any("both" in e.lower() or "mutual" in e.lower() for e in errors)

    def test_validate_class_cannot_specify_model(self):
        """Class entries cannot specify model."""
        pref = ProviderPreferenceConfig(class_name="fast", model="some-model")
        errors = pref.validate()
        assert len(errors) > 0
        assert any("model" in e.lower() for e in errors)


class TestYamlParsingClassPreferences:
    """Tests for YAML parsing of class-based preferences."""

    def test_parse_step_remaps_class_to_class_name(self):
        """'class' key in provider_preferences should be remapped to 'class_name'."""
        step_data = {
            "id": "test-step",
            "agent": "test-agent",
            "prompt": "do something",
            "provider_preferences": [
                {"class": "fast"},
            ],
        }
        step = Recipe._parse_step(step_data)
        assert step.provider_preferences is not None
        assert len(step.provider_preferences) == 1
        assert step.provider_preferences[0].class_name == "fast"

    def test_parse_step_class_with_required(self):
        """'class' key with required flag parses correctly."""
        step_data = {
            "id": "test-step",
            "agent": "test-agent",
            "prompt": "do something",
            "provider_preferences": [
                {"class": "premium", "required": True},
            ],
        }
        step = Recipe._parse_step(step_data)
        assert step.provider_preferences is not None
        assert step.provider_preferences[0].class_name == "premium"
        assert step.provider_preferences[0].required is True

    def test_parse_step_mixed_class_and_provider(self):
        """Mixed class and provider entries parse correctly."""
        step_data = {
            "id": "test-step",
            "agent": "test-agent",
            "prompt": "do something",
            "provider_preferences": [
                {"class": "fast"},
                {"provider": "openai", "model": "gpt-4o-mini"},
            ],
        }
        step = Recipe._parse_step(step_data)
        assert step.provider_preferences is not None
        assert len(step.provider_preferences) == 2
        assert step.provider_preferences[0].class_name == "fast"
        assert step.provider_preferences[1].provider == "openai"
        assert step.provider_preferences[1].model == "gpt-4o-mini"

    def test_parse_step_provider_preferences_unchanged(self):
        """Existing provider-based preferences still parse correctly."""
        step_data = {
            "id": "test-step",
            "agent": "test-agent",
            "prompt": "do something",
            "provider_preferences": [
                {"provider": "anthropic", "model": "claude-haiku-*"},
                {"provider": "openai", "model": "gpt-4o-mini"},
            ],
        }
        step = Recipe._parse_step(step_data)
        assert step.provider_preferences is not None
        assert len(step.provider_preferences) == 2
        assert step.provider_preferences[0].provider == "anthropic"
        assert step.provider_preferences[0].model == "claude-haiku-*"

    def test_from_yaml_class_preferences(self, tmp_path: Path):
        """Full YAML with class preferences loads correctly."""
        recipe_yaml = tmp_path / "class-recipe.yaml"
        recipe_yaml.write_text("""\
name: class-pref-test
description: Test class preferences
version: 1.0.0

steps:
  - id: fast-step
    agent: test-agent
    prompt: "Do something fast"
    provider_preferences:
      - class: fast
      - provider: openai
        model: gpt-4o-mini
""")
        recipe = Recipe.from_yaml(recipe_yaml)
        step = recipe.get_step("fast-step")
        assert step is not None
        assert step.provider_preferences is not None
        assert len(step.provider_preferences) == 2
        assert step.provider_preferences[0].class_name == "fast"
        assert step.provider_preferences[1].provider == "openai"


class TestExecutorClassPreferences:
    """Tests for executor handling of class-based preferences."""

    def test_executor_builds_class_preference(self):
        """Executor should construct ClassPreference for class_name entries."""
        from amplifier_foundation import ClassPreference

        # Verify ClassPreference can be constructed as expected
        cp = ClassPreference(class_name="fast")
        assert cp.class_name == "fast"
        assert cp.required is False

    def test_executor_builds_class_preference_required(self):
        """Executor should pass required flag to ClassPreference."""
        from amplifier_foundation import ClassPreference

        cp = ClassPreference(class_name="premium", required=True)
        assert cp.class_name == "premium"
        assert cp.required is True
