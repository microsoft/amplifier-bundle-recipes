"""Per-agent provenance attribution (manifest.v1 Core 7).

The defect these tests pin: every agent in a plan used to be stamped with the
*declared* dependency that reached it -- URI, revision and digest -- even when
the agent's definition lives in a completely different source tree, pulled in
through that dependency's own ``includes``. Measured on a real migrated recipe,
26 of 39 agents were attributed to a tree they are not in, which makes the
Core 7 map non-discriminating: the field says the same thing whatever the
answer is.

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
from amplifier_recipe_runner.resolver import LocalBundleResolver
from amplifier_recipe_runner.resolver import ResolvedAgent
from amplifier_recipe_runner.resolver import ResolvedBundle
from amplifier_recipe_runner.provenance import run_manifest_from_plan

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
    tree entirely.
    """

    async def resolve(self, dependency: Dependency, *, workspace: Path | None = None) -> ResolvedBundle:
        bundle = await LocalBundleResolver(allow_includes=True).resolve(dependency, workspace=workspace)
        agents = dict(bundle.agents)
        for included in self._includes(Path(str(bundle.local_path))):
            sub = await LocalBundleResolver().resolve(
                Dependency(source=str(included), kind="bundle"), workspace=workspace
            )
            agents.update(sub.agents)
        return dataclasses.replace(bundle, agents=MappingProxyType(agents))

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
    """An agent the declared dependency itself defines carries no `via` marks."""
    lead = transitive_plan.agents["umbrella:lead"]

    assert lead.supplied_by == str(UMBRELLA)
    assert lead.local_path == str(UMBRELLA / "agents" / "lead.md")
    assert lead.via_includes is False
    assert lead.defined_in is None


def test_transitive_agent_records_the_tree_that_really_defines_it(transitive_plan) -> None:
    """Reached via the declared dependency's includes -- and says so."""
    helper = transitive_plan.agents["satellite:helper"]

    # supplied_by stays the DECLARED dependency: that is what the recipe asked
    # for, and what a resume re-resolves.
    assert helper.supplied_by == str(UMBRELLA)
    # ...but the attribution is no longer a bare claim about the umbrella tree.
    assert helper.via_includes is True
    assert helper.defined_in == str(SATELLITE / "agents" / "helper.md")


def test_no_agent_is_silently_misattributed(transitive_plan) -> None:
    """The discriminating assertion: a definition outside the claimed tree
    without ``via_includes`` is a mis-attribution, and there are none."""
    trees = {dep.uri: dep.local_path for dep in transitive_plan.dependencies}

    silent = [
        name
        for name, prov in transitive_plan.agents.items()
        if prov.local_path
        and not Path(prov.local_path).resolve().is_relative_to(Path(trees[prov.supplied_by]).resolve())
        and not prov.via_includes
    ]

    assert silent == []
    # And the pair really is discriminating -- not both-marked, not neither.
    marks = {name: prov.via_includes for name, prov in transitive_plan.agents.items()}
    assert marks == {"umbrella:lead": False, "satellite:helper": True}


def test_attribution_survives_the_run_manifest_round_trip(transitive_plan) -> None:
    """`plan --json` (and resume) carry the same honest attribution."""
    payload = run_manifest_from_plan(transitive_plan, run_id="r1").to_mapping()

    assert payload["agents"]["satellite:helper"]["via_includes"] is True
    assert payload["agents"]["satellite:helper"]["defined_in"] == str(
        SATELLITE / "agents" / "helper.md"
    )
    assert payload["agents"]["umbrella:lead"]["via_includes"] is False
    assert payload["agents"]["umbrella:lead"]["defined_in"] is None


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

    assert packager.supplied_by == str(WIDGET)
    assert packager.dependency_digest == widget_dep.content_digest
    assert packager.via_includes is False
    assert packager.defined_in is None
    # acme's digest is NOT what got stamped.
    acme_dep = next(dep for dep in result.dependencies if dep.uri == str(ACME))
    assert packager.dependency_digest != acme_dep.content_digest
