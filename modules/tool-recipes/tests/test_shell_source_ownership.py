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
