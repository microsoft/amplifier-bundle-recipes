"""Independent semantic and whole-program negative controls for =~ owners."""

from dataclasses import replace

import pytest
from amplifier_module_tool_recipes.shell_source import prepare
from amplifier_module_tool_recipes.shell_source.binding import bind, events_in
from amplifier_module_tool_recipes.shell_source.compiler import compile_bound
from amplifier_module_tool_recipes.shell_source.model import ShellSourceError, Span
from amplifier_module_tool_recipes.shell_source.model import FenceEvent
from amplifier_module_tool_recipes.shell_source.ownership import establish_ownership
from amplifier_module_tool_recipes.shell_source.witness import verify_program
from amplifier_module_tool_recipes.shell_source.word_witness import verify_field
from amplifier_module_tool_recipes.shell_source.words import cook_island


@pytest.mark.parametrize("source", [
    "[[ {{lhs}} =~ {{p}} ]]", '[[ {{lhs}} =~ "{{p}}" ]]',
])
def test_only_rhs_owns_regex(source):
    bound = bind(source, {"lhs": "abc", "p": "a.c"}.__getitem__)
    assert [w.role for w in establish_ownership(bound).words] == ["TEST_DATA", "TEST_REGEX"]


@pytest.mark.parametrize("source,value,forged", [
    ("[[ abc =~ {{p}} ]]", "a.c", "'a.c'"),
    ('[[ abc =~ "{{p}}" ]]', "a.c", "a.c"),
    ("[[ abc =~ {{p}} ]]", "^a.c$", "a.c$"),
    ("[[ abc =~ {{p}} ]]", "^(a)(bc)$", "^(a)(b)c$"),
    ("[[ abc =~ {{p}} ]]", "[[:alpha:]]", "'[[:alpha:]]'"),
    ('[[ abc =~ [a"-"{{p}}] ]]', "z", "[a-z]"),
    ("[[ abc =~ {{p}} ]]", "abc", "a${HOME}bc"),
    ("[[ abc =~ {{p}} ]]", "abc", "a$(touch marker)bc"),
    ("[[ abc =~ {{p}} ]]", "abc", "a`touch marker`bc"),
    ("[[ abc =~ {{p}} ]]", "abc", "a$((1+2))bc"),
    ("[[ abc =~ {{p}} ]]", "abc", "a<(touch marker)bc"),
    ("[[ abc =~ {{p}} ]]", "abc", "abc || true"),
    ("[[ abc =~ {{p}} ]]", "abc", "abc extra"),
])
def test_forged_regex_bytes_fail_independent_local_witness(source, value, forged):
    compiled = compile_bound(bind(source, lambda _: value))
    field = compiled.replacements[0].fields[0]
    with pytest.raises(ShellSourceError):
        verify_field(field, forged)


def test_empty_quote_scaffolding_does_not_shift_regex_protection():
    compiled = compile_bound(bind('[[ abc =~ "{{p}}" ]]', lambda _: "a'.b"))
    field = compiled.replacements[0].fields[0]
    verify_field(field, '"a\'"' + '""' + '".b"')
    with pytest.raises(ShellSourceError):
        verify_field(field, '"a\'".b')


@pytest.mark.parametrize("forged", [
    "a${HOME}bc", "a$(touch marker)bc", "a`touch marker`bc",
    "a$((1+2))bc", "a<(touch marker)bc", "abc || true",
    "abc extra", "abc ]] || [[ true",
])
def test_opaque_regex_cannot_hide_executable_structure_from_program_witness(forged):
    bound = bind("[[ abc =~ {{p}} ]]", lambda _: "abc")
    compiled = compile_bound(bound)
    original = compiled.replacements[0]
    edit = replace(original, text=forged)
    raw = bound.source.encode()
    candidate = (raw[:edit.source.start] + forged.encode() + raw[edit.source.end:]).decode()
    with pytest.raises(ShellSourceError):
        verify_program(bound, compiled.ownership, (edit,), candidate)


