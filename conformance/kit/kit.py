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
import os
import re
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

from _bootstrap import PLACEMENT_FIELDS  # noqa: E402
from _bootstrap import RUNNER_PACKAGE  # noqa: E402
from _bootstrap import ensure_runner_importable  # noqa: E402
from _bootstrap import graph_identity  # noqa: E402
from _bootstrap import runner_source_path  # noqa: E402


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


# --------------------------------------------------------------------------
# Talking to a second host process
# --------------------------------------------------------------------------


def subprocess_env() -> dict[str, str]:
    """Environment for a second host, pinned to the SAME runner source.

    If ``_bootstrap`` had to put the library on ``sys.path`` (a checkout rather
    than an install), a child process would not inherit that and could silently
    import a *different* copy -- turning "two hosts disagree" into a statement
    about two versions rather than about conformance. Forwarding the path it
    chose makes both hosts provably the same implementation.
    """
    env = dict(os.environ)
    added = runner_source_path()
    if added:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{added}{os.pathsep}{existing}" if existing else added
    return env


#: The two fields ``_bootstrap.graph_identity`` drops from the in-process plan.
#: A run id and a wall-clock timestamp describe THIS invocation, not the graph
#: it resolved; comparing them would fail the fixture for a reason that has
#: nothing to do with conformance.
PER_RUN_FIELDS: tuple[str, ...] = ("run_id", "created_at")


def manifest_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """``graph_identity`` for a run manifest that arrived as JSON.

    The CLI prints the library's run-manifest mapping verbatim (lib.v1 Core 7)
    -- the same mapping ``graph_identity`` starts from -- so exactly the same
    exclusions apply, and only those: the per-run fields above and the
    placement fields ``_bootstrap`` already names. Nothing else is dropped,
    renamed, or coerced, so an extra or missing key still fails the comparison.
    """
    data = {key: value for key, value in payload.items() if key not in PER_RUN_FIELDS}
    for dependency in data.get("dependencies") or []:
        for key in PLACEMENT_FIELDS:
            dependency.pop(key, None)
    for agent in (data.get("agents") or {}).values():
        for key in PLACEMENT_FIELDS:
            agent.pop(key, None)
    return data


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
# Surface reflection -- the instrument the absence probes share
# --------------------------------------------------------------------------
#
# A behavioural fixture proves what the runner DOES. A prohibition -- "no host
# imports beyond the five ports", "no port exposes an agent map", "coordinator
# is not public API" -- is a claim about what does NOT exist, and no single
# happy path can establish it. The probes below therefore ENUMERATE the
# surface and compare it against an authored expectation, so that anything
# added later fails loud *by name* rather than passing unnoticed.
#
# Two rules keep these probes honest:
#
# 1. They read only what the library itself declares. Walking `dir()` would
#    sweep in `object`, `Protocol`, `Enum`, and `Exception` machinery and
#    report it as authored surface; `authored_members` walks the MRO and keeps
#    only classes the runner package owns.
# 2. Every probe carries a NON-VACUITY control: the same scanner is run over a
#    deliberately tainted stand-in and must flag it. A scanner that silently
#    matched nothing would report a clean surface for the same reason a broken
#    one would.


def _library_owned(obj: Any) -> bool:
    """True when ``obj`` was defined inside the runner package."""
    module = getattr(obj, "__module__", None)
    return isinstance(module, str) and (
        module == RUNNER_PACKAGE or module.startswith(f"{RUNNER_PACKAGE}.")
    )


#: Dunders that are genuinely part of an authored protocol's surface. Without
#: this, ``ApprovalCallback`` -- whose only member IS ``__call__`` -- would be
#: scanned as if it had no surface at all.
AUTHORED_DUNDERS: tuple[str, ...] = ("__call__",)

#: Modules whose classes contribute *machinery*, not authored surface:
#: ``object``, ``Protocol``, ``Enum``, ``Exception``. Skipping them by module
#: rather than by "is it the runner's?" keeps the scanner usable on the
#: non-library stand-in the controls depend on.
MACHINERY_MODULES: frozenset[str] = frozenset({"builtins", "typing", "typing_extensions", "abc", "enum"})


