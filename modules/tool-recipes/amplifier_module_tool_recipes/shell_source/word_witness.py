"""Read actual emitted word source independently of the encoder's fragments."""

from __future__ import annotations

import re
from dataclasses import dataclass

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from .encoding import simple_patterns
from .model import FieldProjection, ShellSourceError


@dataclass(frozen=True)
class ReadPart:
    kind: str
    text: str
    protected: bool


def _expansions(node: Node, source: bytes, offset: int) -> dict[int, tuple[int, str]]:
    result = {}

    def walk(current: Node) -> None:
        text = source[current.start_byte : current.end_byte].decode()
        if (
            current.type
            in (
                "simple_expansion",
                "expansion",
                "command_substitution",
                "arithmetic_expansion",
                "process_substitution",
                "ansi_c_string",
            )
            or current.type == "string"
            and text.startswith('$"')
        ):
            result[current.start_byte - offset] = (current.end_byte - offset, text)
            return
        for child in current.named_children:
            walk(child)

    walk(node)
    return result


def read_field(source: str) -> tuple[ReadPart, ...]:
    """Independently require exactly one actual argument, then read its bytes."""
    prefix = b"__recipe_word__ "
    raw = source.encode()
    tree = Parser(Language(tree_sitter_bash.language())).parse(prefix + raw).root_node
    if (
        tree.has_error
        or len(tree.named_children) != 1
        or tree.named_children[0].type != "command"
    ):
        raise ShellSourceError(
            "WORD_WITNESS", "emitted field changes executable topology"
        )
    command = tree.named_children[0]
    arguments = [
        child
        for index, child in enumerate(command.children)
        if command.field_name_for_child(index) == "argument"
    ]
    if (
        len(arguments) != 1
        or arguments[0].start_byte != len(prefix)
        or arguments[0].end_byte != len(prefix) + len(raw)
    ):
        raise ShellSourceError(
            "WORD_WITNESS", "emitted field is not exactly one complete argument"
        )
    expansion = _expansions(arguments[0], prefix + raw, len(prefix))
    output: list[ReadPart] = []
    mode = "ordinary"
    opened_at = 0
    index = 0
    while index < len(raw):
        char = chr(raw[index])
        if mode == "single":
            if char == "'":
                if len(output) == opened_at:
                    output.append(ReadPart("literal", "", True))
                mode = "ordinary"
                index += 1
                continue
        else:
            if char == '"':
                if mode == "double":
                    if len(output) == opened_at:
                        output.append(ReadPart("literal", "", True))
                    mode = "ordinary"
                else:
                    mode = "double"
                    opened_at = len(output)
                index += 1
                continue
            if char == "'" and mode == "ordinary":
                mode = "single"
                opened_at = len(output)
                index += 1
                continue
            if char == "\\":
                if index + 1 >= len(raw):
                    raise ShellSourceError(
                        "WORD_WITNESS", "emitted escape is incomplete"
                    )
                following = chr(raw[index + 1])
                if mode == "ordinary" or following in '$`"\\\n':
                    index += 1
                    if following == "\n":
                        index += 1
                        continue
                    character = raw[index:].decode()[0]
                    output.append(ReadPart("literal", character, True))
                    index += len(character.encode())
                    continue
            atom = expansion.get(index)
            if atom is not None:
                end, spelling = atom
                output.append(ReadPart("expansion", spelling, mode == "double"))
                index = end
                continue
            if mode == "ordinary" and char == "{":
                brace = re.match(
                    rb"\{[A-Za-z0-9_./:+-]+(?:,[A-Za-z0-9_./:+-]+)+\}", raw[index:]
                )
                if brace is not None:
                    spelling = brace.group().decode()
                    output.append(ReadPart("expansion", spelling, False))
                    index += len(brace.group())
                    continue
        character = raw[index:].decode()[0]
        output.append(ReadPart("literal", character, mode != "ordinary"))
        index += len(character.encode())
    if mode != "ordinary":
        raise ShellSourceError("WORD_WITNESS", "emitted quote is incomplete")
    return tuple(output)


def _semantic(parts) -> tuple:
    result = []
    text = []
    forced = False

    def flush():
        nonlocal forced
        if text:
            result.append(("literal", "".join(text)))
            text.clear()
        elif forced:
            result.append(("empty-quote",))
        forced = False

    for part in parts:
        if part.kind == "literal":
            if part.text:
                text.append(part.text)
            else:
                forced = True
        else:
            flush()
            spelling = part.text
            if re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", spelling):
                spelling = "${" + spelling[1:] + "}"
            result.append(("expansion", spelling, part.protected))
    flush()
    return tuple(result)


def _actual_patterns(parts: tuple[ReadPart, ...]) -> tuple[tuple[int, str], ...]:
    result = []
    position = 0
    index = 0
    while index < len(parts):
        item = parts[index]
        if item.kind == "literal" and not item.protected:
            if item.text in ("*", "?"):
                result.append((position, item.text))
            elif item.text == "[":
                end = index + 1
                token = "["
                while (
                    end < len(parts)
                    and parts[end].kind == "literal"
                    and not parts[end].protected
                    and parts[end].text
                ):
                    token += parts[end].text
                    if parts[end].text == "]":
                        result.append((position, token))
                        break
                    end += 1
        position += len(item.text.encode())
        index += 1
    return tuple(result)


def verify_field(field: FieldProjection, source: str) -> None:
    if field.role == "TEST_REGEX":
        from .regex import read_regex

        actual = read_regex(source)
    else:
        actual = read_field(source)
    expected = tuple(ReadPart(p.kind, p.text, p.quoted) for p in field.parts)
    if not expected and field.forced:
        expected = (ReadPart("literal", "", True),)
    if _semantic(actual) != _semantic(expected):
        raise ShellSourceError(
            "WORD_WITNESS", "emitted literal/expansion/empty-word semantics differ"
        )
    if field.role == "TEST_REGEX":
        # ERE protection is not a pathname-glob mask. In particular dots,
        # anchors, grouping and captures retain their authored protection.
        # This comparison reads emitted bytes; it never calls the encoder.
        def protection(parts):
            return tuple(
                (i, p.text, p.protected)
                for i, p in enumerate(parts)
                if p.kind == "literal" and p.text in ".^$*+?[](){}|\\"
            )

        if protection(actual) != protection(expected):
            raise ShellSourceError("REGEX_WITNESS", "emitted ERE protection differs")
        return
    wanted = tuple(
        (atom.span.start, atom.text) for atom in simple_patterns(field.parts)
    )
    if _actual_patterns(actual) != wanted:
        raise ShellSourceError("WORD_WITNESS", "emitted pathname-pattern masks differ")
