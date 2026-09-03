"""Conformance check over the recipes this bundle SHIPS.

The rule, in one sentence:

    A shipped recipe that references a namespaced agent must declare that
    agent in its own ``schema_version: 2`` dependency manifest.

Why this exists
---------------
A recipe with no ``schema_version`` runs ``legacy-caller-bound``: its
``agent:`` strings resolve from the *calling session's* agent map. That is
fine for a recipe that spawns nothing, and fatal for one that does -- it can
only run from a bundle that happens to carry the same agents. Live-verified
under ``-b anchors-amp-dev``, three shipped recipes died on their first agent
step with "Agent 'foundation:zen-architect' not found in configuration", and a
fourth silently degraded because its step set ``on_error: continue``
(recipes-l46, recipes-fho).

Declaring the closure makes the recipe portable: the runner resolves every
agent from the declared dependency, and an undeclared reference is refused at
preflight instead of at the first spawn.

Scope
-----
The *shipped, top-level* recipe surface -- ``examples/*.yaml``,
``templates/*.yaml``, ``recipes/*.yaml``. These are the files a user is
pointed at by name.

Subdirectories were deliberately NOT in scope for recipes-l46, and that
exclusion was never silent: :func:`test_unmigrated_subdirectory_recipes_are_pinned`
pins the exact set still on legacy, so one cannot be added or removed without
this test saying so. recipes-c6w then migrated 28 of the 29 that were pinned,
leaving only the entries whose reasons are spelled out on
``_KNOWN_UNMIGRATED_SUBDIR_RECIPES`` itself.

A migrated subdirectory recipe is held to the same parser-level standard as a
top-level one (:func:`test_migrated_subdirectory_recipe_parses_as_v2_manifest`).
That check is not redundant with the pin above: the pin reads
``dependencies:`` straight off the YAML, so a block that looks right to the eye
but the shipped parser rejects -- an unknown top-level key is the real,
observed case -- would satisfy the pin and still fail at run time. Seven
context-intelligence recipes carried exactly that defect (a dead top-level
``output:`` key, a STEP field the ``Recipe`` model has never had) and only the
real parser caught it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.runner_adapter import collect_agent_references
from amplifier_module_tool_recipes.runner_adapter import runner_available

# <bundle-root>/modules/tool-recipes/tests/this_file.py -> <bundle-root>
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Directories whose top-level ``*.yaml`` files are the shipped recipe surface.
_SHIPPED_DIRS = ("examples", "templates", "recipes")

#: Subdirectories under the shipped dirs that are out of scope here. Their
#: recipes are pinned by ``test_unmigrated_subdirectory_recipes_are_pinned``
#: rather than enforced, so the debt is visible instead of invisible.
_OUT_OF_SCOPE_SUBDIRS = (
    "examples/attractor",
    "examples/context-intelligence",
    "recipes/tests",
)


def _shipped_recipes() -> list[Path]:
    """Top-level ``*.yaml`` under each shipped dir, sorted, repo-relative."""
    found: list[Path] = []
    for name in _SHIPPED_DIRS:
        found.extend(sorted((_REPO_ROOT / name).glob("*.yaml")))
    return found


def _subdirectory_recipes() -> list[Path]:
    """Every ``*.yaml`` below a shipped dir but NOT at its top level."""
    found: list[Path] = []
    for name in _SHIPPED_DIRS:
        root = _REPO_ROOT / name
        found.extend(sorted(p for p in root.rglob("*.yaml") if p.parent != root))
    return found


def _rel(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _declared_agents(data: dict) -> set[str]:
    """Agents declared across every dependency's ``required_agents``.

    Read straight off the YAML so the check states the rule in the shipped
    file's own terms; :func:`test_shipped_recipes_parse_as_v2_manifests`
    separately proves the real parser agrees.
    """
    declared: set[str] = set()
    for dep in data.get("dependencies") or ():
        if isinstance(dep, dict):
            declared.update(dep.get("required_agents") or ())
    return declared


def _undeclared_agents(path: Path) -> set[str]:
    """Namespaced agents a recipe references but does not declare.

    Empty for a recipe that references no agents at all -- such a recipe is
    entitled to stay legacy, which is what keeps the bash-only controls
    (``examples/bash-step-example.yaml``) exempt without naming them.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    referenced = collect_agent_references(Recipe.from_yaml(path))
    if not referenced:
        return set()
    return referenced - _declared_agents(data)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe_path", _shipped_recipes(), ids=_rel)
def test_shipped_recipe_declares_every_agent_it_references(recipe_path: Path) -> None:
    """A shipped recipe never borrows an agent from its caller."""
    undeclared = _undeclared_agents(recipe_path)
    assert not undeclared, (
        f"{_rel(recipe_path)} references {sorted(undeclared)} but does not "
        f"declare them. A legacy recipe resolves `agent:` from the CALLING "
        f"session, so this file cannot run from a bundle that lacks those "
        f"agents. Add `schema_version: 2` and a `dependencies:` block listing "
        f"them in `required_agents` (see examples/multi-file-analysis.yaml)."
    )