def _authored_base(base: Any) -> bool:
    return base is not object and getattr(base, "__module__", "") not in MACHINERY_MODULES


def authored_members(cls: Any) -> dict[str, Any]:
    """Public members declared on ``cls`` itself, across its non-machinery MRO.

    Inherited ``object``/``Protocol``/``Enum``/``Exception`` members are
    excluded -- they are not authored surface, and including them would bury a
    real addition in noise.
    """
    found: dict[str, Any] = {}
    for base in reversed(getattr(cls, "__mro__", (cls,))):
        if not _authored_base(base):
            continue
        for name, value in vars(base).items():
            if not name.startswith("_") or name in AUTHORED_DUNDERS:
                found[name] = value
    return found


def authored_annotations(cls: Any) -> dict[str, str]:
    """Annotation TEXT for every field declared on ``cls`` itself.

    ``from __future__ import annotations`` is in force throughout the runner,
    so annotations arrive as source strings. They are compared as text on
    purpose: resolving them would need the very host types whose absence is
    the thing under test.
    """
    found: dict[str, str] = {}
    for base in reversed(getattr(cls, "__mro__", (cls,))):
        if not _authored_base(base):
            continue
        for name, annotation in (getattr(base, "__annotations__", None) or {}).items():
            if not name.startswith("_"):
                found[name] = annotation if isinstance(annotation, str) else str(annotation)
    return found


def signature_tokens(fn: Any) -> list[str]:
    """Parameter names, parameter annotations, and return annotation of ``fn``.

    Returns an empty list for anything with no introspectable signature; a
    non-callable is simply not a signature, and pretending otherwise would
    manufacture tokens the surface does not have.
    """
    import inspect

    if not callable(fn):
        return []
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins, some descriptors
        return []

    tokens: list[str] = []
    for name, parameter in signature.parameters.items():
        if name in ("self", "cls"):
            continue
        tokens.append(name)
        if parameter.annotation is not inspect.Parameter.empty:
            tokens.append(str(parameter.annotation))
    if signature.return_annotation is not inspect.Signature.empty:
        tokens.append(str(signature.return_annotation))
    return tokens


def surface_tokens(*targets: Any) -> dict[str, list[str]]:
    """Every authored name and annotation reachable on ``targets``.

    Keyed by ``"<target>.<where>"`` so a hit names the exact place it was
    found, rather than reporting that "something, somewhere" matched.
    """
    tokens: dict[str, list[str]] = {}
    for target in targets:
        label = getattr(target, "__name__", repr(target))
        annotations = authored_annotations(target)
        if annotations:
            tokens[f"{label}.__annotations__"] = [
                token for name, text in annotations.items() for token in (name, text)
            ]
        for name, member in authored_members(target).items():
            found = [name, *signature_tokens(member)]
            if isinstance(member, property):
                found += signature_tokens(member.fget)
            tokens[f"{label}.{name}"] = found
        if callable(target) and not isinstance(target, type):
            tokens[f"{label}()"] = signature_tokens(target)
    return tokens


def forbidden_hits(tokens: dict[str, list[str]], vocabulary: tuple[str, ...]) -> list[str]:
    """Every ``where -> token`` in ``tokens`` matching ``vocabulary``.

    Matching is case-insensitive substring, which is deliberately generous:
    a probe for an absence should over-report rather than miss, and every hit
    is reported with its location so a false positive is obvious on sight.
    """
    hits: list[str] = []
    for where, found in sorted(tokens.items()):
        for token in found:
            lowered = str(token).lower()
            for word in vocabulary:
                if word in lowered:
                    hits.append(f"{where}: {token!r} matches {word!r}")
    return hits


#: Modules a runner type may legitimately be built from: the standard library's
#: own vocabulary. Anything else resolving out of a public annotation is a
#: foreign type reaching the surface.
NEUTRAL_MODULES: frozenset[str] = frozenset(
    {
        "builtins",
        "typing",
        "types",
        "abc",
        "enum",
        "pathlib",
        "datetime",
        "collections",
        "collections.abc",
        "dataclasses",
    }
)