def test_changed_operator_and_lost_or_crossed_owners_fail():
    bound = bind("[[ abc =~ {{p}} ]]", lambda _: "abc")
    compiled = compile_bound(bound)
    with pytest.raises(ShellSourceError):
        verify_program(bound, compiled.ownership, compiled.replacements, compiled.source.replace("=~", "=="))
    with pytest.raises(ShellSourceError):
        verify_program(bound, compiled.ownership, (), compiled.source)
    edit = compiled.replacements[0]
    bad = replace(edit, source=Span(edit.source.start - 1, edit.source.end))
    with pytest.raises(ShellSourceError):
        verify_program(bound, compiled.ownership, (bad,), compiled.source)


def test_adjacent_and_empty_fences_survive_regex_escape_composition():
    bound = bind(
        r"[[ a.c =~ a{{slash}}{{empty}}{{dot}}c ]]",
        {"slash": "\\", "empty": "", "dot": "."}.__getitem__,
    )
    compiled = compile_bound(bound)
    edit = compiled.replacements[0]
    assert edit.source == compiled.ownership.words[0].span
    field = edit.fields[0]
    assert [(f.occurrence, f.edge) for f in field.fences] == [
        (e.occurrence, e.edge) for e in field.source_events if isinstance(e, FenceEvent)
    ]
    dot = next(p for p in field.parts if p.text == ".")
    assert dot.data and dot.quoted and len(dot.events) == 2


@pytest.mark.parametrize("source", [
    '[[ "{{p}}" == abc ]]', '[[ "{{p}}" != xyz ]]', '[ "{{p}}" = abc ]',
])
def test_nonregex_operands_do_not_enter_regex_path(source):
    bound = bind(source, lambda _: "abc")
    assert all(w.role != "TEST_REGEX" for w in establish_ownership(bound).words)
    prepare(source, lambda _: "abc")


@pytest.mark.parametrize("value", [
    "$[1+1]", "$[x]", "$[x=7]", "λ$[a[1]+1]", "$[(x<<2)|1]",
    "$[$(cat <<DOC\n2\nDOC\n)+1]",
])
def test_legacy_arithmetic_same_bytes_are_not_literal_field_authority(value):
    bound = bind("[[ abc =~ {{p}} ]]", lambda _: value)
    owner = establish_ownership(bound).words[0]
    field = cook_island(events_in(bound, owner.span), owner.role).pieces[0]
    # The emitted bytes are IDENTICAL to DATA, but Bash evaluates them.
    with pytest.raises(ShellSourceError):
        verify_field(field, value)
    with pytest.raises(ShellSourceError):
        prepare(bound.source, lambda _: value)


@pytest.mark.parametrize("forged", [
    "$[1+1]", "$[x]", "$[x=7]", "λ$[a[1]+1]",
    "$[$(printf 2)+1]", "$[$(printf 2 | cat)+1]",
    "$[(x<<2)|1]", "$[$(cat <<DOC\n2\nDOC\n)+1]",
])
def test_legacy_arithmetic_forgery_fails_program_without_field_witness(forged):
    bound = bind("[[ abc =~ {{p}} ]]", lambda _: "abc")
    compiled = compile_bound(bound)
    edit = replace(compiled.replacements[0], text=forged, fields=())
    raw = bound.source.encode()
    candidate = (raw[:edit.source.start] + forged.encode() + raw[edit.source.end:]).decode()
    with pytest.raises(ShellSourceError, match="PROGRAM_WITNESS"):
        verify_program(bound, compiled.ownership, (edit,), candidate)


@pytest.mark.parametrize("atom", [
    "$[x]", "$[x=7]", "$[x*3+1]", "$[a[1]+2]",
    "$[$(printf 2)+1]", "$[$(printf 2 | cat)+1]",
    "$[$(printf '%s' ']'; printf 2)+1]",
    "$[(x<<2)|1]", "$[a[1<<1]+2]", "$[(a[1]<<2)|1]",
    "$[$(cat <<FIRST <<SECOND\n1\nFIRST\n2\nSECOND\n)+1]",
])
def test_legacy_arithmetic_reader_recovers_complete_byte_span(atom):
    from amplifier_module_tool_recipes.shell_source.regex import (
        _legacy_arithmetic_span, parameter_atoms, read_regex,
    )

    raw = ("λ" + atom + "z").encode()
    assert _legacy_arithmetic_span(raw, len("λ".encode())) == Span(
        len("λ".encode()), len(("λ" + atom).encode()),
    )
    atoms = {}
    read_regex("λ" + atom + "z", expansions=atoms)
    assert atoms == {1: (1 + len(atom), atom)}  # Character, not byte offsets.
    bound = bind("[[ abc =~ λ" + atom + "{{p}} ]]", lambda _: "z")
    owner = establish_ownership(bound).words[0]
    recovered = parameter_atoms(bound, owner.span)
    assert len(recovered) == 1
    expansion = next(iter(recovered.values()))
    assert expansion.kind == "arithmetic_expansion"
    assert bound.source.encode()[expansion.source.start:expansion.source.end] == atom.encode()


