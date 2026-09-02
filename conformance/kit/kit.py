#!/usr/bin/env python3
"""Executable conformance kit for the two DRAFT recipe contracts.

Implements the ``## Conformance`` sections of:

* ``contracts/recipe-dependency-manifest.v1.md``
* ``contracts/recipe-runner-lib.v1.md``

as **discriminating pairs**: GOOD fixtures that must pass against a conforming
implementation, and BAD fixtures that must fail *for the specific named reason*
-- each asserting a distinct typed error, never merely a non-zero exit.

The point of a conformance kit is not that it passes. It is that it **fails
against a knowingly-broken implementation**. Run ``./discriminate.sh`` to see
that proved: it mutates the runner to reintroduce a caller-map fallback, runs
this kit, and reverts.

Usage::

    python kit.py --list                 # fixtures, polarity, clauses, ledger rows
    python kit.py --run                  # run all; exit 1 if any fixture fails
    python kit.py --run --only <id>      # run one
    python kit.py --run --json           # machine-readable results

Everything runs offline against local fixture bundles with injected spawn
backends: no network, no model call, no Foundation required.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

KIT_DIR = Path(__file__).resolve().parent
FIXTURES = KIT_DIR / "fixtures"
RECIPES = FIXTURES / "recipes"
BUNDLES = FIXTURES / "bundles"

sys.path.insert(0, str(KIT_DIR))

from _bootstrap import ensure_runner_importable  # noqa: E402
from _bootstrap import graph_identity  # noqa: E402


# --------------------------------------------------------------------------
# Assertion vocabulary
# --------------------------------------------------------------------------


class KitFailure(AssertionError):
    """One conformance assertion did not hold. Always names what was expected."""


def _brief(value: Any, limit: int = 220) -> str:
    """A repr short enough to read. A 4KB ExecutionPlan dump hides its own point."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}... [{len(text)} chars, truncated]"


def expect(condition: Any, message: str) -> None:
    if not condition:
        raise KitFailure(message)


def expect_eq(actual: Any, expected: Any, what: str) -> None:
    if actual != expected:
        raise KitFailure(f"{what}: expected {expected!r}, got {actual!r}")


def expect_in(needle: str, haystack: str, what: str) -> None:
    if needle not in haystack:
        raise KitFailure(f"{what}: {needle!r} not found in {haystack!r}")


async def expect_raises(exc_type: type[BaseException], coro: Awaitable[Any], what: str) -> BaseException:
    """Await ``coro`` expecting exactly ``exc_type``. A pass is a failure here.

    A BAD fixture that merely observes "something went wrong" would accept a
    typo, an import error, or a fabricated failure as conformance. This demands
    the *named* type.
    """
    try:
        result = await coro
    except exc_type as exc:
        return exc
    except BaseException as exc:  # noqa: BLE001 - the wrong error is a real failure
        raise KitFailure(
            f"{what}: expected {exc_type.__name__}, got {type(exc).__name__}: {exc}"
        ) from exc
    raise KitFailure(
        f"{what}: expected {exc_type.__name__}, but the call SUCCEEDED and returned {_brief(result)}"
    )


def expect_raises_sync(exc_type: type[BaseException], fn: Callable[[], Any], what: str) -> BaseException:
    try:
        result = fn()
    except exc_type as exc:
        return exc
    except BaseException as exc:  # noqa: BLE001
        raise KitFailure(
            f"{what}: expected {exc_type.__name__}, got {type(exc).__name__}: {exc}"
        ) from exc
    raise KitFailure(
        f"{what}: expected {exc_type.__name__}, but the call SUCCEEDED and returned {_brief(result)}"
    )


# --------------------------------------------------------------------------
# Host doubles -- the five ports, and the spawn seam
# --------------------------------------------------------------------------


class Providers:
    """Port 1. Offers a role so the session builds; resolves to nothing real."""

    def roles(self) -> list[str]:
        return ["general"]

    def resolve(self, role: str) -> object:
        return object()


class CollectingSink:
    """Port 3. Records events so a fixture can assert what a run announced."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


class RecordingBackend:
    """Spawn backend double. Records every resolved request; calls no model."""

    def __init__(self, reply: str = "ok") -> None:
        self.requests: list[Any] = []
        self._reply = reply

    async def spawn(self, request: Any) -> str:
        self.requests.append(request)
        return f"{self._reply}:{request.canonical}"

    @property
    def canonicals(self) -> list[str]:
        return [r.canonical for r in self.requests]


class ExplodingBackend:
    """Backend that must never be reached. Reaching it IS the failure."""

    def __init__(self) -> None:
        self.calls = 0

    async def spawn(self, request: Any) -> str:
        self.calls += 1
        raise AssertionError(
            f"spawn backend reached for {request.agent!r}: preflight should have refused first"
        )


class RecordingResolver:
    """Wraps a resolver and counts calls.

    The instrument behind "before any remote fetch": if the resolver was never
    asked for anything, nothing was fetched. That is an observation, not a
    reading of the implementation.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str] = []

    async def resolve(self, dependency: Any, *, workspace: Path | None = None) -> Any:
        self.calls.append(dependency.source)
        return await self._inner.resolve(dependency, workspace=workspace)


_WORKSPACE: list[Path] = []


def workspace() -> Path:
    """A throwaway workspace directory (port 4). Never inside the repo."""
    if not _WORKSPACE:
        _WORKSPACE.append(Path(tempfile.mkdtemp(prefix="recipes-conformance-kit-")))
    return _WORKSPACE[0]


def services(workspace: Path, *, sink: CollectingSink | None = None) -> Any:
    from amplifier_recipe_runner.ports import HostServices

    return HostServices(
        provider_access=Providers(),
        workspace=workspace,
        event_sink=sink,
    )


