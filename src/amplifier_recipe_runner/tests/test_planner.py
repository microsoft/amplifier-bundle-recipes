"""Tests for dependency planning (manifest.v1 Core 3, 5, 6, 7; lib.v1 Core 5, 7).

Everything here runs against LOCAL fixture bundles under ``fixtures/``: no
network, no git clone, no Foundation required. The one test that does exercise
the real Foundation-backed resolver is skipped when ``amplifier_foundation``
is not importable, so the suite proves the planner rather than the install.

The discriminating pairs the contract names are all present:

* GOOD -- a fully declared closure reports every agent's supplying dependency,
  with local path and revision/digest, executing nothing.
* GOOD -- a behavior-partial dependency composes only its declared contribution.
* BAD -- an undeclared agent reference fails preflight, naming the reference
  and the remedy.
* BAD -- two dependencies supplying the same agent name fail preflight, naming
  both sources; the collision is never resolved by precedence.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import re
import textwrap
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from amplifier_recipe_runner.api import LockMode
from amplifier_recipe_runner.errors import AgentCollisionError
from amplifier_recipe_runner.errors import LegacyRecipeError
from amplifier_recipe_runner.errors import TrustRefusedError
from amplifier_recipe_runner.errors import UndeclaredAgentError
from amplifier_recipe_runner.manifest import Dependency
from amplifier_recipe_runner.manifest import parse_manifest_file
from amplifier_recipe_runner.planner import plan
from amplifier_recipe_runner.resolver import DependencyResolutionError
from amplifier_recipe_runner.resolver import LocalBundleResolver
from amplifier_recipe_runner.resolver import ResolvedAgent
from amplifier_recipe_runner.resolver import ResolvedBundle
from amplifier_recipe_runner.resolver import canonical_agent_name
from amplifier_recipe_runner.resolver import split_source
from amplifier_recipe_runner.trust import TrustPolicy as RealTrustPolicy

FIXTURES = Path(__file__).parent / "fixtures"
ACME = FIXTURES / "acme"
WIDGET = FIXTURES / "widget"
CLASH = FIXTURES / "clash"
COMPOSED = FIXTURES / "composed"
BEHAVIOR = f"{ACME}#subdirectory=behaviors/review-only.yaml"

SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def write_recipe(tmp_path: Path, body: str, *, name: str = "recipe.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def planned(
    recipe_path: Path,
    *,
    resolver: object | None = None,
    workspace: Path | None = None,
    **kwargs: object,
):
    """Parse ``recipe_path`` and plan it. Returns the ExecutionPlan."""
    manifest = parse_manifest_file(recipe_path)
    return asyncio.run(
        plan(
            manifest,  # type: ignore[arg-type]
            resolver or LocalBundleResolver(),  # type: ignore[arg-type]
            workspace or recipe_path.parent,
            **kwargs,  # type: ignore[arg-type]
        )
    )


class RecordingResolver:
    """Wraps a real resolver and records exactly what it was asked for.

    Proves manifest Core 11 (no namespace inference): the planner asks for the
    declared sources and nothing else.
    """

    def __init__(self, inner: object | None = None) -> None:
        self.inner = inner or LocalBundleResolver()
        self.calls: list[str] = []

    async def resolve(self, dependency: Dependency, *, workspace: Path | None = None) -> ResolvedBundle:
        self.calls.append(dependency.source)
        return await self.inner.resolve(dependency, workspace=workspace)  # type: ignore[attr-defined]


class FakeGitResolver:
    """Resolver double for a git source, which a local fixture cannot be."""

    REVISION = "0" * 39 + "a"

    async def resolve(self, dependency: Dependency, *, workspace: Path | None = None) -> ResolvedBundle:
        _, subdirectory, ref = split_source(dependency.source)
        return ResolvedBundle(
            source=dependency.source,
            kind=dependency.kind,
            namespace="remote",
            agents=MappingProxyType(
                {
                    "remote:builder": ResolvedAgent(
                        name="remote:builder",
                        local_path="/cache/remote/agents/builder.md",
                    )
                }
            ),
            local_path="/cache/remote",
            resolved_revision=self.REVISION,
            content_digest=None,
            requested_ref=ref,
            subdirectory=subdirectory,
            version="2.0.0",
        )


class RefusingPolicy:
    """Trust policy double that refuses everything, loudly."""

    name = "refuse-all"

    def __init__(self) -> None:
        self.checked: list[str] = []

    def check_source(self, source: str, *, locked_ref: str | None = None) -> None:
        self.checked.append(source)
        raise TrustRefusedError(source, reason="fixture policy refuses everything", policy=self.name)


class PermissivePolicy:
    name = "allow-all"

    def __init__(self) -> None:
        self.checked: list[str] = []

    def check_source(self, source: str, *, locked_ref: str | None = None) -> None:
        self.checked.append(source)


# --------------------------------------------------------------------------
# Core 3 + Core 7 -- closure and provenance
# --------------------------------------------------------------------------


def test_plan_reports_closure_with_provenance(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        name: review-and-package
        dependencies:
          - source: {ACME}
            kind: bundle
            required_agents: [acme:reviewer]
          - source: {WIDGET}
            kind: bundle
        agents:
          packager: widget:packager
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: "review it"
          - id: package
            agent: "packager"
            prompt: "package it"
        """,
    )

    result = planned(recipe)

    assert result.schema_version == 2
    assert SHA256.match(result.recipe_digest)
    assert result.step_ids == ("review", "package")
    assert result.policy is not None
    assert result.policy.lock_mode is LockMode.LOCKED
    assert result.policy.isolated is True
    assert result.manifest_version == 1
    assert result.runner_version

    # One record per declared dependency, in declaration order.
    assert [d.uri for d in result.dependencies] == [str(ACME), str(WIDGET)]
    acme_dep, widget_dep = result.dependencies
    assert acme_dep.kind.value == "bundle"
    assert acme_dep.namespace == "acme"
    assert acme_dep.version == "1.2.0"
    assert acme_dep.local_path == str(ACME)
    assert SHA256.match(acme_dep.content_digest or "")
    assert acme_dep.required_agents == ("acme:reviewer",)
    assert widget_dep.version == "0.4.1"

    # Closed-world catalog: exactly the declared dependencies' agents,
    # plus the alias a step actually used.
    assert set(result.agents) == {
        "acme:reviewer",
        "acme:summarizer",
        "widget:packager",
        "widget:reviewer",
        "packager",
    }

    reviewer = result.agents["acme:reviewer"]
    assert reviewer.supplied_by == str(ACME)
    assert reviewer.alias is None
    assert reviewer.local_path == str(ACME / "agents" / "reviewer.md")
    assert reviewer.dependency_digest == acme_dep.content_digest

    alias = result.agents["packager"]
    assert alias.agent == "widget:packager"
    assert alias.alias == "packager"
    assert alias.supplied_by == str(WIDGET)


