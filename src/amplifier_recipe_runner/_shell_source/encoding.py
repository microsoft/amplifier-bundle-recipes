"""Literal source encoding and qualified simple pathname-pattern atoms."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import FieldPart, FieldProjection, ShellSourceError, Span

_IDENTITY = re.compile(r"[A-Za-z0-9_./:+,\-]+\Z")


@dataclass(frozen=True)
class LiteralAtom:
    span: Span
    text: str
    pattern: bool


def quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _bracket(text: str) -> bool:
    inner = text[1:-1]
    inner = inner.removeprefix("!")
    if not inner:
        return False
    index = 0
    while index < len(inner):
        character = inner[index]
        if character == "-" and index in (0, len(inner) - 1):
            index += 1
            continue
        if not character.isascii() or not character.isalnum():
            return False
        if index + 2 < len(inner) and inner[index + 1] == "-":
            end = inner[index + 2]
            if not any(
                low <= character <= end <= high
                for low, high in (("a", "z"), ("A", "Z"), ("0", "9"))
            ):
                return False
            index += 3
        else:
            index += 1
    return True


def simple_patterns(parts: tuple[FieldPart, ...]) -> tuple[LiteralAtom, ...]:
    """Recognize entire eligible bracket atoms, including across DATA fences."""
    offsets = []
    offset = 0
    for part in parts:
        offsets.append(offset)
        offset += len(part.text.encode())
    result = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part.kind != "literal" or part.quoted or not part.text:
            index += 1
            continue
        if part.text in ("*", "?"):
            result.append(
                LiteralAtom(Span(offsets[index], offsets[index] + 1), part.text, True)
            )
        elif part.text == "[":
            end = index + 1
            while (
                end < len(parts)
                and parts[end].kind == "literal"
                and not parts[end].quoted
                and parts[end].text
                and parts[end].text != "]"
            ):
                end += 1
            if (
                end < len(parts)
                and parts[end].kind == "literal"
                and not parts[end].quoted
                and parts[end].text == "]"
            ):
                text = "".join(p.text for p in parts[index : end + 1])
                if not _bracket(text):
                    raise ShellSourceError(
                        "GLOB_DOMAIN",
                        "unquoted bracket pattern is outside the simple-pattern domain",
                    )
                result.append(
                    LiteralAtom(Span(offsets[index], offsets[end] + 1), text, True)
                )
                index = end
        index += 1
    return tuple(result)


def encode_field(
    field: FieldProjection, *, preserved: tuple[LiteralAtom, ...] = ()
) -> str:
    """Emit closed literals around exact qualified pattern/tilde envelopes.

    ``preserved`` is supplied by the separately verified tilde component. Every
    span addresses complete cooked parts and may not cross another envelope.
    """
    atoms = tuple(
        sorted(
            (*simple_patterns(field.parts), *preserved),
            key=lambda atom: (atom.span.start, -atom.span.end),
        )
    )
    # A larger tilde raw-finder envelope may deliberately own a glob spelling.
    chosen: list[LiteralAtom] = []
    for atom in atoms:
        if chosen and atom.span.start < chosen[-1].span.end:
            if atom.span.end <= chosen[-1].span.end:
                continue
            raise ShellSourceError("ENCODING_ENVELOPE", "qualified literal atoms cross")
        chosen.append(atom)
    by_start = {atom.span.start: atom for atom in chosen}
    output: list[str] = []
    literal: list[str] = []
    offset = 0
    index = 0

    def flush() -> None:
        if literal:
            text = "".join(literal)
            output.append(text if _IDENTITY.fullmatch(text) else quote(text))
            literal.clear()

    while index < len(field.parts):
        part = field.parts[index]
        atom = by_start.get(offset)
        if atom is not None:
            flush()
            output.append(atom.text)
            while index < len(field.parts) and offset < atom.span.end:
                offset += len(field.parts[index].text.encode())
                index += 1
            if offset != atom.span.end:
                raise ShellSourceError(
                    "ENCODING_ENVELOPE", "literal atom cuts a cooked part"
                )
            continue
        if part.kind == "expansion":
            flush()
            output.append('"' + part.text + '"' if part.quoted else part.text)
        elif part.text == "":
            flush()
            output.append("''")
        elif part.quoted:
            # Protected wildcard/tilde characters stay protected. Other inert
            # characters may use the same closed literal representation.
            if part.text in "*?[]~":
                flush()
                output.append(quote(part.text))
            else:
                literal.append(part.text)
        else:
            literal.append(part.text)
        offset += len(part.text.encode())
        index += 1
    flush()
    if not field.parts and field.forced:
        return "''"
    return "".join(output)
