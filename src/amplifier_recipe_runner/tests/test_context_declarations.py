"""A declarative `context:` entry binds its default -- or is refused by name.

Defect (recipes-u2f): a recipe's `context:` is a variable -> VALUE mapping, and
both engines merged it as such. An entry written in the declarative schema form
every other tool uses --

    context:
      continue_from:
        type: string
        default: ""

-- was therefore bound AS THE SCHEMA MAPPING. `{{continue_from}}` substituted
`{'type': 'string', 'default': '', ...}` into prompts (as noise) and into
conditions (as a hard failure). Nothing diagnosed it.

These tests pin the library's half of the fix at every layer it can be seen:
:func:`~amplifier_recipe_runner.manifest.resolve_context_block` (semantics),
:func:`~amplifier_recipe_runner.manifest.parse_manifest` and the ``validate``
command (a malformed declaration reported BY NAME),
:func:`~amplifier_recipe_runner.engine.parse_program` (the default is bound,
the mapping never is), and :func:`amplifier_recipe_runner.run` (an unsupplied
``required: true`` variable stops the run before any step).
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import amplifier_recipe_runner as pkg
from amplifier_recipe_runner.api import RunRequest
from amplifier_recipe_runner.api import RunStatus
from amplifier_recipe_runner.cli import EXIT_PREFLIGHT
from amplifier_recipe_runner.cli import cli
from amplifier_recipe_runner.engine import parse_program
from amplifier_recipe_runner.errors import MissingContextVariableError
from amplifier_recipe_runner.manifest import ManifestError
from amplifier_recipe_runner.manifest import is_context_declaration
from amplifier_recipe_runner.manifest import parse_manifest
from amplifier_recipe_runner.manifest import resolve_context_block
from amplifier_recipe_runner.ports import HostServices
from amplifier_recipe_runner.resolver import LocalBundleResolver


# --------------------------------------------------------------------------
# recognition
# --------------------------------------------------------------------------


class TestRecognition:
    def test_schema_form_is_a_declaration(self) -> None:
        assert is_context_declaration({"type": "string", "default": ""})
        assert is_context_declaration({"required": True})
        assert is_context_declaration({"description": "doc only"})

    def test_mapping_with_any_other_key_is_a_plain_value(self) -> None:
        # Guards recipes that legitimately carry dict-valued context entries.
        assert not is_context_declaration({"type": "string", "repo_owner": "x"})
        assert not is_context_declaration({"by_impact": {}, "themes": []})

    def test_empty_or_non_mapping_is_a_plain_value(self) -> None:
        assert not is_context_declaration({})
        assert not is_context_declaration("plain")
        assert not is_context_declaration(None)


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


class TestResolution:
    def test_default_is_bound_and_the_mapping_never_is(self) -> None:
        resolved = resolve_context_block({"topic": {"type": "string", "default": "auth"}})
        assert resolved.values == {"topic": "auth"}
        assert not isinstance(resolved.values["topic"], dict)

    def test_required_without_a_default_defers_to_the_caller(self) -> None:
        resolved = resolve_context_block({"topic": {"type": "string", "required": True}})
        assert resolved.values == {}
        assert resolved.required == ("topic",)

    def test_optional_and_defaultless_is_left_unbound(self) -> None:
        resolved = resolve_context_block({"note": {"type": "string"}})
        assert resolved.values == {}
        assert resolved.required == ()

    def test_plain_values_are_untouched(self) -> None:
        block = {"a": "x", "n": 3, "d": {"by_impact": {}, "themes": []}, "e": {}}
        assert resolve_context_block(block).values == block

    def test_required_beside_a_default_warns_and_binds_the_default(self) -> None:
        resolved = resolve_context_block({"topic": {"required": True, "default": "auth"}})
        assert resolved.values == {"topic": "auth"}
        assert resolved.required == ()
        assert any("topic" in warning for warning in resolved.warnings)

    @pytest.mark.parametrize(
        "declaration",
        [
            {"required": "yes"},
            {"type": "str"},
            {"type": 3},
            {"description": ["not", "a", "string"]},
            {"enum": []},
            {"enum": "a,b"},
            {"enum": ["a", "b"], "default": "c"},
        ],
    )
    def test_malformed_is_reported_by_variable_name_and_bound_to_nothing(
        self, declaration: dict[str, Any]
    ) -> None:
        resolved = resolve_context_block({"topic": declaration})
        assert resolved.errors, f"{declaration!r} should be reported"
        assert all("'topic'" in message for message in resolved.errors)
        assert "topic" not in resolved.values


# --------------------------------------------------------------------------
# parse -- manifest and program
# --------------------------------------------------------------------------


MALFORMED = """
schema_version: 2
name: ctx
dependencies: []
context:
  topic:
    type: str
    required: true