def test_local_source_records_digest_and_optional_revision(tmp_path: Path) -> None:
    """Core 7: a local source records a content digest; a git checkout may also
    record a revision, and never fabricates one."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    dep = planned(recipe).dependencies[0]
    assert SHA256.match(dep.content_digest or "")
    assert dep.resolved_revision is None or GIT_SHA.match(dep.resolved_revision)


def test_git_source_records_resolved_revision(tmp_path: Path) -> None:
    """Core 7: where the source is a git URI, the immutable revision is recorded
    per dependency AND on every agent it supplies."""
    recipe = write_recipe(
        tmp_path,
        """
        schema_version: 2
        dependencies:
          - source: git+https://example.invalid/org/remote@main
            kind: bundle
        steps:
          - id: build
            agent: "remote:builder"
            prompt: p
        """,
    )

    result = planned(recipe, resolver=FakeGitResolver())

    dep = result.dependencies[0]
    assert dep.requested_ref == "main"
    assert dep.resolved_revision == FakeGitResolver.REVISION
    builder = result.agents["remote:builder"]
    assert builder.resolved_revision == FakeGitResolver.REVISION
    assert builder.local_path == "/cache/remote/agents/builder.md"


def test_catalog_contains_only_declared_dependencies(tmp_path: Path) -> None:
    """Core 3 + Core 11: only declared sources are resolved, and only their
    agents appear -- no inference from an agent name's namespace."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    resolver = RecordingResolver()
    result = planned(recipe, resolver=resolver)

    assert resolver.calls == [str(ACME)]
    assert set(result.agents) == {"acme:reviewer", "acme:summarizer"}
    assert not any(name.startswith("widget:") for name in result.agents)


