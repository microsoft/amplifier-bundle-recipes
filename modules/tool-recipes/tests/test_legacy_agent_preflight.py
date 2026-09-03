"""Plan-time agent preflight for the legacy (no ``schema_version``) path.

A legacy recipe binds ``agent:`` to the *caller's* agent map. Run one from a
bundle that does not mount an agent it references and, before this preflight,
the run started, printed the legacy deprecation notice, and then died at its
first agent step on a bare "agent not found" -- naming neither the bundle whose
map was consulted nor anything the user could do about it.

These tests pin the replacement: the run fails BEFORE any step, with a message
naming the agent(s), the bundle, and both remedies.

The load-bearing constraint is the *negative* one: the preflight may only fire
where the run was already doomed. A recipe whose agents ARE present, or a host
that exposes no readable agent registry, must be completely unaffected --
that is what keeps ``conformance/legacy-compat`` byte-identical and what the
"complete map" tests below assert.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from amplifier_module_tool_recipes import RecipesTool
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Stage
from amplifier_module_tool_recipes.models import Step
from amplifier_module_tool_recipes.runner_adapter import LEGACY_EXECUTION_MODE
from amplifier_module_tool_recipes.runner_adapter import check_legacy_agents_available
from amplifier_module_tool_recipes.runner_adapter import collect_agent_references
from amplifier_module_tool_recipes.runner_adapter import execution_mode_of
from amplifier_module_tool_recipes.runner_adapter import legacy_missing_agents_message

# The recipe from the bug report: legacy, one agent step, agent namespaced.
LEGACY_RECIPE = """\
name: legacy-recipe
description: A recipe with no manifest
version: "1.0.0"

steps:
  - id: review
    agent: "foundation:zen-architect"
    prompt: "Review it"
    output: review_result
"""

V2_RECIPE = """\
schema_version: 2

name: portable-recipe
description: Carries its own agents
version: "1.0.0"

dependencies:
  bundles:
    - foundation

steps:
  - id: review
    agent: "foundation:zen-architect"
    prompt: "Review it"
    output: review_result
