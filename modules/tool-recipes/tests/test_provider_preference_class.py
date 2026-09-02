"""Regression tests for the ``class:`` form of ``provider_preferences``.

Defect (recipes-d1r): ``examples/attractor/attractor.yaml`` and
``examples/attractor/factory-iteration.yaml`` ship ``provider_preferences``
entries of the form ``- class: reasoning``. ``ProviderPreferenceConfig`` only
had ``provider``/``model`` fields, so ``Recipe.from_yaml()`` died with a bare::

    TypeError: ProviderPreferenceConfig.__init__() got an unexpected keyword
               argument 'class'

Disposition: ``class:`` is a legitimate, already-documented provider-preference
form (``docs/RECIPE_SCHEMA.md`` describes class entries, the model-class table,
and the rule "each entry must have either a ``class`` key or a ``provider``
key"). It maps onto the engine's existing ``model_role`` concept and is
resolved through the same ``model_role_resolver`` capability.

These tests pin three things:
  1. Both shipped attractor recipes parse and validate cleanly.
  2. An unknown ``provider_preferences`` key raises a readable ValueError
     naming the offending key and the recipe path -- never a raw TypeError.
  3. A ``class:`` entry is actually honored at execution time (resolved, not
     silently dropped).
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from amplifier_foundation.spawn_utils import ProviderPreference
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import (
    ProviderPreferenceConfig,
    Recipe,
    Step,
)

# repo root: modules/tool-recipes/tests/ -> up three
REPO_ROOT = Path(__file__).resolve().parents[3]
ATTRACTOR_RECIPES = [
    REPO_ROOT / "examples" / "attractor" / "attractor.yaml",
    REPO_ROOT / "examples" / "attractor" / "factory-iteration.yaml",
]


# --------------------------------------------------------------------------
# 1. The two shipped example recipes load
# --------------------------------------------------------------------------


class TestShippedAttractorRecipesParse:
    """The two shipped recipes that used to crash at parse now load."""

    @pytest.mark.parametrize("recipe_path", ATTRACTOR_RECIPES, ids=lambda p: p.name)
    def test_recipe_parses(self, recipe_path: Path):
        """Recipe.from_yaml succeeds - no TypeError from the dataclass."""
        assert recipe_path.exists(), f"fixture recipe missing: {recipe_path}"
        recipe = Recipe.from_yaml(recipe_path)
        assert recipe.name

    @pytest.mark.parametrize("recipe_path", ATTRACTOR_RECIPES, ids=lambda p: p.name)
    def test_recipe_validates_clean(self, recipe_path: Path):
        """The parsed recipe reports no validation errors."""
        recipe = Recipe.from_yaml(recipe_path)
        assert recipe.validate() == []

    def test_class_entry_lands_on_model_class_field(self):
        """YAML `class:` is stored on `model_class` (class is a reserved word)."""
        recipe = Recipe.from_yaml(
            REPO_ROOT / "examples" / "attractor" / "factory-iteration.yaml"
        )
        generate = next(s for s in recipe.steps if s.id == "generate")
        assert generate.provider_preferences is not None
        first = generate.provider_preferences[0]
        assert first.model_class == "reasoning"
        assert first.provider == ""
        assert first.model == ""
        # The explicit provider entries after it are untouched.
        assert generate.provider_preferences[1].provider == "anthropic"
        assert generate.provider_preferences[1].model == "claude-sonnet-*"


# --------------------------------------------------------------------------
# 2. Unknown keys produce a readable validation error, never a TypeError
# --------------------------------------------------------------------------


def _write_recipe(tmp_path: Path, pref_yaml: str) -> Path:
    path = tmp_path / "recipe.yaml"
    path.write_text(
        "name: unknown-key-recipe\n"
        "description: fixture\n"
        "version: '1.0.0'\n"
        "steps:\n"
        "  - id: do-work\n"
        "    agent: some-agent\n"
        "    prompt: Do something\n"
        "    provider_preferences:\n" + pref_yaml,
        encoding="utf-8",
    )
    return path


class TestUnknownProviderPreferenceKey:
    """An unrecognized key is a readable ValueError, not a raw TypeError."""

    def test_unknown_key_raises_value_error(self, tmp_path: Path):
        path = _write_recipe(tmp_path, "      - clas: reasoning\n")
        with pytest.raises(ValueError) as exc_info:
            Recipe.from_yaml(path)
        # Explicitly NOT a TypeError - that was the original defect.
        assert not isinstance(exc_info.value, TypeError)

    def test_error_names_the_offending_key(self, tmp_path: Path):
        path = _write_recipe(tmp_path, "      - clas: reasoning\n")
        with pytest.raises(ValueError) as exc_info:
            Recipe.from_yaml(path)
        message = str(exc_info.value)
        assert "clas" in message
        assert "unknown key" in message.lower()

    def test_error_names_valid_keys_and_step_and_index(self, tmp_path: Path):
        path = _write_recipe(
            tmp_path, "      - provider: anthropic\n        rubbish: 1\n"
        )
        with pytest.raises(ValueError) as exc_info:
            Recipe.from_yaml(path)
        message = str(exc_info.value)
        assert "rubbish" in message
        assert "do-work" in message, "error should name the offending step"
        assert "provider_preferences[0]" in message, "error should name the index"
        for valid in ("'class'", "'provider'", "'model'"):
            assert valid in message, f"error should list {valid} as a valid key"

    def test_error_names_the_recipe_path(self, tmp_path: Path):
        path = _write_recipe(tmp_path, "      - clas: reasoning\n")
        with pytest.raises(ValueError) as exc_info:
            Recipe.from_yaml(path)
        assert str(path) in str(exc_info.value)


# --------------------------------------------------------------------------
# 3. ProviderPreferenceConfig.validate() semantics
# --------------------------------------------------------------------------


class TestProviderPreferenceConfigValidation:
    """Entry-level validation: exactly one of `class` or `provider`."""

    def test_class_only_entry_is_valid(self):
        assert ProviderPreferenceConfig(model_class="reasoning").validate() == []

    def test_provider_only_entry_is_valid(self):
        assert ProviderPreferenceConfig(provider="anthropic").validate() == []

    def test_provider_with_model_is_valid(self):
        pref = ProviderPreferenceConfig(provider="openai", model="gpt-4o")
        assert pref.validate() == []

    def test_empty_entry_is_invalid(self):
        errors = ProviderPreferenceConfig().validate()
        assert errors
        assert any("'class'" in e and "'provider'" in e for e in errors)

    def test_class_and_provider_together_is_invalid(self):
        errors = ProviderPreferenceConfig(
            model_class="reasoning", provider="anthropic"
        ).validate()
        assert any("cannot set both 'class' and 'provider'" in e for e in errors)

    def test_class_and_model_together_is_invalid(self):
        errors = ProviderPreferenceConfig(
            model_class="reasoning", model="claude-sonnet-*"
        ).validate()
        assert any("cannot set both 'class' and 'model'" in e for e in errors)

    def test_step_surfaces_entry_errors_with_index(self):
        step = Step(
            id="s1",
            agent="a",
            prompt="p",
            provider_preferences=[
                ProviderPreferenceConfig(provider="anthropic"),
                ProviderPreferenceConfig(),  # neither class nor provider
            ],
        )
        errors = step.validate()
        assert any("provider_preferences[1]" in e for e in errors)


# --------------------------------------------------------------------------
# 4. Execution: a class entry is resolved, not silently dropped
# --------------------------------------------------------------------------


@pytest.fixture
def mock_session_manager():
    manager = MagicMock()
    manager.create_session.return_value = "test-session-id"
    manager.load_state.return_value = {
        "current_step_index": 0,
        "context": {},
        "completed_steps": [],
        "started": "2025-01-01T00:00:00",
    }
    manager.is_cancellation_requested.return_value = False
    manager.is_immediate_cancellation.return_value = False
    return manager


def _make_coordinator(model_role_resolver=None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    spawn_capability = AsyncMock()
    capabilities = {
        "session.spawn": spawn_capability,
        "model_role_resolver": model_role_resolver,
    }
    coordinator.get_capability.return_value = spawn_capability
    coordinator.get_capability.side_effect = lambda name: capabilities.get(name)
    return coordinator


def _recipe_with_class_entry() -> Recipe:
    return Recipe(
        name="class-pref-recipe",
        description="fixture",
        version="1.0.0",
        steps=[
            Step(
                id="do-work",
                agent="some-agent",
                prompt="Do something",
                output="result",
                provider_preferences=[
                    ProviderPreferenceConfig(model_class="reasoning"),
                    ProviderPreferenceConfig(
                        provider="anthropic", model="claude-sonnet-*"
                    ),
                ],
            )
        ],
        context={},
    )


class TestClassEntryExecution:
    """A `class:` entry resolves through the model_role_resolver capability."""

    @pytest.mark.asyncio
    async def test_class_entry_is_resolved_and_prepended(
        self, mock_session_manager, tmp_path
    ):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=[ProviderPreference(provider="openai", model="o3")]
        )
        coordinator = _make_coordinator(model_role_resolver=resolver)
        mock_spawn = coordinator.get_capability("session.spawn")
        mock_spawn.return_value = "step result"

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(_recipe_with_class_entry(), {}, tmp_path)

        resolver.resolve.assert_awaited_once_with("reasoning")
        prefs = mock_spawn.call_args[1]["provider_preferences"]
        assert prefs is not None
        # Resolved class entry first, explicit fallback entry after it.
        assert (prefs[0].provider, prefs[0].model) == ("openai", "o3")
        assert prefs[1].provider == "anthropic"

    @pytest.mark.asyncio
    async def test_no_resolver_skips_class_entry_but_keeps_fallbacks(
        self, mock_session_manager, tmp_path
    ):
        """Without a routing bundle the class entry is skipped, not fatal."""
        coordinator = _make_coordinator(model_role_resolver=None)
        mock_spawn = coordinator.get_capability("session.spawn")
        mock_spawn.return_value = "step result"

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(_recipe_with_class_entry(), {}, tmp_path)

        prefs = mock_spawn.call_args[1]["provider_preferences"]
        assert prefs is not None
        assert len(prefs) == 1
        assert prefs[0].provider == "anthropic"
