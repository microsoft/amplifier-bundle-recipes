"""Bind original recipe occurrences exactly once, before shell interpretation."""

from __future__ import annotations

from collections.abc import Callable
import re

from .model import BoundTemplate, Event, FenceEvent, Occurrence, ShellSourceError, Span, TextEvent


_HOLE = re.compile(r"\{\{(\w+(?:\.\w+)*)\}\}")


def bind(source: str, resolve: Callable[[str], str]) -> BoundTemplate:
    """Call the engine's existing STRING converter once per original hole.

    ``resolve`` takes the variable reference without braces. Returned strings
    are never scanned for additional holes; converter exceptions propagate.
    """
    try:
        source.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ShellSourceError("INVALID_ENCODING", "template is not valid UTF-8") from exc
    if "\x00" in source:
        raise ShellSourceError("NUL_SOURCE", "Bash source cannot contain NUL")

    occurrences: list[Occurrence] = []
    events: list[Event] = []
    char_cursor = byte_cursor = 0

    def trusted(text: str) -> None:
        nonlocal byte_cursor
        for character in text:
            end = byte_cursor + len(character.encode("utf-8"))
            events.append(TextEvent("trusted", character, Span(byte_cursor, end)))
            byte_cursor = end

    for match in _HOLE.finditer(source):
        trusted(source[char_cursor:match.start()])
        span = Span(byte_cursor, byte_cursor + len(match.group().encode("utf-8")))
        value = resolve(match.group(1))
        if not isinstance(value, str):
            raise TypeError("recipe STRING converter must return str")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ShellSourceError("INVALID_ENCODING", "context value is not valid UTF-8", span) from exc
        if "\x00" in value:
            raise ShellSourceError("NUL_DATA", "Bash context data cannot contain NUL", span)
        occurrence = Occurrence(len(occurrences), match.group(1), span, value)
        occurrences.append(occurrence)
        events.append(FenceEvent(occurrence.id, "start", span))
        value_cursor = 0
        for character in value:
            end = value_cursor + len(character.encode("utf-8"))
            events.append(TextEvent("data", character, span, occurrence.id, Span(value_cursor, end)))
            value_cursor = end
        events.append(FenceEvent(occurrence.id, "end", span))
        char_cursor = match.end()
        byte_cursor = span.end
    trusted(source[char_cursor:])
    return BoundTemplate(source, tuple(occurrences), tuple(events))


def neutral_bytes(bound: BoundTemplate) -> bytes:
    """Replace each original hole with inert ASCII of identical byte width."""
    source = bound.source.encode("utf-8")
    result = bytearray(source)
    for occurrence in bound.occurrences:
        result[occurrence.span.start:occurrence.span.end] = b"x" * (occurrence.span.end - occurrence.span.start)
    return bytes(result)


def events_in(bound: BoundTemplate, span: Span) -> tuple[Event, ...]:
    """Return complete events within an original owned source interval."""
    result: list[Event] = []
    for event in bound.events:
        owner = event.source
        if owner.start >= span.start and owner.end <= span.end:
            result.append(event)
        elif owner.start < span.end and owner.end > span.start:
            raise ShellSourceError("SPLIT_OCCURRENCE", "source owner splits a character or occurrence", owner)
    return tuple(result)
