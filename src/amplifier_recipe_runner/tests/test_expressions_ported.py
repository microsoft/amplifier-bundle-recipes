"""The legacy engine's expression tests, run against the library's copy.

Ported verbatim from ``modules/tool-recipes/tests/test_expression_evaluator.py``
-- only the import line differs. That is the whole point: the library vendors
the evaluator (``amplifier_recipe_runner.expressions``), and a vendored copy is
only trustworthy while somebody proves it still answers the way the original
does. This file is that proof, and it fails the moment either copy drifts.

Parity matrix: docs/EXECUTOR_PARITY.md, row "condition / while_condition /
break_when".
"""

import pytest

from amplifier_recipe_runner.expressions import ExpressionError
from amplifier_recipe_runner.expressions import evaluate_condition


class TestBasicComparisons:
    """Tests for == and != operators."""

    def test_string_equality_single_quotes(self):
        """Variable equals string literal with single quotes."""
        ctx = {"status": "success"}
        assert evaluate_condition("{{status}} == 'success'", ctx) is True
        assert evaluate_condition("{{status}} == 'failure'", ctx) is False

    def test_string_equality_double_quotes(self):
        """Variable equals string literal with double quotes."""
        ctx = {"status": "success"}
        assert evaluate_condition('{{status}} == "success"', ctx) is True
        assert evaluate_condition('{{status}} == "failure"', ctx) is False

    def test_string_inequality(self):
        """Variable not equals string literal."""
        ctx = {"status": "success"}
        assert evaluate_condition("{{status}} != 'failure'", ctx) is True
        assert evaluate_condition("{{status}} != 'success'", ctx) is False

    def test_comparison_with_spaces(self):
        """Comparisons handle whitespace gracefully."""
        ctx = {"value": "test"}
        assert evaluate_condition("{{value}}=='test'", ctx) is True
        assert evaluate_condition("{{value}} == 'test'", ctx) is True
        assert evaluate_condition("  {{value}}  ==  'test'  ", ctx) is True


class TestBooleanOperators:
    """Tests for and/or operators."""

    def test_and_both_true(self):
        """Both conditions true with 'and'."""
        ctx = {"a": "yes", "b": "yes"}
        assert evaluate_condition("{{a}} == 'yes' and {{b}} == 'yes'", ctx) is True

    def test_and_one_false(self):
        """One condition false with 'and'."""
        ctx = {"a": "yes", "b": "no"}
        assert evaluate_condition("{{a}} == 'yes' and {{b}} == 'yes'", ctx) is False

    def test_or_one_true(self):
        """One condition true with 'or'."""
        ctx = {"a": "yes", "b": "no"}
        assert evaluate_condition("{{a}} == 'yes' or {{b}} == 'yes'", ctx) is True

    def test_or_both_false(self):
        """Both conditions false with 'or'."""
        ctx = {"a": "no", "b": "no"}
        assert evaluate_condition("{{a}} == 'yes' or {{b}} == 'yes'", ctx) is False

    def test_chained_and(self):
        """Multiple 'and' conditions chained."""
        ctx = {"a": "1", "b": "2", "c": "3"}
        assert evaluate_condition("{{a}} == '1' and {{b}} == '2' and {{c}} == '3'", ctx) is True
        assert evaluate_condition("{{a}} == '1' and {{b}} == '2' and {{c}} == '4'", ctx) is False

    def test_chained_or(self):
        """Multiple 'or' conditions chained."""
        ctx = {"a": "1", "b": "2", "c": "3"}
        assert evaluate_condition("{{a}} == '9' or {{b}} == '9' or {{c}} == '3'", ctx) is True

    def test_and_or_precedence(self):
        """'and' has higher precedence than 'or'."""
        ctx = {"a": "yes", "b": "no", "c": "yes"}
        # 'and' binds tighter: (a==yes) or ((b==yes) and (c==yes))
        # = yes or (no and yes) = yes or no = True
        assert evaluate_condition("{{a}} == 'yes' or {{b}} == 'yes' and {{c}} == 'yes'", ctx) is True