def foreign_types(*targets: Any) -> list[str]:
    """Types reachable from ``targets``' annotations that are neither the
    library's own nor standard-library vocabulary.

    A name-based scan can only catch a host type that *announces* itself
    (``agent_configs``, ``coordinator``). This catches one that arrives under
    an innocuous name, by resolving each identifier in an annotation against
    the module that declared it and asking where the resulting type lives.
    """
    hits: list[str] = []
    for target in targets:
        module = sys.modules.get(getattr(target, "__module__", "") or "")
        if module is None:
            continue
        for where, tokens in sorted(surface_tokens(target).items()):
            for token in tokens:
                for identifier in set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(token))):
                    resolved = getattr(module, identifier, None)
                    if not isinstance(resolved, type):
                        continue
                    origin = getattr(resolved, "__module__", "")
                    if _library_owned(resolved) or origin in NEUTRAL_MODULES:
                        continue
                    hits.append(f"{where}: {identifier!r} resolves to {origin}.{resolved.__name__}")
    return sorted(set(hits))


class _TaintedStandIn:
    """A surface that DOES carry the things the probes forbid.

    Every probe runs its scanner over this first. If the scanner reports it
    clean, the scanner is broken and the probe's real result would be
    meaningless -- so the control failing is itself a fixture failure.
    """

    coordinator: "object"
    agent_configs: "dict[str, object]"

    def agent_catalog(self, parent_session: object) -> "dict[str, object]":  # noqa: D102
        raise NotImplementedError


def imported_amplifier_modules() -> list[str]:
    """Amplifier modules a *fresh interpreter* pulls in by importing the runner.

    Measured in a second process against a before/after snapshot of
    ``sys.modules``, so the answer is what the import ADDS -- not whatever the
    interpreter happened to start with, and not a reading of import statements.
    """
    code = (
        "import sys, json\n"
        "before = set(sys.modules)\n"
        f"import {RUNNER_PACKAGE}\n"
        "added = set(sys.modules) - before\n"
        "print(json.dumps(sorted(\n"
        "    m for m in added\n"
        f"    if m.split('.')[0] != {RUNNER_PACKAGE!r} and m.split('.')[0].startswith('amplifier')\n"
        ")))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        env=subprocess_env(),
    )
    expect_eq(
        completed.returncode,
        0,
        f"a fresh interpreter could not import the runner: {completed.stderr.strip()}",
    )
    return json.loads(completed.stdout)


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
        "Host B is a separate OS process. It prefers the real `recipe-runner` CLI, "
        "invoked through its DOCUMENTED dual entry point "
        "`python -m amplifier_recipe_runner plan --json`, and falls back to "
        "conformance/kit/host_adapter.py where the CLI is not installed -- reporting "
        "which surface it used either way. The Amplifier tool adapter is not yet a "
        "runner host, so it is not compared -- see kit README residual R1."
    ),
)
async def good_identical_graph_across_hosts() -> str:
    import importlib.util

    in_process = graph_identity(await plan_recipe("declared.yaml"))

    expect(in_process["dependencies"], "resolved graph has no dependencies; comparison would be vacuous")
    expect(in_process["agents"], "resolved graph has no agents; comparison would be vacuous")

    # `python -m amplifier_recipe_runner` -- NOT `-m amplifier_recipe_runner.cli`.
    # cli.py declares no `__main__` guard, so importing it as a module runs
    # nothing, exits 0, and prints an empty stdout; __main__.py is the entry
    # point the library documents, and the console script shares its `main`.
    cli_available = importlib.util.find_spec("amplifier_recipe_runner.__main__") is not None
    if cli_available:
        argv = [
            sys.executable,
            "-m",
            "amplifier_recipe_runner",
            "plan",
            "--json",
            # Configure host B exactly as host A: the in-process request passes
            # no trust policy and resolves offline. Anything else would compare
            # two DIFFERENTLY configured hosts and report the difference as a
            # conformance failure.
            "--offline",
            "--trust",
            "none",
            str(RECIPES / "declared.yaml"),
        ]
        surface = "recipe-runner CLI (python -m amplifier_recipe_runner plan --json)"
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

    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=120,
        # The recipe declares `bundles/supplier` as a path relative to the
        # fixture root, which is the root host A pins via
        # LocalBundleResolver(base_path=FIXTURES). The CLI's offline resolver
        # falls back to the process CWD, so this is the same root, named.
        cwd=str(FIXTURES),
        env=subprocess_env(),
    )
    expect_eq(completed.returncode, 0, f"second host exited non-zero: {completed.stderr.strip()}")
    expect(completed.stdout.strip(), f"second host printed nothing on stdout (stderr: {completed.stderr.strip()!r})")
    out_of_process = json.loads(completed.stdout)
    if cli_available:
        out_of_process = manifest_identity(out_of_process)

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


