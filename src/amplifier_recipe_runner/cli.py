"""The ``recipe-runner`` command line -- a thin adapter over the library.

Contract: ``recipe-runner-lib.v1`` Core 1 (one execution home) and Core 2
(public API usable with no UI), plus ``recipe-dependency-manifest.v1`` Core 10
(the standalone runner REFUSES a legacy recipe).

**What this module is allowed to do.** Parse arguments, resolve the three-tier
configuration, build the request/policy/host objects the library takes, call
the library, render what it returns, and pick an exit code. That is the whole
list.

**What it must never do.** Resolve a dependency, build an agent catalog, decide
what a step means, or execute anything. Every one of those lives in the
library, and a copy here would be a second execution home -- exactly what
Core 1 forbids. When a capability is missing from the library, this CLI says
so and exits; it does not reimplement it (see :func:`resume`).

Machine-readable output
-----------------------

``--json`` is accepted both before the subcommand (``recipe-runner --json
plan ...``) and on ``validate`` / ``plan`` / ``run`` themselves
(``recipe-runner plan --json ...``); the subcommand's own flag wins, so
``--json plan --text`` is text. Either spelling makes the same promise:

* **stdout carries exactly one JSON document and nothing else.** Every
  human or diagnostic line moves to stderr (:func:`_note`), so another host
  can ``json.loads`` the stream without stripping prose first.
* the document is the library's own documented run-manifest shape (lib
  Core 7) -- the resolved graph's dependencies, per-agent provenance, and
  policy, exactly as :class:`~amplifier_recipe_runner.api.ExecutionPlan`
  already models them. This CLI reshapes nothing and computes nothing; two
  hosts planning the same recipe therefore report the same identity, which
  is what lib Core 1's conformance section asks to be checkable.

Three-tier resolution
---------------------

Every flag defaults to ``None`` so "not passed" is distinguishable from
"passed the default value". :func:`_pick` then applies, in order:

1. the flag, when it is not ``None``;
2. the config file key, when present and not null;
3. the built-in default.

Exit codes
----------

=====  ======================================================================
Code   Meaning
=====  ======================================================================
0      Success.
1      Generic failure (an unexpected error escaped the typed model).
2      Usage error (Click's own).
3      Typed preflight refusal -- nothing ran.
4      Legacy recipe refused (manifest Core 10). Distinct on purpose.
5      Provenance mismatch on resume (manifest Core 8).
6      A real, named capability this library version does not provide.
=====  ======================================================================
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Final

import click
import yaml

from . import __version__
from .api import ExecutionPlan
from .api import LockMode
from .api import RunRequest
from .api import RunResult
from .api import RunStatus
from .api import ValidationIssue
from .api import ValidationReport
from .errors import LegacyRecipeError
from .errors import PreflightError
from .errors import ProvenanceMismatchError
from .execution import plan as plan_recipe
from .execution import run as run_recipe
from .lockfile import LockResult
from .lockfile import apply_lock_mode
from .lockfile import lock_path_for
from .manifest import ManifestError
from .ports import HostServices
from .ports import ProviderHandle
from .ports import WorkspacePath
from .provenance import check_resume_provenance
from .provenance import read_run_manifest
from .provenance import run_manifest_from_plan
from .provenance import run_manifest_path_for
from .provenance import write_run_manifest
from .resolver import DependencyResolver
from .resolver import LocalBundleResolver
from .trust import TrustPolicy

__all__ = [
    "EXIT_FAILURE",
    "EXIT_LEGACY_RECIPE",
    "EXIT_OK",
    "EXIT_PREFLIGHT",
    "EXIT_PROVENANCE_MISMATCH",
    "EXIT_UNSUPPORTED",
    "EXIT_USAGE",
    "RunnerFailure",
    "UnsupportedCapabilityError",
    "cli",
    "exit_code_for",
    "main",
]

EXIT_OK: Final[int] = 0
EXIT_FAILURE: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_PREFLIGHT: Final[int] = 3
EXIT_LEGACY_RECIPE: Final[int] = 4
EXIT_PROVENANCE_MISMATCH: Final[int] = 5
EXIT_UNSUPPORTED: Final[int] = 6

#: Config filenames searched under the workspace, in order.
CONFIG_FILENAMES: Final[tuple[str, ...]] = (
    "recipe-runner.yaml",
    "recipe-runner.yml",
    ".recipe-runner.yaml",
)

#: User-level config, searched after the workspace.
USER_CONFIG: Final[Path] = Path("~/.config/recipe-runner/config.yaml")

#: Recognised config keys. An unknown key is an error, never ignored -- the
#: same posture the manifest parser takes toward unknown manifest keys.
CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "dry_run",
        "json",
        "lock_mode",
        "offline",
        "provider_roles",
        "state_dir",
        "trust",
        "workspace",
    }
)

#: Where run state lives, relative to the workspace, unless overridden.
DEFAULT_STATE_DIR: Final[str] = ".recipe-runner/runs"

#: Sidecar recording how a run was invoked, so ``resume`` can re-resolve it.
#: The library's run manifest records what a run *resolved to*, not the
#: command that produced it; that is host state, so the host stores it.
RUN_CONTEXT_FILENAME: Final[str] = "run.json"

TRUST_CHOICES: Final[tuple[str, ...]] = ("ci", "interactive", "none")


# --------------------------------------------------------------------------
# Failure rendering -- one place, no tracebacks
# --------------------------------------------------------------------------


class UnsupportedCapabilityError(Exception):
    """A real, named capability this library version does not provide.

    Deliberately not a :class:`~amplifier_recipe_runner.errors.RecipeRunnerError`:
    the library never raised it. It exists so the CLI can report a missing
    capability honestly instead of faking one or reimplementing it here.
    """

    def __init__(self, message: str, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy


class RunnerFailure(click.ClickException):
    """A library error, rendered as a message and a remedy.

    Click prints this and exits with :attr:`exit_code`; the user never sees a
    traceback for an error the library already modelled.
    """

    def __init__(self, exc: BaseException) -> None:
        super().__init__(str(getattr(exc, "message", None) or exc))
        self.cause = exc
        self.remedy = getattr(exc, "remedy", None)
        self.exit_code = exit_code_for(exc)

    def show(self, file: Any = None) -> None:
        click.echo(f"error: {self.message}", err=True)
        if self.remedy:
            click.echo(f"remedy: {self.remedy}", err=True)


def exit_code_for(exc: BaseException) -> int:
    """Map a library error to its exit code. Order matters: the most specific
    error wins, so a legacy refusal never collapses into "some preflight
    error"."""
    if isinstance(exc, LegacyRecipeError):
        return EXIT_LEGACY_RECIPE
    if isinstance(exc, ProvenanceMismatchError):
        return EXIT_PROVENANCE_MISMATCH
    if isinstance(exc, (PreflightError, ManifestError)):
        return EXIT_PREFLIGHT
    if isinstance(exc, UnsupportedCapabilityError):
        return EXIT_UNSUPPORTED
    return EXIT_FAILURE