@pytest.mark.parametrize("source", [
    "[[ 2 =~ $[x+{{p}}] ]]",
    "[[ 2 =~ $[x{{p}}+1] ]]",
    "[[ 2 =~ $[$(printf '{{p}}')+1] ]]",
    "[[ 2 =~ $(printf '%s' $[$(printf '{{p}}')+1]) ]]",
    "[[ 2 =~ ${UNSET:-$[x+{{p}}]} ]]",
])
@pytest.mark.parametrize("value", ["", "1"])
def test_original_arithmetic_cannot_absorb_even_nested_or_empty_occurrence(source, value):
    with pytest.raises(ShellSourceError):
        prepare(source, lambda _: value)


@pytest.mark.parametrize("source", [
    "[[ 2 =~ $[x+{{p}}] ]]",
    "[[ 2 =~ $[$(printf '{{p}}')+1] ]]",
    "[[ 2 =~ $(printf '%s' $[$(printf '{{p}}')+1]) ]]",
    "[[ 2 =~ ${UNSET:-$[x+{{p}}]} ]]",
])
def test_program_checks_original_arithmetic_authority_without_compiler(source):
    from amplifier_module_tool_recipes.shell_source.binding import neutral_bytes
    from amplifier_module_tool_recipes.shell_source.documents import collect_documents
    from amplifier_module_tool_recipes.shell_source.ownership import parse
    from amplifier_module_tool_recipes.shell_source.witness import _Program

    bound = bind(source, lambda _: "")
    neutral = neutral_bytes(bound)
    docs = collect_documents(bound)
    with pytest.raises(ShellSourceError):
        _Program(
            neutral, docs, frozenset(), source.encode(),
            tuple(o.span for o in bound.occurrences),
        ).key(parse(neutral))


@pytest.mark.parametrize("forged", [
    "$[3+1]z", "$[$(printf 3)+1]z", "$[$(echo 2)+1]z",
    "$[$(printf 2 && cat)+1]z", "$[2+1]z",
])
def test_legacy_arithmetic_descendants_cannot_disappear_from_program(forged):
    bound = bind("[[ 3z =~ $[$(printf 2 | cat)+1]{{p}} ]]", lambda _: "z")
    compiled = compile_bound(bound)
    edit = replace(compiled.replacements[0], text=forged, fields=())
    raw = bound.source.encode()
    candidate = (raw[:edit.source.start] + forged.encode() + raw[edit.source.end:]).decode()
    with pytest.raises(ShellSourceError, match="PROGRAM_WITNESS"):
        verify_program(bound, compiled.ownership, (edit,), candidate)


# These tests inspect source only; forged programs are never run.
DOCUMENT_ARITHMETIC = "$[$(cat <<FIRST <<SECOND\n1\nFIRST\n2\nSECOND\n)+1]"


