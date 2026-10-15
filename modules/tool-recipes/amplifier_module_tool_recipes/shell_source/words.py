"""Compose original word islands before encoding context data.

The cooker consumes trusted escapes across empty occurrence fences before
deciding field boundaries. DATA characters never change quote or expansion
state. This module does not execute a shell or resolve expansion results.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re

from .model import Event, FenceEvent, FieldFence, FieldPart, FieldProjection, ShellSourceError, Span, TextEvent


@dataclass(frozen=True)
class Expansion:
    source: Span
    spelling: str
    kind: str


@dataclass(frozen=True)
class FieldSeparator:
    text: str
    events: tuple[int, ...]
    trusted: bool


@dataclass(frozen=True)
class CookedIsland:
    pieces: tuple[FieldProjection | FieldSeparator, ...]
    consumed: tuple[int, ...]
    # Every original fence survives, including those between deleted fields.
    fences: tuple[tuple[int, int, str], ...]


_NAME_START = re.compile(r"[A-Za-z_]")
_NAME_CHAR = re.compile(r"[A-Za-z_0-9]")
_ASSIGNMENT_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\Z")


def cook_island(events: tuple[Event, ...], role: str, *, expansion_at: Callable[[int], Expansion | None] = lambda _: None) -> CookedIsland:
    pieces: list[FieldProjection | FieldSeparator] = []
    parts: list[FieldPart] = []
    field_fences: list[FieldFence] = []
    consumed: list[int] = []
    fences: list[tuple[int, int, str]] = []
    mode = "ordinary"
    forced = False
    index = 0
    field_start = 0
    quote_start: tuple[int, int] | None = None

    def position() -> int:
        return sum(len(part.text.encode("utf-8")) for part in parts)

    def fence(event: FenceEvent, event_index: int) -> None:
        field_fences.append(FieldFence(event.occurrence, event.edge, position()))
        fences.append((event_index, event.occurrence, event.edge))

    def literal(text: str, quoted: bool, data: bool, owners: tuple[int, ...]) -> None:
        for char in text:
            parts.append(FieldPart("literal", char, quoted, data, owners))

    def flush(stop: int) -> None:
        nonlocal forced
        if parts or forced:
            prefix: list[str] = []
            for part in parts:
                if part.kind != "literal" or part.quoted or part.data:
                    break
                prefix.append(part.text)
                if part.text == "=":
                    break
            spelling = "".join(prefix)
            assigned = spelling if _ASSIGNMENT_PREFIX.fullmatch(spelling) else None
            pieces.append(FieldProjection(tuple(parts), tuple(field_fences), role, forced, assigned, events[field_start:stop], field_start))
        parts.clear()
        field_fences.clear()
        forced = False

    def next_text(start: int) -> int:
        while start < len(events) and isinstance(events[start], FenceEvent):
            fence(events[start], start)
            start += 1
        return start

    while index < len(events):
        event = events[index]
        if isinstance(event, FenceEvent):
            fence(event, index)
            index += 1
            continue
        char = event.text
        if event.kind == "data":
            if mode == "ordinary" and role in ("ARG", "FOR_VALUES") and char in " \t":
                flush(index)
                owners = [index]
                end = index + 1
                while end < len(events) and isinstance(events[end], TextEvent) and events[end].kind == "data" and events[end].text in " \t":
                    owners.append(end)
                    end += 1
                # DATA layout has field semantics; it never creates operators.
                pieces.append(FieldSeparator(" ", tuple(owners), False))
                index = end
                field_start = end
                continue
            literal(char, mode != "ordinary", True, (index,))
            index += 1
            continue
        if mode == "single":
            if char == "'":
                if quote_start is not None and len(parts) == quote_start[1]:
                    parts.append(FieldPart("literal", "", True, False, (quote_start[0], index)))
                quote_start = None
                mode = "ordinary"
                consumed.append(index)
            else:
                literal(char, True, False, (index,))
            index += 1
            continue
        if char == "'" and mode == "ordinary":
            quote_start = (index, len(parts))
            mode = "single"
            forced = True
            consumed.append(index)
            index += 1
            continue
        if char == '"':
            if mode == "double":
                if quote_start is not None and len(parts) == quote_start[1]:
                    parts.append(FieldPart("literal", "", True, False, (quote_start[0], index)))
                quote_start = None
            else:
                quote_start = (index, len(parts))
            mode = "ordinary" if mode == "double" else "double"
            forced = True
            consumed.append(index)
            index += 1
            continue
        if char == "\\":
            following = next_text(index + 1)
            if following == len(events):
                raise ShellSourceError("OWNER_CONFLICT", "trusted escape leaves its original word island", event.source)
            target = events[following]
            assert isinstance(target, TextEvent)
            crossed_fence = following != index + 1
            if crossed_fence and target.kind == "trusted" and target.text in "\n'\";&|()<>":
                raise ShellSourceError("OWNER_CONFLICT", "empty occurrence exposes an authored structural boundary", target.source)
            special = mode == "ordinary" or target.text in '$`"\\\n'
            if special:
                consumed.append(index)
                if target.text == "\n":
                    consumed.append(following)
                else:
                    literal(target.text, True, target.kind == "data", (index, following))
                index = following + 1
                continue
            literal("\\", True, False, (index,))
            # The following character is not consumed by a DQ backslash.
            index = following
            continue
        if char in " \t" and mode == "ordinary" and role in ("ARG", "FOR_VALUES"):
            flush(index)
            end = index + 1
            while end < len(events) and isinstance(events[end], TextEvent) and events[end].kind == "trusted" and events[end].text in " \t":
                end += 1
            pieces.append(FieldSeparator("".join(events[i].text for i in range(index, end)), tuple(range(index, end)), True))
            index = end
            field_start = end
            continue
        if char == "$":
            # Fences, even EMPTY, terminate a trusted simple parameter name.
            end = index + 1
            if end < len(events) and isinstance(events[end], TextEvent) and events[end].kind == "trusted" and _NAME_START.fullmatch(events[end].text):
                end += 1
                while end < len(events) and isinstance(events[end], TextEvent) and events[end].kind == "trusted" and _NAME_CHAR.fullmatch(events[end].text):
                    end += 1
                name = "".join(events[i].text for i in range(index + 1, end))
                parts.append(FieldPart("expansion", "${" + name + "}", mode == "double", False, tuple(range(index, end))))
                index = end
                continue
            if end == len(events) or isinstance(events[end], FenceEvent):
                literal("$", mode == "double", False, (index,))
                index += 1
                continue
        opaque = expansion_at(event.source.start)
        if opaque is not None:
            if opaque.source.start != event.source.start:
                raise ShellSourceError("EXPANSION_OWNER", "opaque expansion starts outside its original owner", opaque.source)
            end = index
            while end < len(events) and events[end].source.start < opaque.source.end:
                if isinstance(events[end], FenceEvent):
                    fence(events[end], end)
                if events[end].source.end > opaque.source.end:
                    raise ShellSourceError("EXPANSION_OWNER", "opaque expansion cuts an occurrence", opaque.source)
                end += 1
            parts.append(FieldPart("expansion", opaque.spelling, mode == "double", False, tuple(range(index, end))))
            index = end
            continue
        literal(char, mode == "double", False, (index,))
        index += 1
    if mode != "ordinary":
        raise ShellSourceError("UNCLOSED_WORD", "word owner did not close its authored quote")
    flush(len(events))
    return CookedIsland(tuple(pieces), tuple(consumed), tuple(fences))
