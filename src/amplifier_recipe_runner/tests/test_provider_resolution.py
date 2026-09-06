"""Where a standalone run's model comes from (executor-parity delta 10).

The rule is layered and the recipe wins:

1. the composed closure declares providers  -> ``recipe-closure`` (pinned);
2. it declares none and the host's port offers a mountable provider
   -> ``host-port``;
3. neither -> a refusal naming BOTH remedies.

Every test here runs against a *fake* composed bundle: the decision under test
is which layer supplies the provider and what is recorded about it, not whether
Foundation can install a provider module. No network, no model call.
"""

from __future__ import annotations

import asyncio
import dataclasses
import textwrap
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from amplifier_recipe_runner.api import ExecutionPlan
from amplifier_recipe_runner.api import RunRequest
from amplifier_recipe_runner.api import RunStatus
from amplifier_recipe_runner.execution import DEFAULT_ROLE_PREFERENCE
from amplifier_recipe_runner.execution import PROVIDER_SOURCE_HOST
from amplifier_recipe_runner.execution import PROVIDER_SOURCE_INJECTED
from amplifier_recipe_runner.execution import PROVIDER_SOURCE_NONE
from amplifier_recipe_runner.execution import PROVIDER_SOURCE_RECIPE
from amplifier_recipe_runner.execution import FoundationSessionFactory
from amplifier_recipe_runner.execution import FoundationSpawnBackend
from amplifier_recipe_runner.execution import ModelRoleUnavailableError
from amplifier_recipe_runner.execution import NoProviderError
from amplifier_recipe_runner.execution import PlanCatalog
from amplifier_recipe_runner.execution import ProviderResolution
from amplifier_recipe_runner.execution import SpawnRequest
from amplifier_recipe_runner.execution import run as run_recipe
from amplifier_recipe_runner.manifest import parse_manifest_file
from amplifier_recipe_runner.planner import plan as plan_dependencies
from amplifier_recipe_runner.ports import HostServices
from amplifier_recipe_runner.ports import ProviderSpec
from amplifier_recipe_runner.ports import RunEvent
from amplifier_recipe_runner.ports import provider_specs
from amplifier_recipe_runner.resolver import LocalBundleResolver

FIXTURES = Path(__file__).parent / "fixtures" / "exec"
SUPPLIER = FIXTURES / "supplier"

SONNET = ProviderSpec(
    module="provider-anthropic",
    source="git+https://example.invalid/provider-anthropic@v1",
    config={"default_model": "claude-sonnet-5"},
    id="sonnet",
)
HAIKU = ProviderSpec(
    module="provider-anthropic",
    source="git+https://example.invalid/provider-anthropic@v1",
    config={"default_model": "claude-haiku-4-5"},
    id="haiku",
)
PINNED = {
    "module": "provider-openai",
    "source": "git+https://example.invalid/provider-openai@v1",
    "config": {"default_model": "gpt-pinned"},
}


# --------------------------------------------------------------------------
# doubles -- a composed bundle, without Foundation
# --------------------------------------------------------------------------


class FakeCoordinator:
    def __init__(self) -> None:
        self.capabilities: dict[str, Any] = {}

    def register_capability(self, name: str, value: Any) -> None:
        self.capabilities[name] = value


class FakeSession:
    def __init__(self, mount_plan: Mapping[str, Any]) -> None:
        self.mount_plan = dict(mount_plan)
        self.coordinator = FakeCoordinator()
        self.instructions: list[str] = []
        self.cleaned = False

    async def execute(self, instruction: str) -> str:
        self.instructions.append(instruction)
        return f"ran:{instruction}"

    async def cleanup(self) -> None:
        self.cleaned = True


@dataclasses.dataclass
class FakePrepared:
    mount_plan: dict[str, Any]
    sessions: list[FakeSession] = dataclasses.field(default_factory=list)

    async def create_session(self, session_cwd: Path | None = None) -> FakeSession:
        session = FakeSession(self.mount_plan)
        self.sessions.append(session)
        return session


