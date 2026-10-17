"""Actual emitted-source structure, including corrupted compiler outputs.

These tests deliberately construct replacement records independently of the
encoder. Passing this witness alone does not certify field value semantics.
"""

import subprocess
from dataclasses import replace

import pytest
from amplifier_module_tool_recipes.shell_source.binding import bind
from amplifier_module_tool_recipes.shell_source.model import (
    Replacement,
    ShellSourceError,
    Span,
)
from amplifier_module_tool_recipes.shell_source.ownership import establish_ownership
from amplifier_module_tool_recipes.shell_source.witness import verify_program


def fixture(source, values=None):
    bound = bind(source, lambda name: (values or {}).get(name, "data"))
    return bound, establish_ownership(bound)


def emitted(bound, replacements):
    source = bound.source.encode()
    parts = []
    position = 0
    for item in replacements:
        parts.extend((source[position : item.source.start], item.text.encode()))
        position = item.source.end
    return b"".join(parts + [source[position:]]).decode()


def word_case(source, text, *, kind="word"):
    bound, ownership = fixture(source)
    owner = ownership.words[0]
    edits = (Replacement(owner.span, text, kind, owner.role),)
    return bound, ownership, edits, emitted(bound, edits)


@pytest.mark.parametrize(
    "text",
    [
        "value",
        "one two",
        "'space and tab\t'",
        "",
        " \t ",
        "'é雪'",
        r"'$(touch marker); `id` >file'",
        r"'{{never.rescan}}'",
        "'line\nline'",
        r"\$HOME",
    ],
)
def test_argument_data_can_change_literal_representation_and_field_count(text):
    verify_program(*word_case("echo {{x}}", text))


@pytest.mark.parametrize(
    "text",
    [
        "x; touch marker",
        "x && echo other",
        "x | echo other",
        "x\necho other",
        "$(touch marker)",
        "`touch marker`",
        "x >out",
        "x #comment",
        "x &",
        "x;",
        "${evil}",
        "<(touch marker)",
    ],
)
def test_corrupted_argument_output_cannot_introduce_executable_structure(text):
    with pytest.raises(ShellSourceError):
        verify_program(*word_case("echo {{x}}", text))


def test_empty_argument_does_not_allow_its_command_to_disappear():
    bound, ownership, edits, candidate = word_case("echo {{x}}", "")
    with pytest.raises(ShellSourceError, match="reconstruction"):
        verify_program(bound, ownership, edits, "")
    verify_program(bound, ownership, edits, candidate)


@pytest.mark.parametrize(
    "source,text",
    [
        ('echo "{{x}}"', "'different literal representation'"),
        ("value={{x}} echo okay", "''"),
        ("value={{x}} echo okay", "'one two'"),
        ("export NAME={{x}}", "NAME='one two'"),
        ("cat >{{x}}", "'space in filename'"),
        ('for item in {{x}}; do echo "$item"; done', "one two"),
        ('for item in {{x}}; do echo "$item"; done', ""),
        ("[[ {{x}} = expected ]]", "'literal'"),
        ("echo ${HOME}{{x}}", "${HOME}'literal'"),
        ("echo $HOME{{x}}", "${HOME}'literal'"),
        ("echo $((1+2)){{x}}", "$((1+2))'literal'"),
    ],
)
def test_original_ordinary_roles_and_trusted_expansions_remain(source, text):
    verify_program(*word_case(source, text))


@pytest.mark.parametrize("text", ["~/'one'", "~root/'one'", "~~/'one'"])
def test_contiguous_tilde_prefix_is_part_of_conditional_operand(text):
    verify_program(*word_case('[[ {{x}} == "$HOME/one" ]]', text))


@pytest.mark.parametrize(
    "text", ["~ /'one'", "! /'one'", "~$(printf BAD)", "~'one' || true"]
)
def test_conditional_tilde_spelling_cannot_hide_unowned_syntax(text):
    with pytest.raises(ShellSourceError):
        verify_program(*word_case('[[ {{x}} == "$HOME/one" ]]', text))