class TestVariableSubstitution:
    """Tests for variable reference handling."""

    def test_simple_variable(self):
        """Simple variable reference."""
        ctx = {"name": "test"}
        assert evaluate_condition("{{name}} == 'test'", ctx) is True

    def test_nested_variable(self):
        """Dotted path variable reference."""
        ctx = {"user": {"name": "alice"}}
        assert evaluate_condition("{{user.name}} == 'alice'", ctx) is True

    def test_deeply_nested_variable(self):
        """Multiple levels of nesting."""
        ctx = {"a": {"b": {"c": "deep"}}}
        assert evaluate_condition("{{a.b.c}} == 'deep'", ctx) is True

    def test_undefined_variable_raises(self):
        """Undefined variable raises ExpressionError."""
        ctx = {"defined": "value"}
        with pytest.raises(ExpressionError, match="Undefined variable"):
            evaluate_condition("{{undefined}} == 'value'", ctx)

    def test_undefined_nested_variable_raises(self):
        """Undefined nested variable raises ExpressionError."""
        ctx = {"user": {"name": "alice"}}
        with pytest.raises(ExpressionError, match="Undefined variable"):
            evaluate_condition("{{user.email}} == 'test'", ctx)


class TestBooleanLiterals:
    """Tests for true/false literals."""

    def test_boolean_variable_true(self):
        """Boolean variable compared to true literal."""
        ctx = {"enabled": True}
        assert evaluate_condition("{{enabled}} == true", ctx) is True
        assert evaluate_condition("{{enabled}} == false", ctx) is False

    def test_boolean_variable_false(self):
        """Boolean variable compared to false literal."""
        ctx = {"disabled": False}
        assert evaluate_condition("{{disabled}} == false", ctx) is True
        assert evaluate_condition("{{disabled}} == true", ctx) is False

    def test_boolean_literal_case_insensitive(self):
        """Boolean literals are case-insensitive."""
        ctx = {"flag": True}
        assert evaluate_condition("{{flag}} == TRUE", ctx) is True
        assert evaluate_condition("{{flag}} == True", ctx) is True
        assert evaluate_condition("{{flag}} == true", ctx) is True


class TestEdgeCases:
    """Tests for edge cases and special situations."""

    def test_empty_condition_returns_true(self):
        """Empty or whitespace-only condition returns True."""
        ctx = {}
        assert evaluate_condition("", ctx) is True
        assert evaluate_condition("   ", ctx) is True
        assert evaluate_condition(None, ctx) is True  # type: ignore

    def test_string_with_spaces(self):
        """String values with spaces handled correctly."""
        ctx = {"message": "hello world"}
        assert evaluate_condition("{{message}} == 'hello world'", ctx) is True

    def test_comparing_two_variables(self):
        """Comparing two variable values."""
        ctx = {"a": "same", "b": "same"}
        assert evaluate_condition("{{a}} == {{b}}", ctx) is True
        ctx = {"a": "one", "b": "two"}
        assert evaluate_condition("{{a}} == {{b}}", ctx) is False

    def test_invalid_syntax_raises(self):
        """Invalid expression syntax raises ExpressionError."""
        ctx = {"x": "value"}
        with pytest.raises(ExpressionError, match="Invalid expression"):
            evaluate_condition("{{x}} >>> 'value'", ctx)

    def test_number_as_string(self):
        """Numeric values are converted to strings for comparison."""
        ctx = {"count": 42}
        assert evaluate_condition("{{count}} == '42'", ctx) is True


class TestRecipePatterns:
    """Tests matching common recipe usage patterns."""

    def test_classification_routing(self):
        """Pattern: route based on classification result."""
        ctx = {"classification": "complex"}
        assert evaluate_condition("{{classification}} == 'simple'", ctx) is False
        assert evaluate_condition("{{classification}} == 'complex'", ctx) is True

    def test_severity_check(self):
        """Pattern: check severity level."""
        ctx = {"severity": "critical"}
        assert evaluate_condition("{{severity}} == 'critical' or {{severity}} == 'high'", ctx) is True

    def test_status_and_type_check(self):
        """Pattern: multiple conditions for processing."""
        ctx = {"status": "complete", "type": "report"}
        cond = "{{status}} == 'complete' and {{type}} == 'report'"
        assert evaluate_condition(cond, ctx) is True

    def test_fallback_condition(self):
        """Pattern: default case (always-true condition)."""
        ctx = {"anything": "whatever"}
        # Empty condition = always run
        assert evaluate_condition("", ctx) is True
