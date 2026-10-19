"""Conditional ERE source, not pathname patterns or an ERE evaluator.

The grammar's opaque ``regex`` terminal is not authority about executable
expansions. Read its shell quotes/escapes and parameter forms independently,
in an actual =~ operand, before either local or program certification.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .encoding import quote
from .binding import neutral_bytes
from .documents import collect_documents
from .envelopes import parameter_end
from .model import (
    BoundTemplate, FieldFence, FieldProjection, Replacement, ShellSourceError, Span,
    TextEvent,
)
from .ownership import parse
from .word_witness import ReadPart, _expansions
from .words import Expansion


def _fail(message: str) -> None:
    raise ShellSourceError("REGEX_WITNESS", message)


def _legacy_arithmetic_span(source: bytes, start: int) -> Span:
    """Recover one $[...] atom in BYTE coordinates using ordinary-word CST.

    A subscript or nested command may contain ']'. No endpoint is authority
    until the complete sentinel command has exactly one arithmetic argument.
    """
    prefix = b"__recipe_word__ "
    endpoint = source.find(b"]", start + 2)
    while endpoint != -1:
        stop = endpoint + 1
        mini = prefix + source[start:stop]
        try:
            documents = collect_documents(BoundTemplate(mini.decode(), (), ()))
            root = parse(documents.skeleton)
        except ShellSourceError:
            root = None
        if root is not None and len(root.named_children) == 1:
            command = root.named_children[0]
            name = command.child_by_field_name("name")
            arguments = command.children_by_field_name("argument")
            if (
                command.type == "command"
                and command.start_byte == 0 and command.end_byte == len(mini)
                and name is not None and mini[name.start_byte:name.end_byte] == prefix[:-1]
                and len(command.named_children) == 2 and len(arguments) == 1
                and arguments[0].type == "arithmetic_expansion"
                and arguments[0].start_byte == len(prefix)
                and arguments[0].end_byte == len(mini)
            ):
                return Span(start, stop)
        endpoint = source.find(b"]", stop)
    raise ShellSourceError(
        "DATA_ARITHMETIC_SYNTAX", "regex legacy arithmetic is not one complete ordinary-word atom"
    )


def read_regex(
    source: str, *, expansions: dict[int, tuple[int, str]] | None = None,
    authored: str | None = None,
) -> tuple[ReadPart, ...]:
    """Read one whole conditional RHS, independently of lowering.

    Only the original-template caller supplies ``authored``: its already
    owned neutral CST has identical byte coordinates but holes are not Bash.
    Emitted-source witnesses always parse and read the actual same bytes.
    """
    prefix = b"[[ __recipe_lhs__ =~ "
    raw = source.encode()
    syntax = raw
    program = prefix + raw + b" ]]"
    try:
        root = parse(program)
    except ShellSourceError:
        # The pinned CST cannot represent a genuine multi-document FIFO.
        # Mask only reader-owned document bytes, preserving all coordinates.
        documents = collect_documents(BoundTemplate(program.decode(), (), ()))
        root = parse(documents.skeleton)
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
    if authored is not None:
        if len(authored.encode()) != len(raw):
            _fail("original regex projection changed byte coordinates")
        source = authored
        raw = source.encode()
    atoms = _expansions(right, prefix + raw + b" ]]", len(prefix))
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
            if char == "$":
                if source.startswith("$[", index):
                    byte = len(source[:index].encode())
                    # The neutral syntax, never original placeholder spelling,
                    # owns endpoint discovery. Slice authored bytes only after
                    # recovering that complete byte-width-preserving span.
                    arithmetic = _legacy_arithmetic_span(syntax, byte)
                    spelling = raw[arithmetic.start:arithmetic.end].decode()
                    # The public expansion map remains CHARACTER-indexed.
                    stop = index + len(spelling)
                    parts.append(ReadPart("expansion", spelling, mode == "double"))
                    if expansions is not None:
                        expansions[index] = (stop, spelling)
                    index = stop
                    continue
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
    """Recover closed authored parameters/arithmetic hidden by opaque regex CST.

    DATA cannot enter this map: endpoints must be entirely outside occurrences.
    Simple parameters are already fence-aware in the shared word cooker.
    """
    source = bound.source.encode()
    text = source[span.start:span.end].decode()
    # This read also refuses hidden executable syntax in the original owner.
    atoms = {}
    projected = neutral_bytes(bound)[span.start:span.end].decode()
    read_regex(projected, expansions=atoms, authored=text)
    result = {}
    for begin, (stop, spelling) in atoms.items():
        arithmetic = spelling.startswith("$[")
        if not arithmetic and not spelling.startswith("${"):
            continue
        start = span.start + len(text[:begin].encode())
        end = span.start + len(text[:stop].encode())
        if any(o.span.start < end and start < o.span.end for o in bound.occurrences):
            code = "DATA_ARITHMETIC_SYNTAX" if arithmetic else "DATA_PARAMETER_SYNTAX"
            raise ShellSourceError(code, "regex expansion crosses a DATA fence")
        # Quoted/escaped lookalikes are harmless: cook_island consults this map
        # only after its own quote/escape handling.
        kind = "arithmetic_expansion" if arithmetic else "expansion"
        result[start] = Expansion(Span(start, end), source[start:end].decode(), kind)
    return result


def _data_escapes(field: FieldProjection) -> FieldProjection:
    """DATA backslash retains regex source-escape semantics, not globbing.

    Retain the events of both characters and remap every fence, including a
    zero-width fence between the backslash and the character it protects.
    Authored escapes were already reconciled by the shared cooker.
    """
    modes = {}
    mode = "ordinary"
    skipped = set()
    opaque = {i for p in field.parts if p.kind == "expansion" for i in p.events}
    events = field.source_events
    for local, event in enumerate(events):
        event_id = local + field.source_event_start
        modes[event_id] = mode
        if local in skipped or event_id in opaque or not isinstance(event, TextEvent):
            continue
        if event.kind == "data":
            continue
        if mode == "single":
            if event.text == "'":
                mode = "ordinary"
        elif event.text == "'":
            if mode == "ordinary":
                mode = "single"
        elif event.text == '"':
            mode = "ordinary" if mode == "double" else "double"
        elif event.text == "\\":
            following = next(
                (i for i in range(local + 1, len(events)) if isinstance(events[i], TextEvent)),
                None,
            )
            if following is not None and (mode == "ordinary" or events[following].text in '$`"\\\n'):
                skipped.add(following)
    parts = []
    mapping = {0: 0}
    old = new = index = 0
    while index < len(field.parts):
        part = field.parts[index]
        width = len(part.text.encode())
        data_backslash = (
            part.kind == "literal" and part.data and part.text == "\\"
            and len(part.events) == 1
        )
        double = data_backslash and modes[part.events[0]] == "double"
        target = field.parts[index + 1] if index + 1 < len(field.parts) else None
        consumes = not part.quoted or double and target is not None and target.text in '$`"\\\n'
        if data_backslash and consumes:
            if index + 1 == len(field.parts):
                raise ShellSourceError("REGEX_DOMAIN", "DATA escape leaves its regex owner")
            if (
                target.kind != "literal" or not target.text or target.text == "\n"
                or target.quoted and not double
            ):
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
        elif part.quoted or not part.text or part.text == "'":
            # Closed DQ literals avoid the pinned grammar's rejection of
            # single-quote break sequences while retaining ERE protection.
            literal = part.text.replace("\\", "\\\\").replace('"', '\\"')
            literal = literal.replace("$", "\\$").replace("`", "\\`")
            output.append('"' + literal + '"')
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