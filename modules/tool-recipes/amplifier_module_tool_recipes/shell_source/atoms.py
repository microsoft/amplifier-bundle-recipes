"""Find complete trusted expansion atoms without skipping DATA descendants."""

from __future__ import annotations

from collections.abc import Callable
import re

from tree_sitter import Node

from .envelopes import copied_envelope
from .model import BoundTemplate, ShellSourceError, Span
from .words import Expansion


def expansion_atoms(bound: BoundTemplate, root: Node, *, nested: Callable[[Span], str]) -> dict[int, Expansion]:
    """CST endpoints plus independent envelope checks; nested frames recurse."""
    source = bound.source.encode()
    result: dict[int, Expansion] = {}

    def walk(node: Node) -> None:
        span = Span(node.start_byte, node.end_byte)
        text = source[span.start:span.end].decode()
        holes = any(o.span.start < span.end and o.span.end > span.start for o in bound.occurrences)
        kind = node.type
        if kind in ("command_substitution", "arithmetic_expansion", "process_substitution"):
            if kind == "command_substitution" and text.startswith("`"):
                if holes:
                    raise ShellSourceError("DATA_BACKTICK", "backtick envelopes must be wholly trusted", span)
                copied_envelope(text, kind, span)
                result[span.start] = Expansion(span, text, kind)
                return
            if kind == "process_substitution" and holes:
                raise ShellSourceError("DATA_PROCESS_SUBSTITUTION", "process substitution envelopes must be wholly trusted", span)
            spelling = nested(span) if holes else text
            result[span.start] = Expansion(span, spelling, kind)
            for child in node.named_children:
                walk(child)
            return
        if kind in ("expansion", "simple_expansion", "ansi_c_string"):
            if holes:
                # A simple $name may end at a value fence. The word cooker
                # closes that original prefix; braced/special forms cannot.
                if kind != "simple_expansion":
                    raise ShellSourceError("DATA_EXPANSION", "context occurrence is inside a closed source envelope", span)
            else:
                copied_envelope(text, kind, span)
                result[span.start] = Expansion(span, text, kind)
                return
        if kind == "string" and text.startswith('$"'):
            if holes:
                raise ShellSourceError("DATA_TRANSLATED_STRING", "translated quote must be wholly trusted", span)
            # CST plus lexical collector independently owns its DQ boundary.
            result[span.start] = Expansion(span, text, "translated_string")
            return
        for child in node.named_children:
            walk(child)

    walk(root)
    # Bash's grammar exposes a flat brace list as several concatenated words,
    # not one brace node. Only this closed, hole-free original byte language is
    # an admitted opaque expansion; DATA braces remain literal characters.
    for match in re.finditer(rb"\{[A-Za-z0-9_./:+-]+(?:,[A-Za-z0-9_./:+-]+)+\}", source):
        span = Span(*match.span())
        if any(o.span.start < span.end and o.span.end > span.start for o in bound.occurrences):
            continue
        node = root.descendant_for_byte_range(span.start, span.end)
        if node.type not in ("word", "concatenation"):
            continue
        # Quoted braces are string content, and cannot reach this CST owner.
        result[span.start] = Expansion(span, match.group().decode(), "brace_expression")
    return result
