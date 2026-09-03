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

Second half: the ARGUMENT is not the only thing a spawn carries
---------------------------------------------------------------

Re-tested live on the fixed engine (child session
``0000000000000000-a0a049acf77d43c7_foundation-zen-architect``), the pinned
ARGUMENT worked -- ``session:fork`` shows ``opus`` at priority 0, where the
first capture showed ``fable``. The child still ran on
``gemini-3.1-flash-image-preview`` and still took the 400.

Because a spawn also carries the agent's OVERLAY, and
``foundation:zen-architect``'s definition file declares its own
``provider_preferences`` in module names. The host merges that overlay into the
child's session config (``session_spawner.spawn_sub_session`` ->
``agent_config.merge_configs``; a list value simply overrides) and nothing
downstream rewrites it -- ``apply_provider_preferences_with_resolution`` edits
only the mount plan's ``providers``. So the child booted *declaring* the
untranslated chain, and ``hooks-routing``'s ``role_pin._declared_pins``
(``role_pin.py:266``) read that declaration back at ``session:start``,
re-matched "gemini" against the mounted instance ids and undid the promotion:
``session:config`` shows gemini 0, opus demoted 1.

Hence the tests below simulate the child-side re-pin against what the engine
hands to spawn as a WHOLE -- preferences argument *and* overlay -- not against
the argument alone, which is the modelling error that let the first fix pass
its tests and still fail in the field.
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
    instance_id: str | None, module: str, priority: int, default_model: str
) -> dict[str, Any]:
    """One mount-plan provider entry.

    ``instance_id=None`` is the module's DEFAULT instance -- an entry the host
    allows to carry no id at all (at most one per module,
    ``_session_init.py:136-152``). It is not a hypothetical: this host's 14th
    provider is one, and it is the entry that exposed the mount-key hole.
    ``id``/``instance_id`` both appear on id'd entries because
    ``_map_id_to_instance_id`` (``runtime/config.py:448-484``) copies one to
    the other without stripping either, and the two host matchers read
    different ones.
    """
    entry: dict[str, Any] = {
        "module": module,
        "config": {"priority": priority, "default_model": default_model},
    }
    if instance_id:
        entry["id"] = instance_id
        entry["instance_id"] = instance_id
    return entry


def mount_key(entry: dict[str, Any]) -> str:
    """The name the KERNEL mounts an entry under.

    Mirrors ``amplifier_core._session_init`` (``_session_init.py:154-214``):
    the instance id if there is one, else the module id with a leading
    ``provider-`` stripped. Keying these mirrors by ``entry["id"]`` instead --
    as they first did -- silently drops every id-less entry from the simulated
    child, which is precisely where the two matchers can still disagree.
    """
    instance_id = entry.get("instance_id") or entry.get("id")
    if isinstance(instance_id, str) and instance_id:
        return instance_id
    return str(entry.get("module", "")).removeprefix("provider-")


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
    # The 14th, and the only one with NO id: the default instance of its own
    # module, mounted as "openai-chatgpt". Present because the capture has it
    # -- a 13-entry fixture cannot see what an id-less entry does to either
    # matcher.
    _provider(None, "provider-openai-chatgpt", 17, "gpt-5.6-sol"),
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
            return mount_key(providers[lookup[pref.provider]])
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
    mounted = [mount_key(entry) for entry in providers if mount_key(entry)]

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
# The whole child boot, mirrored: overlay -> child config -> re-pin
# ---------------------------------------------------------------------------
#
# `child_role_pin_promotes` above answers "what would the re-assert do with
# THESE preferences" -- but the re-assert never sees the spawn's preferences
# argument. It reads the CHILD SESSION CONFIG's own `provider_preferences`
# (`role_pin._declared_pins`, role_pin.py:251-289), which the host built by
# merging the agent overlay over the parent's config. Simulating the argument
# alone is what made the first fix look correct while the field run still went
# to gemini, so the simulation below starts from the overlay.


