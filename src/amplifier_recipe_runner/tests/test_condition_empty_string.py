"""Conditions resolve VALUES, not text -- library side (recipes-kft).

Mirrors ``modules/tool-recipes/tests/test_condition_empty_string.py``
scenario for scenario. The library's evaluator is a verbatim copy of the
legacy one and its loop paths resolve condition text the same way, so the
empty-string defect and its fix are shared: both copies must render a value
as a well-formed *literal*, never paste it in bare.

Nothing here spawns a model: every step is a bash step.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from amplifier_recipe_runner.engine import RecipeProgram
from amplifier_recipe_runner.engine import StepEngine
from amplifier_recipe_runner.engine import parse_program
from amplifier_recipe_runner.expressions import ExpressionError
from amplifier_recipe_runner.expressions import evaluate_condition
from amplifier_recipe_runner.expressions import substitute_condition_variables


def _program(body: str) -> RecipeProgram:
    return parse_program(yaml.safe_load(textwrap.dedent(body)))


def _run(body: str, *, workspace: Path, context: dict[str, Any]) -> dict[str, Any]:
    """Execute ``body`` and return the final context."""

    async def _never_called(step: Any, instruction: str, ctx: Any) -> Any:  # pragma: no cover
        raise AssertionError("no agent step in this test")

    ctx = dict(context)
    engine = StepEngine(
        _program(body),
        invoke_agent=_never_called,
        workspace=workspace,
        run_id="run-kft",
    )
    asyncio.run(engine.execute(ctx))
    return ctx


class TestConditionSubstitutionRenders:
    def test_empty_string_renders_as_an_empty_literal(self) -> None:
        assert substitute_condition_variables("{{flag}} == ''", {"flag": ""}) == "'' == ''"

    def test_string_with_spaces_renders_as_one_literal(self) -> None:
        assert substitute_condition_variables("{{s}} == 'a b'", {"s": "a b"}) == "'a b' == 'a b'"

    def test_string_containing_a_quote_is_escaped(self) -> None:
        assert substitute_condition_variables("{{s}} == 'x'", {"s": "it's"}) == "'it\\'s' == 'x'"

    def test_number_renders_bare(self) -> None:
        assert substitute_condition_variables("{{n}} > 3", {"n": 5}) == "5 > 3"

    def test_bool_renders_as_a_lowercase_literal(self) -> None:
        assert substitute_condition_variables("{{b}}", {"b": True}) == "true"
        assert substitute_condition_variables("{{b}}", {"b": False}) == "false"

    def test_none_renders_as_null(self) -> None:
        assert substitute_condition_variables("{{z}} == ''", {"z": None}) == "null == ''"

    def test_a_reference_the_context_lacks_is_still_an_error(self) -> None:
        with pytest.raises(ExpressionError, match="Undefined variable: nope"):
            substitute_condition_variables("{{nope}} == ''", {"flag": ""})

    def test_a_reference_the_author_quoted_is_not_double_quoted(self) -> None:
        assert substitute_condition_variables("'{{flag}}' == ''", {"flag": ""}) == "'' == ''"
        assert (
            substitute_condition_variables('"{{s}}" != ""', {"s": 'say "hi"'})
            == '"say \\"hi\\"" != ""'
        )

    def test_substitution_is_stable_when_applied_twice(self) -> None:
        once = substitute_condition_variables("{{s}} == 'x'", {"s": "a'b"})
        assert substitute_condition_variables(once, {"s": "a'b"}) == once


class TestConditionEvaluatesForEveryValueType:
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
    def test_evaluates(self, expression: str, context: dict[str, Any], expected: bool) -> None:
        assert evaluate_condition(expression, context) is expected

    def test_a_pre_resolved_condition_evaluates_the_same_way(self) -> None:
        context = {"continue_from": ""}
        resolved = substitute_condition_variables("{{continue_from}} == ''", context)
        assert evaluate_condition(resolved, context) is True


class TestEnginePathsThatUsedToDie:
    def test_top_level_condition_on_an_empty_string_default(self, tmp_path: Path) -> None:
        ctx = _run(
            """
            steps:
              - id: gate
                type: bash
                condition: "{{flag}} == ''"
                command: echo ran
                output: gate_out
            """,
            workspace=tmp_path,
            context={"flag": ""},
        )
        assert ctx["gate_out"] == "ran\n"

    def test_while_condition_on_an_empty_string_default(self, tmp_path: Path) -> None:
        ctx = _run(
            """
            steps:
              - id: converge
                type: bash
                while_condition: "{{flag}} == ''"
                update_context:
                  flag: done
                command: echo tick
                collect: ticks
                max_while_iterations: 5
            """,
            workspace=tmp_path,
            context={"flag": ""},
        )
        assert ctx["ticks"] == ["tick\n"]
        assert ctx["flag"] == "done"

    def test_sub_step_condition_on_a_value_with_a_space(self, tmp_path: Path) -> None:
        ctx = _run(
            """
            steps:
              - id: loop
                while_condition: "{{flag}} == ''"
                update_context:
                  flag: done
                max_while_iterations: 5
                steps:
                  - id: inner
                    type: bash
                    condition: "{{note}} == 'all clear'"
                    command: echo inner
                    output: inner_out
            """,
            workspace=tmp_path,
            context={"flag": "", "note": "all clear"},
        )
        assert ctx["inner_out"] == "inner\n"

    def test_break_when_on_an_empty_string_default(self, tmp_path: Path) -> None:
        ctx = _run(
            """
            steps:
              - id: converge
                type: bash
                while_condition: "{{_loop_iteration}} < 9"
                break_when: "{{flag}} == ''"
                command: echo tick
                collect: ticks
                max_while_iterations: 5
            """,
            workspace=tmp_path,
            context={"flag": "", "_loop_iteration": 0},
        )
        # break_when was true after the first body, so the loop stopped there
        # instead of swallowing an expression error and running to the limit.
        assert ctx["ticks"] == ["tick\n"]