class FakeBundle:
    """Just enough of Foundation's ``Bundle`` for the provider decision."""

    def __init__(self, providers: Any) -> None:
        self.providers = providers
        self.agents: dict[str, Any] = {}
        self.name = "fake"
        self.prepared: FakePrepared | None = None

    async def prepare(self, install_deps: bool = True) -> FakePrepared:
        self.prepared = FakePrepared(mount_plan={"providers": list(self.providers or [])})
        return self.prepared


class StubFactory(FoundationSessionFactory):
    """The real factory, composing a fake bundle instead of fetching one."""

    def __init__(self, bundle: FakeBundle) -> None:
        super().__init__(install_deps=False, registry=object())
        self.bundle = bundle

    async def compose(self, plan: ExecutionPlan, catalog: PlanCatalog) -> Any:
        return self.bundle


class HostProviders:
    """A ``provider_access`` port serving mountable specs."""

    def __init__(self, by_role: Mapping[str, Sequence[ProviderSpec]]) -> None:
        self._by_role = {role: tuple(specs) for role, specs in by_role.items()}
        self.resolved: list[str] = []

    def roles(self) -> Sequence[str]:
        return tuple(self._by_role)

    def resolve(self, role: str) -> Any:
        self.resolved.append(role)
        return self._by_role[role]


class OpaqueProviders:
    """The default CLI port: names roles, says nothing about what serves them."""

    def __init__(self, roles: Sequence[str] = ("general",)) -> None:
        self._roles = tuple(roles)

    def roles(self) -> Sequence[str]:
        return self._roles

    def resolve(self, role: str) -> Any:
        if role not in self._roles:
            raise KeyError(role)
        return role


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


RECIPE = """
    schema_version: 2
    name: review
    dependencies:
      - source: {supplier}
        kind: bundle
        required_agents: [supplier:reviewer]
    steps:
      - id: review
        agent: supplier:reviewer
        instruction: Review the change.
"""

ROLE_RECIPE = """
    schema_version: 2
    name: review
    dependencies:
      - source: {supplier}
        kind: bundle
        required_agents: [supplier:reviewer]
    steps:
      - id: review
        agent: supplier:reviewer
        model_role: {role}
        instruction: Review the change.
"""


def write_recipe(tmp_path: Path, body: str = RECIPE, **fields: Any) -> Path:
    path = tmp_path / "recipe.yaml"
    path.write_text(textwrap.dedent(body).lstrip().format(supplier=SUPPLIER, **fields), encoding="utf-8")
    return path


def planned(recipe_path: Path) -> ExecutionPlan:
    manifest = parse_manifest_file(recipe_path)
    return asyncio.run(
        plan_dependencies(
            manifest,  # type: ignore[arg-type]
            LocalBundleResolver(),  # type: ignore[arg-type]
            recipe_path.parent,
            recipe=recipe_path,
        )
    )


def services_for(tmp_path: Path, access: Any, **kwargs: Any) -> HostServices:
    return HostServices(provider_access=access, workspace=tmp_path, **kwargs)  # type: ignore[arg-type]


def build(tmp_path: Path, bundle: FakeBundle, access: Any) -> Any:
    plan = planned(write_recipe(tmp_path))
    return asyncio.run(
        StubFactory(bundle).create(
            plan,
            PlanCatalog.from_plan(plan),
            services_for(tmp_path, access),
            run_id="run-test",
        )
    )


def spawn(backend: FoundationSpawnBackend, *, model_role: str | None = None) -> str:
    request = SpawnRequest(
        agent="supplier:reviewer",
        canonical="supplier:reviewer",
        instruction="Review.",
        run_id="run-test",
        workspace=Path("/tmp"),
        provenance=None,  # type: ignore[arg-type]
        definition={},
        step_id="review",
        model_role=model_role,
    )
    return asyncio.run(backend.spawn(request))


# --------------------------------------------------------------------------
# The port's vocabulary
# --------------------------------------------------------------------------


def test_provider_spec_coerces_a_mount_shaped_mapping() -> None:
    spec = ProviderSpec.coerce(PINNED)

    assert spec is not None
    assert spec.module == "provider-openai"
    assert spec.instance == "provider-openai"  # no id -> the module names it
    assert spec.model == "gpt-pinned"
    assert spec.to_mount() == PINNED


