"""R4 body ownership, actual Bash output, and independent witness controls."""

import subprocess
from dataclasses import replace

import pytest
from amplifier_module_tool_recipes.shell_source.heredoc import (
    compile_document,
    verify_document,
)
from amplifier_module_tool_recipes.shell_source.model import (
    EventDisposition,
    FenceEvent,
    HereDocument,
    OutputOrigin,
    ShellSourceError,
    Span,
    TextEvent,
)


def document(*parts, quoted=False, delimiter="EOF"):
    events = []
    offset = 10
    occurrence = 0
    terminal = None
    for kind, text in parts:
        if kind == "T":
            for char in text:
                span = Span(offset, offset + len(char.encode("utf-8")))
                events.append(TextEvent("trusted", char, span))
                terminal = span if char == "\n" else None
                offset = span.end
        else:
            source = Span(offset, offset + 9)
            events.append(FenceEvent(occurrence, "start", source))
            value_offset = 0
            for char in text:
                value_span = Span(
                    value_offset, value_offset + len(char.encode("utf-8"))
                )
                events.append(TextEvent("data", char, source, occurrence, value_span))
                value_offset = value_span.end
                terminal = None
            events.append(FenceEvent(occurrence, "end", source))
            occurrence += 1
            offset = source.end
    return HereDocument(
        0,
        0,
        delimiter,
        quoted,
        False,
        Span(0, len(delimiter)),
        Span(10, offset),
        Span(offset, offset + len(delimiter)),
        tuple(events),
        terminal,
    )


