"""Tests for the schema-v2 / legacy routing adapter.

What these tests are actually defending:

* **Routing** (manifest.v1 Core 1) -- the manifest, not the caller, decides
  which engine runs a recipe.
* **Legacy confinement** (manifest.v1 Core 10) -- a legacy recipe still runs
  caller-bound, is labeled as such, and says so out loud. Byte-identity of that
  path is proved separately and more strongly by
  ``conformance/legacy-compat/harness.py --assert``; here we prove the label
  and warning ride channels that harness does not record.
* **No caller agent map on the v2 path** (lib.v1 Core 4, manifest.v1 Core 3) --
  the defect this whole effort exists to remove. The legacy executor passes
  ``coordinator.config["agents"]`` into every spawn; the v2 handover must not
  carry it, and the leak detector must be able to *see* it when planted, or the
  negative test would be vacuous.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from amplifier_module_tool_recipes import V2_RUN_STATE_KEY
from amplifier_module_tool_recipes import RecipesTool
from amplifier_module_tool_recipes import mount
from amplifier_module_tool_recipes import runner_adapter as ra

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

RUNNER_AVAILABLE = ra.runner_available()
requires_runner = pytest.mark.skipif(
    not RUNNER_AVAILABLE,
    reason=(
        f"{ra.RUNNER_DISTRIBUTION} is not installed; the adapter's lazy import "
        "path is covered by test_missing_runner_library_* instead."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

LEGACY_RECIPE = """\
name: legacy-recipe
description: A recipe with no manifest
version: "1.0.0"

steps:
  - id: review
    agent: "foundation:zen-architect"
    prompt: "Review it"
    output: review_result
"""

V2_RECIPE = """\
schema_version: 2

name: portable-recipe
description: A recipe that declares its own dependencies
version: "1.0.0"

dependencies:
  - source: "git+https://example.invalid/amplifier-bundle-foundation@main"
    kind: bundle
    required_agents:
      - "foundation:zen-architect"

steps:
  - id: review
    agent: "foundation:zen-architect"
    instruction: "Review it"
"""


class FakeResolver:
    """Duck-typed ``model_role_resolver`` capability."""

    def __init__(self, roles: dict[str, list[str]] | None = None, known: Any = None):
        self._roles = roles or {}
        self.known_roles = list(self._roles) if known is None else known
        self.resolved: list[str] = []

    async def resolve(self, role: str) -> list[str]:
        self.resolved.append(role)
        return list(self._roles.get(role, []))


class FakeHooks:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, name: str, data: dict[str, Any]) -> None:
        self.events.append((name, data))


class FakeDisplay:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def show_message(self, message: str, level: str = "info", source: str = "") -> None:
        self.messages.append(message)


class FakeCoordinator:
    """A caller that HAS an agent map -- the thing that must not leak.

    Deliberately not a ``MagicMock``: the leak detector works by object
    identity, and a mock's auto-created attributes would make both the positive
    and the negative result meaningless.
    """

    def __init__(self, agents: dict[str, Any] | None = None, resolver: Any = None):
        self.config: dict[str, Any] = {
            "agents": agents
            if agents is not None
            else {"foundation:zen-architect": {"description": "caller's own agent"}}
        }
        self.session = object()
        self.hooks = FakeHooks()
        self.display_system = FakeDisplay()
        self._capabilities: dict[str, Any] = {"model_role_resolver": resolver}

    @property
    def agent_map(self) -> dict[str, Any]:
        return self.config["agents"]

    @property
    def available_agents(self) -> list[str]:
        return sorted(self.config["agents"])

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value


def write_recipe(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def make_tool(coordinator: Any = None, session_manager: Any = None) -> RecipesTool:
    coordinator = coordinator if coordinator is not None else FakeCoordinator()
    session_manager = session_manager if session_manager is not None else MagicMock()
    executor = MagicMock()
    executor.execute_recipe = AsyncMock(
        return_value={"session": {"id": "sess-1"}, "final_output": "done"}
    )
    return RecipesTool(executor, session_manager, coordinator, {})


def make_v2_session_tool(
    tmp_path: Path,
    *,
    v2_run: dict[str, Any] | None = None,
    recipe_body: str = V2_RECIPE,
) -> RecipesTool:
    """A tool whose session ``sess-1`` holds a schema-v2 recipe.

    ``v2_run`` is the run record the session recorded, i.e. exactly what
    ``_record_v2_run`` writes; ``None`` means the run recorded nothing, which
    is its own distinct resume outcome.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir(exist_ok=True)
    write_recipe(session_dir, "recipe.yaml", recipe_body)
    original = write_recipe(tmp_path, "v2.yaml", recipe_body)

    state: dict[str, Any] = {"recipe_path": str(original)}
    if v2_run is not None:
        state[V2_RUN_STATE_KEY] = v2_run

    session_manager = MagicMock()
    session_manager.session_exists.return_value = True
    session_manager.load_state.return_value = state
    session_manager.get_session_dir.return_value = session_dir
    return make_tool(session_manager=session_manager)


# ---------------------------------------------------------------------------
# Manifest detection (manifest.v1 Core 1)
# ---------------------------------------------------------------------------