def test_behavior_partial_composes_only_its_declared_contribution(tmp_path: Path) -> None:
    """GOOD fixture: a behavior partial contributes only what it declares --
    the sibling agent in the same bundle stays out of the closure."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {BEHAVIOR}
            kind: behavior
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    result = planned(recipe)

    assert set(result.agents) == {"acme:reviewer"}
    assert "acme:summarizer" not in result.agents

    dep = result.dependencies[0]
    assert dep.kind.value == "behavior"
    assert dep.subdirectory == "behaviors/review-only.yaml"
    # namespace_root: the partial lives in behaviors/, its agents live one up.
    assert dep.local_path == str(ACME)
    assert result.agents["acme:reviewer"].local_path == str(ACME / "agents" / "reviewer.md")


# --------------------------------------------------------------------------
# Core 5 -- collision
# --------------------------------------------------------------------------


def test_two_dependencies_supplying_same_agent_is_a_preflight_error(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
          - source: {CLASH}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    with pytest.raises(AgentCollisionError) as excinfo:
        planned(recipe)

    error = excinfo.value
    assert error.agent == "acme:reviewer"
    assert set(error.sources) == {str(ACME), str(CLASH)}
    # Names BOTH sources, and never resolves by precedence.
    assert str(ACME) in str(error)
    assert str(CLASH) in str(error)
    assert "precedence" in str(error).lower()


def test_ambiguous_bare_reference_is_a_collision(tmp_path: Path) -> None:
    """Two namespaces both supplying 'reviewer' is legal until a step asks for
    the bare name -- which is then ambiguous, and never guessed."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
          - source: {WIDGET}
            kind: bundle
        steps:
          - id: review
            agent: "reviewer"
            prompt: p
        """,
    )

    with pytest.raises(AgentCollisionError) as excinfo:
        planned(recipe)

    assert excinfo.value.agent == "reviewer"
    assert set(excinfo.value.sources) == {str(ACME), str(WIDGET)}
    assert "acme:reviewer" in str(excinfo.value)
    assert "widget:reviewer" in str(excinfo.value)


def test_distinct_namespaces_sharing_a_bare_name_plan_cleanly(tmp_path: Path) -> None:
    """The same two dependencies plan fine when references are canonical:
    distinct namespaces do not override each other."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
          - source: {WIDGET}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
          - id: review-pkg
            agent: "widget:reviewer"
            prompt: p
        """,
    )

    result = planned(recipe)
    assert result.agents["acme:reviewer"].supplied_by == str(ACME)
    assert result.agents["widget:reviewer"].supplied_by == str(WIDGET)


# --------------------------------------------------------------------------
# Core 6 -- undeclared references
# --------------------------------------------------------------------------


def test_undeclared_agent_reference_fails_naming_reference_and_remedy(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: architect
            agent: "foundation:zen-architect"
            prompt: p
        """,
    )

    with pytest.raises(UndeclaredAgentError) as excinfo:
        planned(recipe)

    error = excinfo.value
    assert error.agent == "foundation:zen-architect"
    assert error.step_id == "architect"
    assert "acme:reviewer" in error.declared_agents
    message = str(error)
    assert "foundation:zen-architect" in message
    assert "dependencies" in message  # the remedy names where to declare it


def test_undeclared_reference_inside_a_nested_loop_body_is_caught(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: outer
            foreach: "{{{{ items }}}}"
            while_steps:
              - id: inner
                agent: "widget:packager"
                prompt: p
        """,
    )

    with pytest.raises(UndeclaredAgentError) as excinfo:
        planned(recipe)

    assert excinfo.value.agent == "widget:packager"
    assert excinfo.value.step_id == "inner"


def test_alias_pointing_outside_the_closure_fails(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        agents:
          packager: widget:packager
        steps:
          - id: package
            agent: "packager"
            prompt: p
        """,
    )

    with pytest.raises(UndeclaredAgentError) as excinfo:
        planned(recipe)

    assert excinfo.value.agent == "packager"
    assert "widget:packager" in str(excinfo.value)


def test_required_agent_not_supplied_by_its_dependency_fails(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
            required_agents: [acme:nonexistent]
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    with pytest.raises(UndeclaredAgentError) as excinfo:
        planned(recipe)

    assert excinfo.value.agent == "acme:nonexistent"
    assert "required_agents" in str(excinfo.value)


def test_templated_agent_reference_is_not_resolved_at_plan_time(tmp_path: Path) -> None:
    """A ``{{ }}`` reference has no value yet; planning neither resolves nor
    fabricates one."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: dynamic
            agent: "{{{{ chosen_agent }}}}"
            prompt: p
        """,
    )

    result = planned(recipe)
    assert result.step_ids == ("dynamic",)
    assert set(result.agents) == {"acme:reviewer", "acme:summarizer"}