# ==========================================================================
# ABSENCE PROBES -- enumerated surface, not a happy path
# ==========================================================================
#
# These are GOOD in polarity (they must hold against a conforming runner) but
# a different genre from the fixtures above: each one enumerates a surface and
# asserts that a named construct is ABSENT from it. Their discrimination is
# proved by the four `mutations/*.patch` files that reintroduce exactly those
# constructs -- see `discriminate.sh`.


#: Fields ``RunRequest`` carries today. A host constructs this object, so a new
#: field is a new host-facing channel; it must be justified against manifest.v1
#: Core 4 rather than appearing silently.
EXPECTED_RUN_REQUEST_FIELDS: tuple[str, ...] = (
    "recipe",
    "context",
    "services",
    "trust_policy",
    "lock_mode",
    "run_id",
    "legacy_mode",
)

#: Every entry point a host calls, pinned parameter-for-parameter. The three
#: injectables on ``run`` are the library's OWN protocols (asserted below), not
#: host catalogs.
EXPECTED_ENTRY_POINT_PARAMETERS: dict[str, tuple[str, ...]] = {
    "plan": ("request", "resolver"),
    "run": ("request", "resolver", "spawn_backend", "session_factory"),
    "create_execution_session": ("plan", "services", "run_id", "spawn_backend", "session_factory"),
    "RecipeRunner.validate": ("request",),
    "RecipeRunner.plan": ("request",),
    "RecipeRunner.run": ("request",),
    "RecipeRunner.resume": ("run_id", "services"),
}

#: Names that would mean a host's ambient agent map had reached a surface.
AGENT_MAP_VOCABULARY: tuple[str, ...] = (
    "agent",
    "catalog",
    "roster",
    "coordinator",
    "caller",
)

#: Names that would mean an Amplifier-internal session object had reached the
#: library's public API. ``session`` alone is NOT here: the library's own
#: ``ExecutionSession`` and ``SessionFactory`` are the neutral abstraction lib
#: Core 3 requires, so banning the word would ban the remedy.
AMPLIFIER_SESSION_VOCABULARY: tuple[str, ...] = (
    "coordinator",
    "amplifier.",
    "amplifier_core",
    "amplifiersession",
    "parent_session",
    "caller_session",
    "host_session",
)


def parameter_names(fn: Any) -> tuple[str, ...]:
    """Declared parameter names of ``fn``, excluding ``self``/``cls``."""
    import inspect

    signature = inspect.signature(fn)
    return tuple(name for name in signature.parameters if name not in ("self", "cls"))


