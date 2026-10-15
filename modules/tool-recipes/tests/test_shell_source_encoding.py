import subprocess

import pytest

from amplifier_module_tool_recipes.shell_source.binding import bind, events_in
from amplifier_module_tool_recipes.shell_source.encoding import encode_field
from amplifier_module_tool_recipes.shell_source.model import FieldProjection, ShellSourceError
from amplifier_module_tool_recipes.shell_source.ownership import establish_ownership
from amplifier_module_tool_recipes.shell_source.word_witness import verify_field
from amplifier_module_tool_recipes.shell_source.words import cook_island


def encoded(source, value):
    bound = bind(source, lambda _: value)
    owner = establish_ownership(bound).words[0]
    fields = [f for f in cook_island(events_in(bound, owner.span), owner.role).pieces if isinstance(f, FieldProjection)]
    result = []
    for field in fields:
        text = encode_field(field)
        verify_field(field, text)
        result.append((field, text))
    return result


@pytest.mark.parametrize("value", ["ordinary", "'", '"', "$(printf INJECTION)", "`printf INJECTION`", ";printf INJECTION", "\\", "a\nb\r", "λ 空", "", "{{unresolved}}"])
def test_quoted_data_roundtrips_as_literal_in_actual_bash(value):
    [(field, text)] = encoded('echo "{{x}}"', value)
    actual = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", "printf '%s' " + text], check=True, capture_output=True)
    assert actual.stdout == value.encode()


@pytest.mark.parametrize("value", ["a  b", "a\tb", "", " \t", "one two three"])
def test_unquoted_fields_preserve_actual_argument_count(value):
    result = encoded("echo {{x}}", value)
    command = "set -- " + " ".join(text for _, text in result) + "; printf '%s\\n' \"$#\"; printf '<%s>' \"$@\""
    actual = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", command], check=True, capture_output=True)
    count, _ = actual.stdout.split(b"\n", 1)
    assert int(count) == len(value.split())


def test_actual_globs_use_same_bash_directory_without_python_expansion(tmp_path):
    for name in ("a.txt", "b.txt", "other.log"):
        (tmp_path / name).write_text(name)
    for pattern in ("*.txt", "?.txt", "[a-b].txt"):
        [(field, text)] = encoded("echo {{x}}", pattern)
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", "printf '%s\\n' " + text], cwd=tmp_path, capture_output=True, check=True)
        assert result.stdout == b"a.txt\nb.txt\n"


def test_quoted_pattern_stays_literal_and_unsafe_pattern_domain_is_named():
    [(field, text)] = encoded('echo "{{x}}"', "[a-z]*")
    verify_field(field, text)
    with pytest.raises(ShellSourceError, match="GLOB_DOMAIN"):
        encoded("echo {{x}}", "[[:alpha:]]")


def test_independent_reader_rejects_injected_syntax_collapsed_literal_and_pattern_mask():
    [(field, text)] = encoded('echo "{{x}}"', "literal*")
    for corrupt in ("literal*", "'different'", "'literal*'; printf EXTRA", "'literal*' other", "'literal*' #hidden"):
        with pytest.raises(ShellSourceError, match="WORD_WITNESS"):
            verify_field(field, corrupt)
