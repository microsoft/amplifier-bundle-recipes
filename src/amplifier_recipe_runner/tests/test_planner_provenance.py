"""Per-agent provenance attribution (manifest.v1 Core 7).

The defect these tests pin: every agent in a plan used to be stamped with the
*declared* dependency that reached it -- URI, revision and digest -- even when
the agent's definition lives in a completely different source tree, pulled in
through that dependency's own ``includes``. Measured on a real migrated recipe,
26 of 39 agents were attributed to a tree they are not in, which makes the
Core 7 map non-discriminating: the field says the same thing whatever the
answer is.

The record now separates two facts that shared one field. ``supplied_by`` (with
``resolved_revision``, ``dependency_digest`` and ``defined_in``) names the tree
that DEFINES the agent; ``declared_by`` names the declared dependency it
arrived through, and ``via_includes`` is the path between them.

Everything here runs against LOCAL fixture bundles under ``fixtures/``: the
``umbrella`` bundle includes ``satellite``, so one declared dependency yields
one agent it defines itself (``umbrella:lead``) and one it merely reaches
(``satellite:helper``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import textwrap
from pathlib import Path
from types import MappingProxyType

import pytest

from amplifier_recipe_runner.manifest import Dependency
from amplifier_recipe_runner.manifest import parse_manifest_file
from amplifier_recipe_runner.planner import plan
from amplifier_recipe_runner.provenance import run_manifest_from_plan
from amplifier_recipe_runner.resolver import LocalBundleResolver
from amplifier_recipe_runner.resolver import ResolvedAgent
from amplifier_recipe_runner.resolver import ResolvedBundle
from amplifier_recipe_runner.resolver import SourceTree

FIXTURES = Path(__file__).parent / "fixtures"
UMBRELLA = FIXTURES / "umbrella"
SATELLITE = FIXTURES / "satellite"
ACME = FIXTURES / "acme"
WIDGET = FIXTURES / "widget"


class IncludingResolver:
    """Composes a local bundle's ``includes:``, the way Foundation does.

    ``LocalBundleResolver`` deliberately refuses includes rather than
    under-report a closure, so it cannot produce the shape this defect lives
    in. This double produces it faithfully: ONE resolved dependency (the
    declared source, with the declared source's own digest and local path)
    whose agent map contains agents whose definition files live in another
    tree entirely -- and, like the real resolver, a ``source_trees`` entry for
    each tree it composed, carrying that tree's own identity and the include
    path it arrived by.
    """

    async def resolve(self, dependency: Dependency, *, workspace: Path | None = None) -> ResolvedBundle:
        bundle = await LocalBundleResolver(allow_includes=True).resolve(dependency, workspace=workspace)
        agents = dict(bundle.agents)
        trees = dict(bundle.source_trees)
        for included in self._includes(Path(str(bundle.local_path))):
            sub = await LocalBundleResolver().resolve(
                Dependency(source=str(included), kind="bundle"), workspace=workspace
            )
            agents.update(sub.agents)
            trees[sub.namespace] = SourceTree(
                name=sub.namespace,
                local_path=sub.local_path,
                uri=str(included),
                resolved_revision=sub.resolved_revision,
                content_digest=sub.content_digest,
                via_includes=(sub.namespace,),
            )
        return dataclasses.replace(
            bundle,
            agents=MappingProxyType(agents),
            source_trees=MappingProxyType(trees),
        )

    @staticmethod
    def _includes(root: Path) -> list[Path]:
        import yaml

        text = (root / "bundle.md").read_text(encoding="utf-8").lstrip()
        frontmatter = text[3 : text.find("\n---", 3)]
        data = yaml.safe_load(frontmatter) or {}
        found: list[Path] = []
        for entry in data.get("includes") or ():
            target = entry.get("bundle") if isinstance(entry, dict) else entry
            if isinstance(target, str):
                found.append((root / target).resolve())
        return found


def write_recipe(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "recipe.yaml"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def planned(recipe_path: Path, resolver: object):
    manifest = parse_manifest_file(recipe_path)
    return asyncio.run(
        plan(manifest, resolver, recipe_path.parent)  # type: ignore[arg-type]
    )


TRANSITIVE_RECIPE = f"""
    schema_version: 2
    name: transitive
    dependencies:
      - source: "{UMBRELLA}"
        kind: bundle
    steps:
      - id: lead
        agent: "umbrella:lead"
        instruction: "Lead it"
      - id: help
        agent: "satellite:helper"
        instruction: "Help it"
"""


@pytest.fixture
def transitive_plan(tmp_path: Path):
    return planned(write_recipe(tmp_path, TRANSITIVE_RECIPE), IncludingResolver())


def test_directly_defined_agent_claims_nothing_extra(transitive_plan) -> None:
    """An agent the declared dependency itself defines records no include path."""
    lead = transitive_plan.agents["umbrella:lead"]

    # fixtures/umbrella/agents/lead.md really is inside fixtures/umbrella.
    assert lead.local_path == str(UMBRELLA / "agents" / "lead.md")
    assert lead.supplied_by == str(UMBRELLA)
    assert lead.declared_by == str(UMBRELLA)
    assert lead.defined_in == str(UMBRELLA)
    assert lead.via_includes == ()


def test_transitive_agent_records_the_tree_that_really_defines_it(transitive_plan) -> None:
    """Reached via the declared dependency's includes -- and says which tree."""
    helper = transitive_plan.agents["satellite:helper"]
    umbrella_dep = next(dep for dep in transitive_plan.dependencies if dep.uri == str(UMBRELLA))

    # fixtures/satellite/agents/helper.md is NOT inside fixtures/umbrella, so
    # the umbrella tree is not what defines it.
    assert helper.local_path == str(SATELLITE / "agents" / "helper.md")
    assert helper.supplied_by == str(SATELLITE)
    assert helper.defined_in == str(SATELLITE)
    # ...while the DECLARED dependency -- what the recipe asked for, and what a
    # resume re-resolves -- is still recorded, with the path between them.
    assert helper.declared_by == str(UMBRELLA)
    assert helper.via_includes == ("satellite",)
    # The identity stamped on it is the satellite tree's, not the umbrella
    # tree's: that is the whole point of the discrimination.
    assert helper.dependency_digest != umbrella_dep.content_digest


def test_no_agent_is_silently_misattributed(transitive_plan) -> None:
    """The discriminating assertion: every definition lies inside the tree the
    record claims defines it, and every claimed declaration was declared."""
    declared = {dep.uri for dep in transitive_plan.dependencies}

    misattributed = [
        name
        for name, prov in transitive_plan.agents.items()
        if prov.local_path
        and prov.defined_in
        and not Path(prov.local_path).resolve().is_relative_to(Path(prov.defined_in).resolve())
    ]
    assert misattributed == []
    assert all(prov.declared_by in declared for prov in transitive_plan.agents.values())

    # And the record really is discriminating -- not both-marked, not neither.
    assert {name: prov.supplied_by for name, prov in transitive_plan.agents.items()} == {
        "umbrella:lead": str(UMBRELLA),
        "satellite:helper": str(SATELLITE),
    }


def test_attribution_survives_the_run_manifest_round_trip(transitive_plan) -> None:
    """`plan --json` (and resume) carry the same honest attribution."""
    payload = run_manifest_from_plan(transitive_plan, run_id="r1").to_mapping()

    helper = payload["agents"]["satellite:helper"]
    assert helper["supplied_by"] == str(SATELLITE)
    assert helper["declared_by"] == str(UMBRELLA)
    assert helper["defined_in"] == str(SATELLITE)
    assert helper["via_includes"] == ["satellite"]

    lead = payload["agents"]["umbrella:lead"]
    assert lead["supplied_by"] == str(UMBRELLA)
    assert lead["declared_by"] == str(UMBRELLA)
    assert lead["via_includes"] == []


# --------------------------------------------------------------------------
# Two declared dependencies: attributed to the one that really holds it
# --------------------------------------------------------------------------


class BorrowingResolver:
    """Resolves ``acme`` to a bundle carrying an agent defined in ``widget``.

    The cross-dependency case: the agent arrives through acme's closure, but
    its definition file lives inside another DECLARED dependency's resolved
    tree. The name is acme's own, so nothing collides -- the only question is
    which dependency the plan stamps it with.
    """

    async def resolve(self, dependency: Dependency, *, workspace: Path | None = None) -> ResolvedBundle:
        bundle = await LocalBundleResolver().resolve(dependency, workspace=workspace)
        if Path(str(bundle.local_path)) != ACME:
            return bundle
        agents = dict(bundle.agents)
        agents["acme:packager"] = ResolvedAgent(
            name="acme:packager",
            local_path=str(WIDGET / "agents" / "packager.md"),
        )
        return dataclasses.replace(bundle, agents=MappingProxyType(agents))


def test_agent_is_attributed_to_the_declared_dependency_that_holds_it(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        name: two-deps
        dependencies:
          - source: "{ACME}"
            kind: bundle
          - source: "{WIDGET}"
            kind: bundle
        steps:
          - id: pack
            agent: "acme:packager"
            instruction: "Pack it"
        """,
    )

    result = planned(recipe, BorrowingResolver())
    packager = result.agents["acme:packager"]
    widget_dep = next(dep for dep in result.dependencies if dep.uri == str(WIDGET))

    # fixtures/widget/agents/packager.md is inside fixtures/widget, which is
    # itself declared -- so no include path is involved, only the question of
    # which declared tree gets stamped.
    assert packager.supplied_by == str(WIDGET)
    assert packager.defined_in == str(WIDGET)
    assert packager.dependency_digest == widget_dep.content_digest
    assert packager.via_includes == ()
    # It still entered the closure through acme, and the record says so.
    assert packager.declared_by == str(ACME)
    # acme's digest is NOT what got stamped.
    acme_dep = next(dep for dep in result.dependencies if dep.uri == str(ACME))
    assert packager.dependency_digest != acme_dep.content_digest


def test_unknown_tree_is_recorded_as_unknown_not_guessed(tmp_path: Path) -> None:
    """A definition no reported tree holds records no defining tree at all.

    The failure mode this forbids is the original one in miniature: filling
    the gap with the reaching dependency's tree, which reads as a claim the
    file is there.
    """

    class StrandedResolver:
        async def resolve(self, dependency: Dependency, *, workspace: Path | None = None) -> ResolvedBundle:
            bundle = await LocalBundleResolver().resolve(dependency, workspace=workspace)
            agents = dict(bundle.agents)
            agents["acme:stray"] = ResolvedAgent(
                name="acme:stray",
                local_path=str(tmp_path / "elsewhere" / "agents" / "stray.md"),
            )
            return dataclasses.replace(bundle, agents=MappingProxyType(agents))

    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        name: stranded
        dependencies:
          - source: "{ACME}"
            kind: bundle
        steps:
          - id: stray
            agent: "acme:stray"
            instruction: "Stray"
        """,
    )

    stray = planned(recipe, StrandedResolver()).agents["acme:stray"]

    assert stray.declared_by == str(ACME)
    assert stray.defined_in is None
    assert stray.via_includes == ()


# --------------------------------------------------------------------------
# The resolver half: which trees get reported, and on what evidence
# --------------------------------------------------------------------------


class _FakeState:
    """Stand-in for Foundation's ``BundleState`` -- only what is read."""

    def __init__(self, uri: str, local_path: str, includes: list[str] | None = None) -> None:
        self.uri = uri
        self.local_path = local_path
        self.includes = includes


class _FakeRegistry:
    """Stand-in for Foundation's ``BundleRegistry.get_state``."""

    def __init__(self, states: dict[str, _FakeState]) -> None:
        self._states = states

    def get_state(self, name: str | None = None):
        return dict(self._states) if name is None else self._states.get(name)


def _two_tree_layout(tmp_path: Path) -> tuple[Path, Path, dict[str, ResolvedAgent], dict[str, Path]]:
    """A hub bundle plus a leaf checkout it composes, with one agent each."""
    hub = tmp_path / "hub"
    leaf = tmp_path / "leaf"
    (hub / "agents").mkdir(parents=True)
    (leaf / "agents").mkdir(parents=True)
    (leaf / "behaviors").mkdir(parents=True)
    (hub / "bundle.md").write_text("---\nbundle:\n  name: hub\n---\n", encoding="utf-8")
    (leaf / "bundle.md").write_text("---\nbundle:\n  name: leaf\n---\n", encoding="utf-8")
    (hub / "agents" / "one.md").write_text("one", encoding="utf-8")
    (leaf / "agents" / "two.md").write_text("two", encoding="utf-8")

    agents = {
        "hub:one": ResolvedAgent(name="hub:one", local_path=str(hub / "agents" / "one.md")),
        "leaf:two": ResolvedAgent(name="leaf:two", local_path=str(leaf / "agents" / "two.md")),
        # No definition file: nothing to attribute, so no tree to report.
        "hub:ghost": ResolvedAgent(name="hub:ghost", local_path=None),
    }
    roots = {
        "hub": hub,
        "leaf": leaf,
        # A nested root that holds no agent file -- reporting it would cost a
        # subprocess to say nothing.
        "leaf-behavior": leaf / "behaviors",
    }
    return hub, leaf, agents, roots


def _compose(registry: _FakeRegistry, hub: Path, agents, roots):
    from amplifier_recipe_runner.resolver import _compose_source_trees

    return asyncio.run(
        _compose_source_trees(
            registry,
            root_name="hub",
            source_base_paths=roots,
            agents=agents,
            own_root=hub,
            own_uri="git+https://example.invalid/hub@v1",
            own_revision="aaa",
            own_digest="sha256:hub",
        )
    )


def test_composed_trees_are_named_with_their_own_identity_and_include_path(tmp_path: Path) -> None:
    hub, leaf, agents, roots = _two_tree_layout(tmp_path)
    registry = _FakeRegistry(
        {
            "hub": _FakeState("git+https://example.invalid/hub@v1", str(hub), ["leaf-behavior"]),
            "leaf-behavior": _FakeState(
                "git+https://example.invalid/leaf@main#subdirectory=behaviors/leaf.yaml",
                str(leaf / "behaviors" / "leaf.yaml"),
            ),
            "leaf": _FakeState("git+https://example.invalid/leaf@main", str(leaf)),
        }
    )

    trees = _compose(registry, hub, agents, roots)

    assert set(trees) == {"hub", "leaf"}
    assert trees["hub"].via_includes == ()
    assert trees["leaf"].uri == "git+https://example.invalid/leaf@main"
    assert trees["leaf"].local_path == str(leaf)
    assert trees["leaf"].content_digest is not None
    # foundation composed leaf's tree by including a behavior that lives in it.
    assert trees["leaf"].via_includes == ("leaf-behavior",)


def test_a_registry_entry_for_another_tree_is_not_borrowed(tmp_path: Path) -> None:
    """The registry outlives one closure, so a same-named entry may be someone
    else's. It is trusted only when its own path is the tree we matched."""
    hub, leaf, agents, roots = _two_tree_layout(tmp_path)
    registry = _FakeRegistry(
        {
            "hub": _FakeState("git+https://example.invalid/hub@v1", str(hub), ["leaf-behavior"]),
            "leaf-behavior": _FakeState("git+https://example.invalid/leaf@main", str(leaf / "behaviors")),
            # A stale entry from an unrelated earlier load.
            "leaf": _FakeState("git+https://example.invalid/SOMEONE-ELSE@main", str(tmp_path / "elsewhere")),
        }
    )

    trees = _compose(registry, hub, agents, roots)

    assert trees["leaf"].uri is None
    assert trees["leaf"].local_path == str(leaf)