@fixture(
    id="probe-host-surface-is-exactly-the-five-ports",
    polarity="GOOD",
    title="Enumerated: the runner's host-facing surface is the five ports and nothing else",
    clauses=("manifest.v1 Core 4", "lib.v1 Core 4"),
    rows=("RCP-004", "RCP-104"),
    notes=(
        "ABSENCE PROBE. Enumerates HostServices, RunRequest, and every host entry "
        "point against an authored expectation, then MEASURES -- in a fresh "
        "interpreter -- that importing the library pulls in no Amplifier module. "
        "Discrimination: mutations/sixth-host-port.patch."
    ),
)
async def probe_host_surface_is_five_ports() -> str:
    import dataclasses as _dc

    from amplifier_recipe_runner import execution as execution_module
    from amplifier_recipe_runner import ports as ports_module
    from amplifier_recipe_runner.api import RecipeRunner
    from amplifier_recipe_runner.api import RunRequest

    # 1. The bundle a host hands over is the five ports, one field each.
    #    Non-vacuity first: an empty HOST_PORTS would make every check below
    #    pass while proving nothing.
    expect(len(ports_module.HOST_PORTS) == 5, f"HOST_PORTS is not five ports: {ports_module.HOST_PORTS}")
    service_fields = tuple(f.name for f in _dc.fields(ports_module.HostServices))
    expect_eq(sorted(service_fields), sorted(ports_module.HOST_PORTS), "HostServices fields vs HOST_PORTS")
    expect_eq(len(service_fields), len(ports_module.HOST_PORTS), "HostServices field count")

    # 2. RunRequest is the OTHER object a host constructs. A field here would
    #    be a host-import channel that bypasses the ports entirely.
    request_fields = tuple(f.name for f in _dc.fields(RunRequest))
    if sorted(request_fields) != sorted(EXPECTED_RUN_REQUEST_FIELDS):
        added = sorted(set(request_fields) - set(EXPECTED_RUN_REQUEST_FIELDS))
        removed = sorted(set(EXPECTED_RUN_REQUEST_FIELDS) - set(request_fields))
        raise KitFailure(
            f"RunRequest's host-facing fields changed -- added {added}, removed {removed}. "
            "Every field a host fills is a host-import channel and must be justified "
            "against manifest.v1 Core 4 before this expectation is updated."
        )

    # 3. Every entry point, pinned parameter-for-parameter.
    actual: dict[str, tuple[str, ...]] = {
        "plan": parameter_names(execution_module.plan),
        "run": parameter_names(execution_module.run),
        "create_execution_session": parameter_names(execution_module.create_execution_session),
        "RecipeRunner.validate": parameter_names(RecipeRunner.validate),
        "RecipeRunner.plan": parameter_names(RecipeRunner.plan),
        "RecipeRunner.run": parameter_names(RecipeRunner.run),
        "RecipeRunner.resume": parameter_names(RecipeRunner.resume),
    }
    for name, expected in EXPECTED_ENTRY_POINT_PARAMETERS.items():
        expect_eq(actual[name], expected, f"host entry point {name} parameters")

    # 4. The three injectables on `run` are the library's OWN protocols, built
    #    from library and standard-library types only. A count of parameters
    #    would not notice one of them becoming a host agent catalog.
    #
    #    A NAME scan is deliberately not used here: `SessionFactory.create`
    #    legitimately takes the plan's own `PlanCatalog`, and banning the word
    #    "catalog" would ban the conforming design along with the violation.
    #    Provenance is the discriminator -- where the type comes from.
    for parameter, protocol in (
        ("resolver", execution_module.DependencyResolver),
        ("spawn_backend", execution_module.SpawnBackend),
        ("session_factory", execution_module.SessionFactory),
    ):
        expect(
            _library_owned(protocol),
            f"`run`'s {parameter} injectable is not library-owned: "
            f"{getattr(protocol, '__module__', '?')}.{getattr(protocol, '__name__', protocol)}",
        )
        foreign = foreign_types(protocol)
        expect(not foreign, f"`run`'s {parameter} protocol names a foreign type: {foreign}")

    # 5. Control: the scanner used in (4) must flag a surface that DOES carry
    #    a host agent map. A scanner matching nothing would report every
    #    protocol clean for the same reason a correct one would.
    control = forbidden_hits(surface_tokens(_TaintedStandIn), AGENT_MAP_VOCABULARY)
    expect(control, "the agent-map scanner flagged nothing on a deliberately tainted stand-in")

    # 6. Measured, not read: importing the library imports no Amplifier module.
    leaked = imported_amplifier_modules()
    expect_eq(leaked, [], "Amplifier modules imported by the runner in a fresh interpreter")

    return (
        f"HostServices == HOST_PORTS {ports_module.HOST_PORTS}; "
        f"RunRequest fields {sorted(request_fields)} unchanged; "
        f"{len(EXPECTED_ENTRY_POINT_PARAMETERS)} entry points pinned; "
        f"3 injectables library-owned and agent-free; "
        f"0 Amplifier modules imported (scanner control flagged {len(control)} taint(s))"
    )