class TestManifestDetection:
    def test_recipe_declaring_schema_version_is_v2(self, temp_dir: Path):
        assert ra.is_v2_recipe(write_recipe(temp_dir, "v2.yaml", V2_RECIPE)) is True

    def test_recipe_without_schema_version_is_legacy(self, temp_dir: Path):
        assert ra.is_v2_recipe(write_recipe(temp_dir, "l.yaml", LEGACY_RECIPE)) is False

    def test_declared_schema_version_is_reported_verbatim(self, temp_dir: Path):
        assert ra.declared_schema_version(write_recipe(temp_dir, "v2.yaml", V2_RECIPE)) == 2
        assert ra.declared_schema_version(write_recipe(temp_dir, "l.yaml", LEGACY_RECIPE)) is None

    def test_present_but_invalid_schema_version_still_routes_to_the_library(
        self, temp_dir: Path
    ):
        """Presence, not value, routes. Manifest validity is the library's call.

        A second opinion about validity here would be a second manifest parser
        (lib.v1 Core 1 forbids exactly that).
        """
        path = write_recipe(temp_dir, "future.yaml", "schema_version: 99\nname: x\nsteps: []\n")
        assert ra.is_v2_recipe(path) is True
        assert ra.declared_schema_version(path) == 99

    def test_malformed_recipe_routes_to_legacy_so_its_error_text_is_unchanged(
        self, temp_dir: Path
    ):
        path = write_recipe(temp_dir, "bad.yaml", "name: [unclosed\n")
        assert ra.manifest_header(path) is None
        assert ra.is_v2_recipe(path) is False

    def test_missing_file_is_not_claimed_as_v2(self, temp_dir: Path):
        assert ra.is_v2_recipe(temp_dir / "nope.yaml") is False


# ---------------------------------------------------------------------------
# Routing (behavior 1)
# ---------------------------------------------------------------------------