def test_provider_spec_refuses_to_invent_a_module_source() -> None:
    """A provider-preference chain names no module, so nothing is guessed."""
    assert ProviderSpec.coerce({"provider": "anthropic", "model": "claude-sonnet-5"}) is None
    assert ProviderSpec.coerce("general") is None
    assert ProviderSpec.coerce({"module": "provider-anthropic"}) is None
    assert provider_specs([{"provider": "anthropic", "model": "x"}]) == ()
    assert provider_specs("general") == ()


def test_provider_specs_reads_one_spec_or_a_chain_of_them() -> None:
    assert provider_specs(SONNET) == (SONNET,)
    assert provider_specs([SONNET, HAIKU]) == (SONNET, HAIKU)
    assert provider_specs([SONNET, "nonsense"]) == (SONNET,)


# --------------------------------------------------------------------------
# Layer 1 -- the recipe's own closure wins
# --------------------------------------------------------------------------


def test_recipe_declared_providers_are_used(tmp_path: Path) -> None:
    build_result = build(tmp_path, FakeBundle([PINNED]), OpaqueProviders())

    assert build_result.provider.source == PROVIDER_SOURCE_RECIPE
    assert build_result.provider.record()["provider"] == "provider-openai"
    assert build_result.provider.record()["model"] == "gpt-pinned"


def test_recipe_declared_providers_win_over_the_host_port(tmp_path: Path) -> None:
    """Both layers present: the recipe's pin is authoritative, and the host's
    port is not even consulted -- pinning that a host could silently override
    would not be a pin."""
    bundle = FakeBundle([PINNED])
    host = HostProviders({"default": (SONNET,)})

    build_result = build(tmp_path, bundle, host)

    assert build_result.provider.source == PROVIDER_SOURCE_RECIPE
    assert host.resolved == [], "the host's port was consulted despite a pinned closure"
    assert bundle.providers == [PINNED], "the bridge overwrote the recipe's own providers"


def test_a_bundle_that_makes_no_providers_claim_is_not_a_denial(tmp_path: Path) -> None:
    """An embedder whose bundle object has a different shape must not be
    refused for an absence that cannot be measured."""
    build_result = build(tmp_path, FakeBundle(None), OpaqueProviders())

    assert build_result.provider.source == PROVIDER_SOURCE_RECIPE


# --------------------------------------------------------------------------
# Layer 2 -- the host port, bridged in
# --------------------------------------------------------------------------


def test_host_port_is_bridged_when_the_closure_declares_none(tmp_path: Path) -> None:
    bundle = FakeBundle([])

    build_result = build(tmp_path, bundle, HostProviders({"default": (SONNET,)}))

    assert build_result.provider.source == PROVIDER_SOURCE_HOST
    assert bundle.providers == [SONNET.to_mount()], "the host's provider was not mounted"
    record = build_result.provider.record()
    assert record["provider"] == "sonnet"
    assert record["model"] == "claude-sonnet-5"
    assert record["model_role"] == "default"
    assert record["roles"] == ("default",)


def test_bridged_run_actually_reaches_a_session(tmp_path: Path) -> None:
    bundle = FakeBundle([])
    build_result = build(tmp_path, bundle, HostProviders({"default": (SONNET,)}))

    assert spawn(build_result.backend) == "ran:Review."
    assert bundle.prepared is not None
    assert bundle.prepared.sessions[0].mount_plan["providers"] == [SONNET.to_mount()]


def test_every_bridged_role_is_activated_and_the_step_gets_only_its_own(tmp_path: Path) -> None:
    """A module never activated cannot be mounted later, so the union is
    prepared; the invocation is then narrowed to the role it asked for."""
    bundle = FakeBundle([])
    build_result = build(tmp_path, bundle, HostProviders({"default": (SONNET,), "cheap": (HAIKU,)}))

    assert bundle.providers == [SONNET.to_mount(), HAIKU.to_mount()]
    spawn(build_result.backend, model_role="cheap")

    assert bundle.prepared is not None
    assert bundle.prepared.sessions[-1].mount_plan["providers"] == [HAIKU.to_mount()]


