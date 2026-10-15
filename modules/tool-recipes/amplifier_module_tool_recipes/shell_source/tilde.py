"""Preserve Bash 5.2.21 MAIN/finder requests without resolving any lookup.

The source model follows subst.c's ``= : ~`` cases and general.c's
``bash_tilde_find_word``. The library is deliberately opaque: each request has
both an unchanged-result continuation and a consumed, quoted-result continuation.
Equal requests are paired stateful transitions, never independent user lookups.

This is one word component. Its caller must also qualify the Bash process and
call sites, preserve program/frame ownership, and check ordinary word semantics.
It is not a shell interpreter, a runtime qualifier, or a one-field certificate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

import tree_sitter_bash
from tree_sitter import Language, Parser

from .model import (
    FieldProjection,
    ShellSourceError,
    Span,
    TextEvent,
    TildeEnvelope,
    TildePlan,
    TildeRequest,
)

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CHAIN = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:+~=-"
)
_BRACE = re.compile(r"\{[A-Za-z0-9_./:+-]+(?:,[A-Za-z0-9_./:+-]+)+\}")


def _fail(code: str, message: str) -> None:
    raise ShellSourceError(code, message)


def _canonical_expansion(text: str) -> str:
    if re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", text):
        return "${" + text[1:] + "}"
    return text


@dataclass(frozen=True)
class _Source:
    text: str
    # One origin and effective DATA protection entry per Unicode character.
    trusted: tuple[bool, ...]
    protected: tuple[bool, ...]
    # Complete trusted atoms, keyed by raw character position.
    atoms: dict[int, tuple[int, str]]
    # Original empty occurrence positions, in raw character coordinates.
    fences: frozenset[int] = frozenset()
    # Brace expansion creates ordered words before MAIN. These coordinates
    # map each selected character back to the unchanged authored field.
    origin: _Source | None = None
    positions: tuple[int, ...] = ()

    def byte(self, position: int) -> int:
        if self.origin is not None:
            position = (
                self.positions[position]
                if position < len(self.positions)
                else self.positions[-1] + 1
            )
            return self.origin.byte(position)
        return len(self.text[:position].encode("utf-8"))

    def span(self, start: int, end: int) -> tuple[int, int]:
        if self.origin is None:
            return start, end
        return self.origin.span(self.positions[start], self.positions[end - 1] + 1)


def _left(field: FieldProjection) -> _Source:
    if not field.source_events and field.parts:
        _fail("TILDE_SOURCE", "exact original field events are required")
    text: list[str] = []
    trusted: list[bool] = []
    protected: list[bool] = []
    indices: dict[int, int] = {}
    fences: set[int] = set()
    masks = {event: part.quoted for part in field.parts for event in part.events}
    for offset, event in enumerate(field.source_events):
        index = field.source_event_start + offset
        if not isinstance(event, TextEvent):
            fences.add(len(text))
            continue
        indices[index] = len(text)
        text.append(event.text)
        trusted.append(event.kind == "trusted")
        protected.append(masks.get(index, False))
    raw = "".join(text)
    atoms: dict[int, tuple[int, str]] = {}
    for part in field.parts:
        if part.kind != "expansion":
            continue
        positions = [indices[index] for index in part.events if index in indices]
        if not positions:
            _fail("TILDE_SOURCE", "trusted expansion has no original source")
        start, end = min(positions), max(positions) + 1
        # The enclosing compiler may have lowered a nested frame. Such an atom
        # is still valid outside a request, but cannot be copied into a request
        # as though its original and lowered source were identical.
        atoms[start] = (end, part.text)
    return _Source(raw, tuple(trusted), tuple(protected), atoms, frozenset(fences))


def _right(text: str) -> _Source:
    """Derive actual emitted atoms from a fresh parse, not encoder annotations."""
    prefix = b"__recipe_tilde__ "
    raw = text.encode("utf-8", errors="strict")
    root = Parser(Language(tree_sitter_bash.language())).parse(prefix + raw).root_node
    if (
        root.has_error
        or len(root.named_children) != 1
        or root.named_children[0].type != "command"
    ):
        _fail("TILDE_WITNESS", "emitted field changes executable topology")
    command = root.named_children[0]
    arguments = [
        node
        for index, node in enumerate(command.children)
        if command.field_name_for_child(index) == "argument"
    ]
    # The pinned grammar can split a contiguous word into adjacent argument
    # nodes (notably Unicode + assignment-colon-tilde concatenations). No
    # separator or operator may occur between these source slices.
    if (
        not arguments
        or arguments[0].start_byte != len(prefix)
        or arguments[-1].end_byte != len(prefix) + len(raw)
        or any(
            left.end_byte != right.start_byte
            for left, right in pairwise(arguments)
        )
    ):
        _fail("TILDE_WITNESS", "emitted field is not one complete source operand")
    atoms: dict[int, tuple[int, str]] = {}

    def walk(node) -> None:
        spelling = (prefix + raw)[node.start_byte : node.end_byte].decode()
        if (
            node.type
            in (
                "simple_expansion",
                "expansion",
                "command_substitution",
                "arithmetic_expansion",
                "process_substitution",
                "ansi_c_string",
            )
            or node.type == "string"
            and spelling.startswith('$"')
        ):
            start = len(raw[: node.start_byte - len(prefix)].decode())
            end = len(raw[: node.end_byte - len(prefix)].decode())
            atoms[start] = (end, spelling)
            return
        for child in node.named_children:
            walk(child)

    for argument in arguments:
        walk(argument)
    # Independently read simple parameter names at lexical positions as well;
    # the same grammar split can omit that node. Quote/escape/opaque-envelope
    # handling in MAIN decides whether each position is actually reached.
    for match in re.finditer(r"\$[A-Za-z_][A-Za-z0-9_]*", text):
        if not any(start <= match.start() < end for start, (end, _) in atoms.items()):
            atoms[match.start()] = (match.end(), match.group())
    for match in _BRACE.finditer(text):
        if not any(start <= match.start() < end for start, (end, _) in atoms.items()):
            atoms[match.start()] = (match.end(), match.group())
    return _Source(text, (True,) * len(text), (False,) * len(text), atoms)


def _brace_words(source: _Source) -> tuple[_Source, ...]:
    """Expand only the independently owned, hole-free flat list language.

    GNU subst.c:12985-13007 performs brace_expand_word_list before MAIN.
    Expansion order is left-to-right Cartesian order, not a symbolic choice
    of one successful alternative. Every generated word reaches MAIN in order.
    """
    selected: list[tuple[int, int, tuple[tuple[int, ...], ...]]] = []
    index = 0
    mode = "ordinary"
    text = source.text
    while index < len(text):
        char = text[index]
        syntax = source.trusted[index]
        if mode == "single":
            if syntax and char == "'":
                mode = "ordinary"
            index += 1
            continue
        if syntax and char == '"':
            mode = "ordinary" if mode == "double" else "double"
            index += 1
            continue
        if syntax and char == "'" and mode == "ordinary":
            mode = "single"
            index += 1
            continue
        if (
            syntax
            and char == "\\"
            and index + 1 < len(text)
            and (mode == "ordinary" or text[index + 1] in '$`"\\\n')
        ):
            index += 2
            continue
        atom = source.atoms.get(index) if syntax else None
        if atom is not None:
            end, spelling = atom
            if mode == "ordinary" and _BRACE.fullmatch(spelling):
                if (
                    spelling != text[index:end]
                    or not all(source.trusted[index:end])
                    or any(index < p < end for p in source.fences)
                ):
                    _fail(
                        "TILDE_BRACE_OWNER",
                        "pre-MAIN brace list must be exact, wholly authored source",
                    )
                choices = []
                start = index + 1
                for cursor in range(start, end):
                    if text[cursor] in ",}":
                        choices.append(tuple(range(start, cursor)))
                        start = cursor + 1
                selected.append((index, end, tuple(choices)))
            index = end
            continue
        index += 1
    if not selected:
        return (source,)
    words: list[tuple[int, ...]] = [()]
    cursor = 0
    for start, end, choices in selected:
        words = [
            prefix + tuple(range(cursor, start)) + choice
            for prefix in words
            for choice in choices
        ]
        if len(words) > 1024:
            _fail(
                "TILDE_GRAPH_LIMIT",
                "brace-generated words exceed the qualified graph bound",
            )
        cursor = end
    words = [prefix + tuple(range(cursor, len(text))) for prefix in words]
    result = []
    for positions in words:
        mapping = {old: new for new, old in enumerate(positions)}
        atoms = {}
        for start, (end, spelling) in source.atoms.items():
            if start in mapping and end - 1 in mapping:
                atoms[mapping[start]] = (mapping[end - 1] + 1, spelling)
        result.append(
            _Source(
                "".join(text[p] for p in positions),
                tuple(source.trusted[p] for p in positions),
                tuple(source.protected[p] for p in positions),
                atoms,
                frozenset(),
                source,
                positions,
            )
        )
    return tuple(result)


def _main_source(source: _Source) -> _Source:
    """Remove outer parser continuations, retaining exact source coordinates.

    Original opaque expansion interiors retain their independently owned raw
    spelling; their own parser/frame proof is separate. A continuation in the
    surrounding word is gone before MAIN observes positions or finder bytes.
    """
    positions = []
    index = 0
    mode = "ordinary"
    text = source.text
    while index < len(text):
        char, syntax = text[index], source.trusted[index]
        if mode == "single":
            if syntax and char == "'":
                mode = "ordinary"
            positions.append(index)
            index += 1
            continue
        atom = source.atoms.get(index) if syntax else None
        if atom is not None and not _BRACE.fullmatch(atom[1]):
            positions.extend(range(index, atom[0]))
            index = atom[0]
            continue
        if syntax and char == '"':
            mode = "ordinary" if mode == "double" else "double"
        elif syntax and char == "'" and mode == "ordinary":
            mode = "single"
        elif syntax and char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            if mode == "ordinary" or following in '$`"\\\n':
                if following != "\n":
                    positions.extend((index, index + 1))
                index += 2
                continue
        positions.append(index)
        index += 1
    if len(positions) == len(text):
        return source
    mapping = {old: new for new, old in enumerate(positions)}
    atoms = {
        mapping[start]: (mapping[end - 1] + 1, spelling)
        for start, (end, spelling) in source.atoms.items()
        if start in mapping and end - 1 in mapping
    }
    fences = frozenset(
        sum(index < fence for index in positions) for fence in source.fences
    )
    return _Source(
        "".join(text[p] for p in positions),
        tuple(source.trusted[p] for p in positions),
        tuple(source.protected[p] for p in positions),
        atoms,
        fences,
        source,
        tuple(positions),
    )


def _shape(source: _Source, role: str, *, original: bool) -> str | None:
    """Read the raw assignment predicate before choosing caller flags."""
    if role == "ASSIGN":
        return None  # The owning assignment has already separated its RHS.
    match = _NAME.match(source.text)
    if not match:
        return None
    end = match.end()
    rest = source.text[end:]
    if (
        rest.startswith("+=")
        or rest.startswith("[")
        and re.match(r"\[.*\]\+?=", rest, re.DOTALL)
    ):
        _fail(
            "TILDE_ASSIGNMENT_SHAPE",
            "append/subscript assignment arguments require their own owner",
        )
    if not rest.startswith("="):
        return None
    prefix = source.text[: end + 1]
    if original and (
        not all(source.trusted[: end + 1])
        or any(position < end + 1 for position in source.fences)
    ):
        _fail(
            "TILDE_ASSIGNMENT_SHAPE",
            "DATA cannot create or cross an assignment name/operator",
        )
    return prefix


@dataclass(frozen=True)
class _Profile:
    name: str
    assignment: bool
    declaration: bool
    rhs: bool
    posix: bool


def _profiles(role: str, prefix: str | None) -> tuple[_Profile, ...]:
    if role == "ASSIGN":
        return tuple(
            _Profile(f"rhs:{mode}", False, False, True, posix)
            for mode, posix in (("default", False), ("posix", True))
        )
    if role == "ARG":
        profiles = []
        for mode, posix in (("default", False), ("posix", True)):
            profiles.append(
                _Profile(
                    f"arg:ordinary:{mode}", prefix is not None, False, False, posix
                )
            )
            if prefix is not None:
                profiles.append(
                    _Profile(f"arg:declaration:{mode}", True, True, False, posix)
                )
        return tuple(profiles)
    if role in ("FILE", "FOR_VALUES"):
        # GNU 5.2.21 redir.c:305-322 and execute_cmd.c:2895 call
        # expand_words_no_vars -> shell_expand_word_list -> MAIN. Neither
        # dispatches an assignment builtin or sets W_ASSIGNRHS/W_TILDEEXP.
        # POSIX redirection adds W_NOSPLIT, not a tilde flag. FILE ownership
        # currently excludes here-strings; those use a different RHS caller.
        return tuple(
            _Profile(f"{role.lower()}:{mode}", prefix is not None, False, False, posix)
            for mode, posix in (("default", False), ("posix", True))
        )
    if role == "TEST_DATA":
        # execute_cmd.c:3916,3958-3965 -> subst.c:4117-4131 calls MAIN
        # directly, adding NOSPLIT2 only. Its Q_ARITH variant does not set
        # either Q_DOUBLE_QUOTES or Q_HERE_DOCUMENT in the tilde gate.
        # Pattern/regex and variable-name interpretation after MAIN belong
        # to the enclosing conditional owner, not this tilde proof.
        return tuple(
            _Profile(f"test:{mode}", prefix is not None, False, False, posix)
            for mode, posix in (("default", False), ("posix", True))
        )
    # Role names alone do not qualify the actual Bash expansion call site.
    return ()


def _finder(text: str, start: int, tflag: int) -> tuple[int, bool]:
    end = start
    while end < len(text) and text[end] != "/":
        if text[end] in "\\'\"":
            return end, True
        if tflag and text[end] == ":":
            break
        end += 1
    return end, False


class _Reader:
    def __init__(self, source: _Source, profile: _Profile, *, check_data: bool):
        self.source = source
        self.profile = profile
        self.check_data = check_data
        self.envelopes: list[tuple[int, int]] = []
        self.requests: list[TildeRequest] = []
        self.paths = 0

    def _preserve(
        self, start: int, end: int, activation: int, *, aborted: bool
    ) -> None:
        source = self.source
        # Aborted authored prefixes must retain their quote/escape and slash.
        # Scan the remainder only to establish the exact preservation interval;
        # it does not become a positive backend request.
        boundary = end
        if aborted:
            slash = source.text.find("/", start)
            boundary = len(source.text) if slash == -1 else slash
        request_end = boundary
        if boundary < len(source.text) and source.text[boundary] in "/:":
            boundary += 1
        if self.check_data:
            for position in range(start, request_end):
                if not source.trusted[position] and (
                    source.protected[position] or source.text[position] not in _CHAIN
                ):
                    _fail(
                        "TILDE_CHAIN_DOMAIN",
                        "DATA in a raw finder envelope is not an unprotected CHAIN literal",
                    )
            for atom_start, (atom_end, spelling) in source.atoms.items():
                if (
                    atom_start < boundary
                    and atom_end > start
                    and _canonical_expansion(source.text[atom_start:atom_end])
                    != _canonical_expansion(spelling)
                ):
                    _fail(
                        "TILDE_CHANGED_EXPANSION",
                        "a changed nested expansion cannot be copied into a raw finder envelope",
                    )
        self.envelopes.append(source.span(activation, boundary))

    def scan(
        self,
        index: int = 0,
        *,
        rhs: bool | None = None,
        assignoff: int = -1,
        mode: str = "ordinary",
        activation: int = -1,
        posix: bool | None = None,
        mode_unknown: bool = False,
        history: tuple[str, ...] = (),
    ) -> tuple:
        self.paths += 1
        if self.paths > 8192 or len(history) > 128:
            _fail(
                "TILDE_GRAPH_LIMIT",
                "request branching exceeds this component's qualified graph bound",
            )
        source, profile = self.source, self.profile
        text = source.text
        rhs = profile.rhs if rhs is None else rhs
        posix = profile.posix if posix is None else posix
        sequence: list[tuple] = []
        while index < len(text):
            char = text[index]
            syntax = source.trusted[index]
            if mode == "single":
                if syntax and char == "'":
                    mode = "ordinary"
                else:
                    sequence.append(("literal", char, char in "*?[]"))
                index += 1
                continue
            atom = source.atoms.get(index) if syntax else None
            # Brace expansion, when enabled for this caller, already ran.
            # Any remaining brace spelling is literal MAIN input.
            if atom is not None and _BRACE.fullmatch(atom[1]):
                atom = None
            # Opaque ANSI-C/translated quote atoms precede quote handling.
            if atom is not None:
                end, spelling = atom
                sequence.append(
                    ("expansion", _canonical_expansion(spelling), mode == "double", rhs)
                )
                # Trusted expansions are ordered opaque effects. Parameter or
                # arithmetic assignment can change POSIXLY_CORRECT in this
                # shell. Do not freeze its later gate at the field's entry
                # mode; pair both outcomes from the same trusted effect.
                mode_unknown = True
                index = end
                continue
            if syntax and char == '"':
                mode = "ordinary" if mode == "double" else "double"
                index += 1
                continue
            if syntax and char == "'" and mode == "ordinary":
                mode = "single"
                index += 1
                continue
            if syntax and char == "\\" and index + 1 < len(text):
                following = text[index + 1]
                if mode == "ordinary" or following in '$`"\\\n':
                    if following != "\n":
                        sequence.append(("literal", following, following in "*?[]"))
                    index += 2
                    continue
            protected = mode != "ordinary" or not syntax and source.protected[index]
            if (
                not protected
                and char in "=:"
                and mode_unknown
                and profile.assignment
                and not profile.declaration
                and not profile.rhs
            ):
                branches = []
                for label, next_mode in (("default", False), ("posix", True)):
                    branches.append(
                        self.scan(
                            index,
                            rhs=rhs,
                            assignoff=assignoff,
                            mode=mode,
                            activation=activation,
                            posix=next_mode,
                            history=history + ("mode:" + label,),
                        )
                    )
                sequence.append(("posix-guard", *branches))
                return tuple(sequence)
            if not protected and char == "=":
                if not rhs:
                    if (
                        profile.assignment
                        and (not posix or profile.declaration)
                        and assignoff == -1
                        and index > 0
                    ):
                        assignoff = index
                    if index == assignoff and text[index + 1 : index + 2] == "~":
                        activation = index
                    if profile.declaration:
                        rhs = True
            elif not protected and char == ":":
                if (
                    (profile.assignment or rhs)
                    and (not posix or profile.declaration or profile.rhs)
                    and text[index + 1 : index + 2] == "~"
                ):
                    activation = index
            elif char == "~":
                eligible = not protected and (index == 0 or activation >= 0)
                edge = max(activation, 0)
                activation = -1
                if eligible:
                    tflag = (
                        2
                        if rhs
                        else 1
                        if profile.assignment or profile.declaration
                        else 0
                    )
                    end, aborted = _finder(text, index, tflag)
                    self._preserve(index, end, edge, aborted=aborted)
                    if aborted:
                        sequence.append(("finder-abort", tflag))
                    else:
                        request = text[index:end]
                        self.requests.append(
                            TildeRequest(
                                source.byte(index),
                                request,
                                tflag,
                                profile.name,
                                history,
                            )
                        )
                        call = f"{tflag}:{request}"
                        fallback = self.scan(
                            index + 1,
                            rhs=rhs,
                            assignoff=assignoff,
                            mode=mode,
                            posix=posix,
                            mode_unknown=mode_unknown,
                            history=history + (call + ":unchanged",),
                        )
                        consumed = self.scan(
                            end,
                            rhs=rhs,
                            assignoff=assignoff,
                            mode=mode,
                            posix=posix,
                            mode_unknown=mode_unknown,
                            history=history + (call + ":consumed-quoted",),
                        )
                        sequence.append(
                            (
                                "request",
                                request,
                                tflag,
                                (("literal", "~", False),) + fallback,
                                consumed,
                            )
                        )
                        return tuple(sequence)
            sequence.append(("literal", char, protected and char in "*?[]"))
            index += 1
        if mode != "ordinary":
            _fail("TILDE_SOURCE", "request continuation leaves an unclosed quote")
        return tuple(sequence)


def _merge(source: _Source, spans: list[tuple[int, int]]) -> tuple[TildeEnvelope, ...]:
    # A generated-word span may begin/end inside a selected brace alternative.
    # Preservation owns the complete authored atom, never a rewritten branch.
    for index, (start, end) in enumerate(spans):
        for atom_start, (atom_end, spelling) in source.atoms.items():
            if _BRACE.fullmatch(spelling) and atom_start < end and atom_end > start:
                start, end = min(start, atom_start), max(end, atom_end)
        spans[index] = (start, end)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(
        TildeEnvelope(Span(source.byte(start), source.byte(end)))
        for start, end in merged
    )


def _read(
    field: FieldProjection, source: _Source, *, original: bool
) -> tuple[TildePlan, tuple]:
    raw_source = source
    source = _main_source(source)
    prefix = _shape(source, field.role, original=original)
    if original and prefix != field.authored_assignment_prefix and field.role == "ARG":
        _fail(
            "TILDE_ASSIGNMENT_SHAPE",
            "raw assignment shape differs from authored ownership",
        )
    profiles = _profiles(field.role, prefix)
    if not profiles:
        # Only admit fields with no possible tilde opportunity. The maximal
        # RHS probe is a rejection detector, not an invented caller profile;
        # actual DATA/FILE/etc. caller qualification cannot be inferred here.
        probe = _Reader(
            source,
            _Profile("unqualified", True, False, True, False),
            check_data=original,
        )
        graph = probe.scan()
        if probe.envelopes:
            _fail("TILDE_CALLER", "this role has no qualified tilde caller profile")
        return TildePlan((), ()), (prefix, (("no-tilde",),))
    envelopes = [source.span(0, len(prefix))] if prefix is not None else []
    requests: list[TildeRequest] = []
    graphs = []
    # True assignment RHS (and conditional operands) call MAIN directly;
    # ordinary arguments, redirects and for-lists go through brace expansion.
    expanded = (
        _brace_words(source)
        if field.role in ("ARG", "FILE", "FOR_VALUES")
        else (source,)
    )
    modes = (
        (("", (source,)),)
        if len(expanded) == 1 and expanded[0] is source
        else ((":brace:on", expanded), (":brace:off", (source,)))
    )
    for profile in profiles:
        for brace_mode, words in modes:
            word_graphs = []
            for ordinal, word in enumerate(words):
                if word is not source:
                    # subst.c:12452-12459 reconstructs changed words using
                    # make_word_flags; make_cmd.c:102-130 retains only dollar
                    # and quote flags, not ASSIGNMENT/ASSIGNARG/TILDEEXP.
                    caller = _Profile(
                        profile.name + brace_mode, False, False, False, profile.posix
                    )
                else:
                    caller = _Profile(
                        profile.name + brace_mode,
                        profile.assignment,
                        profile.declaration,
                        profile.rhs,
                        profile.posix,
                    )
                reader = _Reader(word, caller, check_data=original)
                history = (f"after-brace-words:{ordinal}",) if len(words) > 1 else ()
                graph = reader.scan(history=history)
                # No MAIN event means this component has no stateful transition
                # to pair. The independent word witness owns ordinary semantics.
                word_graphs.append(graph if reader.envelopes else ("no-tilde",))
                envelopes.extend(reader.envelopes)
                requests.extend(reader.requests)
            graphs.append((profile.name + brace_mode, tuple(word_graphs)))
    return TildePlan(_merge(raw_source, envelopes), tuple(requests)), (
        prefix,
        tuple(graphs),
    )


def plan_tildes(field: FieldProjection) -> TildePlan:
    """Return full raw source envelopes, before scalar or literal encoding."""
    return _read(field, _left(field), original=True)[0]


def verify_tildes(field: FieldProjection, emitted: str) -> None:
    """Pair actual request graphs, including every opaque backend branch.

    No compiler envelope, supplied environment, HOME, passwd entry or preferred
    lookup result participates in the right-hand proof. Caller/process-wide
    state induction remains the enclosing compiler's obligation.
    """
    _, expected = _read(field, _left(field), original=True)
    _, actual = _read(field, _right(emitted), original=False)
    if expected != actual:
        _fail(
            "TILDE_WITNESS",
            "emitted assignment shape, request history, or branch semantics differ",
        )
