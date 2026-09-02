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

from amplifier_module_tool_recipes import RecipesTool
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
    async def test_resuming_a_v2_session_refuses_rather_than_rebinding_agents(
        self, temp_dir: Path
    ):
        session_manager = MagicMock()
        session_manager.session_exists.return_value = True
        session_manager.load_state.return_value = {"recipe_path": str(temp_dir / "v2.yaml")}
        session_dir = temp_dir / "session"
        session_dir.mkdir()
        write_recipe(session_dir, "recipe.yaml", V2_RECIPE)
        session_manager.get_session_dir.return_value = session_dir

        tool = make_tool(session_manager=session_manager)
        result = await tool._resume_recipe({"session_id": "sess-1"})

        assert result.success is False
        assert result.error["type"] == "V2ResumeUnsupported"
        tool.executor.execute_recipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_validating_a_v2_recipe_refuses_the_legacy_validator(self, temp_dir: Path):
        tool = make_tool()
        recipe = write_recipe(temp_dir, "v2.yaml", V2_RECIPE)

        result = await tool._validate_recipe({"recipe_path": str(recipe)})

        assert result.success is False
        assert result.error["type"] == "V2ValidateUnsupported"

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
    async def test_no_resolver_capability_means_no_roles_offered(self):
        access = await ra.CoordinatorProviderAccess.create(FakeCoordinator(resolver=None))

        assert access.roles() == ()

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