def local_resolver() -> Any:
    from amplifier_recipe_runner.resolver import LocalBundleResolver

    return LocalBundleResolver(base_path=FIXTURES)


def request_for(recipe: str, *, with_services: bool = False, **kwargs: Any) -> Any:
    from amplifier_recipe_runner.api import RunRequest

    return RunRequest(
        recipe=RECIPES / recipe,
        services=services(workspace()) if with_services else None,
        **kwargs,
    )


async def plan_recipe(recipe: str, *, resolver: Any = None, **kwargs: Any) -> Any:
    from amplifier_recipe_runner.execution import plan as _plan

    return await _plan(request_for(recipe, **kwargs), resolver=resolver or local_resolver())


async def simulated_caller_agents() -> set[str]:
    """The agent map of a CALLER that has no reviewer of any kind.

    Resolved through the same resolver, from a real bundle, so the premise of
    the "runs from a lean caller" fixture is established by measurement rather
    than asserted in prose.
    """
    from amplifier_recipe_runner.manifest import Dependency

    bundle = await local_resolver().resolve(Dependency(source="bundles/lean-caller", kind="bundle"))
    return set(bundle.agents)


# --------------------------------------------------------------------------
# Fixture registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    id: str
    polarity: str
    title: str
    clauses: tuple[str, ...]
    rows: tuple[str, ...]
    run: Callable[[], Awaitable[str]]
    notes: str | None = None


FIXTURES_REGISTRY: list[Fixture] = []


def fixture(
    *,
    id: str,
    polarity: str,
    title: str,
    clauses: tuple[str, ...],
    rows: tuple[str, ...],
    notes: str | None = None,
) -> Callable[[Callable[[], Awaitable[str]]], Callable[[], Awaitable[str]]]:
    def decorate(fn: Callable[[], Awaitable[str]]) -> Callable[[], Awaitable[str]]:
        FIXTURES_REGISTRY.append(
            Fixture(
                id=id,
                polarity=polarity,
                title=title,
                clauses=clauses,
                rows=rows,
                run=fn,
                notes=notes,
            )
        )
        return fn

    return decorate


# ==========================================================================
# GOOD fixtures
# ==========================================================================


@fixture(
    id="good-declared-dependency-runs-from-lean-caller",
    polarity="GOOD",
    title="A recipe declaring its dependency runs from a caller that lacks the agent",
    clauses=(
        "manifest.v1 Core 3",
        "manifest.v1 Core 4",
        "lib.v1 Core 2",
        "lib.v1 Core 4",
    ),
    rows=("RCP-003", "RCP-004", "RCP-102", "RCP-104"),
)
async def good_declared_dependency() -> str:
    from amplifier_recipe_runner.api import RunStatus
    from amplifier_recipe_runner.execution import run as run_recipe

    # Premise, measured: the caller supplies no reviewer.
    caller = await simulated_caller_agents()
    expect(
        "supplier:reviewer" not in caller,
        f"premise broken: the simulated caller already supplies supplier:reviewer ({sorted(caller)})",
    )

    backend = RecordingBackend()
    sink = CollectingSink()

    from amplifier_recipe_runner.api import RunRequest

    result = await run_recipe(
        RunRequest(recipe=RECIPES / "declared.yaml", services=services(workspace(), sink=sink)),
        resolver=local_resolver(),
        spawn_backend=backend,
    )

    expect_eq(result.status, RunStatus.SUCCEEDED, "run status")
    expect_eq(result.completed_steps, ("review",), "completed steps")
    expect_eq(backend.canonicals, ["supplier:reviewer"], "agents actually invoked")

    assert result.plan is not None
    provenance = result.plan.agents.get("supplier:reviewer")
    expect(provenance is not None, "plan records no provenance for supplier:reviewer")
    assert provenance is not None
    expect_eq(provenance.supplied_by, "bundles/supplier", "supplying dependency")
    expect_in(
        str(BUNDLES / "supplier"),
        str(provenance.local_path),
        "agent resolved outside the declared dependency",
    )
    expect("session:ready" in sink.kinds, f"no session:ready event emitted (got {sink.kinds})")

    return (
        f"caller roster {sorted(caller)} lacks supplier:reviewer; the run still succeeded, "
        f"resolving it from {provenance.supplied_by!r}"
    )


@fixture(
    id="good-identical-resolved-graph-across-hosts",
    polarity="GOOD",
    title="Two independent hosts produce identical resolved-graph identity",
    clauses=("lib.v1 Core 1", "lib.v1 Core 7", "manifest.v1 Core 7"),
    rows=("RCP-101", "RCP-107", "RCP-007"),
    notes=(
        "Host B is a separate OS process. It prefers the real `recipe-runner` CLI "
        "when importable and falls back to conformance/kit/host_adapter.py, "
        "reporting which surface it used. The Amplifier tool adapter is not yet a "
        "runner host, so it is not compared -- see kit README residual R1."
    ),
)
async def good_identical_graph_across_hosts() -> str:
    import importlib.util

    in_process = graph_identity(await plan_recipe("declared.yaml"))

    expect(in_process["dependencies"], "resolved graph has no dependencies; comparison would be vacuous")
    expect(in_process["agents"], "resolved graph has no agents; comparison would be vacuous")

    cli_available = importlib.util.find_spec("amplifier_recipe_runner.cli") is not None
    if cli_available:  # pragma: no cover - the CLI module does not exist yet
        argv = [sys.executable, "-m", "amplifier_recipe_runner.cli", "plan", "--json", str(RECIPES / "declared.yaml")]
        surface = "amplifier_recipe_runner.cli"
    else:
        argv = [
            sys.executable,
            str(KIT_DIR / "host_adapter.py"),
            "--recipe",
            str(RECIPES / "declared.yaml"),
            "--fixtures",
            str(FIXTURES),
        ]
        surface = "conformance/kit/host_adapter.py (standalone process)"

    completed = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    expect_eq(completed.returncode, 0, f"second host exited non-zero: {completed.stderr.strip()}")
    out_of_process = json.loads(completed.stdout)

    if in_process != out_of_process:
        differing = sorted(
            key for key in set(in_process) | set(out_of_process)
            if in_process.get(key) != out_of_process.get(key)
        )
        raise KitFailure(
            f"resolved-graph identity differs between hosts on {differing}: "
            f"in-process={json.dumps({k: in_process.get(k) for k in differing}, sort_keys=True)} "
            f"other-host={json.dumps({k: out_of_process.get(k) for k in differing}, sort_keys=True)}"
        )

    return (
        f"identical across in-process library and {surface}: "
        f"recipe_digest={in_process['recipe_digest'][:19]}..., "
        f"{len(in_process['dependencies'])} dependency, {len(in_process['agents'])} agent(s)"
    )


