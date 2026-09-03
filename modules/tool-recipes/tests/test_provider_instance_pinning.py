"""A resolved ``model_role`` must pin the provider INSTANCE it names.

The field defect this reproduces (captured from a real run, session
``0000000000000000-c075c72254494a08_foundation-zen-architect``):

A schema-v2 recipe ran ``foundation:zen-architect`` in-session. That agent's
own definition file declares ``model_role: [reasoning, general]``, so the
closed-world catalog (``closed_world.py`` ``_OVERLAY_KEYS``) carried the
declaration into the engine's agent map, ``executor.execute_step``'s
agent-config ``model_role`` fallback resolved it through the host's
``model_role_resolver``, and the resulting chain went to ``session.spawn``.

The chain the anthropic routing matrix returns names provider **modules**
(``anthropic``, ``openai``, ``gemini``, ``github-copilot``) -- but this host,
like any host with more than one instance of a module, mounts provider
*instances* (``opus``, ``opus-4.8``, ``sonnet``, ``haiku``, ``fable``, ...).
Two downstream consumers match those names differently, and both got it wrong:

* ``amplifier_foundation.spawn_utils._build_provider_lookup`` (site-packages
  ``spawn_utils.py:649-674``) indexes each provider under its module name AND
  its instance id in one flat dict, so the *last declared* instance of a module
  wins the module-name key. ``anthropic`` therefore resolved to ``fable``,
  which was promoted to priority 0 with ``default_model=claude-opus-5``.
* the child's own ``hooks-routing`` role-pin re-assert
  (``role_pin.py:230-330``) matches those same preferences against the
  *mounted* providers, which are keyed by INSTANCE ID only. ``anthropic`` and
  ``openai`` matched nothing there -- but ``gemini``, whose instance id happens
  to equal its module's short name, matched literally. ``gemini`` was promoted
  to priority 0, and the agent ran on ``gemini-3.1-flash-image-preview``
  (65,536-token cap) until its ~152K-char system prompt drew a 400.

The control (a plain ``delegate`` of an agent with the identical
``model_role``) never emitted preferences at all, so the child inherited the
parent's priority ordering and resolved ``opus``.

So the engine must hand ``session.spawn`` preferences naming provider instance
ids that both matchers agree on -- or nothing at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from amplifier_foundation.spawn_utils import ProviderPreference

from amplifier_module_tool_recipes import runner_adapter as ra
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step
from tests.test_v2_closed_world_engine import FakeSessionManager
from tests.test_v2_closed_world_engine import FakeSpawn
from tests.test_v2_closed_world_engine import make_plan

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

RUNNER_AVAILABLE = ra.runner_available()
requires_runner = pytest.mark.skipif(
    not RUNNER_AVAILABLE, reason=f"{ra.RUNNER_DISTRIBUTION} is not importable"
)


# ---------------------------------------------------------------------------
# The host, as captured from the failing run's session:fork event
# ---------------------------------------------------------------------------


def _provider(
    instance_id: str, module: str, priority: int, default_model: str
) -> dict[str, Any]:
    return {
        "id": instance_id,
        "instance_id": instance_id,
        "module": module,
        "config": {"priority": priority, "default_model": default_model},
    }


#: The 14 provider instances the failing host had mounted, in declaration
#: order, with the priorities and default models it declared. Note the two
#: traps: five instances share ``provider-anthropic`` (so a bare "anthropic"
#: is ambiguous, and ``fable`` is the LAST of them), and the ``gemini``
#: instance id collides with the ``gemini`` module's short name.
HOST_PROVIDERS: list[dict[str, Any]] = [
    _provider("sol", "provider-openai", 3, "gpt-5.6-sol"),
    _provider("terra", "provider-openai", 2, "gpt-5.6-terra"),
    _provider("opus-4.8", "provider-anthropic", 4, "claude-opus-4-8"),
    _provider("opus", "provider-anthropic", 1, "claude-opus-5"),
    _provider("sonnet", "provider-anthropic", 5, "claude-sonnet-5"),
    _provider("gemini", "provider-gemini", 7, "gemini-3.1-flash-image-preview"),
    _provider("ghcp", "provider-github-copilot", 8, "claude-opus-4.7-high"),
    _provider("azure-openai", "provider-azure-openai", 10, "gpt-5"),
    _provider("haiku", "provider-anthropic", 12, "claude-haiku-4-5"),
    _provider("fable", "provider-anthropic", 13, "claude-fable-5-1"),
    _provider("luna", "provider-openai", 14, "gpt-5.6-luna"),
    _provider("luna-max", "provider-openai", 15, "gpt-5.6-luna"),
    _provider("sol-max", "provider-openai", 16, "gpt-5.6-sol"),
]

#: What the host's routing matrix (``routing.matrix=anthropic``) actually
#: returned for ``["reasoning", "general"]`` -- copied verbatim from the
#: failing child's ``session:fork`` ``provider_preferences``.
MATRIX_REASONING_GENERAL: tuple[tuple[str, str], ...] = (
    ("anthropic", "claude-opus-*"),
    ("openai", "gpt-5*-pro"),
    ("openai", "gpt-5.[0-9]"),
    ("gemini", "gemini-*-pro-preview"),
    ("gemini", "gemini-*-pro"),
    ("github-copilot", "claude-opus-*"),
    ("github-copilot", "gpt-5.[0-9]"),
)


class AnthropicMatrixResolver:
    """The ``model_role_resolver`` capability, as the anthropic matrix serves it.

    Returns ``list[ProviderPreference]`` naming provider MODULES -- which is
    what every shipped routing matrix declares (``routing/anthropic.yaml``:
    ``provider: anthropic``). The instance an id like that means is the host's
    problem to answer, not the matrix's.
    """

    name = "routing-matrix(anthropic)"
    known_roles = ("general", "fast", "coding", "reasoning")

    def __init__(self, chain: tuple[tuple[str, str], ...] = MATRIX_REASONING_GENERAL):
        self._chain = chain
        self.resolved: list[Any] = []

    async def resolve(self, model_role: Any) -> list[ProviderPreference]:
        self.resolved.append(model_role)
        config = {"reasoning_effort": "xhigh"}
        return [
            ProviderPreference(provider=provider, model=model, config=dict(config))
            for provider, model in self._chain
        ]


# ---------------------------------------------------------------------------
# The two downstream matchers, mirrored so the test can see what they see
# ---------------------------------------------------------------------------


def host_spawn_promotes(
    providers: list[dict[str, Any]], preferences: Any
) -> str | None:
    """Which instance ``apply_provider_preferences`` promotes to priority 0.

    Mirrors ``amplifier_foundation.spawn_utils._build_provider_lookup`` +
    ``apply_provider_preferences`` (``spawn_utils.py:649-727``): one flat dict
    of module ids, short names, ``provider-`` prefixed names and instance ids,
    built by enumeration -- so for a name several providers answer to, the
    LAST declared one wins.
    """
    lookup: dict[str, int] = {}
    for index, entry in enumerate(providers):
        module = entry.get("module", "")
        lookup[module] = index
        short = module.replace("provider-", "")
        if short != module:
            lookup[short] = index
        lookup[f"provider-{short}"] = index
        instance_id = entry.get("id")
        if instance_id:
            lookup[instance_id] = index

    for pref in preferences or ():
        if pref.provider in lookup:
            return providers[lookup[pref.provider]].get("id")
    return None


def child_role_pin_promotes(
    providers: list[dict[str, Any]], preferences: Any
) -> str | None:
    """Which instance the child's own routing re-assert promotes at session:start.

    Mirrors ``hooks-routing``'s ``role_pin._match_mounted`` /
    ``_select_preference`` (``role_pin.py:218-330``): the mounted providers are
    keyed by INSTANCE ID, the first preference that matches one wins, and an
    ambiguous name is refused rather than guessed.
    """
    mounted = [entry["id"] for entry in providers if entry.get("id")]

    def variants(name: str) -> set[str]:
        short = name.replace("provider-", "")
        return {name, short, f"provider-{short}"}

    for pref in preferences or ():
        if pref.provider in mounted:
            return pref.provider
        matches = [key for key in mounted if variants(pref.provider) & variants(key)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None  # ambiguous: role_pin refuses to guess
    return None


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class HostCoordinator:
    """A caller mirroring the failing host: many instances, few modules."""

    def __init__(
        self,
        spawn: Any,
        *,
        agents: dict[str, Any] | None = None,
        providers: list[dict[str, Any]] | None = None,
        resolver: Any = None,
    ) -> None:
        self.config: dict[str, Any] = {"agents": agents or {}}
        if providers is not None:
            self.config["providers"] = providers
        self.session = object()
        self.hooks = None
        self._capabilities: dict[str, Any] = {
            "session.spawn": spawn,
            "model_role_resolver": resolver,
        }

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value

    def get(self, name: str) -> Any:
        return self.config.get(name)


@pytest.fixture
def mock_session_manager():
    """A session manager with just enough state for the engine to run a step."""
    from unittest.mock import MagicMock

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


AGENT_FILE = """\
---
meta:
  name: zen-architect
  description: The reasoning-heavy architect the RECIPE declared