steps:
  - id: echo
    type: bash
    command: "echo '{{topic}}'"
"""

DECLARATIVE = """
schema_version: 2
name: ctx
dependencies: []
context:
  continue_from:
    type: string
    default: "prior.md"
    description: Path to a previously verified document
  topic:
    type: string
    required: true
steps:
  - id: echo
    type: bash
    command: "echo '{{continue_from}}'"
    output: rendered
"""


def _body(text: str) -> dict[str, Any]:
    return yaml.safe_load(textwrap.dedent(text))


class TestParse:
    def test_manifest_refuses_a_malformed_declaration_by_name(self) -> None:
        with pytest.raises(ManifestError) as excinfo:
            parse_manifest(_body(MALFORMED), source="ctx.yaml")
        assert "'topic'" in str(excinfo.value)

    def test_manifest_accepts_a_well_formed_declaration(self) -> None:
        manifest = parse_manifest(_body(DECLARATIVE), source="ctx.yaml")
        assert manifest.schema_version == 2

    def test_program_binds_the_default_and_records_the_required_name(self) -> None:
        program = parse_program(_body(DECLARATIVE))
        assert program.context == {"continue_from": "prior.md"}
        assert program.required_context == ("topic",)

    def test_program_refuses_a_malformed_declaration_by_name(self) -> None:
        with pytest.raises(ManifestError) as excinfo:
            parse_program(_body(MALFORMED))
        assert "'topic'" in str(excinfo.value)


# --------------------------------------------------------------------------
# validate -- the CLI reports it rather than running it
# --------------------------------------------------------------------------


def test_validate_reports_a_malformed_declaration_as_a_report_error(tmp_path: Path) -> None:
    recipe = tmp_path / "ctx.yaml"
    recipe.write_text(textwrap.dedent(MALFORMED).lstrip(), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        ["--workspace", str(tmp_path), "--json", "validate", "--offline", str(recipe)],
        catch_exceptions=False,
    )

    assert result.exit_code == EXIT_PREFLIGHT, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "'topic'" in payload["errors"][0]["message"]


# --------------------------------------------------------------------------
# run -- end to end
# --------------------------------------------------------------------------


class Providers:
    """The provider_access port, unused by a bash-only recipe."""

    def preference_for(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        return None


def _run(recipe_text: str, tmp_path: Path, context: dict[str, Any] | None = None):
    recipe = tmp_path / "ctx.yaml"
    recipe.write_text(textwrap.dedent(recipe_text).lstrip(), encoding="utf-8")
    request = RunRequest(
        recipe=recipe,
        context=context or {},
        services=HostServices(provider_access=Providers(), workspace=tmp_path),  # type: ignore[arg-type]
        run_id="run-test",
    )
    return asyncio.run(pkg.run(request, resolver=LocalBundleResolver()))


def test_run_renders_the_default_not_the_declaration(tmp_path: Path) -> None:
    result = _run(DECLARATIVE, tmp_path, {"topic": "sessions"})

    assert result.status is RunStatus.SUCCEEDED, result.error
    assert result.context["rendered"].strip() == "prior.md"
    assert "{'type'" not in result.context["rendered"]


def test_run_refuses_an_unsupplied_required_variable_by_name(tmp_path: Path) -> None:
    with pytest.raises(MissingContextVariableError) as excinfo:
        _run(DECLARATIVE, tmp_path)

    assert excinfo.value.variables == ("topic",)
    assert "'topic'" in str(excinfo.value)
