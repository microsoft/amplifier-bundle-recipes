"""Actual STRING converters and both engine preparation seams, without models."""

import subprocess

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.executor import (
    _substitute_shell_variables as legacy_prepare,
)
from amplifier_module_tool_recipes.shell_source.model import ShellSourceError

from amplifier_recipe_runner.engine import _substitute_shell_variables as native_prepare
from amplifier_recipe_runner.engine import substitute_variables as native_string


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        3,
        -5,
        {"message": "literal$(printf BAD)"},
        ["a", "λ"],
        "one\ntwo",
        "{{later}}",
        "'\"\\`$()",
    ],
)
@pytest.mark.parametrize("native", [False, True])
def test_real_owning_string_converter_reaches_actual_bash_as_literal(value, native):
    context = {"payload": {"value": value}}
    executor = object.__new__(RecipeExecutor)
    singleton = "{{payload.value}}"
    if native:
        expected = native_string(singleton, context)
        candidate = native_prepare("printf '%s' \"{{payload.value}}\"", context)
    else:
        expected = executor.substitute_variables(singleton, context)
        candidate = legacy_prepare(
            "printf '%s' \"{{payload.value}}\"",
            lambda name: executor.substitute_variables("{{" + name + "}}", context),
        )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", candidate],
        capture_output=True,
        check=True,
    )
    assert result.stdout == expected.encode()


@pytest.mark.parametrize("native", [False, True])
def test_original_converter_owns_undefined_reference_error(native):
    context = {"known": "value"}
    executor = object.__new__(RecipeExecutor)
    converter = native_string if native else executor.substitute_variables
    with pytest.raises(ValueError) as original:
        converter("{{missing}}", context)
    with pytest.raises(ValueError) as prepared:
        if native:
            native_prepare("printf '%s' \"{{missing}}\"", context)
        else:
            legacy_prepare(
                "printf '%s' \"{{missing}}\"",
                lambda name: converter("{{" + name + "}}", context),
            )
    assert type(prepared.value) is type(original.value)
    assert str(prepared.value) == str(original.value)


def prepare_with_converter(source, context, native):
    if native:
        return native_prepare(source, context)
    converter = object.__new__(RecipeExecutor).substitute_variables
    return legacy_prepare(source, lambda name: converter("{{" + name + "}}", context))


def run_bash(source, cwd=None):
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", source],
        cwd=cwd, capture_output=True, check=False,
    )
    return result.stdout, result.stderr, result.returncode


