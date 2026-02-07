"""Safe expression evaluator for recipe conditions.

Supports boolean expressions with:
- Comparison: == != < > >= <=
- Boolean operators: and or not
- Parenthesized grouping: (expr)
- Variable references: {{variable}}
- String literals: 'value' or "value"
- Numeric literals: 42, 3.14

Implemented as a recursive descent parser with proper operator precedence:
  or (lowest) -> and -> not -> comparison -> atom (highest)

NO eval() or exec() - safe string parsing only.
"""

import re
from typing import Any


class ExpressionError(Exception):
    """Error evaluating condition expression."""

    pass


def evaluate_condition(expression: str, context: dict[str, Any]) -> bool:
    """Evaluate a condition expression against context.

    Args:
        expression: Condition string (e.g., "{{status}} == 'success'")
        context: Dictionary of variable values

    Returns:
        True if condition passes, False otherwise

    Raises:
        ExpressionError: On undefined variables or invalid syntax
    """
    if not expression or not expression.strip():
        return True  # Empty condition = always true

    # Substitute variables first
    substituted = _substitute_variables(expression, context)

    # Parse and evaluate the expression
    return _evaluate_expression(substituted.strip())


def _substitute_variables(expression: str, context: dict[str, Any]) -> str:
    """Replace {{variable}} references with their values."""
    pattern = re.compile(r"\{\{(\w+(?:\.\w+)*)\}\}")

    def replace_var(match: re.Match) -> str:
        var_path = match.group(1)
        value = _resolve_variable(var_path, context)
        if value is None:
            raise ExpressionError(f"Undefined variable: {var_path}")
        # Convert to string representation for comparison
        if isinstance(value, str):
            return f"'{value}'"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return pattern.sub(replace_var, expression)


def _resolve_variable(path: str, context: dict[str, Any]) -> Any:
    """Resolve dotted variable path (e.g., 'step.id')."""
    parts = path.split(".")
    value = context
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _evaluate_expression(expr: str) -> bool:
    """Evaluate substituted expression to boolean.

    Uses a recursive descent parser with proper operator precedence:
      or (lowest) -> and -> not -> comparison -> atom (highest)
    """
    expr = expr.strip()
    if not expr:
        return True

    tokens = _tokenize(expr)
    parser = _Parser(tokens)
    result = parser.parse()
    return result


def _parse_value(token: str) -> str | bool:
    """Parse a value token (string literal or boolean)."""
    token = token.strip()

    # String literal with single quotes
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1]

    # String literal with double quotes
    if token.startswith('"') and token.endswith('"'):
        return token[1:-1]

    # Boolean literals
    if token.lower() == "true":
        return True
    if token.lower() == "false":
        return False

    # Unquoted value (treat as string after variable substitution)
    return token


# ---------------------------------------------------------------------------
# Recursive descent parser stubs (to be implemented)
#
# These functions will replace the string-split approach in _evaluate_expression
# with a proper tokenizer + recursive descent parser supporting:
#   - Comparison operators: < > >= <= == !=
#   - Boolean operators: and, or, not (with correct precedence)
#   - Parenthesized grouping: (expr)
#   - Numeric comparison (numeric-first, fall back to string)
#   - Boolean normalization for truthy/falsy values
#
# Grammar (precedence low→high):
#   expression  := or_expr
#   or_expr     := and_expr ("or" and_expr)*
#   and_expr    := not_expr ("and" not_expr)*
#   not_expr    := "not" not_expr | comparison
#   comparison  := atom (("==" | "!=" | "<" | ">" | ">=" | "<=") atom)?
#   atom        := "(" expression ")" | string_literal | number | boolean | identifier
# ---------------------------------------------------------------------------


def _tokenize(expression: str) -> list[str]:
    """Tokenize an expression string into a list of tokens.

    Token types: string literals ('...'/\"...\"), operators (== != < > >= <=),
    keywords (and or not), parentheses, numbers, and bare identifiers.

    Args:
        expression: Substituted expression string (variables already replaced)

    Returns:
        List of token strings

    Raises:
        ExpressionError: On unterminated strings or invalid characters
    """
    tokens: list[str] = []
    i = 0
    n = len(expression)

    while i < n:
        ch = expression[i]

        # Skip whitespace
        if ch.isspace():
            i += 1
            continue

        # String literals (single or double quoted)
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n and expression[j] != quote:
                j += 1
            if j >= n:
                raise ExpressionError(
                    f"Unterminated string literal starting at position {i}"
                )
            # Include quotes in token so _parse_atom can identify it
            tokens.append(expression[i : j + 1])
            i = j + 1
            continue

        # Parentheses
        if ch in ("(", ")"):
            tokens.append(ch)
            i += 1
            continue

        # Two-character operators: ==, !=, >=, <=
        if i + 1 < n and expression[i : i + 2] in ("==", "!=", ">=", "<="):
            tokens.append(expression[i : i + 2])
            i += 2
            continue

        # Single-character operators: <, >
        if ch in ("<", ">"):
            tokens.append(ch)
            i += 1
            continue

        # Words (keywords and identifiers) and numbers
        if ch.isalnum() or ch == "_" or ch == ".":
            j = i
            while j < n and (expression[j].isalnum() or expression[j] in ("_", ".")):
                j += 1
            tokens.append(expression[i:j])
            i = j
            continue

        raise ExpressionError(f"Unexpected character '{ch}' at position {i}")

    return tokens