@fixture(
    id="probe-no-dependency-inferred-from-an-agent-namespace",
    polarity="GOOD",
    title="Enumerated: the resolver is asked for exactly the declared sources, never a namespace-derived one",
    clauses=("manifest.v1 Core 11",),
    rows=("RCP-011",),
    notes=(
        "ABSENCE PROBE. The undeclared reference's namespace IS resolvable as a "
        "bundle source (asserted as a control), so a namespace-inferring runner "
        "would SUCCEED here -- which is what makes the absence meaningful. "
        "Discrimination: mutations/namespace-inferred-dependency.patch. This "
        "probe is orthogonal to the row's OPEN-PINNED interpretive ruling."
    ),
)
async def probe_no_namespace_inference() -> str:
    from amplifier_recipe_runner.errors import UndeclaredAgentError
    from amplifier_recipe_runner.manifest import Dependency
    from amplifier_recipe_runner.manifest import parse_manifest_file

    reference = "lean-caller:packager"
    namespace = reference.split(":", 1)[0]
    inferable_source = f"bundles/{namespace}"

    # Control: inference WOULD work. The namespace names a real, resolvable
    # bundle that really supplies the referenced agent. Without this, "no
    # inference happened" could just mean "inference would have failed anyway".
    inferable = await local_resolver().resolve(Dependency(source=inferable_source, kind="bundle"))
    expect(
        reference in inferable.agents,
        f"control broken: {inferable_source!r} does not supply {reference!r} "
        f"(supplies {sorted(inferable.agents)}), so inference would have failed regardless",
    )

    # The declared closure is read from the fixture recipe, not restated here,
    # so the comparison cannot drift from the recipe it describes.
    manifest = parse_manifest_file(RECIPES / "undeclared.yaml")
    declared = [dependency.source for dependency in manifest.dependencies]
    expect(
        inferable_source not in declared,
        f"premise broken: {inferable_source!r} IS declared by undeclared.yaml ({declared})",
    )

    recording = RecordingResolver(local_resolver())
    exc = await expect_raises(
        UndeclaredAgentError,
        plan_recipe("undeclared.yaml", resolver=recording),
        f"planning a recipe referencing {reference!r} with no dependency supplying it",
    )
    assert isinstance(exc, UndeclaredAgentError)
    expect_eq(exc.agent, reference, "the refused reference")
    expect_eq(
        recording.calls,
        declared,
        f"sources the resolver was asked for -- a source derived from the {namespace!r} "
        "namespace would appear here",
    )

    # The same enumeration on recipes that PLAN CLEANLY: a runner could infer a
    # dependency on a path that never reaches an undeclared reference at all.
    for recipe in ("declared.yaml", "behavior-partial.yaml"):
        expected = [d.source for d in parse_manifest_file(RECIPES / recipe).dependencies]
        watcher = RecordingResolver(local_resolver())
        await plan_recipe(recipe, resolver=watcher)
        expect_eq(watcher.calls, expected, f"sources requested while planning {recipe}")

    return (
        f"{inferable_source!r} really supplies {reference!r}, is NOT declared, and was never "
        f"requested: the resolver saw exactly {declared} and the reference was refused by name; "
        "declared.yaml and behavior-partial.yaml likewise requested only their declared sources"
    )


