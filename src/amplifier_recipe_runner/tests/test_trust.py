"""Tests for trust policy and capability intersection.

Contracts: ``recipe-dependency-manifest.v1`` Core 6 and Core 9;
``recipe-runner-lib.v1`` Core 6.

Everything here is local and offline: fixture bundles under ``fixtures/``, no
network, no git clone, no Foundation. The remote sources the refusal tests use
are *strings that are never reached* -- which is the point. A test that had to
contact ``github.com`` to prove a refusal would have already lost.

The discriminating pairs the contract names:

* BAD -- a disallowed scheme is refused, naming the source and the rule.
* BAD -- a disallowed host is refused, naming the source and the rule.
* BAD -- under a CI posture, a floating ``@main`` with no lock is refused;
  GOOD -- the same source pinned to a full sha, or covered by a lock entry,
  passes.
* GOOD -- a permitted source passes and the resolver is actually called.
* PROOF -- with a refused dependency anywhere in the list, a resolver spy
  records **zero** calls: refusal precedes fetch, not the other way round.
* Core 9 -- the three-way intersection, including the empty case, which is a
  reported result and never an exception.
"""

from __future__ import annotations

import asyncio
import dataclasses
import textwrap
from pathlib import Path

import pytest

from amplifier_recipe_runner.api import TrustPolicy as TrustPolicyProtocol
from amplifier_recipe_runner.errors import PreflightError
from amplifier_recipe_runner.errors import TrustRefusedError
from amplifier_recipe_runner.manifest import Dependency
from amplifier_recipe_runner.manifest import parse_manifest_file
from amplifier_recipe_runner.planner import plan
from amplifier_recipe_runner.resolver import LocalBundleResolver
from amplifier_recipe_runner.resolver import ResolvedBundle
from amplifier_recipe_runner.trust import DEFAULT_REMOTE_SCHEMES
from amplifier_recipe_runner.trust import EffectiveCapabilities
from amplifier_recipe_runner.trust import TrustPolicy
from amplifier_recipe_runner.trust import intersect_capabilities
from amplifier_recipe_runner.trust import is_immutable_ref
from amplifier_recipe_runner.trust import parse_source

FIXTURES = Path(__file__).parent / "fixtures"
ACME = FIXTURES / "acme"
WIDGET = FIXTURES / "widget"

SHA = "a" * 40
SHA256 = "b" * 64

REMOTE = "git+https://github.com/acme/bundle"
REMOTE_FLOATING = f"{REMOTE}@main"
REMOTE_PINNED = f"{REMOTE}@{SHA}"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def ci_policy(**overrides: object) -> TrustPolicy:
    """CI posture that also permits the local fixture tree."""
    kwargs: dict[str, object] = {
        "allowed_local_roots": (FIXTURES,),
        "allowed_hosts": ("github.com",),
    }
    kwargs.update(overrides)
    return TrustPolicy.ci(**kwargs)  # type: ignore[arg-type]