def _is_truthy(value: str) -> bool:
    """Determine if a string value is truthy using boolean normalization.

    Falsy values: 'false', 'False', '', '0', 'none', 'None'
    Truthy values: 'true', 'True', any other non-empty string

    Args:
        value: String value to test

    Returns:
        True if value is truthy, False otherwise
    """
    return value not in ("false", "False", "", "0", "none", "None")


def _try_numeric(value: str) -> float | None:
    """Attempt to parse a string as a number for numeric comparison.

    Args:
        value: String to attempt numeric parsing on

    Returns:
        Float value if parseable, None otherwise
    """
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


class _Parser:
    """Recursive descent parser for condition expressions.

    Consumes a token list and produces a boolean result using
    proper operator precedence.

    Attributes:
        tokens: List of token strings from _tokenize()
        pos: Current position in the token list
    """

    COMPARISON_OPS = ("==", "!=", "<", ">", ">=", "<=")

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> str | None:
        """Return current token without consuming, or None if at end."""
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _consume(self) -> str:
        """Consume and return current token."""
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse(self) -> bool:
        """Parse the full expression and return boolean result.

        Raises:
            ExpressionError: On syntax errors or unexpected tokens
        """
        result = self._parse_or()
        if self.pos < len(self.tokens):
            raise ExpressionError(
                f"Unexpected token '{self.tokens[self.pos]}' at position {self.pos}"
            )
        return result

    def _parse_or(self) -> bool:
        """Parse or-expression: and_expr ('or' and_expr)*."""
        left = self._parse_and()
        while self._peek() == "or":
            self._consume()  # eat 'or'
            right = self._parse_and()
            left = left or right
        return left

    def _parse_and(self) -> bool:
        """Parse and-expression: not_expr ('and' not_expr)*."""
        left = self._parse_not()
        while self._peek() == "and":
            self._consume()  # eat 'and'
            right = self._parse_not()
            left = left and right
        return left

    def _parse_not(self) -> bool:
        """Parse not-expression: 'not' not_expr | comparison."""
        if self._peek() == "not":
            self._consume()  # eat 'not'
            value = self._parse_not()  # recursive for chained not
            return not value
        return self._parse_comparison()

    def _parse_comparison(self) -> bool:
        """Parse comparison: atom (comp_op atom)?

        Supports: == != < > >= <=
        Numeric-first comparison (try float, fall back to string).
        """
        left_val = self._parse_atom()

        if self._peek() in self.COMPARISON_OPS:
            op = self._consume()
            right_val = self._parse_atom()

            # Strip quotes from string literals for comparison
            left_cmp = (
                left_val[1:-1]
                if (left_val.startswith("'") and left_val.endswith("'"))
                or (left_val.startswith('"') and left_val.endswith('"'))
                else left_val
            )
            right_cmp = (
                right_val[1:-1]
                if (right_val.startswith("'") and right_val.endswith("'"))
                or (right_val.startswith('"') and right_val.endswith('"'))
                else right_val
            )

            # Numeric-first comparison
            left_num = _try_numeric(left_cmp)
            right_num = _try_numeric(right_cmp)

            if left_num is not None and right_num is not None:
                # Both numeric - compare as numbers
                if op == "==":
                    return left_num == right_num
                if op == "!=":
                    return left_num != right_num
                if op == "<":
                    return left_num < right_num
                if op == ">":
                    return left_num > right_num
                if op == ">=":
                    return left_num >= right_num
                if op == "<=":
                    return left_num <= right_num
            else:
                # String comparison
                if op == "==":
                    return left_cmp == right_cmp
                if op == "!=":
                    return left_cmp != right_cmp
                if op == "<":
                    return left_cmp < right_cmp
                if op == ">":
                    return left_cmp > right_cmp
                if op == ">=":
                    return left_cmp >= right_cmp
                if op == "<=":
                    return left_cmp <= right_cmp

        # No comparison operator - interpret as boolean via truthiness
        # Strip quotes for truthiness check
        stripped = (
            left_val[1:-1]
            if (left_val.startswith("'") and left_val.endswith("'"))
            or (left_val.startswith('"') and left_val.endswith('"'))
            else left_val
        )
        return _is_truthy(stripped)

    def _parse_atom(self) -> str:
        """Parse atom: '(' expression ')' | literal | identifier.

        Returns the raw string value of the atom for comparison operators
        to consume.
        """
        token = self._peek()

        if token is None:
            raise ExpressionError("Unexpected end of expression")

        # Parenthesized sub-expression
        if token == "(":
            self._consume()  # eat '('
            result = self._parse_or()
            if self._peek() != ")":
                raise ExpressionError("Expected ')' to close parenthesized expression")
            self._consume()  # eat ')'
            # Return a synthetic token representing the boolean result
            return "true" if result else "false"

        # String literals (quoted)
        if (token.startswith("'") and token.endswith("'")) or (
            token.startswith('"') and token.endswith('"')
        ):
            self._consume()
            return token  # Return with quotes intact; _parse_comparison strips them

        # Keywords that are NOT operators get returned as values
        if token in ("and", "or", "not"):
            raise ExpressionError(f"Unexpected keyword '{token}' where value expected")

        if token in (")", "==", "!=", "<", ">", ">=", "<="):
            raise ExpressionError(f"Unexpected operator '{token}' where value expected")

        # Bare identifier / number / boolean literal
        self._consume()
        return token