def child_config_preferences(
    parent_config: dict[str, Any], overlay: Any
) -> list[dict[str, Any]] | None:
    """The ``provider_preferences`` the child session boots up declaring.

    Mirrors the slice of ``amplifier_app_cli.agent_config.merge_configs`` that
    decides this key: a list value in the overlay overrides the parent's, an
    absent one inherits it. (Verified against the installed
    ``merge_configs``; the child's ``session:fork`` record in the capture is
    byte-identical to the agent file's own block.)
    """
    if isinstance(overlay, dict) and "provider_preferences" in overlay:
        return overlay["provider_preferences"]
    return parent_config.get("provider_preferences")


def child_mount_plan(
    providers: list[dict[str, Any]], preferences: Any
) -> dict[str, dict[str, Any]]:
    """The child's provider priorities after the SPAWN applied its argument.

    Mirrors ``spawn_utils._apply_single_override`` (spawn_utils.py:756-812):
    the matched instance goes to priority 0 with the preference's model, and
    every other instance at or below 0 is demoted to 1.
    """
    plan = {
        mount_key(entry): {
            "priority": entry["config"]["priority"],
            "default_model": entry["config"]["default_model"],
        }
        for entry in providers
    }
    target = host_spawn_promotes(providers, preferences)
    if target is None:
        return plan
    model = next(
        (pref.model for pref in preferences if host_spawn_promotes(providers, [pref])),
        "",
    )
    plan[target]["priority"] = 0
    if model:
        plan[target]["default_model"] = model
    for name, state in plan.items():
        if name != target and state["priority"] <= 0:
            state["priority"] = 1
    return plan


def child_role_pin_reassert(
    plan: dict[str, dict[str, Any]], declared: Any
) -> dict[str, Any]:
    """``role_pin.reassert_own_role_pin``, mirrored field for field.

    Mirrors, in order: ``_declared_pins`` (session-level key; entries without a
    provider name dropped), ``_name_variants``/``_match_mounted`` (mounted keys
    are INSTANCE IDS; exact hit first, then variant intersection; an ambiguous
    name aborts the whole re-assert rather than guessing),
    ``_select_preference`` (the FIRST pin that matches a mounted key wins), and
    the apply block (target -> priority 0; every other provider at or below 0
    -> 1; a non-glob pinned model written to the target's ``default_model``).

    Returns ``{"winner", "priorities", "reasserted", "target"}``.
    """

    def variants(name: str) -> set[str]:
        short = name.replace("provider-", "")
        return {name, short, f"provider-{short}"}

    def winner_of(state: dict[str, dict[str, Any]]) -> str:
        return min(state.items(), key=lambda kv: (kv[1]["priority"], kv[0]))[0]

    pins: list[dict[str, Any]] = []
    for entry in declared or ():
        provider = (
            entry.get("provider")
            if isinstance(entry, dict)
            else getattr(entry, "provider", None)
        )
        model = (
            entry.get("model")
            if isinstance(entry, dict)
            else getattr(entry, "model", None)
        )
        if isinstance(provider, str) and provider:
            pins.append({"provider": provider, "model": model or None})

    result = {
        "winner": winner_of(plan),
        "priorities": {name: state["priority"] for name, state in plan.items()},
        "models": {name: state["default_model"] for name, state in plan.items()},
        "reasserted": False,
        "target": None,
    }
    if not pins:
        return result

    target: str | None = None
    pinned_model: str | None = None
    for pin in pins:
        if pin["provider"] in plan:
            target, pinned_model = pin["provider"], pin["model"]
            break
        matches = [key for key in plan if variants(pin["provider"]) & variants(key)]
        if len(matches) == 1:
            target, pinned_model = matches[0], pin["model"]
            break
        if matches:
            return result  # ambiguous: refused, ordering untouched
    if target is None:
        return result  # nothing mounted answers to this chain

    drifted = winner_of(plan) != target
    model_drifted = bool(
        pinned_model
        and not any(c in pinned_model for c in "*?[")
        and plan[target]["default_model"] != pinned_model
    )
    if not (drifted or model_drifted):
        result["target"] = target
        return result

    if drifted:
        plan[target]["priority"] = 0
        for name, state in plan.items():
            if name != target and state["priority"] <= 0:
                state["priority"] = 1
    if model_drifted:
        plan[target]["default_model"] = pinned_model

    return {
        "winner": winner_of(plan),
        "priorities": {name: state["priority"] for name, state in plan.items()},
        "models": {name: state["default_model"] for name, state in plan.items()},
        "reasserted": True,
        "target": target,
    }


