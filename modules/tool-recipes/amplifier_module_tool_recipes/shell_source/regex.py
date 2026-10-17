"""Conditional ERE source, not pathname patterns or an ERE evaluator.

The grammar's opaque ``regex`` terminal is not authority about executable
expansions. Read its shell quotes/escapes and parameter forms independently,
in an actual =~ operand, before either local or program certification.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .encoding import quote
from .envelopes import parameter_end
from .model import (
    BoundTemplate, FieldFence, FieldProjection, Replacement, ShellSourceError, Span,
)
from .ownership import parse
from .word_witness import ReadPart, _expansions
from .words import Expansion


def _fail(message: str) -> None:
    raise ShellSourceError("REGEX_WITNESS", message)


def read_regex(
    source: str, *, expansions: dict[int, tuple[int, str]] | None = None
) -> tuple[ReadPart, ...]:
    """Require one whole actual conditional RHS, then read every source byte."""
    prefix = b"[[ __recipe_lhs__ =~ "
    raw = source.encode()
    program = prefix + raw + b" ]]"
    root = parse(program)
    if len(root.named_children) != 1 or root.named_children[0].type != "test_command":
        _fail("regex field changed its test boundary")
    test = root.named_children[0]
    if len(test.named_children) != 1:
        _fail("regex field added a test operand")
    binary = test.named_children[0]
    left = binary.child_by_field_name("left")
    right = binary.child_by_field_name("right")
    op = binary.child_by_field_name("operator")
    if (
        binary.type != "binary_expression"
        or left is None or program[left.start_byte:left.end_byte] != b"__recipe_lhs__"
        or op is None or program[op.start_byte:op.end_byte] != b"=~"
        or right is None or right.start_byte != len(prefix)
        or right.end_byte != len(prefix) + len(raw)
        or test.start_byte != 0 or test.end_byte != len(program)
    ):
        _fail("regex field is not exactly its conditional RHS")
    atoms = _expansions(right, program, len(prefix))
    parts = []
    mode = "ordinary"
    opened = 0
    index = 0
    while index < len(source):
        char = source[index]
        if mode == "single":
            if char == "'":
                if len(parts) == opened:
                    parts.append(ReadPart("literal", "", True))
                mode = "ordinary"
                index += 1
                continue
        else:
            if char == '"' or char == "'" and mode == "ordinary":
                if mode == "double" and char == '"':
                    if len(parts) == opened:
                        parts.append(ReadPart("literal", "", True))
                    mode = "ordinary"
                else:
                    mode = "single" if char == "'" else "double"
                    opened = len(parts)
                index += 1
                continue
            if char == "\\":
                if index + 1 == len(source):
                    _fail("incomplete regex escape")
                following = source[index + 1]
                if mode == "ordinary" or following in '$`"\\\n':
                    if following != "\n":
                        parts.append(ReadPart("literal", following, True))
                    index += 2
                    continue
            byte = len(source[:index].encode())
            atom = atoms.get(byte)
            if atom is not None:
                end, spelling = atom
                parts.append(ReadPart("expansion", spelling, mode == "double"))
                stop = index + len(raw[byte:end].decode())
                if expansions is not None:
                    expansions[index] = (stop, spelling)
                index = stop
                continue
            if char == "$":
                if source.startswith("${", index):
                    end = parameter_end(source, index)
                else:
                    name = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*|[0-9@?$!#*\-])", source[index:])
                    end = index + len(name.group()) if name else index
                if end > index:
                    parts.append(ReadPart("expansion", source[index:end], mode == "double"))
                    if expansions is not None:
                        expansions[index] = (end, source[index:end])
                    index = end
                    continue
                if source[index + 1:index + 2] in ("(", "'", '"'):
                    _fail("opaque regex concealed an executable dollar envelope")
            if char == "`":
                _fail("opaque regex concealed a backtick envelope")
            if mode == "ordinary" and char in " \t\n;&<>":
                _fail("unowned shell syntax inside regex terminal")
        parts.append(ReadPart("literal", char, mode != "ordinary"))
        index += 1
    if mode != "ordinary":
        _fail("incomplete regex quote")
    return tuple(parts)


def parameter_atoms(bound: BoundTemplate, span: Span) -> dict[int, Expansion]:
    """Recover closed authored parameters hidden by the opaque CST terminal.

    DATA cannot enter this map: endpoints must be entirely outside occurrences.
    Simple parameters are already fence-aware in the shared word cooker.
    """
    source = bound.source.encode()
    text = source[span.start:span.end].decode()
    # This read also refuses hidden executable syntax in the original owner.
    atoms = {}
    read_regex(text, expansions=atoms)
    result = {}
    for begin, (stop, spelling) in atoms.items():
        if not spelling.startswith("${"):
            continue
        start = span.start + len(text[:begin].encode())
        end = span.start + len(text[:stop].encode())
        if any(o.span.start < end and start < o.span.end for o in bound.occurrences):
            raise ShellSourceError("DATA_PARAMETER_SYNTAX", "regex parameter crosses a DATA fence")
        # Quoted/escaped lookalikes are harmless: cook_island consults this map
        # only after its own quote/escape handling.
        result[start] = Expansion(Span(start, end), source[start:end].decode(), "expansion")
    return result


def _data_escapes(field: FieldProjection) -> FieldProjection:
    """Unquoted DATA backslash has regex source-escape semantics, not globbing.

    Retain the events of both characters and remap every fence, including a
    zero-width fence between the backslash and the character it protects.
    Authored escapes were already reconciled by the shared cooker.
    """
    parts = []
    mapping = {0: 0}
    old = new = index = 0
    while index < len(field.parts):
        part = field.parts[index]
        width = len(part.text.encode())
        if part.kind == "literal" and part.data and not part.quoted and part.text == "\\":
            if index + 1 == len(field.parts):
                raise ShellSourceError("REGEX_DOMAIN", "DATA escape leaves its regex owner")
            target = field.parts[index + 1]
            if target.kind != "literal" or target.quoted or not target.text or target.text == "\n":
                raise ShellSourceError("REGEX_DOMAIN", "DATA escape crosses a protected regex boundary")
            mapping[old + width] = new
            part = replace(target, quoted=True, data=part.data or target.data,
                           events=part.events + target.events)
            width += len(target.text.encode())
            index += 1
        parts.append(part)
        old += width
        new += len(part.text.encode())
        mapping[old] = new
        index += 1
    fences = tuple(FieldFence(f.occurrence, f.edge, mapping[f.position]) for f in field.fences)
    return replace(field, parts=tuple(parts), fences=fences)


def compile_regex(span: Span, cooked, *, raw_envelopes) -> Replacement:
    from .word_witness import verify_field
    from .tilde import plan_tildes, verify_tildes

    if len(cooked.pieces) != 1 or not isinstance(cooked.pieces[0], FieldProjection):
        raise ShellSourceError("REGEX_DOMAIN", "regex requires a complete nonempty or authored quoted RHS", span)
    field = _data_escapes(cooked.pieces[0])
    plan = plan_tildes(field)
    preserved = {a.span.start: a for a in raw_envelopes(field, plan)}
    output = []
    index = offset = 0
    while index < len(field.parts):
        atom = preserved.get(offset)
        if atom is not None:
            output.append(atom.text)
            while index < len(field.parts) and offset < atom.span.end:
                offset += len(field.parts[index].text.encode())
                index += 1
            if offset != atom.span.end:
                raise ShellSourceError("REGEX_DOMAIN", "tilde envelope cuts a regex part", span)
            continue
        part = field.parts[index]
        if part.kind == "expansion":
            output.append('"' + part.text + '"' if part.quoted else part.text)
        elif part.text == "\\":
            # The pinned conditional grammar rejects a single-quoted lone
            # backslash; the equivalent DQ spelling has a complete CST.
            output.append('"\\\\"')
        elif part.quoted or not part.text:
            output.append(quote(part.text))
        elif part.text.isalnum() or part.text in "_./:^$*+?[](){}|,!=~-":
            output.append(part.text)
        else:
            output.append(quote(part.text))
        offset += len(part.text.encode())
        index += 1
    text = "".join(output)
    verify_field(field, text)
    verify_tildes(field, text)
    return Replacement(span, text, "word", "TEST_REGEX", (field,))