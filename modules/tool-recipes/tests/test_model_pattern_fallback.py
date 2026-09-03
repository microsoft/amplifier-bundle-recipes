"""A model pattern that matches nothing must fall back, not 404.

The documented contract (``context/recipe-instructions.md``,
``docs/BEST_PRACTICES.md``): "if model pattern has no matches -> uses
provider's default model". ``resolve_model_pattern`` on its own returns an
unmatched glob unchanged, and handing a provider a model literally named
``claude-haiku-*`` gets a ``not_found_error`` -- so the executor closes that
gap. These tests pin both halves of it: the fallback fires on positive
evidence of no match, and does NOT fire when the provider catalogue simply
could not be read.
"""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from amplifier_foundation.spawn_utils import ModelResolutionResult
from amplifier_module_tool_recipes.executor import (
    RecipeExecutor,
    _model_after_pattern_resolution,
)
from amplifier_module_tool_recipes.models import ProviderPreferenceConfig, Recipe, Step

REPO_ROOT = Path(__file__).resolve().parents[3]


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


def _coordinator_with_catalog(models: list[str] | None) -> MagicMock:
    """A coordinator whose provider offers ``models``.

    ``None`` means the provider cannot be enumerated at all -- the shape the
    legacy-compat harness deliberately uses, and the shape a host with no
    ``list_models`` support presents.
    """
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None

    spawn_capability = AsyncMock()
    spawn_capability.return_value = "step result"
    coordinator.get_capability.return_value = spawn_capability
    coordinator.get_capability.side_effect = lambda name: (
        spawn_capability if name == "session.spawn" else None
    )

    if models is None:
        coordinator.get.side_effect = lambda key: None
    else:
        provider = MagicMock()
        provider.list_models = AsyncMock(return_value=list(models))
        coordinator.get.side_effect = lambda key: (
            {"provider-anthropic": provider} if key == "providers" else None
        )
    return coordinator


def _one_step_recipe(step: Step) -> Recipe:
    return Recipe(
        name="model-pattern-recipe",
        description="Pin model pattern fallback behaviour",
        version="1.0.0",
        steps=[step],
        context={},
    )


class TestModelAfterPatternResolution:
    """The pure decision function, one row per resolution shape."""

    def test_non_pattern_passes_through(self):
        """A bare model id was never a pattern; it is the author's decision."""
        result = ModelResolutionResult(
            resolved_model="claude-haiku",
            pattern=None,
            available_models=None,
            matched_models=None,
        )
        assert _model_after_pattern_resolution(result, "anthropic") == "claude-haiku"

    def test_matched_pattern_uses_the_match(self):
        result = ModelResolutionResult(
            resolved_model="claude-haiku-4-5-20251001",
            pattern="claude-haiku-*",
            available_models=["claude-haiku-4-5-20251001", "claude-sonnet-4-5"],
            matched_models=["claude-haiku-4-5-20251001"],
        )
        assert (
            _model_after_pattern_resolution(result, "anthropic")
            == "claude-haiku-4-5-20251001"
        )

    def test_pattern_matching_nothing_falls_back_to_provider_default(self):
        """The documented fallback: empty model == "use the provider's default"."""
        result = ModelResolutionResult(
            resolved_model="claude-haiku-*",
            pattern="claude-haiku-*",
            available_models=["claude-sonnet-4-5", "claude-opus-4-1"],
            matched_models=[],
        )
        assert _model_after_pattern_resolution(result, "anthropic") == ""

    def test_fallback_is_logged_as_a_warning(self, caplog):
        result = ModelResolutionResult(
            resolved_model="claude-haiku-*",
            pattern="claude-haiku-*",
            available_models=["claude-sonnet-4-5"],
            matched_models=[],
        )
        with caplog.at_level(
            logging.WARNING, logger="amplifier_module_tool_recipes.executor"
        ):
            _model_after_pattern_resolution(result, "anthropic")
        messages = [record.getMessage() for record in caplog.records]
        assert any("claude-haiku-*" in message for message in messages), (
            f"the fallback must say which pattern it dropped; got {messages!r}"
        )

    def test_unreadable_catalog_leaves_the_pattern_alone(self):
        """No catalogue is not evidence of no match.

        The host may still resolve the glob against the instance it finally
        picks (see ``pin_preferences_to_instances``); discarding the author's
        pattern here would silently downgrade a step that resolves fine.
        """
        for available in (None, []):
            result = ModelResolutionResult(
                resolved_model="claude-haiku-*",
                pattern="claude-haiku-*",
                available_models=available,
                matched_models=[],
            )
            assert (
                _model_after_pattern_resolution(result, "anthropic")
                == "claude-haiku-*"
            ), f"available_models={available!r} must not trigger the fallback"

    def test_resolved_model_none_never_leaks_as_a_string(self):
        """Newer ``amplifier-foundation`` signals "unresolved" with ``None``.

        Two shapes are in the wild for the same fact: older builds hand the
        pattern back unchanged, newer ones return ``resolved_model=None``. A
        ``None`` must become the empty "use the provider's default" model --
        never the literal string ``"None"``, which would be a model id no
        provider has.
        """
        for available, matched in (([], []), (["claude-sonnet-4-5"], [])):
            result = ModelResolutionResult(
                resolved_model=None,  # type: ignore[arg-type]
                pattern="claude-haiku-*",
                available_models=available,
                matched_models=matched,
            )
            assert _model_after_pattern_resolution(result, "anthropic") == ""


