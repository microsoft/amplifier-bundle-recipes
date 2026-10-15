"""Complete-body heredoc lowering with an independent emitted-source reader.

The enclosing compiler owns headers, FIFO queues and shell frames. This module
owns the supplied complete body and its paired delimiter. It never executes a
shell or resolves a value. Empty occurrence fences are syntax boundaries even
when an authored escape consumes a character on the other side of them.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

from .model import (
    EncodedDocument,
    EventDisposition,
    FenceEvent,
    HereDocument,
    OutputOrigin,
    ShellSourceError,
    Span,
    TextEvent,
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z", re.ASCII)
_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_NAME = _NAME_START | frozenset("0123456789")
_SPECIAL_DOLLAR = frozenset("0123456789@*#?-$!([")
_ESCAPED = frozenset("\\$`")
_PARAMETER_TAIL = re.compile(
    r"(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))", re.ASCII
)


@dataclass(frozen=True)
class _Atom:
    kind: str
    spelling: str
    members: tuple[int, ...]


def _fail(code: str, message: str, doc: HereDocument) -> None:
    raise ShellSourceError(code, message, doc.body)


def _validate(doc: HereDocument) -> None:
    if not _IDENTIFIER.fullmatch(doc.delimiter) or doc.strip_tabs:
        _fail(
            "UNSUPPORTED_HEREDOC_HEADER",
            "requires a whole identifier delimiter without tab stripping",
            doc,
        )
    width = len(doc.delimiter.encode("utf-8"))
    if (
        doc.opener_delimiter.end - doc.opener_delimiter.start != width
        or doc.closer_delimiter.end - doc.closer_delimiter.start != width
        or doc.opener_delimiter.end > doc.body.start
        or doc.body.end > doc.closer_delimiter.start
    ):
        _fail(
            "BODY_OWNERSHIP_CONFLICT", "opener, body and closer ownership overlap", doc
        )
    text = [(i, e) for i, e in enumerate(doc.events) if isinstance(e, TextEvent)]
    if doc.terminal_lf_source is None:
        if doc.body.start != doc.body.end:
            _fail(
                "BODY_OWNERSHIP_CONFLICT",
                "nonempty authored body lacks its final LF identity",
                doc,
            )
    else:
        terminal = [
            (i, e)
            for i, e in text
            if e.kind == "trusted" and e.source == doc.terminal_lf_source
        ]
        if (
            len(terminal) != 1
            or terminal[0][1].text != "\n"
            or terminal[0] != text[-1]
            or doc.terminal_lf_source.end != doc.body.end
        ):
            _fail(
                "BODY_OWNERSHIP_CONFLICT",
                "final authored LF identity is not the body's last character",
                doc,
            )


def _has_values(doc: HereDocument) -> bool:
    return any(isinstance(e, FenceEvent) or e.kind == "data" for e in doc.events)


def _continuations(doc: HereDocument) -> tuple[list[int], set[int]]:
    kept: list[int] = []
    consumed: set[int] = set()
    i = 0
    while i < len(doc.events):
        e = doc.events[i]
        if isinstance(e, TextEvent) and e.kind == "trusted" and e.text == "\\":
            j = i + 1
            while j < len(doc.events) and isinstance(doc.events[j], FenceEvent):
                j += 1
            if j == len(doc.events):
                _fail(
                    "BODY_OWNERSHIP_CONFLICT",
                    "authored escape has no following character",
                    doc,
                )
            nxt = doc.events[j]
            assert isinstance(nxt, TextEvent)
            if nxt.text == "\n":
                if nxt.kind == "trusted" and nxt.source == doc.terminal_lf_source:
                    _fail(
                        "BODY_OWNERSHIP_CONFLICT",
                        "authored continuation consumes the final body LF",
                        doc,
                    )
                kept.extend(range(i + 1, j))
                consumed.update((i, j))
            else:
                kept.extend(range(i, j + 1))
            i = j + 1
        else:
            kept.append(i)
            i += 1
    return kept, consumed


def _atoms(doc: HereDocument, kept: list[int]) -> list[_Atom]:
    atoms: list[_Atom] = []
    p = 0
    while p < len(kept):
        index = kept[p]
        event = doc.events[index]
        if isinstance(event, FenceEvent):
            p += 1
            continue
        char = event.text
        if event.kind == "data":
            atoms.append(_Atom("literal", char, (index,)))
            p += 1
            continue
        if char == "\\":
            q = p + 1
            while q < len(kept) and isinstance(doc.events[kept[q]], FenceEvent):
                q += 1
            if q == len(kept):
                _fail(
                    "BODY_OWNERSHIP_CONFLICT",
                    "open escape after continuation filtering",
                    doc,
                )
            nxt = doc.events[kept[q]]
            assert isinstance(nxt, TextEvent)
            if nxt.text == "\n":
                _fail("BODY_OWNERSHIP_CONFLICT", "uncooked continuation remains", doc)
            atoms.append(_Atom("escape", "\\" + nxt.text, (index, kept[q])))
            p = q + 1
            continue
        if char == "`":
            _fail(
                "UNSUPPORTED_TRUSTED_EXPANSION",
                "active backticks in a value-bearing body",
                doc,
            )
        if char == "$" and p + 1 < len(kept):
            nxt = doc.events[kept[p + 1]]
            if isinstance(nxt, TextEvent) and nxt.kind == "trusted":
                q = p + 1
                if nxt.text in _NAME_START:
                    while q < len(kept):
                        e = doc.events[kept[q]]
                        if (
                            not isinstance(e, TextEvent)
                            or e.kind != "trusted"
                            or e.text not in _NAME
                        ):
                            break
                        q += 1
                    members = tuple(kept[p:q])
                    atoms.append(
                        _Atom(
                            "parameter",
                            "".join(doc.events[x].text for x in members),
                            members,
                        )
                    )
                    p = q
                    continue
                if nxt.text == "{":
                    q += 1
                    name = ""
                    while q < len(kept):
                        e = doc.events[kept[q]]
                        if not isinstance(e, TextEvent) or e.kind != "trusted":
                            break
                        if e.text == "}":
                            break
                        name += e.text
                        q += 1
                    if (
                        q == len(kept)
                        or not isinstance(doc.events[kept[q]], TextEvent)
                        or doc.events[kept[q]].kind != "trusted"
                        or doc.events[kept[q]].text != "}"
                        or not _IDENTIFIER.fullmatch(name)
                    ):
                        _fail(
                            "MIXED_OR_UNSUPPORTED_PARAMETER",
                            "parameter must be wholly authored and simple",
                            doc,
                        )
                    members = tuple(kept[p : q + 1])
                    atoms.append(_Atom("parameter", "${" + name + "}", members))
                    p = q + 1
                    continue
                if nxt.text in _SPECIAL_DOLLAR:
                    _fail(
                        "UNSUPPORTED_TRUSTED_EXPANSION",
                        "only simple named parameters are supported in this body",
                        doc,
                    )
        atoms.append(_Atom("literal", char, (index,)))
        p += 1
    return atoms


def _logical_source(body: str) -> str:
    """Heredoc collection semantics, before parameter/escape expansion."""
    result: list[str] = []
    p = 0
    while p < len(body):
        if body[p] == "\\" and p + 1 < len(body):
            if body[p + 1] != "\n":
                result.extend(body[p : p + 2])
            p += 2
        else:
            result.append(body[p])
            p += 1
    return "".join(result)


def _delimiter(doc: HereDocument, body: str) -> str:
    logical = body if doc.quoted else _logical_source(body)
    if logical and not logical.endswith("\n"):
        _fail(
            "BODY_OWNERSHIP_CONFLICT",
            "encoded body does not end before its owned closer",
            doc,
        )
    lines = set(body.split("\n")) | set(logical.split("\n"))
    if doc.delimiter not in lines:
        return doc.delimiter
    number = 0
    while f"__AMP_HDOC_{number}__" in lines:
        number += 1
    return f"__AMP_HDOC_{number}__"


def _zero_positions(count: int, owned: dict[int, list[Span]], end: int) -> list[int]:
    positions = [end] * count
    nearest = end
    for index in range(count - 1, -1, -1):
        positions[index] = nearest
        if index in owned:
            nearest = min(nearest, *(s.start for s in owned[index]))
    return positions


def compile_document(doc: HereDocument) -> EncodedDocument:
    """Lower exactly one already-owned document and verify its actual bytes."""
    _validate(doc)
    plain = doc.quoted or not _has_values(doc)
    if plain:
        atoms = [
            _Atom("plain", e.text, (i,))
            for i, e in enumerate(doc.events)
            if isinstance(e, TextEvent)
        ]
        consumed: set[int] = set()
    else:
        kept, consumed = _continuations(doc)
        atoms = _atoms(doc, kept)
    chunks: list[str] = []
    origins: list[OutputOrigin] = []
    owned: dict[int, list[Span]] = {}
    cooked: set[int] = set()
    offset = 0

    def emit(value: str, members: tuple[int, ...] = ()) -> None:
        nonlocal offset
        if not value:
            return
        span = Span(offset, offset + len(value.encode("utf-8")))
        chunks.append(value)
        origins.append(OutputOrigin(span, members))
        for member in members:
            owned.setdefault(member, []).append(span)
        offset = span.end

    for a, atom in enumerate(atoms):
        if atom.kind in {"plain", "literal"}:
            if atom.kind == "literal" and atom.spelling in _ESCAPED:
                emit("\\")
            emit(atom.spelling, atom.members)
        elif atom.kind == "escape":
            emit(atom.spelling[0], (atom.members[0],))
            emit(atom.spelling[1], (atom.members[1],))
            cooked.update(atom.members)
        else:
            spelling = atom.spelling
            next_start = ""
            if a + 1 < len(atoms):
                following = atoms[a + 1]
                next_start = following.spelling[0]
                if following.kind == "literal" and next_start in _ESCAPED:
                    next_start = "\\"
            brace = not spelling.startswith("${") and (
                next_start in _NAME or (next_start and not next_start.isascii())
            )
            for n, (char, member) in enumerate(
                zip(spelling, atom.members, strict=True)
            ):
                emit(char, (member,))
                if brace and n == 0:
                    emit("{")
            if brace:
                emit("}")
            cooked.update(atom.members)
    body = "".join(chunks)
    dispositions: list[EventDisposition] = []
    zero_positions = _zero_positions(len(doc.events), owned, offset)
    for i, event in enumerate(doc.events):
        if i in owned:
            spans = owned[i]
            dispositions.append(
                EventDisposition(
                    i,
                    Span(min(s.start for s in spans), max(s.end for s in spans)),
                    "cooked" if i in cooked else "emitted",
                )
            )
        else:
            position = zero_positions[i]
            reason = "fence" if isinstance(event, FenceEvent) else "continuation"
            if reason == "continuation" and i not in consumed:
                _fail("ORIGIN_TRANSPORT", "a text event has no disposition", doc)
            dispositions.append(EventDisposition(i, Span(position, position), reason))
    result = EncodedDocument(
        _delimiter(doc, body), body, tuple(origins), tuple(dispositions)
    )
    verify_document(doc, result)
    return result


def _intended(
    doc: HereDocument,
) -> tuple[list[tuple[str, str, tuple[int, ...]]], set[int]]:
    """Independent input reader: first collect characters, then read tokens.

    This does not call the compiler's continuation, atom or emitter functions.
    Fences survive collection, and remain hard boundaries for token recognition.
    """
    collected: list[tuple[int, TextEvent | FenceEvent]] = []
    removed: set[int] = set()
    iterator = iter(enumerate(doc.events))
    for index, event in iterator:
        if (
            isinstance(event, FenceEvent)
            or event.kind != "trusted"
            or event.text != "\\"
        ):
            collected.append((index, event))
            continue
        between: list[tuple[int, FenceEvent]] = []
        for second, following in iterator:
            if isinstance(following, FenceEvent):
                between.append((second, following))
                continue
            if following.text == "\n":
                if (
                    following.kind == "trusted"
                    and following.source == doc.terminal_lf_source
                ):
                    _fail(
                        "BODY_OWNERSHIP_CONFLICT",
                        "witness found consumption of final LF",
                        doc,
                    )
                removed.update((index, second))
                collected.extend(between)
            else:
                collected.append((index, event))
                collected.extend(between)
                collected.append((second, following))
            break
        else:
            _fail(
                "BODY_OWNERSHIP_CONFLICT", "witness found an unterminated escape", doc
            )
    meaning: list[tuple[str, str, tuple[int, ...]]] = []
    trusted_spelling = "".join(
        e.text if isinstance(e, TextEvent) and e.kind == "trusted" else "\x00"
        for _, e in collected
    )
    position = 0
    while position < len(collected):
        index, event = collected[position]
        position += 1
        if isinstance(event, FenceEvent):
            continue
        if event.kind == "data":
            meaning.append(("literal", event.text, (index,)))
            continue
        if event.text == "\\":
            while position < len(collected) and isinstance(
                collected[position][1], FenceEvent
            ):
                position += 1
            if position == len(collected):
                _fail("BODY_OWNERSHIP_CONFLICT", "witness has no escape target", doc)
            second, following = collected[position]
            position += 1
            assert isinstance(following, TextEvent)
            if following.text in _ESCAPED:
                meaning.append(("literal", following.text, (index, second)))
            else:
                meaning.extend(
                    (
                        ("literal", "\\", (index,)),
                        ("literal", following.text, (second,)),
                    )
                )
            continue
        if event.text == "`":
            _fail(
                "UNSUPPORTED_TRUSTED_EXPANSION",
                "witness found active trusted backticks",
                doc,
            )
        if event.text != "$":
            meaning.append(("literal", event.text, (index,)))
            continue
        match = _PARAMETER_TAIL.match(trusted_spelling, position)
        if match:
            count = len(match.group(0))
            owners = (index,) + tuple(
                i for i, _ in collected[position : position + count]
            )
            meaning.append(("parameter", match.group(1) or match.group(2), owners))
            position += count
        elif trusted_spelling[position : position + 1] == "{":
            _fail(
                "MIXED_OR_UNSUPPORTED_PARAMETER",
                "witness found a non-simple or mixed parameter",
                doc,
            )
        elif (
            position < len(trusted_spelling)
            and trusted_spelling[position] in _SPECIAL_DOLLAR
        ):
            _fail(
                "UNSUPPORTED_TRUSTED_EXPANSION",
                "witness found an unsupported dollar expansion",
                doc,
            )
        else:
            meaning.append(("literal", "$", (index,)))
    return meaning, removed


def _ledger(
    doc: HereDocument, encoded: EncodedDocument, removed: set[int]
) -> list[tuple[str, Span]]:
    characters: list[tuple[str, Span]] = []
    offset = 0
    for char in encoded.body:
        end = offset + len(char.encode("utf-8"))
        characters.append((char, Span(offset, end)))
        offset = end
    boundaries = {0, offset} | {span.start for _, span in characters}
    cursor = 0
    owned: dict[int, list[Span]] = {}
    for origin in encoded.origins:
        if (
            origin.output.start != cursor
            or origin.output.end <= cursor
            or origin.output.start not in boundaries
            or origin.output.end not in boundaries
            or len(set(origin.events)) != len(origin.events)
        ):
            _fail(
                "ORIGIN_TRANSPORT",
                "origin ranges must partition complete UTF-8 source",
                doc,
            )
        for index in origin.events:
            if (
                index < 0
                or index >= len(doc.events)
                or index in removed
                or isinstance(doc.events[index], FenceEvent)
            ):
                _fail("ORIGIN_TRANSPORT", "invalid emitted event identity", doc)
            owned.setdefault(index, []).append(origin.output)
        cursor = origin.output.end
    if cursor != offset or len(encoded.dispositions) != len(doc.events):
        _fail("ORIGIN_TRANSPORT", "source or event ledger is incomplete", doc)
    zero_positions = _zero_positions(len(doc.events), owned, offset)
    seen: set[int] = set()
    for disposition in encoded.dispositions:
        index = disposition.event
        if index in seen or index < 0 or index >= len(doc.events):
            _fail("ORIGIN_TRANSPORT", "event dispositions must occur exactly once", doc)
        seen.add(index)
        if index in owned:
            spans = owned[index]
            expected = Span(min(s.start for s in spans), max(s.end for s in spans))
            if disposition.output != expected or disposition.reason not in {
                "emitted",
                "cooked",
            }:
                _fail(
                    "ORIGIN_TRANSPORT",
                    "emitted disposition does not match actual origins",
                    doc,
                )
        else:
            position = zero_positions[index]
            reason = (
                "fence" if isinstance(doc.events[index], FenceEvent) else "continuation"
            )
            if (
                disposition.output != Span(position, position)
                or disposition.reason != reason
                or (reason == "continuation" and index not in removed)
            ):
                _fail(
                    "ORIGIN_TRANSPORT", "unsupported zero-width event disposition", doc
                )
    return characters


def _read_emitted(
    doc: HereDocument,
    encoded: EncodedDocument,
    characters: list[tuple[str, Span]],
    plain: bool,
) -> list[tuple[str, str, tuple[int, ...]]]:
    starts = [origin.output.start for origin in encoded.origins]

    def owners(spans: list[Span]) -> tuple[int, ...]:
        result: list[int] = []
        seen: set[int] = set()
        for span in spans:
            position = max(0, bisect_right(starts, span.start) - 1)
            while position < len(encoded.origins):
                origin = encoded.origins[position]
                if origin.output.start >= span.end:
                    break
                for index in origin.events:
                    if index not in seen:
                        result.append(index)
                        seen.add(index)
                position += 1
        return tuple(result)

    if plain:
        return [("literal", char, owners([span])) for char, span in characters]
    # Read actual concatenated source, independent of the compiler's atom ends.
    logical: list[tuple[str, Span]] = []
    queue = iter(characters)
    for char, span in queue:
        if char != "\\":
            logical.append((char, span))
            continue
        nxt = next(queue, None)
        if nxt is None:
            _fail("BODY_OWNERSHIP_CONFLICT", "emitted source has an open escape", doc)
        if nxt[0] != "\n":
            logical.extend(((char, span), nxt))
    result: list[tuple[str, str, tuple[int, ...]]] = []
    logical_spelling = "".join(char for char, _ in logical)
    p = 0
    while p < len(logical):
        char, span = logical[p]
        p += 1
        if char == "\\":
            if p == len(logical):
                _fail("BODY_OWNERSHIP_CONFLICT", "logical body has an open escape", doc)
            nxt, nxt_span = logical[p]
            p += 1
            if nxt in _ESCAPED:
                result.append(("literal", nxt, owners([span, nxt_span])))
            else:
                result.extend(
                    (
                        ("literal", char, owners([span])),
                        ("literal", nxt, owners([nxt_span])),
                    )
                )
        elif char == "`":
            _fail("EMITTED_EXPANSION", "emitted body contains an active backtick", doc)
        elif char == "$":
            match = _PARAMETER_TAIL.match(logical_spelling, p)
            if match:
                count = len(match.group(0))
                result.append(
                    (
                        "parameter",
                        match.group(1) or match.group(2),
                        owners([span] + [s for _, s in logical[p : p + count]]),
                    )
                )
                p += count
            elif p < len(logical_spelling) and (
                logical_spelling[p] in _SPECIAL_DOLLAR or logical_spelling[p] == "{"
            ):
                _fail(
                    "EMITTED_EXPANSION",
                    "emitted body contains an unsupported active expansion",
                    doc,
                )
            else:
                result.append(("literal", char, owners([span])))
        else:
            result.append(("literal", char, owners([span])))
    return result


def verify_document(doc: HereDocument, encoded: EncodedDocument) -> None:
    """Check actual output meaning, event transport and first-closer ownership.

    This is a document witness, not a claim to have verified the surrounding
    header's actual bytes or FIFO/frame association; the enclosing witness must
    check both emitted delimiter replacements at their owned source intervals.
    """
    _validate(doc)
    try:
        encoded.body.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("EMITTED_ENCODING", "emitted body is not valid UTF-8", doc)
    if "\x00" in encoded.body or not _IDENTIFIER.fullmatch(encoded.delimiter):
        _fail("EMITTED_ENCODING", "invalid body byte or delimiter", doc)
    plain = doc.quoted or not _has_values(doc)
    if plain:
        expected = [
            ("literal", e.text, (i,))
            for i, e in enumerate(doc.events)
            if isinstance(e, TextEvent)
        ]
        removed: set[int] = set()
    else:
        expected, removed = _intended(doc)
    characters = _ledger(doc, encoded, removed)
    if _read_emitted(doc, encoded, characters, plain) != expected:
        _fail(
            "DOCUMENT_WITNESS",
            "actual emitted source changes body meaning or origin transport",
            doc,
        )
    if encoded.delimiter != _delimiter(doc, encoded.body):
        _fail(
            "BODY_OWNERSHIP_CONFLICT",
            "delimiter is not the first absent owned identifier",
            doc,
        )
