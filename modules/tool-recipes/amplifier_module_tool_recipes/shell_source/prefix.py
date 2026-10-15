"""Source-derived nonvanishing prefixes prevent DATA command-name promotion."""

from __future__ import annotations

from tree_sitter import Node

from .binding import events_in
from .envelopes import copied_envelope, scalar_expansion
from .model import BoundTemplate, FieldProjection, ShellSourceError, Span
from .words import Expansion, cook_island


def _nodes(node: Node):
    yield node
    for child in node.named_children:
        yield from _nodes(child)


def certified_word(bound: BoundTemplate, node: Node) -> bool:
    """Verify one wholly trusted source word; never inspect runtime values."""
    span = Span(node.start_byte, node.end_byte)
    if any(
        o.span.start < span.end and o.span.end > span.start for o in bound.occurrences
    ):
        return False
    source = bound.source.encode()
    expansions: dict[int, Expansion] = {}
    for child in _nodes(node):
        if child.type not in ("expansion", "simple_expansion", "command_substitution"):
            continue
        spelling = source[child.start_byte : child.end_byte].decode()
        if child.type == "command_substitution":
            if not spelling.startswith("$(") or not spelling.endswith(")"):
                return False
            # Its actual child topology is checked in the complete-root CST.
        else:
            try:
                copied_envelope(
                    spelling, child.type, Span(child.start_byte, child.end_byte)
                )
            except ShellSourceError:
                return False
        expansions[child.start_byte] = Expansion(
            Span(child.start_byte, child.end_byte), spelling, child.type
        )
    try:
        cooked = cook_island(events_in(bound, span), "ARG", expansion_at=expansions.get)
    except ShellSourceError:
        return False
    if len(cooked.pieces) != 1 or not isinstance(cooked.pieces[0], FieldProjection):
        return False
    field = cooked.pieces[0]
    for part in field.parts:
        if part.kind == "expansion":
            if not part.quoted or not (
                scalar_expansion(part.text) or part.text.startswith("$(")
            ):
                return False
        elif not part.quoted and part.text in "*?[]~{}()":
            return False
    return field.forced or any(p.kind == "literal" and p.text for p in field.parts)


def require_prefix(bound: BoundTemplate, command: Node, first_occurrence: int) -> None:
    """A certified original word must precede the first possible DATA field."""
    candidates = []
    for index, child in enumerate(command.children):
        field = command.field_name_for_child(index)
        if field in ("name", "argument") and child.end_byte <= first_occurrence:
            candidates.append(child)
    # Declaration builtins have an anonymous fixed keyword instead of name.
    if command.type == "declaration_command":
        keyword = next((c for c in command.children if not c.is_named), None)
        if keyword is not None and keyword.end_byte <= first_occurrence:
            candidates.insert(0, keyword)
    for node in candidates:
        if not certified_word(bound, node):
            continue
        # The boundary after a certificate cannot be consumed by an authored
        # trailing escape. Cooked word qualification rejects that escape too.
        return
    raise ShellSourceError(
        "NONVANISHING_PREFIX",
        "no hole-free scalar word precedes the first data argument",
        Span(command.start_byte, command.end_byte),
    )
