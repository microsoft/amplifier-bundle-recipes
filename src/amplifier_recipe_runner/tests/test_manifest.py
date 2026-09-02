"""Tests for schema_version 2 manifest parsing (recipe-dependency-manifest.v1)."""

from __future__ import annotations

import textwrap

import pytest

from amplifier_recipe_runner.manifest import CONTRACT
from amplifier_recipe_runner.manifest import Dependency
from amplifier_recipe_runner.manifest import LegacyRecipe
from amplifier_recipe_runner.manifest import Manifest
from amplifier_recipe_runner.manifest import ManifestError
from amplifier_recipe_runner.manifest import parse_manifest
from amplifier_recipe_runner.manifest import parse_manifest_file
from amplifier_recipe_runner.manifest import parse_manifest_text

VALID_V2 = textwrap.dedent(
    """
    schema_version: 2
    name: validator
    description: Validate a thing
    version: "1.0"
    dependencies:
      - source: git+https://github.com/microsoft/amplifier-foundation@main
        kind: bundle
        required_agents:
          - foundation:zen-architect
      - source: git+https://github.com/microsoft/amplifier-bundle-recipes@main#subdirectory=behaviors/review.yaml
        kind: behavior
    agents:
      architect: foundation:zen-architect
    steps:
      - id: review
        agent: architect
        prompt: Review it.
    """
)


# --- Core 1 / Core 2: valid v2 parse -------------------------------------


def test_valid_v2_parses_into_typed_manifest():
    manifest = parse_manifest_text(VALID_V2, source="recipe.yaml")

    assert isinstance(manifest, Manifest)
    assert manifest.is_legacy is False
    assert manifest.schema_version == 2
    assert manifest.source == "recipe.yaml"

    assert manifest.dependencies == (
        Dependency(
            source="git+https://github.com/microsoft/amplifier-foundation@main",
            kind="bundle",
            required_agents=("foundation:zen-architect",),
        ),
        Dependency(
            source=(
                "git+https://github.com/microsoft/amplifier-bundle-recipes@main"
                "#subdirectory=behaviors/review.yaml"
            ),
            kind="behavior",
            required_agents=(),
        ),
    )
    assert dict(manifest.agents) == {"architect": "foundation:zen-architect"}


def test_manifest_is_frozen_and_dependencies_immutable():
    manifest = parse_manifest_text(VALID_V2)

    with pytest.raises((AttributeError, TypeError)):
        manifest.schema_version = 3  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        manifest.dependencies[0].kind = "behavior"  # type: ignore[misc]
    with pytest.raises(TypeError):
        manifest.agents["extra"] = "ns:name"  # type: ignore[index]


def test_empty_dependency_list_is_allowed():
    manifest = parse_manifest({"schema_version": 2, "dependencies": []})
    assert isinstance(manifest, Manifest)
    assert manifest.dependencies == ()
    assert dict(manifest.agents) == {}


def test_parse_manifest_file_round_trip(tmp_path):
    path = tmp_path / "recipe.yaml"
    path.write_text(VALID_V2, encoding="utf-8")

    manifest = parse_manifest_file(path)

    assert isinstance(manifest, Manifest)
    assert manifest.source == str(path)


# --- Core 1: unknown keys are a parse ERROR naming the key ---------------


def test_unknown_top_level_key_errors_and_names_it():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"schema_version": 2, "dependencies": [], "naem": "typo"})

    message = str(excinfo.value)
    assert "'naem'" in message
    assert "unknown top-level manifest key" in message
    assert f"{CONTRACT} Core 1" in message


def test_unknown_top_level_keys_are_all_named():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(
            {"schema_version": 2, "dependencies": [], "naem": 1, "stesp": 2},
        )

    message = str(excinfo.value)
    assert "'naem'" in message
    assert "'stesp'" in message


def test_unknown_dependency_key_errors_and_names_it():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(
            {
                "schema_version": 2,
                "dependencies": [{"source": "pkg", "kind": "bundle", "agents": ["x"]}],
            }
        )

    message = str(excinfo.value)
    assert "'agents'" in message
    assert "dependencies[0]" in message
    assert f"{CONTRACT} Core 1" in message


def test_manifest_keys_without_schema_version_error():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"name": "r", "dependencies": []})

    message = str(excinfo.value)
    assert "'dependencies'" in message
    assert "schema_version" in message


# --- Core 12: agent_config is rejected at parse --------------------------


def test_agent_config_step_field_is_rejected():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(
            {
                "schema_version": 2,
                "dependencies": [],
                "steps": [
                    {"id": "review", "agent": "a", "prompt": "p", "agent_config": {"tools": []}}
                ],
            }
        )

    message = str(excinfo.value)
    assert "agent_config" in message
    assert "'review'" in message
    assert f"{CONTRACT} Core 12" in message