@pytest.mark.parametrize("recipe_path", _shipped_recipes(), ids=_rel)
def test_shipped_recipe_parses_as_v2_manifest_when_it_spawns(
    recipe_path: Path,
) -> None:
    """The real parser -- not just the YAML shape -- accepts the declaration.

    Guards the failure mode where a ``dependencies:`` block looks right to the
    eye but the shipped parser rejects it (an unknown top-level key, a bad
    dependency entry), which would surface only at run time.
    """
    if not runner_available():
        pytest.skip("amplifier_recipe_runner not importable")

    from amplifier_recipe_runner.manifest import LegacyRecipe  # noqa: PLC0415
    from amplifier_recipe_runner.manifest import parse_manifest  # noqa: PLC0415

    data = yaml.safe_load(recipe_path.read_text(encoding="utf-8")) or {}
    referenced = collect_agent_references(Recipe.from_yaml(recipe_path))
    manifest = parse_manifest(data, source=_rel(recipe_path))

    if not referenced:
        # No agent steps: legacy is a legitimate, deliberate choice.
        return

    assert not isinstance(manifest, LegacyRecipe), (
        f"{_rel(recipe_path)} references {sorted(referenced)} but the shipped "
        f"manifest parser reads it as a legacy recipe: {manifest.reason}"
    )
    declared = {a for dep in manifest.dependencies for a in dep.required_agents}
    assert referenced <= declared, (
        f"{_rel(recipe_path)} references {sorted(referenced - declared)} which "
        f"the parsed manifest does not supply"
    )


# ---------------------------------------------------------------------------
# The check must actually bite
# ---------------------------------------------------------------------------


