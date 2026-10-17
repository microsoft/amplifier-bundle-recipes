"""Independent semantic and whole-program negative controls for =~ owners."""

from dataclasses import replace

import pytest
from amplifier_module_tool_recipes.shell_source import prepare
from amplifier_module_tool_recipes.shell_source.binding import bind
from amplifier_module_tool_recipes.shell_source.compiler import compile_bound
from amplifier_module_tool_recipes.shell_source.model import ShellSourceError, Span
from amplifier_module_tool_recipes.shell_source.model import FenceEvent
from amplifier_module_tool_recipes.shell_source.ownership import establish_ownership
from amplifier_module_tool_recipes.shell_source.witness import verify_program
from amplifier_module_tool_recipes.shell_source.word_witness import verify_field


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