def test_a_role_the_host_does_not_serve_is_refused_by_name(tmp_path: Path) -> None:
    build_result = build(tmp_path, FakeBundle([]), HostProviders({"default": (SONNET,)}))

    with pytest.raises(ModelRoleUnavailableError) as excinfo:
        spawn(build_result.backend, model_role="coding")

    message = str(excinfo.value)
    assert "'coding'" in message
    assert "default" in message
    assert "review" in message


def test_several_roles_with_no_conventional_default_refuse_rather_than_pick(tmp_path: Path) -> None:
    build_result = build(tmp_path, FakeBundle([]), HostProviders({"cheap": (HAIKU,), "smart": (SONNET,)}))

    assert build_result.provider.default_role is None
    with pytest.raises(ModelRoleUnavailableError) as excinfo:
        spawn(build_result.backend)

    assert "no obvious default" in str(excinfo.value)
    # ...and naming one resolves it.
    assert spawn(build_result.backend, model_role="smart") == "ran:Review."


@pytest.mark.parametrize("conventional", DEFAULT_ROLE_PREFERENCE)
def test_a_conventional_role_name_is_the_default(tmp_path: Path, conventional: str) -> None:
    build_result = build(
        tmp_path,
        FakeBundle([]),
        HostProviders({"cheap": (HAIKU,), conventional: (SONNET,)}),
    )

    assert build_result.provider.default_role == conventional
    assert build_result.provider.record()["provider"] == "sonnet"


def test_model_role_on_a_pinned_closure_is_refused_not_silently_ignored(tmp_path: Path) -> None:
    build_result = build(tmp_path, FakeBundle([PINNED]), HostProviders({"default": (SONNET,)}))

    with pytest.raises(ModelRoleUnavailableError) as excinfo:
        spawn(build_result.backend, model_role="coding")

    assert "PINS its provider" in str(excinfo.value)


# --------------------------------------------------------------------------
# Layer 3 -- neither, refused naming BOTH remedies
# --------------------------------------------------------------------------


def test_neither_layer_supplies_a_provider(tmp_path: Path) -> None:
    build_result = build(tmp_path, FakeBundle([]), OpaqueProviders(("general",)))

    assert build_result.provider.source == PROVIDER_SOURCE_NONE
    assert build_result.provider.offered_roles == ("general",)
    assert build_result.provider.uninterpretable_roles == ("general",)


def test_the_refusal_names_both_remedies(tmp_path: Path) -> None:
    build_result = build(tmp_path, FakeBundle([]), OpaqueProviders(("general",)))

    with pytest.raises(NoProviderError) as excinfo:
        spawn(build_result.backend)

    message = f"{excinfo.value} {excinfo.value.remedy}"
    assert "dependencies:" in message, "the recipe-side remedy is missing"
    assert "provider_access" in message, "the host-port remedy is missing"
    assert "--host-providers" in message
    assert "general" in message, "the roles the host DID offer are not named"


def test_a_role_the_host_refuses_to_resolve_is_recorded(tmp_path: Path) -> None:
    class Liar:
        def roles(self) -> Sequence[str]:
            return ("promised",)

        def resolve(self, role: str) -> Any:
            raise KeyError(role)

    build_result = build(tmp_path, FakeBundle([]), Liar())

    assert build_result.provider.source == PROVIDER_SOURCE_NONE
    assert build_result.provider.uninterpretable_roles == ("promised",)


def test_the_refusal_fires_at_spawn_not_at_composition(tmp_path: Path) -> None:
    """A recipe whose agent steps are all skipped never needs a provider."""
    bundle = FakeBundle([])

    build_result = build(tmp_path, bundle, OpaqueProviders())  # no raise

    assert build_result.backend is not None
    assert bundle.prepared is not None, "composition still happened"


# --------------------------------------------------------------------------
# Provenance reaches the run and every agent step's record
# --------------------------------------------------------------------------