def child_boot(
    spawn_call: dict[str, Any],
    agent: str,
    *,
    providers: list[dict[str, Any]] | None = None,
    parent_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What the child session resolves, from ONE recorded ``session.spawn`` call.

    The whole hop the field defect lives in: the spawn's preferences argument
    promotes an instance, the spawn's agent overlay becomes the child's own
    declared chain, and the child's routing re-assert then runs on both.
    """
    providers = providers if providers is not None else HOST_PROVIDERS
    preferences = spawn_call.get("provider_preferences")
    overlay = (spawn_call.get("agent_configs") or {}).get(agent)
    plan = child_mount_plan(providers, preferences)
    declared = child_config_preferences(parent_config or {}, overlay)
    return child_role_pin_reassert(plan, declared)


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

#: ``foundation:zen-architect`` as actually shipped: BOTH a ``model_role`` and
#: its own module-named ``provider_preferences`` block. The second key is what
#: the closed-world catalog carried into the child's session config verbatim,
#: and what the child's routing re-assert then re-pinned itself from -- so a
#: fixture with only ``model_role`` cannot reproduce the field defect.
AGENT_FILE_AS_SHIPPED = (
    """\
---
meta:
  name: zen-architect
  description: The reasoning-heavy architect the RECIPE declared
model_role:
  - reasoning
  - general
provider_preferences:
"""
    + "".join(
        f"  - provider: {provider}\n    model: {model!r}\n"
        for provider, model in MATRIX_REASONING_GENERAL
    )
    + """\
---
You are the declared architect.
"""
)

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


def write_agent(tmp_path: Path, content: str = AGENT_FILE) -> Path:
    agents_dir = tmp_path / "supplier" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / "zen-architect.md"
    path.write_text(content, encoding="utf-8")
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

    def test_a_pinned_argument_with_an_untranslated_overlay_still_goes_to_gemini(
        self,
    ):
        """The re-test capture, reproduced: right argument, wrong overlay.

        This is the shape the engine emitted after the argument-only fix --
        preferences pinned to ``opus``, overlay still carrying the agent
        file's own module-named chain. The child promoted ``opus`` at fork and
        then re-pinned itself onto ``gemini`` at ``session:start``, exactly as
        session ``...a0a049acf77d43c7`` recorded.
        """
        pre_fix_call = {
            "provider_preferences": [
                ProviderPreference(provider="opus", model="claude-opus-5")
            ],
            "agent_configs": {
                "foundation:zen-architect": {
                    "model_role": ["reasoning", "general"],
                    "provider_preferences": [
                        {"provider": provider, "model": model}
                        for provider, model in MATRIX_REASONING_GENERAL
                    ],
                }
            },
        }

        # The spawn argument really did promote opus...
        plan = child_mount_plan(
            HOST_PROVIDERS, pre_fix_call["provider_preferences"]
        )
        assert min(plan, key=lambda name: plan[name]["priority"]) == "opus"

        # ...and the child's own declaration took it straight back.
        child = child_boot(pre_fix_call, "foundation:zen-architect")
        assert child["reasserted"] is True
        assert child["winner"] == "gemini"
        assert child["priorities"]["gemini"] == 0
        assert child["priorities"]["opus"] == 1
        assert child["models"]["gemini"] == "gemini-3.1-flash-image-preview"


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
        by_id = {mount_key(entry): entry for entry in HOST_PROVIDERS}
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
# The overlay half: what the child boots up DECLARING
# ---------------------------------------------------------------------------


SHIPPED_AGENT_OVERLAY: dict[str, Any] = {
    "description": "the reasoning-heavy architect",
    "model_role": ["reasoning", "general"],
    "provider_preferences": [
        {"provider": provider, "model": model}
        for provider, model in MATRIX_REASONING_GENERAL
    ],
}


class TestOverlaySurvivesTheChildsRePin:
    """The spawn's overlay declares the same instances its argument promotes."""

    @pytest.mark.asyncio
    async def test_the_overlay_declares_the_pinned_chain(
        self, mock_session_manager, temp_dir
    ):
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={"foundation:zen-architect": dict(SHIPPED_AGENT_OVERLAY)},
            providers=HOST_PROVIDERS,
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe(), {}, temp_dir)

        call = spawn.calls[0]
        overlay = call["agent_configs"]["foundation:zen-architect"]
        mounted = {mount_key(entry) for entry in HOST_PROVIDERS}

        assert overlay["provider_preferences"][0] == {
            "provider": "opus",
            "model": "claude-opus-5",
        }
        assert {p["provider"] for p in overlay["provider_preferences"]} <= mounted, (
            "the overlay still names provider modules; that is the declaration "
            "the child re-pins itself from"
        )
        # Argument and overlay say the same thing, in the same order.
        assert [p["provider"] for p in overlay["provider_preferences"]] == [
            pref.provider for pref in call["provider_preferences"]
        ]

    @pytest.mark.asyncio
    async def test_the_child_re_pin_finds_nothing_to_correct(
        self, mock_session_manager, temp_dir
    ):
        """The whole point: opus survives ``session:start``, gemini is untouched."""
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={"foundation:zen-architect": dict(SHIPPED_AGENT_OVERLAY)},
            providers=HOST_PROVIDERS,
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe(), {}, temp_dir)

        child = child_boot(spawn.calls[0], "foundation:zen-architect")
        assert child["winner"] == "opus"
        assert child["reasserted"] is False, (
            "a re-assert here means the child disagreed with the spawn again"
        )
        assert child["models"]["opus"] == "claude-opus-5"
        assert child["priorities"]["opus"] == 0
        assert child["priorities"]["gemini"] == 7, (
            "gemini must be left exactly where the host declared it"
        )

    @pytest.mark.asyncio
    async def test_model_role_is_still_forwarded_and_is_inert(
        self, mock_session_manager, temp_dir
    ):
        """``model_role`` rides along; it is the pinned chain that decides.

        Determined against the installed host rather than assumed:
        ``role_pin._declared_pins`` reads ``config["provider_preferences"]``
        only (role_pin.py:266); the routing hook resolves ``model_role``
        exclusively for entries under ``config["agents"]``
        (hooks-routing ``__init__.py`` ``_resolve_one``, i.e. children this
        session may spawn); and ``session_spawner`` never re-derives
        preferences from it -- ``model_role`` reaches it only as an explicit
        resume argument. So forwarding it costs nothing and keeps the pin's
        provenance in the child's own config.
        """
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={"foundation:zen-architect": dict(SHIPPED_AGENT_OVERLAY)},
            providers=HOST_PROVIDERS,
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe(), {}, temp_dir)

        overlay = spawn.calls[0]["agent_configs"]["foundation:zen-architect"]
        assert overlay["model_role"] == ["reasoning", "general"]
        assert child_boot(spawn.calls[0], "foundation:zen-architect")["winner"] == "opus"

    @pytest.mark.asyncio
    async def test_an_unservable_chain_leaves_no_declaration_behind(
        self, mock_session_manager, temp_dir
    ):
        """Nothing pinnable -> the overlay's chain goes too, not just the argument.

        Otherwise "inherit the parent's ordering" (what the argument now says)
        and "re-pin yourself onto whatever spelling collides" (what the stale
        overlay would say) are the same spawn.
        """
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={
                "foundation:zen-architect": {
                    "model_role": ["reasoning"],
                    "provider_preferences": [
                        {"provider": "ollama", "model": "llama-*"},
                        {"provider": "gemini", "model": "gemini-*-pro"},
                    ],
                }
            },
            providers=[
                entry for entry in HOST_PROVIDERS if mount_key(entry) != "gemini"
            ],
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe(), {}, temp_dir)

        call = spawn.calls[0]
        overlay = call["agent_configs"]["foundation:zen-architect"]
        assert call["provider_preferences"] is None
        assert "provider_preferences" not in overlay
        assert overlay["model_role"] == ["reasoning"]

        providers = [
            entry for entry in HOST_PROVIDERS if mount_key(entry) != "gemini"
        ]
        child = child_boot(call, "foundation:zen-architect", providers=providers)
        assert child["reasserted"] is False
        # Parent ordering, exactly as a `delegate` of the same agent gets.
        assert child["winner"] == "opus"

    @pytest.mark.asyncio
    async def test_the_callers_agent_map_is_never_edited(
        self, mock_session_manager, temp_dir
    ):
        """Alignment copies. A spawn must not rewrite the caller's live map.

        The host's agent map is shared by every session in the process; editing
        an entry here would pin some later, unrelated delegate to this step's
        provider.
        """
        spawn = FakeSpawn()
        host_map = {"foundation:zen-architect": dict(SHIPPED_AGENT_OVERLAY)}
        coordinator = HostCoordinator(
            spawn, agents=host_map, providers=HOST_PROVIDERS
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe(), {}, temp_dir)

        assert host_map["foundation:zen-architect"]["provider_preferences"] == [
            {"provider": provider, "model": model}
            for provider, model in MATRIX_REASONING_GENERAL
        ]

    @pytest.mark.asyncio
    async def test_a_recipe_with_no_provider_intent_hands_the_map_through(
        self, mock_session_manager, temp_dir
    ):
        """No preferences anywhere -> the caller's own object, untouched.

        The legacy identity path: nothing to align means nothing copied and
        nothing rewritten, so a legacy recipe's spawn is byte-identical.
        """
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={"plain-agent": {"description": "no provider intent at all"}},
            providers=HOST_PROVIDERS,
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(one_step_recipe("plain-agent"), {}, temp_dir)

        assert spawn.calls[0]["provider_preferences"] is None
        assert spawn.calls[0]["agent_configs"] is coordinator.config["agents"]