model_role:
  - reasoning
  - general
---
You are the declared architect.
"""

V2_RECIPE = """\
schema_version: 2
name: architect-recipe
description: one agent step, whose agent declares a model_role
version: "1.0.0"

dependencies:
  - source: "bundles/supplier"
    kind: bundle
    required_agents:
      - "supplier:zen-architect"

steps:
  - id: "review"
    agent: "supplier:zen-architect"
    prompt: "Review it"
    output: "review_result"
"""


async def _resolved(plan: Any) -> Any:
    return plan


def write_agent(tmp_path: Path) -> Path:
    agents_dir = tmp_path / "supplier" / "agents"
    agents_dir.mkdir(parents=True)
    path = agents_dir / "zen-architect.md"
    path.write_text(AGENT_FILE, encoding="utf-8")
    return path


def one_step_recipe(agent: str = "foundation:zen-architect") -> Recipe:
    return Recipe(
        name="test-recipe",
        description="one agent step",
        version="1.0.0",
        steps=[Step(id="review", agent=agent, prompt="Review it", output="result")],
        context={},
    )


# ---------------------------------------------------------------------------
# The premise: module-named preferences really are what broke the run
# ---------------------------------------------------------------------------


class TestTheDefectPremise:
    """Untranslated matrix output lands on the wrong instances. Both matchers."""

    def test_module_named_preferences_promote_the_last_declared_instance(self):
        raw = [
            ProviderPreference(provider=provider, model=model)
            for provider, model in MATRIX_REASONING_GENERAL
        ]

        # The spawner promotes `fable` -- the LAST anthropic instance declared,
        # not the one this session resolves by priority.
        assert host_spawn_promotes(HOST_PROVIDERS, raw) == "fable"

    def test_module_named_preferences_hand_the_child_to_a_name_collision(self):
        raw = [
            ProviderPreference(provider=provider, model=model)
            for provider, model in MATRIX_REASONING_GENERAL
        ]

        # In the child, "anthropic"/"openai" match no mounted instance at all;
        # "gemini" matches literally, because an instance is named that.
        assert child_role_pin_promotes(HOST_PROVIDERS, raw) == "gemini"


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class TestInstancePinning:
    @pytest.mark.asyncio
    async def test_agent_model_role_pins_the_priority_winning_instance(
        self, mock_session_manager, temp_dir
    ):
        """``reasoning`` on this host means ``opus``, and says so by id."""
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={
                "foundation:zen-architect": {
                    "description": "reasoning-heavy",
                    "model_role": ["reasoning", "general"],
                }
            },
            providers=HOST_PROVIDERS,
            resolver=AnthropicMatrixResolver(),
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe(), {}, temp_dir)

        prefs = spawn.calls[0]["provider_preferences"]
        assert prefs, "expected the resolved role to reach spawn"
        assert prefs[0].provider == "opus"
        assert prefs[0].model == "claude-opus-5"
        # The matrix's own knobs still ride along.
        assert prefs[0].config == {"reasoning_effort": "xhigh"}

        # Both downstream matchers now agree, and neither lands on the
        # instances the field defect landed on.
        assert host_spawn_promotes(HOST_PROVIDERS, prefs) == "opus"
        assert child_role_pin_promotes(HOST_PROVIDERS, prefs) == "opus"

    @pytest.mark.asyncio
    async def test_no_preference_names_an_unrelated_provider(
        self, mock_session_manager, temp_dir
    ):
        """Every emitted preference names an instance of its own module.

        ``fable`` (an anthropic instance the matrix never asked for) and any
        cross-module pick are the failure this forbids.
        """
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={
                "foundation:zen-architect": {"model_role": ["reasoning", "general"]}
            },
            providers=HOST_PROVIDERS,
            resolver=AnthropicMatrixResolver(),
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe(), {}, temp_dir)

        prefs = spawn.calls[0]["provider_preferences"]
        by_id = {entry["id"]: entry for entry in HOST_PROVIDERS}
        declared_modules = {
            f"provider-{provider}" for provider, _ in MATRIX_REASONING_GENERAL
        }

        assert "fable" not in {pref.provider for pref in prefs}
        for pref in prefs:
            assert pref.provider in by_id, (
                f"preference {pref.provider!r} is not a mounted instance id; a bare "
                "module name is what collided with the `gemini` instance"
            )
            assert by_id[pref.provider]["module"] in declared_modules

    @pytest.mark.asyncio
    async def test_a_role_no_installed_instance_serves_inherits_the_parent(
        self, mock_session_manager, temp_dir
    ):
        """No mapping is possible -> None (inherit parent), never a wrong pin.

        Parity with ``delegate``, which emits no preferences at all for a
        frontmatter ``model_role`` and lets the child inherit the parent's
        priority ordering.
        """
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={"foundation:zen-architect": {"model_role": ["reasoning"]}},
            providers=HOST_PROVIDERS,
            resolver=AnthropicMatrixResolver(
                chain=(("ollama", "llama-*"), ("bedrock", "claude-opus-*"))
            ),
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe(), {}, temp_dir)

        assert spawn.calls[0]["provider_preferences"] is None

    @pytest.mark.asyncio
    async def test_preferences_pass_through_when_the_host_lists_no_providers(
        self, mock_session_manager, temp_dir
    ):
        """With nothing to translate against, nothing is invented or dropped."""
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={
                "budget-agent": {
                    "provider_preferences": [
                        {"provider": "anthropic", "model": "claude-haiku-*"},
                        {"provider": "openai", "model": "gpt-5-mini"},
                    ]
                }
            },
            providers=None,
            resolver=None,
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe("budget-agent"), {}, temp_dir)

        prefs = spawn.calls[0]["provider_preferences"]
        assert [(p.provider, p.model) for p in prefs] == [
            ("anthropic", "claude-haiku-*"),
            ("openai", "gpt-5-mini"),
        ]

    @pytest.mark.asyncio
    async def test_a_modelless_preference_gets_the_instances_own_default(
        self, mock_session_manager, temp_dir
    ):
        """``provider:`` with no model means that instance's configured default.

        The legacy ``step.provider``-only path emitted ``model=""``, which the
        spawner writes straight into the promoted provider's ``default_model``
        -- blanking the model of whichever instance it guessed at.
        """
        spawn = FakeSpawn()
        coordinator = HostCoordinator(spawn, providers=HOST_PROVIDERS)

        recipe = Recipe(
            name="test-recipe",
            description="one agent step",
            version="1.0.0",
            steps=[
                Step(
                    id="review",
                    agent="some-agent",
                    prompt="Review it",
                    provider="anthropic",
                    output="result",
                )
            ],
            context={},
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(recipe, {}, temp_dir)

        prefs = spawn.calls[0]["provider_preferences"]
        assert [(p.provider, p.model) for p in prefs] == [("opus", "claude-opus-5")]

    @pytest.mark.asyncio
    async def test_an_explicit_instance_id_is_left_exactly_as_written(
        self, mock_session_manager, temp_dir
    ):
        """A recipe that pins ``fable`` by id gets ``fable`` -- pinning is not policy."""
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={
                "budget-agent": {
                    "provider_preferences": [
                        {"provider": "fable", "model": "claude-fable-5-1"}
                    ]
                }
            },
            providers=HOST_PROVIDERS,
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe("budget-agent"), {}, temp_dir)

        prefs = spawn.calls[0]["provider_preferences"]
        assert [(p.provider, p.model) for p in prefs] == [("fable", "claude-fable-5-1")]


# ---------------------------------------------------------------------------
# End to end, on the v2 path the field defect actually took
# ---------------------------------------------------------------------------


@requires_runner
class TestV2InSessionRun:
    @pytest.mark.asyncio
    async def test_a_v2_agent_step_pins_the_instance_its_role_names(
        self, tmp_path: Path
    ):
        from amplifier_recipe_runner.api import RunStatus

        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={"supplier:zen-architect": {"description": "the CALLER's impostor"}},
            providers=HOST_PROVIDERS,
            resolver=AnthropicMatrixResolver(),
        )
        sessions = FakeSessionManager(tmp_path)
        plan = make_plan(write_agent(tmp_path), reference="supplier:zen-architect")
        recipe = tmp_path / "architect.yaml"
        recipe.write_text(V2_RECIPE, encoding="utf-8")

        result = await ra.run_v2_recipe_in_session(
            coordinator,
            sessions,
            recipe,
            {},
            tmp_path,
            plan=lambda request: _resolved(plan),
        )

        assert result.status is RunStatus.SUCCEEDED, result.error
        assert len(spawn.calls) == 1
        # Premise: the catalog really did carry the agent's own model_role.
        assert spawn.calls[0]["agent_configs"]["supplier:zen-architect"][
            "model_role"
        ] == ["reasoning", "general"]

        prefs = spawn.calls[0]["provider_preferences"]
        assert prefs, "the v2 path resolved the role but handed spawn nothing"
        assert prefs[0].provider == "opus"
        assert host_spawn_promotes(HOST_PROVIDERS, prefs) == "opus"
        assert child_role_pin_promotes(HOST_PROVIDERS, prefs) == "opus"
        assert "fable" not in {pref.provider for pref in prefs}