@fixture(
    id="good-behavior-partial-composes-only-declared-contribution",
    polarity="GOOD",
    title="A behavior partial contributes only what it declares",
    clauses=("manifest.v1 Core 2", "manifest.v1 Core 7"),
    rows=("RCP-002", "RCP-007"),
)
async def good_behavior_partial() -> str:
    partial = await plan_recipe("behavior-partial.yaml")
    whole = await plan_recipe("declared.yaml")

    partial_agents = {name for name, prov in partial.agents.items() if prov.alias is None}
    whole_agents = {name for name, prov in whole.agents.items() if prov.alias is None}

    # Control: the partial must narrow something REAL. If the full bundle did
    # not supply summarizer either, the fixture would pass vacuously.
    expect(
        "supplier:summarizer" in whole_agents,
        f"control broken: the whole bundle does not supply supplier:summarizer ({sorted(whole_agents)})",
    )
    expect_eq(partial_agents, {"supplier:reviewer"}, "behavior partial's contributed roster")

    expect_eq(len(partial.dependencies), 1, "declared dependency count")
    dependency = partial.dependencies[0]
    expect_eq(str(dependency.kind.value), "behavior", "dependency kind")
    expect_eq(dependency.subdirectory, "behaviors/review-only.yaml", "recorded partial subdirectory")
    return (
        f"partial contributed {sorted(partial_agents)}; the same bundle whole contributes "
        f"{sorted(whole_agents)} -- summarizer correctly excluded"
    )


@fixture(
    id="good-plan-reports-provenance-without-executing-anything",
    polarity="GOOD",
    title="plan() names every agent's supplying dependency and touches nothing",
    clauses=("lib.v1 Core 2", "lib.v1 Core 7", "manifest.v1 Core 7"),
    rows=("RCP-102", "RCP-107", "RCP-007"),
)
async def good_plan_is_side_effect_free() -> str:
    import tempfile

    from amplifier_recipe_runner.api import RunRequest
    from amplifier_recipe_runner.execution import plan as plan_only

    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp)
        resolved = await plan_only(
            RunRequest(recipe=RECIPES / "declared.yaml", services=services(empty)),
            resolver=local_resolver(),
        )
        leftovers = sorted(p.name for p in empty.iterdir())
        expect_eq(leftovers, [], "plan() wrote into the workspace")

    # Core 7's record is a closed list; assert field presence per item rather
    # than a subjective "enough provenance" judgement.
    for name, prov in resolved.agents.items():
        expect(prov.supplied_by, f"agent {name!r} has no supplying dependency recorded")
        expect(prov.local_path, f"agent {name!r} records no local path")
        expect(
            prov.resolved_revision or prov.dependency_digest,
            f"agent {name!r} records neither a revision nor a content digest",
        )
    for dependency in resolved.dependencies:
        expect(dependency.uri, "a dependency recorded no declared URI")
        expect(
            dependency.resolved_revision or dependency.content_digest,
            f"dependency {dependency.uri!r} recorded no immutable identity",
        )
    expect(resolved.recipe_digest.startswith("sha256:"), "recipe digest is not a sha256")
    expect(resolved.runner_version, "plan records no runner version")
    expect_eq(resolved.step_ids, ("review",), "recorded step ids")
    expect(resolved.policy is not None, "plan records no effective policy")
    assert resolved.policy is not None
    expect(resolved.policy.isolated, "plan reports a non-isolated policy for a schema-2 recipe")

    # And it works with NO host wiring at all (lib Core 2).
    bare = await plan_only(
        RunRequest(recipe=RECIPES / "declared.yaml"),
        resolver=local_resolver(),
    )
    expect_eq(bare.recipe_digest, resolved.recipe_digest, "planning without host services changed the graph")

    return (
        f"planned {len(resolved.dependencies)} dependency and {len(resolved.agents)} agent(s) with full "
        "provenance; workspace untouched; identical with no host services supplied"
    )


