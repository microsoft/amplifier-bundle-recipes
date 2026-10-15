import pytest

from amplifier_module_tool_recipes.shell_source.binding import bind
from amplifier_module_tool_recipes.shell_source.model import ShellSourceError
from amplifier_module_tool_recipes.shell_source.ownership import establish_ownership


def test_command_name_owns_descendant_occurrences_before_inner_arguments():
    result = establish_ownership(bind('$(printf "%s" {{x}}) trusted', lambda _: "echo"))
    assert [owner.role for owner in result.words] == ["NAME"]


@pytest.mark.parametrize("source", ["echo ${prefix{{x}}}", "echo ${v:-{{x}}}", "echo <(printf {{x}})", "cat <<<{{x}}"])
def test_hole_cannot_complete_a_trusted_source_envelope(source):
    with pytest.raises(ShellSourceError, match="(?:SOURCE_ENVELOPE|PARAMETER_SYNTAX)"):
        establish_ownership(bind(source, lambda _: "literal"))


def test_whole_root_closed_marker_language_recovers_only_classification():
    source = 'echo "{{heading}}"\n{% if notes != \'\' %}\ncat "{{notes}}"\n{% endif %}\n'
    bound = bind(source, lambda _: "quoted data")
    result = establish_ownership(bound)
    assert len(result.recovery_markers) == 2
    assert [owner.role for owner in result.words] == ["ARG", "ARG"]
    assert bound.source == source


@pytest.mark.parametrize("extra", ['echo "$HOME"', 'echo "escaped\\word"', "echo unquoted", "cat file >other", "# comment", "echo \"first\nsecond\""])
def test_marker_recovery_rejects_any_unadmitted_line_in_entire_root(extra):
    source = 'echo "{{x}}"\n{% if notes != \'\' %}\n' + extra + '\n{% endif %}\n'
    with pytest.raises(ShellSourceError, match="INVALID_TEMPLATE_SYNTAX"):
        establish_ownership(bind(source, lambda _: "value"))


@pytest.mark.parametrize("prefix", ['echo', '"$cmd"', '"${AMPLIFIER_PYTHON:-python3}"', '${maybe_empty} -I', '"$(printf echo)"'])
def test_hole_free_nonvanishing_prefix_is_certified_without_runtime_lookup(prefix):
    assert establish_ownership(bind(prefix + " {{x}}", lambda _: "payload")).words


@pytest.mark.parametrize("prefix", ['${maybe_empty}', 'prefix$runtime', '"$@"', '"${@:1:0}"', 'no_match_*', '~missing'])
def test_runtime_name_cannot_vanish_and_promote_data_to_executable_name(prefix):
    with pytest.raises(ShellSourceError, match="NONVANISHING_PREFIX"):
        establish_ownership(bind(prefix + " {{x}} -I", lambda _: "payload"))
