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

    The opener span covers identifier bytes only, excluding quotes. The closer
    span also includes authored line continuations in a logical closer.
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
    closer_spelling: str | None = None


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


@dataclass(frozen=True)
class FieldPart:
    """One cooked literal character or one complete trusted expansion.

    An empty protected literal retains an authored empty quote at its exact
    position; this differs from an expansion which might itself vanish.

    ``quoted`` is the original effective protection mask after trusted escape
    reconciliation, not the quoting chosen by the emitter. ``data`` cannot be
    true for an expansion. Events index the containing island's event tuple.
    """

    kind: Literal["literal", "expansion"]
    text: str
    quoted: bool
    data: bool
    events: tuple[int, ...]


@dataclass(frozen=True)
class FieldFence:
    occurrence: int
    edge: Literal["start", "end"]
    # Byte offset in the concatenated FieldPart.text spellings.
    position: int


@dataclass(frozen=True)
class FieldProjection:
    parts: tuple[FieldPart, ...]
    fences: tuple[FieldFence, ...]
    role: str
    forced: bool
    # A hole-free original NAME= prefix, when the ARG itself is assignment
    # shaped. True ASSIGN owners contain only the RHS and use role=ASSIGN.
    authored_assignment_prefix: str | None = None
    # Exact pre-encoding field events (including authored quote/escape bytes
    # and consumed continuations), excluding only field-separator whitespace.
    # Raw tilde lookup requests cannot be derived from normalized expansions.
    source_events: tuple[Event, ...] = ()
    # FieldPart.events index the enclosing island. This offset maps each raw
    # source_events entry back to that immutable island-event coordinate.
    source_event_start: int = 0


@dataclass(frozen=True)
class TildeEnvelope:
    # UTF-8 offsets in concatenated TextEvent.text from source_events, retaining
    # original quote/escape/expansion spelling (fences have zero width).
    span: Span


@dataclass(frozen=True)
class TildeRequest:
    position: int
    request: str
    tflag: int
    profile: str
    # Symbolic history: earlier requests may consume or fall back. No lookup
    # result or library state is inferred from the manager's machine.
    history: tuple[str, ...]


@dataclass(frozen=True)
class TildePlan:
    envelopes: tuple[TildeEnvelope, ...]
    requests: tuple[TildeRequest, ...]


@dataclass(frozen=True)
class Replacement:
    source: Span
    text: str
    kind: Literal["word", "document-body", "document-delimiter", "comment", "number"]
    role: str
    # Attempt-local intended fields, qualified by the independent word reader.
    # They are not an authority to suppress root/frame/command checks.
    fields: tuple[FieldProjection, ...] = ()
    # Independently checked trailing DATA LF count emitted only as empty list
    # terminators. Pending-heredoc handoff uses BODY events instead.
    tail_lfs: int = 0


class ShellSourceError(ValueError):
    """A named pre-launch ownership/encoding failure, never a raw fallback."""

    def __init__(self, code: str, message: str, span: Span | None = None):
        self.code = code
        self.span = span
        super().__init__(f"{code}: {message}")
