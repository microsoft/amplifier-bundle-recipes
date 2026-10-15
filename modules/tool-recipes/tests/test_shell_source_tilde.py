import re
import subprocess
from dataclasses import replace

import pytest
from amplifier_module_tool_recipes.shell_source.binding import bind
from amplifier_module_tool_recipes.shell_source.model import (
    FieldProjection,
    ShellSourceError,
    Span,
)
from amplifier_module_tool_recipes.shell_source.tilde import plan_tildes, verify_tildes
from amplifier_module_tool_recipes.shell_source.words import Expansion, cook_island


def field(template, value="", *, role="ARG"):
    bound = bind(template, lambda _: value)
    # These exact fixture atoms have no DATA descendant. The production
    # collector independently owns complete original expansion envelopes.
    atoms = {}
    for match in re.finditer(r"\$\{[A-Za-z0-9_:=~/-]+\}", template):
        start = len(template[: match.start()].encode())
        end = len(template[: match.end()].encode())
        atoms[start] = Expansion(Span(start, end), match.group(), "expansion")
    for match in re.finditer(r"\{[a-z]+(?:,[a-z]+)+\}", template):
        start = len(template[: match.start()].encode())
        end = len(template[: match.end()].encode())
        atoms[start] = Expansion(Span(start, end), match.group(), "brace_expression")
    result = cook_island(bound.events, role, expansion_at=atoms.get)
    return next(item for item in result.pieces if isinstance(item, FieldProjection))


def requests(plan):
    return {(item.request, item.tflag) for item in plan.requests}


def raw_envelopes(item):
    from amplifier_module_tool_recipes.shell_source.model import TextEvent

    raw = "".join(
        event.text for event in item.source_events if isinstance(event, TextEvent)
    ).encode()
    return [
        raw[e.span.start : e.span.end].decode() for e in plan_tildes(item).envelopes
    ]


@pytest.mark.parametrize(
    "value", ["~bob:a/b", "~bob=~joe/b", "~bob:a", "~+/x", "~-/x", "~bob:~joe=~other/x"]
)
def test_complete_ordinary_finder_is_preserved(value):
    item = field("{{p}}", value)
    plan = plan_tildes(item)
    prefix, _, suffix = value.partition("/")
    assert requests(plan) == {(prefix, 0)}
    assert raw_envelopes(item) == [prefix + ("/" if "/" in value else "")]
    verify_tildes(item, prefix + ("/'" + suffix + "'" if "/" in value else ""))


@pytest.mark.parametrize("emitted", ["~bob:'a'/b", "'~bob:a'/b", "~bob:a''/b"])
def test_quote_inside_finder_changes_request_or_removes_call(emitted):
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(field("{{p}}", "~bob:a/b"), emitted)


def test_assignment_argument_pairs_ordinary_declaration_and_posix_profiles():
    item = field("X={{p}}", "~bob=~joe/b")
    plan = plan_tildes(item)
    assert requests(plan) == {("~bob=~joe", 1), ("~bob=~joe", 2)}
    assert {r.profile for r in plan.requests} == {
        "arg:ordinary:default",
        "arg:declaration:default",
        "arg:declaration:posix",
    }
    assert raw_envelopes(item) == ["X=~bob=~joe/"]
    verify_tildes(item, "X=~bob=~joe/'b'")


def test_only_first_equals_activates_main_before_any_backend_request():
    item = field("X={{p}}", "a=~/b")
    assert not plan_tildes(item).requests
    assert raw_envelopes(item) == ["X="]
    verify_tildes(item, "X='a=~/b'")


def test_true_rhs_colon_calls_retain_previous_backend_outcome_history():
    item = field("{{p}}", "~bob:a:~/b", role="ASSIGN")
    plan = plan_tildes(item)
    assert requests(plan) == {("~bob", 2), ("~", 2)}
    later = [r for r in plan.requests if r.request == "~"]
    assert {r.history for r in later} == {
        ("2:~bob:unchanged",),
        ("2:~bob:consumed-quoted",),
    }
    verify_tildes(item, "~bob:'a':~/'b'")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "'~bob:a:~/b'")


@pytest.mark.parametrize(
    "template,emitted,bad",
    [
        ("{{p}}~/x", "~/'x'", "''~/x"),
        ("X={{p}}~/x", "X=~/'x'", "X=''~/x"),
        ("~{{p}}/x", "~/'x'", "~''/x"),
    ],
)
def test_empty_fences_emit_nothing_at_tilde_activation(template, emitted, bad):
    item = field(template)
    assert plan_tildes(item).requests
    verify_tildes(item, emitted)
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, bad)


