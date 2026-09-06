"""A model pattern that matches nothing must fall back, not 404 -- and not 400.

The documented contract (``context/recipe-instructions.md``,
``docs/BEST_PRACTICES.md``): "if model pattern has no matches -> uses
provider's default model". ``resolve_model_pattern`` on its own returns an
unmatched glob unchanged, and handing a provider a model literally named
``claude-haiku-*`` gets a ``not_found_error`` -- so the executor closes that
gap. These tests pin both halves of it: the fallback fires on positive
evidence of no match, and does NOT fire when the provider catalogue simply
could not be read.

Second round (recipes-f0v). The first fix spelled "the provider's default
model" as the EMPTY STRING, and that is not the same fact. A preference's
model is stamped verbatim onto the promoted instance's mount config
(``spawn_utils._apply_single_override``: ``config["default_model"] = model``),
and the provider module reads that key back as its own default -- so an empty
model BLANKS it. Measured live: ``provider: anthropic`` +
``model: "claude-nosuchfamily-*"`` died with ``InvalidRequestError ... "model:
String should have at least 1 character"``. The 404 had become a 400 and the
run still did not happen. So the fallback must name a REAL model, and a
preference whose default cannot be named is dropped rather than emitted.
"""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from amplifier_foundation.spawn_utils import ModelResolutionResult
from amplifier_foundation.spawn_utils import ProviderPreference
from amplifier_module_tool_recipes.executor import (
    RecipeExecutor,
    _model_after_pattern_resolution,
    _provider_default_model,
    resolve_default_models,
)
from amplifier_module_tool_recipes.models import ProviderPreferenceConfig, Recipe, Step

REPO_ROOT = Path(__file__).resolve().parents[3]

#: What the fake anthropic instance reports as its own default model. Any
#: assertion on this value is asserting "the provider's default reached the
#: spawn", which is the whole contract.
INSTANCE_DEFAULT = "claude-sonnet-5"


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