def test_staged_recipe_steps_are_walked(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        stages:
          - name: first
            steps:
              - id: review
                agent: "acme:reviewer"
                prompt: p
          - name: second
            steps:
              - id: summarize
                agent: "acme:summarizer"
                prompt: p
        """,
    )

    result = planned(recipe)
    assert result.step_ids == ("review", "summarize")


# --------------------------------------------------------------------------
# Planning has no side effects; the trust hook precedes resolution
# --------------------------------------------------------------------------


def test_plan_executes_nothing_and_writes_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    result = planned(recipe, workspace=workspace)

    assert list(workspace.iterdir()) == []
    assert result.agents  # a real plan, not an empty one


def test_trust_policy_is_consulted_before_the_resolver(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    resolver = RecordingResolver()
    policy = RefusingPolicy()

    with pytest.raises(TrustRefusedError):
        planned(recipe, resolver=resolver, trust_policy=policy)

    assert policy.checked == [str(ACME)]
    assert resolver.calls == [], "refusal must precede any fetch"


def test_permissive_policy_is_recorded_in_effective_policy(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    policy = PermissivePolicy()
    result = planned(recipe, trust_policy=policy, lock_mode=LockMode.UNLOCKED)

    assert policy.checked == [str(ACME)]
    assert result.policy is not None
    assert result.policy.trust_policy == "allow-all"
    assert result.policy.lock_mode is LockMode.UNLOCKED


# --------------------------------------------------------------------------
# Core 9 -- effective capabilities are the three-way intersection
# --------------------------------------------------------------------------


def capability_recipe(tmp_path: Path, declared: str) -> Path:
    """A minimal planned recipe declaring ``capabilities: <declared>``."""
    return write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        capabilities: {declared}
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )


def test_plan_capabilities_are_the_three_way_intersection(tmp_path: Path) -> None:
    """host ∩ runner ∩ manifest -- the manifest term now has a real source."""
    recipe = capability_recipe(tmp_path, "[net, fs, exec]")
    policy = RealTrustPolicy.interactive(capability_allowlist=("net", "fs"))

    result = planned(recipe, trust_policy=policy, host_capabilities=("fs", "exec"))

    assert result.policy is not None
    assert result.policy.capabilities == ("fs",)


def test_a_manifest_cannot_widen_what_the_policies_allow(tmp_path: Path) -> None:
    recipe = capability_recipe(tmp_path, "[net, exec]")
    policy = RealTrustPolicy.interactive(capability_allowlist=("net", "exec"))

    result = planned(recipe, trust_policy=policy, host_capabilities=("fs",))

    assert result.policy is not None
    assert result.policy.capabilities == ()


def test_a_recipe_declaring_no_capabilities_is_granted_none(tmp_path: Path) -> None:
    """Both open policies still grant nothing: an intersection cannot add."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    result = planned(recipe, trust_policy=RealTrustPolicy.interactive(), host_capabilities=None)

    assert result.policy is not None
    assert result.policy.capabilities == ()


def test_empty_declaration_and_no_declaration_plan_identically(tmp_path: Path) -> None:
    policy = RealTrustPolicy.interactive(capability_allowlist=("net",))

    declared = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        capabilities: []
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
        name="declared.yaml",
    )
    absent = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
        name="absent.yaml",
    )

    declared_plan = planned(declared, trust_policy=policy, host_capabilities=("net",))
    absent_plan = planned(absent, trust_policy=policy, host_capabilities=("net",))

    assert declared_plan.policy is not None
    assert absent_plan.policy is not None
    assert declared_plan.policy.capabilities == absent_plan.policy.capabilities == ()


def test_a_policy_carrying_no_allowlist_is_unconstrained_not_empty(tmp_path: Path) -> None:
    """The TrustPolicy protocol names only `name`/`check_source`.

    A minimal conforming policy must therefore mean "the runner imposes no
    capability constraint" -- reading its absent allowlist as an empty set
    would silently strip every capability the recipe declared.
    """
    recipe = capability_recipe(tmp_path, "[net, fs]")

    result = planned(recipe, trust_policy=PermissivePolicy(), host_capabilities=None)

    assert result.policy is not None
    assert result.policy.capabilities == ("fs", "net")


def test_a_host_permitting_nothing_differs_from_no_host_constraint(tmp_path: Path) -> None:
    recipe = capability_recipe(tmp_path, "[net]")
    policy = RealTrustPolicy.interactive(capability_allowlist=("net",))

    unconstrained = planned(recipe, trust_policy=policy, host_capabilities=None)
    closed = planned(recipe, trust_policy=policy, host_capabilities=())

    assert unconstrained.policy is not None and closed.policy is not None
    assert unconstrained.policy.capabilities == ("net",)
    assert closed.policy.capabilities == ()


