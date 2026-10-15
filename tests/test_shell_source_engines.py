"""Actual STRING converters and both engine preparation seams, without models."""

import subprocess

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.executor import (
    _substitute_shell_variables as legacy_prepare,
)

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