@fixture(
    id="good-injected-offline-resolver-satisfies-a-locked-run",
    polarity="GOOD",
    title="An embedder-injected offline resolver satisfies a locked run",
    clauses=("lib.v1 Core 5", "manifest.v1 Core 8"),
    rows=("RCP-105", "RCP-008"),
    notes="No network is reachable in this fixture by construction: the resolver only reads local paths.",
)
async def good_injected_offline_resolver() -> str:
    import tempfile

    from amplifier_recipe_runner.api import LockMode
    from amplifier_recipe_runner.lockfile import apply_lock_mode
    from amplifier_recipe_runner.resolver import FoundationResolver
    from amplifier_recipe_runner.resolver import LocalBundleResolver

    injected = local_resolver()
    expect(
        isinstance(injected, LocalBundleResolver) and not isinstance(injected, FoundationResolver),
        "the fixture is not actually injecting a non-default resolver",
    )

    recording = RecordingResolver(injected)
    resolved = await plan_recipe("declared.yaml", resolver=recording)
    expect_eq(recording.calls, ["bundles/supplier"], "resolver call log")

    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "declared.lock.yaml"

        generated = apply_lock_mode(resolved, path=lock_path, mode=LockMode.UPDATE_LOCK)
        expect(generated.rewritten, "update-lock did not write the lockfile")
        expect(lock_path.is_file(), "update-lock produced no lockfile")

        verified = apply_lock_mode(resolved, path=lock_path, mode=LockMode.LOCKED)
        expect(not verified.rewritten, "locked mode rewrote the lock -- locks are never updated silently")
        expect_eq(verified.warnings, (), f"locked verification warned: {verified.warnings}")

        # `unlocked` must warn rather than silently pin nothing.
        relaxed = apply_lock_mode(resolved, path=lock_path, mode=LockMode.UNLOCKED)
        expect(relaxed.warnings, "unlocked mode produced no warning")
        expect_in("interactive only", " ".join(relaxed.warnings), "unlocked warning does not say it is interactive only")

    return (
        "offline LocalBundleResolver satisfied update-lock then locked verification with no network; "
        "locked mode did not rewrite, unlocked warned"
    )


# ==========================================================================
# BAD fixtures -- each asserts a SPECIFIC typed error
# ==========================================================================


@fixture(
    id="bad-undeclared-agent-fails-preflight-before-side-effects",
    polarity="BAD",
    title="UndeclaredAgentError, raised before any step or spawn",
    clauses=("manifest.v1 Core 3", "manifest.v1 Core 6", "lib.v1 Core 8"),
    rows=("RCP-003", "RCP-006", "RCP-108"),
    notes="The undeclared name is one the CALLER supplies, so a caller-map fallback would satisfy it.",
)
async def bad_undeclared_agent() -> str:
    from amplifier_recipe_runner.api import RunRequest
    from amplifier_recipe_runner.api import RunStatus
    from amplifier_recipe_runner.errors import UndeclaredAgentError
    from amplifier_recipe_runner.execution import run as run_recipe

    caller = await simulated_caller_agents()
    expect(
        "lean-caller:packager" in caller,
        "premise broken: the caller does not supply the undeclared name, so this proves nothing",
    )

    # Path 1: plan() refuses outright.
    exc = await expect_raises(
        UndeclaredAgentError,
        plan_recipe("undeclared.yaml"),
        "planning a recipe with an undeclared agent",
    )
    assert isinstance(exc, UndeclaredAgentError)
    expect_eq(exc.agent, "lean-caller:packager", "named undeclared agent")
    expect(exc.remedy, "UndeclaredAgentError carries no remedy")
    expect_in("dependencies", str(exc), "error text does not name the remedy")

    # Path 2: run() refuses with NO step executed and the backend never reached.
    backend = ExplodingBackend()
    sink = CollectingSink()
    result = await run_recipe(
        RunRequest(recipe=RECIPES / "undeclared.yaml", services=services(workspace(), sink=sink)),
        resolver=local_resolver(),
        spawn_backend=backend,
    )
    expect_eq(result.status, RunStatus.FAILED, "run status")
    expect(
        isinstance(result.error, UndeclaredAgentError),
        f"run reported {type(result.error).__name__}, not UndeclaredAgentError",
    )
    expect_eq(result.completed_steps, (), "completed steps (a refused run must run none)")
    expect_eq(backend.calls, 0, "spawn backend invocations before refusal")
    expect(
        "session:ready" not in sink.kinds,
        f"a session was built despite preflight refusal (events: {sink.kinds})",
    )
    return (
        f"UndeclaredAgentError({exc.agent!r}) from plan(); run() FAILED with the same type, "
        f"0 steps completed, 0 spawns, no session built"
    )


@fixture(
    id="bad-colliding-declared-dependencies-fail-preflight",
    polarity="BAD",
    title="AgentCollisionError, naming both supplying dependencies",
    clauses=("manifest.v1 Core 5",),
    rows=("RCP-005", "RCP-108"),
)
async def bad_collision() -> str:
    from amplifier_recipe_runner.errors import AgentCollisionError

    exc = await expect_raises(
        AgentCollisionError,
        plan_recipe("collision.yaml"),
        "planning a recipe whose dependencies collide",
    )
    assert isinstance(exc, AgentCollisionError)
    expect_eq(exc.agent, "supplier:reviewer", "colliding agent name")
    expect_eq(
        set(exc.sources),
        {"bundles/supplier", "bundles/impostor"},
        "collision must name BOTH sources, not pick a winner",
    )
    expect_in("precedence", str(exc), "error text does not say collisions are never resolved by precedence")
    return f"AgentCollisionError({exc.agent!r}) naming {sorted(exc.sources)} -- no precedence applied"


