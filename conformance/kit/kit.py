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
import shutil
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


class KitSkipped(Exception):
    """This environment cannot run the fixture at all -- reported as SKIP, never PASS.

    A skip is not a pass and is never counted as one: the summary line names
    every skipped fixture and its reason, so "the tool this check needs is not
    installed here" can never be mistaken for "the check ran and held".

    Raised only for a missing *precondition of the measurement itself* (an
    absent ``uv``, an unreadable git object, a host environment that cannot
    supply what module activation assumes). Never for a failed assertion --
    that is a :class:`KitFailure`.
    """


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


class FailingBackend:
    """Backend that IS reached, and fails there -- the way a real spawn fails.

    Deliberately raises a plain ``RuntimeError``, not one of the library's own
    typed errors: an executor that only recognises its own exception families
    lets a host/provider failure escape the step loop, which is how a run came
    to report ``completed`` for a step that errored (recipes-30w).

    ``fail_on`` is the zero-based index of the call that raises; ``-1`` never
    fails, so the same double drives the honest-success control.
    """

    def __init__(self, fail_on: int = 0, message: str = "No providers available") -> None:
        self.fail_on = fail_on
        self.message = message
        self.calls = 0

    async def spawn(self, request: Any) -> str:
        index = self.calls
        self.calls += 1
        if index == self.fail_on:
            raise RuntimeError(self.message)
        return f"ok:{request.canonical}"


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


def imported_runner_path() -> str | None:
    """The directory holding the runner package THIS process actually imported.

    ``runner_source_path`` reports only what ``_bootstrap`` had to add, which
    is ``None`` when the library was already importable -- including when it
    was importable because the *invocation* put it on the path
    (``PYTHONPATH=src ... kit.py``). This reports the copy in hand either way.
    """
    module = sys.modules.get("amplifier_recipe_runner")
    if module is None:  # pragma: no cover - every caller has already planned
        import importlib

        module = importlib.import_module("amplifier_recipe_runner")
    file = getattr(module, "__file__", None)
    return str(Path(file).resolve().parent.parent) if file else None


def subprocess_env() -> dict[str, str]:
    """Environment for a second host, pinned to the SAME runner source.

    If ``_bootstrap`` had to put the library on ``sys.path`` (a checkout rather
    than an install), a child process would not inherit that and could silently
    import a *different* copy -- turning "two hosts disagree" into a statement
    about two versions rather than about conformance. Forwarding the path it
    chose makes both hosts provably the same implementation.

    Two ways that guarantee leaked, both closed here:

    * An inherited PYTHONPATH entry can be RELATIVE (``PYTHONPATH=src``, the
      documented way to run this kit from the repo root). The child runs with
      its own ``cwd``, so the same entry names a different directory there --
      usually one that does not exist, sending the child to whatever copy is
      installed. Every inherited entry is therefore resolved against the cwd
      the parent itself used.
    * ``runner_source_path()`` is ``None`` when the library was already
      importable, so nothing was pinned at all. The copy this process really
      imported (:func:`imported_runner_path`) is pinned first instead.

    Neither widens what the child may import: the entries are the parent's own,
    made to mean in the child what they meant in the parent.
    """
    env = dict(os.environ)
    entries: list[str] = []
    for candidate in (imported_runner_path(), runner_source_path()):
        if candidate and candidate not in entries:
            entries.append(candidate)
    for inherited in env.get("PYTHONPATH", "").split(os.pathsep):
        if not inherited:
            continue
        resolved = str(Path(inherited).resolve())
        if resolved not in entries:
            entries.append(resolved)
    if entries:
        env["PYTHONPATH"] = os.pathsep.join(entries)
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


# --------------------------------------------------------------------------
# The real module-activation install path
# --------------------------------------------------------------------------
#
# Every other fixture in this kit imports the implementation the way a *test*
# does: through PYTHONPATH, or through an editable install made WITH uv
# sources. Production does neither. Amplifier's module activator runs exactly
#
#     uv pip install -e <bundle>/modules/tool-recipes --python <env> --no-sources
#
# and on 2026-09-02 that command -- and only that command -- failed, breaking
# `amplifier update` and session prepare for every user while all four gates
# stayed green (see recipes-eir, hotfixed in 02a3dfe). The gap was not a weak
# assertion; it was that no gate ever ran the consumer's command at all.
#
# This fixture runs it, into a throwaway venv, and then asserts the two things
# that make a successful install *useful*: the module imports, and
# `load_runner()` resolves the runner shipped in THIS bundle tree rather than a
# second copy from somewhere else (the recipes-4g5 symptom -- a cache clone
# answering for the in-bundle library, so the code under test is not the code
# in hand).

REPO_ROOT = KIT_DIR.parents[1]
MODULE_DIR = REPO_ROOT / "modules" / "tool-recipes"
LIBRARY_SRC = REPO_ROOT / "src"

# The activation command, verbatim, as the activator issues it.
ACTIVATION_INSTALL = ("uv", "pip", "install", "-e", "<module>", "--python", "<env>", "--no-sources")

# The commit that HOTFIXED the regression (02a3dfe, "module installs under
# --no-sources"); its parent therefore holds the pre-hotfix pyproject -- a hard
# direct-URL dependency on amplifier-recipe-runner with no
# `tool.hatch.metadata.allow-direct-references`. That blob is this fixture's
# discrimination control: the same fixture, run against it, must FAIL.
PRE_HOTFIX_PYPROJECT_REV = "02a3dfe8f071c5ee92db6266609025bca87731a1^"
PRE_HOTFIX_PYPROJECT_PATH = "modules/tool-recipes/pyproject.toml"

# Host-provided at activation time: the activator installs into the Amplifier
# CLI's own environment, which already carries these. They are deliberately not
# dependencies of the module, so a bare venv cannot import it without them.
HOST_PROVIDED = ("amplifier_core", "amplifier_foundation")


def _host_site_packages() -> str:
    """The site-packages of the interpreter running this kit, as a PYTHONPATH.

    This is how the throwaway venv is given what the *host* environment
    supplies at activation time -- and no more. The kit's own PYTHONPATH is
    deliberately NOT forwarded: it usually contains ``<repo>/src``, which would
    hand the child an importable runner and make the in-bundle fallback
    assertion vacuous.
    """
    import site

    dirs = [d for d in (list(site.getsitepackages()) + [site.getusersitepackages()]) if d]
    return os.pathsep.join(dirs)


