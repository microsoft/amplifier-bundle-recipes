"""A lean bundle (no model-role routing) can still run a schema-v2 recipe.

The defect these tests pin: under a session with no ``model_role_resolver``
capability -- Foundation's plain ``anchors`` bundle, say -- the adapter handed
the runner library zero model roles, so the library's (correct) precondition
refused every agent step: *"The host's provider access offers no model roles,
so no agent could run."* The same session runs its own agent work fine on its
configured default provider, so the net effect was that a v2 recipe could
resolve its agents and then never run them.

The fix is HOST-side, which is where provider routing belongs: the adapter
synthesizes one role backed by the session's default provider configuration
and labels it ``provider_roles=session-default-fallback``. The library's
precondition is untouched -- it still refuses a host that genuinely offers
nothing -- and an explicitly requested role is still refused by name rather
than silently downgraded onto the default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import amplifier_module_tool_recipes.runner_adapter as ra
from tests.test_runner_adapter import FakeCoordinator
from tests.test_runner_adapter import FakeResolver
from tests.test_runner_adapter import requires_runner
from tests.test_runner_adapter import write_recipe

LEAN_RECIPE = """\
schema_version: 2
name: lean-recipe
dependencies:
  - source: "{source}"
    kind: bundle
    required_agents:
      - "supplier:reviewer"
steps:
  - id: review
    agent: "supplier:reviewer"
    instruction: "Review it"
"""

ROLE_RECIPE = """\
schema_version: 2
name: role-recipe
dependencies:
  - source: "{source}"
    kind: bundle
steps:
  - id: review
    agent: "supplier:reviewer"
    model_role: coding
    instruction: "Review it"