def test_run_result_and_step_events_carry_the_provider_source(tmp_path: Path) -> None:
    bundle = FakeBundle([])
    sink = CollectingSink()
    recipe = write_recipe(tmp_path)
    request = RunRequest(
        recipe=recipe,
        services=services_for(tmp_path, HostProviders({"default": (SONNET,)}), event_sink=sink),
    )

    result = asyncio.run(
        run_recipe(request, resolver=LocalBundleResolver(), session_factory=StubFactory(bundle))
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.provider is not None
    assert result.provider["provider_source"] == PROVIDER_SOURCE_HOST
    assert result.provider["provider"] == "sonnet"
    assert result.provider["model"] == "claude-sonnet-5"

    agent_events = [e for e in sink.events if e.kind.startswith("agent:")]
    assert agent_events, "no agent step was recorded"
    for event in agent_events:
        assert event.data["provider_source"] == PROVIDER_SOURCE_HOST
        assert event.data["provider"] == "sonnet"
        assert event.data["model"] == "claude-sonnet-5"


def test_a_pinned_closure_is_recorded_as_such_end_to_end(tmp_path: Path) -> None:
    request = RunRequest(
        recipe=write_recipe(tmp_path),
        services=services_for(tmp_path, HostProviders({"default": (SONNET,)})),
    )

    result = asyncio.run(
        run_recipe(
            request,
            resolver=LocalBundleResolver(),
            session_factory=StubFactory(FakeBundle([PINNED])),
        )
    )

    assert result.provider is not None
    assert result.provider["provider_source"] == PROVIDER_SOURCE_RECIPE
    assert result.provider["model"] == "gpt-pinned"


def test_a_failed_run_still_reports_which_layer_was_consulted(tmp_path: Path) -> None:
    request = RunRequest(
        recipe=write_recipe(tmp_path),
        services=services_for(tmp_path, OpaqueProviders()),
    )

    result = asyncio.run(
        run_recipe(request, resolver=LocalBundleResolver(), session_factory=StubFactory(FakeBundle([])))
    )

    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, NoProviderError)
    assert result.provider is not None
    assert result.provider["provider_source"] == PROVIDER_SOURCE_NONE