@pytest.mark.parametrize(
    "source,text",
    [
        ("export NAME={{x}}", "EVIL='one two'"),
        ("value={{x}} echo okay", "x other=bad"),
        ("cat >{{x}}", "file other"),
        ("cat >{{x}}", ""),
        ("[[ {{x}} = expected ]]", "x -o other"),
        ("echo ${HOME}{{x}}", "'literal'"),
        ("echo $HOME{{x}}", "${EVIL}'literal'"),
        ("echo $((1+2)){{x}}", "$((1*2))'literal'"),
    ],
)
def test_corrupted_roles_names_or_trusted_expansions_are_rejected(source, text):
    with pytest.raises(ShellSourceError):
        verify_program(*word_case(source, text))


def test_composed_outer_word_does_not_hide_its_nested_command():
    source = "echo \"{{x}}$(printf '%s' '{{y}}')\""
    valid = "\"data$(printf '%s' 'other')\""
    verify_program(*word_case(source, valid))
    for corrupt in (
        "\"data$(touch '%s' 'other')\"",
        "\"data$(printf '%s' 'other'; touch marker)\"",
        '"data"',
    ):
        with pytest.raises(ShellSourceError):
            verify_program(*word_case(source, corrupt))


def test_trusted_nested_commands_can_remain_in_separate_result_fields():
    verify_program(
        *word_case(
            "echo $(printf one){{x}}$(printf two)", "$(printf one) $(printf two)"
        )
    )


@pytest.mark.parametrize(
    "source,text",
    [
        ("echo $((1+{{x}}))", "23"),
        ("echo $((1+{{x}}))", "-23"),
        ("[[ {{x}} -eq 3 ]]", "23"),
    ],
)
def test_numeric_owner_keeps_closed_decimal_node(source, text):
    verify_program(*word_case(source, text, kind="number"))


@pytest.mark.parametrize("text", ["x", "1+2", "2;id", "2\n", "1.0"])
def test_numeric_certificate_cannot_hide_arbitrary_source(text):
    with pytest.raises(ShellSourceError):
        verify_program(*word_case("echo $((1+{{x}}))", text, kind="number"))


def test_comment_has_its_own_physical_boundary():
    verify_program(*word_case("# {{x}}\necho okay", "# data", kind="comment"))
    with pytest.raises(ShellSourceError):
        verify_program(
            *word_case("# {{x}}\necho okay", "# data\ntouch marker", kind="comment")
        )


def document_case(source, bodies, delimiter=None):
    bound, ownership = fixture(source)
    edits = []
    for document, body in zip(ownership.documents.documents, bodies):
        edits.append(Replacement(document.body, body, "document-body", "DOCUMENT"))
        if delimiter is not None:
            edits.extend(
                Replacement(span, delimiter, "document-delimiter", "DOCUMENT")
                for span in (document.opener_delimiter, document.closer_delimiter)
            )
    edits = tuple(sorted(edits, key=lambda edit: edit.source.start))
    return bound, ownership, edits, emitted(bound, edits)


@pytest.mark.parametrize(
    "source,bodies,delimiter",
    [
        ("cat <<END\n{{x}}\nEND\n", ["literal\n"], None),
        ("cat <<'END'\n{{x}}\nEND\n", ["$(literal)\n"], None),
        ("cat <<END\n{{x}}\nEND\n", ["END\n"], "__AMP_HDOC_1__"),
        ("cat <<A <<B\n{{x}}\nA\n{{y}}\nB\n", ["one\ntwo\n", "three\n"], None),
        ("cat <<END\nEND\n", ["inserted\n"], None),
        ('echo "$(cat <<END\n{{x}}\nEND\n)"', ["literal\n"], None),
    ],
)
def test_actual_document_headers_fifo_frames_and_closers(source, bodies, delimiter):
    verify_program(*document_case(source, bodies, delimiter))


