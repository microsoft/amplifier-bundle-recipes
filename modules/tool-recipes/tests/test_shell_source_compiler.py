import subprocess

import pytest

from amplifier_module_tool_recipes.shell_source import prepare
from amplifier_module_tool_recipes.shell_source.model import ShellSourceError


def lower(source, **values):
    return prepare(source, values.__getitem__)


def bash(source, cwd=None):
    return subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", source], cwd=cwd, capture_output=True, check=True).stdout


@pytest.mark.parametrize("value", ["ordinary", "'\"$(printf ATTACK)`printf ATTACK`\\", "a\nb\r", "", "λ 空"])
def test_complete_quoted_owner_preserves_actual_literal_output(value):
    assert bash(lower('printf \'%s\' "{{x}}"', x=value)) == value.encode()


@pytest.mark.parametrize("value,expected", [("a  b", b"a|b|"), ("a\tb", b"a|b|"), ("", b"|"), ("one\n\n", b"one|")])
def test_complete_unquoted_fields_and_empty_tail_terminators(value, expected):
    assert bash(lower("printf '%s|' {{x}}", x=value)) == expected


def test_nested_command_frame_keeps_inner_data_inside_inner_argument():
    source = 'printf \'%s\' "$(printf \'%s\' \'{{inner}}\') {{outer}}"'
    assert bash(lower(source, inner="inner'$(printf BAD)", outer="outer$(printf BAD)")) == b"inner'$(printf BAD) outer$(printf BAD)"


def test_assignment_and_redirect_keep_their_original_scalar_roles(tmp_path):
    source = 'value="{{x}}"; printf \'%s\' "$value" >"{{path}}"'
    rendered = lower(source, x="literal$(printf BAD)", path="result file.txt")
    assert bash(rendered, cwd=tmp_path) == b""
    assert (tmp_path / "result file.txt").read_text() == "literal$(printf BAD)"


@pytest.mark.parametrize("value", ["a  ", "\t ", ""])
def test_assignment_dead_suffix_whitespace_does_not_become_another_command(value):
    assert bash(lower('value={{x}}; printf \'%s\' "$value"', x=value)) == value.rstrip(" \t").encode()


@pytest.mark.parametrize("source,value", [("value={{x}}", "a b"), ("cat >{{x}}", "a b"), ("echo {{x}} suffix", "a\n"), ("echo {{x}}", "a\nb")])
def test_unowned_whitespace_or_internal_line_placement_refuses_before_launch(source, value):
    with pytest.raises(ShellSourceError):
        lower(source, x=value)


def test_heredoc_fifo_and_delimiter_rotation_are_actual_bash_owned():
    source = "cat <<FIRST <<'SECOND'\n{{one}}\nFIRST\n{{two}}\nSECOND\n"
    assert bash(lower(source, one="FIRST\n$(printf BAD)", two="SECOND\n$(printf BAD)")) == b"SECOND\n$(printf BAD)\n"


def test_pending_document_tail_lf_handoff_preserves_exact_body_bytes():
    source = "cat <<EOF {{suffix}}\nline\nEOF\n"
    assert bash(lower(source, suffix="-\n\n")) == b"\n\nline\n"


def test_for_values_and_test_operands_preserve_structure():
    assert bash(lower("for item in {{x}}; do printf '%s|' \"$item\"; done", x="a b")) == b"a|b|"
    assert bash(lower('if [ "{{x}}" = ok ]; then printf yes; else printf no; fi', x="ok")) == b"yes"


@pytest.mark.parametrize("value,expected", [("1", b"2"), ("-5", b"8"), ("+3", b"0")])
def test_numeric_complete_rvalue_retains_decimal_semantics(value, expected):
    assert bash(lower('printf \'%s\' "$((3-{{x}}))"', x=value)) == expected


@pytest.mark.parametrize("value", ["01", "1+2", "array[0]", " 2", "٢"])
def test_numeric_domain_is_not_an_arithmetic_source_converter(value):
    with pytest.raises(ShellSourceError, match="NUMERIC_DOMAIN"):
        lower('printf \'%s\' "$((3-{{x}}))"', x=value)


def test_comment_data_cannot_add_a_statement_and_zero_holes_are_identity():
    source = "# {{x}}\nprintf safe"
    assert bash(lower(source, x="comment\nprintf BAD")) == b"safe"
    unchanged = "printf '%s' '$HOME'\n"
    assert prepare(unchanged, lambda _: pytest.fail("converter called without occurrence")) == unchanged