def test_check_catches_an_undeclared_agent(tmp_path: Path) -> None:
    """A recipe that references an undeclared agent is caught.

    Without this, the rule above could pass simply because
    :func:`collect_agent_references` returned nothing.
    """
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "dependencies": [
                    {
                        "source": "git+https://example.invalid/bundle@v1",
                        "kind": "bundle",
                        "required_agents": ["foundation:zen-architect"],
                    }
                ],
                "name": "bad",
                "description": "references an agent it never declared",
                "steps": [
                    {"id": "a", "agent": "foundation:zen-architect", "prompt": "hi"},
                    {"id": "b", "agent": "foundation:bug-hunter", "prompt": "hi"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert _undeclared_agents(bad) == {"foundation:bug-hunter"}


def test_check_exempts_a_recipe_with_no_agent_steps(tmp_path: Path) -> None:
    """A bash-only recipe is not required to declare anything."""
    ok = tmp_path / "bash-only.yaml"
    ok.write_text(
        yaml.safe_dump(
            {
                "name": "bash-only",
                "description": "no agent steps at all",
                "steps": [{"id": "a", "type": "bash", "command": "echo hi"}],
            }
        ),
        encoding="utf-8",
    )
    assert _undeclared_agents(ok) == set()


def test_shipped_surface_is_not_empty() -> None:
    """A glob that silently matched nothing would make every check vacuous."""
    shipped = _shipped_recipes()
    assert len(shipped) >= 20, f"only found {len(shipped)} shipped recipes"
    assert any(p.name == "bash-step-example.yaml" for p in shipped)
    assert any(p.name == "generate-recipe-docs.yaml" for p in shipped)


# ---------------------------------------------------------------------------
# Visible debt: subdirectory recipes still on legacy
# ---------------------------------------------------------------------------

#: Recipes below a shipped dir that still reference agents without declaring
#: them. Pinned as an exact set so drift in EITHER direction is reported: a
#: new legacy recipe fails here, and so does a migration that forgets to
#: shrink this list.
#:
#: recipes-c6w migrated the 28 that could be migrated -- ``examples/attractor``
#: (2) and ``examples/context-intelligence`` (26) -- each verified against the
#: shipped planner with a real ``FoundationResolver`` and no caller agents.
#: What remains is deliberate, and each entry states why. This is the whole
#: list, not a sample: an entry with no reason below is a bug in this comment.
#:
#: ``recipes/tests/fixtures/*.yaml`` -- validator FIXTURES, not shipped
#:     recipes. They exist to be fed to ``validate_recipe`` and produce a known
#:     verdict; ``broken-recipe.yaml`` is required to be invalid. They are
#:     never executed and are skipped by ``validate-recipes``' own Phase 1
#:     discovery (``recipes/tests/test_phase1_recipe_discovery.py`` asserts
#:     that skip). Migrating them would change what they test.
#:
#: ``examples/context-intelligence/verification/adversarial-verification.yaml``
#:     -- blocked on a real gap, not an oversight. Six of its steps use
#:     ``agent: "self"``. ``self`` is a documented pseudo-agent of the legacy
#:     engine (``validator.py`` names it; ``collect_agent_references`` excludes
#:     it) but the recipe-runner library has no notion of it anywhere, so
#:     planning the file as v2 fails preflight with ``UndeclaredAgentError:
#:     Agent 'self' referenced by step 'validate_inputs' is not supplied by any
#:     declared dependency`` -- and no ``dependencies:`` block can satisfy it,
#:     because ``self`` names the CURRENT agent rather than a bundle-supplied
#:     one. What ``self`` should mean inside a closed world, where the recipe
#:     owns its session and the calling agent is deliberately out of reach, is
#:     a decision for the manifest contract; a migration may not invent it.
#:     Tracked as recipes-80q.
#:     The file's stale ``lsp-python:python-code-intel`` references WERE
#:     corrected to ``python-dev:code-intel`` (recipes-c6w), which is right
#:     whatever schema version it ends up on.
_KNOWN_UNMIGRATED_SUBDIR_RECIPES = frozenset(
    {
        "examples/context-intelligence/verification/adversarial-verification.yaml",
        "recipes/tests/fixtures/broken-recipe.yaml",
        "recipes/tests/fixtures/valid-recipe.yaml",
        "recipes/tests/fixtures/warnings-recipe.yaml",
    }
)


def test_out_of_scope_subdirs_still_exist() -> None:
    """A renamed directory must not turn the exclusion into a silent pass."""
    for rel in _OUT_OF_SCOPE_SUBDIRS:
        assert (_REPO_ROOT / rel).is_dir(), f"{rel} no longer exists"


def _migrated_subdirectory_recipes() -> list[Path]:
    """Subdirectory recipes that are NOT on the legacy pin list."""
    return [p for p in _subdirectory_recipes() if _rel(p) not in _KNOWN_UNMIGRATED_SUBDIR_RECIPES]


@pytest.mark.parametrize("recipe_path", _migrated_subdirectory_recipes(), ids=_rel)
def test_migrated_subdirectory_recipe_parses_as_v2_manifest(recipe_path: Path) -> None:
    """A migrated subdirectory recipe satisfies the REAL parser, not the eye.

    ``test_unmigrated_subdirectory_recipes_are_pinned`` reads ``dependencies:``
    straight off the YAML, which cannot see a manifest the shipped parser
    refuses. That gap is not hypothetical: seven of these files carried a
    top-level ``output:`` key -- a STEP field that ``Recipe`` has never had at
    the top level, so nothing ever read it -- and the parser rejected every one
    of them as an unknown key while the YAML-level check was perfectly happy.
    """
    if not runner_available():
        pytest.skip("amplifier_recipe_runner not importable")

    from amplifier_recipe_runner.manifest import LegacyRecipe  # noqa: PLC0415
    from amplifier_recipe_runner.manifest import parse_manifest  # noqa: PLC0415

    data = yaml.safe_load(recipe_path.read_text(encoding="utf-8")) or {}
    referenced = collect_agent_references(Recipe.from_yaml(recipe_path))
    if not referenced:
        # No agent steps: legacy is a legitimate, deliberate choice.
        return

    manifest = parse_manifest(data, source=_rel(recipe_path))
    assert not isinstance(manifest, LegacyRecipe), (
        f"{_rel(recipe_path)} references {sorted(referenced)} but the shipped "
        f"manifest parser reads it as a legacy recipe: {manifest.reason}"
    )
    declared = {a for dep in manifest.dependencies for a in dep.required_agents}
    assert referenced <= declared, (
        f"{_rel(recipe_path)} references {sorted(referenced - declared)} which "
        f"the parsed manifest does not supply"
    )


def test_the_migrated_subdirectory_surface_is_not_empty() -> None:
    """Guards the check above against becoming vacuous if the glob breaks."""
    migrated = _migrated_subdirectory_recipes()
    assert len(migrated) >= 28, f"only found {len(migrated)} migrated subdir recipes"


def test_unmigrated_subdirectory_recipes_are_pinned() -> None:
    """Debt below the shipped dirs is pinned, not silently accumulating."""
    actual = {
        _rel(p)
        for p in _subdirectory_recipes()
        if _undeclared_agents(p)
    }
    new = actual - _KNOWN_UNMIGRATED_SUBDIR_RECIPES
    fixed = _KNOWN_UNMIGRATED_SUBDIR_RECIPES - actual
    assert not new, (
        f"new subdirectory recipe(s) reference agents without declaring them: "
        f"{sorted(new)}. Declare the closure, or add them to "
        f"_KNOWN_UNMIGRATED_SUBDIR_RECIPES with a reason."
    )
    assert not fixed, (
        f"these are now migrated -- remove them from "
        f"_KNOWN_UNMIGRATED_SUBDIR_RECIPES: {sorted(fixed)}"
    )
