"""Reparse the emitted program before allowing either engine to launch it.

Local word/document witnesses prove value semantics. This witness owns the
surrounding program: edits must reconstruct the actual output, stay within
their original slots, and preserve commands, names, operators, redirects and
nested executable frames. An outer word certificate never hides its commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tree_sitter import Node

from .binding import events_in
from .documents import DocumentOwnership, collect_documents
from .model import BoundTemplate, Replacement, ShellSourceError, Span, TextEvent
from .ownership import Ownership, parse
from .recovery import recovery_projection


def _fail(message: str, span: Span | None = None) -> None:
    raise ShellSourceError("PROGRAM_WITNESS", message, span)


def _span(node: Node) -> Span:
    return Span(node.start_byte, node.end_byte)


def _field(node: Node) -> str | None:
    if node.parent is None:
        return None
    for i, child in enumerate(node.parent.children):
        if child.id == node.id:
            return node.parent.field_name_for_child(i)
    _fail("parsed child is absent from its parent", _span(node))


def _role(node: Node) -> str | None:
    parent = node.parent
    if parent is None:
        return None
    field = _field(node)
    if parent.type in ("command", "declaration_command", "unset_command"):
        if field == "argument":
            return "ARG"
        if parent.type == "declaration_command" and node.type == "variable_assignment":
            return "DECLARG"
    if parent.type == "variable_assignment" and field == "value":
        return "ASSIGN"
    if parent.type == "file_redirect" and field == "destination":
        destinations = parent.children_by_field_name("destination")
        # The pinned grammar groups words after a redirect under that redirect.
        # Only its first destination is the file operand.
        return "FILE" if destinations[0].id == node.id else "ARG"
    if parent.type == "herestring_redirect" and field == "body":
        return "FILE"
    if parent.type == "for_statement" and field == "value":
        return "FOR_VALUES"
    if (
        parent.type in ("binary_expression", "unary_expression")
        and (
            field in ("left", "right", "argument")
            or parent.type == "unary_expression"
            and field is None
        )
        and node.type
        in (
            "word",
            "string",
            "raw_string",
            "concatenation",
            "simple_expansion",
            "expansion",
            "number",
            "command_substitution",
            "arithmetic_expansion",
        )
    ):
        ancestor = parent
        while ancestor is not None:
            if ancestor.type == "arithmetic_expansion":
                return None
            if ancestor.type == "test_command":
                return "TEST_DATA"
            ancestor = ancestor.parent
    return None


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


@dataclass(frozen=True)
class _Edit:
    replacement: Replacement
    output: Span


def _reconstruct(
    bound: BoundTemplate, replacements: tuple[Replacement, ...], candidate: str
) -> tuple[_Edit, ...]:
    source = bound.source.encode("utf-8")
    try:
        actual = candidate.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ShellSourceError(
            "PROGRAM_WITNESS", "emitted source is not valid UTF-8"
        ) from error
    if b"\x00" in actual:
        _fail("emitted source contains NUL")
    chunks: list[bytes] = []
    edits: list[_Edit] = []
    cursor = output = 0
    for replacement in replacements:
        span = replacement.source
        if (
            span.start < cursor
            or span.end > len(source)
            or (span.start == span.end and replacement.kind != "document-body")
        ):
            _fail(
                "final edits overlap, are unordered, empty, or leave the original source",
                span,
            )
        try:
            source[cursor : span.start].decode("utf-8")
            source[span.start : span.end].decode("utf-8")
            text = replacement.text.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise ShellSourceError(
                "PROGRAM_WITNESS",
                "edit splits a character or contains invalid text",
                span,
            ) from error
        prefix = source[cursor : span.start]
        chunks.extend((prefix, text))
        output += len(prefix)
        edits.append(_Edit(replacement, Span(output, output + len(text))))
        output += len(text)
        cursor = span.end
    chunks.append(source[cursor:])
    if b"".join(chunks) != actual:
        _fail("actual emitted source differs from the verified edit reconstruction")
    for occurrence in bound.occurrences:
        if not any(
            edit.replacement.source.start <= occurrence.span.start
            and occurrence.span.end <= edit.replacement.source.end
            for edit in edits
        ):
            _fail("an original occurrence has no final edit owner", occurrence.span)
    return tuple(edits)


def _mapped(position: int, edits: tuple[_Edit, ...]) -> int | None:
    delta = 0
    for edit in edits:
        old = edit.replacement.source
        if old.start == old.end and position >= old.end:
            delta += edit.output.end - edit.output.start
            continue
        if position <= old.start:
            return position + delta
        if position < old.end:
            return None  # A composed outer word owns this nested coordinate.
        delta += (edit.output.end - edit.output.start) - (old.end - old.start)
    return position + delta


def _edit_owners(ownership: Ownership, edits: tuple[_Edit, ...]) -> None:
    for edit in edits:
        replacement = edit.replacement
        if replacement.kind in ("word", "number", "comment"):
            owner = next(
                (
                    owner
                    for owner in ownership.words
                    if owner.span == replacement.source
                    and owner.role == replacement.role
                ),
                None,
            )
            if owner is None or owner.role == "NAME":
                _fail(
                    "final word edit does not have its claimed original owner",
                    replacement.source,
                )
            kinds = {
                "NUMBER": ("number",),
                "TEST_NUMBER": ("number", "word"),
                "COMMENT": ("comment",),
            }
            if replacement.kind not in kinds.get(owner.role, ("word",)):
                _fail(
                    "final edit kind does not match its original owner",
                    replacement.source,
                )
        elif replacement.kind == "document-body":
            if not any(
                document.body == replacement.source
                for document in ownership.documents.documents
            ):
                _fail(
                    "final document body edit has no original FIFO owner",
                    replacement.source,
                )
        elif replacement.kind == "document-delimiter":
            if not any(
                replacement.source
                in (document.opener_delimiter, document.closer_delimiter)
                for document in ownership.documents.documents
            ):
                _fail(
                    "final delimiter edit has no original FIFO owner",
                    replacement.source,
                )
        else:
            _fail("unknown final replacement kind", replacement.source)


def _tail_boundaries(
    bound: BoundTemplate, ownership: Ownership, edits: tuple[_Edit, ...]
) -> None:
    source = bound.source.encode("utf-8")
    for edit in edits:
        replacement = edit.replacement
        count = replacement.tail_lfs
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            _fail("invalid declared final DATA line-feed count", replacement.source)
        if not count:
            continue
        if replacement.kind != "word" or replacement.role not in ("ARG", "FILE"):
            _fail(
                "final DATA line feeds have no ordinary word owner", replacement.source
            )
        if len(replacement.text) - len(replacement.text.rstrip("\n")) != count:
            _fail(
                "actual emitted line-feed tail differs from its declared count",
                replacement.source,
            )
        if source[replacement.source.end : replacement.source.end + 1] not in (
            b"",
            b"\n",
        ):
            _fail(
                "final DATA line feeds precede authored executable source",
                replacement.source,
            )
        if any(
            document.body.start == replacement.source.end + 1
            for document in ownership.documents.documents
        ):
            _fail(
                "pending document requires its separate header/body handoff",
                replacement.source,
            )
        events = [
            event
            for event in events_in(bound, replacement.source)
            if isinstance(event, TextEvent)
        ]
        actual = 0
        for event in reversed(events):
            if event.kind != "data" or event.text != "\n":
                break
            if ownership.documents.contexts[event.source.start].mode != "ordinary":
                _fail(
                    "quoted DATA line feed cannot become a command terminator",
                    event.source,
                )
            actual += 1
        if count != actual:
            _fail(
                "declared final line feeds do not own the exact original DATA suffix",
                replacement.source,
            )


def _logical_body(source: bytes, strip_tabs: bool) -> bytes:
    """Read actual unquoted document source, including read-time continuation."""
    result = bytearray()
    index = 0
    while index < len(source):
        if source[index : index + 1] == b"\\" and index + 1 < len(source):
            if source[index + 1] == 10:
                index += 2
                continue
            result.extend(source[index : index + 2])
            index += 2
        else:
            result.append(source[index])
            index += 1
    value = bytes(result)
    return (
        b"\n".join(line.lstrip(b"\t") for line in value.split(b"\n"))
        if strip_tabs
        else value
    )


def _documents(
    original: DocumentOwnership,
    emitted: DocumentOwnership,
    candidate: bytes,
    edits: tuple[_Edit, ...],
) -> None:
    if len(original.documents) != len(emitted.documents):
        _fail("emitted document count differs from original FIFO ownership")
    for left, right in zip(original.documents, emitted.documents):
        if (left.frame, left.ordinal, left.quoted, left.strip_tabs) != (
            right.frame,
            right.ordinal,
            right.quoted,
            right.strip_tabs,
        ):
            _fail(
                "emitted document changed frame, FIFO order, quoting, or tab mode",
                right.opener_delimiter,
            )
        for old, new in (
            (left.opener_delimiter, right.opener_delimiter),
            (left.closer_delimiter, right.closer_delimiter),
            (left.body, right.body),
        ):
            exact_edit = next(
                (edit for edit in edits if edit.replacement.source == old), None
            )
            start, end = (
                (exact_edit.output.start, exact_edit.output.end)
                if exact_edit is not None
                else (_mapped(old.start, edits), _mapped(old.end, edits))
            )
            if (
                start is not None
                and start != new.start
                or end is not None
                and end != new.end
            ):
                _fail(
                    "emitted document no longer owns the original header/body/closer boundary",
                    new,
                )
        can_rename = any(
            edit.replacement.source.start <= left.opener_delimiter.start
            and left.opener_delimiter.end <= edit.replacement.source.end
            for edit in edits
        )
        if not can_rename and left.delimiter != right.delimiter:
            _fail("an unowned document delimiter changed", right.opener_delimiter)
        body = candidate[right.body.start : right.body.end]
        logical = body if right.quoted else _logical_body(body, right.strip_tabs)
        if body and not logical.endswith(b"\n"):
            _fail(
                "emitted document consumes its authored closing-line boundary",
                right.body,
            )
        lines = logical.split(b"\n")
        if right.strip_tabs and right.quoted:
            lines = [line.lstrip(b"\t") for line in lines]
        if right.delimiter.encode("ascii") in lines:
            _fail(
                "emitted document closes early after read-time continuation", right.body
            )


def _parse_projection(
    bound: BoundTemplate, documents: DocumentOwnership, *, allowed_recovery: bool
) -> tuple[Node, tuple[Span, ...], bytes]:
    try:
        return parse(documents.skeleton), (), documents.skeleton
    except ShellSourceError:
        if not allowed_recovery:
            raise
        recovered = recovery_projection(bound, documents.skeleton)
        if recovered is None:
            raise
        projected, markers = recovered
        return parse(projected), markers, projected


def _empty_for_projection(
    original_root: Node, projected: bytearray, edits: tuple[_Edit, ...]
) -> None:
    """Recover the pinned grammar's rejection of a valid empty ``in`` list.

    Only an original FOR_VALUES owner lowered to no fields qualifies. The actual
    source keeps its ``in`` (and therefore zero iterations). The width-preserving
    parse projection erases that exact keyword; the root witness still compares
    the loop variable/body/operators, and never executes this projection.
    """
    for edit in edits:
        if (
            edit.replacement.kind != "word"
            or edit.replacement.role != "FOR_VALUES"
            or edit.replacement.text.strip(" \t")
        ):
            continue
        owner = edit.replacement.source
        loops = [
            node
            for node in _walk(original_root)
            if node.type == "for_statement"
            and any(
                node.start_byte <= owner.start
                and owner.end <= node.end_byte
                and child.start_byte >= owner.start
                and child.end_byte <= owner.end
                for child in node.children_by_field_name("value")
            )
        ]
        if len(loops) != 1:
            _fail("empty for-list has no unique original loop owner", owner)
        keyword = next(
            (child for child in loops[0].children if child.type == "in"), None
        )
        if keyword is None:
            _fail("empty for-list lost its original in keyword", owner)
        start, end = (
            _mapped(keyword.start_byte, edits),
            _mapped(keyword.end_byte, edits),
        )
        if start is None or end is None or projected[start:end] != b"in":
            _fail("empty for-list changed its original in keyword", owner)
        if bytes(projected[end : edit.output.start]).strip(b" \t"):
            _fail(
                "empty for-list does not immediately follow its original in keyword",
                owner,
            )
        projected[start:end] = b"  "


def _slot_boundaries(root: Node, source: bytes, edits: tuple[_Edit, ...]) -> None:
    nodes = tuple(_walk(root))
    for edit in edits:
        replacement, span = edit.replacement, edit.output
        span = Span(span.start, span.end - replacement.tail_lfs)
        if replacement.kind in ("document-body", "document-delimiter"):
            continue
        text = source[span.start : span.end]
        if replacement.kind == "number":
            decimal = rb"[+-]?(?:0|[1-9][0-9]*)"
            signed_rvalue = rb"\([+-](?:0|[1-9][0-9]*)\)"
            if not re.fullmatch(decimal, text) and not (
                replacement.role == "NUMBER" and re.fullmatch(signed_rvalue, text)
            ):
                _fail("numeric replacement is not a closed decimal source token", span)
            if not any(
                node.start_byte == span.start and node.end_byte == span.end
                for node in nodes
            ):
                _fail("numeric replacement is not a complete emitted node", span)
            continue
        if replacement.kind == "comment":
            if not any(
                node.type == "comment" and _span(node) == span for node in nodes
            ):
                _fail("comment replacement escaped its physical comment owner", span)
            continue
        if replacement.kind != "word":
            _fail("unknown final replacement kind", replacement.source)
        role = "TEST_DATA" if replacement.role == "TEST_NUMBER" else replacement.role
        if not text.strip(b" \t"):
            if role not in ("ARG", "FOR_VALUES", "ASSIGN"):
                _fail("a required emitted operand disappeared", span)
            continue
        # Outermost slots inside the edit cover the actual emitted source.
        # Nested commands are checked by the structural walk below.
        slots: list[Node] = []
        for node in nodes:
            actual_role = _role(node)
            if actual_role is None:
                continue
            actual_role = "ARG" if actual_role == "DECLARG" else actual_role
            overlaps = node.start_byte < span.end and span.start < node.end_byte
            if not overlaps:
                continue
            if node.start_byte < span.start or node.end_byte > span.end:
                # An unchanged outer word can enclose an edited nested command
                # argument. Only complete inner slots may cover this edit. A
                # word crossing the boundary has no such complete coverage.
                continue
            if any(
                parent.start_byte <= node.start_byte
                and node.end_byte <= parent.end_byte
                for parent in slots
            ):
                continue
            if actual_role != role:
                _fail("emitted data changed its original word role", _span(node))
            slots.append(node)
        slots.sort(key=lambda node: node.start_byte)
        cursor = span.start
        for node in slots:
            if source[cursor : node.start_byte].strip(b" \t"):
                _fail(
                    "emitted word region contains unowned syntax",
                    Span(cursor, node.start_byte),
                )
            cursor = node.end_byte
        if not slots or source[cursor : span.end].strip(b" \t"):
            _fail(
                "emitted word region does not consist of its original word slots", span
            )
        if role not in ("ARG", "FOR_VALUES") and len(slots) != 1:
            _fail("single-operand owner became multiple emitted operands", span)


class _Program:
    def __init__(
        self,
        source: bytes,
        documents: DocumentOwnership,
        numbers: frozenset[Span],
        authored: bytes | None = None,
    ):
        self.source = source
        self.documents = documents
        self.numbers = numbers
        self.authored = authored

    def raw(self, node: Node) -> bytes:
        return self.source[node.start_byte : node.end_byte]

    def inside_word(self, node: Node) -> tuple:
        if node.type in ("simple_expansion", "expansion"):
            if node.type == "simple_expansion" and self.authored is not None:
                # Neutral placeholder letters can extend a parameter token in
                # the original CST. Its actual authored name stops at the
                # occurrence fence, before any DATA or adjacent trusted text.
                raw = self.authored[node.start_byte : node.end_byte]
                match = re.match(rb"\$([A-Za-z_][A-Za-z_0-9]*|[0-9@*#?$!_-])", raw)
                if match is not None:
                    return (("parameter", match.group(1)),)
            name = next(
                (
                    child
                    for child in node.named_children
                    if child.type in ("variable_name", "special_variable_name")
                ),
                None,
            )
            if name is not None and self.raw(node) in (
                b"$" + self.raw(name),
                b"${" + self.raw(name) + b"}",
            ):
                return (("parameter", self.raw(name)),)
            return (self.key(node, ignore_role=True),)
        if node.type in (
            "command_substitution",
            "process_substitution",
            "arithmetic_expansion",
        ):
            return (self.key(node, ignore_role=True),)
        result = []
        for child in node.children:
            result.extend(self.inside_word(child))
        return tuple(result)

    def key(self, node: Node, *, ignore_role: bool = False):
        if _span(node) in self.numbers:
            return ("number-data",)
        role = None if ignore_role else _role(node)
        if role is not None and role != "DECLARG":
            document = next(
                (
                    d
                    for d in self.documents.documents
                    if node.start_byte <= d.opener_delimiter.start
                    and d.opener_delimiter.end <= node.end_byte
                ),
                None,
            )
            if document is not None:
                return (
                    "document-operand",
                    document.frame,
                    document.ordinal,
                    document.quoted,
                    document.strip_tabs,
                )
            nested = self.inside_word(node)
            if role in ("ARG", "FOR_VALUES"):
                # Field count/protection belongs to the independent word
                # reader. Keep every executable expansion in source order.
                return ("word-executables", nested) if nested else None
            return (role, nested)
        if node.type == "comment":
            return ("comment",)
        if node.type in ("command_name", "variable_name", "special_variable_name"):
            return (node.type, self.raw(node))
        if not node.children:
            return (node.type, self.raw(node))
        children = []
        for i, child in enumerate(node.children):
            if node.type == "for_statement" and child.type == "in":
                # Exact reconstruction preserves the keyword in real source;
                # the closed empty-list projection above only aids parsing.
                continue
            key = self.key(child)
            if key is not None:
                if key[0] == "word-executables":
                    children.extend(
                        (node.field_name_for_child(i), ("word-executable", nested))
                        for nested in key[1]
                    )
                else:
                    children.append((node.field_name_for_child(i), key))
        return (node.type, tuple(children))


def verify_program(
    bound: BoundTemplate,
    ownership: Ownership,
    replacements: tuple[Replacement, ...],
    candidate: str,
) -> None:
    """Raise before launch if actual emitted source loses its original owners.

    This is supplementary to independent word/document value witnesses, never an
    alternative to them. ``candidate`` is parsed as actual Bash, without rescanning
    it for template occurrences or executing a shell.
    """
    edits = _reconstruct(bound, replacements, candidate)
    _edit_owners(ownership, edits)
    _tail_boundaries(bound, ownership, edits)
    raw = candidate.encode("utf-8")
    emitted_bound = BoundTemplate(candidate, (), ())
    emitted_docs = collect_documents(emitted_bound)
    _documents(ownership.documents, emitted_docs, raw, edits)
    original_root, old_markers, original_source = _parse_projection(
        bound, ownership.documents, allowed_recovery=bool(ownership.recovery_markers)
    )
    if old_markers != ownership.recovery_markers:
        _fail("emitted source changed the admitted whole-root recovery language")
    original_bytes = bound.source.encode("utf-8")
    projected = bytearray(emitted_docs.skeleton)
    for old in old_markers:
        start, end = _mapped(old.start, edits), _mapped(old.end, edits)
        if start is None or end is None:
            _fail("a word edit absorbed an executable recovery marker", old)
        new = Span(start, end)
        if original_bytes[old.start : old.end] != raw[new.start : new.end]:
            _fail(
                "emitted recovery marker differs from its original executable bytes",
                new,
            )
        if (start and raw[start - 1 : start] != b"\n") or (
            end < len(raw) and raw[end : end + 1] != b"\n"
        ):
            _fail("an executable recovery marker lost its original line boundary", new)
        projected[start:end] = b" " * (end - start)
    _empty_for_projection(original_root, projected, edits)
    emitted_source = bytes(projected)
    emitted_root = parse(emitted_source)
    _slot_boundaries(emitted_root, emitted_source, edits)
    old_numbers = frozenset(
        edit.replacement.source for edit in edits if edit.replacement.kind == "number"
    )
    new_numbers = frozenset(
        edit.output for edit in edits if edit.replacement.kind == "number"
    )
    left = _Program(
        original_source, ownership.documents, old_numbers, original_bytes
    ).key(original_root)
    right = _Program(emitted_source, emitted_docs, new_numbers).key(emitted_root)
    if left != right:
        _fail("emitted executable structure differs from the original program")
