"""Tests for the public API surface.

These assert the parts of ``recipe-runner-lib.v1`` that an interface layer can
actually prove:

* Core 2 -- ``validate``/``plan``/``run``/``resume`` exist, are async, and are
  usable with no UI and no Amplifier CLI installed.
* Core 3 -- no Amplifier coordinator/session type appears in the exported
  surface, and the package imports with ``amplifier_app_cli`` unimportable.
* Core 4 -- exactly five host ports, none of which carries an agent map.
* Core 8 -- preflight errors are distinct, typed, and name their remedy.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import amplifier_recipe_runner as pkg
from amplifier_recipe_runner import HOST_PORTS
from amplifier_recipe_runner import AgentCollisionError
from amplifier_recipe_runner import ApprovalCallback
from amplifier_recipe_runner import CancellationToken
from amplifier_recipe_runner import EventSink
from amplifier_recipe_runner import ExecutionPlan
from amplifier_recipe_runner import HostServices
from amplifier_recipe_runner import LegacyRecipeError
from amplifier_recipe_runner import LockMode
from amplifier_recipe_runner import ManifestValidationError
from amplifier_recipe_runner import PreflightError
from amplifier_recipe_runner import ProvenanceMismatchError
from amplifier_recipe_runner import ProviderAccess
from amplifier_recipe_runner import RecipeRunner
from amplifier_recipe_runner import RecipeRunnerError
from amplifier_recipe_runner import RunRequest
from amplifier_recipe_runner import RunResult
from amplifier_recipe_runner import RunStatus
from amplifier_recipe_runner import TrustRefusedError
from amplifier_recipe_runner import UndeclaredAgentError
from amplifier_recipe_runner import ValidationReport

PACKAGE_DIR = Path(pkg.__file__).parent
SRC_DIR = PACKAGE_DIR.parent
PUBLIC_MODULES = ("__init__.py", "api.py", "ports.py", "errors.py")

# The deliberate public API. Restated here on purpose: if __init__ drifts, this
# test fails rather than rubber-stamping whatever __init__ happens to say.
ALLOWLIST = {
    "HOST_PORTS",
    "RUN_MANIFEST_VERSION",
    "__version__",
    "plan",
    "resume",
    "run",
    "AgentProvenance",
    "DependencyKind",
    "EffectivePolicy",
    "ExecutionPlan",
    "ExecutionSession",
    "LockMode",
    "RecipeRunner",
    "ResolvedDependency",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "TrustPolicy",
    "ValidationIssue",
    "ValidationReport",
    "ApprovalCallback",
    "ApprovalDecision",
    "ApprovalRequest",
    "CancellationToken",
    "EventSink",
    "HostServices",
    "ProviderAccess",
    "ProviderHandle",
    "RunEvent",
    "WorkspacePath",
    "AgentCollisionError",
    "LegacyRecipeError",
    "ManifestValidationError",
    "PreflightError",
    "ProvenanceMismatchError",
    "RecipeRunnerError",
    "TrustRefusedError",
    "UndeclaredAgentError",
}

PREFLIGHT_ERRORS = (
    UndeclaredAgentError,
    AgentCollisionError,
    TrustRefusedError,
    ProvenanceMismatchError,
    LegacyRecipeError,
    ManifestValidationError,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _protocol_functions(proto: type) -> Iterator[tuple[str, object]]:
    """Yield (name, function) for each member declared on a Protocol."""
    for name, obj in vars(proto).items():
        if name.startswith("_") and name != "__call__":
            continue
        if isinstance(obj, property):
            if obj.fget is not None:
                yield name, obj.fget
        elif inspect.isfunction(obj):
            yield name, obj


def _annotation_strings(obj: type) -> Iterator[str]:
    """Every annotation and parameter name reachable from an exported type."""
    if dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj):
            yield f.name
            yield str(f.type)
    for name, func in _protocol_functions(obj):
        yield name
        for pname, param in inspect.signature(func).parameters.items():
            yield pname
            if param.annotation is not inspect.Parameter.empty:
                yield str(param.annotation)
        yield str(getattr(func, "__annotations__", {}).get("return", ""))


# --------------------------------------------------------------------------
# Core 2 / Core 3 -- exported surface
# --------------------------------------------------------------------------


def test_public_api_matches_allowlist() -> None:
    assert set(pkg.__all__) == ALLOWLIST
    assert len(pkg.__all__) == len(set(pkg.__all__)), "duplicate export"
    for name in pkg.__all__:
        assert hasattr(pkg, name), f"__all__ names {name} but it is not exported"


def test_no_public_name_escapes_all() -> None:
    """A name reachable on the package but absent from __all__ is accidental API."""
    escaped = {
        name
        for name in vars(pkg)
        if not name.startswith("_")
        and name not in pkg.__all__
        and name != "annotations"  # `from __future__ import annotations`
        and not inspect.ismodule(getattr(pkg, name))
    }
    assert escaped == set()


def test_public_modules_import_nothing_from_amplifier() -> None:
    """lib Core 3: no Amplifier import may appear in the exported surface."""
    offenders: list[str] = []
    for filename in PUBLIC_MODULES:
        tree = ast.parse((PACKAGE_DIR / filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] if node.level == 0 else []
            else:
                continue
            offenders += [
                f"{filename}: {n}"
                for n in names
                if n.split(".")[0].startswith("amplifier")
            ]
    assert offenders == []


def test_exported_signatures_have_no_amplifier_session_types() -> None:
    """lib Core 3: coordinator / Amplifier session objects are not public API."""
    forbidden = ("coordinator", "amplifier_app_cli", "amplifier_core", "amplifier.")
    offenders: list[str] = []
    for name in pkg.__all__:
        obj = getattr(pkg, name)
        if not isinstance(obj, type):
            continue
        for text in _annotation_strings(obj):
            low = text.lower()
            offenders += [f"{name}: {text}" for token in forbidden if token in low]
    assert offenders == []


def test_package_imports_without_amplifier_app_cli() -> None:
    """The library must work with no Amplifier CLI installed (lib Core 2)."""
    code = r"""