@fixture(
    id="bad-colliding-caller-agent-cannot-alter-the-result",
    polarity="BAD",
    title="A host-supplied colliding agent map is discarded, visibly",
    clauses=("manifest.v1 Core 3", "manifest.v1 Core 5", "lib.v1 Core 4"),
    rows=("RCP-003", "RCP-005", "RCP-104"),
    notes="Passes the impostor through `agent_configs` -- the exact argument a real Amplifier host uses.",
)
async def bad_colliding_caller_agent() -> str:
    from amplifier_recipe_runner.execution import PlanCatalog
    from amplifier_recipe_runner.execution import PlanCatalogSpawnAdapter
    from amplifier_recipe_runner.manifest import Dependency

    resolved = await plan_recipe("declared.yaml")
    catalog = PlanCatalog.from_plan(resolved)
    backend = RecordingBackend()
    adapter = PlanCatalogSpawnAdapter(
        catalog,
        backend,
        run_id="kit-collision",
        workspace=workspace(),
    )

    # The host's colliding catalog, built from a REAL impostor bundle so its
    # definition is genuinely different from the declared one.
    impostor = await local_resolver().resolve(Dependency(source="bundles/impostor", kind="bundle"))
    host_agent_configs = {
        name: {"name": name, "local_path": agent.local_path} for name, agent in impostor.agents.items()
    }
    expect(
        "supplier:reviewer" in host_agent_configs,
        "premise broken: the impostor does not supply the colliding name",
    )

    outcome = await adapter(
        "supplier:reviewer",
        "Review the change.",
        parent_session=object(),
        agent_configs=host_agent_configs,
        step_id="review",
    )

    expect_eq(outcome["supplied_by"], "bundles/supplier", "agent's supplying dependency after host offer")
    expect_eq(len(backend.requests), 1, "spawn count")
    definition = backend.requests[0].definition
    expect_in(
        str(BUNDLES / "supplier"),
        str(definition["local_path"]),
        "resolved definition came from outside the declared dependency",
    )
    expect(
        str(BUNDLES / "impostor") not in str(definition["local_path"]),
        f"the impostor's definition was used: {definition['local_path']!r}",
    )
    expect_in("supplier:reviewer", ",".join(adapter.ignored_host_agents), "host agent map was not recorded as ignored")
    expect_in("agent_configs", ",".join(adapter.ignored_arguments), "agent_configs was not recorded as discarded")
    expect_in("parent_session", ",".join(adapter.ignored_arguments), "parent_session was not recorded as discarded")
    return (
        f"host offered {sorted(host_agent_configs)}; adapter ignored {list(adapter.ignored_host_agents)} "
        f"and resolved from {outcome['supplied_by']!r}"
    )


@fixture(
    id="bad-locked-resume-with-changed-revision-fails-visibly",
    polarity="BAD",
    title="ProvenanceMismatchError on both the resume and the locked path",
    clauses=("manifest.v1 Core 8", "lib.v1 Core 7"),
    rows=("RCP-008", "RCP-107"),
)
async def bad_locked_resume_mismatch() -> str:
    import tempfile

    from amplifier_recipe_runner.errors import ProvenanceMismatchError
    from amplifier_recipe_runner.lockfile import apply_lock_mode
    from amplifier_recipe_runner.lockfile import lock_from_plan
    from amplifier_recipe_runner.lockfile import write_lock
    from amplifier_recipe_runner.provenance import check_resume_provenance
    from amplifier_recipe_runner.provenance import run_manifest_from_plan

    other_revision = "0" * 40
    resolved = await plan_recipe("declared.yaml")

    # Control: an unmodified record resumes cleanly. Without this the fixture
    # could pass because EVERY resume fails, which proves nothing.
    faithful = run_manifest_from_plan(resolved, run_id="kit-resume", created_at="fixed")
    check_resume_provenance(faithful, resolved)

    drifted = dataclasses.replace(
        faithful,
        dependencies=tuple(
            dataclasses.replace(dep, resolved_revision=other_revision, content_digest=None)
            for dep in faithful.dependencies
        ),
    )
    exc = expect_raises_sync(
        ProvenanceMismatchError,
        lambda: check_resume_provenance(drifted, resolved),
        "resuming a run whose dependency resolved to a different revision",
    )
    assert isinstance(exc, ProvenanceMismatchError)
    expect_eq(exc.source, "bundles/supplier", "mismatch names the wrong source")
    expect_eq(exc.expected, other_revision, "mismatch does not report the RECORDED identity")
    expect(
        exc.actual not in (None, other_revision),
        f"mismatch does not report the freshly resolved identity (got {exc.actual!r})",
    )
    expect_in(
        "refusing to re-resolve",
        str(exc).lower(),
        "error text does not state that it refuses to re-resolve silently",
    )

    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "recipe.lock.yaml"

        # Control: a faithful lock verifies in `locked` mode.
        write_lock(lock_path, lock_from_plan(resolved))
        apply_lock_mode(resolved, path=lock_path, mode="locked")

        stale = lock_from_plan(resolved)
        write_lock(
            lock_path,
            dataclasses.replace(
                stale,
                entries=tuple(
                    dataclasses.replace(entry, resolved_revision=other_revision, content_digest=None)
                    for entry in stale.entries
                ),
            ),
        )
        lock_exc = expect_raises_sync(
            ProvenanceMismatchError,
            lambda: apply_lock_mode(resolved, path=lock_path, mode="locked"),
            "running `locked` against a lock pinning a different revision",
        )
        assert isinstance(lock_exc, ProvenanceMismatchError)
        expect_eq(lock_exc.expected, other_revision, "locked-mode mismatch does not report the pinned identity")
        expect(lock_path.read_text(encoding="utf-8").count(other_revision) > 0, "locked mode rewrote the lock")

    return (
        f"resume and locked mode both raised ProvenanceMismatchError "
        f"(expected={other_revision[:8]}..., actual={str(exc.actual)[:8]}...); "
        "faithful controls passed, and locked mode did not rewrite the lock"
    )


