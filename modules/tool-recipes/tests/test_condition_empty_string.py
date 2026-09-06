"""Conditions resolve VALUES, not text (recipes-kft).

The defect: condition text was resolved by the general-purpose *textual*
substituter in three places -- a loop sub-step's ``condition:``, a
``while_condition:`` and a ``break_when:``. Textual substitution pastes the
value in bare, so a variable defaulting to the empty string turned

    {{continue_from}} == ''      into       == ''

-- an operator with no left-hand value -- and the evaluator refused it. A
value with a space was just as fatal (``a b == 'a b'``: two bare tokens).

The fix routes those three call sites through the evaluator's own
``substitute_condition_variables``, which renders a value as a well-formed
*literal*. This file pins both halves: the rendering matrix, and the three
executor paths that were dying on their own defaults.
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.expression_evaluator import ExpressionError
from amplifier_module_tool_recipes.expression_evaluator import evaluate_condition
from amplifier_module_tool_recipes.expression_evaluator import (
    substitute_condition_variables,
)
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step


@pytest.fixture
def mock_coordinator():
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


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


class TestConditionSubstitutionRenders:
    """Every value type renders as something the parser can tokenize."""

    def test_empty_string_renders_as_an_empty_literal(self):
        # The reported defect, exactly: bare, this left ` == ''`.
        assert substitute_condition_variables("{{flag}} == ''", {"flag": ""}) == "'' == ''"

    def test_string_with_spaces_renders_as_one_literal(self):
        # Bare, this became two tokens: `a b == 'a b'`.
        assert (
            substitute_condition_variables("{{s}} == 'a b'", {"s": "a b"})
            == "'a b' == 'a b'"
        )

    def test_string_containing_a_quote_is_escaped(self):
        assert (
            substitute_condition_variables("{{s}} == 'x'", {"s": "it's"})
            == "'it\\'s' == 'x'"
        )

    def test_number_renders_bare(self):
        assert substitute_condition_variables("{{n}} > 3", {"n": 5}) == "5 > 3"

    def test_bool_renders_as_a_lowercase_literal(self):
        assert substitute_condition_variables("{{b}}", {"b": True}) == "true"
        assert substitute_condition_variables("{{b}}", {"b": False}) == "false"

    def test_none_renders_as_null(self):
        assert substitute_condition_variables("{{z}} == ''", {"z": None}) == "null == ''"

    def test_a_reference_the_context_lacks_is_still_an_error(self):
        # A None VALUE is not "undefined"; an absent KEY still is.
        with pytest.raises(ExpressionError, match="Undefined variable: nope"):
            substitute_condition_variables("{{nope}} == ''", {"flag": ""})

    def test_a_reference_the_author_quoted_is_not_double_quoted(self):
        # '{{var}}' == '' is the spelling the issue reported as the remedy.
        # Wrapping the value in quotes of our own would make it '''' -- two
        # empty literals side by side, which the parser rejects.
        assert substitute_condition_variables("'{{flag}}' == ''", {"flag": ""}) == "'' == ''"
        assert (
            substitute_condition_variables('"{{s}}" != ""', {"s": 'say "hi"'})
            == '"say \\"hi\\"" != ""'
        )

    def test_substitution_is_stable_when_applied_twice(self):
        # Call sites resolve for the skip message and hand the resolved text
        # to evaluate_condition, which substitutes again.
        once = substitute_condition_variables("{{s}} == 'x'", {"s": "a'b"})
        assert substitute_condition_variables(once, {"s": "a'b"}) == once


class TestConditionEvaluatesForEveryValueType:
    """The rendered text evaluates, rather than raising."""

    @pytest.mark.parametrize(
        "expression,context,expected",
        [
            ("{{flag}} == ''", {"flag": ""}, True),
            ("{{flag}} != ''", {"flag": ""}, False),
            ("{{flag}} == ''", {"flag": "v3.md"}, False),
            ("{{flag}} != ''", {"flag": "v3.md"}, True),
            ("'{{flag}}' == ''", {"flag": ""}, True),
            ("{{s}} == 'a b'", {"s": "a b"}, True),
            ("{{s}} == \"it's\"", {"s": "it's"}, True),
            ("{{n}} > 3", {"n": 5}, True),
            ("{{n}} == 5", {"n": 5}, True),
            ("{{b}} == true", {"b": True}, True),
            ("not {{b}}", {"b": False}, True),
            ("{{z}} == ''", {"z": None}, False),
            ("{{z}} != ''", {"z": None}, True),
            ("not {{z}}", {"z": None}, True),
        ],
    )
    def test_evaluates(self, expression, context, expected):
        assert evaluate_condition(expression, context) is expected

    def test_a_pre_resolved_condition_evaluates_the_same_way(self):
        # This composition -- resolve, then evaluate -- is what the loop and
        # sub-step paths do, and is exactly what recipes-kft reported dying.
        context = {"continue_from": ""}
        resolved = substitute_condition_variables("{{continue_from}} == ''", context)
        assert evaluate_condition(resolved, context) is True


class TestExecutorPathsThatUsedToDie:
    """The three call sites, driven through the real executor."""

    @pytest.mark.asyncio
    async def test_while_condition_on_an_empty_string_default(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["body_ran"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="converge",
                    agent="a",
                    prompt="p",
                    while_condition="{{flag}} == ''",
                    update_context={"flag": "done"},
                    max_while_iterations=5,
                ),
            ],
            context={"flag": ""},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # One iteration: the gate was true on entry (it used to raise), then
        # update_context made it false.
        assert mock_spawn.call_count == 1
        assert result["flag"] == "done"

    @pytest.mark.asyncio
    async def test_sub_step_condition_on_a_value_with_a_space(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["inner_ran"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="loop",
                    while_condition="{{flag}} == ''",
                    while_steps=[
                        {
                            "id": "inner",
                            "agent": "a",
                            "prompt": "p",
                            "condition": "{{note}} == 'all clear'",
                        }
                    ],
                    update_context={"flag": "done"},
                    max_while_iterations=5,
                ),
            ],
            context={"flag": "", "note": "all clear"},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        assert mock_spawn.call_count == 1

    @pytest.mark.asyncio
    async def test_break_when_on_an_empty_string_default(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        mock_spawn = mock_coordinator.get_capability.return_value
        mock_spawn.side_effect = ["body_ran"]

        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="converge",
                    agent="a",
                    prompt="p",
                    while_condition="{{_loop_iteration}} < 9",
                    break_when="{{flag}} == ''",
                    max_while_iterations=5,
                ),
            ],
            context={"flag": "", "_loop_iteration": 0},
        )

        await executor.execute_recipe(recipe, {}, temp_dir)

        # break_when was true after the first body, so the loop stopped there
        # instead of warning "break_when expression error" and running on.
        assert mock_spawn.call_count == 1