@pytest.mark.parametrize(
    "body", ["END\ntouch marker\n", "E\\\nND\n", "literal\\\n", "no final LF"]
)
def test_actual_document_cannot_end_early_or_consume_closing_boundary(body):
    with pytest.raises(ShellSourceError):
        verify_program(*document_case("cat <<END\n{{x}}\nEND\n", [body]))


def test_delimiter_edit_cannot_change_quoted_mode():
    with pytest.raises(ShellSourceError):
        verify_program(
            *document_case("cat <<END\n{{x}}\nEND\n", ["value\n"], "'OTHER'")
        )


def test_only_matching_original_word_owners_can_authorize_edits():
    bound, ownership, edits, candidate = word_case("echo {{x}}", "okay")
    for corrupt in (
        replace(edits[0], role="FILE"),
        replace(edits[0], kind="number"),
        replace(edits[0], source=Span(0, 4), text="touch"),
    ):
        with pytest.raises(ShellSourceError):
            verify_program(bound, ownership, (corrupt,), emitted(bound, (corrupt,)))
    with pytest.raises(ShellSourceError, match="reconstruction"):
        verify_program(bound, ownership, edits, candidate + "; id")


def test_missing_duplicate_and_overlapping_occurrence_owners_are_rejected():
    bound, ownership, edits, candidate = word_case("echo {{x}}", "okay")
    with pytest.raises(ShellSourceError, match="no final edit owner"):
        verify_program(bound, ownership, (), bound.source)
    with pytest.raises(ShellSourceError, match="overlap"):
        verify_program(bound, ownership, edits + edits, candidate)


def test_actual_output_is_not_bound_again_even_when_data_looks_like_a_hole():
    verify_program(*word_case('echo "{{x}}"', "'{{other}}'"))


def test_closed_original_recovery_markers_remain_exact_executable_bytes():
    source = "{% if NAME != '' %}\necho \"{{x}}\"\n{% endif %}\n"
    verify_program(*word_case(source, '"literal\\$value\\"quote"'))
    with pytest.raises(ShellSourceError):
        verify_program(*word_case(source, '"literal"; touch marker'))


def test_recovery_never_masks_a_new_marker_or_unknown_error():
    source = "{% if NAME != '' %}\necho \"{{x}}\"\n{% endif %}\n"
    with pytest.raises(ShellSourceError):
        verify_program(*word_case(source, "\"literal\"\n{% if EVIL != '' %}"))


def test_utf8_reconstruction_and_boundaries_use_bytes():
    verify_program(*word_case("echo '雪' {{x}}", "'雪' 'évalue'"))
    bound, ownership, edits, _ = word_case("echo '雪' {{x}}", "'雪' 'évalue'")
    corrupt = replace(
        edits[0], source=Span(edits[0].source.start + 2, edits[0].source.end)
    )
    with pytest.raises(ShellSourceError):
        verify_program(bound, ownership, (corrupt,), "anything")


@pytest.mark.parametrize(
    "source",
    [
        "for item in {{x}}; do echo unexpected; done; echo final",
        "for item in {{x}}\ndo echo unexpected; done\necho final",
        "if true; then for item in {{x}}; do echo unexpected; done; fi; echo final",
    ],
)
def test_empty_for_projection_preserves_actual_zero_iteration_semantics(source):
    case = word_case(source, "")
    verify_program(*case)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", case[3]],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "final\n"


@pytest.mark.parametrize(
    "source,text",
    [
        ('echo "$(printf {{x}})"', "'data'"),
        ('echo "$(printf {{x}})unchanged"', "'data'"),
        ("echo $(printf {{x}})", "'data'"),
        ("echo $HOME{{x}}suffix", "${HOME}'datasuffix'"),
    ],
)
def test_edits_nested_within_unchanged_words_keep_actual_boundary(source, text):
    verify_program(*word_case(source, text))