# ---------------------------------------------------------------------------
# The id-less default instance: the 14th, and the name neither matcher owns
# ---------------------------------------------------------------------------
#
# A provider entry without an ``id`` is legal (at most one per module) and IS
# mounted -- under the module's short name (``_session_init.py:154-214``). The
# two matchers read that name differently the moment its module has more than
# one entry: the spawner's flat lookup gives the module's short name to the
# LAST declared instance (``spawn_utils.py:648-673``), while the child mounts
# the id-less entry there. So a chain naming it promotes one provider at spawn
# and a different one at ``session:start`` -- the field defect's exact shape,
# reached by a different collision.
#
# Measured against the installed host before the guard existed (the same
# harness as the capture above, with this host's own id-less entry moved into
# ``provider-openai``): spawn promoted ``sol-max``, ``role_pin`` re-pinned to
# ``openai``.


#: The hostile-but-legal host: one id-less ``provider-openai`` entry, at the
#: lowest priority number, alongside two id'd instances of the same module.
MIXED_PROVIDERS: list[dict[str, Any]] = [
    _provider(None, "provider-openai", 1, "gpt-5.6-default"),
    _provider("sol", "provider-openai", 3, "gpt-5.6-sol"),
    _provider("sol-max", "provider-openai", 9, "gpt-5.6-sol"),
    _provider("opus", "provider-anthropic", 5, "claude-opus-5"),
]