class TestRouting:
    @pytest.mark.asyncio
    async def test_legacy_recipe_runs_on_the_legacy_executor(self, temp_dir: Path):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)
        v2_called: list[Any] = []
        tool._execute_v2_recipe = AsyncMock(side_effect=lambda *a, **k: v2_called.append(a))

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert result.success is True
        assert tool.executor.execute_recipe.await_count == 1
        assert v2_called == []

    @pytest.mark.asyncio
    async def test_v2_recipe_never_touches_the_legacy_executor(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        seen: dict[str, Any] = {}

        async def fake_run_v2(coordinator, session_manager, path, ctx, project, **kwargs):
            seen["path"] = path
            seen["context"] = ctx
            return MagicMock()

        monkeypatch.setattr("amplifier_module_tool_recipes.run_v2_recipe", fake_run_v2)
        monkeypatch.setattr(
            RecipesTool, "_v2_tool_result", lambda self, *a, **k: MagicMock(success=True)
        )

        await tool._execute_recipe({"recipe_path": str(recipe), "context": {"k": "v"}})

        assert seen["path"] == recipe
        assert seen["context"] == {"k": "v"}
        tool.executor.execute_recipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_v2_recipe_does_not_fall_back_to_legacy_when_library_is_absent(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A missing library is a loud failure, never a silent caller-bound run."""
        tool = make_tool()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)

        def boom() -> Any:
            raise ra.RecipeRunnerUnavailableError(ImportError("no module"))

        monkeypatch.setattr("amplifier_module_tool_recipes.load_runner", boom)

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert result.success is False
        message = result.error["message"]
        assert ra.RUNNER_DISTRIBUTION in message
        assert "NOT run in legacy caller-bound mode" in message
        tool.executor.execute_recipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resuming_a_v2_session_never_rebinds_agents_to_the_caller(
        self, temp_dir: Path
    ):
        """However a v2 resume ends, it never reaches the legacy executor.

        The refusal text changed when the library route landed; the invariant
        did not. Resuming a v2 recipe caller-bound would resolve its agents
        from this session instead of its declared dependencies (Core 3), so
        the legacy executor must stay untouched on every branch.
        """
        tool = make_v2_session_tool(temp_dir)

        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert result.success is False
        assert result.error["type"] == "V2RunNotRecorded"
        assert ra.execution_mode_of(result) == ra.V2_EXECUTION_MODE
        tool.executor.execute_recipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_validating_a_v2_recipe_goes_to_the_library_not_the_validator(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The legacy validator is never consulted for a v2 recipe.

        It ignores the ``dependencies`` manifest entirely, so it would report
        "valid" while knowing nothing about what the recipe resolves to.
        """
        tool = make_tool()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        legacy_calls: list[Any] = []
        monkeypatch.setattr(
            "amplifier_module_tool_recipes.validate_recipe",
            lambda *a, **k: legacy_calls.append(a),
        )

        result = await tool._validate_recipe({"recipe_path": str(recipe)})

        assert legacy_calls == []
        assert ra.execution_mode_of(result) == ra.V2_EXECUTION_MODE

    @pytest.mark.asyncio
    async def test_validating_a_legacy_recipe_still_works(self, temp_dir: Path):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)

        result = await tool._validate_recipe({"recipe_path": str(recipe)})

        assert result.success is True
        assert result.output["status"] == "valid"


# ---------------------------------------------------------------------------
# Legacy labeling + deprecation (behavior 2, manifest.v1 Core 10)
# ---------------------------------------------------------------------------


class TestLegacyLabelling:
    @pytest.mark.asyncio
    async def test_legacy_result_is_labeled_caller_bound(self, temp_dir: Path):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert ra.execution_mode_of(result) == ra.LEGACY_EXECUTION_MODE
        assert ra.LEGACY_EXECUTION_MODE == "legacy-caller-bound"

    @pytest.mark.asyncio
    async def test_label_does_not_change_the_serialized_payload(self, temp_dir: Path):
        """The legacy-compat baselines pin ``model_dump()``; the label rides beside it.

        If the label ever lands *inside* the payload, every baseline drifts and
        the Core 10 byte-identity claim is false.
        """
        tool = make_tool()
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert set(result.model_dump()) == {"success", "output", "error"}
        assert "execution_mode" not in result.output

    @pytest.mark.asyncio
    async def test_legacy_execution_emits_a_deprecation_warning_naming_the_remedy(
        self, temp_dir: Path
    ):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)

        with pytest.warns(DeprecationWarning) as record:
            await tool._execute_recipe({"recipe_path": str(recipe)})

        message = str(record[0].message)
        assert "schema_version" in message
        assert "dependencies" in message
        assert ra.LEGACY_EXECUTION_MODE in message

    @pytest.mark.asyncio
    async def test_v2_result_is_labeled_isolated(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        monkeypatch.setattr("amplifier_module_tool_recipes.load_runner", lambda: MagicMock())

        async def fake_run_v2(*a: Any, **k: Any) -> Any:
            return MagicMock()

        monkeypatch.setattr("amplifier_module_tool_recipes.run_v2_recipe", fake_run_v2)
        monkeypatch.setattr(
            RecipesTool, "_v2_tool_result", lambda self, *a, **k: MagicMock(success=True)
        )

        result = await tool._execute_recipe({"recipe_path": str(recipe)})

        assert ra.execution_mode_of(result) == ra.V2_EXECUTION_MODE

    def test_deprecation_message_names_the_confinement_rule(self, temp_dir: Path):
        message = ra.legacy_deprecation_message(temp_dir / "x.yaml")
        assert "recipe-dependency-manifest.v1 Core 10" in message
        assert "recipe-runner CLI" in message


# ---------------------------------------------------------------------------
# The defect: no caller agent map on the v2 path (behavior 3)
# ---------------------------------------------------------------------------


class TestNoCallerAgentMap:
    def test_legacy_executor_really_does_pass_the_caller_agent_map(self):
        """The defect exists -- otherwise everything below tests nothing.

        ``executor.py`` reads ``coordinator.config["agents"]`` and hands it to
        ``spawn(agent_configs=...)``. This is asserted against the source so the
        v2 assertions below are measured against a real, present hazard.
        """
        source = (
            Path(__file__).resolve().parents[1]
            / "amplifier_module_tool_recipes"
            / "executor.py"
        ).read_text(encoding="utf-8")
        assert 'self.coordinator.config.get("agents", {})' in source
        assert "agent_configs=agents," in source

    def test_leak_detector_finds_a_planted_agent_map(self):
        """Guard against a vacuous negative: the detector must be able to see."""
        coordinator = FakeCoordinator()
        agents = ra.caller_agent_map(coordinator)

        class Leaky:
            def __init__(self, payload: Any) -> None:
                self.nested = {"deeper": [payload]}

        assert ra.find_caller_agent_leak(Leaky(agents), agents) is not None
        assert ra.find_caller_agent_leak({"clean": "payload"}, agents) is None

    def test_leak_detector_finds_a_single_planted_agent_config(self):
        coordinator = FakeCoordinator()
        agents = ra.caller_agent_map(coordinator)
        one_config = next(iter(agents.values()))

        assert ra.find_caller_agent_leak({"agent": one_config}, agents) is not None

    def test_agent_name_strings_are_not_a_leak(self):
        """A recipe naming an agent is normal; handing over the catalog is not."""
        coordinator = FakeCoordinator()
        agents = ra.caller_agent_map(coordinator)

        payload = {"agent": "foundation:zen-architect", "steps": ["review"]}
        assert ra.find_caller_agent_leak(payload, agents) is None

    @requires_runner
    def test_host_services_has_exactly_the_five_ports_and_no_agent_field(self):
        import dataclasses

        runner = ra.load_runner()
        fields = {f.name for f in dataclasses.fields(runner.HostServices)}

        assert fields == set(runner.HOST_PORTS)
        assert not fields & {"agents", "agent_configs", "session", "coordinator"}

    @requires_runner
    @pytest.mark.asyncio
    async def test_built_host_services_carry_no_caller_agent_map(self, temp_dir: Path):
        coordinator = FakeCoordinator(resolver=FakeResolver({"general": ["anthropic"]}))
        services = await ra.build_host_services(coordinator, MagicMock(), temp_dir)

        assert ra.find_caller_agent_leak(services, coordinator.agent_map) is None

    @requires_runner
    @pytest.mark.asyncio
    async def test_the_run_request_handed_to_the_library_carries_no_agent_map(
        self, temp_dir: Path
    ):
        """The load-bearing assertion of this lane (lib.v1 BAD 1)."""
        coordinator = FakeCoordinator(resolver=FakeResolver({"general": ["anthropic"]}))
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        captured: dict[str, Any] = {}

        async def capture(request: Any) -> Any:
            captured["request"] = request
            return MagicMock()

        await ra.run_v2_recipe(
            coordinator, MagicMock(), recipe, {"k": "v"}, temp_dir, run=capture
        )

        request = captured["request"]
        assert request.legacy_mode is False
        assert ra.find_caller_agent_leak(request, coordinator.agent_map) is None
        assert not hasattr(request, "agents")
        assert not hasattr(request.services, "agents")

    @requires_runner
    def test_a_planted_leak_refuses_the_run_instead_of_executing_it(self, temp_dir: Path):
        coordinator = FakeCoordinator()

        class LeakyServices:
            def __init__(self, agents: Any) -> None:
                self.agent_configs = agents

        with pytest.raises(ra.CallerAgentLeakError) as excinfo:
            ra.build_run_request(
                temp_dir / "v2.yaml",
                {},
                LeakyServices(coordinator.agent_map),
                coordinator,
            )

        assert "caller agent map" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Port mapping (behavior 3)
# ---------------------------------------------------------------------------


class TestProviderAccessPort:
    @pytest.mark.asyncio
    async def test_roles_come_from_the_model_role_resolver_capability(self):
        resolver = FakeResolver({"general": ["anthropic:sonnet"], "fast": ["anthropic:haiku"]})
        access = await ra.CoordinatorProviderAccess.create(FakeCoordinator(resolver=resolver))

        assert set(access.roles()) == {"general", "fast"}
        assert access.resolve("general") == ["anthropic:sonnet"]

    @pytest.mark.asyncio
    async def test_an_unserved_role_raises_rather_than_downgrading(self):
        access = await ra.CoordinatorProviderAccess.create(
            FakeCoordinator(resolver=FakeResolver({"general": ["anthropic"]}))
        )

        with pytest.raises(KeyError) as excinfo:
            access.resolve("vision")

        assert "vision" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_role_resolving_to_nothing_is_not_offered(self):
        access = await ra.CoordinatorProviderAccess.create(
            FakeCoordinator(resolver=FakeResolver({"general": []}))
        )

        assert access.roles() == ()

    @pytest.mark.asyncio
    async def test_no_resolver_capability_falls_back_to_the_session_default(self):
        """A host that routes nothing still runs its own agents, so the adapter
        serves one labeled default role rather than none at all -- see
        ``test_lean_bundle_provider_fallback.py`` for the full behavior."""
        access = await ra.CoordinatorProviderAccess.create(FakeCoordinator(resolver=None))

        assert access.roles() == (ra.SESSION_DEFAULT_ROLE,)
        assert access.role_source == ra.PROVIDER_ROLES_FALLBACK

    @pytest.mark.asyncio
    async def test_provider_access_holds_no_reference_to_the_caller(self):
        coordinator = FakeCoordinator(resolver=FakeResolver({"general": ["anthropic"]}))
        access = await ra.CoordinatorProviderAccess.create(coordinator)

        assert ra.find_caller_agent_leak(access, coordinator.agent_map) is None


@requires_runner
class TestApprovalPort:
    @pytest.mark.asyncio
    async def test_an_approved_stage_is_approved(self, temp_dir: Path):
        from amplifier_module_tool_recipes.session import ApprovalStatus

        runner = ra.load_runner()
        session_manager = MagicMock()
        session_manager.get_stage_approval_status.return_value = ApprovalStatus.APPROVED
        session_manager.load_state.return_value = {"_approval_message": "merge"}
        callback = ra.SessionApprovalCallback(session_manager, temp_dir, "sess-1")

        decision = await callback(
            runner.ApprovalRequest(run_id="r", stage="planning", prompt="ok?")
        )

        assert decision.approved is True
        assert decision.message == "merge"

    @pytest.mark.asyncio
    async def test_a_denied_stage_is_refused(self, temp_dir: Path):
        from amplifier_module_tool_recipes.session import ApprovalStatus

        runner = ra.load_runner()
        session_manager = MagicMock()
        session_manager.get_stage_approval_status.return_value = ApprovalStatus.DENIED
        callback = ra.SessionApprovalCallback(session_manager, temp_dir, "sess-1")

        decision = await callback(
            runner.ApprovalRequest(run_id="r", stage="planning", prompt="ok?")
        )

        assert decision.approved is False

    @pytest.mark.asyncio
    async def test_an_unanswered_gate_records_a_pending_approval_and_refuses(
        self, temp_dir: Path
    ):
        from amplifier_module_tool_recipes.session import ApprovalStatus

        runner = ra.load_runner()
        session_manager = MagicMock()
        session_manager.get_stage_approval_status.return_value = ApprovalStatus.NOT_REQUIRED
        callback = ra.SessionApprovalCallback(session_manager, temp_dir, "sess-1")

        decision = await callback(
            runner.ApprovalRequest(run_id="r", stage="planning", prompt="ok?")
        )

        assert decision.approved is False
        session_manager.set_pending_approval.assert_called_once()
        assert "approve" in (decision.message or "")

    @pytest.mark.asyncio
    async def test_with_no_bound_session_nothing_is_approved(self, temp_dir: Path):
        runner = ra.load_runner()
        callback = ra.SessionApprovalCallback(MagicMock(), temp_dir, None)

        decision = await callback(
            runner.ApprovalRequest(run_id="r", stage="planning", prompt="ok?")
        )

        assert decision.approved is False
        assert "Nothing was approved" in (decision.message or "")


class TestCancellationPort:
    def test_token_reflects_the_sessions_cancellation_state(self, temp_dir: Path):
        session_manager = MagicMock()
        session_manager.is_cancellation_requested.return_value = True
        token = ra.SessionCancellationToken(session_manager, temp_dir, "sess-1")

        assert token.cancelled is True
        with pytest.raises(ra.RecipeCancelledError):
            token.raise_if_cancelled()

    def test_an_uncancelled_session_does_not_raise(self, temp_dir: Path):
        session_manager = MagicMock()
        session_manager.is_cancellation_requested.return_value = False
        token = ra.SessionCancellationToken(session_manager, temp_dir, "sess-1")

        assert token.cancelled is False
        token.raise_if_cancelled()

    def test_no_bound_session_reports_not_cancelled(self, temp_dir: Path):
        token = ra.SessionCancellationToken(MagicMock(), temp_dir, None)

        assert token.cancelled is False


class TestEventSinkPort:
    @pytest.mark.asyncio
    async def test_events_reach_hooks_and_display(self):
        coordinator = FakeCoordinator()
        sink = ra.CoordinatorEventSink.from_coordinator(coordinator)

        sink.emit(MagicMock(kind="step:start", run_id="r1", data={"step_id": "one"}))
        await asyncio.sleep(0)

        assert coordinator.hooks.events[0][0] == "recipe:runner:step:start"
        assert coordinator.hooks.events[0][1]["step_id"] == "one"
        assert coordinator.display_system.messages

    @pytest.mark.asyncio
    async def test_a_failing_sink_never_fails_the_run(self):
        class Exploding:
            def show_message(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("display is down")

        sink = ra.CoordinatorEventSink(hook_emit=None, show_message=Exploding().show_message)

        sink.emit(MagicMock(kind="step:start", run_id="r1", data={}))  # must not raise

    def test_emitting_without_a_running_loop_is_harmless(self):
        coordinator = FakeCoordinator()
        sink = ra.CoordinatorEventSink.from_coordinator(coordinator)

        sink.emit(MagicMock(kind="session:ready", run_id="r1", data={}))

        assert coordinator.hooks.events == []

    @pytest.mark.asyncio
    async def test_sink_holds_no_reference_to_the_caller_agent_map(self):
        coordinator = FakeCoordinator()
        sink = ra.CoordinatorEventSink.from_coordinator(coordinator)

        assert ra.find_caller_agent_leak(sink, coordinator.agent_map) is None


# ---------------------------------------------------------------------------
# Result translation (lib.v1 Core 8 -- never a fabricated success)
# ---------------------------------------------------------------------------


@requires_runner
class TestResultTranslation:
    def _result(self, status: Any, **kwargs: Any) -> Any:
        runner = ra.load_runner()
        defaults: dict[str, Any] = {"run_id": "run-1", "status": status}
        defaults.update(kwargs)
        return runner.RunResult(**defaults)

    def test_a_succeeded_run_reports_completed_with_provenance(self, temp_dir: Path):
        runner = ra.load_runner()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        plan = runner.ExecutionPlan(
            recipe_digest="sha256:abc",
            schema_version=2,
            agents={
                "foundation:zen-architect": runner.AgentProvenance(
                    agent="foundation:zen-architect",
                    supplied_by="git+https://example.invalid/bundle@main",
                )
            },
        )
        tool = make_tool()

        result = tool._v2_tool_result(
            self._result(runner.RunStatus.SUCCEEDED, plan=plan, completed_steps=("review",)),
            runner,
            recipe,
            "portable-recipe",
            "sess-1",
        )

        assert result.success is True
        assert result.output["status"] == "completed"
        assert result.output["execution_mode"] == ra.V2_EXECUTION_MODE
        assert result.output["agent_provenance"] == {
            "foundation:zen-architect": "git+https://example.invalid/bundle@main"
        }

    def test_a_failed_run_is_never_reported_as_completed(self, temp_dir: Path):
        runner = ra.load_runner()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        error = runner.UndeclaredAgentError("foundation:zen-architect")
        tool = make_tool()

        result = tool._v2_tool_result(
            self._result(runner.RunStatus.FAILED, error=error),
            runner,
            recipe,
            "portable-recipe",
            None,
        )

        assert result.success is False
        assert result.output["status"] == "failed"
        assert "UndeclaredAgentError" == result.error["type"]

    def test_a_paused_run_reports_the_stage_awaiting_approval(self, temp_dir: Path):
        runner = ra.load_runner()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        tool = make_tool()

        result = tool._v2_tool_result(
            self._result(runner.RunStatus.PAUSED, pending_approval="planning"),
            runner,
            recipe,
            "portable-recipe",
            "sess-1",
        )

        assert result.output["status"] == "paused_for_approval"
        assert result.output["stage_name"] == "planning"

    def test_a_cancelled_run_reports_cancelled(self, temp_dir: Path):
        runner = ra.load_runner()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        tool = make_tool()

        result = tool._v2_tool_result(
            self._result(runner.RunStatus.CANCELLED),
            runner,
            recipe,
            "portable-recipe",
            "sess-1",
        )

        assert result.output["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Lazy import (behavior 4)
# ---------------------------------------------------------------------------


class TestLazyImport:
    def test_unavailable_error_names_the_distribution_and_the_refusal(self):
        message = str(ra.RecipeRunnerUnavailableError(ImportError("boom")))

        assert ra.RUNNER_DISTRIBUTION in message
        assert "NOT run in legacy caller-bound mode" in message
        assert "boom" in message

    def test_the_adapter_module_does_not_import_the_runner_at_module_scope(self):
        """A legacy recipe must still run on an install without the library."""
        source = (
            Path(__file__).resolve().parents[1]
            / "amplifier_module_tool_recipes"
            / "runner_adapter.py"
        ).read_text(encoding="utf-8")
        module_scope = source.split("def load_runner", 1)[0]

        assert f"import {ra.RUNNER_IMPORT_NAME}" not in module_scope


# ---------------------------------------------------------------------------
# v2 validate (lib.v1 Core 2 `validate`)
# ---------------------------------------------------------------------------


@requires_runner
class TestV2Validate:
    """`validate` on a v2 recipe is manifest parse + plan preflight, no run.

    The claim under test is not "validate returns something". It is that the
    answer comes from the *library* -- so a recipe whose declared closure
    cannot be resolved is reported as unresolvable, rather than pronounced
    valid by a validator that never looked at `dependencies` at all.
    """

    @pytest.mark.asyncio
    async def test_a_plan_preflight_failure_becomes_a_typed_finding(self, temp_dir: Path):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)

        result = await tool._validate_recipe({"recipe_path": str(recipe)})

        assert result.success is False
        assert result.error["message"] == "Recipe validation failed"
        assert result.error["schema_version"] == 2
        # The library's own dependency source is unreachable by construction
        # (example.invalid), so the finding must name a real library error --
        # not a generic string, and never "valid".
        (finding,) = result.error["errors"]
        assert finding["code"] == "DependencyResolutionError"
        assert finding["message"]

    @pytest.mark.asyncio
    async def test_a_clean_plan_reports_valid_with_its_schema_version(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        runner = ra.load_runner()
        tool = make_tool()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)

        async def fake_plan(request: Any) -> Any:
            return runner.ExecutionPlan(recipe_digest="d", schema_version=2, step_ids=("review",))

        monkeypatch.setattr(runner, "plan", fake_plan)

        result = await tool._validate_recipe({"recipe_path": str(recipe)})

        assert result.success is True
        assert result.output["status"] == "valid"
        assert result.output["schema_version"] == 2
        assert result.output["execution_mode"] == ra.V2_EXECUTION_MODE

    @pytest.mark.asyncio
    async def test_validate_executes_nothing_and_carries_no_host_services(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A side-effect-free check must not be handed the five host ports.

        `services=None` is what keeps a *validation* from having any reach
        into the calling session at all -- there is nothing for a caller agent
        map to travel through.
        """
        runner = ra.load_runner()
        tool = make_tool()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        seen: dict[str, Any] = {}
        ran: list[Any] = []

        async def fake_plan(request: Any) -> Any:
            seen["request"] = request
            return runner.ExecutionPlan(recipe_digest="d", schema_version=2)

        async def fake_run(request: Any) -> Any:  # pragma: no cover - must not run
            ran.append(request)
            raise AssertionError("validate must not execute the recipe")

        monkeypatch.setattr(runner, "plan", fake_plan)
        monkeypatch.setattr(runner, "run", fake_run)

        await tool._validate_recipe({"recipe_path": str(recipe)})

        assert ran == []
        assert seen["request"].services is None
        assert seen["request"].legacy_mode is False
        assert seen["request"].context == {}

    @pytest.mark.asyncio
    async def test_a_missing_library_is_reported_not_downgraded_to_the_validator(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise ra.RecipeRunnerUnavailableError(ImportError("no module"))

        monkeypatch.setattr("amplifier_module_tool_recipes.validate_v2_recipe", boom)

        result = await tool._validate_recipe({"recipe_path": str(recipe)})

        assert result.success is False
        assert ra.RUNNER_DISTRIBUTION in result.error["message"]

    @pytest.mark.asyncio
    async def test_legacy_recipes_keep_the_legacy_validator(self, temp_dir: Path):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "legacy.yaml", LEGACY_RECIPE)

        result = await tool._validate_recipe({"recipe_path": str(recipe)})

        assert result.success is True
        assert result.output["status"] == "valid"
        assert "schema_version" not in result.output

    @pytest.mark.asyncio
    async def test_the_legacy_validator_would_have_called_this_recipe_valid(
        self, temp_dir: Path
    ):
        """The whole point, on one recipe.

        This recipe declares NO dependency for the agent its step references,
        but the *caller* happens to have that agent. The legacy validator asks
        the caller, so it answers "valid" -- the recipe would then fail, or
        worse silently resolve a different agent, on any other machine. Routed
        to the library it is `UndeclaredAgentError`, because a v2 recipe's
        agents come only from its own declared closure (Core 3).

        If this test ever passes with both answers agreeing, the routing has
        stopped mattering.
        """
        from amplifier_module_tool_recipes.models import Recipe
        from amplifier_module_tool_recipes.validator import validate_recipe

        body = (
            "schema_version: 2\n"
            "name: undeclared\n"
            "description: declares no dependency for the agent it uses\n"
            'version: "1.0.0"\n'
            "dependencies: []\n"
            "steps:\n"
            "  - id: review\n"
            '    agent: "foundation:zen-architect"\n'
            '    prompt: "go"\n'
            "    output: r\n"
        )
        recipe = write_recipe(temp_dir, "undeclared.yaml", body)
        # The caller HAS the agent -- which is exactly why the legacy answer
        # is wrong rather than merely uninformed.
        tool = make_tool(coordinator=FakeCoordinator())

        legacy = validate_recipe(Recipe.from_yaml(recipe), tool.coordinator)
        assert legacy.is_valid is True

        result = await tool._validate_recipe({"recipe_path": str(recipe)})

        assert result.success is False
        assert [e["code"] for e in result.error["errors"]] == ["UndeclaredAgentError"]

    def test_issue_for_matches_the_standalone_cli_field_for_field(self):
        runner = ra.load_runner()
        exc = runner.UndeclaredAgentError("some:agent", step_id="review", remedy="declare it")

        issue = ra.issue_for(exc)

        assert issue.code == "UndeclaredAgentError"
        assert issue.remedy == "declare it"
        assert issue.message


# ---------------------------------------------------------------------------
# v2 resume (lib.v1 Core 2 `resume`; manifest.v1 Core 8)
# ---------------------------------------------------------------------------


@requires_runner
class TestV2Resume:
    """`resume` on a v2 session routes to the library, or refuses. Never legacy.

    Four outcomes, decided by what the run actually recorded. The one thing
    none of them may do is approximate: silently re-running completed steps,
    or resuming caller-bound, are both invisible from the outside.
    """

    @pytest.mark.asyncio
    async def test_a_finished_run_reports_nothing_to_resume(self, temp_dir: Path):
        tool = make_v2_session_tool(
            temp_dir,
            v2_run={
                "status": "succeeded",
                "run_id": "run-1",
                "completed_steps": ["review"],
                "step_ids": ["review"],
                "recipe_path": str(temp_dir / "v2.yaml"),
            },
        )

        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert result.success is True
        assert result.output["status"] == "nothing_to_resume"
        assert result.output["run_id"] == "run-1"

    @pytest.mark.asyncio
    async def test_nothing_completed_resumes_by_running_from_the_start(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Resuming a run that never started a step IS running it.

        Same reading the standalone CLI's `resume` takes for the same case:
        one library call, under the recorded run id, re-running nothing.
        """
        runner = ra.load_runner()
        tool = make_v2_session_tool(
            temp_dir,
            v2_run={
                "status": "failed",
                "run_id": "run-1",
                "completed_steps": [],
                "step_ids": ["review"],
                "recipe_path": str(temp_dir / "v2.yaml"),
            },
        )
        seen: dict[str, Any] = {}

        async def fake_run(request: Any) -> Any:
            seen["request"] = request
            return runner.RunResult(run_id=request.run_id, status=runner.RunStatus.SUCCEEDED)

        monkeypatch.setattr(runner, "run", fake_run)

        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert result.success is True
        assert seen["request"].run_id == "run-1"
        assert seen["request"].legacy_mode is False
        tool.executor.execute_recipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_partly_completed_run_refuses_and_names_the_missing_seam(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        runner = ra.load_runner()
        tool = make_v2_session_tool(
            temp_dir,
            v2_run={
                "status": "failed",
                "run_id": "run-1",
                "completed_steps": ["review"],
                "step_ids": ["review", "apply"],
                "recipe_path": str(temp_dir / "v2.yaml"),
            },
        )
        ran: list[Any] = []

        async def fake_run(request: Any) -> Any:  # pragma: no cover - must not run
            ran.append(request)
            raise AssertionError("a partly completed run must not be re-run from the start")

        monkeypatch.setattr(runner, "run", fake_run)
        monkeypatch.setattr("amplifier_module_tool_recipes.runner_adapter.library_resume", lambda: None)

        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert ran == []
        assert result.success is False
        assert result.error["type"] == "V2ResumeUnavailableError"
        assert result.error["completed_steps"] == ["review"]
        assert "resume" in result.error["remedy"]
        tool.executor.execute_recipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_library_resume_entry_point_is_used_when_it_exists(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The seam: when the library exports `resume`, it wins outright.

        This is what makes the refusal above temporary rather than a design
        decision -- no change here is needed for the entry point to take over,
        including for the mid-run case the refusal covers.
        """
        runner = ra.load_runner()
        tool = make_v2_session_tool(
            temp_dir,
            v2_run={
                "status": "failed",
                "run_id": "run-1",
                "completed_steps": ["review"],
                "step_ids": ["review", "apply"],
                "recipe_path": str(temp_dir / "v2.yaml"),
            },
        )
        seen: dict[str, Any] = {}

        async def fake_resume(request: Any) -> Any:
            seen["request"] = request
            return runner.RunResult(
                run_id=request.run_id,
                status=runner.RunStatus.SUCCEEDED,
                completed_steps=("review", "apply"),
            )

        monkeypatch.setattr(runner, "resume", fake_resume, raising=False)

        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert result.success is True
        assert seen["request"].run_id == "run-1"
        assert result.output["completed_steps"] == ["review", "apply"]

    @pytest.mark.asyncio
    async def test_an_unrecorded_run_refuses_rather_than_assuming_nothing_ran(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        runner = ra.load_runner()
        tool = make_v2_session_tool(temp_dir, v2_run=None)
        ran: list[Any] = []

        async def fake_run(request: Any) -> Any:  # pragma: no cover - must not run
            ran.append(request)
            raise AssertionError("an unrecorded run must not be re-run")

        monkeypatch.setattr(runner, "run", fake_run)

        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert ran == []
        assert result.success is False
        assert result.error["type"] == "V2RunNotRecorded"

    @pytest.mark.asyncio
    async def test_unknown_completed_steps_refuse_rather_than_re_running(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A run that raised recorded no step list; that is not "no steps ran"."""
        runner = ra.load_runner()
        tool = make_v2_session_tool(
            temp_dir,
            v2_run={
                "status": "errored",
                "run_id": None,
                "completed_steps": None,
                "step_ids": None,
                "recipe_path": str(temp_dir / "v2.yaml"),
            },
        )
        ran: list[Any] = []

        async def fake_run(request: Any) -> Any:  # pragma: no cover - must not run
            ran.append(request)
            raise AssertionError("an unknown completed-step set must not be re-run")

        monkeypatch.setattr(runner, "run", fake_run)

        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert ran == []
        assert result.success is False
        assert result.error["type"] == "V2CompletedStepsUnknown"

    @pytest.mark.asyncio
    async def test_a_v2_run_records_what_it_completed(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Without this record, a later resume could only guess.

        The library returns `completed_steps` and persists nothing itself, so
        if the adapter drops them the resume decision above has no input.
        """
        runner = ra.load_runner()
        saved: dict[str, Any] = {}
        session_manager = MagicMock()
        session_manager.create_session.return_value = "sess-1"
        session_manager.load_state.return_value = {}
        session_manager.save_state.side_effect = (
            lambda sid, path, state: saved.update(state)
        )
        tool = make_tool(session_manager=session_manager)
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)

        async def fake_run_v2(*args: Any, **kwargs: Any) -> Any:
            return runner.RunResult(
                run_id="run-9",
                status=runner.RunStatus.PAUSED,
                completed_steps=("review",),
                pending_approval="planning",
            )

        monkeypatch.setattr("amplifier_module_tool_recipes.run_v2_recipe", fake_run_v2)

        await tool._execute_recipe({"recipe_path": str(recipe)})

        record = saved[V2_RUN_STATE_KEY]
        assert record["run_id"] == "run-9"
        assert record["status"] == "paused"
        assert record["completed_steps"] == ["review"]
        assert record["recipe_path"] == str(recipe)

    @pytest.mark.asyncio
    async def test_a_raising_v2_run_records_completed_steps_as_unknown(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        saved: dict[str, Any] = {}
        session_manager = MagicMock()
        session_manager.create_session.return_value = "sess-1"
        session_manager.load_state.return_value = {}
        session_manager.save_state.side_effect = (
            lambda sid, path, state: saved.update(state)
        )
        tool = make_tool(session_manager=session_manager)
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("exploded mid-run")

        monkeypatch.setattr("amplifier_module_tool_recipes.run_v2_recipe", boom)

        await tool._execute_recipe({"recipe_path": str(recipe)})

        record = saved[V2_RUN_STATE_KEY]
        assert record["status"] == "errored"
        assert record["completed_steps"] is None

    @pytest.mark.asyncio
    async def test_resuming_a_legacy_session_is_untouched(self, temp_dir: Path):
        tool = make_v2_session_tool(temp_dir, recipe_body=LEGACY_RECIPE)

        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert result.success is True
        assert ra.execution_mode_of(result) == ra.LEGACY_EXECUTION_MODE
        assert tool.executor.execute_recipe.await_count == 1


# ---------------------------------------------------------------------------
# Adapter configuration (manifest.v1 Core 12's spirit -- never silently inert)
# ---------------------------------------------------------------------------


class TestAdapterConfig:
    """A config key is read or refused. It is never accepted and ignored.

    `legacy_mode` is the specific key this item names: it was accepted by
    `RunRequest`, never read, and had no adapter-side meaning at all -- so a
    host setting it got a run that looked configured and was not.
    """

    def test_the_keys_mount_reads_are_accepted(self):
        ra.check_adapter_config({"session_dir": "/tmp/x", "auto_cleanup_days": 3})
        ra.check_adapter_config({})
        ra.check_adapter_config(None)

    def test_legacy_mode_is_refused_with_the_reason_it_cannot_be_a_setting(self):
        with pytest.raises(ra.AdapterConfigError) as excinfo:
            ra.check_adapter_config({"legacy_mode": True})

        message = str(excinfo.value)
        assert excinfo.value.key == "legacy_mode"
        assert "decided by the recipe's own manifest" in message
        assert "schema_version" in message

    def test_legacy_mode_false_is_refused_too(self):
        """Refusing only the "dangerous" value would still leave it inert."""
        with pytest.raises(ra.AdapterConfigError):
            ra.check_adapter_config({"legacy_mode": False})

    def test_an_unread_key_is_refused_naming_the_keys_that_are_read(self):
        with pytest.raises(ra.AdapterConfigError) as excinfo:
            ra.check_adapter_config({"sesion_dir": "/tmp/typo"})

        message = str(excinfo.value)
        assert "sesion_dir" in message
        assert "session_dir" in message
        assert "auto_cleanup_days" in message

    @pytest.mark.asyncio
    async def test_mount_refuses_rather_than_mounting_a_misconfigured_tool(self):
        coordinator = FakeCoordinator()
        coordinator.mount_points = {"tools": {}}

        with pytest.raises(ra.AdapterConfigError):
            await mount(coordinator, {"legacy_mode": True})

        assert coordinator.mount_points["tools"] == {}

    @pytest.mark.asyncio
    async def test_mount_still_works_with_a_valid_config(self, temp_dir: Path):
        coordinator = FakeCoordinator()
        coordinator.mount_points = {"tools": {}}

        await mount(coordinator, {"session_dir": str(temp_dir), "auto_cleanup_days": 1})

        assert "recipes" in coordinator.mount_points["tools"]

    @requires_runner
    def test_the_v2_run_request_is_never_caller_bound(self, temp_dir: Path):
        """The only value this adapter can produce for `legacy_mode` is False.

        Not a preference: a v2 recipe run caller-bound would resolve a
        different agent catalog and still report success.
        """
        coordinator = FakeCoordinator()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)
        services = object()

        request = ra.build_run_request(recipe, {}, services, coordinator)

        assert request.legacy_mode is False
        assert ra.build_validate_request(recipe).legacy_mode is False
