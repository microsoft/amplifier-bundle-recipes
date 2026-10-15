"""CST-backed owners for original recipe occurrences, before DATA emission."""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Language, Node, Parser
import tree_sitter_bash

from .documents import DocumentOwnership, collect_documents
from .model import BoundTemplate, ShellSourceError, Span
from .recovery import recovery_projection


@dataclass(frozen=True)
class WordOwner:
    span: Span
    role: str
    frame: int
    occurrences: tuple[int, ...]
    # The command spelling is original source, not a resolved runtime command.
    command: str | None = None
    operator: str | None = None
    command_span: Span | None = None


@dataclass(frozen=True)
class Ownership:
    documents: DocumentOwnership
    words: tuple[WordOwner, ...]
    recovery_markers: tuple[Span, ...] = ()


def parse(source: bytes) -> Node:
    parser = Parser(Language(tree_sitter_bash.language()))
    root = parser.parse(source).root_node
    if root.has_error:
        raise ShellSourceError("INVALID_TEMPLATE_SYNTAX", "original or emitted Bash source has an error/missing CST node", Span(root.start_byte, root.end_byte))
    return root


def _field(node: Node) -> str | None:
    if node.parent is None:
        return None
    for index, child in enumerate(node.parent.children):
        if child.id == node.id:
            return node.parent.field_name_for_child(index)
    raise ShellSourceError("CST_OWNERSHIP", "CST node is absent from its parent")


def _command(node: Node, source: bytes) -> str | None:
    current = node
    while current is not None:
        if current.type in ("command", "declaration_command", "unset_command"):
            name = current.child_by_field_name("name")
            if name is None:
                name = next((c for c in current.children if not c.is_named), None)
            return source[name.start_byte:name.end_byte].decode() if name is not None else None
        current = current.parent
    return None


def _command_node(node: Node) -> Node | None:
    return _ancestor(node, ("command", "declaration_command", "unset_command"))


def _island(node: Node, role: str, source: bytes) -> Span:
    parent = node.parent
    if parent is None or role not in ("ARG", "FOR_VALUES"):
        return Span(node.start_byte, node.end_byte)
    field = "argument" if role == "ARG" else "value"
    candidates = [child for index, child in enumerate(parent.children)
                  if parent.field_name_for_child(index) == field
                  or (role == "ARG" and parent.type == "declaration_command" and child.type == "variable_assignment")]
    if node not in candidates:
        return Span(node.start_byte, node.end_byte)
    position = candidates.index(node)
    first = last = position
    while first and all(c in b" \t" for c in source[candidates[first - 1].end_byte:candidates[first].start_byte]):
        first -= 1
    while last + 1 < len(candidates) and all(c in b" \t" for c in source[candidates[last].end_byte:candidates[last + 1].start_byte]):
        last += 1
    return Span(candidates[first].start_byte, candidates[last].end_byte)


def _ancestor(node: Node, types: tuple[str, ...]) -> Node | None:
    current = node
    while current is not None:
        if current.type in types:
            return current
        current = current.parent
    return None


def _choose(node: Node, source: bytes, arithmetic: bool) -> tuple[Node, str, str | None]:
    # A nested command argument cannot launder DATA into the outer command
    # name. Check the complete ancestor chain before choosing the nearest ARG.
    if _ancestor(node, ("command_name",)) is not None:
        return node, "NAME", None
    unsupported = _ancestor(node, ("process_substitution", "herestring_redirect", "ansi_c_string", "translated_string", "brace_expression"))
    if unsupported is not None:
        raise ShellSourceError("UNSUPPORTED_SOURCE_ENVELOPE", f"context occurrence is inside {unsupported.type}", Span(unsupported.start_byte, unsupported.end_byte))
    expansion = _ancestor(node, ("expansion",))
    if expansion is not None:
        raise ShellSourceError("DATA_PARAMETER_SYNTAX", "context data cannot complete a braced parameter envelope", Span(expansion.start_byte, expansion.end_byte))
    current = node
    while current is not None:
        parent = current.parent
        field = _field(current)
        if current.type == "comment":
            return current, "COMMENT", None
        if parent is not None:
            if parent.type == "command_name" or current.type == "command_name":
                return current, "NAME", None
            if parent.type == "variable_assignment":
                if field == "value":
                    if parent.parent is not None and parent.parent.type == "declaration_command":
                        return parent, "ARG", None
                    return current, "ASSIGN", None
                return current, "NAME", None
            if parent.type in ("file_redirect", "herestring_redirect") and field in ("destination", "body"):
                return current, "FILE", None
            if parent.type in ("command", "declaration_command", "unset_command") and field == "argument":
                return current, "ARG", None
            if parent.type == "for_statement":
                return current, "FOR_VALUES" if field == "value" else "NAME", None
            if parent.type in ("binary_expression", "unary_expression") and _ancestor(parent, ("test_command",)) is not None:
                operator = parent.child_by_field_name("operator")
                spelling = source[operator.start_byte:operator.end_byte].decode() if operator is not None else None
                return current, "TEST_NUMBER" if spelling in ("-eq", "-ne", "-lt", "-le", "-gt", "-ge") else "TEST_DATA", spelling
            if arithmetic and current.type in ("variable_name", "number"):
                return current, "NUMBER", None
        current = parent
    raise ShellSourceError("UNOWNED_OCCURRENCE", "context occurrence has no supported original word owner", Span(node.start_byte, node.end_byte))


def establish_ownership(bound: BoundTemplate) -> Ownership:
    documents = collect_documents(bound)
    recovery_markers: tuple[Span, ...] = ()
    try:
        root = parse(documents.skeleton)
    except ShellSourceError as error:
        if error.code != "INVALID_TEMPLATE_SYNTAX":
            raise
        recovered = recovery_projection(bound, documents.skeleton)
        if recovered is None:
            raise
        projection, recovery_markers = recovered
        root = parse(projection)
    grouped: dict[tuple[int, int, str, int, str | None, str | None, tuple[int, int] | None], list[int]] = {}
    for occurrence in bound.occurrences:
        context = documents.contexts[occurrence.span.start]
        if context.mode == "heredoc":
            continue
        node = root.descendant_for_byte_range(occurrence.span.start, occurrence.span.end)
        owner, role, operator = _choose(node, documents.skeleton, context.mode == "arithmetic")
        if owner.start_byte > occurrence.span.start or owner.end_byte < occurrence.span.end:
            raise ShellSourceError("SPLIT_OCCURRENCE", "CST owner does not contain the entire occurrence", occurrence.span)
        command = _command(owner, documents.skeleton)
        span = _island(owner, role, documents.skeleton)
        command_node = _command_node(owner)
        command_span = (command_node.start_byte, command_node.end_byte) if command_node is not None else None
        key = (span.start, span.end, role, context.frame, command, operator, command_span)
        grouped.setdefault(key, []).append(occurrence.id)
    words = tuple(WordOwner(Span(start, end), role, frame, tuple(ids), command, operator, Span(*command_span) if command_span is not None else None) for (start, end, role, frame, command, operator, command_span), ids in sorted(grouped.items()))
    for left, right in zip(words, words[1:]):
        if left.span.start < right.span.end and right.span.start < left.span.end:
            # An outer DQ word can own holes beside a nested command whose ARG
            # owns other holes. The compiler must recurse through these owners;
            # neither may absorb the other's DATA/events.
            if not (left.span.start <= right.span.start and left.span.end >= right.span.end):
                raise ShellSourceError("CROSSING_OWNERS", "original word owners cross rather than nest", right.span)
    return Ownership(documents, words, recovery_markers)
