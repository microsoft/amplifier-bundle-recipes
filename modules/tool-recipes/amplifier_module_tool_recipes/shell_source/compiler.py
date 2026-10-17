"""Compile context DATA inside complete original shell owners before launch."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .atoms import expansion_atoms
from .binding import events_in
from .encoding import LiteralAtom, encode_field
from .heredoc import compile_document
from .model import (
    BoundTemplate,
    FenceEvent,
    FieldFence,
    FieldProjection,
    Replacement,
    ShellSourceError,
    Span,
    TextEvent,
)
from .ownership import Ownership, WordOwner, establish_ownership, parse
from .recovery import recovery_projection
from .word_witness import verify_field
from .words import FieldSeparator, cook_island


@dataclass(frozen=True)
class CompiledSource:
    source: str
    ownership: Ownership
    replacements: tuple[Replacement, ...]


def _apply(source: bytes, span: Span, edits: tuple[Replacement, ...]) -> str:
    cursor = span.start
    pieces = []
    for edit in sorted(edits, key=lambda item: item.source.start):
        if edit.source.start < cursor or edit.source.end > span.end:
            raise ShellSourceError(
                "COMPOSITION_OWNER",
                "nested edits cross or leave their owning interval",
                edit.source,
            )
        pieces.extend((source[cursor : edit.source.start], edit.text.encode()))
        cursor = edit.source.end
    pieces.append(source[cursor : span.end])
    return b"".join(pieces).decode()


def _numeric(bound: BoundTemplate, owner: WordOwner) -> Replacement:
    events = events_in(bound, owner.span)
    cooked = cook_island(events, owner.role)
    fields = [part for part in cooked.pieces if isinstance(part, FieldProjection)]
    if len(fields) != 1 or any(part.kind != "literal" for part in fields[0].parts):
        raise ShellSourceError(
            "NUMERIC_DOMAIN", "numeric operand must be one decimal literal", owner.span
        )
    value = "".join(part.text for part in fields[0].parts)
    if not re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)", value, flags=re.ASCII):
        raise ShellSourceError(
            "NUMERIC_DOMAIN",
            "numeric context value must be a strict ASCII decimal string",
            owner.span,
        )
    if owner.role == "NUMBER":
        # No numeric concatenation, name/subscript or mixed authored token.
        if (
            len(owner.occurrences) != 1
            or bound.occurrences[owner.occurrences[0]].span != owner.span
        ):
            raise ShellSourceError(
                "NUMERIC_OWNER",
                "context occurrence must own a complete arithmetic rvalue",
                owner.span,
            )
        value = "(" + value + ")" if value.startswith(("+", "-")) else value
    return Replacement(owner.span, value, "number", owner.role, tuple(fields))


def _raw_envelopes(field: FieldProjection, plan) -> tuple[LiteralAtom, ...]:
    """Map raw finder intervals through retained events to complete parts."""
    raw_parts = []
    offsets = {}
    raw_offset = 0
    for local, event in enumerate(field.source_events):
        if isinstance(event, TextEvent):
            width = len(event.text.encode())
            offsets[local + field.source_event_start] = Span(
                raw_offset, raw_offset + width
            )
            raw_parts.append(event.text)
            raw_offset += width
    raw = "".join(raw_parts).encode()
    part_offsets = []
    position = 0
    for part in field.parts:
        part_offsets.append(Span(position, position + len(part.text.encode())))
        position = part_offsets[-1].end
    atoms = []
    for envelope in plan.envelopes:
        members = [
            index
            for index, part in enumerate(field.parts)
            if any(
                event in offsets
                and offsets[event].start < envelope.span.end
                and offsets[event].end > envelope.span.start
                for event in part.events
            )
        ]
        if not members:
            raise ShellSourceError(
                "TILDE_TRANSPORT", "raw envelope has no cooked owner"
            )
        first, last = min(members), max(members)
        raw_members = [
            offsets[event]
            for part in field.parts[first : last + 1]
            for event in part.events
            if event in offsets
        ]
        begin = min(envelope.span.start, *(span.start for span in raw_members))
        end = max(envelope.span.end, *(span.end for span in raw_members))
        atoms.append(
            LiteralAtom(
                Span(part_offsets[first].start, part_offsets[last].end),
                raw[begin:end].decode(),
                False,
            )
        )
    return tuple(atoms)


def _without_suffix(
    field: FieldProjection, count: int
) -> tuple[FieldProjection, tuple[TextEvent, ...]]:
    removed = field.parts[-count:]
    ids = {event for part in removed for event in part.events}
    tail = tuple(
        event
        for local, event in enumerate(field.source_events)
        if local + field.source_event_start in ids and isinstance(event, TextEvent)
    )
    parts = field.parts[:-count]
    width = sum(len(part.text.encode()) for part in parts)
    fences = tuple(
        FieldFence(f.occurrence, f.edge, min(f.position, width)) for f in field.fences
    )
    retained = tuple(
        event
        for local, event in enumerate(field.source_events)
        if local + field.source_event_start not in ids
    )
    return replace(field, parts=parts, fences=fences, source_events=retained), tail


def compile_bound(bound: BoundTemplate) -> CompiledSource:
    ownership = establish_ownership(bound)
    source = bound.source.encode()
    projection = ownership.documents.skeleton
    if ownership.recovery_markers:
        recovered = recovery_projection(bound, projection)
        assert recovered is not None
        projection = recovered[0]
    root = parse(projection)
    cache: dict[Span, Replacement] = {}
    handoffs: dict[int, tuple[TextEvent, ...]] = {}

    def document_edits() -> list[Replacement]:
        edits: list[Replacement] = []
        for original in ownership.documents.documents:
            document = replace(
                original, events=handoffs.get(original.ordinal, ()) + original.events
            )
            if not handoffs.get(original.ordinal) and not any(
                isinstance(event, FenceEvent) for event in document.events
            ):
                continue
            encoded = compile_document(document)
            edits.append(
                Replacement(document.body, encoded.body, "document-body", "BODY")
            )
            if encoded.delimiter != document.delimiter:
                edits.append(
                    Replacement(
                        document.opener_delimiter,
                        encoded.delimiter,
                        "document-delimiter",
                        "DELIMITER",
                    )
                )
                # Keep authored physical line boundaries in a logical closer.
                # The delimiter identifier changes; its continuations do not.
                spelling = document.closer_spelling or document.delimiter
                pieces = spelling.split("\\\n")
                cursor = 0
                closing = ""
                for piece in pieces[:-1]:
                    end = min(cursor + len(piece), len(encoded.delimiter))
                    closing += encoded.delimiter[cursor:end] + "\\\n"
                    cursor = end
                closing += encoded.delimiter[cursor:]
                edits.append(
                    Replacement(
                        document.closer_delimiter,
                        closing,
                        "document-delimiter",
                        "DELIMITER",
                    )
                )
        return edits

    def nested(span: Span) -> str:
        contained = [
            owner
            for owner in ownership.words
            if span.start <= owner.span.start and owner.span.end <= span.end
        ]
        outermost = [
            owner
            for owner in contained
            if not any(
                other.span != owner.span
                and other.span.start <= owner.span.start
                and owner.span.end <= other.span.end
                for other in contained
            )
        ]
        edits = [word(owner) for owner in outermost]
        edits.extend(
            edit
            for edit in document_edits()
            if span.start <= edit.source.start
            and edit.source.end <= span.end
            and not any(
                owner.span.start <= edit.source.start
                and edit.source.end <= owner.span.end
                for owner in outermost
            )
        )
        return _apply(source, span, tuple(edits))

    # Resolve nested expansions lazily: building a global map here would
    # compile an owner while trying to discover that same owner's child atoms.
    def word(owner: WordOwner) -> Replacement:
        if owner.span in cache:
            return cache[owner.span]
        if owner.role == "NAME":
            raise ShellSourceError(
                "FORBIDDEN_SLOT",
                "context occurrence cannot own command, assignment, descriptor or grammar names",
                owner.span,
            )
        if owner.role in ("NUMBER", "TEST_NUMBER"):
            result = _numeric(bound, owner)
            cache[owner.span] = result
            return result
        events = events_in(bound, owner.span)
        if owner.role == "COMMENT":
            text = "".join(
                event.text.replace("\n", "\\n") if event.kind == "data" else event.text
                for event in events
                if isinstance(event, TextEvent)
            )
            result = Replacement(owner.span, text, "comment", owner.role)
            cache[owner.span] = result
            return result
        node = root.descendant_for_byte_range(owner.span.start, owner.span.end)
        # A multi-argument island's smallest CST parent may include the command
        # itself. Only proper nested atoms can invoke recursive compilation.
        atoms = expansion_atoms(
            bound,
            node,
            nested=lambda span: (
                nested(span)
                if span != owner.span
                else source[span.start : span.end].decode()
            ),
        )
        if owner.role == "TEST_REGEX":
            from .regex import parameter_atoms

            atoms.update(parameter_atoms(bound, owner.span))
        cooked = cook_island(events, owner.role, expansion_at=atoms.get)
        if owner.role == "TEST_REGEX":
            from .regex import compile_regex

            result = compile_regex(owner.span, cooked, raw_envelopes=_raw_envelopes)
            cache[owner.span] = result
            return result
        pieces = list(cooked.pieces)
        if not pieces and owner.role == "ASSIGN":
            pieces = [FieldProjection((), (), "ASSIGN", True, None, events, 0)]
        tail_lfs = 0
        dead_whitespace = ""
        if pieces and isinstance(pieces[-1], FieldProjection):
            last = pieces[-1]
            for part in reversed(last.parts):
                if (
                    part.kind == "literal"
                    and part.data
                    and not part.quoted
                    and part.text == "\n"
                ):
                    tail_lfs += 1
                else:
                    break
            if tail_lfs:
                if owner.role not in ("ARG", "FILE") or source[
                    owner.span.end : owner.span.end + 1
                ] not in (b"", b"\n"):
                    raise ShellSourceError(
                        "TAIL_LF_OWNER",
                        "data LF is not at an admitted final argument boundary",
                        owner.span,
                    )
                trimmed, tail = _without_suffix(last, tail_lfs)
                pending = [
                    doc
                    for doc in ownership.documents.documents
                    if doc.frame == owner.frame
                    and doc.opener_delimiter.end <= owner.span.start
                    and doc.body.start == owner.span.end + 1
                ]
                if pending:
                    spelling = "".join(part.text for part in trimmed.parts)
                    if any(
                        part.kind != "literal" or part.quoted for part in trimmed.parts
                    ) or not re.fullmatch(r"[A-Za-z0-9_./:-]+", spelling):
                        raise ShellSourceError(
                            "HEADER_LF_OWNER",
                            "document header LF handoff requires a nonempty raw-inert prefix",
                            owner.span,
                        )
                    document = min(pending, key=lambda doc: doc.ordinal)
                    handoffs[document.ordinal] = tail
                    tail_lfs = 0
                pieces[-1] = trimmed
                if not trimmed.parts and not trimmed.forced:
                    pieces.pop()
            if owner.role == "ASSIGN":
                last = pieces[-1]
                count = 0
                for part in reversed(last.parts):
                    if (
                        part.kind == "literal"
                        and not part.quoted
                        and part.text in (" ", "\t")
                    ):
                        count += 1
                    else:
                        break
                if count:
                    trimmed, tail = _without_suffix(last, count)
                    dead_whitespace = "".join(part.text for part in last.parts[-count:])
                    pieces[-1] = replace(trimmed, forced=True)
        fields = [part for part in pieces if isinstance(part, FieldProjection)]
        if owner.role in ("FILE", "TEST_DATA") and (
            len(fields) != 1 or not fields[0].parts and not fields[0].forced
        ):
            raise ShellSourceError(
                "SCALAR_DOMAIN",
                "filename/test operand requires one nonempty unquoted field or an authored quoted field",
                owner.span,
            )
        for field in fields:
            for part in field.parts:
                if (
                    part.kind == "literal"
                    and not part.quoted
                    and part.text in (" ", "\t", "\n")
                ):
                    raise ShellSourceError(
                        "SCALAR_DOMAIN",
                        "unquoted whitespace is not admissible inside this scalar field",
                        owner.span,
                    )
        rendered = []
        for piece in pieces:
            if isinstance(piece, FieldSeparator):
                rendered.append(piece.text)
                continue
            # The tilde component owns raw request/state proof. No raw tilde
            # fallback is available if it is missing during development.
            from .tilde import plan_tildes, verify_tildes

            plan = plan_tildes(piece)
            if all(
                isinstance(event, TextEvent) and event.kind == "trusted"
                for event in piece.source_events
            ):
                # An ARG island can include authored neighboring operands.
                # Preserve their spelling, including trusted test operators;
                # only fields with actual DATA/fences require source lowering.
                text = "".join(event.text for event in piece.source_events)
            else:
                text = encode_field(piece, preserved=_raw_envelopes(piece, plan))
            verify_field(piece, text)
            verify_tildes(piece, text)
            rendered.append(text)
        result = Replacement(
            owner.span,
            "".join(rendered) + dead_whitespace + "\n" * tail_lfs,
            "word",
            owner.role,
            tuple(fields),
            tail_lfs,
        )
        cache[owner.span] = result
        return result

    all_span = Span(0, len(source))
    candidate = nested(all_span)
    final_words = [
        owner
        for owner in ownership.words
        if not any(
            other.span != owner.span
            and other.span.start <= owner.span.start
            and owner.span.end <= other.span.end
            for other in ownership.words
        )
    ]
    edits = [cache[owner.span] for owner in final_words]
    edits.extend(
        edit
        for edit in document_edits()
        if not any(
            owner.span.start <= edit.source.start and edit.source.end <= owner.span.end
            for owner in final_words
        )
    )
    edits.sort(key=lambda item: item.source.start)
    from .witness import verify_program

    verify_program(bound, ownership, tuple(edits), candidate)
    return CompiledSource(candidate, ownership, tuple(edits))