@pytest.mark.parametrize("quoted", [False, True])
def test_legacy_arithmetic_collects_real_document_fifo_and_keeps_byte_coordinates(quoted):
    from amplifier_module_tool_recipes.shell_source.documents import collect_documents
    from amplifier_module_tool_recipes.shell_source.ownership import parse

    atom = '"' + DOCUMENT_ARITHMETIC + '"' if quoted else DOCUMENT_ARITHMETIC
    source = "printf 'λ%s' " + atom + " '{{p}}'"
    bound = bind(source, lambda _: "z")
    docs = collect_documents(bound)
    assert len(docs.skeleton) == len(source.encode())
    first, second = docs.documents
    assert [(d.frame, d.ordinal, d.delimiter, d.quoted, d.strip_tabs) for d in docs.documents] == [
        (1, 0, "FIRST", False, False), (1, 1, "SECOND", False, False),
    ]
    raw = source.encode()
    for document, delimiter, body in [(first, b"FIRST", b"1\n"), (second, b"SECOND", b"2\n")]:
        assert raw[document.opener_delimiter.start:document.opener_delimiter.end] == delimiter
        assert raw[document.closer_delimiter.start:document.closer_delimiter.end] == delimiter
        assert raw[document.body.start:document.body.end] == body
        assert docs.contexts[document.body.start].mode == "heredoc"
        assert docs.contexts[document.body.start].frame == 1
    assert first.body.end < second.body.start
    suffix = raw.index(b"+1]")
    assert docs.contexts[suffix].mode == "arithmetic"
    assert docs.contexts[suffix].frame == 0
    assert docs.contexts[bound.occurrences[0].span.start].mode == "single"
    parse(docs.skeleton)


def test_legacy_arithmetic_nested_fifo_does_not_consume_outer_pending_document():
    from amplifier_module_tool_recipes.shell_source.documents import collect_documents
    from amplifier_module_tool_recipes.shell_source.ownership import parse

    source = "cat <<OUTER $(printf '%s' " + DOCUMENT_ARITHMETIC + ")\nouter\nOUTER\n"
    docs = collect_documents(bind(source, lambda _: ""))
    assert [(d.frame, d.ordinal, d.delimiter) for d in docs.documents] == [
        (0, 0, "OUTER"), (2, 1, "FIRST"), (2, 2, "SECOND"),
    ]
    assert [source.encode()[d.body.start:d.body.end] for d in docs.documents] == [
        b"outer\n", b"1\n", b"2\n",
    ]
    parse(docs.skeleton)


@pytest.mark.parametrize("before,after", [
    ("cat", "tac"),
    ("\n1\n", "\n8\n"),  # Even the redirected-away first document is owned.
    ("\n2\n", "\n4\n"),
    ("FIRST", "OTHER"),
    ("<<FIRST", "<<'FIRST'"),
    ("<<FIRST <<SECOND\n1\nFIRST\n2\nSECOND\n", "<<SECOND <<FIRST\n2\nSECOND\n1\nFIRST\n"),
    (DOCUMENT_ARITHMETIC, "$[$(printf 2)+1]"),
])
@pytest.mark.parametrize("quoted", [False, True])
def test_program_only_legacy_arithmetic_cannot_certify_changed_documents(before, after, quoted):
    atom = '"' + DOCUMENT_ARITHMETIC + '"' if quoted else DOCUMENT_ARITHMETIC
    source = "[[ 3z =~ " + atom + "{{p}} ]]"
    bound = bind(source, lambda _: "z")
    compiled = compile_bound(bound)
    original = compiled.replacements[0]
    forged = original.text.replace(before, after)
    assert forged != original.text
    edit = replace(original, text=forged, fields=())
    raw = source.encode()
    candidate = (raw[:edit.source.start] + forged.encode() + raw[edit.source.end:]).decode()
    with pytest.raises(ShellSourceError, match="PROGRAM_WITNESS"):
        verify_program(bound, compiled.ownership, (edit,), candidate)


@pytest.mark.parametrize("value", ["", "1"])
@pytest.mark.parametrize("atom", [
    "$[(x<<2)+{{p}}]",
    "$[a[{{p}}]+1]",
    "$[$(printf '{{p}}')+1]",
    "$[$(cat <<DOC\n{{p}}\nDOC\n)+1]",
])
def test_neutral_legacy_endpoint_recovery_reaches_typed_data_ownership_refusal(atom, value):
    from amplifier_module_tool_recipes.shell_source.binding import neutral_bytes
    from amplifier_module_tool_recipes.shell_source.documents import collect_documents
    from amplifier_module_tool_recipes.shell_source.ownership import parse
    from amplifier_module_tool_recipes.shell_source.regex import parameter_atoms
    from amplifier_module_tool_recipes.shell_source.witness import _Program

    source = "[[ 3 =~ " + atom + " ]]"
    bound = bind(source, lambda _: value)
    # Read the complete RHS directly, without allowing an earlier ownership
    # refusal to hide whether endpoint recovery actually used neutral syntax.
    span = Span(len(b"[[ 3 =~ "), len(source.encode()) - 3)
    with pytest.raises(ShellSourceError) as refused:
        parameter_atoms(bound, span)
    assert refused.value.code == "DATA_ARITHMETIC_SYNTAX"
    docs = collect_documents(bound)
    with pytest.raises(ShellSourceError, match="PROGRAM_WITNESS") as refused:
        _Program(
            docs.skeleton, docs, frozenset(), source.encode(),
            tuple(o.span for o in bound.occurrences), syntax=neutral_bytes(bound),
        ).key(parse(docs.skeleton))
    assert "legacy arithmetic crosses a DATA occurrence" in str(refused.value)