def test_an_injected_backend_is_labeled_rather_than_claimed(tmp_path: Path) -> None:
    """The library resolved nothing, so it says that instead of naming a layer."""

    class Backend:
        async def spawn(self, request: SpawnRequest) -> str:
            return "injected"

    request = RunRequest(recipe=write_recipe(tmp_path), services=services_for(tmp_path, OpaqueProviders()))

    result = asyncio.run(
        run_recipe(request, resolver=LocalBundleResolver(), spawn_backend=Backend())
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.provider is not None
    assert result.provider["provider_source"] == PROVIDER_SOURCE_INJECTED


def test_a_step_model_role_reaches_the_spawn_request(tmp_path: Path) -> None:
    seen: list[str | None] = []

    class Backend:
        async def spawn(self, request: SpawnRequest) -> str:
            seen.append(request.model_role)
            return "ok"

    request = RunRequest(
        recipe=write_recipe(tmp_path, ROLE_RECIPE, role="cheap"),
        services=services_for(tmp_path, OpaqueProviders()),
    )

    asyncio.run(run_recipe(request, resolver=LocalBundleResolver(), spawn_backend=Backend()))

    assert seen == ["cheap"]


# --------------------------------------------------------------------------
# The record itself
# --------------------------------------------------------------------------


def test_resolution_record_always_names_a_source() -> None:
    for source in (
        PROVIDER_SOURCE_RECIPE,
        PROVIDER_SOURCE_HOST,
        PROVIDER_SOURCE_NONE,
        PROVIDER_SOURCE_INJECTED,
    ):
        record = ProviderResolution(source=source).record()
        assert record["provider_source"] == source
        assert "provider" in record and "model" in record


def test_mount_specs_deduplicates_a_provider_shared_by_two_roles() -> None:
    resolution = ProviderResolution(
        source=PROVIDER_SOURCE_HOST,
        by_role={"default": (SONNET,), "review": (SONNET, HAIKU)},
        default_role="default",
    )

    assert resolution.mount_specs == (SONNET, HAIKU)


# --------------------------------------------------------------------------
# The CLI's second remedy: --host-providers
# --------------------------------------------------------------------------


SETTINGS = """
config:
  providers:
    - id: sonnet
      module: provider-anthropic
      source: git+https://example.invalid/provider-anthropic@v1
      config:
        default_model: claude-sonnet-5
        base_url: ${RECIPE_RUNNER_TEST_BASE_URL}
        priority: 5
    - id: opus
      module: provider-anthropic
      source: git+https://example.invalid/provider-anthropic@v1
      config:
        default_model: claude-opus-5
        priority: 1
    - id: broken
      module: provider-nothing
      config:
        default_model: nope
"""


def write_settings(tmp_path: Path, body: str = SETTINGS) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def test_host_settings_are_read_in_the_hosts_own_priority_order(tmp_path: Path) -> None:
    from amplifier_recipe_runner.cli import _HostSettingsProviderAccess

    access = _HostSettingsProviderAccess.load(write_settings(tmp_path))

    assert [spec.instance for spec in access.specs] == ["opus", "sonnet"]
    assert access.roles() == (_HostSettingsProviderAccess.ROLE,)
    assert provider_specs(access.resolve(_HostSettingsProviderAccess.ROLE)) == access.specs


def test_an_entry_without_a_module_source_is_not_bridged(tmp_path: Path) -> None:
    from amplifier_recipe_runner.cli import _HostSettingsProviderAccess

    access = _HostSettingsProviderAccess.load(write_settings(tmp_path))

    assert "broken" not in [spec.instance for spec in access.specs]


def test_an_unset_placeholder_is_reported_not_silently_passed(tmp_path: Path, monkeypatch: Any) -> None:
    from amplifier_recipe_runner.cli import _HostSettingsProviderAccess

    monkeypatch.delenv("RECIPE_RUNNER_TEST_BASE_URL", raising=False)
    access = _HostSettingsProviderAccess.load(write_settings(tmp_path))

    assert access.unresolved == ("RECIPE_RUNNER_TEST_BASE_URL",)
    # ...and the literal placeholder is NOT handed to the provider as a URL.
    assert "base_url" not in dict(access.specs[1].config)

    monkeypatch.setenv("RECIPE_RUNNER_TEST_BASE_URL", "https://example.invalid")
    resolved = _HostSettingsProviderAccess.load(write_settings(tmp_path))
    assert resolved.unresolved == ()
    assert dict(resolved.specs[1].config)["base_url"] == "https://example.invalid"


def test_provider_id_narrows_and_orders(tmp_path: Path) -> None:
    from amplifier_recipe_runner.cli import _HostSettingsProviderAccess

    access = _HostSettingsProviderAccess.load(write_settings(tmp_path), only=("sonnet",))

    assert [spec.instance for spec in access.specs] == ["sonnet"]


def test_an_unknown_provider_id_names_what_is_configured(tmp_path: Path) -> None:
    import click

    from amplifier_recipe_runner.cli import _HostSettingsProviderAccess

    with pytest.raises(click.UsageError) as excinfo:
        _HostSettingsProviderAccess.load(write_settings(tmp_path), only=("nope",))

    assert "'nope'" in str(excinfo.value)
    assert "opus" in str(excinfo.value)


def test_settings_with_no_providers_block_refuses(tmp_path: Path) -> None:
    import click

    from amplifier_recipe_runner.cli import _HostSettingsProviderAccess

    path = tmp_path / "settings.yaml"
    path.write_text("config: {}\n", encoding="utf-8")

    with pytest.raises(click.UsageError) as excinfo:
        _HostSettingsProviderAccess.load(path)

    assert "config.providers" in str(excinfo.value)


def test_a_missing_settings_file_names_the_path_it_looked_for(tmp_path: Path) -> None:
    import click

    from amplifier_recipe_runner.cli import Runtime
    from amplifier_recipe_runner.cli import _services

    runtime = Runtime(workspace=tmp_path, config={}, config_path=None, json_output=False)
    missing = tmp_path / "absent.yaml"

    with pytest.raises(click.UsageError) as excinfo:
        _services(runtime, host_providers=True, host_settings=missing)

    assert str(missing) in str(excinfo.value)
    assert "--host-settings" in str(excinfo.value)


def test_without_the_flag_the_port_stays_deliberately_unmountable(tmp_path: Path) -> None:
    from amplifier_recipe_runner.cli import Runtime
    from amplifier_recipe_runner.cli import _services

    services = _services(Runtime(workspace=tmp_path, config={}, config_path=None, json_output=False))

    assert tuple(services.provider_access.roles()) == ("general",)
    assert provider_specs(services.provider_access.resolve("general")) == ()
