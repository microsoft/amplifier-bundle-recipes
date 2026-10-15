import pytest
from amplifier_module_tool_recipes.shell_source.binding import bind, events_in
from amplifier_module_tool_recipes.shell_source.model import (
    FieldProjection,
    ShellSourceError,
)
from amplifier_module_tool_recipes.shell_source.ownership import establish_ownership
from amplifier_module_tool_recipes.shell_source.words import cook_island


def cooked(source, value):
    bound = bind(source, lambda _: value)
    ownership = establish_ownership(bound)
    owner = ownership.words[0]
    return cook_island(events_in(bound, owner.span), owner.role)


def fields(result):
    return [
        "".join(p.text for p in f.parts)
        for f in result.pieces
        if isinstance(f, FieldProjection)
    ]


@pytest.mark.parametrize(
    "value,expected",
    [("a  b", ["a", "b"]), ("a\tb", ["a", "b"]), ("", []), (" \t", [])],
)
def test_unquoted_data_has_real_field_cardinality(value, expected):
    assert fields(cooked("echo {{x}}", value)) == expected


def test_empty_occurrence_exposes_only_same_argument_island_separator():
    result = cooked("printf '%s|' pre\\{{x}} post", "")
    assert fields(result) == ["%s|", "pre post"]
    assert len(result.fences) == 2


def test_quote_and_escape_state_is_not_owned_by_data():
    value = "'\"$(touch NEVER)`touch NEVER`\\"
    result = cooked("printf '%s' {{x}}", value)
    assert fields(result) == ["%s", "'\"$(touch", "NEVER)`touch", "NEVER`\\"]
    assert not any(
        p.kind == "expansion"
        for f in result.pieces
        if isinstance(f, FieldProjection)
        for p in f.parts
    )


def test_authored_quotes_force_one_empty_field_and_prevent_data_splitting():
    assert fields(cooked('echo "{{x}}"', "")) == [""]
    assert fields(cooked("echo '{{x}}'", "a  b")) == ["a  b"]


def test_trusted_escape_consumes_data_lf_before_field_partition():
    assert fields(cooked("echo a\\{{x}}b", "\nz")) == ["azb"]


def test_empty_occurrence_cannot_expose_structural_newline():
    with pytest.raises(ShellSourceError, match="OWNER_CONFLICT"):
        cooked("echo a\\{{x}}\necho other", "")


def test_empty_fence_closes_trusted_parameter_name_without_new_lookup():
    assert fields(cooked('echo "$HOME{{x}}suffix"', "")) == ["${HOME}suffix"]
    result = cooked('echo "$HOME{{x}}suffix"', "")
    expansion = [
        p
        for f in result.pieces
        if isinstance(f, FieldProjection)
        for p in f.parts
        if p.kind == "expansion"
    ]
    assert [p.text for p in expansion] == ["${HOME}"]


def test_empty_fence_cannot_complete_dollar_parameter_syntax():
    result = cooked('echo "${{x}}HOME"', "")
    assert fields(result) == ["$HOME"]
    assert not any(
        p.kind == "expansion"
        for f in result.pieces
        if isinstance(f, FieldProjection)
        for p in f.parts
    )


def test_raw_field_events_preserve_quotes_escape_and_original_parameter_spelling():
    result = cooked('echo ""~bob$SFX/{{x}} next', "data")
    field = next(f for f in result.pieces if isinstance(f, FieldProjection))
    from amplifier_module_tool_recipes.shell_source.model import TextEvent

    assert (
        "".join(e.text for e in field.source_events if isinstance(e, TextEvent))
        == '""~bob$SFX/data'
    )
    assert "${SFX}" in "".join(p.text for p in field.parts)