def _coordinator_with_catalog(
    models: list[str] | None,
    *,
    instance_default: str | None = INSTANCE_DEFAULT,
    mount_entries: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """A coordinator whose provider offers ``models``.

    ``None`` means the provider cannot be enumerated at all -- the shape the
    legacy-compat harness deliberately uses, and the shape a host with no
    ``list_models`` support presents.

    ``instance_default`` is what the LIVE mounted provider reports as its own
    ``default_model``; ``None`` models the (unusual) host where nothing names
    one. ``mount_entries`` populates ``coordinator.config["providers"]`` -- the
    mount plan -- which is a *separate* source of the same fact and the one a
    normal host declares.
    """
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    if mount_entries is not None:
        coordinator.config["providers"] = mount_entries
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
        # A MagicMock auto-creates any attribute, so `default_model` must be
        # set to a real string (or deleted) rather than left to the mock --
        # otherwise the test would prove nothing about a real provider.
        if instance_default is None:
            del provider.default_model
        else:
            provider.default_model = instance_default
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
        """The documented fallback names a REAL model, not the empty string."""
        result = ModelResolutionResult(
            resolved_model="claude-haiku-*",
            pattern="claude-haiku-*",
            available_models=["claude-sonnet-4-5", "claude-opus-4-1"],
            matched_models=[],
        )
        coordinator = _coordinator_with_catalog(["claude-sonnet-4-5"])
        assert (
            _model_after_pattern_resolution(result, "anthropic", coordinator)
            == INSTANCE_DEFAULT
        )

    def test_fallback_never_substitutes_the_empty_string(self):
        """The 400 this round exists to stop.

        ``spawn_utils._apply_single_override`` writes a preference's model into
        the promoted provider's ``config["default_model"]`` unconditionally, so
        an empty model is not "leave it alone" -- it blanks the provider's own
        default and the request fails with "model: String should have at least
        1 character".
        """
        result = ModelResolutionResult(
            resolved_model=None,  # type: ignore[arg-type]
            pattern="claude-nosuchfamily-*",
            available_models=[
                "claude-fable-5-1",
                "claude-haiku-4-5-20251001",
                "claude-opus-5",
                "claude-sonnet-5",
            ],
            matched_models=[],
        )
        coordinator = _coordinator_with_catalog(["claude-sonnet-5"])
        assert (
            _model_after_pattern_resolution(result, "anthropic", coordinator)
            == INSTANCE_DEFAULT
        )

    def test_fallback_prefers_the_mount_plans_declared_default(self):
        """A host that declares ``default_model`` gets exactly that model."""
        result = ModelResolutionResult(
            resolved_model=None,  # type: ignore[arg-type]
            pattern="claude-nosuchfamily-*",
            available_models=["claude-opus-5"],
            matched_models=[],
        )
        coordinator = _coordinator_with_catalog(
            ["claude-opus-5"],
            instance_default="claude-sonnet-5",
            mount_entries=[
                {
                    "module": "provider-anthropic",
                    "config": {"priority": 0, "default_model": "claude-opus-5"},
                }
            ],
        )
        assert (
            _model_after_pattern_resolution(result, "anthropic", coordinator)
            == "claude-opus-5"
        )

    def test_fallback_with_no_nameable_default_stays_empty_for_the_drop(self):
        """"" survives only as the signal `resolve_default_models` drops on."""
        result = ModelResolutionResult(
            resolved_model=None,  # type: ignore[arg-type]
            pattern="claude-nosuchfamily-*",
            available_models=["claude-opus-5"],
            matched_models=[],
        )
        coordinator = _coordinator_with_catalog(
            ["claude-opus-5"], instance_default=None
        )
        assert _model_after_pattern_resolution(result, "anthropic", coordinator) == ""

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
        ``None`` must never become the literal string ``"None"``, which would
        be a model id no provider has -- with or without a coordinator to name
        a default with, and whether or not the catalogue could be read.
        """
        coordinator = _coordinator_with_catalog(["claude-sonnet-4-5"])
        for available, matched in (([], []), (["claude-sonnet-4-5"], [])):
            result = ModelResolutionResult(
                resolved_model=None,  # type: ignore[arg-type]
                pattern="claude-haiku-*",
                available_models=available,
                matched_models=matched,
            )
            for coord in (None, coordinator):
                assert _model_after_pattern_resolution(
                    result, "anthropic", coord
                ) != "None"


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
        assert prefs is not None, "the step must still be pinned to a provider"
        assert prefs[0].provider == "anthropic"
        assert prefs[0].model == INSTANCE_DEFAULT, (
            "an unmatched pattern must become the provider's REAL default "
            "model, not ride through as a 404-guaranteed model id and not "
            "collapse to the empty string the spawner writes over the "
            "provider's own default (a 400)"
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
        assert prefs is not None
        assert prefs[0].provider == "anthropic"
        assert prefs[0].model == INSTANCE_DEFAULT

    @pytest.mark.asyncio
    async def test_unmatched_pattern_uses_the_mount_plans_default_model(
        self, mock_session_manager, temp_dir
    ):
        """The declared host shape: mount entry names the default model.

        This is the acceptance criterion in full -- an unmatchable pattern on a
        provider whose mount entry declares ``default_model`` must reach the
        spawner as that model id, on the preference actually handed over.
        """
        coordinator = _coordinator_with_catalog(
            ["claude-fable-5-1", "claude-haiku-4-5-20251001", "claude-opus-5"],
            instance_default="ignored-if-the-mount-plan-declares-one",
            mount_entries=[
                {
                    "id": "anthropic",
                    "instance_id": "anthropic",
                    "module": "provider-anthropic",
                    "config": {"priority": 0, "default_model": "claude-opus-5"},
                }
            ],
        )
        mock_spawn = coordinator.get_capability("session.spawn")

        recipe = _one_step_recipe(
            Step(
                id="ping",
                agent="foundation:zen-architect",
                prompt="Reply OK",
                output="pong",
                provider="anthropic",
                model="claude-nosuchfamily-*",
            )
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        prefs = mock_spawn.call_args[1]["provider_preferences"]
        assert prefs is not None
        assert [(p.provider, p.model) for p in prefs] == [
            ("anthropic", "claude-opus-5")
        ]

    @pytest.mark.asyncio
    async def test_an_unnameable_default_drops_the_preference_not_the_run(
        self, mock_session_manager, temp_dir
    ):
        """No default anywhere: drop the pin, never emit the blanking value.

        Emitting ``model=""`` would promote the provider and blank its
        configured model, failing the request outright. Dropping the preference
        costs the promotion -- the child inherits the parent session's provider
        ordering, exactly as an unpinned ``delegate`` does -- and the step runs.
        """
        coordinator = _coordinator_with_catalog(
            ["claude-sonnet-4-5"], instance_default=None
        )
        mock_spawn = coordinator.get_capability("session.spawn")

        recipe = _one_step_recipe(
            Step(
                id="ping",
                agent="foundation:zen-architect",
                prompt="Reply OK",
                output="pong",
                provider="anthropic",
                model="claude-nosuchfamily-*",
            )
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        prefs = mock_spawn.call_args[1]["provider_preferences"]
        assert prefs is None, (
            "an unfillable preference must be dropped, never handed to the "
            f"spawn with an empty model; got {prefs!r}"
        )

    @pytest.mark.asyncio
    async def test_provider_without_model_also_names_a_real_model(
        self, mock_session_manager, temp_dir
    ):
        """``provider:`` alone is the other route to the same empty string.

        The legacy provider-only branch spells "use the provider's default" as
        ``model=""`` too, and it reaches the spawner through the same door.
        """
        coordinator = _coordinator_with_catalog(["claude-sonnet-4-5"])
        mock_spawn = coordinator.get_capability("session.spawn")

        recipe = _one_step_recipe(
            Step(
                id="ping",
                agent="foundation:zen-architect",
                prompt="Reply OK",
                output="pong",
                provider="anthropic",
            )
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        prefs = mock_spawn.call_args[1]["provider_preferences"]
        assert prefs is not None
        assert prefs[0].model == INSTANCE_DEFAULT


class TestResolveDefaultModels:
    """The last stop before the spawn, exercised directly."""

    def test_a_named_model_is_left_alone(self):
        coordinator = _coordinator_with_catalog(["claude-sonnet-4-5"])
        prefs = [ProviderPreference(provider="anthropic", model="claude-opus-5")]
        assert resolve_default_models(prefs, coordinator) == prefs

    def test_an_empty_model_is_filled_from_the_provider(self):
        coordinator = _coordinator_with_catalog(["claude-sonnet-4-5"])
        resolved = resolve_default_models(
            [ProviderPreference(provider="anthropic", model="")], coordinator
        )
        assert resolved is not None
        assert [(p.provider, p.model) for p in resolved] == [
            ("anthropic", INSTANCE_DEFAULT)
        ]

    def test_preference_config_survives_the_substitution(self):
        """Routing config on the preference is not collateral damage."""
        coordinator = _coordinator_with_catalog(["claude-sonnet-4-5"])
        resolved = resolve_default_models(
            [
                ProviderPreference(
                    provider="anthropic", model="", config={"temperature": 0.3}
                )
            ],
            coordinator,
        )
        assert resolved is not None
        assert resolved[0].config == {"temperature": 0.3}

    def test_an_unfillable_preference_is_dropped(self, caplog):
        coordinator = _coordinator_with_catalog(
            ["claude-sonnet-4-5"], instance_default=None
        )
        with caplog.at_level(
            logging.WARNING, logger="amplifier_module_tool_recipes.executor"
        ):
            resolved = resolve_default_models(
                [ProviderPreference(provider="anthropic", model="")], coordinator
            )
        assert resolved is None
        assert any(
            "at least 1 character" in record.getMessage()
            for record in caplog.records
        ), "the drop must say why an empty model is not safe to pass on"

    def test_only_the_unfillable_entry_is_dropped(self):
        """A fallback chain keeps every entry that can still be named."""
        coordinator = _coordinator_with_catalog(
            ["claude-sonnet-4-5"], instance_default=None
        )
        resolved = resolve_default_models(
            [
                ProviderPreference(provider="anthropic", model=""),
                ProviderPreference(provider="openai", model="gpt-5-mini"),
            ],
            coordinator,
        )
        assert resolved is not None
        assert [(p.provider, p.model) for p in resolved] == [
            ("openai", "gpt-5-mini")
        ]

    def test_no_preferences_is_passed_through_untouched(self):
        coordinator = _coordinator_with_catalog(["claude-sonnet-4-5"])
        assert resolve_default_models(None, coordinator) is None
        assert resolve_default_models([], coordinator) == []

    def test_provider_default_model_reads_the_mount_plan_first(self):
        coordinator = _coordinator_with_catalog(
            ["claude-sonnet-4-5"],
            instance_default="from-the-live-instance",
            mount_entries=[
                {
                    "module": "provider-anthropic",
                    "config": {"priority": 0, "default_model": "from-the-mount-plan"},
                }
            ],
        )
        assert (
            _provider_default_model(coordinator, "anthropic") == "from-the-mount-plan"
        )

    def test_provider_default_model_falls_through_to_the_live_instance(self):
        """The host shape that produced the field 400.

        A mount entry that declares no ``default_model`` is perfectly normal --
        the provider module supplies its own. ``pin_preferences_to_instances``
        has nothing to substitute there, so the live instance is the only thing
        that still knows the answer.
        """
        coordinator = _coordinator_with_catalog(
            ["claude-sonnet-4-5"],
            mount_entries=[
                {"module": "provider-anthropic", "config": {"priority": 0}}
            ],
        )
        assert _provider_default_model(coordinator, "anthropic") == INSTANCE_DEFAULT

    def test_a_module_name_resolves_through_the_instance_it_is_mounted_as(self):
        """The measured host shape: instances, addressed by module name.

        Mounted providers are keyed by INSTANCE ID, so ``anthropic`` matches
        none of them directly. The mount plan is what says which instances are
        that module, and the lowest priority number is the one the session
        resolves -- the same rule ``spawn_utils._find_provider_instance`` uses.
        """
        coordinator = _coordinator_with_catalog(None)
        low = MagicMock()
        low.default_model = "claude-sonnet-5"
        high = MagicMock()
        high.default_model = "claude-fable-5-1"
        coordinator.get.side_effect = lambda key: (
            {"rx-nodefault": low, "fable": high} if key == "providers" else None
        )
        coordinator.config["providers"] = [
            {
                "id": "fable",
                "instance_id": "fable",
                "module": "provider-anthropic",
                "config": {"priority": 13, "default_model": "claude-fable-5-1"},
            },
            {
                "id": "rx-nodefault",
                "instance_id": "rx-nodefault",
                "module": "provider-anthropic",
                "config": {"priority": 0},
            },
        ]

        # By module name: priority 0 wins, and it declares no default_model,
        # so only the live instance can answer.
        assert _provider_default_model(coordinator, "anthropic") == "claude-sonnet-5"
        # By instance id: exactly that instance, never its higher-priority sibling.
        assert _provider_default_model(coordinator, "rx-nodefault") == "claude-sonnet-5"
        assert _provider_default_model(coordinator, "fable") == "claude-fable-5-1"

    def test_provider_default_model_is_empty_when_nothing_names_one(self):
        coordinator = _coordinator_with_catalog(
            ["claude-sonnet-4-5"], instance_default=None
        )
        assert _provider_default_model(coordinator, "anthropic") == ""
        assert _provider_default_model(coordinator, "") == ""


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
