"""Regression test: AttributeError when a step's output is a free-form string
and the next step references it via {{var}} in its prompt.

Bug: When step N declares 'output: foo' and the agent returns a plain string
(not JSON), context['foo'] is stored as a str.  During step N+1 dispatch,
executor.py calls .get() on a value it expects to be a dict but is actually
a string — raising ``AttributeError: 'str' object has no attribute 'get'``.

The error surfaces when the coordinator's agents config stores an agent's
configuration value as a plain string (e.g. ``agents["my-agent"] = "some desc"``)
rather than a dict.  The executor's ``execute_step`` function calls
``agent_cfg = agents.get(step.agent, {})`` and then immediately calls
``agent_cfg.get("provider_preferences", [])`` (line 1785) and
``agent_cfg.get("model_role")`` (line 1798) — both of which crash with
``AttributeError`` when ``agent_cfg`` is a string.

The real-world trigger: ``amplifier-bundle-reality-check``'s
``reality-check-pipeline.yaml``.  Step 1 (dtu-launch) returns free-form prose
stored as ``context["dtu_result"]``.  Step 2 (terminal-validation) references
``{{dtu_result}}`` in its prompt and has its agent config stored as a string
in the coordinator's agents dict.  The error is caught by
``_execute_recipe``'s broad ``except Exception as e:`` handler at
``__init__.py:464``, which discards the traceback and surfaces only:
  "Recipe execution failed: 'str' object has no attribute 'get'"
"""

import textwrap
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_coordinator_string_agent_cfg():
    """Mock coordinator where step-2's agent config is stored as a STRING.

    This mimics the real amplifier-bundle-reality-check environment where
    some agent configurations are stored as plain string descriptions rather
    than structured dicts.  The executor's ``execute_step`` hits line 1785
    (``agent_cfg.get("provider_preferences", [])``), which crashes with
    ``AttributeError: 'str' object has no attribute 'get'``.
    """
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    # Agent config stored as a STRING for step-2's agent — this is the bug trigger.
    # The step-1 agent is stored as a dict (fine); step-2's agent is a string.
    coordinator.config = {
        "agents": {
            "test:step1-agent": {"description": "Step 1 agent", "model_role": None},
            "test:step2-agent": "reality-check:terminal-tester",  # STRING, not dict!
        }
    }
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


@pytest.fixture
def mock_coordinator_empty_agents():
    """Mock coordinator with empty agents config (baseline — no bug)."""
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


@pytest.fixture
def mock_session_manager():
    """Create a mock session manager."""
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


# ---------------------------------------------------------------------------
# Regression test — RED before fix
# ---------------------------------------------------------------------------


class TestStringStepOutputDispatch:
    """Regression: string step output + string agent_cfg does not crash dispatch."""

    @pytest.mark.asyncio
    async def test_step_dispatch_with_string_output_from_previous_step(
        self, mock_coordinator_string_agent_cfg, mock_session_manager, temp_dir
    ):
        """Regression: when a previous step's output is a free-form string and
        the next step references it via {{foo}} in its prompt, dispatch must not
        crash with ``AttributeError: 'str' object has no attribute 'get'``.

        Reproduces the reality-check-pipeline.yaml failure:
          step 1 (dtu-launch) returns a plain string → context['dtu_result']
          step 2 (terminal-validation) references {{dtu_result}} in prompt AND
            has its agent config stored as a string in coordinator.config['agents']
          → AttributeError at execute_step: agent_cfg.get("provider_preferences")
            (executor.py line 1785) or agent_cfg.get("model_role") (line 1798)

        The test asserts that no AttributeError is raised.  Step 2 may succeed
        or fail for an unrelated reason (e.g. no real agent) — the only
        forbidden outcome is the raw AttributeError from the dispatch path.
        """
        # Configure mock: step 1 returns a plain TEXT string (not JSON).
        mock_spawn = mock_coordinator_string_agent_cfg.get_capability.return_value
        mock_spawn.side_effect = [
            # Step 1 return value: free-form prose, NOT parseable as JSON.
            # This matches the dtu-profile-builder returning a prose DTU summary.
            "DTU environment launched successfully. Access URL: http://localhost:8080. "
            "Environment ID: dtu-abc123. Status: running.",
            # Step 2 return value (only reached if bug is fixed)
            "Terminal tests completed. All 3 tests passed.",
        ]

        executor = RecipeExecutor(
            mock_coordinator_string_agent_cfg, mock_session_manager
        )

        recipe = Recipe(
            name="test-recipe",
            description="Regression test for string step output + string agent_cfg",
            version="0.1.0",
            steps=[
                Step(
                    id="step1",
                    agent="test:step1-agent",
                    prompt="Launch the environment for the software.",
                    output="dtu_result",
                    timeout=60,
                ),
                Step(
                    id="step2",
                    agent="test:step2-agent",  # has string config in agents dict
                    prompt=textwrap.dedent("""\
                        Run terminal tests against the deployed application.

                        The DTU environment was launched with these details:
                        {{dtu_result}}

                        Run all CLI-type tests now.
                    """),
                    output="terminal_results",
                    timeout=60,
                ),
            ],
            context={},
        )

        # The bug fires during step 2 dispatch: agent_cfg is a string, so
        # agent_cfg.get("provider_preferences", []) → AttributeError.
        try:
            result = await executor.execute_recipe(recipe, {}, temp_dir)
            # Bug is fixed: both steps dispatched without crashing.
            assert "dtu_result" in result, (
                "context['dtu_result'] should be set after step 1"
            )
            assert isinstance(result["dtu_result"], str), (
                "dtu_result should be a string (free-form agent output)"
            )
        except AttributeError as exc:
            pytest.fail(
                f"BUG REPRODUCED — AttributeError during step-2 dispatch: {exc}\n"
                "executor.py calls .get() on agent_cfg that is a str, not a dict.\n"
                "Lines 1785 or 1798: agent_cfg = agents.get(step.agent, {}) returns\n"
                "a string when coordinator.config['agents'][agent_name] is a string.\n"
                "Fix: add isinstance(agent_cfg, dict) guard before calling .get()."
            )

    @pytest.mark.asyncio
    async def test_string_output_prompt_substitution_does_not_raise(
        self, mock_coordinator_empty_agents, mock_session_manager, temp_dir
    ):
        """String context values are correctly substituted into prompts.

        Baseline test confirming that string step outputs are correctly
        substituted in subsequent step prompts when agent configs are dicts
        (or absent).  This is the simple working case — isolated from the
        agent-config-lookup bug tested above.
        """
        mock_spawn = mock_coordinator_empty_agents.get_capability.return_value
        mock_spawn.side_effect = [
            "A plain string response from step 1.",
            "Step 2 completed successfully.",
        ]

        executor = RecipeExecutor(mock_coordinator_empty_agents, mock_session_manager)

        recipe = Recipe(
            name="simple-test",
            description="Simple string substitution test",
            version="0.1.0",
            steps=[
                Step(
                    id="step1",
                    agent="test:agent",
                    prompt="Do step 1.",
                    output="step1_out",
                    timeout=60,
                ),
                Step(
                    id="step2",
                    agent="test:agent",
                    prompt="Step 1 said: {{step1_out}}. Now do step 2.",
                    output="step2_out",
                    timeout=60,
                ),
            ],
            context={},
        )

        try:
            result = await executor.execute_recipe(recipe, {}, temp_dir)
            assert isinstance(result.get("step1_out"), str)
            assert isinstance(result.get("step2_out"), str)
        except AttributeError as exc:
            pytest.fail(
                f"BUG REPRODUCED — AttributeError with empty agents config: {exc}"
            )
