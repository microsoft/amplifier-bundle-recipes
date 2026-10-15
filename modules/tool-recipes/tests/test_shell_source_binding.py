"""Binding prerequisites, independent of any eventual Bash admission verdict."""

import pytest

from amplifier_module_tool_recipes.shell_source.binding import bind, events_in, neutral_bytes
from amplifier_module_tool_recipes.shell_source.model import FenceEvent, ShellSourceError, Span, TextEvent


def test_binding_preserves_original_order_empty_fences_and_utf8_byte_spans():
    seen = []
    values = {"a": "λ{{not.a.second.lookup}}", "empty": "", "nest.value": "line\n"}

    def resolve(name):
        seen.append(name)
        return values[name]

    bound = bind("é:{{a}}{{empty}} / {{nest.value}} / {{a}}", resolve)
    assert seen == ["a", "empty", "nest.value", "a"]
    assert [o.id for o in bound.occurrences] == [0, 1, 2, 3]
    assert bound.occurrences[0].span.start == 3
    assert [e.edge for e in events_in(bound, bound.occurrences[1].span)] == ["start", "end"]
    data = [e for e in bound.events if isinstance(e, TextEvent) and e.kind == "data"]
    assert data[0].text == "λ" and data[0].value_span == Span(0, 2)
    assert sum(isinstance(e, FenceEvent) for e in bound.events) == 8
    neutral = neutral_bytes(bound)
    assert len(neutral) == len(bound.source.encode())
    assert neutral.startswith("é:".encode())
    assert b"{{" not in neutral


def test_converter_exception_is_not_replaced_or_retried():
    error = LookupError("existing converter error")
    calls = []

    def resolve(name):
        calls.append(name)
        raise error

    with pytest.raises(LookupError) as caught:
        bind("{{first}} {{second}}", resolve)
    assert caught.value is error
    assert calls == ["first"]


@pytest.mark.parametrize("value,code", [("a\x00b", "NUL_DATA"), ("\ud800", "INVALID_ENCODING")])
def test_invalid_process_source_data_fails_before_any_shell(value, code):
    with pytest.raises(ShellSourceError) as caught:
        bind("echo {{x}}", lambda _: value)
    assert caught.value.code == code


def test_owner_cannot_slice_a_hole_or_unicode_character():
    bound = bind("é{{x}}", lambda _: "value")
    with pytest.raises(ShellSourceError, match="SPLIT_OCCURRENCE"):
        events_in(bound, Span(0, 1))
    with pytest.raises(ShellSourceError, match="SPLIT_OCCURRENCE"):
        events_in(bound, Span(3, 6))