"""


class FakeCoordinator:
    """A caller with a declared agent map and a declared bundle name.

    Deliberately not a ``MagicMock``: a mock auto-creates ``available_agents``
    and ``config``, which would make both the positive and the negative result
    here meaningless (the enumeration is best-effort and duck-typed).
    """

    def __init__(
        self,
        agents: dict[str, Any] | None = None,
        bundle_name: str | None = "anchors",
    ):
        self.config: dict[str, Any] = {
            "agents": {} if agents is None else dict(agents),
        }
        if bundle_name is not None:
            self.config["bundle_name"] = bundle_name
        self.session = object()
        self._capabilities: dict[str, Any] = {}

    @property
    def available_agents(self) -> list[str]:
        return sorted(self.config["agents"])

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value


class OpaqueCoordinator:
    """A host that publishes no readable agent registry at all."""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {"agents": {}}
        self.session = object()

    def get_capability(self, name: str) -> Any:
        return None


def write_recipe(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def make_tool(coordinator: Any) -> RecipesTool:
    executor = MagicMock()
    executor.execute_recipe = AsyncMock(
        return_value={"session": {"id": "sess-1"}, "final_output": "done"}
    )
    return RecipesTool(executor, MagicMock(), coordinator, {})


def _recipe(*steps: Step, **kwargs: Any) -> Recipe:
    return Recipe(
        name="t", description="t", version="1.0.0", steps=list(steps), **kwargs
    )


# ---------------------------------------------------------------------------
# Reference collection
# ---------------------------------------------------------------------------


class TestCollectAgentReferences:
    def test_flat_steps(self):
        recipe = _recipe(
            Step(id="a", agent="foundation:zen-architect", prompt="p", output="o"),
            Step(id="b", agent="foundation:explorer", prompt="p", output="o"),
        )
        assert collect_agent_references(recipe) == {
            "foundation:zen-architect",
            "foundation:explorer",
        }

    def test_staged_steps_are_included(self):
        """Staged recipes hide their steps behind ``stages``, not ``steps``."""
        recipe = Recipe(
            name="t",
            description="t",
            version="1.0.0",
            stages=[
                Stage(
                    name="plan",
                    steps=[Step(id="a", agent="ns:planner", prompt="p", output="o")],
                ),
                Stage(
                    name="build",
                    steps=[Step(id="b", agent="ns:builder", prompt="p", output="o")],
                ),
            ],
        )
        assert collect_agent_references(recipe) == {"ns:planner", "ns:builder"}

    def test_nested_loop_body_is_included(self):
        """A foreach/while body is raw dicts the parsed model never exposes.

        This is the reference class a naive ``get_all_steps()`` walk misses
        entirely -- and the one that would still die mid-run.
        """
        recipe = _recipe(
            Step(
                id="loop",
                foreach="{{items}}",
                while_steps=[
                    {"id": "inner", "agent": "ns:worker", "prompt": "p", "output": "o"}
                ],
            )
        )
        assert collect_agent_references(recipe) == {"ns:worker"}

    def test_nested_bodies_recurse(self):
        recipe = _recipe(
            Step(
                id="outer",
                foreach="{{items}}",
                while_steps=[
                    {
                        "id": "mid",
                        "foreach": "{{sub}}",
                        "steps": [
                            {"id": "deep", "agent": "ns:deep", "prompt": "p"},
                        ],
                    }
                ],
            )
        )
        assert collect_agent_references(recipe) == {"ns:deep"}

    def test_self_is_exempt(self):
        """``self`` spawns the current agent -- it is never a registry lookup."""
        recipe = _recipe(
            Step(id="a", agent="self", prompt="p", output="o"),
            Step(
                id="loop",
                foreach="{{items}}",
                while_steps=[{"id": "inner", "agent": "self", "prompt": "p"}],
            ),
        )
        assert collect_agent_references(recipe) == set()

    def test_non_agent_steps_contribute_nothing(self):
        recipe = _recipe(
            Step(id="b", type="bash", command="echo hi", output="o"),
            Step(id="r", type="recipe", recipe="sub.yaml", output="o"),
        )
        assert collect_agent_references(recipe) == set()


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------


class TestMessage:
    def test_names_agents_bundle_and_both_remedies(self):
        message = legacy_missing_agents_message(
            ["foundation:zen-architect", "foundation:explorer"], "anchors"
        )
        assert "foundation:zen-architect" in message
        assert "foundation:explorer" in message
        assert "anchors" in message
        # Remedy 1: run it somewhere that has them.
        assert "amplifier tool invoke -b foundation recipes" in message
        # Remedy 2: migrate it so it carries its own.
        assert "schema_version: 2" in message
        assert "dependencies:" in message
        assert "docs/RECIPE_SCHEMA.md" in message

    def test_example_bundle_comes_from_the_missing_reference(self):
        assert "-b myteam recipes" in legacy_missing_agents_message(
            ["myteam:reviewer"], "anchors"
        )

    def test_unnamespaced_agent_gets_a_placeholder_not_an_invented_bundle(self):
        message = legacy_missing_agents_message(["reviewer"], "anchors")
        assert "-b <bundle> recipes" in message

    def test_unknown_bundle_name_says_less_rather_than_guessing(self):
        message = legacy_missing_agents_message(["ns:a"], None)
        assert "the calling bundle does not mount" in message
        assert "''" not in message


# ---------------------------------------------------------------------------
# The preflight decision
# ---------------------------------------------------------------------------


class TestPreflightDecision:
    def test_missing_agent_is_reported_with_a_message(self):
        recipe = _recipe(
            Step(id="a", agent="foundation:zen-architect", prompt="p", output="o")
        )
        result = check_legacy_agents_available(recipe, FakeCoordinator())
        assert result is not None
        missing, message = result
        assert missing == ["foundation:zen-architect"]
        assert "foundation:zen-architect" in message
        assert "anchors" in message

    def test_complete_map_passes(self):
        recipe = _recipe(
            Step(id="a", agent="foundation:zen-architect", prompt="p", output="o")
        )
        coordinator = FakeCoordinator({"foundation:zen-architect": {}})
        assert check_legacy_agents_available(recipe, coordinator) is None

    def test_missing_agents_are_reported_sorted_and_deduplicated(self):
        recipe = _recipe(
            Step(id="a", agent="ns:beta", prompt="p", output="o"),
            Step(id="b", agent="ns:alpha", prompt="p", output="o"),
            Step(id="c", agent="ns:beta", prompt="p", output="o"),
        )
        result = check_legacy_agents_available(recipe, FakeCoordinator())
        assert result is not None
        assert result[0] == ["ns:alpha", "ns:beta"]

    def test_partially_complete_map_reports_only_what_is_missing(self):
        recipe = _recipe(
            Step(id="a", agent="ns:present", prompt="p", output="o"),
            Step(id="b", agent="ns:absent", prompt="p", output="o"),
        )
        coordinator = FakeCoordinator({"ns:present": {}})
        result = check_legacy_agents_available(recipe, coordinator)
        assert result is not None
        assert result[0] == ["ns:absent"]

    def test_unreadable_registry_skips_rather_than_refusing_the_run(self):
        """Refusing a runnable recipe over an unreadable registry is worse.

        Same rule ``check_agent_availability`` already applies: enumeration is
        best-effort, and ``None`` means "do not guess", not "assume empty".
        """
        recipe = _recipe(
            Step(id="a", agent="foundation:zen-architect", prompt="p", output="o")
        )
        assert check_legacy_agents_available(recipe, OpaqueCoordinator()) is None
        assert check_legacy_agents_available(recipe, None) is None

    def test_self_never_trips_the_preflight(self):
        recipe = _recipe(Step(id="a", agent="self", prompt="p", output="o"))
        assert check_legacy_agents_available(recipe, FakeCoordinator()) is None

    def test_agent_free_recipe_passes_against_an_empty_map(self):
        recipe = _recipe(Step(id="b", type="bash", command="echo hi", output="o"))
        assert check_legacy_agents_available(recipe, FakeCoordinator()) is None

    def test_nested_loop_agent_is_caught(self):
        recipe = _recipe(
            Step(
                id="loop",
                foreach="{{items}}",
                while_steps=[{"id": "inner", "agent": "ns:worker", "prompt": "p"}],
            )
        )
        result = check_legacy_agents_available(recipe, FakeCoordinator())
        assert result is not None
        assert result[0] == ["ns:worker"]


# ---------------------------------------------------------------------------
# End to end through the tool
# ---------------------------------------------------------------------------


class TestLegacyExecuteEndToEnd:
    @pytest.mark.asyncio
    async def test_missing_agent_fails_before_any_step_runs(self, temp_dir: Path):
        """The whole point: zero steps executed, and the message explains why."""
        tool = make_tool(FakeCoordinator())
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert result.success is False
        tool.executor.execute_recipe.assert_not_awaited()

        message = result.error["message"]
        assert "foundation:zen-architect" in message
        assert "anchors" in message
        assert "amplifier tool invoke -b foundation recipes" in message
        assert "schema_version: 2" in message
        assert result.error["missing_agents"] == ["foundation:zen-architect"]
        assert result.error["type"] == "LegacyAgentsUnavailable"

    @pytest.mark.asyncio
    async def test_failure_is_still_labeled_legacy_caller_bound(self, temp_dir: Path):
        """A preflight refusal is a legacy-path outcome and must say so."""
        tool = make_tool(FakeCoordinator())
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert execution_mode_of(result) == LEGACY_EXECUTION_MODE

    @pytest.mark.asyncio
    async def test_complete_map_runs_exactly_as_before(self, temp_dir: Path):
        """The negative case that keeps legacy-compat byte-identical."""
        tool = make_tool(FakeCoordinator({"foundation:zen-architect": {}}))
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert result.success is True
        assert tool.executor.execute_recipe.await_count == 1

    @pytest.mark.asyncio
    async def test_unreadable_registry_runs_exactly_as_before(self, temp_dir: Path):
        tool = make_tool(OpaqueCoordinator())
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert result.success is True
        assert tool.executor.execute_recipe.await_count == 1

    @pytest.mark.asyncio
    async def test_v2_path_is_untouched_by_the_preflight(self, temp_dir: Path):
        """A v2 recipe resolves agents from its own closure, not the caller's.

        Preflighting it against the caller's map would reintroduce exactly the
        caller binding schema v2 exists to end.
        """
        tool = make_tool(FakeCoordinator())
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        v2 = AsyncMock(return_value=MagicMock(success=True))
        tool._execute_v2_recipe = v2

        await tool._execute_recipe({"recipe_path": str(recipe)})

        assert v2.await_count == 1
        tool.executor.execute_recipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_structural_validation_still_reports_first(self, temp_dir: Path):
        """A broken recipe gets its structural error, not an agent diagnostic.

        The preflight answers "can this caller serve it"; that question is only
        meaningful once the recipe is well-formed.
        """
        tool = make_tool(FakeCoordinator())
        recipe = write_recipe(
            temp_dir,
            "broken.yaml",
            'name: broken\ndescription: d\nversion: "1.0.0"\n'
            "steps:\n  - id: a\n    agent: \"ns:missing\"\n",
        )

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert result.success is False
        assert result.error["message"] == "Recipe validation failed"
        tool.executor.execute_recipe.assert_not_awaited()