def actual_output(doc, encoded):
    opener = repr(encoded.delimiter) if doc.quoted else encoded.delimiter
    command = f"cat <<{opener}\n{encoded.body}{encoded.delimiter}\n"
    completed = subprocess.run(
        ["bash", "--noprofile", "--norc"],
        input=command.encode(),
        capture_output=True,
        check=False,
        timeout=3,
        env={
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C.UTF-8",
            "stamp": "STAMP",
            "sta": "SHORT",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == b""
    return completed.stdout.decode()


@pytest.mark.parametrize(
    "parts, expected",
    [
        (
            (("T", "\\"), ("D", "$(printf '%s' MARKER)"), ("T", "\n")),
            "$(printf '%s' MARKER)\n",
        ),
        (
            (("T", "\\"), ("D", ""), ("D", "$"), ("D", "(printf MARKER)"), ("T", "\n")),
            "$(printf MARKER)\n",
        ),
        ((("T", "\\"), ("D", "a"), ("T", "\n")), "\\a\n"),
        ((("T", "\\"), ("D", ""), ("T", "$stamp\n")), "$stamp\n"),
        (
            (("T", "\\"), ("D", "\\"), ("D", "$(printf MARKER)"), ("T", "\n")),
            "\\$(printf MARKER)\n",
        ),
        ((("T", "\\"), ("D", "\nA"), ("T", "\n")), "A\n"),
        ((("D", "\\\n"), ("T", "A\n")), "\\\nA\n"),
        ((("T", "$stamp; "), ("D", "two\nlines"), ("T", "\n")), "STAMP; two\nlines\n"),
        ((("T", "$stamp"), ("D", "more"), ("T", "\n")), "STAMPmore\n"),
        ((("T", "$sta"), ("D", "mp"), ("T", "\n")), "SHORTmp\n"),
        ((("T", "$"), ("D", "stamp"), ("T", "\n")), "$stamp\n"),
        ((("D", "trailing\\"), ("T", "\n")), "trailing\\\n"),
        ((("D", "ends\n"), ("T", "\n")), "ends\n\n"),
        ((("T", "${stamp}"), ("D", "_end"), ("T", "\n")), "STAMP_end\n"),
        ((("T", "$stamp"), ("D", "雪"), ("T", "\n")), "STAMP雪\n"),
        ((("T", "雪\\"), ("D", "☃"), ("T", "\n")), "雪\\☃\n"),
        ((("T", "$sta\\\nmp"), ("D", "!"), ("T", "\n")), "STAMP!\n"),
        ((("T", "\\\\\n"), ("D", "next"), ("T", "\n")), "\\\nnext\n"),
        ((("D", "\n\n"), ("T", "BODY\n")), "\n\nBODY\n"),
    ],
)
def test_composed_escape_and_parameter_effects_in_actual_bash(parts, expected):
    doc = document(*parts)
    encoded = compile_document(doc)
    assert actual_output(doc, encoded) == expected
    assert len(encoded.dispositions) == len(doc.events)


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize(
    "value",
    [
        "$(printf BAD)",
        "`printf BAD`",
        "${stamp}",
        "a\nb\r\nc",
        "{{another}}",
        "é雪",
        "",
    ],
)
def test_data_remains_literal_in_both_expansion_modes(quoted, value):
    doc = document(("D", value), ("T", "\n"), quoted=quoted)
    encoded = compile_document(doc)
    assert actual_output(doc, encoded) == value + "\n"
    if quoted:
        assert encoded.body == value + "\n"


@pytest.mark.parametrize("quoted", [False, True])
def test_collision_scan_sees_adjacent_empty_holes_and_all_candidate_lines(quoted):
    doc = document(
        ("D", "EO"), ("D", ""), ("D", "F\n__AMP_HDOC_0__\n"), ("T", "\n"), quoted=quoted
    )
    encoded = compile_document(doc)
    assert encoded.delimiter == "__AMP_HDOC_1__"
    assert actual_output(doc, encoded) == "EOF\n__AMP_HDOC_0__\n\n"


def test_trusted_logical_collision_is_rotated_before_closer():
    doc = document(("T", "EO\\\nF"), ("D", ""), ("T", "\n"))
    encoded = compile_document(doc)
    assert encoded.delimiter == "__AMP_HDOC_0__"
    assert actual_output(doc, encoded) == "EOF\n"


@pytest.mark.parametrize("value", ["EOF ", "EOF\r", "EO\\\nF"])
def test_collision_matching_does_not_strip_or_normalize(value):
    doc = document(("D", value), ("T", "\n"))
    encoded = compile_document(doc)
    assert encoded.delimiter == "EOF"
    assert actual_output(doc, encoded) == value + "\n"


def test_final_authored_lf_cannot_be_swallowed_even_after_bol():
    doc = document(("T", "A\n\\"), ("D", ""), ("T", "\n"))
    with pytest.raises(ShellSourceError, match="BODY_OWNERSHIP_CONFLICT"):
        compile_document(doc)


@pytest.mark.parametrize(
    "prefix, suffix", [("${sta", "}"), ("${", "stamp}"), ("${stamp:-", "x}")]
)
def test_empty_fence_cannot_complete_an_active_braced_parameter(prefix, suffix):
    doc = document(("T", prefix), ("D", ""), ("T", suffix + "\n"))
    with pytest.raises(ShellSourceError, match="MIXED_OR_UNSUPPORTED_PARAMETER"):
        compile_document(doc)


@pytest.mark.parametrize(
    "expansion",
    [
        "$1",
        "$@",
        "$*",
        "$#",
        "$?",
        "$-",
        "$$",
        "$!",
        "$(echo ok)",
        "$((1+1))",
        "$[1+1]",
        "`echo ok`",
        "${stamp:-ok}",
    ],
)
def test_unsupported_active_trusted_expansion_is_not_silently_literalized(expansion):
    doc = document(("T", expansion), ("D", ""), ("T", "\n"))
    with pytest.raises(ShellSourceError, match="UNSUPPORTED"):
        compile_document(doc)


def test_fully_authored_body_is_unchanged_even_with_complex_expansions():
    doc = document(("T", "$(printf ok) $((1+1)) ${stamp:-fallback}\n"))
    encoded = compile_document(doc)
    assert encoded.body == "$(printf ok) $((1+1)) ${stamp:-fallback}\n"
    assert actual_output(doc, encoded) == "ok 2 STAMP\n"


@pytest.mark.parametrize("quoted", [False, True])
def test_original_empty_document_has_no_invented_lf(quoted):
    doc = document(quoted=quoted)
    encoded = compile_document(doc)
    assert encoded.body == ""
    assert actual_output(doc, encoded) == ""


def test_quoted_body_preserves_backslash_and_cr_without_cooking():
    doc = document(("T", "\\"), ("D", "$stamp\r\n`x`\\"), ("T", "\n"), quoted=True)
    encoded = compile_document(doc)
    assert actual_output(doc, encoded) == "\\$stamp\r\n`x`\\\n"


def test_verifier_rejects_changed_source_with_self_consistent_length():
    doc = document(("D", "ax"), ("T", "\n"))
    encoded = compile_document(doc)
    with pytest.raises(ShellSourceError, match="DOCUMENT_WITNESS"):
        verify_document(doc, replace(encoded, body="$x\n"))


def test_verifier_rejects_swapped_identical_character_origins():
    doc = document(("D", "aa"), ("T", "\n"))
    encoded = compile_document(doc)
    first, second, *rest = encoded.origins
    origins = (
        replace(first, events=second.events),
        replace(second, events=first.events),
        *rest,
    )
    spans = {index: origin.output for origin in origins for index in origin.events}
    dispositions = tuple(
        replace(d, output=spans[d.event]) if d.event in spans else d
        for d in encoded.dispositions
    )
    with pytest.raises(ShellSourceError, match="ORIGIN_TRANSPORT|DOCUMENT_WITNESS"):
        verify_document(
            doc, replace(encoded, origins=origins, dispositions=dispositions)
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_origin",
        "duplicated_event",
        "missing_fence",
        "fake_zero",
        "unsafe_closer",
        "mid_utf8",
    ],
)
def test_verifier_rejects_incomplete_or_forged_origin_transport(mutation):
    doc = document(("D", "雪EOF"), ("T", "\n"))
    encoded = compile_document(doc)
    if mutation == "missing_origin":
        bad = replace(encoded, origins=encoded.origins[1:])
    elif mutation == "duplicated_event":
        bad = replace(
            encoded,
            origins=(replace(encoded.origins[0], events=(1, 1)), *encoded.origins[1:]),
        )
    elif mutation == "missing_fence":
        bad = replace(encoded, dispositions=encoded.dispositions[1:])
    elif mutation == "fake_zero":
        bad = replace(
            encoded,
            dispositions=tuple(
                EventDisposition(d.event, Span(0, 0), "continuation")
                if d.event == 1
                else d
                for d in encoded.dispositions
            ),
        )
    elif mutation == "unsafe_closer":
        bad = replace(encoded, delimiter="雪EOF")
    else:
        bad = replace(
            encoded,
            origins=(
                OutputOrigin(Span(0, 1), (1,)),
                OutputOrigin(Span(1, 3), (1,)),
                *encoded.origins[1:],
            ),
        )
    with pytest.raises(ShellSourceError):
        verify_document(doc, bad)


def test_terminal_identity_is_checked_independently_of_endswith_lf():
    doc = document(("D", "data\n"), ("T", "\n"))
    with pytest.raises(ShellSourceError, match="BODY_OWNERSHIP_CONFLICT"):
        compile_document(replace(doc, terminal_lf_source=Span(10, 11)))


def test_parameter_name_is_braced_only_when_next_encoded_source_can_extend_it():
    assert (
        compile_document(document(("T", "$stamp"), ("D", "more"), ("T", "\n"))).body
        == "${stamp}more\n"
    )
    assert (
        compile_document(document(("T", "$stamp"), ("D", "!"), ("T", "\n"))).body
        == "$stamp!\n"
    )


def test_data_glob_and_bash_syntax_in_body_do_not_become_commands():
    value = "* ? [abc] ; printf BAD\nEOF\nprintf BAD\n"
    doc = document(("D", value), ("T", "\n"))
    assert actual_output(doc, compile_document(doc)) == value + "\n"