@fixture(
    id="probe-no-coordinator-in-the-public-api",
    polarity="GOOD",
    title="Enumerated: no Amplifier coordinator or session type appears in __all__ or any public signature",
    clauses=("lib.v1 Core 3",),
    rows=("RCP-103",),
    notes=(
        "ABSENCE PROBE. Walks every name in the package's `__all__`, its authored "
        "members, field annotations, and signatures. "
        "Discrimination: mutations/coordinator-on-public-session.patch."
    ),
)
async def probe_no_coordinator_in_public_api() -> str:
    import amplifier_recipe_runner as package
    from amplifier_recipe_runner.api import ExecutionSession

    exported = tuple(package.__all__)
    expect(len(exported) > 20, f"__all__ is implausibly small ({len(exported)}); the scan would be vacuous")

    # 1. Every exported symbol is the library's own. A re-export of an
    #    Amplifier type would be public API by definition.
    scanned: list[Any] = []
    for name in exported:
        obj = getattr(package, name)
        if isinstance(obj, (str, int, tuple)) and not isinstance(obj, type):
            continue  # HOST_PORTS, RUN_MANIFEST_VERSION, __version__ -- plain data
        expect(
            _library_owned(obj),
            f"__all__ exports {name!r} defined in "
            f"{getattr(obj, '__module__', '?')!r}, which the library does not own",
        )
        scanned.append(obj)

    # 2. Nothing named for an Amplifier session object appears anywhere on the
    #    exported surface: not as an exported name, a member, a field
    #    annotation, a parameter, or a return type.
    name_hits = forbidden_hits({"__all__": list(exported)}, AMPLIFIER_SESSION_VOCABULARY)
    expect(not name_hits, f"__all__ itself names an Amplifier session object: {name_hits}")

    tokens = surface_tokens(*scanned)
    expect(len(tokens) > 40, f"the surface scan reached only {len(tokens)} places; it is not covering __all__")
    hits = forbidden_hits(tokens, AMPLIFIER_SESSION_VOCABULARY)
    expect(not hits, f"Amplifier session objects reachable from the public API: {hits}")

    # 2b. And one that arrives under an innocuous NAME: every type resolvable
    #     out of a public annotation is the library's own or standard library.
    foreign = foreign_types(*scanned)
    expect(not foreign, f"foreign types reachable from the public API: {foreign}")

    # 3. The neutral abstraction lib Core 3 requires is the library's own, and
    #    is what the public surface actually exposes.
    expect(
        _library_owned(ExecutionSession),
        f"ExecutionSession is defined in {getattr(ExecutionSession, '__module__', '?')!r}",
    )
    expect("ExecutionSession" in exported, "the library's neutral session abstraction is not exported")

    # 4. Control: the same scanner must flag a surface that DOES expose one.
    control = forbidden_hits(surface_tokens(_TaintedStandIn), AMPLIFIER_SESSION_VOCABULARY)
    expect(control, "the Amplifier-session scanner flagged nothing on a deliberately tainted stand-in")

    return (
        f"{len(exported)} exported names, all library-owned; {len(tokens)} authored members / "
        f"annotations / signatures scanned; 0 hits for {list(AMPLIFIER_SESSION_VOCABULARY)}; "
        f"ExecutionSession is {ExecutionSession.__module__}'s own "
        f"(scanner control flagged {len(control)} taint(s))"
    )