@fixture(
    id="bad-trust-disallowed-dependency-refused-before-any-fetch",
    polarity="BAD",
    title="TrustRefusedError, with the resolver never called at all",
    clauses=("manifest.v1 Core 6", "lib.v1 Core 6"),
    rows=("RCP-006", "RCP-106"),
    notes="The recipe declares a permitted LOCAL dependency FIRST; zero resolver calls proves ordering.",
)
async def bad_trust_refusal() -> str:
    from amplifier_recipe_runner.errors import TrustRefusedError
    from amplifier_recipe_runner.trust import TrustPolicy

    policy = TrustPolicy.ci(allowed_hosts=("github.com",))
    recording = RecordingResolver(local_resolver())

    exc = await expect_raises(
        TrustRefusedError,
        plan_recipe("untrusted.yaml", resolver=recording, trust_policy=policy),
        "planning a recipe whose dependency the trust policy disallows",
    )
    assert isinstance(exc, TrustRefusedError)
    expect_eq(exc.source, "git+https://blocked.example.invalid/pkg@main", "refused source")
    expect_eq(exc.policy, "ci", "policy name recorded on the refusal")
    expect_in("Nothing was fetched", str(exc), "error does not state that nothing was fetched")
    expect_eq(
        recording.calls,
        [],
        "the resolver was called before the refusal -- a side effect ahead of a trust decision",
    )

    # Control: the same recipe under a policy that permits the host still fails
    # only at resolution, which proves the refusal above was the TRUST rule and
    # not an unrelated error.
    permissive = TrustPolicy.interactive(allowed_hosts=None)
    control = RecordingResolver(local_resolver())
    try:
        await plan_recipe("untrusted.yaml", resolver=control, trust_policy=permissive)
    except TrustRefusedError as unexpected:  # pragma: no cover - would be a real defect
        raise KitFailure(f"permissive policy still refused: {unexpected}") from unexpected
    except Exception:  # noqa: BLE001 - resolution of a fake host is expected to fail
        pass
    expect(
        control.calls != [],
        "under a permitting policy the resolver was still never called; the fixture is not isolating trust",
    )

    return (
        f"TrustRefusedError({exc.source!r}) by policy {exc.policy!r} with 0 resolver calls; "
        f"a permitting policy reached the resolver ({len(control.calls)} call(s))"
    )