import sys

BLOCKED = ("amplifier_app_cli", "amplifier_core", "amplifier_foundation", "amplifier")


class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        for blocked in BLOCKED:
            if fullname == blocked or fullname.startswith(blocked + "."):
                raise ImportError("blocked by test: " + fullname)
        return None


sys.meta_path.insert(0, Blocker())

import amplifier_recipe_runner as pkg

for name in pkg.__all__:
    getattr(pkg, name)

leaked = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
assert not leaked, leaked
print("OK", len(pkg.__all__))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC_DIR), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("OK ")


# --------------------------------------------------------------------------
# Core 4 -- host ports
# --------------------------------------------------------------------------


def test_exactly_five_host_ports() -> None:
    assert HOST_PORTS == (
        "provider_access",
        "approval_callback",
        "event_sink",
        "workspace",
        "cancellation",
    )
    assert len(HOST_PORTS) == 5


def test_host_services_fields_are_exactly_the_ports() -> None:
    fields = {f.name for f in dataclasses.fields(HostServices)}
    assert fields == set(HOST_PORTS)


def test_no_port_exposes_an_agent_map() -> None:
    """lib Core 4 / manifest Core 3: the seam has no shape that carries agents."""
    forbidden = ("agent", "session", "coordinator", "catalog")
    offenders: list[str] = []
    port_types = (
        ProviderAccess,
        ApprovalCallback,
        EventSink,
        CancellationToken,
        HostServices,
    )
    for port in port_types:
        for text in _annotation_strings(port):
            low = text.lower()
            offenders += [
                f"{port.__name__}: {text}" for token in forbidden if token in low
            ]
    assert offenders == []


def test_host_services_requires_only_provider_and_workspace(tmp_path: Path) -> None:
    class _Providers:
        def roles(self) -> list[str]:
            return ["general"]

        def resolve(self, role: str) -> object:
            return object()

    services = HostServices(provider_access=_Providers(), workspace=tmp_path)  # type: ignore[arg-type]
    assert services.approval_callback is None
    assert services.event_sink is None
    assert services.cancellation is None
    assert isinstance(services.provider_access, ProviderAccess)


# --------------------------------------------------------------------------
# Core 8 -- error model
# --------------------------------------------------------------------------


def test_preflight_errors_are_distinct_and_typed() -> None:
    assert len(set(PREFLIGHT_ERRORS)) == len(PREFLIGHT_ERRORS)
    for err in PREFLIGHT_ERRORS:
        assert issubclass(err, PreflightError)
        assert issubclass(err, RecipeRunnerError)
    # Distinct: catching one must not catch another.
    for err in PREFLIGHT_ERRORS:
        others = [o for o in PREFLIGHT_ERRORS if o is not err]
        assert not any(issubclass(o, err) for o in others), err


def test_undeclared_agent_error_names_reference_and_remedy() -> None:
    exc = UndeclaredAgentError("foundation:zen-architect", step_id="review")
    assert exc.agent == "foundation:zen-architect"
    assert exc.step_id == "review"
    assert "foundation:zen-architect" in str(exc)
    assert "Remedy:" in str(exc)