def test_original_empty_quote_suppresses_word_start_opportunity():
    item = field("''{{p}}", "~/x")
    assert not plan_tildes(item).requests
    verify_tildes(item, "''" + "'~/x'")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "~/x")


def test_quoted_data_tilde_stays_literal():
    item = field('"{{p}}"', "~/b")
    assert not plan_tildes(item).requests
    verify_tildes(item, "'~/b'")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "~/b")


@pytest.mark.parametrize(
    "value", ["~bob:a*", "~bob?", "~bob$SFX", "~bob\\x", "~bob'", "~bébé"]
)
def test_unprotected_data_chain_has_closed_ascii_domain(value):
    with pytest.raises(ShellSourceError, match="TILDE_CHAIN_DOMAIN"):
        plan_tildes(field("{{p}}", value))


@pytest.mark.parametrize(
    "template,value",
    [
        ("foo{{p}}", "=~/x"),
        ("{{p}}", "NAME=~/x"),
        ("N{{p}}AME=~/x", ""),
        ("{{p}}", "NAME+=~/x"),
        ("{{p}}", "NAME[0]=~/x"),
        ("NAME{{p}}=~/x", ""),
    ],
)
def test_assignment_shape_is_derived_before_encoder_can_hide_it(template, value):
    with pytest.raises(ShellSourceError, match="TILDE_ASSIGNMENT_SHAPE"):
        plan_tildes(field(template, value))


def test_forged_compiler_assignment_certificate_is_not_authority():
    item = field("foo{{p}}", "=~/x")
    with pytest.raises(ShellSourceError, match="TILDE_ASSIGNMENT_SHAPE"):
        plan_tildes(replace(item, authored_assignment_prefix="foo="))


def test_right_reader_rederives_assignment_shape():
    item = field("X={{p}}", "value")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "'X=value'")


def test_trusted_expansion_spelling_is_raw_inside_request_and_paired_on_fallback():
    item = field("~{{p}}$SFX/x", "bob")
    assert requests(plan_tildes(item)) == {("~bob$SFX", 0)}
    assert raw_envelopes(item) == ["~bob$SFX/"]
    verify_tildes(item, "~bob$SFX/'x'")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "~bob${SFX}/'x'")


def test_safe_parameter_normalization_outside_finder_is_still_accepted():
    verify_tildes(field("prefix$SFX/{{p}}", "value"), "'prefix'${SFX}'/value'")


def test_consumed_branch_can_reach_colon_inside_raw_expansion():
    item = field("{{p}}${SFX:-~other}/x", "~bob", role="ASSIGN")
    assert ("~bob${SFX", 2) in requests(plan_tildes(item))
    verify_tildes(item, "~bob${SFX:-~other}/'x'")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "~bob${SFX:-'~other'}/'x'")


def test_wholly_authored_aborting_prefix_is_preserved_through_slash():
    item = field("~'bob'/{{p}}", "value")
    assert not plan_tildes(item).requests
    assert raw_envelopes(item) == ["~'bob'/"]
    verify_tildes(item, "~'bob'/'value'")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "'~bob/value'")


def test_raw_offsets_are_utf8_and_not_normalized_expansion_offsets():
    item = field("X=é:~{{p}}$SFX/x", "bob")
    raw = "X=é:~bob$SFX/x".encode()
    envelopes = plan_tildes(item).envelopes
    assert [raw[e.span.start : e.span.end].decode() for e in envelopes] == [
        "X=",
        ":~bob$SFX/",
    ]
    verify_tildes(item, "X='é':~bob$SFX/'x'")


@pytest.mark.parametrize("role", ["UNQUALIFIED"])
def test_role_label_is_not_an_ordinary_argument_caller_certificate(role):
    with pytest.raises(ShellSourceError, match="TILDE_CALLER"):
        plan_tildes(field("{{p}}", "~/x", role=role))


@pytest.mark.parametrize("role", ["FILE", "FOR_VALUES"])
def test_source_qualified_redirect_and_for_callers_have_no_declaration_dispatch(role):
    item = field("{{p}}", "~bob:a/x", role=role)
    assert requests(plan_tildes(item)) == {("~bob:a", 0)}
    verify_tildes(item, "~bob:a/'x'")
    assignment_shaped = field("X={{p}}", "~/x", role=role)
    assert requests(plan_tildes(assignment_shaped)) == {("~", 1)}
    assert {r.profile for r in plan_tildes(assignment_shaped).requests} == {
        role.lower() + ":default"
    }