@fixture(
    id="bad-legacy-recipe-rejected-by-the-standalone-surface",
    polarity="BAD",
    title="LegacyRecipeError, in-process and from a standalone host process",
    clauses=("manifest.v1 Core 1", "manifest.v1 Core 10"),
    rows=("RCP-001", "RCP-010"),
    notes="Legacy handling belongs to the labeled Amplifier adapter; the standalone surface must reject.",
)
async def bad_legacy_rejected() -> str:
    from amplifier_recipe_runner.errors import LegacyRecipeError
    from amplifier_recipe_runner.manifest import LegacyRecipe
    from amplifier_recipe_runner.manifest import parse_manifest_file

    parsed = parse_manifest_file(RECIPES / "legacy.yaml")
    expect(
        isinstance(parsed, LegacyRecipe),
        f"premise broken: fixture recipe parsed as {type(parsed).__name__}, not LegacyRecipe",
    )

    exc = await expect_raises(
        LegacyRecipeError,
        plan_recipe("legacy.yaml"),
        "planning a legacy recipe on the standalone surface",
    )
    assert isinstance(exc, LegacyRecipeError)
    expect(exc.remedy, "LegacyRecipeError carries no remedy")
    expect_in("schema_version", str(exc), "error text does not name the remedy")

    completed = subprocess.run(
        [
            sys.executable,
            str(KIT_DIR / "host_adapter.py"),
            "--recipe",
            str(RECIPES / "legacy.yaml"),
            "--fixtures",
            str(FIXTURES),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    expect_eq(completed.returncode, 2, f"standalone host exit code (stderr: {completed.stderr.strip()})")
    reported = json.loads(completed.stdout)
    expect_eq(reported["error"], "LegacyRecipeError", "standalone host reported the wrong error type")

    return (
        "LegacyRecipeError in-process and from a standalone host process "
        f"(exit 2, error={reported['error']!r})"
    )


# --------------------------------------------------------------------------
# Ledger coverage -- authored judgements, emitted as ledger-map.yaml
# --------------------------------------------------------------------------
#
# The reconciler consumes `ledger-map.yaml`; this dict is where its content is
# decided. Fixture->row wiring is derived from the registry above (so it cannot
# drift), but "how much of this clause does the kit actually check?" is a
# judgement and is written down here, per row, in full.
#
# `covered` and `not_covered` are BOTH required for a partial row. A map that
# only says what is covered reads as full coverage, which is exactly the
# overclaim a conformance ledger exists to prevent.

LEDGER_COVERAGE: dict[str, dict[str, Any]] = {
    "RCP-001": {
        "coverage": "partial",
        "covered": "A recipe with no `schema_version` parses as a LegacyRecipe marker, not silently as a manifest.",
        "not_covered": "Unknown manifest keys as a parse ERROR, and the `dependencies`-block requirement, have no fixture.",
    },
    "RCP-002": {
        "coverage": "partial",
        "covered": "`kind: behavior` with a `#subdirectory=` partial resolves and is recorded; `required_agents` is exercised on a bundle dependency.",
        "not_covered": "Rejection of `kind` values outside bundle|behavior, and malformed dependency entries, have no fixture.",
    },
    "RCP-003": {
        "coverage": "full",
        "covered": "Closed-world resolution proved three ways: a run succeeds from a caller lacking the agent; an undeclared reference raises UndeclaredAgentError; a host-supplied colliding agent map is discarded. Both halves are discrimination-proved (mutations/caller-map-fallback.patch, mutations/host-agent-precedence.patch).",
        "not_covered": None,
    },
    "RCP-004": {
        "coverage": "partial",
        "covered": "The execution session is built from the plan catalog alone; every host argument that could widen it (agent_configs, parent_session, inheritance kwargs) is discarded and recorded.",
        "not_covered": "An enumerated absence probe over the runner's whole host surface -- proving NO host import exists beyond the five ports -- is not implemented.",
    },
    "RCP-005": {
        "coverage": "full",
        "covered": "Two declared dependencies supplying one name raise AgentCollisionError naming BOTH sources; a colliding caller agent cannot alter the resolved definition.",
        "not_covered": None,
    },
    "RCP-006": {
        "coverage": "full",
        "covered": "Trust refusal happens with ZERO resolver calls even though a permitted local dependency is declared first; a missing declaration fails naming the reference and the remedy, with no step run and no session built.",
        "not_covered": None,
    },
    "RCP-007": {
        "coverage": "partial",
        "covered": "Field presence asserted per item: recipe digest, declared URI, immutable identity, agent->dependency provenance, runner version, effective policy, recorded partial subdirectory.",
        "not_covered": "Effective capability policy contents are not asserted (see RCP-009); foundation_version is recorded but not required.",
    },
    "RCP-008": {
        "coverage": "partial",
        "covered": "locked verifies without rewriting; update-lock rewrites explicitly; unlocked warns and is named interactive-only; a resume against a changed revision raises ProvenanceMismatchError naming both identities, and so does locked mode against a stale lock. Faithful controls pass, so the failures are discriminating.",
        "not_covered": "The library exposes no `resume` entry point (residual R2), so the clause is checked through `check_resume_provenance` rather than through the API a host would call.",
    },
    "RCP-009": {
        "coverage": "none",
        "covered": None,
        "not_covered": "Capability intersection (host n runner n manifest) has no fixture. The manifest schema has no capability-declaration field yet, so the third term of the intersection is not addressable.",
    },
    "RCP-010": {
        "coverage": "partial",
        "covered": "The standalone surface rejects a legacy recipe with LegacyRecipeError and an actionable remedy, in-process AND from a separate host process.",
        "not_covered": "The labeled caller-bound adapter mode does not exist (residual R2: RunRequest.legacy_mode is accepted and ignored), so neither the deprecation warning nor the byte-identical-behaviour half is checkable here. The byte-identical baseline is recipes-o6f's deliverable (conformance/legacy-compat/).",
    },
    "RCP-011": {
        "coverage": "none",
        "covered": None,
        "not_covered": "No absence probe for namespace inference. The row's disposition is OPEN-PINNED pending an interpretive ruling, so the kit deliberately does not force one.",
    },
    "RCP-012": {
        "coverage": "none",
        "covered": None,
        "not_covered": "The kit exercises the runner, which rejects `agent_config` at parse. The row's VIOLATION is about the shipped tool module (modules/tool-recipes/.../models.py), a surface this kit does not drive.",
    },
    "RCP-101": {
        "coverage": "partial",
        "covered": "Two independent hosts (in-process library, separate process) produce byte-identical resolved-graph identity, so neither carries resolution logic of its own.",
        "not_covered": "The ledger records 'every host surface is a thin adapter' as NOT-ASSERTABLE (architectural judgement). Only two hosts exist to compare; the Amplifier tool adapter is not yet a runner host (residual R1).",
    },
    "RCP-102": {
        "coverage": "partial",
        "covered": "`plan` and `run` are exercised with no UI and no Amplifier CLI; `plan` is proved side-effect free (empty workspace after) and works with no host services at all.",
        "not_covered": "`validate` and `resume` are absent from the shipped surface (residual R2), so two of the four required entry points have no fixture.",
    },
    "RCP-103": {
        "coverage": "none",
        "covered": None,
        "not_covered": "No absence probe over the exported surface for `coordinator` / Amplifier-internal session objects. The kit uses the neutral session throughout, which is evidence but not an assertion.",
    },
    "RCP-104": {
        "coverage": "partial",
        "covered": "No port carries agents in practice: the spawn adapter discards and records agent_configs, parent_session, and inheritance kwargs, and still resolves from the plan.",
        "not_covered": "An enumerated probe that HOST_PORTS is exactly the five contract names, and that no port signature exposes an agent map, is not implemented.",
    },
    "RCP-105": {
        "coverage": "partial",
        "covered": "An embedder-injected offline resolver satisfies update-lock then locked verification with no network, proving the resolver interface is genuinely injectable.",
        "not_covered": "The default Foundation BundleRegistry implementation is not exercised (it needs Foundation and network). The ledger records the cache-location sub-claim as NOT-ASSERTABLE.",
    },
    "RCP-106": {
        "coverage": "full",
        "covered": "A CI trust policy refuses a disallowed host before any fetch (zero resolver calls), and a permitting policy reaches the resolver -- so the refusal is the trust rule and not an unrelated failure.",
        "not_covered": None,
    },
    "RCP-107": {
        "coverage": "partial",
        "covered": "The documented run-manifest shape is compared field-for-field across two hosts and asserted non-vacuous; required fields are asserted present.",
        "not_covered": "Stability ACROSS VERSIONS (a committed schema snapshot compared over time) is not checked; only stability across hosts at one revision.",
    },
    "RCP-108": {
        "coverage": "full",
        "covered": "All four named preflight classes are asserted by type, not by exit code: UndeclaredAgentError, AgentCollisionError, TrustRefusedError, ProvenanceMismatchError. 'Never a fabricated success' is asserted as FAILED status with 0 completed steps and 0 spawns on a refused run.",
        "not_covered": None,
    },
}

#: Rows the kit deliberately does not touch, with the reason recorded.
UNCOVERED_ROWS: dict[str, str] = {
    "RCP-000": "SYNC row. It asserts no clause; contract-hash verification belongs to the reconciler, not to an executable fixture.",
}


def ledger_map() -> dict[str, Any]:
    """Build the reconciler-consumable map. Fails loud on an unmapped row."""
    by_row: dict[str, list[str]] = {}
    for item in FIXTURES_REGISTRY:
        for row in item.rows:
            by_row.setdefault(row, []).append(item.id)

    unmapped = sorted(set(by_row) - set(LEDGER_COVERAGE))
    if unmapped:
        raise SystemExit(
            f"fixtures cite ledger rows with no authored coverage judgement: {unmapped}. "
            "Add them to LEDGER_COVERAGE -- an unexplained row is an overclaim."
        )

    rows: dict[str, Any] = {}
    for row, judgement in sorted(LEDGER_COVERAGE.items()):
        entry: dict[str, Any] = {
            "coverage": judgement["coverage"],
            "fixtures": sorted(by_row.get(row, [])),
        }
        if judgement.get("covered"):
            entry["covered"] = judgement["covered"]
        if judgement.get("not_covered"):
            entry["not_covered"] = judgement["not_covered"]
        rows[row] = entry
    for row, reason in sorted(UNCOVERED_ROWS.items()):
        rows[row] = {"coverage": "none", "fixtures": [], "not_covered": reason}
    return {
        "kit": {
            "entrypoint": "conformance/kit/kit.py",
            "run": "python3 conformance/kit/kit.py --run",
            "list": "python3 conformance/kit/kit.py --list",
            "discrimination_proof": "conformance/kit/discriminate.sh",
            "generated_by": "python3 conformance/kit/kit.py --ledger-map",
        },
        "contracts": [
            "contracts/recipe-dependency-manifest.v1.md",
            "contracts/recipe-runner-lib.v1.md",
        ],
        "fixtures": [
            {
                "id": item.id,
                "polarity": item.polarity,
                "title": item.title,
                "clauses": list(item.clauses),
                "satisfies": list(item.rows),
                **({"notes": item.notes} if item.notes else {}),
            }
            for item in FIXTURES_REGISTRY
        ],
        "rows": rows,
    }


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


@dataclass
class Result:
    fixture: Fixture
    passed: bool
    detail: str
    trace: str | None = None
    duration_s: float = 0.0


async def run_fixture(item: Fixture) -> Result:
    import time

    started = time.monotonic()
    try:
        detail = await item.run()
        return Result(item, True, detail, duration_s=time.monotonic() - started)
    except KitFailure as exc:
        return Result(item, False, str(exc), duration_s=time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 - an unexpected error is still a failure
        return Result(
            item,
            False,
            f"unexpected {type(exc).__name__}: {exc}",
            trace=traceback.format_exc(),
            duration_s=time.monotonic() - started,
        )


async def run_all(only: str | None) -> list[Result]:
    selected = [f for f in FIXTURES_REGISTRY if only is None or f.id == only]
    if only is not None and not selected:
        raise SystemExit(f"no such fixture: {only!r}. Run --list to see the ids.")
    return [await run_fixture(item) for item in selected]


def cmd_list(as_json: bool) -> int:
    if as_json:
        json.dump(
            [
                {
                    "id": f.id,
                    "polarity": f.polarity,
                    "title": f.title,
                    "clauses": list(f.clauses),
                    "ledger_rows": list(f.rows),
                    "notes": f.notes,
                }
                for f in FIXTURES_REGISTRY
            ],
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    good = [f for f in FIXTURES_REGISTRY if f.polarity == "GOOD"]
    bad = [f for f in FIXTURES_REGISTRY if f.polarity == "BAD"]
    print(f"conformance kit: {len(FIXTURES_REGISTRY)} fixtures ({len(good)} GOOD, {len(bad)} BAD)\n")
    for group, items in (("GOOD", good), ("BAD", bad)):
        print(f"{group}")
        for f in items:
            print(f"  {f.id}")
            print(f"      {f.title}")
            print(f"      clauses: {', '.join(f.clauses)}")
            print(f"      ledger:  {', '.join(f.rows)}")
            if f.notes:
                print(f"      note:    {f.notes}")
        print()
    return 0


def cmd_ledger_map() -> int:
    import yaml

    print("# GENERATED by `python3 conformance/kit/kit.py --ledger-map` -- do not hand-edit.")
    print("#")
    print("# Wires this repo's executable conformance kit to conformance/ledger.yaml.")
    print("# The kit does NOT edit the ledger; the reconciler consumes this map and")
    print("# decides dispositions. `coverage: partial` always names what is NOT covered.")
    print("#")
    print(f"# fixtures: {len(FIXTURES_REGISTRY)} "
          f"({sum(1 for f in FIXTURES_REGISTRY if f.polarity == 'GOOD')} GOOD, "
          f"{sum(1 for f in FIXTURES_REGISTRY if f.polarity == 'BAD')} BAD)")
    yaml.safe_dump(ledger_map(), sys.stdout, sort_keys=False, width=100, default_flow_style=False)
    return 0


def cmd_run(only: str | None, as_json: bool) -> int:
    provenance = ensure_runner_importable()
    results = asyncio.run(run_all(only))
    failed = [r for r in results if not r.passed]

    if as_json:
        json.dump(
            {
                "implementation": provenance,
                "total": len(results),
                "passed": len(results) - len(failed),
                "failed": len(failed),
                "results": [
                    {
                        "id": r.fixture.id,
                        "polarity": r.fixture.polarity,
                        "status": "PASS" if r.passed else "FAIL",
                        "ledger_rows": list(r.fixture.rows),
                        "detail": r.detail,
                    }
                    for r in results
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 1 if failed else 0

    print(f"implementation under test: {provenance}\n")
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{mark}] {r.fixture.polarity:<4} {r.fixture.id}  ({r.duration_s:.2f}s)")
        print(f"       {r.detail}")
        if r.trace:
            print("       " + r.trace.replace("\n", "\n       ").rstrip())
    print()
    print(f"{len(results) - len(failed)}/{len(results)} fixtures passed")
    if failed:
        print("\nFAILED:")
        for r in failed:
            print(f"  - {r.fixture.id} [{', '.join(r.fixture.rows)}]")
            print(f"      {r.detail.splitlines()[0]}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list fixtures without running them")
    group.add_argument("--run", action="store_true", help="run fixtures; exit 1 if any fail")
    group.add_argument(
        "--ledger-map",
        action="store_true",
        help="emit ledger-map.yaml on stdout (fixture -> conformance ledger row wiring)",
    )
    parser.add_argument("--only", help="run a single fixture by id")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list(args.json)
    if args.ledger_map:
        return cmd_ledger_map()
    return cmd_run(args.only, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