# CLOSED benign matrix: only these explicitly benign raw references execute.
# A match records the complete match and all captures, not just yes/no.
REGEX_CASES = [
    ("dot", "abc", "{{p}}", "a.c"),
    ("anchors", "abc", "{{p}}", "^a.c$"),
    ("quoted-dot", "abc", '"{{p}}"', "a.c"),
    ("quoted-anchors", "abc", '"{{p}}"', "^a.c$"),
    ("quoted-dot-match", "a.c", '"{{p}}"', "a.c"),
    ("quoted-anchor-match", "^a.c$", '"{{p}}"', "^a.c$"),
    ("captures", "abc", "{{p}}", "^(a|z)(b.c|bc)$"),
    ("alternation", "xyz", "{{p}}", "^(abc|xyz)$"),
    ("bracket", "abbc", "{{p}}", "^a[b-d]+c$"),
    ("class-repeat", "a12", "{{p}}", "^[[:alpha:]][[:digit:]]{2}$"),
    ("negated-bracket", "xyz", "{{p}}", "^[^0-9]{3}$"),
    ("literal-hyphen", "-", '[a"-"{{p}}]', "z"),
    ("equals", "a=b", "{{p}}", "a=b"),
    ("assignment-lookalike", "a=~", "{{p}}", "a=~"),
    ("append-lookalike", "aaa=~", "{{p}}", "a+=~"),
    ("optional", "ac", "{{p}}", "^ab?c$"),
    ("escaped-dot", "a.c", "{{p}}", r"^a\.c$"),
    ("escaped-parens", "(abc)", "{{p}}", r"^\(abc\)$"),
    ("backslash", r"a\b", "{{p}}", r"a\\b"),
    ("quoted-backslash", r"a\.c", '"{{p}}"', r"a\.c"),
    ("double-escaped-backslash", r"a\b", '"{{p}}"', r"a\\b"),
    ("single-quoted-backslash", r"a\b", "'{{p}}'", r"a\b"),
    ("escaped-dollar", "a$c", "{{p}}", r"a\$c"),
    ("escaped-backtick", "a`c", "{{p}}", r"a\`c"),
    ("split-escape", "a.c", r"a{{slash}}{{empty}}{{p}}c", "."),
    ("authored-escape", "a.c", r"a\{{empty}}{{p}}c", "."),
    ("partial-dot", "a.c", 'a"."{{p}}', "c"),
    ("partial-hole", "a.c", '^"{{p}}"c$', "a."),
    ("empty-quoted", "abc", '"{{p}}"', ""),
    ("empty-adjacent", "abc", "{{empty}}{{p}}", "^a.c$"),
    ("split-adjacent", "abc", "{{left}}{{p}}", "c$"),
    ("unicode", "λ空", "{{p}}", "^λ(空)$"),
    ("authored-param", "abc", "$PREFIX{{p}}", ".c$"),
    ("authored-braced", "abc", "${PREFIX}{{p}}", ".c$"),
    ("authored-quoted-param", "a.c", '"$PREFIX"{{p}}', r"\.c$"),
    ("authored-default-param", "abc", "${UNSET_FOR_REGEX:-a}{{p}}", ".c$"),
    ("authored-command", "abc", "$(printf a){{p}}", ".c$"),
    ("authored-quoted-command", "abc", '"$(printf a)"{{p}}', ".c$"),
    ("nested-data-command", "abc", "a$(printf '%s' '{{p}}')", "bc"),
    ("tilde", "~missing-user/abc", "~{{p}}/a.c", "missing-user"),
    ("tilde-authored-param", "~a/abc", "~$PREFIX/{{p}}", "a.c"),
    ("tilde-escaped-dot", "~missing-user/a.c", "~missing-user/{{p}}", r"a\.c"),
]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("case,lhs,rhs,pattern", REGEX_CASES, ids=[r[0] for r in REGEX_CASES])
def test_closed_regex_reference_matrix(case, lhs, rhs, pattern, native):
    context = {"p": pattern, "empty": "", "left": "^ab", "slash": "\\"}
    source = (
        "PREFIX=a; unset UNSET_FOR_REGEX; if [[ '" + lhs + "' =~ " + rhs + " ]]; then "
        "printf yes; printf '<%s>' \"${BASH_REMATCH[@]}\"; else printf no; fi"
    )
    converter = native_string if native else object.__new__(RecipeExecutor).substitute_variables
    reference = converter(source, context)
    candidate = prepare_with_converter(source, context, native)
    assert run_bash(candidate) == run_bash(reference)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("value", ["a'b", "a'.b", "λ'.空"])
def test_quoted_regex_apostrophe_keeps_literal_meaning(value, native):
    source = (
        'if [[ "' + value + '" =~ "{{p}}" ]]; then printf yes; '
        "else printf no; fi"
    )
    converter = native_string if native else object.__new__(RecipeExecutor).substitute_variables
    reference = converter(source, {"p": value})
    assert run_bash(reference) == (b"yes", b"", 0)
    candidate = prepare_with_converter(source, {"p": value}, native)
    assert run_bash(candidate) == run_bash(reference)


REGEX_CONFINEMENT_PATTERNS = [
    "$(touch marker)", "$((x=7))", "<(touch marker)", "`touch marker`",
    "'\";touch marker", "x ]] || touch marker #", "x && touch marker",
    "x > marker", "x\ntouch marker", "${HOME}", "a|$(touch marker)",
]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("pattern", REGEX_CONFINEMENT_PATTERNS)
def test_regex_confinement_never_runs_malicious_reference(pattern, quoted, native, tmp_path):
    rhs = '"{{p}}"' if quoted else "{{p}}"
    source = "x=0; if [[ abc =~ " + rhs + " ]]; then printf yes; else printf no; fi; printf ':x=%s' \"$x\""
    try:
        candidate = prepare_with_converter(source, {"p": pattern}, native)
    except (ShellSourceError, ValueError) as error:
        # Native and canonical packages have independent typed error classes.
        assert getattr(error, "code", None)
    else:
        stdout, stderr, status = run_bash(candidate, tmp_path)
        assert status == 0
        assert stderr == b""
        assert stdout in (b"yes:x=0", b"no:x=0")
    assert not (tmp_path / "marker").exists()