"""


def make_bundle(tmp_path: Path) -> Path:
    """A local fixture bundle supplying exactly one agent."""
    root = tmp_path / "supplier"
    (root / "agents").mkdir(parents=True)
    (root / "bundle.md").write_text(
        "---\nbundle:\n  name: supplier\n  version: 1.0.0\n\n"
        "agents:\n  include:\n    - supplier:reviewer\n---\n\n# Supplier\n",
        encoding="utf-8",
    )
    (root / "agents" / "reviewer.md").write_text(
        "---\nmeta:\n  description: Reviewer.\n---\n\n# Reviewer\n", encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------------------
# The port itself
# ---------------------------------------------------------------------------


class TestSessionDefaultFallback:
    @pytest.mark.asyncio
    async def test_a_host_with_no_resolver_serves_the_session_default_role(self):
        access = await ra.CoordinatorProviderAccess.create(FakeCoordinator(resolver=None))

        assert access.roles() == (ra.SESSION_DEFAULT_ROLE,)
        assert access.role_source == ra.PROVIDER_ROLES_FALLBACK
        assert access.is_session_default_fallback is True

    @pytest.mark.asyncio
    async def test_the_fallback_handle_is_labeled_not_silent(self, caplog):
        with caplog.at_level("WARNING"):
            access = await ra.CoordinatorProviderAccess.create(FakeCoordinator(resolver=None))

        handle = access.resolve(ra.SESSION_DEFAULT_ROLE)
        assert handle["source"] == ra.PROVIDER_ROLES_FALLBACK
        # No routing is invented: the handle carries no preference chain.
        assert handle["provider_preferences"] == ()
        assert "provider_roles=session-default-fallback" in caplog.text

    @pytest.mark.asyncio
    async def test_an_explicit_role_is_still_refused_by_name(self):
        access = await ra.CoordinatorProviderAccess.create(FakeCoordinator(resolver=None))

        with pytest.raises(KeyError) as excinfo:
            access.resolve("coding")

        assert "coding" in str(excinfo.value)
        assert "session-default-fallback" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_host_that_does_route_is_taken_at_its_word(self):
        access = await ra.CoordinatorProviderAccess.create(
            FakeCoordinator(resolver=FakeResolver({"general": ["anthropic"]}))
        )

        assert access.roles() == ("general",)
        assert access.role_source == ra.PROVIDER_ROLES_RESOLVER

    @pytest.mark.asyncio
    async def test_the_fallback_leaks_no_caller_agent_map(self):
        coordinator = FakeCoordinator(resolver=None)
        access = await ra.CoordinatorProviderAccess.create(coordinator)

        assert ra.find_caller_agent_leak(access, coordinator.agent_map) is None

    def test_the_label_is_readable_without_resolving_anything(self):
        assert ra.provider_roles_label(FakeCoordinator(resolver=None)) == ra.PROVIDER_ROLES_FALLBACK
        assert (
            ra.provider_roles_label(FakeCoordinator(resolver=FakeResolver({"general": ["x"]})))
            == ra.PROVIDER_ROLES_RESOLVER
        )


# ---------------------------------------------------------------------------
# Explicit model roles a recipe asks for
# ---------------------------------------------------------------------------


class TestDeclaredModelRoles:
    def test_a_step_naming_a_role_is_found(self, temp_dir: Path):
        recipe = write_recipe(temp_dir, "role.yaml", ROLE_RECIPE.format(source="x"))

        assert ra.declared_model_roles(recipe) == (("review", ("coding",)),)

    def test_a_recipe_naming_none_asks_for_none(self, temp_dir: Path):
        recipe = write_recipe(temp_dir, "lean.yaml", LEAN_RECIPE.format(source="x"))

        assert ra.declared_model_roles(recipe) == ()

    @pytest.mark.asyncio
    async def test_an_unservable_role_fails_loud_naming_it(self, temp_dir: Path):
        access = await ra.CoordinatorProviderAccess.create(FakeCoordinator(resolver=None))
        recipe = write_recipe(temp_dir, "role.yaml", ROLE_RECIPE.format(source="x"))

        with pytest.raises(ra.ModelRoleUnavailableError) as excinfo:
            ra.check_model_roles(recipe, access)

        assert excinfo.value.role == "coding"
        assert excinfo.value.step_id == "review"
        assert "coding" in str(excinfo.value)
        assert "session-default-fallback" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_served_role_passes(self, temp_dir: Path):
        access = await ra.CoordinatorProviderAccess.create(
            FakeCoordinator(resolver=FakeResolver({"coding": ["anthropic"]}))
        )
        recipe = write_recipe(temp_dir, "role.yaml", ROLE_RECIPE.format(source="x"))

        ra.check_model_roles(recipe, access)  # does not raise


# ---------------------------------------------------------------------------
# End to end, through the library's own precondition
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self) -> None:
        self.coordinator = MagicMock()
        self.executed: list[str] = []

    async def execute(self, instruction: str) -> str:
        self.executed.append(instruction)
        return f"ran: {instruction}"

    async def cleanup(self) -> None:
        return None


class FakePrepared:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    async def create_session(self, session_cwd: Any = None) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session


class FakeBundle:
    async def prepare(self, install_deps: bool = True) -> FakePrepared:
        return FakePrepared()


@requires_runner
class TestLeanBundleRunsEndToEnd:
    """The library's real precondition runs -- only composition is faked."""

    def _factory(self):
        runner = ra.load_runner()
        from amplifier_recipe_runner.execution import FoundationSessionFactory

        class NoFoundationFactory(FoundationSessionFactory):
            """Real ``create`` (roles precondition included), faked closure."""

            async def compose(self, plan: Any, catalog: Any) -> Any:
                return FakeBundle()

        assert runner is not None
        return NoFoundationFactory()

    async def _run(self, coordinator: Any, recipe: Path, tmp_path: Path):
        from amplifier_recipe_runner.execution import run as library_run
        from amplifier_recipe_runner.resolver import LocalBundleResolver

        async def run_locally(request: Any) -> Any:
            return await library_run(
                request,
                resolver=LocalBundleResolver(),
                session_factory=self._factory(),
            )

        return await ra.run_v2_recipe(
            coordinator,
            MagicMock(),
            recipe,
            {},
            tmp_path,
            session_id=None,
            run=run_locally,
        )

    @pytest.mark.asyncio
    async def test_an_agent_step_runs_on_the_session_default(self, temp_dir: Path):
        bundle = make_bundle(temp_dir)
        recipe = write_recipe(temp_dir, "lean.yaml", LEAN_RECIPE.format(source=bundle))

        result = await self._run(FakeCoordinator(resolver=None), recipe, temp_dir)

        assert result.error is None, result.error
        assert result.status.name == "SUCCEEDED"
        assert result.completed_steps == ("review",)
        assert result.outputs["review"] == "ran: Review it"

    @pytest.mark.asyncio
    async def test_a_step_naming_a_role_refuses_instead_of_downgrading(self, temp_dir: Path):
        bundle = make_bundle(temp_dir)
        recipe = write_recipe(temp_dir, "role.yaml", ROLE_RECIPE.format(source=bundle))

        with pytest.raises(ra.ModelRoleUnavailableError) as excinfo:
            await self._run(FakeCoordinator(resolver=None), recipe, temp_dir)

        assert "coding" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_the_run_carries_no_caller_agent_map(self, temp_dir: Path):
        coordinator = FakeCoordinator(resolver=None)

        services = await ra.build_host_services(coordinator, MagicMock(), temp_dir)

        assert ra.find_caller_agent_leak(services, coordinator.agent_map) is None
        assert services.provider_access.roles() == (ra.SESSION_DEFAULT_ROLE,)