class TestExecutorHonoursTheFallback:
    """End to end through ``execute_step``: what actually reaches the spawn."""

    @pytest.mark.asyncio
    async def test_legacy_provider_model_pattern_with_no_match(
        self, mock_session_manager, temp_dir
    ):
        coordinator = _coordinator_with_catalog(
            ["claude-sonnet-4-5-20250929", "claude-opus-4-1-20250805"]
        )
        mock_spawn = coordinator.get_capability("session.spawn")

        recipe = _one_step_recipe(
            Step(
                id="assess-severity",
                agent="foundation:zen-architect",
                prompt="Classify this",
                output="severity",
                provider="anthropic",
                model="claude-haiku-*",
            )
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        prefs = mock_spawn.call_args[1]["provider_preferences"]
        assert prefs[0].provider == "anthropic"
        assert prefs[0].model == "", (
            "an unmatched pattern must become the provider's default model, "
            "not ride through as a 404-guaranteed model id"
        )

    @pytest.mark.asyncio
    async def test_legacy_provider_model_pattern_that_matches(
        self, mock_session_manager, temp_dir
    ):
        coordinator = _coordinator_with_catalog(
            ["claude-haiku-4-5-20251001", "claude-haiku-3-5-20241022"]
        )
        mock_spawn = coordinator.get_capability("session.spawn")

        recipe = _one_step_recipe(
            Step(
                id="assess-severity",
                agent="foundation:zen-architect",
                prompt="Classify this",
                output="severity",
                provider="anthropic",
                model="claude-haiku-*",
            )
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        prefs = mock_spawn.call_args[1]["provider_preferences"]
        assert prefs[0].model == "claude-haiku-4-5-20251001", (
            "newest match wins; the fallback must not fire when a match exists"
        )

    @pytest.mark.asyncio
    async def test_step_provider_preferences_pattern_with_no_match(
        self, mock_session_manager, temp_dir
    ):
        """The same rule on the modern ``provider_preferences`` path."""
        coordinator = _coordinator_with_catalog(["claude-sonnet-4-5-20250929"])
        mock_spawn = coordinator.get_capability("session.spawn")

        recipe = _one_step_recipe(
            Step(
                id="assess-severity",
                agent="foundation:zen-architect",
                prompt="Classify this",
                output="severity",
                provider_preferences=[
                    ProviderPreferenceConfig(
                        provider="anthropic", model="claude-haiku-*"
                    ),
                ],
            )
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        prefs = mock_spawn.call_args[1]["provider_preferences"]
        assert prefs[0].provider == "anthropic"
        assert prefs[0].model == ""


class TestShippedExamplesPinResolvableModels:
    """The defect this file exists for started as a bad pin in a shipped example.

    ``examples/code-review-recipe.yaml`` pinned ``claude-haiku`` -- neither a
    glob nor a real Anthropic model id -- which 404'd the step and took
    ``examples/comprehensive-review.yaml`` (the only shipped example of recipe
    composition) down with it. A model id cannot be checked offline without a
    live catalogue, but the convention the examples themselves state CAN be:
    every pin is a glob pattern, or a template the caller fills in.
    """

    @staticmethod
    def _model_pins(node, path="") -> list[tuple[str, str]]:
        pins: list[tuple[str, str]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "model" and isinstance(value, str):
                    pins.append((path, value))
                else:
                    pins.extend(
                        TestShippedExamplesPinResolvableModels._model_pins(
                            value, f"{path}.{key}"
                        )
                    )
        elif isinstance(node, list):
            for index, value in enumerate(node):
                pins.extend(
                    TestShippedExamplesPinResolvableModels._model_pins(
                        value, f"{path}[{index}]"
                    )
                )
        return pins

    def test_every_example_step_model_is_a_glob_or_a_template(self):
        examples = sorted((REPO_ROOT / "examples").glob("*.yaml"))
        assert examples, f"no example recipes found under {REPO_ROOT / 'examples'}"

        offenders: list[str] = []
        for recipe_path in examples:
            loaded = yaml.safe_load(recipe_path.read_text())
            for where, model in self._model_pins(loaded):
                if any(char in model for char in "*?[") or "{{" in model:
                    continue
                offenders.append(f"{recipe_path.name}{where}: {model!r}")

        assert not offenders, (
            "shipped example pins a bare model id -- use a glob "
            "(e.g. 'claude-haiku-*') so the pin survives a model release:\n  "
            + "\n  ".join(offenders)
        )
