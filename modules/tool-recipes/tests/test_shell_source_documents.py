import pytest
from tree_sitter import Language, Parser
import tree_sitter_bash

from amplifier_module_tool_recipes.shell_source.binding import bind
from amplifier_module_tool_recipes.shell_source.documents import collect_documents
from amplifier_module_tool_recipes.shell_source.model import ShellSourceError, TextEvent


def collect(source, value="literal"):
    bound = bind(source, lambda _: value)
    return bound, collect_documents(bound)


def test_fifo_documents_have_exact_original_owners_and_parseable_skeleton():
    source = "cat <<EOF <<'SECOND'\n{{one}}\nEOF\n{{two}}\nSECOND\necho finished\n"
    bound, result = collect(source, "EOF\nSECOND\n$(not executed)")
    assert len(result.documents) == 2
    first, second = result.documents
    assert (first.delimiter, first.quoted, second.delimiter, second.quoted) == ("EOF", False, "SECOND", True)
    assert first.body.end < second.body.start
    assert first.frame == second.frame == 0
    assert first.terminal_lf_source.end == first.body.end
    assert second.terminal_lf_source.end == second.body.end
    assert [e.occurrence for e in first.events if isinstance(e, TextEvent) and e.kind == "data"] == [0] * len(bound.occurrences[0].value)
    assert len(result.skeleton) == len(source.encode())
    parser = Parser(Language(tree_sitter_bash.language()))
    assert not parser.parse(result.skeleton).root_node.has_error


def test_comment_and_quoted_fake_openers_do_not_claim_a_document():
    _, result = collect("# <<FAKE {{x}}\nprintf '%s' '<<ALSO_FAKE' \"{{x}}\"\n")
    assert result.documents == ()
    assert result.contexts[0].mode == "comment"


def test_nested_command_substitution_resets_quote_context_and_owns_its_document():
    source = 'echo "outer $(cat <<EOF\n{{x}}\nEOF\n) suffix {{y}}"'
    bound, result = collect(source)
    assert len(result.documents) == 1
    assert result.documents[0].frame != 0
    assert result.contexts[bound.occurrences[0].span.start].mode == "heredoc"
    assert result.contexts[bound.occurrences[1].span.start].mode == "double"


def test_tab_stripping_accepts_only_leading_tabs_at_exact_closer():
    _, result = collect("cat <<-EOF\n\t{{x}}\n\tEOF\n")
    assert result.documents[0].strip_tabs
    assert result.documents[0].closer_delimiter.start == len("cat <<-EOF\n\t{{x}}\n\t".encode())
    with pytest.raises(ShellSourceError, match="UNCLOSED_DOCUMENT"):
        collect("cat <<EOF\n{{x}}\nEOF \n")


def test_escaped_fake_opener_and_arithmetic_shift_are_not_documents():
    _, result = collect(r"echo \<\<EOF {{x}}; n=$((1 << 2))")
    assert not result.documents


def test_each_nested_frame_has_its_own_pending_fifo():
    source = "cat <<OUTER $(cat <<INNER\n{{x}}\nINNER\n)\n{{y}}\nOUTER\n"
    _, result = collect(source)
    assert [d.delimiter for d in result.documents] == ["OUTER", "INNER"]
    assert result.documents[0].frame != result.documents[1].frame
    parser = Parser(Language(tree_sitter_bash.language()))
    assert not parser.parse(result.skeleton).root_node.has_error