def write_recipe(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "recipe.yaml"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


class ResolverSpy:
    """Records every resolve call, so 'was anything fetched?' is answerable.

    Wraps the real local resolver, so a permitted plan still produces a real
    closure rather than a fake one that could not fail.
    """

    def __init__(self) -> None:
        self.inner = LocalBundleResolver()
        self.calls: list[str] = []

    async def resolve(self, dependency: Dependency, *, workspace: Path | None = None) -> ResolvedBundle:
        self.calls.append(dependency.source)
        return await self.inner.resolve(dependency, workspace=workspace)


def planned(recipe: Path, *, resolver: object, policy: TrustPolicy):
    manifest = parse_manifest_file(recipe)
    return asyncio.run(
        plan(
            manifest,  # type: ignore[arg-type]
            resolver,  # type: ignore[arg-type]
            recipe.parent,
            trust_policy=policy,  # type: ignore[arg-type]
        )
    )


# --------------------------------------------------------------------------
# The policy is the protocol the library already declares
# --------------------------------------------------------------------------


def test_policy_satisfies_the_public_trust_policy_protocol() -> None:
    policy = TrustPolicy.interactive()
    assert isinstance(policy, TrustPolicyProtocol)
    assert policy.name == "interactive"


def test_policy_is_frozen_and_hashable() -> None:
    policy = ci_policy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.require_immutable_refs = False  # type: ignore[misc]
    assert hash(policy) == hash(ci_policy())
    assert policy == ci_policy()


def test_refusal_is_a_preflight_error() -> None:
    """A host catching PreflightError must catch trust refusals too."""
    assert issubclass(TrustRefusedError, PreflightError)


# --------------------------------------------------------------------------
# Core 6 -- disallowed scheme
# --------------------------------------------------------------------------


def test_disallowed_scheme_is_refused_naming_source_and_rule() -> None:
    policy = ci_policy()
    source = f"git+ssh://github.com/acme/bundle@{SHA}"

    with pytest.raises(TrustRefusedError) as caught:
        policy.check_source(source)

    error = caught.value
    assert error.source == source
    assert "allowed_schemes" in (error.reason or "")
    assert "git+ssh" in (error.reason or "")
    assert error.policy == "ci"
    # The remedy names what to do, not merely what went wrong.
    assert "git+https" in (error.remedy or "")


def test_scheme_is_checked_even_under_the_permissive_posture() -> None:
    """Permissive is not unconditional."""
    with pytest.raises(TrustRefusedError) as caught:
        TrustPolicy.interactive().check_source("ftp://example.com/bundle")

    assert "allowed_schemes" in (caught.value.reason or "")


# --------------------------------------------------------------------------
# Core 6 -- disallowed host
# --------------------------------------------------------------------------


def test_disallowed_host_is_refused_naming_source_and_rule() -> None:
    policy = ci_policy()
    source = f"git+https://evil.example.com/acme/bundle@{SHA}"

    with pytest.raises(TrustRefusedError) as caught:
        policy.check_source(source)

    error = caught.value
    assert error.source == source
    assert "allowed_hosts" in (error.reason or "")
    assert "evil.example.com" in (error.reason or "")


def test_host_matching_ignores_case_userinfo_and_port() -> None:
    policy = ci_policy()
    policy.check_source(f"git+https://git@GitHub.com:443/acme/bundle@{SHA}")


def test_empty_host_allowlist_permits_nothing_and_none_permits_anything() -> None:
    """Empty is not 'unset' -- the distinction is the whole safety property."""
    closed = TrustPolicy.ci(allowed_hosts=())
    with pytest.raises(TrustRefusedError):
        closed.check_source(REMOTE_PINNED)

    TrustPolicy.ci(allowed_hosts=None).check_source(REMOTE_PINNED)


# --------------------------------------------------------------------------
# Core 6 -- local roots
# --------------------------------------------------------------------------


def test_local_source_outside_every_allowed_root_is_refused(tmp_path: Path) -> None:
    policy = ci_policy()
    outside = tmp_path / "elsewhere"

    with pytest.raises(TrustRefusedError) as caught:
        policy.check_source(str(outside))

    assert "allowed_local_roots" in (caught.value.reason or "")


def test_local_source_under_an_allowed_root_passes() -> None:
    ci_policy().check_source(str(ACME))


def test_empty_local_roots_permit_nothing_and_none_permits_anything() -> None:
    with pytest.raises(TrustRefusedError):
        TrustPolicy.ci(allowed_local_roots=()).check_source(str(ACME))

    TrustPolicy.ci(allowed_local_roots=None).check_source(str(ACME))


def test_file_url_is_treated_as_local_not_remote() -> None:
    ci_policy().check_source(f"file://{ACME}")


def test_a_local_path_containing_an_at_sign_is_not_read_as_a_pinned_ref(tmp_path: Path) -> None:
    """``/root/bundle@2`` is a directory name, not a revision.

    Truncating it at the ``@`` would run the root check against ``/root/bundle``
    -- a different path than the one that would actually be read.
    """
    root = tmp_path / "root"
    nested = root / "bundle@2"
    nested.mkdir(parents=True)

    facts = parse_source(str(nested))
    assert facts.requested_ref is None
    assert facts.path == str(nested)

    TrustPolicy.ci(allowed_local_roots=(root,)).check_source(str(nested))


def test_a_root_check_is_not_fooled_by_a_shared_name_prefix(tmp_path: Path) -> None:
    """``/allowed-evil`` is not inside ``/allowed``, despite the prefix."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    sibling = tmp_path / "allowed-evil"
    sibling.mkdir()

    policy = TrustPolicy.ci(allowed_local_roots=(allowed,))
    policy.check_source(str(allowed / "inner"))

    with pytest.raises(TrustRefusedError):
        policy.check_source(str(sibling))


def test_a_traversal_out_of_an_allowed_root_is_refused(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    with pytest.raises(TrustRefusedError) as caught:
        TrustPolicy.ci(allowed_local_roots=(allowed,)).check_source(f"{allowed}/../escaped")

    assert "allowed_local_roots" in (caught.value.reason or "")


# --------------------------------------------------------------------------
# lib Core 6 -- CI mode requires locked immutable refs
# --------------------------------------------------------------------------


def test_ci_mode_refuses_a_floating_ref_with_no_lock() -> None:
    policy = ci_policy()

    with pytest.raises(TrustRefusedError) as caught:
        policy.check_source(REMOTE_FLOATING)

    error = caught.value
    assert error.source == REMOTE_FLOATING
    assert "require_immutable_refs" in (error.reason or "")
    assert "main" in (error.reason or "")
    assert "no lock entry" in (error.reason or "")


def test_ci_mode_refuses_a_source_with_no_ref_at_all() -> None:
    """An absent ref is a floating ref: the default branch moves."""
    with pytest.raises(TrustRefusedError) as caught:
        ci_policy().check_source(REMOTE)

    assert "no ref is declared" in (caught.value.reason or "")


def test_ci_mode_accepts_a_full_sha_and_a_lock_entry() -> None:
    policy = ci_policy()
    policy.check_source(REMOTE_PINNED)
    policy.check_source(f"{REMOTE}@{SHA256}")
    # A floating ref is fine once a lock pins it to a real revision.
    policy.check_source(REMOTE_FLOATING, locked_ref=SHA)


def test_a_lock_that_pins_a_moving_ref_pins_nothing() -> None:
    with pytest.raises(TrustRefusedError) as caught:
        ci_policy().check_source(REMOTE_FLOATING, locked_ref="v1.2.3")

    assert "pins nothing" in (caught.value.reason or "")


def test_interactive_mode_allows_a_floating_ref() -> None:
    TrustPolicy.interactive().check_source(REMOTE_FLOATING)


def test_ci_mode_does_not_demand_a_ref_from_a_local_source() -> None:
    """A local path has no ref to pin; it is pinned by digest instead."""
    ci_policy().check_source(str(ACME))


@pytest.mark.parametrize(
    ("ref", "immutable"),
    [
        (SHA, True),
        (SHA256, True),
        ("main", False),
        ("v1.2.3", False),
        ("a" * 39, False),
        ("A" * 40, False),  # uppercase is not a git object id
        (None, False),
        ("", False),
    ],
)
def test_immutable_ref_recognition(ref: str | None, immutable: bool) -> None:
    assert is_immutable_ref(ref) is immutable


# --------------------------------------------------------------------------
# Post-resolution re-check: no field is inert
# --------------------------------------------------------------------------


def test_ci_mode_refuses_a_resolution_with_no_content_digest() -> None:
    with pytest.raises(TrustRefusedError) as caught:
        ci_policy().check_resolved(str(ACME), content_digest=None)

    assert "require_content_digest" in (caught.value.reason or "")


def test_ci_mode_refuses_a_remote_that_resolved_to_a_moving_revision() -> None:
    with pytest.raises(TrustRefusedError) as caught:
        ci_policy().check_resolved(REMOTE_PINNED, resolved_revision="main", content_digest="sha256:x")

    assert "require_immutable_refs" in (caught.value.reason or "")


def test_resolved_facts_that_satisfy_the_policy_pass() -> None:
    ci_policy().check_resolved(REMOTE_PINNED, resolved_revision=SHA, content_digest="sha256:x")
    ci_policy().check_resolved(str(ACME), content_digest="sha256:x")


def test_dependency_install_is_refused_under_the_ci_posture() -> None:
    with pytest.raises(TrustRefusedError) as caught:
        ci_policy().check_dependency_install(str(ACME), package="requests")

    assert "allow_dependency_install" in (caught.value.reason or "")
    assert "requests" in (caught.value.reason or "")

    # The interactive posture permits it.
    TrustPolicy.interactive().check_dependency_install(str(ACME), package="requests")


# --------------------------------------------------------------------------
# Source parsing
# --------------------------------------------------------------------------


def test_parse_source_splits_scheme_host_ref_and_subdirectory() -> None:
    facts = parse_source(f"git+https://github.com/acme/bundle@{SHA}#subdirectory=behaviors/x.yaml")
    assert facts.scheme == "git+https"
    assert facts.host == "github.com"
    assert facts.path == "/acme/bundle"
    assert facts.requested_ref == SHA
    assert facts.subdirectory == "behaviors/x.yaml"
    assert facts.is_local is False


def test_parse_source_reports_a_bare_path_as_local_never_guesses_remote() -> None:
    facts = parse_source(str(ACME))
    assert facts.scheme == "path"
    assert facts.host is None
    assert facts.is_local is True
    assert facts.requested_ref is None


def test_default_remote_schemes_exclude_credential_carrying_transports() -> None:
    assert "git+ssh" not in DEFAULT_REMOTE_SCHEMES
    assert "git+https" in DEFAULT_REMOTE_SCHEMES


# --------------------------------------------------------------------------
# manifest Core 9 -- capability intersection
# --------------------------------------------------------------------------


def test_capabilities_are_the_three_way_intersection() -> None:
    result = intersect_capabilities(
        manifest=("net", "fs", "exec"),
        host=("net", "fs", "clock"),
        runner=("fs", "exec"),
    )

    assert isinstance(result, EffectiveCapabilities)
    assert result.granted == ("fs",)
    assert result.requested == ("exec", "fs", "net")
    assert result.withheld_by_host == ("exec",)
    assert result.withheld_by_runner == ("net",)
    assert result.denied == ("exec", "net")
    assert "fs" in result
    assert len(result) == 1


def test_intersection_never_grants_what_the_manifest_did_not_declare() -> None:
    result = intersect_capabilities(manifest=("fs",), host=("fs", "net"), runner=("fs", "net"))
    assert result.granted == ("fs",)


def test_empty_intersection_is_reported_not_raised() -> None:
    result = intersect_capabilities(manifest=("net",), host=("fs",), runner=("net",))

    assert result.granted == ()
    assert result.is_empty is True
    assert result.withheld_by_host == ("net",)
    assert result.withheld_by_runner == ()
    assert result.denied == ("net",)


def test_a_manifest_declaring_nothing_gets_nothing() -> None:
    result = intersect_capabilities(manifest=(), host=None, runner=None)
    assert result.granted == ()
    assert result.requested == ()
    assert result.denied == ()


def test_none_is_unconstrained_and_empty_grants_nothing() -> None:
    unconstrained = intersect_capabilities(manifest=("net", "fs"), host=None, runner=None)
    assert unconstrained.granted == ("fs", "net")

    closed = intersect_capabilities(manifest=("net", "fs"), host=(), runner=None)
    assert closed.granted == ()
    assert closed.withheld_by_host == ("fs", "net")


def test_policy_supplies_its_allowlist_as_the_runner_term() -> None:
    policy = ci_policy(capability_allowlist=("fs",))
    result = policy.effective_capabilities(manifest=("fs", "net"), host=("fs", "net"))

    assert result.granted == ("fs",)
    assert result.withheld_by_runner == ("net",)

    # An unconstrained allowlist withholds nothing of its own.
    open_policy = ci_policy(capability_allowlist=None)
    assert open_policy.effective_capabilities(manifest=("fs", "net")).granted == ("fs", "net")


# --------------------------------------------------------------------------
# manifest Core 6 -- enforcement precedes fetch, proven with a resolver spy
# --------------------------------------------------------------------------


def test_refused_dependency_means_the_resolver_is_never_called(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {REMOTE_FLOATING}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )
    resolver = ResolverSpy()

    with pytest.raises(TrustRefusedError):
        planned(recipe, resolver=resolver, policy=ci_policy())

    assert resolver.calls == [], "the resolver must not be called for a refused source"


def test_a_later_refusal_still_precedes_the_first_fetch(tmp_path: Path) -> None:
    """The sharp case: dependency one is permitted, dependency two is not.

    Checking in-loop would fetch the first before refusing the second -- a
    side effect ahead of a refusal. Core 6 says every source is checked first,
    so the spy must record nothing at all.
    """
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
          - source: {REMOTE_FLOATING}
            kind: bundle
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )
    resolver = ResolverSpy()

    with pytest.raises(TrustRefusedError) as caught:
        planned(recipe, resolver=resolver, policy=ci_policy())

    assert caught.value.source == REMOTE_FLOATING
    assert resolver.calls == [], "nothing may be fetched before every source is cleared"


def test_permitted_sources_plan_normally_and_do_reach_the_resolver(tmp_path: Path) -> None:
    """The GOOD half of the pair: a passing policy is not a policy that
    silently blocks everything."""
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
        """,
    )
    resolver = ResolverSpy()

    result = planned(recipe, resolver=resolver, policy=ci_policy())

    assert resolver.calls == [str(ACME), str(WIDGET)]
    assert "acme:reviewer" in result.agents
    assert result.policy is not None
    assert result.policy.trust_policy == "ci"