def test_capabilities_are_granted_with_no_trust_policy_at_all(tmp_path: Path) -> None:
    recipe = capability_recipe(tmp_path, "[net]")

    result = planned(recipe)

    assert result.policy is not None
    assert result.policy.capabilities == ("net",)


def test_legacy_recipe_is_refused_by_the_planner(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        """
        name: legacy
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    with pytest.raises(LegacyRecipeError):
        planned(recipe)


def test_explicit_recipe_body_overrides_manifest_source(tmp_path: Path) -> None:
    """``plan`` accepts an already-loaded body, so a host that has parsed the
    recipe once does not have to hand back a path."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps: []
        """,
    )
    manifest = parse_manifest_file(recipe)
    body: Mapping[str, object] = {
        "steps": [{"id": "review", "agent": "acme:reviewer", "prompt": "p"}]
    }

    result = asyncio.run(
        plan(manifest, LocalBundleResolver(), tmp_path, recipe=body)  # type: ignore[arg-type]
    )

    assert result.step_ids == ("review",)
    assert SHA256.match(result.recipe_digest)


# --------------------------------------------------------------------------
# Resolver behaviour
# --------------------------------------------------------------------------


def test_local_resolver_refuses_a_bundle_it_cannot_compose() -> None:
    """Under-reporting a closure would silently weaken every check above, so
    an offline resolver refuses a bundle with includes instead."""
    resolver = LocalBundleResolver()
    dependency = Dependency(source=str(COMPOSED), kind="bundle")

    with pytest.raises(DependencyResolutionError) as excinfo:
        asyncio.run(resolver.resolve(dependency))

    assert "include" in str(excinfo.value)
    assert "FoundationResolver" in str(excinfo.value)


def test_local_resolver_refuses_a_remote_source() -> None:
    resolver = LocalBundleResolver()
    dependency = Dependency(source="git+https://example.invalid/org/repo@main", kind="bundle")

    with pytest.raises(DependencyResolutionError) as excinfo:
        asyncio.run(resolver.resolve(dependency))

    assert "offline" in str(excinfo.value)


def test_local_resolver_names_a_missing_path() -> None:
    resolver = LocalBundleResolver()
    dependency = Dependency(source=str(FIXTURES / "does-not-exist"), kind="bundle")

    with pytest.raises(DependencyResolutionError) as excinfo:
        asyncio.run(resolver.resolve(dependency))

    assert "does-not-exist" in str(excinfo.value)


def test_split_source_separates_subdirectory_and_ref() -> None:
    base, subdirectory, ref = split_source("git+https://host/org/repo@v1.2#subdirectory=behaviors/x.yaml")
    assert base == "git+https://host/org/repo@v1.2"
    assert subdirectory == "behaviors/x.yaml"
    assert ref == "v1.2"

    assert split_source("/tmp/bundle") == ("/tmp/bundle", None, None)


def test_canonical_agent_name_only_prefixes_bare_names() -> None:
    assert canonical_agent_name("reviewer", "acme") == "acme:reviewer"
    assert canonical_agent_name("other:reviewer", "acme") == "other:reviewer"


def test_resolved_bundle_is_immutable() -> None:
    bundle = ResolvedBundle(source="s", kind="bundle", namespace="n")
    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.source = "other"  # type: ignore[misc]
    assert replace(bundle, namespace="m").namespace == "m"


@pytest.mark.skipif(
    importlib.util.find_spec("amplifier_foundation") is None,
    reason="amplifier-foundation is not installed; the default resolver needs it",
)
def test_foundation_resolver_reads_a_local_fixture_bundle(tmp_path: Path) -> None:
    """The default resolver against the real BundleRegistry, offline.

    Skipped when Foundation is not installed -- the planner's own behaviour is
    covered above without it.
    """
    from amplifier_recipe_runner.resolver import FoundationResolver

    resolver = FoundationResolver(home=tmp_path / "runner-home")

    bundle = asyncio.run(resolver.resolve(Dependency(source=str(ACME), kind="bundle")))
    assert bundle.namespace == "acme"
    assert set(bundle.agents) == {"acme:reviewer", "acme:summarizer"}
    assert bundle.version == "1.2.0"
    assert bundle.local_path == str(ACME)
    assert SHA256.match(bundle.content_digest or "")

    partial = asyncio.run(resolver.resolve(Dependency(source=BEHAVIOR, kind="behavior")))
    assert set(partial.agents) == {"acme:reviewer"}
    assert partial.subdirectory == "behaviors/review-only.yaml"
