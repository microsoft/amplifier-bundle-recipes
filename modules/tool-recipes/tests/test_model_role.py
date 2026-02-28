"""Tests for model_role support in recipe Step model and executor."""

import pytest
from amplifier_module_tool_recipes.models import Step


class TestStepModelRole:
    """Tests for model_role field on Step dataclass."""

    def test_step_model_role_defaults_to_none(self):
        """Step model_role defaults to None."""
        step = Step(id="test", agent="test-agent", prompt="Do something")
        assert step.model_role is None

    def test_step_model_role_can_be_set(self):
        """Step model_role can be set to a string value."""
        step = Step(
            id="test", agent="test-agent", prompt="Do something", model_role="fast"
        )
        assert step.model_role == "fast"

    def test_step_model_role_validates_ok_alone(self):
        """Step with model_role and no provider_preferences passes validation."""
        step = Step(
            id="test",
            agent="test-agent",
            prompt="Do something",
            model_role="coding",
        )
        errors = step.validate()
        assert not any("model_role" in e for e in errors)

    def test_step_model_role_mutually_exclusive_with_provider_preferences(self):
        """model_role and provider_preferences are mutually exclusive."""
        from amplifier_module_tool_recipes.models import ProviderPreferenceConfig

        step = Step(
            id="test",
            agent="test-agent",
            prompt="Do something",
            model_role="fast",
            provider_preferences=[
                ProviderPreferenceConfig(provider="anthropic", model="claude-haiku-*")
            ],
        )
        errors = step.validate()
        assert any("model_role" in e and "provider_preferences" in e for e in errors)

    def test_step_model_role_mutually_exclusive_with_provider_model(self):
        """model_role and provider/model legacy fields are mutually exclusive."""
        step = Step(
            id="test",
            agent="test-agent",
            prompt="Do something",
            model_role="fast",
            provider="anthropic",
            model="claude-haiku-*",
        )
        errors = step.validate()
        assert any("model_role" in e for e in errors)

    def test_step_model_role_only_valid_for_agent_steps(self):
        """model_role is only valid for agent steps."""
        step = Step(
            id="test",
            type="bash",
            command="echo hello",
            model_role="fast",
        )
        errors = step.validate()
        assert any("model_role" in e for e in errors)