def test_agent_config_rejected_in_staged_and_nested_steps():
    staged = {
        "schema_version": 2,
        "dependencies": [],
        "stages": [
            {
                "name": "phase",
                "steps": [{"id": "s1", "agent_config": {}}],
            }
        ],
    }
    with pytest.raises(ManifestError, match="agent_config"):
        parse_manifest(staged)

    nested = {
        "schema_version": 2,
        "dependencies": [],
        "steps": [
            {
                "id": "loop",
                "foreach": "{{items}}",
                "steps": [{"id": "inner", "agent_config": {"model": "x"}}],
            }
        ],
    }
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(nested)
    assert "'inner'" in str(excinfo.value)


def test_agent_config_is_never_silently_retained():
    """A clean v2 recipe carries no agent_config anywhere in the parsed result."""
    manifest = parse_manifest_text(VALID_V2)
    assert not hasattr(manifest, "agent_config")
    assert all(not hasattr(dep, "agent_config") for dep in manifest.dependencies)


# --- Core 1 / Core 10: legacy marker -------------------------------------


def test_recipe_without_schema_version_is_legacy_not_an_error():
    legacy_yaml = textwrap.dedent(
        """
        name: old-recipe
        description: Pre-manifest recipe
        version: "1.0"
        steps:
          - id: one
            agent: foundation:zen-architect
            prompt: Do it.
        """
    )

    result = parse_manifest_text(legacy_yaml, source="old.yaml")

    assert isinstance(result, LegacyRecipe)
    assert result.is_legacy is True
    assert result.source == "old.yaml"
    assert "schema_version" in result.reason


def test_legacy_recipe_is_not_strictly_key_checked():
    """Legacy handling lives elsewhere; unknown keys are not this module's call."""
    result = parse_manifest({"name": "old", "output": "x", "metadata": {}})
    assert isinstance(result, LegacyRecipe)


def test_legacy_recipe_with_agent_config_is_not_rejected_here():
    """Core 12 applies under schema 2; legacy recipes are confined, not parsed here."""
    result = parse_manifest({"name": "old", "steps": [{"id": "s", "agent_config": {}}]})
    assert isinstance(result, LegacyRecipe)


# --- schema_version validation -------------------------------------------


@pytest.mark.parametrize("version", [1, 0, -1])
def test_unsupported_schema_version_errors(version):
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"schema_version": version, "dependencies": []})
    assert "schema_version" in str(excinfo.value)


def test_schema_version_above_two_is_reserved():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"schema_version": 3, "dependencies": []})
    message = str(excinfo.value)
    assert "Reserved" in message


@pytest.mark.parametrize("version", ["2", 2.0, True, None])
def test_non_integer_schema_version_errors(version):
    with pytest.raises(ManifestError, match="schema_version"):
        parse_manifest({"schema_version": version, "dependencies": []})


def test_missing_dependencies_block_errors():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"schema_version": 2, "name": "r"})
    message = str(excinfo.value)
    assert "dependencies" in message
    assert f"{CONTRACT} Core 1" in message


# --- Core 2: dependency shape --------------------------------------------


@pytest.mark.parametrize("kind", ["module", "recipe", "agent", "", None])
def test_dependency_kind_beyond_bundle_behavior_errors(kind):
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(
            {"schema_version": 2, "dependencies": [{"source": "pkg", "kind": kind}]}
        )
    message = str(excinfo.value)
    assert "'kind'" in message
    assert "'bundle'" in message and "'behavior'" in message


@pytest.mark.parametrize("bad_source", [None, "", "   ", 42, ["pkg"]])
def test_dependency_requires_non_empty_string_source(bad_source):
    entry: dict = {"kind": "bundle"}
    if bad_source is not None:
        entry["source"] = bad_source
    with pytest.raises(ManifestError, match="source"):
        parse_manifest({"schema_version": 2, "dependencies": [entry]})


def test_dependencies_must_be_a_list():
    with pytest.raises(ManifestError, match="'dependencies' must be a list"):
        parse_manifest({"schema_version": 2, "dependencies": {"source": "pkg"}})


def test_dependency_entry_must_be_a_mapping():
    with pytest.raises(ManifestError, match=r"dependencies\[0\]"):
        parse_manifest({"schema_version": 2, "dependencies": ["pkg"]})


def test_duplicate_dependency_source_errors():
    with pytest.raises(ManifestError, match="duplicate dependency source"):
        parse_manifest(
            {
                "schema_version": 2,
                "dependencies": [
                    {"source": "pkg", "kind": "bundle"},
                    {"source": "pkg", "kind": "behavior"},
                ],
            }
        )


