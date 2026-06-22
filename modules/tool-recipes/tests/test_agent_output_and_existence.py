"""Tests for agent-step output capture and agent-existence validation.

Covers two validator improvements:
- check_agent_output_capture: warn when an agent step has no `output:`
  (the agent's reply is silently discarded). Escape hatch: `output: discard`.
- check_agent_availability: `self` is a valid pseudo-agent and must never
  warn; enumeration is best-effort and skips when no registry is exposed.
"""

from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step
from amplifier_module_tool_recipes.validator import _enumerate_available_agents
from amplifier_module_tool_recipes.validator import check_agent_availability
from amplifier_module_tool_recipes.validator import check_agent_output_capture
from amplifier_module_tool_recipes.validator import validate_recipe


def _recipe(*steps: Step, **kwargs) -> Recipe:
    return Recipe(
        name="t",
        description="t",
        version="1.0.0",
        steps=list(steps),
        **kwargs,
    )


class TestCheckAgentOutputCapture:
    """Tests for check_agent_output_capture (q4c2)."""

    def test_agent_step_without_output_warns(self):
        recipe = _recipe(Step(id="summary", agent="a", prompt="report"))
        warnings = check_agent_output_capture(recipe)
        assert len(warnings) == 1
        assert "summary" in warnings[0]
        assert "output:" in warnings[0]

    def test_agent_step_with_output_no_warning(self):
        recipe = _recipe(
            Step(id="summary", agent="a", prompt="report", output="result")
        )
        assert check_agent_output_capture(recipe) == []

    def test_output_discard_is_explicit_optout(self):
        recipe = _recipe(
            Step(id="summary", agent="a", prompt="report", output="discard")
        )
        assert check_agent_output_capture(recipe) == []

    def test_bash_step_without_output_no_warning(self):
        recipe = _recipe(Step(id="b", type="bash", command="echo hi"))
        assert check_agent_output_capture(recipe) == []

    def test_recipe_step_without_output_no_warning(self):
        recipe = _recipe(Step(id="r", type="recipe", recipe="sub.yaml"))
        assert check_agent_output_capture(recipe) == []

    def test_foreach_collect_counts_as_capture(self):
        recipe = _recipe(
            Step(
                id="fan",
                agent="a",
                prompt="do {{item}}",
                foreach="{{items}}",
                collect="results",
            )
        )
        assert check_agent_output_capture(recipe) == []

    def test_compound_container_skipped(self):
        # foreach + while_steps => compound container; sub-steps carry output.
        recipe = _recipe(
            Step(
                id="loop",
                foreach="{{items}}",
                while_steps=[{"id": "inner", "agent": "a", "prompt": "x"}],
            )
        )
        assert check_agent_output_capture(recipe) == []

    def test_integration_validate_recipe_surfaces_warning(self):
        recipe = _recipe(Step(id="summary", agent="a", prompt="report"))
        result = validate_recipe(recipe)
        assert result.is_valid  # warning, not error
        assert any("summary" in w and "output:" in w for w in result.warnings)


class TestAgentSelfExemption:
    """`self` is a valid pseudo-agent and must never warn (63zf)."""

    def test_self_agent_not_flagged_as_unavailable(self, mock_coordinator):
        # mock_coordinator exposes ["test-agent", "code-analyzer", "reporter"]
        recipe = _recipe(Step(id="s", agent="self", prompt="x", output="o"))
        assert check_agent_availability(recipe, mock_coordinator) == []

    def test_real_missing_agent_still_flagged(self, mock_coordinator):
        recipe = _recipe(Step(id="s", agent="nope", prompt="x", output="o"))
        warnings = check_agent_availability(recipe, mock_coordinator)
        assert len(warnings) == 1
        assert "nope" in warnings[0]


class TestEnumerateAvailableAgents:
    """_enumerate_available_agents best-effort registry reading (63zf)."""

    def test_none_when_no_registry(self):
        class Bare:
            pass

        assert _enumerate_available_agents(Bare()) is None

    def test_none_coordinator(self):
        assert _enumerate_available_agents(None) is None

    def test_list_registry(self):
        class C:
            available_agents = ["x", "y"]

        assert _enumerate_available_agents(C()) == {"x", "y"}

    def test_tuple_registry(self):
        class C:
            available_agents = ("x", "y")

        assert _enumerate_available_agents(C()) == {"x", "y"}

    def test_dict_registry(self):
        class C:
            available_agents = {"x": {}, "y": {}}

        assert _enumerate_available_agents(C()) == {"x", "y"}

    def test_callable_registry(self):
        class C:
            def available_agents(self):
                return ["x"]

        assert _enumerate_available_agents(C()) == {"x"}

    def test_skips_when_registry_unreadable(self):
        # Coordinator without a recognizable registry => skip (no false warnings)
        class Bare:
            pass

        recipe = _recipe(Step(id="s", agent="whatever", prompt="x", output="o"))
        assert check_agent_availability(recipe, Bare()) == []
