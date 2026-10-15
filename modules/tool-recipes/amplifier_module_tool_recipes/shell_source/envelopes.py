"""Closed trusted expansion envelopes, read without evaluating their values."""

from __future__ import annotations

import re

from .model import ShellSourceError, Span


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCALAR_HEAD = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[?$!#*\-])")


def parameter_end(text: str, start: int = 0, *, scalar: bool = False) -> int:
    """Read the approved closed ${...} language, with optional scalar proof.

    General copied parameters may contain array/indirection spelling. A prefix
    certificate admits only scalar heads and the eight scalar default operators.
    Quotes, backticks, escapes and unknown dollar forms are never skipped.
    """
    if not text.startswith("${", start):
        raise ShellSourceError("PARAMETER_ENVELOPE", "expected a braced parameter")
    index = start + 2
    if scalar:
        head = _SCALAR_HEAD.match(text, index)
        if head is None:
            raise ShellSourceError("NONSCALAR_PREFIX", "parameter head is not scalar")
        index = head.end()
        if text[index:index + 1] != "}":
            operator = next((op for op in (":-", ":+", ":=", ":?", "-", "+", "=", "?") if text.startswith(op, index)), None)
            if operator is None or not _NAME.fullmatch(head.group()):
                raise ShellSourceError("NONSCALAR_PREFIX", "parameter operator is not a scalar default operator")
            index += len(operator)
    while index < len(text):
        char = text[index]
        if char == "}":
            return index + 1
        if char in "'\"\\`{":
            raise ShellSourceError("PARAMETER_ENVELOPE", "parameter contains an unowned quote, escape or brace")
        if char == "$":
            if text.startswith("${", index):
                index = parameter_end(text, index, scalar=scalar)
                continue
            name = _NAME.match(text, index + 1)
            if name:
                index = name.end()
                continue
            if text[index + 1:index + 2] in tuple("0123456789?$!#*-" + ("" if scalar else "@")):
                index += 2
                continue
            raise ShellSourceError("PARAMETER_ENVELOPE", "unknown dollar form in parameter envelope")
        index += 1
    raise ShellSourceError("PARAMETER_ENVELOPE", "unclosed parameter envelope")


def scalar_expansion(text: str) -> bool:
    """A syntactically scalar expansion, intended only inside authored DQ."""
    try:
        if text.startswith("${"):
            return parameter_end(text, scalar=True) == len(text)
        return bool(re.fullmatch(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9?$!#*\-])", text))
    except ShellSourceError:
        return False


def copied_envelope(text: str, kind: str, span: Span) -> None:
    """Independently check the lexical endpoints of a hole-free opaque atom."""
    if kind == "expansion":
        if parameter_end(text) != len(text):
            raise ShellSourceError("PARAMETER_ENVELOPE", "parameter has an unowned suffix", span)
        return
    if kind == "simple_expansion":
        if re.fullmatch(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9@?$!#*\-])", text):
            return
    elif kind == "ansi_c_string":
        if text.startswith("$'"):
            index = 2
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                elif text[index] == "'":
                    if index == len(text) - 1:
                        return
                    break
                else:
                    index += 1
    elif kind == "brace_expression":
        if re.fullmatch(r"\{[A-Za-z0-9_./:+-]+(?:,[A-Za-z0-9_./:+-]+)+\}", text):
            return
    elif kind == "command_substitution" and text.startswith("`"):
        index = 1
        while index < len(text):
            if text[index] == "\\" and text[index + 1:index + 2] in ("\\", "$", "`", "\n"):
                index += 2
            elif text[index] == "`":
                if index == len(text) - 1:
                    return
                break
            else:
                index += 1
    raise ShellSourceError("UNSUPPORTED_SOURCE_ENVELOPE", f"unowned trusted {kind} envelope", span)