# Closed boundary regressions. Synthetic candidates below are NEVER executed.
BOUNDARY_NAMESPACES = [
    "amplifier_module_tool_recipes.shell_source",
    "amplifier_recipe_runner._shell_source",
]


def boundary_module(namespace, component):
    from importlib import import_module

    return import_module(namespace + "." + component)


def boundary_candidate(bound, edits):
    raw = bound.source.encode()
    chunks = []
    cursor = 0
    for edit in edits:
        chunks.extend((raw[cursor:edit.source.start], edit.text.encode()))
        cursor = edit.source.end
    return b"".join(chunks + [raw[cursor:]]).decode()


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("style", ["legacy", "modern"])
@pytest.mark.parametrize("role", ["ARG", "ASSIGN", "FILE", "FOR_VALUES", "TEST_REGEX"])
@pytest.mark.parametrize("quoted", [False, True])
def test_boundary_authored_document_corruption_is_independently_refused(namespace, style, role, quoted):
    atom = (
        "$[$(cat <<DOC\n2\nDOC\n)+1]" if style == "legacy"
        else "$(($(cat <<DOC\n2\nDOC\n)+1))"
    )
    word = ('"' + atom + '"' if quoted else atom) + "{{p}}"
    source = {
        "ARG": "printf '%s' " + word,
        "ASSIGN": "x=" + word,
        "FILE": "cat >" + word,
        "FOR_VALUES": "for x in " + word + "; do :; done",
        "TEST_REGEX": "[[ 3z =~ " + word + " ]]",
    }[role]
    binding = boundary_module(namespace, "binding")
    compiler = boundary_module(namespace, "compiler")
    model = boundary_module(namespace, "model")
    witness = boundary_module(namespace, "witness")
    bound = binding.bind(source, lambda _: "z")
    compiled = compiler.compile_bound(bound)
    witness.verify_program(bound, compiled.ownership, compiled.replacements, compiled.source)
    original = next(e for e in compiled.replacements if "\n2\n" in e.text)
    forged = original.text.replace("\n2\n", "\n4\n")
    edits = tuple(
        replace(e, text=forged, fields=()) if e == original else replace(e, fields=())
        for e in compiled.replacements
    )
    with pytest.raises(model.ShellSourceError, match="PROGRAM_WITNESS"):
        witness.verify_program(bound, compiled.ownership, edits, boundary_candidate(bound, edits))
    # The ordinary-ARG local certificate remains independent of the program.
    if role == "ARG":
        field = next(f for f in original.fields if any(p.kind == "expansion" for p in f.parts))
        field_text = ('"' + atom + '"' if quoted else atom) + "z"
        local = boundary_module(namespace, "word_witness")
        local.verify_field(field, field_text)
        with pytest.raises(model.ShellSourceError, match="WORD_WITNESS"):
            local.verify_field(field, field_text.replace("\n2\n", "\n4\n"))


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("style", ["modern", "bare"])
def test_boundary_explicit_authored_body_edit_has_no_data_authority(namespace, style):
    atom = (
        "x=$(($(cat <<DOC\n2\nDOC\n)+1))" if style == "modern"
        else "((x=$(cat <<DOC\n2\nDOC\n)+1))"
    )
    source = atom + "; printf '%s' '{{p}}'"
    binding = boundary_module(namespace, "binding")
    model = boundary_module(namespace, "model")
    witness = boundary_module(namespace, "witness")
    bound = binding.bind(source, lambda _: "z")
    compiled = boundary_module(namespace, "compiler").compile_bound(bound)
    body = compiled.ownership.documents.documents[0].body
    edits = tuple(sorted(
        compiled.replacements + (model.Replacement(body, "4\n", "document-body", "BODY"),),
        key=lambda e: e.source.start,
    ))
    with pytest.raises(model.ShellSourceError, match="PROGRAM_WITNESS"):
        witness.verify_program(bound, compiled.ownership, edits, boundary_candidate(bound, edits))