def test_unqualified_role_cannot_turn_an_originally_quoted_tilde_into_a_call():
    item = field('"{{p}}"', "~/x", role="UNQUALIFIED")
    verify_tildes(item, "'~/x'")
    with pytest.raises(ShellSourceError, match="TILDE_CALLER"):
        verify_tildes(item, "~/x")


def test_conditional_caller_runs_main_without_pre_main_brace_expansion():
    item = field("~{{p}}{one,two}/x", "bob", role="TEST_DATA")
    assert requests(plan_tildes(item)) == {("~bob{one,two}", 0)}
    verify_tildes(item, "~bob{one,two}/'x'")


def test_emitted_request_cannot_hide_a_second_command():
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(field("{{p}}", "~/x"), "~/x; echo changed")


def test_no_tilde_event_defers_ordinary_brace_semantics_to_word_witness():
    item = field("{{p}}/{discovery,repos,reports}", "working")
    assert not plan_tildes(item).requests
    verify_tildes(item, "'working/'{discovery,repos,reports}")


def test_brace_before_main_has_ordered_per_word_tilde_histories():
    item = field("{{p}}/{one,two}", "~bob")
    plan = plan_tildes(item)
    ordinary = [
        r for r in plan.requests if r.profile == "arg:ordinary:default:brace:on"
    ]
    assert [(r.request, r.history) for r in ordinary] == [
        ("~bob", ("after-brace-words:0",)),
        ("~bob", ("after-brace-words:1",)),
    ]
    verify_tildes(item, "~bob/{one,two}")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "~bob/'{one,two}'")


def test_brace_inside_finder_is_preserved_whole_but_requests_use_expanded_words():
    item = field("~{{p}}{one,two}/x", "bob")
    assert requests(plan_tildes(item)) == {
        ("~bobone", 0),
        ("~bobtwo", 0),
        ("~bob{one,two}", 0),
    }
    assert raw_envelopes(item) == ["~bob{one,two}/"]
    verify_tildes(item, "~bob{one,two}/'x'")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "~bob{two,one}/'x'")


def test_brace_cartesian_order_and_start_position_are_not_reset_in_raw_source():
    item = field("{one,two}{{p}}/{red,blue}", "x")
    assert not plan_tildes(item).requests
    verify_tildes(item, "{one,two}'x/'{red,blue}")


def test_true_rhs_has_no_pre_main_brace_expansion():
    item = field("~{{p}}{one,two}/x", "bob", role="ASSIGN")
    assert requests(plan_tildes(item)) == {("~bob{one,two}", 2)}
    verify_tildes(item, "~bob{one,two}/'x'")


def test_changed_brace_words_drop_assignment_and_declaration_flags():
    item = field("X=~{{p}}{one,two}/x", "bob")
    plan = plan_tildes(item)
    assert requests(plan) == {("~bob{one,two}", 1), ("~bob{one,two}", 2)}
    assert all(r.profile.endswith(":brace:off") for r in plan.requests)
    verify_tildes(item, "X=~bob{one,two}/'x'")


@pytest.mark.parametrize(
    "template,emitted,expected_request",
    [
        ("\\\n{{p}}/x", "~bob/'x'", "~bob"),
        ("{{p}}\\\n/x", "~bob/'x'", "~bob"),
        ("{{p}}\\\n$SFX/x", "~bob$SFX/'x'", "~bob$SFX"),
    ],
)
def test_outer_read_time_continuations_are_removed_before_finder(
    template, emitted, expected_request
):
    item = field(template, "~bob")
    assert requests(plan_tildes(item)) == {(expected_request, 0)}
    verify_tildes(item, emitted)


def test_continuation_inside_original_assignment_name_is_not_data_name_creation():
    item = field("NA\\\nME={{p}}", "~/x")
    assert {request for request, _ in requests(plan_tildes(item))} == {"~"}
    verify_tildes(item, "NAME=~/'x'")


def test_trusted_expansion_does_not_freeze_later_posix_gate_at_field_entry():
    item = field("X=${POSIXLY_CORRECT:=1}:{{p}}", "~/x")
    plan = plan_tildes(item)
    ordinary = [r for r in plan.requests if r.profile == "arg:ordinary:default"]
    assert ordinary and all("mode:default" in r.history for r in ordinary)
    verify_tildes(item, "X=${POSIXLY_CORRECT:=1}:~/'x'")
    with pytest.raises(ShellSourceError, match="TILDE_WITNESS"):
        verify_tildes(item, "X=${POSIXLY_CORRECT:=1}:'~/x'")