def test_agent_collision_error_lists_sources() -> None:
    exc = AgentCollisionError("builder", sources=("uri-a", "uri-b"))
    assert exc.sources == ("uri-a", "uri-b")
    assert "uri-a" in str(exc) and "uri-b" in str(exc)


def test_trust_refusal_states_nothing_was_fetched() -> None:
    exc = TrustRefusedError("https://example.invalid/x", reason="unpinned ref")
    assert exc.source == "https://example.invalid/x"
    assert "Nothing was fetched or activated." in str(exc)


def test_provenance_mismatch_records_both_revisions() -> None:
    exc = ProvenanceMismatchError(
        "git+https://x", expected="aaa", actual="bbb", run_id="r1"
    )
    assert (exc.expected, exc.actual) == ("aaa", "bbb")
    assert "aaa" in str(exc) and "bbb" in str(exc)


def test_legacy_recipe_error_is_actionable() -> None:
    exc = LegacyRecipeError("examples/old.yaml")
    assert "schema_version" in str(exc)
    assert exc.remedy


def test_manifest_validation_error_carries_location() -> None:
    exc = ManifestValidationError(
        "unknown key 'stpes'", recipe="r.yaml", location="steps[0]"
    )
    assert exc.location == "steps[0]"
    assert "steps[0]" in str(exc)


@pytest.mark.parametrize("err", PREFLIGHT_ERRORS)
def test_every_preflight_error_supplies_a_remedy(err: type[PreflightError]) -> None:
    exc = err("subject")
    assert exc.remedy, f"{err.__name__} must name a remedy"


# --------------------------------------------------------------------------
# Core 2 -- runner protocol and data shapes
# --------------------------------------------------------------------------


class _StubRunner:
    """Interface-layer stub: proves the protocol is implementable."""

    async def validate(self, request: RunRequest) -> ValidationReport:
        return ValidationReport(ok=True, schema_version=2)

    async def plan(self, request: RunRequest) -> ExecutionPlan:
        return ExecutionPlan(recipe_digest="sha256:deadbeef", schema_version=2)

    async def run(self, request: RunRequest) -> RunResult:
        if request.services is None:
            raise ValueError("run requires host services")
        return RunResult(run_id="r1", status=RunStatus.SUCCEEDED)

    async def resume(self, run_id: str, services: HostServices) -> RunResult:
        return RunResult(run_id=run_id, status=RunStatus.SUCCEEDED)


def test_recipe_runner_protocol_is_satisfiable() -> None:
    assert isinstance(_StubRunner(), RecipeRunner)


def test_recipe_runner_exposes_four_async_methods() -> None:
    for name in ("validate", "plan", "run", "resume"):
        func = getattr(RecipeRunner, name)
        assert inspect.iscoroutinefunction(func), f"{name} must be async"


def test_validate_and_plan_work_without_host_services() -> None:
    """No UI, no CLI, no ports wired -- validate/plan must still run."""
    runner = _StubRunner()
    request = RunRequest(recipe="examples/demo.yaml")
    assert request.services is None

    report = asyncio.run(runner.validate(request))
    assert isinstance(report, ValidationReport)
    assert report.ok and report.legacy is False

    plan = asyncio.run(runner.plan(request))
    assert isinstance(plan, ExecutionPlan)
    assert plan.manifest_version == pkg.RUN_MANIFEST_VERSION == 1
    assert plan.dependencies == ()
    assert dict(plan.agents) == {}


def test_run_request_defaults_are_ci_safe() -> None:
    request = RunRequest(recipe="r.yaml")
    assert request.lock_mode is LockMode.LOCKED
    assert request.legacy_mode is False
    assert request.trust_policy is None
    assert dict(request.context) == {}


def test_run_request_is_immutable() -> None:
    request = RunRequest(recipe="r.yaml")
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.lock_mode = LockMode.UNLOCKED  # type: ignore[misc]


def test_run_result_reports_failure_honestly() -> None:
    exc = UndeclaredAgentError("missing:agent")
    result = RunResult(run_id="r1", status=RunStatus.FAILED, error=exc)
    assert result.succeeded is False
    assert result.error is exc
    ok = RunResult(run_id="r2", status=RunStatus.SUCCEEDED)
    assert ok.succeeded is True


def test_lock_modes_cover_contract_vocabulary() -> None:
    assert {m.value for m in LockMode} == {"locked", "update-lock", "unlocked"}