@fixture(
    id="probe-ports-are-the-five-contract-names-and-carry-no-agent-map",
    polarity="GOOD",
    title="Enumerated: HOST_PORTS is the five contract names and no port signature exposes an agent map",
    clauses=("lib.v1 Core 4", "manifest.v1 Core 4"),
    rows=("RCP-104", "RCP-004"),
    notes=(
        "ABSENCE PROBE. Pins HOST_PORTS to the contract's own five names in "
        "contract order, then scans every port protocol and payload for an agent "
        "map. Discrimination: mutations/port-carries-agent-map.patch -- which "
        "keeps exactly five ports and widens one, so only this probe catches it."
    ),
)
async def probe_ports_carry_no_agent_map() -> str:
    import dataclasses as _dc

    from amplifier_recipe_runner import ports as ports_module

    # 1. The five names, quoted from recipe-runner-lib.v1 Core 4, in its order.
    contract_ports = (
        "provider_access",
        "approval_callback",
        "event_sink",
        "workspace",
        "cancellation",
    )
    expect_eq(ports_module.HOST_PORTS, contract_ports, "HOST_PORTS vs the contract's five port names")

    # 2. One field per port, and no field that is not a port.
    service_fields = tuple(f.name for f in _dc.fields(ports_module.HostServices))
    expect_eq(sorted(service_fields), sorted(contract_ports), "HostServices fields vs the five ports")

    # 3. The exported port vocabulary itself. A sixth port type would arrive
    #    here even if HOST_PORTS were left alone.
    expect_eq(
        sorted(ports_module.__all__),
        sorted(
            (
                "HOST_PORTS",
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
            )
        ),
        "ports.__all__",
    )

    # 4. No port protocol or payload names, accepts, or returns an agent map.
    port_types = (
        ports_module.ProviderAccess,
        ports_module.ApprovalCallback,
        ports_module.ApprovalRequest,
        ports_module.ApprovalDecision,
        ports_module.EventSink,
        ports_module.CancellationToken,
        ports_module.HostServices,
    )
    tokens = surface_tokens(*port_types)
    expect(len(tokens) > 10, f"the port scan reached only {len(tokens)} places; it is not covering the ports")
    hits = forbidden_hits(tokens, AGENT_MAP_VOCABULARY)
    expect(not hits, f"a host port exposes an agent map: {hits}")

    # 4b. And no port is typed with anything foreign -- an agent map handed
    #     across a port under a neutral name would pass (4) and fail here.
    foreign = foreign_types(*port_types)
    expect(not foreign, f"a host port names a foreign type: {foreign}")

    # 5. Control: the same scanner must flag a port-shaped surface that does.
    control = forbidden_hits(surface_tokens(_TaintedStandIn), AGENT_MAP_VOCABULARY)
    expect(control, "the agent-map scanner flagged nothing on a deliberately tainted stand-in")

    return (
        f"HOST_PORTS == {contract_ports} exactly; HostServices carries one field per port and no other; "
        f"ports.__all__ pinned at {len(ports_module.__all__)} names; {len(tokens)} port members / "
        f"annotations / signatures scanned with 0 agent-map hits "
        f"(scanner control flagged {len(control)} taint(s))"
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
        "coverage": "full",
        "covered": (
            "Behaviourally: the execution session is built from the plan catalog alone; every host "
            "argument that could widen it (agent_configs, parent_session, inheritance kwargs) is "
            "discarded and recorded. By ENUMERATION: HostServices is exactly HOST_PORTS one field "
            "for one, RunRequest's host-facing fields are pinned by name, all seven host entry "
            "points are pinned parameter-for-parameter, `run`'s three injectables are proved "
            "library-owned and free of foreign types, and a fresh interpreter importing the "
            "library is MEASURED to pull in zero Amplifier modules. Discrimination-proved "
            "(mutations/sixth-host-port.patch)."
        ),
        "not_covered": None,
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
        "coverage": "full",
        "covered": (
            "Absence probe: the sources handed to the resolver are compared against the sources the "
            "fixture recipe declares, across a refused plan and two clean ones -- a namespace-derived "
            "source would appear there. Non-vacuous by control: the undeclared reference's namespace "
            "IS a resolvable bundle that really supplies the referenced agent, so inference would "
            "have SUCCEEDED. Discrimination-proved "
            "(mutations/namespace-inferred-dependency.patch)."
        ),
        "not_covered": (
            "The probe establishes only that no dependency is inferred from a namespace; it takes no "
            "position on this row's OPEN-PINNED interpretive ruling, which is the reconciler's call."
        ),
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
        "coverage": "full",
        "covered": (
            "Absence probe over the whole public API: every name in `__all__` is asserted "
            "library-owned, and 127 authored members, field annotations, parameters and return "
            "types are scanned for `coordinator` and Amplifier-session vocabulary AND for any type "
            "resolving outside the library and the standard library -- which catches a host type "
            "arriving under an innocuous name. The scanner is proved live against a tainted "
            "stand-in. Discrimination-proved "
            "(mutations/coordinator-on-public-session.patch)."
        ),
        "not_covered": None,
    },
    "RCP-104": {
        "coverage": "full",
        "covered": (
            "Behaviourally: no port carries agents in practice -- the spawn adapter discards and "
            "records agent_configs, parent_session, and inheritance kwargs, and still resolves from "
            "the plan. By ENUMERATION: HOST_PORTS is asserted equal to the contract's own five "
            "names in contract order, HostServices carries one field per port and no other, "
            "ports.__all__ is pinned, and every port protocol and payload is scanned for agent-map "
            "vocabulary and for foreign types. Discrimination-proved twice over "
            "(mutations/sixth-host-port.patch adds a sixth port; "
            "mutations/port-carries-agent-map.patch keeps five and widens one, and ONLY this "
            "probe catches it)."
        ),
        "not_covered": None,
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