@pytest.mark.parametrize(
    "source,text",
    [
        ('echo "$(printf {{x}})"', "'data')\"; touch marker; echo \"$(printf '"),
        ("echo $HOME{{x}}suffix", "${HOME}$(touch marker)'suffix'"),
    ],
)
def test_nested_edit_cannot_escape_its_actual_word_frame(source, text):
    with pytest.raises(ShellSourceError):
        verify_program(*word_case(source, text))


@pytest.mark.parametrize(
    "source,text,value,count",
    [
        ("echo {{x}}", "data\n", "data\n", 1),
        ("echo {{x}}\necho next", "data\n\n", "data\n\n", 2),
        ("cat >{{x}}", "target\n", "target\n", 1),
        ("echo {{x}}", "\n", "\n", 1),
    ],
)
def test_only_qualified_final_data_lfs_can_be_empty_command_terminators(
    source, text, value, count
):
    bound, ownership = fixture(source, {"x": value})
    owner = ownership.words[0]
    edits = (Replacement(owner.span, text, "word", owner.role, tail_lfs=count),)
    verify_program(bound, ownership, edits, emitted(bound, edits))


@pytest.mark.parametrize(
    "source,text,value,count",
    [
        ("echo {{x}}", "data\n", "data", 1),
        ("echo {{x}}", "data\n\n", "data\n", 2),
        ("echo {{x}}; echo next", "data\n", "data\n", 1),
        ('echo "{{x}}"', "data\n", "data\n", 1),
        ("echo {{x}}", "data\n", "data\n", 0),
        ("echo {{x}}", "data\n;touch marker", "data\n", 1),
        ("echo {{x}}", "data\n", "data\n", -1),
        ("echo {{x}}", "data\n", "data\n", True),
        ("cat <<END {{x}}\nbody\nEND\n", "data\n", "data\n", 1),
    ],
)
def test_forged_tail_certificate_cannot_change_authored_boundaries(
    source, text, value, count
):
    bound, ownership = fixture(source, {"x": value})
    owner = ownership.words[0]
    edits = (Replacement(owner.span, text, "word", owner.role, tail_lfs=count),)
    with pytest.raises(ShellSourceError):
        verify_program(bound, ownership, edits, emitted(bound, edits))


@pytest.mark.parametrize(
    "source,text",
    [
        ("echo $((1-{{x}}))", "(-5)"),
        ("echo $((1+{{x}}))", "(+3)"),
        ("echo $((1*{{x}}))", "(-0)"),
        ("[[ {{x}} -eq 3 ]]", "+3"),
    ],
)
def test_signed_decimal_is_one_complete_arithmetic_rvalue(source, text):
    verify_program(*word_case(source, text, kind="number"))


@pytest.mark.parametrize(
    "text",
    [
        "(1+2)",
        "(+3);touch marker",
        "(-x)",
        "(-2+3)",
        "((+3))",
        "(+03)",
        "03",
        "0x10",
        "(-3",
        "+3)",
    ],
)
def test_signed_numeric_wrapper_does_not_mask_other_expression_syntax(text):
    with pytest.raises(ShellSourceError):
        verify_program(*word_case("echo $((1+{{x}}))", text, kind="number"))


@pytest.mark.parametrize(
    "source",
    [
        '[ -f "{{x}}" ]',
        '[ ! -f "{{x}}" ]',
        '[[ -n "{{x}}" ]]',
        'if [ -z "{{x}}" ]; then echo empty; else echo present; fi',
    ],
)
def test_unary_test_unnamed_operand_retains_its_actual_operator(source):
    verify_program(*word_case(source, "'path with spaces'"))
    for corrupt in ("x -o y", "$(touch marker)"):
        with pytest.raises(ShellSourceError):
            verify_program(*word_case(source, corrupt))