MIXED_AGENT_OVERLAY: dict[str, Any] = {
    "description": "wants openai, by module name",
    "model_role": ["fast"],
    "provider_preferences": [{"provider": "openai", "model": "gpt-5.[0-9]"}],
}


class TestTheIdLessDefaultInstance:
    """A mounted instance the engine cannot name is skipped, never pinned."""

    def test_the_capture_host_really_has_a_fourteenth_id_less_instance(self):
        """The fixture is the capture, not a tidied-up version of it."""
        assert len(HOST_PROVIDERS) == 14
        id_less = [entry for entry in HOST_PROVIDERS if not entry.get("id")]
        assert [entry["module"] for entry in id_less] == [
            "provider-openai-chatgpt"
        ]
        # Mounted, and under its module's short name -- not absent, and not "".
        assert mount_key(id_less[0]) == "openai-chatgpt"
        assert "openai-chatgpt" in child_mount_plan(HOST_PROVIDERS, None)

    def test_pinning_to_an_unnameable_instance_would_split_the_matchers(self):
        """Non-vacuity: the name the guard refuses really is a split name.

        Without this, a guard that never fires and a guard that is not needed
        look identical.
        """
        by_mount_name = [ProviderPreference(provider="openai", model="")]
        assert host_spawn_promotes(MIXED_PROVIDERS, by_mount_name) == "sol-max"
        assert child_role_pin_promotes(MIXED_PROVIDERS, by_mount_name) == "openai"

    @pytest.mark.asyncio
    async def test_the_engine_never_emits_the_split_name(
        self, mock_session_manager, temp_dir
    ):
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={"caller:writer": dict(MIXED_AGENT_OVERLAY)},
            providers=MIXED_PROVIDERS,
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(
            one_step_recipe("caller:writer"), {}, temp_dir
        )

        call = spawn.calls[0]
        emitted = [pref.provider for pref in call["provider_preferences"]]
        overlay = call["agent_configs"]["caller:writer"]

        assert emitted == ["sol"], (
            "the engine pinned to the id-less default instance's mount name, "
            "which the spawner resolves to a different provider entirely"
        )
        assert [p["provider"] for p in overlay["provider_preferences"]] == emitted

    @pytest.mark.asyncio
    async def test_spawn_and_child_land_on_the_same_instance(
        self, mock_session_manager, temp_dir
    ):
        """The whole invariant, stated once: both matchers, one instance."""
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={"caller:writer": dict(MIXED_AGENT_OVERLAY)},
            providers=MIXED_PROVIDERS,
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(
            one_step_recipe("caller:writer"), {}, temp_dir
        )

        call = spawn.calls[0]
        promoted = host_spawn_promotes(
            MIXED_PROVIDERS, call["provider_preferences"]
        )
        child = child_boot(
            call, "caller:writer", providers=MIXED_PROVIDERS
        )

        assert promoted == "sol"
        assert child["winner"] == promoted, (
            "spawn promoted %r and the child re-pinned to %r -- the same "
            "disagreement the whole branch exists to remove"
            % (promoted, child["winner"])
        )
        assert child["reasserted"] is False

    @pytest.mark.asyncio
    async def test_a_sole_id_less_instance_is_still_pinnable(
        self, mock_session_manager, temp_dir
    ):
        """Only one entry for the module -> its mount name IS unambiguous.

        The guard refuses split names, not id-less entries: dropping every
        id-less provider would strand the single-provider hosts that are the
        common case.
        """
        providers = [
            _provider(None, "provider-anthropic", 4, "claude-opus-5"),
            _provider("gemini", "provider-gemini", 7, "gemini-3.1-flash"),
        ]
        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={
                "caller:writer": {
                    "model_role": ["reasoning"],
                    "provider_preferences": [
                        {"provider": "anthropic", "model": "claude-opus-*"}
                    ],
                }
            },
            providers=providers,
        )

        executor = RecipeExecutor(coordinator, mock_session_manager)
        await executor.execute_recipe(
            one_step_recipe("caller:writer"), {}, temp_dir
        )

        call = spawn.calls[0]
        assert [pref.provider for pref in call["provider_preferences"]] == [
            "anthropic"
        ]
        assert host_spawn_promotes(providers, call["provider_preferences"]) == (
            "anthropic"
        )
        child = child_boot(call, "caller:writer", providers=providers)
        assert child["winner"] == "anthropic"
        assert child["reasserted"] is False


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

    @pytest.mark.asyncio
    async def test_a_v2_spawn_survives_the_childs_own_re_pin(self, tmp_path: Path):
        """The whole spawn -- argument AND overlay -- lands on ``opus``.

        The re-test that motivated this: the argument alone was already right
        and the run still went to gemini, because the catalog handed the child
        the agent file's own module-named chain to re-pin itself from.
        """
        from amplifier_recipe_runner.api import RunStatus

        spawn = FakeSpawn()
        coordinator = HostCoordinator(
            spawn,
            agents={"supplier:zen-architect": {"description": "the CALLER's impostor"}},
            providers=HOST_PROVIDERS,
            resolver=AnthropicMatrixResolver(),
        )
        sessions = FakeSessionManager(tmp_path)
        plan = make_plan(
            write_agent(tmp_path, AGENT_FILE_AS_SHIPPED),
            reference="supplier:zen-architect",
        )
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
        call = spawn.calls[0]
        overlay = call["agent_configs"]["supplier:zen-architect"]

        # The overlay declares instance ids, not the module names the agent
        # file was written with.
        mounted = {mount_key(entry) for entry in HOST_PROVIDERS}
        assert [p["provider"] for p in overlay["provider_preferences"]] == [
            pref.provider for pref in call["provider_preferences"]
        ]
        assert {p["provider"] for p in overlay["provider_preferences"]} <= mounted

        # ...so the child's own re-assert has nothing to correct.
        child = child_boot(call, "supplier:zen-architect")
        assert child["winner"] == "opus"
        assert child["reasserted"] is False
        assert child["models"]["opus"] == "claude-opus-5"
        assert child["priorities"]["gemini"] == 7

    @pytest.mark.asyncio
    async def test_the_wrapper_aligns_what_it_sends_and_keeps_its_own_record(
        self, tmp_path: Path
    ):
        """``ClosedWorldSpawn`` is the last hop that decides a v2 overlay.

        It discards the engine's map by design (manifest Core 5), so the
        alignment has to happen on the catalog copy it substitutes -- and it
        must not edit the catalog itself, which is this run's record of what
        each agent's definition file actually says.
        """
        from amplifier_module_tool_recipes.closed_world import ClosedWorldSpawn
        from amplifier_module_tool_recipes.closed_world import build_catalog

        catalog = build_catalog(
            make_plan(
                write_agent(tmp_path, AGENT_FILE_AS_SHIPPED),
                reference="supplier:zen-architect",
            )
        )
        spawn = FakeSpawn()
        wrapper = ClosedWorldSpawn(spawn, catalog)
        pinned = [ProviderPreference(provider="opus", model="claude-opus-5")]

        await wrapper(
            agent_name="supplier:zen-architect",
            instruction="Review it",
            provider_preferences=pinned,
        )

        sent = spawn.calls[0]["agent_configs"]["supplier:zen-architect"]
        assert sent["provider_preferences"] == [
            {"provider": "opus", "model": "claude-opus-5"}
        ]
        # ...and the catalog still reports the file as written.
        kept = catalog.agent_configs()["supplier:zen-architect"]
        assert [p["provider"] for p in kept["provider_preferences"]] == [
            provider for provider, _ in MATRIX_REASONING_GENERAL
        ]