@pytest.mark.parametrize("bad", ["foundation:zen", {"a": "b"}, [""], [None]])
def test_required_agents_must_be_a_list_of_names(bad):
    with pytest.raises(ManifestError, match="required_agents"):
        parse_manifest(
            {
                "schema_version": 2,
                "dependencies": [{"source": "pkg", "kind": "bundle", "required_agents": bad}],
            }
        )


# --- Core 3: agent alias map ---------------------------------------------


@pytest.mark.parametrize(
    "agents",
    [
        {"architect": "zen-architect"},
        {"architect": "foundation:zen:extra"},
        {"architect": 7},
        {"ns:alias": "foundation:zen-architect"},
        {"": "foundation:zen-architect"},
        ["architect"],
    ],
)
def test_malformed_agent_alias_map_errors(agents):
    with pytest.raises(ManifestError, match="agents|alias"):
        parse_manifest({"schema_version": 2, "dependencies": [], "agents": agents})


def test_agents_block_is_optional():
    manifest = parse_manifest({"schema_version": 2, "dependencies": []})
    assert dict(manifest.agents) == {}


# --- Core 9: manifest-declared capability needs --------------------------


def test_declared_capabilities_parse_in_declaration_order():
    manifest = parse_manifest({"schema_version": 2, "dependencies": [], "capabilities": ["net", "fs.read"]})

    assert isinstance(manifest, Manifest)
    assert manifest.capabilities == ("net", "fs.read")


def test_capabilities_survive_a_full_yaml_parse():
    """The key spelling is verified against the parser, not just the dataclass."""
    manifest = parse_manifest_text(
        textwrap.dedent(
            """
            schema_version: 2
            name: needs-net
            dependencies: []
            capabilities:
              - net
              - fs.read
            """
        ),
        source="recipe.yaml",
    )

    assert isinstance(manifest, Manifest)
    assert manifest.capabilities == ("net", "fs.read")


def test_absent_capabilities_means_the_recipe_declares_none():
    manifest = parse_manifest({"schema_version": 2, "dependencies": []})

    assert isinstance(manifest, Manifest)
    assert manifest.capabilities == ()


def test_empty_capabilities_list_is_identical_to_absent():
    """`capabilities: []` and no key at all both mean 'declares none'.

    An intersection cannot add, so both grant nothing. There is deliberately no
    manifest spelling for 'unconstrained'.
    """
    absent = parse_manifest({"schema_version": 2, "dependencies": []})
    explicit = parse_manifest({"schema_version": 2, "dependencies": [], "capabilities": []})

    assert isinstance(absent, Manifest)
    assert isinstance(explicit, Manifest)
    assert absent.capabilities == explicit.capabilities == ()


@pytest.mark.parametrize("bad", ["net", {"net": True}, 7, True])
def test_capabilities_must_be_a_list(bad):
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"schema_version": 2, "dependencies": [], "capabilities": bad})

    message = str(excinfo.value)
    assert "'capabilities'" in message
    assert f"{CONTRACT} Core 9" in message


@pytest.mark.parametrize("bad", [[""], ["  "], [None], [7], [True], [["net"]]])
def test_capability_entries_must_be_non_empty_strings(bad):
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"schema_version": 2, "dependencies": [], "capabilities": bad})

    message = str(excinfo.value)
    assert "capabilities[0]" in message
    assert f"{CONTRACT} Core 9" in message


def test_duplicate_capability_errors_naming_both_positions():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"schema_version": 2, "dependencies": [], "capabilities": ["net", "fs", "net"]})

    message = str(excinfo.value)
    assert "capabilities[2]" in message
    assert "capabilities[0]" in message
    assert "'net'" in message


def test_capabilities_without_schema_version_is_an_error_not_a_legacy_recipe():
    """`capabilities` is a MANIFEST key: declaring it bare cannot be ignored (Core 1)."""
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"name": "r", "capabilities": ["net"]})

    message = str(excinfo.value)
    assert "'capabilities'" in message
    assert "schema_version" in message
    assert f"{CONTRACT} Core 1" in message


def test_capabilities_is_not_a_dependency_key():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(
            {
                "schema_version": 2,
                "dependencies": [{"source": "pkg", "kind": "bundle", "capabilities": ["net"]}],
            }
        )

    message = str(excinfo.value)
    assert "'capabilities'" in message
    assert "dependencies[0]" in message


# --- top-level shape ------------------------------------------------------


@pytest.mark.parametrize("data", ["just a string", ["a", "b"], None, 7])
def test_non_mapping_recipe_errors(data):
    with pytest.raises(ManifestError, match="mapping"):
        parse_manifest(data)


def test_error_carries_clause_and_source_metadata():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest({"schema_version": 2, "dependencies": [], "bogus": 1}, source="r.yaml")

    err = excinfo.value
    assert err.clause == "Core 1"
    assert err.source == "r.yaml"
    assert str(err).startswith("r.yaml: ")
    assert isinstance(err, ValueError)