def _run(argv: list[str], *, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)


def _tail(text: str, lines: int = 12) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n        ".join(kept[-lines:])


def _make_venv(path: Path) -> Path:
    """A clean venv on the SAME interpreter as this kit, and its python."""
    made = _run(["uv", "venv", "--python", sys.executable, str(path)], timeout=180)
    if made.returncode != 0:
        raise KitSkipped(f"`uv venv` failed, so no clean environment could be built: {_tail(made.stderr, 4)}")
    python = path / "bin" / "python"
    if not python.exists():  # pragma: no cover - non-POSIX layout
        python = path / "Scripts" / "python.exe"
    return python


def _activation_install(module_dir: Path, venv_python: Path) -> subprocess.CompletedProcess[str]:
    """Exactly what Amplifier's module activator runs. No extra flags, ever."""
    return _run(
        ["uv", "pip", "install", "-e", str(module_dir), "--python", str(venv_python), "--no-sources"],
        timeout=600,
    )


def _probe(venv_python: Path, program: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = _host_site_packages()
    return _run([str(venv_python), "-c", program], timeout=120, env=env)


_AVAILABILITY_PROBE = (
    "import json, importlib.util as u;"
    "print(json.dumps({n: u.find_spec(n) is not None for n in "
    "('amplifier_core', 'amplifier_foundation', 'amplifier_recipe_runner')}))"
)

_RESOLUTION_PROBE = (
    "import json;"
    "import amplifier_module_tool_recipes as m;"
    "from amplifier_module_tool_recipes import runner_adapter;"
    "r = runner_adapter.load_runner();"
    "print(json.dumps({'module_file': m.__file__, 'runner_file': r.__file__,"
    " 'runner_available': runner_adapter.runner_available()}))"
)


def _pre_hotfix_pyproject() -> str:
    """The pre-hotfix ``pyproject.toml``, read from git history.

    Read from the object store rather than vendored so the control is the real
    regression this repo actually shipped, not a re-typed approximation of it.
    """
    if shutil.which("git") is None:
        raise KitSkipped("git is not installed, so the pre-hotfix control blob cannot be read")
    shown = _run(
        ["git", "-C", str(REPO_ROOT), "show", f"{PRE_HOTFIX_PYPROJECT_REV}:{PRE_HOTFIX_PYPROJECT_PATH}"],
        timeout=60,
    )
    if shown.returncode != 0 or not shown.stdout.strip():
        raise KitSkipped(
            f"the pre-hotfix blob {PRE_HOTFIX_PYPROJECT_REV}:{PRE_HOTFIX_PYPROJECT_PATH} is not in this "
            f"checkout (shallow clone?), so the discrimination control cannot run: {_tail(shown.stderr, 3)}"
        )
    return shown.stdout


def _pre_hotfix_bundle(root: Path) -> Path:
    """A bundle-shaped copy of the module carrying the pre-hotfix pyproject.

    Bundle-shaped on purpose: ``<root>/modules/tool-recipes`` beside
    ``<root>/src``, because ``load_runner()``'s in-bundle fallback is defined
    relative to that layout. A control that broke the layout as well as the
    packaging would not tell us which one it caught.
    """
    module_copy = root / "modules" / "tool-recipes"
    module_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(MODULE_DIR, module_copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv"))
    (root / "src").symlink_to(LIBRARY_SRC, target_is_directory=True)
    (module_copy / "pyproject.toml").write_text(_pre_hotfix_pyproject(), encoding="utf-8")
    return module_copy


@fixture(
    id="good-activation-install-resolves-the-in-bundle-runner",
    polarity="GOOD",
    title="The real module-activation install succeeds and resolves the in-bundle runner",
    clauses=("lib.v1 Core 1",),
    rows=("RCP-101",),
    notes=(
        "The only fixture that leaves the in-process library and drives the CONSUMER's command "
        "(`uv pip install -e <module> --python <env> --no-sources`) into a throwaway venv. Carries its "
        "own discrimination control: the same install against the pre-hotfix pyproject "
        f"({PRE_HOTFIX_PYPROJECT_REV}) must fail, naming allow-direct-references. Skips with a reason "
        "when uv or git is absent, or when the host environment cannot supply what activation assumes."
    ),
)
async def good_activation_install_resolves_in_bundle_runner() -> str:
    if shutil.which("uv") is None:
        raise KitSkipped(
            "uv is not installed, and module activation IS a uv command "
            f"(`{' '.join(ACTIVATION_INSTALL)}`) -- there is nothing faithful to run without it"
        )

    # Read the control blob up front: a fixture that installs for 30s and only
    # then discovers it cannot prove it discriminates has wasted the budget.
    pre_hotfix = _pre_hotfix_pyproject()
    expect_in(
        "amplifier-recipe-runner @ git+",
        pre_hotfix,
        "the pre-hotfix control blob does not contain the direct-URL dependency it is supposed to reintroduce",
    )
    expect(
        "allow-direct-references" not in pre_hotfix,
        "the pre-hotfix control blob already sets allow-direct-references, so it is not the pre-hotfix state",
    )

    scratch = Path(tempfile.mkdtemp(prefix="recipes-activation-install-"))
    try:
        venv_python = _make_venv(scratch / "venv")

        # Premises, MEASURED -- each one an environment fact the assertions below
        # depend on. Asserting them as prose would let the fixture pass vacuously.
        available = _probe(venv_python, _AVAILABILITY_PROBE)
        expect_eq(available.returncode, 0, f"the availability probe did not run: {_tail(available.stderr, 4)}")
        present = json.loads(available.stdout)
        missing = [name for name in HOST_PROVIDED if not present.get(name)]
        if missing:
            raise KitSkipped(
                f"this host environment does not provide {', '.join(missing)}, which module activation "
                "assumes (the activator installs into the Amplifier CLI's own environment). The install "
                "half would run, but the import and load_runner() halves could not."
            )
        if present.get("amplifier_recipe_runner"):
            raise KitSkipped(
                "an installed amplifier_recipe_runner is already reachable from this host environment, so "
                "load_runner() would legitimately return it and the IN-BUNDLE fallback could not be observed"
            )

        installed = _activation_install(MODULE_DIR, venv_python)
        expect_eq(
            installed.returncode,
            0,
            "the module-activation install FAILED -- this is the exact command `amplifier update` runs:\n"
            f"        {_tail(installed.stderr)}",
        )

        resolved = _probe(venv_python, _RESOLUTION_PROBE)
        expect_eq(
            resolved.returncode,
            0,
            f"the installed module could not be imported, or load_runner() raised:\n        {_tail(resolved.stderr)}",
        )
        report = json.loads(resolved.stdout)

        module_file = Path(report["module_file"]).resolve()
        expect(
            module_file.is_relative_to(MODULE_DIR),
            f"the imported module is not the one installed from this repo: {module_file}",
        )
        expect(report["runner_available"] is True, "runner_available() is False after a successful activation install")

        runner_file = Path(report["runner_file"]).resolve()
        expect(
            runner_file.is_relative_to(LIBRARY_SRC / "amplifier_recipe_runner"),
            "load_runner() resolved a runner OUTSIDE this bundle tree -- the recipes-4g5 symptom "
            f"(a second copy, e.g. a bundle-cache clone, answering for the in-bundle library):\n"
            f"        resolved: {runner_file}\n"
            f"        expected under: {LIBRARY_SRC / 'amplifier_recipe_runner'}",
        )

        # CONTROL -- the reason this fixture is worth its runtime.
        # The same command, against the pyproject that shipped the outage, must
        # fail, and must fail FOR ITS OWN NAMED REASON. A merely non-zero exit
        # would accept a typo in the copy as proof of discrimination.
        control_root = scratch / "pre-hotfix"
        control_root.mkdir()
        control_module = _pre_hotfix_bundle(control_root)
        control_python = _make_venv(scratch / "control-venv")
        control = _activation_install(control_module, control_python)
        expect(
            control.returncode != 0,
            "THE FIXTURE DOES NOT DISCRIMINATE: the activation install SUCCEEDED against the pre-hotfix "
            f"pyproject ({PRE_HOTFIX_PYPROJECT_REV}), which is the packaging that broke `amplifier update` "
            "in production on 2026-09-02",
        )
        expect_in(
            "allow-direct-references",
            control.stdout + control.stderr,
            "the pre-hotfix install failed for some OTHER reason than the direct-URL dependency it was "
            f"built to reintroduce:\n        {_tail(control.stderr)}",
        )

        return (
            f"`{' '.join(ACTIVATION_INSTALL)}` exited 0 into a clean venv; the installed module imported "
            f"(with only host site-packages alongside, no amplifier_recipe_runner installed) and "
            f"load_runner() resolved the IN-BUNDLE library at {runner_file}; the same command against the "
            f"pre-hotfix pyproject ({PRE_HOTFIX_PYPROJECT_REV}) failed naming allow-direct-references, so "
            "the fixture discriminates"
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


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


@fixture(
    id="bad-errored-step-is-never-a-completed-step",
    polarity="BAD",
    title="A step that errored fails the run and is never reported completed",
    clauses=("lib.v1 Core 8",),
    rows=("RCP-108",),
    notes=(
        "The other half of Core 8. The preflight fixtures assert 'refused before anything ran'; "
        "this one asserts the POST-preflight direction -- a step that really started and then "
        "errored. The failure is an untyped RuntimeError, the shape a real spawn raises "
        "(recipes-30w: a run reported status 'completed' with the errored step listed in "
        "completed_steps and the error visible only inside a summary)."
    ),
)
async def bad_errored_step() -> str:
    from amplifier_recipe_runner.api import RunRequest
    from amplifier_recipe_runner.api import RunStatus
    from amplifier_recipe_runner.execution import run as run_recipe

    async def run_with(backend: FailingBackend, sink: CollectingSink) -> Any:
        return await run_recipe(
            RunRequest(
                recipe=RECIPES / "errored-step.yaml",
                services=services(workspace(), sink=sink),
            ),
            resolver=local_resolver(),
            spawn_backend=backend,
        )

    # Control FIRST: the recipe is genuinely runnable, so a FAILED result below
    # is the errored step and not a broken fixture. Without this, an
    # implementation that failed every run would "pass" this fixture.
    control_sink = CollectingSink()
    control = await run_with(FailingBackend(fail_on=-1), control_sink)
    expect_eq(control.status, RunStatus.SUCCEEDED, "control run status (the recipe must be runnable)")
    expect_eq(control.completed_steps, ("review", "summarize"), "control completed steps")
    expect(control.error is None, f"control run carried an error: {control.error!r}")

    # The second step errors: the first really finished, the second did not.
    sink = CollectingSink()
    result = await run_with(FailingBackend(fail_on=1), sink)

    expect(
        result.status is not RunStatus.SUCCEEDED,
        "a run whose step errored reported SUCCEEDED -- a fabricated success",
    )
    expect_eq(result.status, RunStatus.FAILED, "run status after a step errored")
    expect(
        isinstance(result.error, RuntimeError),
        f"the step's error is not surfaced at the top level of the result (got {result.error!r})",
    )
    expect_in(
        "No providers available",
        str(result.error),
        "the run does not carry the step's real error text at the top level",
    )
    expect_eq(
        result.completed_steps,
        ("review",),
        "completed steps must hold only the step that really finished",
    )
    expect(
        "summarize" not in result.completed_steps,
        "the errored step was reported as completed",
    )
    expect_in("step:failed", sink.kinds, "the run announced no step:failed event")

    # And when the FIRST step errors, nothing completed at all.
    first_sink = CollectingSink()
    first = await run_with(FailingBackend(fail_on=0), first_sink)
    expect_eq(first.status, RunStatus.FAILED, "run status when the first step errored")
    expect_eq(first.completed_steps, (), "completed steps when the first step errored")

    return (
        f"an untyped {type(result.error).__name__}('{result.error}') in step 'summarize' gave "
        f"status={result.status.value} with completed_steps={list(result.completed_steps)} "
        "(errored step excluded); first-step failure completed 0 steps; the faithful control "
        "still succeeded with 2 completed steps"
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
    # A directory THIS RUN owns, for its own resumable state. Justified
    # against manifest.v1 Core 4 (recipes-xov): Core 4 constrains what can
    # reach a recipe's AGENT surface, and a filesystem path cannot -- nothing
    # read back from it is consulted during agent resolution, which happens
    # exclusively against the frozen PlanCatalog. It exists because pause ->
    # approve -> resume spans separate processes, so the run that pauses at an
    # approval gate must leave its position somewhere the run that resumes can
    # read. `None` (the default) means "keep nothing".
    "state_dir",
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
    #
    #    `ProviderSpec` / `provider_specs` are NOT a sixth port: they are the
    #    shape port 1's existing `ProviderHandle` may take, and the one place
    #    that shape is interpreted (executor-parity delta 10). Step 2 above is
    #    what proves no port was added -- HostServices still has exactly five
    #    fields -- and step 4b below is what proves this shape carries no agent
    #    map.
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
                "ProviderSpec",
                "RunEvent",
                "WorkspacePath",
                "provider_specs",
            )
        ),
        "ports.__all__",
    )

    # 4. No port protocol or payload names, accepts, or returns an agent map.
    port_types = (
        ports_module.ProviderAccess,
        ports_module.ProviderSpec,
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


# ==========================================================================
# FULL STEP VOCABULARY -- the two engines, one recipe, diffed
# ==========================================================================


#: The keys `execute_recipe` injects into every context. They legitimately
#: differ between the two engines (session ids, absolute recipe paths), so a
#: diff that included them would report noise as non-conformance.
_ENGINE_INTERNAL_KEYS: tuple[str, ...] = ("recipe", "session", "step", "stage")

#: What the agent step returns, on BOTH sides. Fixed so the comparison is of
#: step semantics, not of two different model answers.
_AGENT_REPLY: str = "done:supplier:reviewer"


def legacy_engine_module() -> Any:
    """Import the legacy in-session engine, or say why it could not be found.

    Located, never stubbed -- the same rule ``_bootstrap`` applies to the
    library. A fixture that quietly compared the library against a double
    would prove nothing at all, which is the one failure mode a parity check
    cannot afford.
    """
    try:
        from amplifier_module_tool_recipes import executor as legacy  # type: ignore[import-not-found]

        return legacy
    except ImportError:
        pass

    module_root = KIT_DIR.parents[1] / "modules" / "tool-recipes"
    if module_root.is_dir() and str(module_root) not in sys.path:
        sys.path.insert(0, str(module_root))
    try:
        from amplifier_module_tool_recipes import executor as legacy  # type: ignore[import-not-found]
    except ImportError as exc:
        raise KitFailure(
            "the legacy step engine (amplifier_module_tool_recipes.executor) is not importable, "
            f"and this fixture refuses to compare the library against a stand-in. Tried {module_root}. "
            "Run the kit with PYTHONPATH=src:modules/tool-recipes, as conformance/README.md documents."
        ) from exc
    return legacy


async def _legacy_run(recipe_path: Path, project_path: Path) -> dict[str, Any]:
    """Run ``recipe_path`` on the LEGACY engine and return its final context."""
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    legacy = legacy_engine_module()
    from amplifier_module_tool_recipes.models import Recipe  # type: ignore[import-not-found]

    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None  # keeps _show_progress from awaiting a MagicMock
    spawn = AsyncMock(return_value=_AGENT_REPLY)
    coordinator.get_capability.return_value = spawn

    session_manager = MagicMock()
    session_manager.create_session.return_value = "legacy-session"
    session_manager.load_state.return_value = {
        "current_step_index": 0,
        "context": {},
        "completed_steps": [],
        "started": "2026-01-01T00:00:00",
    }
    session_manager.is_cancellation_requested.return_value = False
    session_manager.is_immediate_cancellation.return_value = False

    executor = legacy.RecipeExecutor(coordinator, session_manager)
    recipe = Recipe.from_yaml(recipe_path)
    return await executor.execute_recipe(recipe, {}, project_path, recipe_path=recipe_path)


async def _library_run(recipe_path: Path, project_path: Path) -> Any:
    """Run ``recipe_path`` on the LIBRARY, through its real public entry point."""
    from amplifier_recipe_runner.api import RunRequest
    from amplifier_recipe_runner.execution import run as run_recipe

    class FixedReplyBackend:
        async def spawn(self, request: Any) -> str:
            return _AGENT_REPLY

    return await run_recipe(
        RunRequest(recipe=recipe_path, services=services(project_path)),
        resolver=local_resolver(),
        spawn_backend=FixedReplyBackend(),
    )


def _comparable(context: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if key not in _ENGINE_INTERNAL_KEYS}


@fixture(
    id="good-full-step-vocabulary-matches-the-legacy-engine",
    polarity="GOOD",
    title="One full-vocabulary recipe run on BOTH engines produces the same context",
    clauses=("lib.v1 Core 1", "lib.v1 Core 2", "lib.v1 Core 8"),
    rows=("RCP-101", "RCP-102", "RCP-108"),
    notes=(
        "PARITY FIXTURE. Runs conformance/kit/fixtures/recipes/full-vocabulary.yaml -- bash, "
        "parse_json, conditions (taken and not taken), on_error: continue, sequential and "
        "bounded-parallel foreach, a multi-step foreach body, a convergence while loop with "
        "update_context + break_when, a `type: recipe` sub-recipe, a templated timeout and an "
        "agent step -- through the LEGACY in-session engine and through the LIBRARY's public "
        "`run()`, then diffs every recipe-visible context variable. Self-discriminating: it "
        "compares the library against the other implementation rather than against an authored "
        "expectation, so a drift in EITHER engine fails it. Requires the legacy engine to be "
        "importable and refuses to run against a stand-in."
    ),
)
async def good_full_vocabulary_parity() -> str:
    from amplifier_recipe_runner.api import RunStatus

    recipe_path = RECIPES / "full-vocabulary.yaml"
    expect(recipe_path.is_file(), f"missing parity recipe {recipe_path}")

    legacy_workspace = Path(tempfile.mkdtemp(prefix="recipes-parity-legacy-"))
    library_workspace = Path(tempfile.mkdtemp(prefix="recipes-parity-library-"))

    legacy_context = await _legacy_run(recipe_path, legacy_workspace)
    result = await _library_run(recipe_path, library_workspace)

    if result.status is not RunStatus.SUCCEEDED:
        raise KitFailure(
            f"the library could not run the full-vocabulary recipe: {result.status.value} -- "
            f"{type(result.error).__name__ if result.error else 'no error reported'}: {result.error}"
        )

    legacy_seen = _comparable(legacy_context)
    library_seen = _comparable(result.context)

    # Non-vacuity FIRST: an empty comparison would pass while proving nothing.
    expected_variables = {
        "payload",
        "restated",
        "gated_on",
        "absorbed_out",
        "absorbed_code",
        "fanned",
        "fanned_parallel",
        "compounded",
        "ticks",
        "delegated",
        "verdict",
        "settled",
    }
    missing_legacy = sorted(expected_variables - set(legacy_seen))
    missing_library = sorted(expected_variables - set(library_seen))
    expect(not missing_legacy, f"legacy engine produced none of {missing_legacy}; the comparison would be vacuous")
    expect(not missing_library, f"library produced none of {missing_library}; the comparison would be vacuous")

    # The condition that did NOT pass must have written nothing, on both sides.
    expect("gated_off" not in legacy_seen, "legacy engine ran a step whose condition was false")
    expect("gated_off" not in library_seen, "library ran a step whose condition was false")

    differing = sorted(
        key
        for key in set(legacy_seen) | set(library_seen)
        if legacy_seen.get(key, "<absent>") != library_seen.get(key, "<absent>")
    )
    if differing:
        detail = "; ".join(
            f"{key}: legacy={_brief(legacy_seen.get(key, '<absent>'), 120)} "
            f"library={_brief(library_seen.get(key, '<absent>'), 120)}"
            for key in differing
        )
        raise KitFailure(
            f"the two engines disagree on {len(differing)} context variable(s): {detail}. "
            "Every intended difference belongs in docs/EXECUTOR_PARITY.md with its reason; "
            "an undocumented one is a parity defect."
        )

    return (
        f"{len(legacy_seen)} recipe-visible variables identical across both engines "
        f"(covering {len(expected_variables)} named step outputs: bash, parse_json, conditions, "
        f"on_error, foreach sequential + parallel, compound body, convergence loop, sub-recipe, "
        f"templated timeout, agent step)"
    )


# ==========================================================================
# WHERE THE MODEL COMES FROM -- one provider-agnostic recipe, three layers
# ==========================================================================


class _StubProviderSession:
    """A composed session that answers without a model call."""

    def __init__(self, mount_plan: Mapping[str, Any]) -> None:
        self.mount_plan = dict(mount_plan)
        self.coordinator = _StubCoordinator()

    async def execute(self, instruction: str) -> str:
        return _AGENT_REPLY

    async def cleanup(self) -> None:
        return None


class _StubCoordinator:
    def __init__(self) -> None:
        self.capabilities: dict[str, Any] = {}

    def register_capability(self, name: str, value: Any) -> None:
        self.capabilities[name] = value


@dataclasses.dataclass
class _StubPrepared:
    mount_plan: dict[str, Any]
    sessions: list[_StubProviderSession] = dataclasses.field(default_factory=list)

    async def create_session(self, session_cwd: Any = None) -> _StubProviderSession:
        session = _StubProviderSession(self.mount_plan)
        self.sessions.append(session)
        return session


class _StubComposedBundle:
    """Foundation's ``Bundle``, reduced to what the provider decision reads."""

    def __init__(self, providers: list[dict[str, Any]]) -> None:
        self.providers = providers
        self.agents: dict[str, Any] = {}
        self.name = "stub"
        self.prepared: _StubPrepared | None = None

    async def prepare(self, install_deps: bool = True) -> _StubPrepared:
        self.prepared = _StubPrepared(mount_plan={"providers": list(self.providers)})
        return self.prepared


def _stub_session_factory(bundle: _StubComposedBundle) -> Any:
    """The REAL FoundationSessionFactory, composing ``bundle`` instead of fetching.

    Composition is stubbed; the provider decision under test is not. This is
    the same object the standalone CLI uses.
    """
    from amplifier_recipe_runner.execution import FoundationSessionFactory

    class _Factory(FoundationSessionFactory):
        def __init__(self) -> None:
            super().__init__(install_deps=False, registry=object())

        async def compose(self, plan: Any, catalog: Any) -> Any:
            return bundle

    return _Factory()


class _MountableProviderAccess:
    """A host whose port hands over a provider the run can actually mount."""

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def roles(self) -> tuple[str, ...]:
        return ("default",)

    def resolve(self, role: str) -> Any:
        from amplifier_recipe_runner.ports import ProviderSpec

        self.resolved.append(role)
        return ProviderSpec(
            module="provider-stub",
            source="git+https://example.invalid/provider-stub@v1",
            config={"default_model": "stub-model-1"},
            id="host-instance",
        )


#: What a recipe pins when it declares its own provider.
_PINNED_PROVIDER: dict[str, Any] = {
    "module": "provider-stub",
    "source": "git+https://example.invalid/provider-stub@v1",
    "config": {"default_model": "stub-model-1"},
}


async def _run_with_provider_layer(
    recipe_path: Path,
    project_path: Path,
    *,
    declared: list[dict[str, Any]],
    access: Any,
) -> tuple[Any, _StubComposedBundle]:
    """Run ``recipe_path`` with a given closure/port pair, through ``run()``."""
    from amplifier_recipe_runner.api import RunRequest
    from amplifier_recipe_runner.execution import run as run_recipe
    from amplifier_recipe_runner.ports import HostServices

    bundle = _StubComposedBundle(list(declared))
    request = RunRequest(
        recipe=recipe_path,
        services=HostServices(provider_access=access, workspace=project_path),
    )
    result = await run_recipe(
        request,
        resolver=local_resolver(),
        session_factory=_stub_session_factory(bundle),
    )
    return result, bundle


@fixture(
    id="good-provider-agnostic-recipe-runs-on-either-layer-and-refuses-on-neither",
    polarity="GOOD",
    title="A recipe naming no provider runs identically on the host port and on a pinned closure",
    clauses=("lib.v1 Core 4", "lib.v1 Core 8", "manifest.v1 Core 4"),
    rows=("RCP-104", "RCP-108", "RCP-004"),
    notes=(
        "PARITY FIXTURE for executor-parity delta 10. Runs "
        "conformance/kit/fixtures/recipes/provider-agnostic.yaml -- an agent step and no "
        "provider -- three ways through the library's own `run()` and the REAL "
        "FoundationSessionFactory (only composition is stubbed): (a) closure declares none "
        "and the host's provider_access port offers a mountable ProviderSpec, (b) the closure "
        "PINS one and the same port is offered, (c) neither. It then diffs every "
        "recipe-visible context variable of (a) against (b) AND against the LEGACY in-session "
        "engine's run of the same file. Self-discriminating: (a) and (b) are compared against "
        "each other and against the other implementation, not against an authored "
        "expectation. Asserts the pinned run never consults the port (a pin a host could "
        "override is not a pin), that both runs record which layer they used, and that (c) "
        "refuses naming BOTH remedies instead of returning the sub-session's error string as "
        "the step's output."
    ),
)
async def good_provider_layers_are_interchangeable_and_recorded() -> str:
    from amplifier_recipe_runner.api import RunStatus
    from amplifier_recipe_runner.execution import PROVIDER_SOURCE_HOST
    from amplifier_recipe_runner.execution import PROVIDER_SOURCE_NONE
    from amplifier_recipe_runner.execution import PROVIDER_SOURCE_RECIPE
    from amplifier_recipe_runner.execution import NoProviderError

    recipe_path = RECIPES / "provider-agnostic.yaml"
    expect(recipe_path.is_file(), f"missing provider-agnostic recipe {recipe_path}")

    # (a) The host's port supplies it, because the recipe did not.
    host = _MountableProviderAccess()
    bridged, bridged_bundle = await _run_with_provider_layer(
        recipe_path,
        Path(tempfile.mkdtemp(prefix="recipes-provider-host-")),
        declared=[],
        access=host,
    )
    expect(
        bridged.status is RunStatus.SUCCEEDED,
        f"the host-port run did not succeed: {bridged.status.value} -- {bridged.error!r}",
    )
    expect_eq(bridged.provider["provider_source"], PROVIDER_SOURCE_HOST, "bridged provider_source")
    expect_eq(bridged.provider["provider"], "host-instance", "bridged provider instance")
    expect_eq(bridged.provider["model"], "stub-model-1", "bridged model")
    expect_eq(
        bridged_bundle.providers,
        [
            {
                "module": "provider-stub",
                "source": "git+https://example.invalid/provider-stub@v1",
                "config": {"default_model": "stub-model-1"},
                "id": "host-instance",
            }
        ],
        "the host's provider as mounted into the composed closure",
    )
    expect(host.resolved == ["default"], f"the port was consulted {host.resolved} times, expected once")

    # (b) The recipe pins one. The SAME port is offered and must be ignored.
    pinning_host = _MountableProviderAccess()
    pinned, pinned_bundle = await _run_with_provider_layer(
        recipe_path,
        Path(tempfile.mkdtemp(prefix="recipes-provider-pinned-")),
        declared=[_PINNED_PROVIDER],
        access=pinning_host,
    )
    expect(
        pinned.status is RunStatus.SUCCEEDED,
        f"the pinned run did not succeed: {pinned.status.value} -- {pinned.error!r}",
    )
    expect_eq(pinned.provider["provider_source"], PROVIDER_SOURCE_RECIPE, "pinned provider_source")
    expect(
        pinning_host.resolved == [],
        "the host's port was consulted despite a pinned closure -- a pin a host can override is not a pin",
    )
    expect_eq(pinned_bundle.providers, [_PINNED_PROVIDER], "the recipe's own providers, unmodified")

    # (c) Neither. The port names a role but says nothing mountable.
    class _OpaqueAccess:
        def roles(self) -> tuple[str, ...]:
            return ("general",)

        def resolve(self, role: str) -> Any:
            return role

    refused, _ = await _run_with_provider_layer(
        recipe_path,
        Path(tempfile.mkdtemp(prefix="recipes-provider-none-")),
        declared=[],
        access=_OpaqueAccess(),
    )
    expect(refused.status is RunStatus.FAILED, f"a run with no provider reported {refused.status.value}")
    expect(
        isinstance(refused.error, NoProviderError),
        f"expected NoProviderError, got {type(refused.error).__name__}: {refused.error}",
    )
    expect_eq(refused.provider["provider_source"], PROVIDER_SOURCE_NONE, "refused provider_source")
    remedy = f"{refused.error} {getattr(refused.error, 'remedy', '')}"
    expect("dependencies:" in remedy, "the refusal does not name the recipe-side remedy")
    expect("provider_access" in remedy, "the refusal does not name the host-port remedy")
    expect(
        "verdict" not in refused.context,
        "the refused agent step still wrote an output -- an error string reported AS the step's result",
    )

    # The diff: a provider-agnostic recipe's OUTCOME does not depend on which
    # layer paid for the model, nor on which engine ran it.
    legacy_context = await _legacy_run(recipe_path, Path(tempfile.mkdtemp(prefix="recipes-provider-legacy-")))
    seen = {
        "host-port": _comparable(bridged.context),
        "recipe-closure": _comparable(pinned.context),
        "legacy in-session": _comparable(legacy_context),
    }
    expected_variables = {"state", "verdict"}
    for label, values in seen.items():
        missing = sorted(expected_variables - set(values))
        expect(not missing, f"{label} produced none of {missing}; the comparison would be vacuous")

    keys = set().union(*(set(values) for values in seen.values()))
    differing = sorted(
        key
        for key in keys
        if len({_brief(values.get(key, "<absent>"), 200) for values in seen.values()}) > 1
    )
    if differing:
        detail = "; ".join(
            f"{key}: " + ", ".join(f"{label}={_brief(values.get(key, '<absent>'), 80)}" for label, values in seen.items())
            for key in differing
        )
        raise KitFailure(
            f"the three runs disagree on {len(differing)} context variable(s): {detail}. "
            "A provider-agnostic recipe's outcome must not depend on which layer supplied its model."
        )

    return (
        f"{len(keys)} recipe-visible variables identical across host-port, recipe-closure and the "
        f"legacy in-session engine; provider_source recorded as {PROVIDER_SOURCE_HOST} / "
        f"{PROVIDER_SOURCE_RECIPE} respectively (instance 'host-instance', model 'stub-model-1'); "
        f"the pinned run never consulted the port; with neither layer the run FAILED with "
        f"NoProviderError naming both remedies and wrote no step output"
    )


# ==========================================================================
# MID-LOOP RESUME -- the two engines, one interrupted foreach, diffed
# ==========================================================================


class _DictSessions:
    """Dict-backed session state for the legacy engine.

    The full-vocabulary fixture can get away with a MagicMock session manager
    because nothing there reads state back. Mid-loop resume is *entirely*
    about reading state back, so this one has to actually hold it -- a mock
    that returned a fixed dict would make the resume half of the comparison
    vacuous while still reporting a pass.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self.state: dict[str, Any] = {}

    def create_session(self, *_args: Any, **_kwargs: Any) -> str:
        self.state = {
            "current_step_index": 0,
            "context": {},
            "completed_steps": [],
            "started": "2026-01-01T00:00:00",
        }
        return "kit-session"

    def load_state(self, _session_id: str, _project_path: Any = None) -> dict[str, Any]:
        return json.loads(json.dumps(self.state, default=str))

    def save_state(self, _session_id: str, _project_path: Any, state: dict[str, Any]) -> None:
        self.state = json.loads(json.dumps(dict(state), default=str))

    def get_session_dir(self, *_args: Any, **_kwargs: Any) -> Path:
        directory = self._root / "session"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def is_cancellation_requested(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def is_immediate_cancellation(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def cleanup_old_sessions(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get_pending_approval(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set_pending_approval(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def clear_pending_approval(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get_stage_approval_status(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def check_approval_timeout(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def mark_cancelled(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def request_cancellation(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _ran(log: Path) -> list[str]:
    """Which items the loop body actually executed, in order."""
    return log.read_text(encoding="utf-8").split() if log.is_file() else []


async def _legacy_interrupt_and_resume(recipe_path: Path, workspace: Path) -> dict[str, Any]:
    """Run, fail mid-loop, remove the injected error, resume -- on the LEGACY engine."""
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    legacy = legacy_engine_module()
    from amplifier_module_tool_recipes.models import Recipe  # type: ignore[import-not-found]

    log = workspace / "ran.log"
    marker = workspace / "fail-marker"
    marker.write_text("injected", encoding="utf-8")
    variables = {"ran_log": str(log), "fail_marker": str(marker)}

    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock(return_value=_AGENT_REPLY)

    sessions = _DictSessions(workspace)
    executor = legacy.RecipeExecutor(coordinator, sessions)
    recipe = Recipe.from_yaml(recipe_path)

    failed = False
    try:
        await executor.execute_recipe(recipe, dict(variables), workspace, recipe_path=recipe_path)
    except Exception:  # noqa: BLE001 - the injected failure is the point
        failed = True
    first = _ran(log)
    progress = sessions.state.get("foreach_progress")

    marker.unlink()
    log.unlink(missing_ok=True)
    context = await executor.execute_recipe(
        recipe, dict(variables), workspace, session_id="kit-session", recipe_path=recipe_path
    )
    return {"failed": failed, "first": first, "second": _ran(log), "seen": context.get("seen"), "progress": progress}


async def _library_interrupt_and_resume(recipe_path: Path, workspace: Path) -> dict[str, Any]:
    """The same interrupt-and-resume, through the LIBRARY's public run/resume."""
    from amplifier_recipe_runner.api import RunRequest
    from amplifier_recipe_runner.api import RunStatus
    from amplifier_recipe_runner.execution import RunStateStore
    from amplifier_recipe_runner.execution import resume as resume_recipe
    from amplifier_recipe_runner.execution import run as run_recipe

    log = workspace / "ran.log"
    marker = workspace / "fail-marker"
    marker.write_text("injected", encoding="utf-8")
    state_dir = workspace / "run-state"

    def request() -> Any:
        return RunRequest(
            recipe=recipe_path,
            context={"ran_log": str(log), "fail_marker": str(marker)},
            services=services(workspace),
            run_id="kit-run",
            state_dir=state_dir,
        )

    first_result = await run_recipe(request(), resolver=local_resolver())
    first = _ran(log)
    recorded = RunStateStore(state_dir).load() or {}
    progress = (recorded.get("engine_state") or {}).get("foreach_progress")

    marker.unlink()
    log.unlink(missing_ok=True)
    second_result = await resume_recipe(request(), resolver=local_resolver())
    return {
        "failed": first_result.status is RunStatus.FAILED,
        "first": first,
        "second": _ran(log),
        "seen": dict(second_result.context).get("seen"),
        "progress": progress,
        "status": second_result.status,
    }


@fixture(
    id="good-checkpointed-foreach-resumes-mid-loop-on-both-engines",
    polarity="GOOD",
    title="An interrupted `checkpoint_iterations:` foreach resumes mid-loop identically on BOTH engines",
    clauses=("lib.v1 Core 2", "lib.v1 Core 8"),
    rows=("RCP-102", "RCP-108"),
    notes=(
        "PARITY FIXTURE for docs/EXECUTOR_PARITY.md delta 3. Runs "
        "conformance/kit/fixtures/recipes/checkpointed-foreach.yaml on BOTH engines: five items, "
        "an injected failure at item 3 (a marker file, not a context flag -- both engines restore "
        "their saved context on resume, so a flag would be discarded), then the marker is removed "
        "and the run is resumed. Compares WHICH ITEMS EACH ENGINE ACTUALLY EXECUTED, before and "
        "after, plus the final collected results. The loop body appends to a log on disk because a "
        "restored result and a silent re-run are indistinguishable from the context alone -- the "
        "side effect is the only evidence that separates them. Exercises the library's real "
        "`run` -> `resume` pair over a state directory, so it proves the WIRING (engine -> "
        "RunStateStore -> engine) and not merely the engine. Self-discriminating: it compares the "
        "two implementations against each other, so a drift in EITHER fails it."
    ),
)
async def good_checkpointed_foreach_parity() -> str:
    from amplifier_recipe_runner.api import RunStatus

    recipe_path = RECIPES / "checkpointed-foreach.yaml"
    expect(recipe_path.is_file(), f"missing parity recipe {recipe_path}")

    legacy_workspace = Path(tempfile.mkdtemp(prefix="recipes-cp-legacy-"))
    library_workspace = Path(tempfile.mkdtemp(prefix="recipes-cp-library-"))

    legacy_seen = await _legacy_interrupt_and_resume(recipe_path, legacy_workspace)
    library_seen = await _library_interrupt_and_resume(recipe_path, library_workspace)

    # Non-vacuity FIRST. Every one of these would let a broken engine pass.
    expect(legacy_seen["failed"], "the legacy engine did not fail at the injected item; nothing was interrupted")
    expect(library_seen["failed"], "the library did not fail at the injected item; nothing was interrupted")
    expect(
        library_seen["status"] is RunStatus.SUCCEEDED,
        f"the library's resume did not succeed: {library_seen['status']}",
    )
    expect(
        legacy_seen["progress"] is not None,
        "the legacy engine recorded no foreach_progress; there was nothing to resume FROM",
    )
    expect(
        library_seen["progress"] is not None,
        "the library recorded no foreach_progress in its state directory; the store wiring is not live",
    )
    # The recipe's own list and injected failure point, restated here because
    # this fixture is what makes them mean something.
    all_items = ["a", "b", "c", "d", "e"]
    already_done = ["a", "b"]
    for engine, seen in (("legacy", legacy_seen), ("library", library_seen)):
        expect(
            seen["second"] != all_items,
            f"the {engine} engine re-ran the whole list on resume ({seen['second']}); "
            "that is a restart, not a mid-loop resume",
        )
        overlap = sorted(set(seen["second"]) & set(already_done))
        expect(
            not overlap,
            f"the {engine} engine re-executed {overlap} on resume -- those iterations were already "
            "checkpointed as complete, so re-running them is exactly the cost this field exists to avoid",
        )

    differing = [
        f"{name}: legacy={_brief(legacy_seen[name], 120)} library={_brief(library_seen[name], 120)}"
        for name in ("first", "second", "seen")
        if legacy_seen[name] != library_seen[name]
    ]
    if differing:
        raise KitFailure(
            "the two engines disagree about an interrupted foreach: "
            + "; ".join(differing)
            + ". `first`/`second` are the items each engine ACTUALLY executed before and after the "
            "resume; `seen` is the collected result. Every intended difference belongs in "
            "docs/EXECUTOR_PARITY.md with its reason; an undocumented one is a parity defect."
        )

    return (
        f"both engines ran {legacy_seen['first']} then, on resume, only {legacy_seen['second']} -- "
        f"identical collected results ({len(legacy_seen['seen'] or [])} items, index-aligned), "
        f"with mid-loop progress recorded on each side"
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
        "covered": (
            "Two independent hosts (in-process library, separate process) produce byte-identical "
            "resolved-graph identity, so neither carries resolution logic of its own. Separately, the "
            "Amplifier tool adapter is INSTALLED the way production installs it (`uv pip install -e "
            "<module> --python <env> --no-sources`, the command whose failure broke `amplifier update` "
            "on 2026-09-02) into a clean venv, and its load_runner() is measured to resolve the ONE "
            "in-bundle library rather than a second copy -- discrimination-proved in-fixture against "
            "the pre-hotfix pyproject."
        ),
        "not_covered": (
            "The ledger records 'every host surface is a thin adapter' as NOT-ASSERTABLE (architectural "
            "judgement). Only two hosts are COMPARED; the Amplifier tool adapter is exercised only as far "
            "as install + import + runner resolution, not as a runner host producing a resolved graph "
            "(residual R1)."
        ),
    },
    "RCP-102": {
        "coverage": "partial",
        "covered": (
            "`plan`, `run` and `resume` are exercised with no UI and no Amplifier CLI; `plan` is "
            "proved side-effect free (empty workspace after) and works with no host services at all. "
            "`resume` is driven end to end over a real state directory by the mid-loop foreach "
            "fixture -- run, fail, resume -- and compared against the legacy engine's own resume."
        ),
        "not_covered": "`validate` is absent from the shipped surface (residual R2), so one of the four required entry points has no fixture.",
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
        "covered": (
            "All four named preflight classes are asserted by type, not by exit code: "
            "UndeclaredAgentError, AgentCollisionError, TrustRefusedError, ProvenanceMismatchError. "
            "'Never a fabricated success' is asserted in BOTH directions: before preflight, as FAILED "
            "status with 0 completed steps and 0 spawns on a refused run; and after it, as an "
            "untyped step failure giving FAILED status with the error at the top level of the result "
            "and the errored step absent from completed_steps -- against a faithful control run of "
            "the same recipe that still succeeds."
        ),
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
    skipped: bool = False

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        return "PASS" if self.passed else "FAIL"


async def run_fixture(item: Fixture) -> Result:
    import time

    started = time.monotonic()
    try:
        detail = await item.run()
        return Result(item, True, detail, duration_s=time.monotonic() - started)
    except KitSkipped as exc:
        # Not a pass. Reported as SKIP with its reason and counted separately,
        # so an environment that could not run a check never reads as one where
        # the check held.
        return Result(item, True, str(exc), duration_s=time.monotonic() - started, skipped=True)
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
    skipped = [r for r in results if r.skipped]

    if as_json:
        json.dump(
            {
                "implementation": provenance,
                "total": len(results),
                "passed": len(results) - len(failed) - len(skipped),
                "failed": len(failed),
                "skipped": len(skipped),
                "results": [
                    {
                        "id": r.fixture.id,
                        "polarity": r.fixture.polarity,
                        "status": r.status,
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
        print(f"[{r.status}] {r.fixture.polarity:<4} {r.fixture.id}  ({r.duration_s:.2f}s)")
        print(f"       {r.detail}")
        if r.trace:
            print("       " + r.trace.replace("\n", "\n       ").rstrip())
    print()
    print(f"{len(results) - len(failed) - len(skipped)}/{len(results)} fixtures passed")
    if skipped:
        # Named, never a silent absence: a skipped fixture checked nothing.
        print(f"\nSKIPPED ({len(skipped)} -- checked NOTHING, not a pass):")
        for r in skipped:
            print(f"  - {r.fixture.id} [{', '.join(r.fixture.rows)}]")
            print(f"      {r.detail}")
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