BOUNDARY_PARAMETERS = [
    '${v//[/1}', '${v//]/1}', '${v//"["/1}', r'${v//\[/1}', r'${v//\}/1}',
    '${arr[i]}', '${v:-${w:-2}}', '${v:-$((1+1))}',
    '${v:-$(cat <<DOC\n2\nDOC\n)}',
]


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("parameter", BOUNDARY_PARAMETERS)
@pytest.mark.parametrize("style", ["legacy", "modern", "bare"])
def test_boundary_parameter_lexical_endpoint_and_contexts(namespace, parameter, style):
    documents = boundary_module(namespace, "documents")
    binding = boundary_module(namespace, "binding")
    expression = {
        "legacy": "$[ " + parameter + " + 1 ]",
        "modern": "$(( " + parameter + " + 1 ))",
        "bare": "(( x = " + parameter + " + 1 ))",
    }[style]
    source = "λ " + expression + " suffix"
    bound = binding.bind(source, lambda _: "")
    reader = documents._Reader(bound)
    start = len("λ ".encode())
    end = reader.arithmetic(start, 0, dollar=style != "bare")
    assert end == len(("λ " + expression).encode())
    assert len(reader.skeleton) == len(source.encode()) == len(reader.contexts)
    assert reader.contexts[end - 1].mode == "arithmetic"
    if '"["' in parameter:
        at = source.encode().index(b'"["')
        assert [c.mode for c in reader.contexts[at:at + 3]] == ["double"] * 3
    if "\\" in parameter:
        at = source.encode().index(b"\\")
        assert [c.mode for c in reader.contexts[at:at + 2]] == ["arithmetic"] * 2
    if "<<DOC" in parameter:
        doc, = reader.documents
        assert source.encode()[doc.body.start:doc.body.end] == b"2\n"
        assert reader.contexts[doc.body.start].mode == "heredoc"
        assert reader.contexts[doc.body.start].frame == 1
        assert reader.contexts[source.encode().index(b"cat")].mode == "ordinary"


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("substitution", [
    "$(cat <<FIRST <<SECOND\n1\nFIRST\n2\nSECOND\n)",
    "`cat <<FIRST <<SECOND\n1\nFIRST\n2\nSECOND\n`",
])
def test_boundary_parameter_nested_fifo_retains_outer_frame(namespace, substitution):
    source = "cat <<OUTER $[${v:-" + substitution + "}+1]\nouter\nOUTER\n"
    binding = boundary_module(namespace, "binding")
    docs = boundary_module(namespace, "documents").collect_documents(binding.bind(source, lambda _: ""))
    assert [(d.frame, d.ordinal, d.delimiter) for d in docs.documents] == [
        (0, 0, "OUTER"), (1, 1, "FIRST"), (1, 2, "SECOND"),
    ]
    assert [source.encode()[d.body.start:d.body.end] for d in docs.documents] == [
        b"outer\n", b"1\n", b"2\n",
    ]
    assert docs.contexts[source.encode().index(b"+1]")].mode == "arithmetic"
    assert docs.contexts[source.encode().index(b"+1]")].frame == 0


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
def test_boundary_parameter_does_not_hide_empty_legacy_data_fence(namespace):
    source = "printf '%s' $[${v:-$(cat <<DOC\n{{empty}}\nDOC\n)}+1]"
    binding = boundary_module(namespace, "binding")
    model = boundary_module(namespace, "model")
    bound = binding.bind(source, lambda _: "")
    docs = boundary_module(namespace, "documents").collect_documents(bound)
    witness = boundary_module(namespace, "witness")
    root = boundary_module(namespace, "ownership").parse(docs.skeleton)
    with pytest.raises(model.ShellSourceError, match="PROGRAM_WITNESS"):
        witness._Program(
            docs.skeleton, docs, frozenset(), source.encode(),
            tuple(o.span for o in bound.occurrences), syntax=binding.neutral_bytes(bound),
        ).key(root)
    with pytest.raises(model.ShellSourceError):
        boundary_module(namespace, "compiler").compile_bound(bound)


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("style", ["modern", "bare"])
@pytest.mark.parametrize("value", ["2", "", "DOC\n2", "$[1+1]", "λ"])
def test_boundary_data_document_does_not_exempt_authored_sibling(namespace, style, value):
    inner = "$(cat <<FIRST <<DOC\n7\nFIRST\n{{n}}\nDOC\n)"
    expression = "x=$((" + inner + "+1))" if style == "modern" else "((x=" + inner + "+1))"
    source = expression + "; printf '%s' \"$x\""
    binding = boundary_module(namespace, "binding")
    model = boundary_module(namespace, "model")
    witness = boundary_module(namespace, "witness")
    bound = binding.bind(source, lambda _: value)
    compiled = boundary_module(namespace, "compiler").compile_bound(bound)
    witness.verify_program(bound, compiled.ownership, compiled.replacements, compiled.source)
    first, _ = compiled.ownership.documents.documents
    edits = tuple(sorted(
        tuple(replace(e, fields=()) for e in compiled.replacements)
        + (model.Replacement(first.body, "8\n", "document-body", "BODY"),),
        key=lambda e: e.source.start,
    ))
    with pytest.raises(model.ShellSourceError, match="PROGRAM_WITNESS"):
        witness.verify_program(bound, compiled.ownership, edits, boundary_candidate(bound, edits))


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("style", ["legacy", "modern", "bare"])
def test_boundary_arithmetic_body_descriptor_keeps_unmasked_authored_bytes(namespace, style):
    inner = "$(cat <<DOC\n2\nDOC\n)"
    expression = {
        "legacy": "$[" + inner + "+1]",
        "modern": "$((" + inner + "+1))",
        "bare": "((" + inner + "+1))",
    }[style]
    source = expression if style == "bare" else "printf '%s' " + expression
    binding = boundary_module(namespace, "binding")
    documents = boundary_module(namespace, "documents")
    ownership = boundary_module(namespace, "ownership")
    witness = boundary_module(namespace, "witness")
    def signature(text):
        docs = documents.collect_documents(binding.bind(text, lambda _: ""))
        return witness._Program(docs.skeleton, docs, frozenset(), syntax=text.encode()).key(
            ownership.parse(docs.skeleton)
        )
    assert signature(source) != signature(source.replace("\n2\n", "\n4\n"))


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("value", ["2", "", "DOC\n+2"])
def test_boundary_regex_mini_program_rebases_only_original_data_body(namespace, quoted, value):
    atom = "$(($(cat <<FIRST <<DOC\n7\nFIRST\n{{n}}\nDOC\n)+1))"
    word = ('"' + atom + '"' if quoted else atom) + "{{p}}"
    # λ forces character/byte rebasing to differ at both outer and mini spans.
    source = ": 'λ'; [[ λ3z =~ λ" + word + " ]]"
    binding = boundary_module(namespace, "binding")
    compiler = boundary_module(namespace, "compiler")
    model = boundary_module(namespace, "model")
    witness = boundary_module(namespace, "witness")
    bound = binding.bind(source, {"n": value, "p": "z"}.__getitem__)
    compiled = compiler.compile_bound(bound)
    witness.verify_program(bound, compiled.ownership, compiled.replacements, compiled.source)
    original = next(e for e in compiled.replacements if "\n7\n" in e.text)
    edits = tuple(
        replace(e, text=e.text.replace("\n7\n", "\n8\n"), fields=())
        if e == original else replace(e, fields=())
        for e in compiled.replacements
    )
    with pytest.raises(model.ShellSourceError, match="PROGRAM_WITNESS"):
        witness.verify_program(bound, compiled.ownership, edits, boundary_candidate(bound, edits))


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("style", ["modern", "bare"])
def test_boundary_header_lf_handoff_authority_is_original_and_document_local(namespace, style):
    inner = "$(cat <<FIRST <<SECOND {{suffix}}\n7\nFIRST\n2\nSECOND\n)"
    expression = "x=$((" + inner + "+1))" if style == "modern" else "((x=" + inner + "+1))"
    source = expression + "; printf '%s' \"$x\""
    binding = boundary_module(namespace, "binding")
    compiler = boundary_module(namespace, "compiler")
    model = boundary_module(namespace, "model")
    witness = boundary_module(namespace, "witness")
    bound = binding.bind(source, lambda _: "-\n\n")
    compiled = compiler.compile_bound(bound)
    first, second = compiled.ownership.documents.documents
    assert witness._mutable_documents(bound, compiled.ownership) == frozenset([first.opener_delimiter])
    witness.verify_program(bound, compiled.ownership, compiled.replacements, compiled.source)
    edits = tuple(sorted(
        compiled.replacements
        + (model.Replacement(second.body, "4\n", "document-body", "BODY"),),
        key=lambda e: e.source.start,
    ))
    with pytest.raises(model.ShellSourceError, match="PROGRAM_WITNESS"):
        witness.verify_program(bound, compiled.ownership, edits, boundary_candidate(bound, edits))


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("parameter", [
    "${arr[(i+1)]}", r"${arr[i\]]}", '${arr[${v//]/1}]}',
    '${arr[$(cat <<DOC\n2\nDOC\n)]}', '${v//(/1}', '${v:-"}"}',
])
def test_boundary_parameter_keeps_actual_subscript_and_escaped_closers(namespace, parameter):
    binding = boundary_module(namespace, "binding")
    documents = boundary_module(namespace, "documents")
    source = "$(( " + parameter + " + 1 )) suffix"
    reader = documents._Reader(binding.bind(source, lambda _: ""))
    assert reader.arithmetic(0, 0, dollar=True) == len(source.encode()) - len(b" suffix")
    if "<<DOC" in parameter:
        doc, = reader.documents
        assert source.encode()[doc.body.start:doc.body.end] == b"2\n"
        assert reader.contexts[doc.body.start].mode == "heredoc"


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("earlier", ["-", '"a b"', r"a\ b", "$(printf -)"])
@pytest.mark.parametrize("value", ["-\n\n", "- -\n\n"])
def test_boundary_multiargument_handoff_uses_original_final_field(namespace, earlier, value):
    source = (
        "x=$(( $(cat <<FIRST <<SECOND " + earlier + " {{suffix}}\n"
        "7\nFIRST\n2\nSECOND\n) + 1 )); printf '%s' \"$x\""
    )
    bound = boundary_module(namespace, "binding").bind(source, lambda _: value)
    compiled = boundary_module(namespace, "compiler").compile_bound(bound)
    witness = boundary_module(namespace, "witness")
    model = boundary_module(namespace, "model")
    first, second = compiled.ownership.documents.documents
    assert witness._mutable_documents(bound, compiled.ownership) == frozenset([first.opener_delimiter])
    witness.verify_program(bound, compiled.ownership, compiled.replacements, compiled.source)
    edits = tuple(sorted(
        tuple(replace(e, fields=()) for e in compiled.replacements)
        + (model.Replacement(second.body, "4\n", "document-body", "BODY"),),
        key=lambda edit: edit.source.start,
    ))
    with pytest.raises(model.ShellSourceError, match="PROGRAM_WITNESS"):
        witness.verify_program(bound, compiled.ownership, edits, boundary_candidate(bound, edits))


@pytest.mark.parametrize("namespace", BOUNDARY_NAMESPACES)
@pytest.mark.parametrize("prefix", ['"-"', r"\-", "$(printf -)", "''-"])
def test_boundary_handoff_final_field_still_requires_unprotected_literal(namespace, prefix):
    source = (
        "x=$(( $(cat <<FIRST <<SECOND - " + prefix + "{{suffix}}\n"
        "7\nFIRST\n2\nSECOND\n) + 1 )); printf '%s' \"$x\""
    )
    bound = boundary_module(namespace, "binding").bind(source, lambda _: "\n\n")
    ownership = boundary_module(namespace, "ownership").establish_ownership(bound)
    assert not boundary_module(namespace, "witness")._mutable_documents(bound, ownership)