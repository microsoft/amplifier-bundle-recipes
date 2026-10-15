"""Immutable byte-coordinate interfaces shared by shell compiler components.

Offsets refer to UTF-8 bytes, never Python character indexes.  A text event is
one complete Unicode code point; fences retain empty and adjacent occurrences.
The compiler supplies ownership, while each emitted-source witness must derive
the output's syntax independently rather than trusting a claimed role.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


@dataclass(frozen=True, order=True)
class Span:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("invalid source span")


@dataclass(frozen=True)
class Occurrence:
    id: int
    name: str
    span: Span
    value: str


@dataclass(frozen=True)
class TextEvent:
    kind: Literal["trusted", "data"]
    text: str
    # Trusted text owns its exact template span. Data owns its occurrence's
    # complete original hole span plus a distinct UTF-8 span within value.
    source: Span
    occurrence: int | None = None
    value_span: Span | None = None

    def __post_init__(self) -> None:
        if len(self.text) != 1 or self.text == "\x00":
            raise ValueError("text events contain one non-NUL character")
        self.text.encode("utf-8", errors="strict")
        if self.kind == "trusted":
            if self.occurrence is not None or self.value_span is not None:
                raise ValueError("trusted text cannot claim a data occurrence")
        elif self.kind == "data":
            if self.occurrence is None or self.value_span is None:
                raise ValueError("data text requires an occurrence and value span")
        else:
            raise ValueError("unknown text origin")


@dataclass(frozen=True)
class FenceEvent:
    occurrence: int
    edge: Literal["start", "end"]
    source: Span


Event: TypeAlias = TextEvent | FenceEvent


@dataclass(frozen=True)
class BoundTemplate:
    source: str
    occurrences: tuple[Occurrence, ...]
    events: tuple[Event, ...]


@dataclass(frozen=True)
class HereDocument:
    """One original FIFO-owned document, before any DATA is emitted.

    The delimiter spans cover identifier bytes only, excluding quotes/tabs.
    ``body`` includes the authored final LF, when one exists. Its immutable
    identity is explicit so a continuation cannot silently swallow the closer.
    Header-LF handoff is performed by the owning compiler, which prepends DATA
    events to ``events`` while retaining that final authored LF identity.
    """

    frame: int
    ordinal: int
    delimiter: str
    quoted: bool
    strip_tabs: bool
    opener_delimiter: Span
    body: Span
    closer_delimiter: Span
    events: tuple[Event, ...]
    terminal_lf_source: Span | None


@dataclass(frozen=True)
class OutputOrigin:
    """Positive-width emitted source owned by original event indices.

    Several events may compose one closed escape. Compiler-created quote or
    delimiter bytes have no event index and are independently checked syntax.
    This is an origin ledger, never a substitute for emitted-source parsing.
    """

    output: Span
    events: tuple[int, ...]


@dataclass(frozen=True)
class EventDisposition:
    event: int
    output: Span
    reason: Literal["emitted", "fence", "continuation", "cooked"]


@dataclass(frozen=True)
class EncodedDocument:
    delimiter: str
    body: str
    origins: tuple[OutputOrigin, ...]
    dispositions: tuple[EventDisposition, ...]


class ShellSourceError(ValueError):
    """A named pre-launch ownership/encoding failure, never a raw fallback."""

    def __init__(self, code: str, message: str, span: Span | None = None):
        self.code = code
        self.span = span
        super().__init__(f"{code}: {message}")
