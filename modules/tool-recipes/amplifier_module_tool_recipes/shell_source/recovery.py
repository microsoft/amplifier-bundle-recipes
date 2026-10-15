"""The one admitted whole-root classification recovery language.

This is not Jinja evaluation. Marker lines remain executable Bash bytes. The
projection only lets the Bash CST classify independently verified echo/cat
lines; the global witness must preserve every marker's original bytes.
"""

from __future__ import annotations

import re

from .binding import neutral_bytes
from .model import BoundTemplate, Span


_MARKER = re.compile(rb"(?:\{% if [A-Za-z_][A-Za-z0-9_]* != '' %\}|\{% endif %\})\Z")
_OUTPUT = re.compile(rb'(?:echo|cat)[ \t]+"[^"\\$`\n]*"\Z')


def recovery_projection(bound: BoundTemplate, source: bytes) -> tuple[bytes, tuple[Span, ...]] | None:
    """Validate *all* physical lines, then mask only hole-free marker bytes."""
    neutral = neutral_bytes(bound)
    if source != neutral:
        # Documents/other lexical rewrites have no place in this language.
        return None
    result = bytearray(source)
    markers: list[Span] = []
    offset = 0
    lines = source.split(b"\n")
    for index, body in enumerate(lines):
        text = body.strip(b" \t")
        if not text:
            pass
        elif _MARKER.fullmatch(text):
            span = Span(offset, offset + len(body))
            if any(o.span.start < span.end and o.span.end > span.start for o in bound.occurrences):
                return None
            markers.append(span)
            result[span.start:span.end] = b" " * (span.end - span.start)
        elif _OUTPUT.fullmatch(text):
            pass
        else:
            return None
        offset += len(body) + (index < len(lines) - 1)
    return (bytes(result), tuple(markers)) if markers else None