@pytest.fixture(scope="module")
def qualified_bash():
    # Test qualification only. Product preparation does not invoke Bash, read
    # a version, or probe HOME/accounts. These controls bind the source model
    # to the exact interpreter actually used for the finite behavior check.
    version = subprocess.run(
        ["/bin/bash", "--version"], check=True, capture_output=True, text=True
    ).stdout
    assert version.startswith("GNU bash, version 5.2.21("), version
    return "/bin/bash"


@pytest.mark.parametrize("posix", [False, True])
@pytest.mark.parametrize(
    "template,value",
    [
        ("printf '%s\\0' {{p}}", "~/one"),
        ("printf '%s\\0' {{p}}", "~codex_nonexistent_58af:a=~codex_nonexistent_01/one"),
        ("printf '%s\\0' X={{p}}", "~/one:~/two"),
        ("export X={{p}}; printf '%s\\0' \"$X\"", "~/one:~/two"),
        ("X={{p}}; printf '%s\\0' \"$X\"", "~/one:~/two"),
        (
            "X=~/:; printf '%s\\0' {{p}}",
            "~codex_nonexistent_58af:a=~codex_nonexistent_01/one",
        ),
        ("printf '%s\\0' {{p}}/{one,two}", "~/work"),
        ("printf '%s\\0' ~{{p}}{one,two}/x", "codex_nonexistent_58af"),
        ("set +B; printf '%s\\0' ~{{p}}{one,two}/x", "codex_nonexistent_58af"),
        ("X=~{{p}}{one,two}/x; printf '%s\\0' \"$X\"", "codex_nonexistent_58af"),
        ("export X=~{{p}}{one,two}/x; printf '%s\\0' \"$X\"", "codex_nonexistent_58af"),
        ("for x in {{p}}; do printf '%s\\0' \"$x\"; done", "~/one"),
        ('if [[ {{p}} == "$HOME/one" ]]; then printf yes; else printf no; fi', "~/one"),
        ("SFX=suffix; printf '%s\\0' ~{{p}}$SFX/x", "codex_nonexistent_58af"),
        ("printf '%s\\0' \\\n{{p}}/x", "~"),
        ("printf '%s\\0' {{p}}\\\n/x", "~"),
        (
            "unset POSIXLY_CORRECT; printf '%s\\0' X=${POSIXLY_CORRECT:=1}:{{p}}",
            "~/one",
        ),
    ],
)
def test_finite_real_bash_caller_and_state_histories(
    qualified_bash, tmp_path, template, value, posix
):
    from amplifier_module_tool_recipes.shell_source.compiler import compile_bound

    home = tmp_path / "private-home"
    home.mkdir()
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    emitted = compile_bound(bind(template, lambda _: value)).source
    raw = template.replace("{{p}}", value)
    command = (
        [qualified_bash, "--noprofile", "--norc"]
        + (["--posix"] if posix else [])
        + ["-c"]
    )
    reference = subprocess.run(
        command + [raw],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=5,
        check=False,
    )
    actual = subprocess.run(
        command + [emitted],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert (actual.returncode, actual.stdout, actual.stderr) == (
        reference.returncode,
        reference.stdout,
        reference.stderr,
    )
    assert actual.returncode == 0


@pytest.mark.parametrize("posix", [False, True])
def test_finite_real_bash_redirect_preserves_private_file_effect(
    qualified_bash, tmp_path, posix
):
    from amplifier_module_tool_recipes.shell_source.compiler import compile_bound

    home = tmp_path / "private-home"
    home.mkdir()
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    template, value = "printf 'owned tilde redirect' >{{p}}", "~/receipt"
    emitted = compile_bound(bind(template, lambda _: value)).source
    command = (
        [qualified_bash, "--noprofile", "--norc"]
        + (["--posix"] if posix else [])
        + ["-c"]
    )
    reference = subprocess.run(
        command + [template.replace("{{p}}", value)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=5,
        check=False,
    )
    expected = (home / "receipt").read_bytes()
    (home / "receipt").unlink()
    actual = subprocess.run(
        command + [emitted],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert (actual.returncode, actual.stdout, actual.stderr) == (
        reference.returncode,
        reference.stdout,
        reference.stderr,
    )
    assert actual.returncode == 0
    assert (home / "receipt").read_bytes() == expected == b"owned tilde redirect"