def _issue_for(exc: BaseException) -> ValidationIssue:
    """The library error as a :class:`ValidationIssue`, for report rendering."""
    return ValidationIssue(
        code=type(exc).__name__,
        message=str(getattr(exc, "message", None) or exc),
        location=str(getattr(exc, "location", None) or getattr(exc, "source", None) or "") or None,
        remedy=getattr(exc, "remedy", None),
    )


# --------------------------------------------------------------------------
# Three-tier configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Runtime:
    """Shared, already-resolved invocation context."""

    workspace: Path
    config: Mapping[str, Any]
    config_path: Path | None
    json_output: bool


def _pick(flag: Any, config: Mapping[str, Any], key: str, default: Any) -> Any:
    """flag > config > default. ``None`` means "the flag was not passed"."""
    if flag is not None:
        return flag
    value = config.get(key)
    if value is not None:
        return value
    return default


def _find_config(explicit: Path | None, workspace: Path) -> Path | None:
    if explicit is not None:
        return explicit
    for name in CONFIG_FILENAMES:
        candidate = workspace / name
        if candidate.is_file():
            return candidate
    user = USER_CONFIG.expanduser()
    return user if user.is_file() else None


def _load_config(path: Path | None) -> Mapping[str, Any]:
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise click.UsageError(f"config {str(path)!r} could not be read: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise click.UsageError(f"config {str(path)!r} must be a YAML mapping, got {type(data).__name__}")
    unknown = sorted(k for k in data if k not in CONFIG_KEYS)
    if unknown:
        known = ", ".join(sorted(CONFIG_KEYS))
        raise click.UsageError(f"config {str(path)!r} has unknown key(s): {', '.join(unknown)}. Known keys: {known}")
    return dict(data)


# --------------------------------------------------------------------------
# Building the objects the library takes
# --------------------------------------------------------------------------


def _lock_mode(flag: str | None, config: Mapping[str, Any]) -> LockMode:
    raw = _pick(flag, config, "lock_mode", LockMode.LOCKED.value)
    try:
        return LockMode(raw)
    except ValueError as exc:
        raise click.UsageError(f"unsupported lock mode {raw!r}; use one of: locked, update-lock, unlocked") from exc


def _trust_policy(flag: str | None, config: Mapping[str, Any]) -> TrustPolicy | None:
    name = str(_pick(flag, config, "trust", "interactive"))
    if name == "none":
        return None
    if name == "ci":
        return TrustPolicy.ci()
    if name == "interactive":
        return TrustPolicy.interactive()
    raise click.UsageError(f"unknown trust posture {name!r}; use one of: {', '.join(TRUST_CHOICES)}")


def _resolver(flag: bool | None, config: Mapping[str, Any]) -> DependencyResolver | None:
    """Offline resolution injects the library's local resolver (lib Core 5).

    ``None`` means "use the library's default", which is Foundation-backed.
    """
    offline = bool(_pick(flag, config, "offline", False))
    return LocalBundleResolver() if offline else None


class _ConfiguredProviderAccess:
    """The provider-access port, populated from config.

    A :class:`~amplifier_recipe_runner.ports.ProviderHandle` is opaque to the
    runner, so this host hands back the role name itself. Which roles exist is
    a host decision, which is exactly why it comes from config.
    """

    __slots__ = ("_roles",)

    def __init__(self, roles: Sequence[str]) -> None:
        self._roles = tuple(str(r) for r in roles)

    def roles(self) -> Sequence[str]:
        return self._roles

    def resolve(self, role: str) -> ProviderHandle:
        if role not in self._roles:
            raise KeyError(role)
        return ProviderHandle(role)


def _services(runtime: Runtime) -> HostServices:
    roles = _pick(None, runtime.config, "provider_roles", ["general"])
    if isinstance(roles, str):
        roles = [roles]
    return HostServices(
        provider_access=_ConfiguredProviderAccess(roles),  # type: ignore[arg-type]
        workspace=WorkspacePath(runtime.workspace),
    )


def _context(pairs: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise click.UsageError(f"--set expects KEY=VALUE, got {pair!r}")
        values[key] = value
    return values


def _state_dir(flag: Path | None, runtime: Runtime) -> Path:
    raw = _pick(flag, runtime.config, "state_dir", None)
    if raw is None:
        return runtime.workspace / DEFAULT_STATE_DIR
    return Path(raw).expanduser()


def _request(
    recipe: Path,
    *,
    runtime: Runtime,
    lock_mode: LockMode,
    trust: TrustPolicy | None,
    services: HostServices | None = None,
    context: Mapping[str, Any] | None = None,
    run_id: str | None = None,
) -> RunRequest:
    return RunRequest(
        recipe=recipe,
        context=dict(context or {}),
        services=services,
        trust_policy=trust,
        lock_mode=lock_mode,
        run_id=run_id,
        # Never True here: labeled caller-bound legacy mode belongs to the
        # embedded Amplifier tool adapter, not to the standalone runner
        # (manifest Core 10).
        legacy_mode=False,
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _plan_mapping(plan: ExecutionPlan, *, run_id: str) -> dict[str, Any]:
    """The plan in the library's documented run-manifest shape (lib Core 7)."""
    return run_manifest_from_plan(plan, run_id=run_id).to_mapping()


def _note(message: str, *, as_json: bool) -> None:
    """One human or diagnostic line.

    Under ``--json`` it goes to *stderr*, so stdout stays exactly one JSON
    document. Without ``--json`` it goes to stdout, unchanged.
    """
    click.echo(message, err=as_json)


def _echo_plan(plan: ExecutionPlan, recipe: Path, *, as_json: bool, run_id: str) -> None:
    if as_json:
        click.echo(json.dumps(_plan_mapping(plan, run_id=run_id), indent=2, sort_keys=True))
        return

    click.echo(f"recipe: {recipe}")
    click.echo(f"schema_version: {plan.schema_version}")
    click.echo(f"recipe_digest: {plan.recipe_digest}")
    if plan.policy is not None:
        click.echo(f"lock_mode: {plan.policy.lock_mode.value}")
        click.echo(f"trust_policy: {plan.policy.trust_policy or '(none)'}")
        click.echo(f"isolated: {plan.policy.isolated}")

    click.echo(f"dependencies ({len(plan.dependencies)}):")
    for dep in plan.dependencies:
        identity = dep.resolved_revision or dep.content_digest or "(unresolved)"
        click.echo(f"  - {dep.uri} [{dep.kind.value}] -> {identity}")

    click.echo(f"agents ({len(plan.agents)}):")
    for name in sorted(plan.agents):
        provenance = plan.agents[name]
        alias = f" (alias {provenance.alias})" if provenance.alias else ""
        click.echo(f"  - {name}{alias} <- {provenance.supplied_by}")

    click.echo(f"steps: {', '.join(plan.step_ids) if plan.step_ids else '(none)'}")


def _echo_report(report: ValidationReport, recipe: Path, *, as_json: bool) -> None:
    if as_json:
        click.echo(
            json.dumps(
                {
                    "recipe": str(recipe),
                    "ok": report.ok,
                    "legacy": report.legacy,
                    "schema_version": report.schema_version,
                    "errors": [
                        {"code": i.code, "message": i.message, "location": i.location, "remedy": i.remedy}
                        for i in report.errors
                    ],
                    "warnings": [
                        {"code": i.code, "message": i.message, "location": i.location, "remedy": i.remedy}
                        for i in report.warnings
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    click.echo(f"recipe: {recipe}")
    click.echo(f"status: {'ok' if report.ok else 'invalid'}")
    if report.schema_version is not None:
        click.echo(f"schema_version: {report.schema_version}")
    if report.legacy:
        click.echo("legacy: true")
    for issue in report.errors:
        click.echo(f"error: [{issue.code}] {issue.message}")
        if issue.remedy:
            click.echo(f"remedy: {issue.remedy}")
    for issue in report.warnings:
        click.echo(f"warning: [{issue.code}] {issue.message}")


def _echo_lock(result: LockResult, *, as_json: bool = False) -> None:
    """The lock summary as a human line.

    ``as_json`` moves it to stderr for a caller whose stdout is reserved for
    one JSON document; ``lock`` itself, whose whole output IS this summary,
    leaves it on stdout.
    """
    verb = "wrote" if result.rewritten else "verified"
    if result.lock is None:
        _note(f"lock: {result.mode.value} (no lockfile read or written)", as_json=as_json)
    else:
        _note(
            f"lock: {verb} {result.path} ({len(result.lock.entries)} entries, mode {result.mode.value})",
            as_json=as_json,
        )
    for warning in result.warnings:
        click.echo(f"warning: {warning}", err=True)


def _lock_mapping(result: LockResult) -> dict[str, Any]:
    """The lock summary as data. Same facts as :func:`_echo_lock`, no prose."""
    return {
        "mode": result.mode.value,
        "path": str(result.path) if result.lock is not None else None,
        "entries": len(result.lock.entries) if result.lock is not None else None,
        "rewritten": result.rewritten,
        "warnings": list(result.warnings),
    }


def _echo_run(result: RunResult) -> None:
    click.echo(f"run_id: {result.run_id}")
    click.echo(f"status: {result.status.value}")
    if result.completed_steps:
        click.echo(f"completed_steps: {', '.join(result.completed_steps)}")
    if result.pending_approval:
        click.echo(f"pending_approval: {result.pending_approval}")


def _echo_run_json(
    *,
    run_id: str,
    plan: ExecutionPlan,
    lock_result: LockResult,
    provenance: Path,
    result: RunResult | None,
) -> None:
    """The whole run, as one JSON document on stdout.

    ``result`` is ``None`` for ``--dry-run``: nothing executed, so ``status``
    and ``completed_steps`` report exactly that rather than inventing an
    outcome. ``plan`` is the same run-manifest shape ``plan --json`` emits, so
    the resolved-graph identity is readable from either command.
    """
    click.echo(
        json.dumps(
            {
                "run_id": run_id,
                "dry_run": result is None,
                "status": result.status.value if result is not None else None,
                "completed_steps": list(result.completed_steps) if result is not None else [],
                "pending_approval": result.pending_approval if result is not None else None,
                "provenance": str(provenance),
                "lock": _lock_mapping(lock_result),
                "plan": _plan_mapping(plan, run_id=run_id),
            },
            indent=2,
            sort_keys=True,
        )
    )


# --------------------------------------------------------------------------
# Shared option decorators
# --------------------------------------------------------------------------


def lock_options(command: Any) -> Any:
    """``--locked`` / ``--unlocked`` / ``--update-lock``, one destination."""
    command = click.option(
        "--update-lock",
        "lock_mode",
        flag_value=LockMode.UPDATE_LOCK.value,
        default=None,
        help="Re-resolve and rewrite the lockfile.",
    )(command)
    command = click.option(
        "--unlocked",
        "lock_mode",
        flag_value=LockMode.UNLOCKED.value,
        default=None,
        help="Do not read or write the lockfile (interactive only; warns).",
    )(command)
    command = click.option(
        "--locked",
        "lock_mode",
        flag_value=LockMode.LOCKED.value,
        default=None,
        help="Require exact lock entries (default; mandatory for CI).",
    )(command)
    return command


def json_options(command: Any) -> Any:
    """``--json`` / ``--text`` on the subcommand itself.

    The group carries the same pair, so both spellings work. ``None`` means
    "not passed here", which is how :func:`_as_json` lets the subcommand's
    choice win over the group's without erasing it.
    """
    return click.option(
        "--json/--text",
        "json_output",
        default=None,
        help="Machine-readable output: one JSON document on stdout, diagnostics on stderr.",
    )(command)


def _as_json(flag: bool | None, runtime: Runtime) -> bool:
    """Subcommand flag > group flag > config > default."""
    return runtime.json_output if flag is None else bool(flag)


def resolution_options(command: Any) -> Any:
    """Trust posture and resolver selection."""
    command = click.option(
        "--offline/--online",
        "offline",
        default=None,
        help="Resolve dependencies with the library's local resolver only.",
    )(command)
    command = click.option(
        "--trust",
        "trust",
        type=click.Choice(TRUST_CHOICES),
        default=None,
        help="Trust posture handed to the library (default: interactive).",
    )(command)
    return command


# --------------------------------------------------------------------------
# The command group
# --------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="recipe-runner")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None, help="Config file to read.")
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory the run may read and write (the workspace port).",
)
@click.option("--json/--text", "json_output", default=None, help="Machine-readable output.")
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None, workspace: Path | None, json_output: bool | None) -> None:
    """Run dependency-declared Amplifier recipes in isolation.

    Every subcommand is a thin adapter over ``amplifier_recipe_runner``: this
    command line carries no workflow, resolution, or agent-catalog logic.
    """
    if config_path is not None and not config_path.is_file():
        raise click.UsageError(f"config {str(config_path)!r} does not exist")

    # Workspace must be settled before the config search, since the search
    # looks inside it -- but the config may also *name* the workspace, so a
    # config found relative to the CWD still gets its say.
    found = _find_config(config_path, (workspace or Path.cwd()).expanduser())
    config = _load_config(found)
    resolved_workspace = Path(_pick(workspace, config, "workspace", Path.cwd())).expanduser().resolve()

    ctx.obj = Runtime(
        workspace=resolved_workspace,
        config=config,
        config_path=found,
        json_output=bool(_pick(json_output, config, "json", False)),
    )


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


@cli.command("validate")
@click.argument("recipe", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@lock_options
@resolution_options
@json_options
@click.pass_context
def validate_command(
    ctx: click.Context,
    recipe: Path,
    lock_mode: str | None,
    trust: str | None,
    offline: bool | None,
    json_output: bool | None,
) -> None:
    """Check RECIPE's manifest and dependency plan. Executes nothing."""
    runtime: Runtime = ctx.obj
    as_json = _as_json(json_output, runtime)
    request = _request(
        recipe,
        runtime=runtime,
        lock_mode=_lock_mode(lock_mode, runtime.config),
        trust=_trust_policy(trust, runtime.config),
    )
    resolver = _resolver(offline, runtime.config)

    try:
        plan = asyncio.run(plan_recipe(request, resolver=resolver))
    except (PreflightError, ManifestError) as exc:
        report = ValidationReport(
            ok=False,
            schema_version=None,
            legacy=isinstance(exc, LegacyRecipeError),
            errors=(_issue_for(exc),),
        )
        _echo_report(report, recipe, as_json=as_json)
        ctx.exit(exit_code_for(exc))
    except Exception as exc:  # noqa: BLE001 - one place turns anything into a message
        raise RunnerFailure(exc) from exc

    _echo_report(
        ValidationReport(ok=True, schema_version=plan.schema_version, legacy=False),
        recipe,
        as_json=as_json,
    )


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


@cli.command("plan")
@click.argument("recipe", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@lock_options
@resolution_options
@json_options
@click.pass_context
def plan_command(
    ctx: click.Context,
    recipe: Path,
    lock_mode: str | None,
    trust: str | None,
    offline: bool | None,
    json_output: bool | None,
) -> None:
    """Resolve RECIPE's dependency closure and print the plan. Runs nothing.

    Under ``--json`` stdout is exactly the library's run-manifest mapping --
    dependencies, per-agent provenance, policy, step ids -- and nothing else,
    so a second host can compare resolved-graph identity byte for byte.
    """
    runtime: Runtime = ctx.obj
    request = _request(
        recipe,
        runtime=runtime,
        lock_mode=_lock_mode(lock_mode, runtime.config),
        trust=_trust_policy(trust, runtime.config),
    )
    try:
        plan = asyncio.run(plan_recipe(request, resolver=_resolver(offline, runtime.config)))
    except Exception as exc:  # noqa: BLE001
        raise RunnerFailure(exc) from exc

    _echo_plan(plan, recipe, as_json=_as_json(json_output, runtime), run_id="(plan)")


# --------------------------------------------------------------------------
# lock
# --------------------------------------------------------------------------


@cli.command("lock")
@click.argument("recipe", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@resolution_options
@click.pass_context
def lock_command(ctx: click.Context, recipe: Path, trust: str | None, offline: bool | None) -> None:
    """Resolve RECIPE and write its sidecar lockfile.

    Writing a lock is always explicit: this command is the only one that
    rewrites it (manifest Core 8 -- locks are never updated silently on run).
    """
    runtime: Runtime = ctx.obj
    request = _request(
        recipe,
        runtime=runtime,
        lock_mode=LockMode.UPDATE_LOCK,
        trust=_trust_policy(trust, runtime.config),
    )
    try:
        plan = asyncio.run(plan_recipe(request, resolver=_resolver(offline, runtime.config)))
        result = apply_lock_mode(plan, path=lock_path_for(recipe), mode=LockMode.UPDATE_LOCK)
    except Exception as exc:  # noqa: BLE001
        raise RunnerFailure(exc) from exc

    _echo_lock(result)


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


@cli.command("run")
@click.argument("recipe", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@lock_options
@resolution_options
@click.option("--run-id", "run_id", default=None, help="Run identifier; generated when omitted.")
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where run provenance is recorded.",
)
@click.option("--dry-run", "dry_run", is_flag=True, default=None, help="Preflight only; execute no step.")
@click.option("--set", "context_pairs", multiple=True, metavar="KEY=VALUE", help="Recipe context variable.")
@json_options
@click.pass_context
def run_command(
    ctx: click.Context,
    recipe: Path,
    lock_mode: str | None,
    trust: str | None,
    offline: bool | None,
    run_id: str | None,
    state_dir: Path | None,
    dry_run: bool | None,
    context_pairs: tuple[str, ...],
    json_output: bool | None,
) -> None:
    """Preflight RECIPE, record its provenance, then execute it.

    Under ``--json`` the lock, run-id, and provenance lines move to stderr and
    stdout carries one document: the outcome plus the same resolved-graph
    shape ``plan --json`` reports.
    """
    runtime: Runtime = ctx.obj
    as_json = _as_json(json_output, runtime)
    mode = _lock_mode(lock_mode, runtime.config)
    identifier = run_id or f"run-{uuid.uuid4().hex[:12]}"
    request = _request(
        recipe,
        runtime=runtime,
        lock_mode=mode,
        trust=_trust_policy(trust, runtime.config),
        services=_services(runtime),
        context=_context(context_pairs),
        run_id=identifier,
    )
    resolver = _resolver(offline, runtime.config)
    run_dir = _state_dir(state_dir, runtime) / identifier

    try:
        # Preflight first, so lock verification and provenance recording both
        # happen before any step could run. The library's `run` re-plans
        # internally -- it owns that -- which is why nothing here is passed
        # forward as a pre-resolved graph.
        plan = asyncio.run(plan_recipe(request, resolver=resolver))
        lock_result = apply_lock_mode(plan, path=lock_path_for(recipe), mode=mode)
        _record_run(run_dir, plan, identifier, recipe=recipe, mode=mode, trust=trust, offline=offline)
    except Exception as exc:  # noqa: BLE001
        raise RunnerFailure(exc) from exc

    provenance = run_manifest_path_for(run_dir)
    _echo_lock(lock_result, as_json=as_json)
    _note(f"run_id: {identifier}", as_json=as_json)
    _note(f"provenance: {provenance}", as_json=as_json)

    if bool(_pick(dry_run, runtime.config, "dry_run", False)):
        _note(f"dry-run: preflight ok; {len(plan.step_ids)} step(s) would run", as_json=as_json)
        if as_json:
            _echo_run_json(
                run_id=identifier,
                plan=plan,
                lock_result=lock_result,
                provenance=provenance,
                result=None,
            )
        return

    try:
        result = asyncio.run(run_recipe(request, resolver=resolver))
    except Exception as exc:  # noqa: BLE001
        raise RunnerFailure(exc) from exc

    if as_json:
        _echo_run_json(
            run_id=identifier,
            plan=plan,
            lock_result=lock_result,
            provenance=provenance,
            result=result,
        )
    else:
        _echo_run(result)
    # Record the outcome BEFORE failing, so an interrupted run stays resumable.
    _record_outcome(run_dir, result)
    if result.error is not None:
        raise RunnerFailure(result.error)
    if result.status is not RunStatus.SUCCEEDED:
        ctx.exit(EXIT_FAILURE)


def _record_run(
    run_dir: Path,
    plan: ExecutionPlan,
    run_id: str,
    *,
    recipe: Path,
    mode: LockMode,
    trust: str | None,
    offline: bool | None,
) -> None:
    """Persist the library's run manifest, plus how the run was invoked."""
    write_run_manifest(run_manifest_path_for(run_dir), run_manifest_from_plan(plan, run_id=run_id))
    context = {
        "run_id": run_id,
        "recipe": str(Path(recipe).resolve()),
        "lock_mode": mode.value,
        "trust": trust,
        "offline": offline,
    }
    _write_run_context(run_dir, context)


def _record_outcome(run_dir: Path, result: RunResult) -> None:
    """Record what the library reported, so ``resume`` can tell the three
    resumable states apart.

    Values are copied verbatim off :class:`RunResult`; nothing here decides
    what ran. The library owns that and already said so.
    """
    context = dict(_run_context(run_dir))
    context["status"] = result.status.value
    context["completed_steps"] = list(result.completed_steps)
    _write_run_context(run_dir, context)


def _write_run_context(run_dir: Path, context: Mapping[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / RUN_CONTEXT_FILENAME).write_text(
        json.dumps(dict(context), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------


@cli.command("resume")
@click.argument("run_id")
@lock_options
@resolution_options
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where run provenance was recorded.",
)
@click.option(
    "--recipe",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Recipe to re-resolve; defaults to the one this run recorded.",
)
@click.option("--dry-run", "dry_run", is_flag=True, default=None, help="Report what resuming would do.")
@click.pass_context
def resume_command(
    ctx: click.Context,
    run_id: str,
    lock_mode: str | None,
    trust: str | None,
    offline: bool | None,
    state_dir: Path | None,
    recipe: Path | None,
    dry_run: bool | None,
) -> None:
    """Continue RUN_ID from its recorded provenance.

    Provenance is checked first: a recipe edited since the run was recorded
    fails visibly rather than silently re-resolving (manifest Core 8). What
    happens next depends on what the run actually recorded:

    * every step completed -- nothing to resume, and saying so is the correct
      answer, not a failure;
    * no step completed -- resuming *is* running from the start, which is one
      library call;
    * some steps completed -- continuing mid-run needs a library ``resume``
      entry point this version does not export, so it says so rather than
      re-running completed steps behind your back.
    """
    runtime: Runtime = ctx.obj
    run_dir = _state_dir(state_dir, runtime) / run_id
    manifest_path = run_manifest_path_for(run_dir)
    if not manifest_path.is_file():
        raise click.UsageError(
            f"no run provenance for {run_id!r} at {manifest_path}. "
            "Pass --state-dir if the run recorded its state elsewhere."
        )

    recipe_path = recipe or _recorded_recipe(run_dir)
    if recipe_path is None:
        raise click.UsageError(
            f"run {run_id!r} did not record which recipe it ran; pass --recipe to say which one to re-resolve."
        )

    recorded_context = _run_context(run_dir)
    request = _request(
        recipe_path,
        runtime=runtime,
        lock_mode=_lock_mode(lock_mode or recorded_context.get("lock_mode"), runtime.config),
        trust=_trust_policy(trust or recorded_context.get("trust"), runtime.config),
        services=_services(runtime),
        run_id=run_id,
    )
    resolver = _resolver(offline if offline is not None else recorded_context.get("offline"), runtime.config)

    try:
        recorded = read_run_manifest(manifest_path)
        fresh = asyncio.run(plan_recipe(request, resolver=resolver))
        check_resume_provenance(recorded, fresh, run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        raise RunnerFailure(exc) from exc

    click.echo(f"run_id: {run_id}")
    click.echo(f"recipe: {recipe_path}")
    click.echo(f"provenance: verified against {manifest_path}")
    click.echo(f"recorded_steps: {', '.join(recorded.step_ids) if recorded.step_ids else '(none)'}")

    completed = tuple(str(step) for step in (recorded_context.get("completed_steps") or ()))
    click.echo(f"completed_steps: {', '.join(completed) if completed else '(none)'}")

    if _run_is_finished(recorded_context, recorded.step_ids, completed):
        click.echo("resume: nothing to resume; this run already completed every recorded step.")
        return

    if completed:
        # The only genuinely blocked case: continuing mid-run needs the library
        # to skip completed steps, and re-running them here would be both wrong
        # and a second execution home.
        raise RunnerFailure(
            UnsupportedCapabilityError(
                f"Run {run_id!r} stopped after {len(completed)} of {len(recorded.step_ids)} step(s), and "
                "continuing mid-run is not available: this library version exports no `resume` entry point, "
                "so the completed steps cannot be skipped.",
                remedy=(
                    "Start a fresh run with `recipe-runner run` to redo every step (the recorded provenance "
                    "above is unchanged), or upgrade to a runner version whose library exposes resume."
                ),
            )
        )

    if bool(_pick(dry_run, runtime.config, "dry_run", False)):
        click.echo(f"dry-run: would resume by running {len(recorded.step_ids)} step(s) from the start")
        return

    # Nothing completed, so resuming IS running from the start -- one library
    # call, with the recorded run id, against the provenance just verified.
    try:
        result = asyncio.run(run_recipe(request, resolver=resolver))
    except Exception as exc:  # noqa: BLE001
        raise RunnerFailure(exc) from exc

    _echo_run(result)
    _record_outcome(run_dir, result)
    if result.error is not None:
        raise RunnerFailure(result.error)
    if result.status is not RunStatus.SUCCEEDED:
        ctx.exit(EXIT_FAILURE)


def _run_is_finished(context: Mapping[str, Any], recorded_steps: tuple[str, ...], completed: tuple[str, ...]) -> bool:
    """Whether the recorded run has nothing left to do.

    Reads the status the library reported, falling back to step coverage when
    a run was recorded but never executed (a preflight-only ``--dry-run``).
    """
    if context.get("status") == RunStatus.SUCCEEDED.value:
        return True
    return bool(recorded_steps) and set(completed) >= set(recorded_steps)


def _run_context(run_dir: Path) -> Mapping[str, Any]:
    path = run_dir / RUN_CONTEXT_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, Mapping) else {}


def _recorded_recipe(run_dir: Path) -> Path | None:
    recorded = _run_context(run_dir).get("recipe")
    return Path(str(recorded)) if recorded else None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> None:
    """Console-script entry point (``[project.scripts] recipe-runner``)."""
    cli()
