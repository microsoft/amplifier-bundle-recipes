"""Actual STRING converters and both engine preparation seams, without models."""

import subprocess
from unittest.mock import AsyncMock

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


LEGACY_ARITHMETIC_DATA = [
    ("{{p}}", {"p": "$[1+1]"}),
    ("{{p}}", {"p": "$[x]"}),
    ("{{p}}", {"p": "$[x=7]"}),
    ("{{left}}{{p}}", {"left": "$[", "p": "x=7]"}),
    ("$[{{empty}}x=7]{{p}}", {"empty": "", "p": "z"}),
    ("$[x{{empty}}+1]{{p}}", {"empty": "", "p": "z"}),
    ("{{p}}", {"p": "$[(x<<2)|1]"}),
    ("{{left}}{{p}}", {"left": "$[(x<<", "p": "2)|1]"}),
    ("$[(x<<2)+{{empty}}1]{{p}}", {"empty": "", "p": "z"}),
    ("$[$(cat <<DOC\n{{empty}}\nDOC\n)+1]{{p}}", {"empty": "", "p": "z"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("policy", ["fail", "continue", "skip_remaining"])
@pytest.mark.parametrize("rhs,context", LEGACY_ARITHMETIC_DATA)
async def test_legacy_arithmetic_data_refused_at_actual_engine_launch(
    rhs, context, native, policy, monkeypatch, tmp_path,
):
    import asyncio
    from amplifier_module_tool_recipes.models import Step
    from amplifier_module_tool_recipes.executor import SkipRemainingError
    from amplifier_recipe_runner.engine import (
        StepEngine, SkipRemaining, StepFailedError, StepSpec,
    )

    launch = AsyncMock(side_effect=AssertionError("recipe command must not launch"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    source = "x=0; [[ abc =~ " + rhs + " ]]; printf '%s' \"$x\""
    if native:
        engine = object.__new__(StepEngine)
        emitted = []
        engine._emit = lambda *args: emitted.append(args)
        call = engine._run_bash(
            StepSpec(id="arithmetic", type="bash", command=source, on_error=policy),
            context,
        )
        failure, skip = StepFailedError, SkipRemaining
    else:
        engine = object.__new__(RecipeExecutor)
        call = engine._execute_bash_step(
            Step(id="arithmetic", type="bash", command=source, on_error=policy),
            context, tmp_path,
        )
        failure, skip = ValueError, SkipRemainingError
    if policy == "continue":
        outcome = await call
        assert outcome.stdout == ""
        assert outcome.exit_code == 126
        assert "refused shell preparation" in outcome.stderr
        if native:
            assert emitted[0][0] == "step:failed"
            assert emitted[0][1]["absorbed"] is True
    else:
        with pytest.raises(failure if policy == "fail" else skip) as refused:
            await call
        assert getattr(refused.value.__cause__, "code", None)
    launch.assert_not_called()


# Fixed benign references only: trusted arithmetic, not DATA, may evaluate.
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("atom,lhs", [
    ("$[x]", "2"), ("$[x*3+1]", "7"), ("$[x=7]", "7"),
    ("$[a[1]+1]", "5"), ("$[$(printf 2)+1]", "3"),
    ("$[$(printf 2 | cat)+1]", "3"),
    ("$[(x<<2)|1]", "9"), ("$[x>1?x:1]", "2"),
    ("$[a[$(printf 1)]+1]", "5"), ("$[16#ff&15]", "15"),
])
@pytest.mark.parametrize("quoted", [False, True])
def test_wholly_authored_legacy_arithmetic_keeps_exact_meaning(atom, lhs, quoted, native):
    rhs = ('"' + atom + '"' if quoted else atom) + "{{p}}"
    source = (
        "x=2; a=(0 4); if [[ " + lhs + "z =~ " + rhs + " ]]; then "
        "printf yes; printf '<%s>' \"${BASH_REMATCH[@]}\"; else printf no; fi; "
        "printf ':x=%s' \"$x\""
    )
    context = {"p": "z"}
    converter = native_string if native else object.__new__(RecipeExecutor).substitute_variables
    reference = converter(source, context)
    candidate = prepare_with_converter(source, context, native)
    assert atom in candidate  # No normalization to $((...)) or numeric whitelist.
    assert run_bash(candidate) == run_bash(reference)
    assert run_bash(candidate)[0].startswith(b"yes")


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
def test_fixed_authored_legacy_arithmetic_with_real_nested_document_fifo(quoted, native):
    # CLOSED benign raw reference: both documents are literals; the second
    # redirect supplies 2 to cat, so the arithmetic value is exactly 3.
    atom = "$[$(cat <<FIRST <<SECOND\n1\nFIRST\n2\nSECOND\n)+1]"
    rhs = ('"' + atom + '"' if quoted else atom) + "{{p}}"
    source = (
        "if [[ 3z =~ " + rhs + " ]]; then printf yes; "
        "printf '<%s>' \"${BASH_REMATCH[@]}\"; else printf no; fi"
    )
    context = {"p": "z"}
    converter = native_string if native else object.__new__(RecipeExecutor).substitute_variables
    reference = converter(source, context)
    assert run_bash(reference) == (b"yes<3z>", b"", 0)
    candidate = prepare_with_converter(source, context, native)
    assert atom in candidate
    assert run_bash(candidate) == run_bash(reference)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("rhs,context", [
    ("'{{p}}'", {"p": "$[x=7]"}),
    ('"{{p}}"', {"p": "$[x=7]"}),
    ("{{p}}", {"p": r"\$[x=7]"}),
    (r"\$[x=7]{{p}}", {"p": ""}),
    ("'$[x=7]'{{p}}", {"p": ""}),
    (r'"\$[x=7]"{{p}}', {"p": ""}),
    ("'{{p}}'", {"p": "$[(x=7)<<2]"}),
    ('"{{p}}"', {"p": "$[(x=7)<<2]"}),
    (r"\$\[\(x=7\)\<\<2\]{{p}}", {"p": ""}),
    ("'$[(x=7)<<2]'{{p}}", {"p": ""}),
    (r'"\$[(x=7)<<2]"{{p}}', {"p": ""}),
])
def test_legacy_arithmetic_literal_lookalikes_stay_inert(rhs, context, native):
    source = (
        "x=0; if [[ '$[x=7]' =~ " + rhs + " ]]; then printf yes; else printf no; fi; "
        "printf ':x=%s' \"$x\""
    )
    candidate = prepare_with_converter(source, context, native)
    stdout, stderr, status = run_bash(candidate)
    assert stderr == b"" and status == 0
    assert stdout in (b"yes:x=0", b"no:x=0")


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("lhs,rhs,value,expected", [
    ("abc", "{{p}}", "^(a)(bc)$", "yes<abc><a><bc>:x=0"),
    ("$[x=7]", '"{{p}}"', "$[x=7]", "yes<$[x=7]>:x=0"),
    ("$[x=7]", "'{{p}}'", "$[x=7]", "yes<$[x=7]>:x=0"),
    ("abc", '"{{p}}"', "a.c", "no:x=0"),
    ("2z", "$[x+2]{{p}}", "z", "yes<2z>:x=0"),
])
async def test_actual_engine_launch_keeps_benign_regex_and_literal_controls(
    lhs, rhs, value, expected, native, tmp_path,
):
    from amplifier_module_tool_recipes.models import Step
    from amplifier_recipe_runner.engine import StepEngine, StepSpec

    source = (
        "x=0; if [[ '" + lhs + "' =~ " + rhs + " ]]; then "
        "printf yes; printf '<%s>' \"${BASH_REMATCH[@]}\"; else printf no; fi; "
        "printf ':x=%s' \"$x\""
    )
    if native:
        engine = object.__new__(StepEngine)
        engine._workspace = tmp_path
        engine._scratch_dir = None
        outcome = await engine._run_bash(
            StepSpec(id="benign", type="bash", command=source), {"p": value},
        )
    else:
        engine = object.__new__(RecipeExecutor)
        outcome = await engine._execute_bash_step(
            Step(id="benign", type="bash", command=source), {"p": value}, tmp_path,
        )
    assert outcome.stdout == expected
    assert outcome.stderr == ""
    assert outcome.exit_code == 0


BOUNDARY_PARAMETER_REFERENCES = [
    ('${v//[/1}', "v='['", b"2:z", None),
    ('${v//]/1}', "v=']'", b"2:z", "INVALID_TEMPLATE_SYNTAX"),
    ('${v//"["/1}', "v='['", b"2:z", None),
    (r'${v//\[/1}', "v='['", b"2:z", None),
    (r'${v//\}/1}', "v='}'", b"2:z", None),
    ('${arr[i]}', "arr=(1); i=0", b"2:z", None),
    ('${v:-${w:-2}}', "unset v w", b"3:z", None),
    ('${v:-$((1+1))}', "unset v", b"3:z", None),
    ('${v:-$(cat <<DOC\n2\nDOC\n)}', "unset v", b"3:z", None),
]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("style", ["legacy", "modern", "bare"])
@pytest.mark.parametrize("parameter,setup,expected,refusal", BOUNDARY_PARAMETER_REFERENCES)
def test_boundary_fixed_parameter_reference_matrix(parameter, setup, expected, refusal, style, native):
    expression = {
        "legacy": "x=$[ " + parameter + " + 1 ]",
        "modern": "x=$(( " + parameter + " + 1 ))",
        "bare": "(( x = " + parameter + " + 1 ))",
    }[style]
    source = setup + "; " + expression + "; printf '%s:%s' \"$x\" '{{p}}'"
    # Fixed benign raw reference only; no synthetic corrupted source runs.
    reference = source.replace("{{p}}", "z")
    actual_reference = run_bash(reference)
    if style == "legacy" and parameter in ("${v//[/1}", "${v//]/1}"):
        # Bash 5.2 itself cannot delimit these literal legacy bracket patterns.
        # Preserve the exact spelling and report the failure, not a rewrite.
        assert actual_reference[0] == b""
        assert actual_reference[2] == (2 if parameter == "${v//[/1}" else 1)
        assert (b"matching `]'" if parameter == "${v//[/1}" else b"no closing `}'") in actual_reference[1]
    else:
        assert actual_reference == (expected, b"", 0)
    if refusal:
        with pytest.raises(ValueError) as error:
            prepare_with_converter(source, {"p": "z"}, native)
        assert getattr(error.value, "code", None) == refusal
    else:
        candidate = prepare_with_converter(source, {"p": "z"}, native)
        assert parameter in candidate
        assert run_bash(candidate) == actual_reference


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_boundary_fixed_parameter_pattern_reaches_real_engine(native, tmp_path):
    from amplifier_module_tool_recipes.models import Step
    from amplifier_recipe_runner.engine import StepEngine, StepSpec

    source = "v='['; x=$(( ${v//[/1} + 1 )); printf '%s:%s' \"$x\" '{{p}}'"
    assert run_bash(source.replace("{{p}}", "z")) == (b"2:z", b"", 0)
    assert run_bash(prepare_with_converter(source, {"p": "z"}, native)) == (b"2:z", b"", 0)
    if native:
        engine = object.__new__(StepEngine)
        engine._workspace = tmp_path
        engine._scratch_dir = None
        outcome = await engine._run_bash(
            StepSpec(id="parameter", type="bash", command=source), {"p": "z"},
        )
    else:
        engine = object.__new__(RecipeExecutor)
        outcome = await engine._execute_bash_step(
            Step(id="parameter", type="bash", command=source), {"p": "z"}, tmp_path,
        )
    assert (outcome.stdout, outcome.stderr, outcome.exit_code) == ("2:z", "", 0)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("style", ["modern", "bare"])
@pytest.mark.parametrize("value,expected", [("2", b"3"), ("", b"1")])
def test_boundary_legitimate_nested_document_data_retains_meaning(native, style, value, expected):
    inner = "$(cat <<FIRST <<DOC\n7\nFIRST\n{{n}}\nDOC\n)"
    expression = "x=$((" + inner + "+1))" if style == "modern" else "((x=" + inner + "+1))"
    source = expression + "; printf '%s' \"$x\""
    assert run_bash(source.replace("{{n}}", value)) == (expected, b"", 0)
    candidate = prepare_with_converter(source, {"n": value}, native)
    assert run_bash(candidate) == (expected, b"", 0)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("style", ["modern", "bare"])
def test_boundary_data_delimiter_collision_keeps_authored_sibling(native, style):
    # DOC is a benign arithmetic variable (zero), so the renamed body remains
    # a valid arithmetic expression and its fixed meaning is independently 3.
    inner = "$(cat <<FIRST <<DOC\n7\nFIRST\n{{n}}\nDOC\n)"
    expression = "x=$((" + inner + "+1))" if style == "modern" else "((x=" + inner + "+1))"
    source = "DOC=0; " + expression + "; printf '%s' \"$x\""
    candidate = prepare_with_converter(source, {"n": "DOC\n+2"}, native)
    assert "<<__AMP_HDOC_0__" in candidate
    assert "<<FIRST" in candidate and "\n7\nFIRST\n" in candidate
    reference = "DOC=0; " + expression.replace("{{n}}", "DOC\n+2").replace(
        "<<DOC\n", "<<SAFE\n"
    ).replace("\nDOC\n)", "\nSAFE\n)") + "; printf '%s' \"$x\""
    assert run_bash(reference) == (b"3", b"", 0)
    assert run_bash(candidate) == (b"3", b"", 0)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("value", ["-\n\n", "- -\n\n"])
def test_boundary_multiargument_header_handoff_retains_benign_meaning(native, value):
    source = (
        "x=$(( $(cat <<FIRST <<SECOND - {{suffix}}\n"
        "7\nFIRST\n2\nSECOND\n) + 1 )); printf '%s' \"$x\""
    )
    assert run_bash(source.replace("{{suffix}}", value)) == (b"3", b"", 0)
    assert run_bash(prepare_with_converter(source, {"suffix": value}, native)) == (b"3", b"", 0)