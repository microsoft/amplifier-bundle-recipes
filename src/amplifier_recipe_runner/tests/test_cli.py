"""Tests for the ``recipe-runner`` command line (manifest.v1 Core 10; lib.v1 Core 1, 2).

Everything here runs through Click's :class:`~click.testing.CliRunner` against
LOCAL fixture bundles under ``fixtures/cli/``: no network, no git clone, no
Foundation, no model. Two subprocess checks are the exceptions, and they only
prove the *entry points* (``python -m`` and the console script declaration),
not behaviour.

The discriminating pairs the contract names are all present:

* GOOD -- a ``schema_version: 2`` recipe validates, plans, locks, and passes
  preflight, with the same resolved-graph identity the library reports.
* BAD -- a legacy recipe (no ``schema_version``/``dependencies``) is REFUSED
  with a remedy naming *both* options, under an exit code distinct from a
  generic failure (Core 10).
* BAD -- an undeclared agent surfaces as a readable message and a remedy, never
  a traceback.
* BAD -- a recipe edited after a run refuses to resume, rather than silently
  re-resolving (Core 8).
* GOOD -- ``--json`` on the subcommand itself puts exactly one JSON document
  on stdout and every human line on stderr, through the documented dual entry
  point a second host actually invokes.

The last structural test is the one that keeps this CLI thin: it fails if
resolution, catalog, or execution logic ever moves into ``cli.py``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import amplifier_recipe_runner as pkg
from amplifier_recipe_runner import execution
from amplifier_recipe_runner.api import RunRequest
from amplifier_recipe_runner.api import RunResult
from amplifier_recipe_runner.api import RunStatus
from amplifier_recipe_runner import cli as cli_module
from amplifier_recipe_runner.cli import EXIT_FAILURE
from amplifier_recipe_runner.cli import EXIT_LEGACY_RECIPE
from amplifier_recipe_runner.cli import EXIT_OK
from amplifier_recipe_runner.cli import EXIT_PREFLIGHT
from amplifier_recipe_runner.cli import EXIT_PROVENANCE_MISMATCH
from amplifier_recipe_runner.cli import EXIT_UNSUPPORTED
from amplifier_recipe_runner.cli import EXIT_USAGE
from amplifier_recipe_runner.cli import cli
from amplifier_recipe_runner.cli import main
from amplifier_recipe_runner.execution import AmbiguousCompletedStepError
from amplifier_recipe_runner.execution import UnknownCompletedStepError
from amplifier_recipe_runner.ports import HostServices
from amplifier_recipe_runner.resolver import LocalBundleResolver

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
TOOLKIT = FIXTURES / "toolkit"
LEGACY_RECIPE = FIXTURES / "legacy.yaml"
STRAY_MANIFEST = FIXTURES / "stray-manifest.yaml"

PACKAGE_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = PACKAGE_DIR.parent
REPO_ROOT = SRC_DIR.parent
CLI_SOURCE = PACKAGE_DIR / "cli.py"

V2_RECIPE = f"""
schema_version: 2
name: review-and-package
dependencies:
  - source: {TOOLKIT}
    kind: bundle
    required_agents: [toolkit:reviewer]
agents:
  packager: toolkit:packager
steps:
  - id: review
    agent: "toolkit:reviewer"
    prompt: "review it"
  - id: package
    agent: "packager"
    prompt: "package it"
"""

UNDECLARED_AGENT_RECIPE = f"""
schema_version: 2
name: reaches-outside-the-closure
dependencies:
  - source: {TOOLKIT}
    kind: bundle
steps:
  - id: review
    agent: "elsewhere:auditor"
    prompt: "audit it"
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def write_recipe(tmp_path: Path, body: str = V2_RECIPE, *, name: str = "recipe.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def combined(result: object) -> str:
    """Stdout plus stderr, across Click versions that separate them and those
    that do not."""
    text = getattr(result, "output", "") or ""
    try:
        err = result.stderr  # type: ignore[attr-defined]
    except (ValueError, AttributeError):
        err = ""
    if err and err not in text:
        text = f"{text}{err}"
    return text


def invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(cli, args, catch_exceptions=False)


# --------------------------------------------------------------------------
# GOOD -- a declared recipe validates and plans
# --------------------------------------------------------------------------


def test_validate_accepts_a_v2_recipe(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "validate", "--offline", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    assert "status: ok" in result.output
    assert "schema_version: 2" in result.output


def test_validate_json_reports_a_machine_readable_report(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "--json", "validate", "--offline", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["legacy"] is False
    assert payload["schema_version"] == 2
    assert payload["errors"] == []


def test_plan_prints_the_closure_with_per_agent_provenance(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "plan", "--offline", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    assert "schema_version: 2" in result.output
    assert "recipe_digest: sha256:" in result.output
    # Every agent names the dependency that supplied it (manifest Core 7).
    assert f"toolkit:reviewer <- {TOOLKIT}" in result.output
    assert "packager" in result.output
    assert "steps: review, package" in result.output
    assert "lock_mode: locked" in result.output


def test_plan_json_is_the_libraries_run_manifest_shape(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "--json", "plan", "--offline", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    payload = json.loads(result.output)
    assert set(payload) >= {"run_id", "recipe_digest", "schema_version", "dependencies", "agents", "step_ids"}
    assert payload["schema_version"] == 2
    assert payload["step_ids"] == ["review", "package"]
    assert [dep["uri"] for dep in payload["dependencies"]] == [str(TOOLKIT)]
    assert payload["agents"]["toolkit:reviewer"]["supplied_by"] == str(TOOLKIT)


def test_cli_and_library_agree_on_the_resolved_graph(runner: CliRunner, tmp_path: Path) -> None:
    """lib Core 1: the CLI is an adapter, so its plan IS the library's plan."""
    import asyncio

    from amplifier_recipe_runner import RunRequest
    from amplifier_recipe_runner import plan as library_plan
    from amplifier_recipe_runner.resolver import LocalBundleResolver

    recipe = write_recipe(tmp_path)
    direct = asyncio.run(library_plan(RunRequest(recipe=recipe), resolver=LocalBundleResolver()))

    result = invoke(runner, ["--workspace", str(tmp_path), "--json", "plan", "--offline", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    payload = json.loads(result.output)
    assert payload["recipe_digest"] == direct.recipe_digest
    assert payload["step_ids"] == list(direct.step_ids)
    assert sorted(payload["agents"]) == sorted(direct.agents)


# --------------------------------------------------------------------------
# GOOD -- machine-readable plan output, on the subcommand itself
# --------------------------------------------------------------------------


def test_plan_json_flag_on_the_subcommand_is_the_run_manifest_shape(runner: CliRunner, tmp_path: Path) -> None:
    """``plan --json`` -- not just ``--json plan`` -- carries the resolved graph.

    A second host planning the same recipe compares *this* document, so the
    keys asserted here are the identity itself: what was declared, what it
    resolved to, and which dependency supplied each agent (manifest Core 7).
    """
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "plan", "--json", "--offline", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    payload = json.loads(result.output)
    assert set(payload) >= {
        "agents",
        "dependencies",
        "partials",
        "policy",
        "recipe_digest",
        "schema_version",
        "step_ids",
    }
    assert payload["step_ids"] == ["review", "package"]
    assert [dep["uri"] for dep in payload["dependencies"]] == [str(TOOLKIT)]
    assert payload["agents"]["toolkit:reviewer"]["supplied_by"] == str(TOOLKIT)


def test_the_subcommands_own_text_flag_overrides_the_group_json(runner: CliRunner, tmp_path: Path) -> None:
    """Both spellings exist, so precedence has to be stated, not guessed."""
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "--json", "plan", "--text", "--offline", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    assert "schema_version: 2" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


# --------------------------------------------------------------------------
# BAD -- a legacy recipe is refused (manifest Core 10)
# --------------------------------------------------------------------------


def test_legacy_recipe_is_rejected_with_a_remedy_naming_both_options(runner: CliRunner, tmp_path: Path) -> None:
    result = invoke(runner, ["--workspace", str(tmp_path), "validate", "--offline", str(LEGACY_RECIPE)])

    text = combined(result)
    assert result.exit_code == EXIT_LEGACY_RECIPE, text
    assert "legacy" in text.lower()
    # The remedy names BOTH routes: declare dependencies, or the tool adapter.
    assert "schema_version: 2" in text
    assert "dependencies" in text
    assert "Amplifier tool adapter" in text


def test_legacy_exit_code_is_distinct_from_every_other_outcome() -> None:
    codes = {EXIT_OK, EXIT_FAILURE, EXIT_USAGE, EXIT_PREFLIGHT, EXIT_PROVENANCE_MISMATCH, EXIT_UNSUPPORTED}
    assert EXIT_LEGACY_RECIPE not in codes


def test_run_refuses_a_legacy_recipe_too(runner: CliRunner, tmp_path: Path) -> None:
    result = invoke(
        runner,
        ["--workspace", str(tmp_path), "run", "--offline", "--dry-run", "--unlocked", str(LEGACY_RECIPE)],
    )

    text = combined(result)
    assert result.exit_code == EXIT_LEGACY_RECIPE, text
    assert "Amplifier tool adapter" in text


def test_plan_refuses_a_legacy_recipe_too(runner: CliRunner, tmp_path: Path) -> None:
    result = invoke(runner, ["--workspace", str(tmp_path), "plan", "--offline", str(LEGACY_RECIPE)])

    assert result.exit_code == EXIT_LEGACY_RECIPE, combined(result)


def test_manifest_keys_without_a_version_are_a_parse_error_not_legacy(runner: CliRunner, tmp_path: Path) -> None:
    """A forgotten `schema_version` must not be silently downgraded to legacy."""
    result = invoke(runner, ["--workspace", str(tmp_path), "validate", "--offline", str(STRAY_MANIFEST)])

    text = combined(result)
    assert result.exit_code == EXIT_PREFLIGHT, text
    assert result.exit_code != EXIT_LEGACY_RECIPE
    assert "schema_version" in text


# --------------------------------------------------------------------------
# BAD -- preflight errors are readable, not tracebacks
# --------------------------------------------------------------------------


def test_undeclared_agent_is_a_readable_message_with_a_remedy(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path, UNDECLARED_AGENT_RECIPE, name="undeclared.yaml")

    result = invoke(runner, ["--workspace", str(tmp_path), "plan", "--offline", str(recipe)])

    text = combined(result)
    assert result.exit_code == EXIT_PREFLIGHT, text
    assert "Traceback" not in text
    assert "error:" in text
    assert "remedy:" in text
    assert "elsewhere:auditor" in text


def test_validate_reports_the_undeclared_agent_as_a_report_error(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path, UNDECLARED_AGENT_RECIPE, name="undeclared.yaml")

    result = invoke(runner, ["--workspace", str(tmp_path), "--json", "validate", "--offline", str(recipe)])

    assert result.exit_code == EXIT_PREFLIGHT, combined(result)
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "UndeclaredAgentError"
    assert payload["errors"][0]["remedy"]


def test_a_missing_recipe_is_a_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    result = invoke(runner, ["validate", "--offline", str(tmp_path / "nope.yaml")])

    assert result.exit_code == EXIT_USAGE
    assert "Traceback" not in combined(result)


# --------------------------------------------------------------------------
# Lock modes (manifest Core 8)
# --------------------------------------------------------------------------


def test_lock_writes_the_sidecar_lockfile(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "lock", "--offline", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    lock_path = tmp_path / "recipe.lock.yaml"
    assert lock_path.is_file()
    document = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    assert document["lock_version"] == 1
    assert [entry["declared_source"] for entry in document["dependencies"]] == [str(TOOLKIT)]


def test_locked_run_without_a_lockfile_refuses_before_anything_runs(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "run", "--offline", "--locked", "--dry-run", str(recipe)])

    text = combined(result)
    assert result.exit_code == EXIT_PREFLIGHT, text
    assert "lock" in text.lower()
    assert not (tmp_path / ".recipe-runner").exists()


def test_dry_run_after_lock_passes_preflight_and_records_provenance(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    assert invoke(runner, ["--workspace", str(tmp_path), "lock", "--offline", str(recipe)]).exit_code == EXIT_OK

    result = invoke(
        runner,
        [
            "--workspace",
            str(tmp_path),
            "run",
            "--offline",
            "--locked",
            "--dry-run",
            "--run-id",
            "run-fixed",
            str(recipe),
        ],
    )

    assert result.exit_code == EXIT_OK, combined(result)
    assert "dry-run: preflight ok; 2 step(s) would run" in result.output
    manifest = tmp_path / ".recipe-runner" / "runs" / "run-fixed" / "run-manifest.json"
    assert manifest.is_file()
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    assert recorded["run_id"] == "run-fixed"
    assert recorded["step_ids"] == ["review", "package"]


def test_unlocked_mode_warns_rather_than_pretending_to_be_pinned(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "run", "--offline", "--unlocked", "--dry-run", str(recipe)])

    text = combined(result)
    assert result.exit_code == EXIT_OK, text
    assert "unlocked" in text.lower()
    assert not (tmp_path / "recipe.lock.yaml").exists()


def test_update_lock_rewrites_the_lock_on_run(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(
        runner,
        ["--workspace", str(tmp_path), "run", "--offline", "--update-lock", "--dry-run", str(recipe)],
    )

    assert result.exit_code == EXIT_OK, combined(result)
    assert (tmp_path / "recipe.lock.yaml").is_file()


# --------------------------------------------------------------------------
# Three-tier configuration: flag > config > default
# --------------------------------------------------------------------------


def test_config_file_supplies_defaults_when_no_flag_is_passed(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    (tmp_path / "recipe-runner.yaml").write_text("offline: true\ntrust: none\n", encoding="utf-8")

    result = invoke(runner, ["--workspace", str(tmp_path), "plan", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    assert "trust_policy: (none)" in result.output


def test_a_flag_overrides_the_config_file(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    (tmp_path / "recipe-runner.yaml").write_text("offline: true\ntrust: none\n", encoding="utf-8")

    result = invoke(runner, ["--workspace", str(tmp_path), "plan", "--trust", "ci", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    assert "trust_policy: ci" in result.output


def test_the_built_in_default_applies_when_neither_flag_nor_config_speaks(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--workspace", str(tmp_path), "plan", "--offline", str(recipe)])

    assert result.exit_code == EXIT_OK, combined(result)
    assert "trust_policy: interactive" in result.output
    assert "lock_mode: locked" in result.output


def test_an_unknown_config_key_is_an_error_not_silently_ignored(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    (tmp_path / "recipe-runner.yaml").write_text("offline: true\nagent_map: mine\n", encoding="utf-8")

    result = invoke(runner, ["--workspace", str(tmp_path), "plan", str(recipe)])

    text = combined(result)
    assert result.exit_code == EXIT_USAGE, text
    assert "agent_map" in text


def test_a_named_config_that_does_not_exist_is_a_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = invoke(runner, ["--config", str(tmp_path / "absent.yaml"), "plan", "--offline", str(recipe)])

    assert result.exit_code == EXIT_USAGE
    assert "Traceback" not in combined(result)


# --------------------------------------------------------------------------
# Resume: provenance is checked, drift refuses (manifest Core 8)
# --------------------------------------------------------------------------


def _recorded_run(runner: CliRunner, tmp_path: Path, recipe: Path, run_id: str = "run-fixed") -> None:
    result = invoke(
        runner,
        [
            "--workspace",
            str(tmp_path),
            "run",
            "--offline",
            "--unlocked",
            "--dry-run",
            "--run-id",
            run_id,
            str(recipe),
        ],
    )
    assert result.exit_code == EXIT_OK, combined(result)


def _set_outcome(tmp_path: Path, status: str, completed: list[str], run_id: str = "run-fixed") -> None:
    """Rewrite what the run recorded, standing in for an executed run."""
    path = tmp_path / ".recipe-runner" / "runs" / run_id / "run.json"
    context = json.loads(path.read_text(encoding="utf-8"))
    context["status"] = status
    context["completed_steps"] = completed
    path.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_resume_verifies_recorded_provenance(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)

    result = invoke(runner, ["--workspace", str(tmp_path), "resume", "--offline", "--dry-run", "run-fixed"])

    text = combined(result)
    assert result.exit_code == EXIT_OK, text
    assert "provenance: verified" in text
    assert "Traceback" not in text


def test_resume_of_a_completed_run_has_nothing_to_do(runner: CliRunner, tmp_path: Path) -> None:
    """A finished run is not a failure to resume -- it is a finished run."""
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)
    _set_outcome(tmp_path, "succeeded", ["review", "package"])

    result = invoke(runner, ["--workspace", str(tmp_path), "resume", "--offline", "run-fixed"])

    text = combined(result)
    assert result.exit_code == EXIT_OK, text
    assert "nothing to resume" in text
    assert "completed_steps: review, package" in text


def test_resume_with_no_completed_steps_runs_from_the_start(runner: CliRunner, tmp_path: Path) -> None:
    """Nothing ran, so resuming IS running -- and it is one library call."""
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)
    _set_outcome(tmp_path, "failed", [])

    result = invoke(runner, ["--workspace", str(tmp_path), "resume", "--offline", "--dry-run", "run-fixed"])

    text = combined(result)
    assert result.exit_code == EXIT_OK, text
    assert "would resume by running 2 step(s) from the start" in text


def test_resume_of_a_partially_completed_run_hands_the_completed_steps_to_the_library(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The formerly-blocked case: continue mid-run instead of refusing.

    The library is stubbed here because executing for real needs Foundation,
    which this suite deliberately does not install. What is asserted is exactly
    what the CLI owns: which entry point it calls, what it passes it, and what
    it does with the answer. That the library then *skips* those steps is
    asserted against the library itself, further down.
    """
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)
    _set_outcome(tmp_path, "failed", ["review"])

    seen: dict[str, object] = {}

    async def fake_resume(request, *, completed_steps=(), resolver=None, **kwargs):
        seen["run_id"] = request.run_id
        seen["completed_steps"] = tuple(completed_steps)
        return RunResult(
            run_id=str(request.run_id),
            status=RunStatus.SUCCEEDED,
            completed_steps=("review", "package"),
        )

    monkeypatch.setattr(cli_module, "resume_recipe", fake_resume)

    result = invoke(runner, ["--workspace", str(tmp_path), "resume", "--offline", "run-fixed"])

    text = combined(result)
    assert result.exit_code == EXIT_OK, text
    assert result.exit_code != EXIT_UNSUPPORTED
    assert "no `resume` entry point" not in text
    # The recorded completed steps reach the library verbatim -- not re-derived
    # here, not silently dropped.
    assert seen == {"run_id": "run-fixed", "completed_steps": ("review",)}
    assert "completed_steps: review, package" in text
    # And the outcome is recorded, so a second resume has nothing left to do.
    recorded = json.loads((tmp_path / ".recipe-runner" / "runs" / "run-fixed" / "run.json").read_text("utf-8"))
    assert recorded["status"] == "succeeded"
    assert recorded["completed_steps"] == ["review", "package"]


def test_resume_dry_run_mid_run_names_what_it_would_skip(runner: CliRunner, tmp_path: Path) -> None:
    """A preview that says which steps are already done, and runs nothing."""
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)
    _set_outcome(tmp_path, "failed", ["review"])

    result = invoke(runner, ["--workspace", str(tmp_path), "resume", "--offline", "--dry-run", "run-fixed"])

    text = combined(result)
    assert result.exit_code == EXIT_OK, text
    assert "would resume by running 1 remaining step(s), skipping 1 already completed" in text


def test_no_code_path_still_refuses_a_mid_run_resume() -> None:
    """Acceptance: the ``UnsupportedCapabilityError`` branch is deleted.

    Structural, not message-based: the branch could otherwise be reintroduced
    under a different wording and no test would notice. The class and its exit
    code survive on purpose (a future missing capability must not become a
    generic exit 1), so what is asserted is that nothing constructs it.
    """
    tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))
    constructed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "UnsupportedCapabilityError"
    ]
    assert constructed == [], "mid-run resume is wired to the library; nothing should refuse it again"


def test_resume_refuses_a_recipe_edited_after_the_run(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)

    recipe.write_text(recipe.read_text(encoding="utf-8").replace("review it", "review it twice"), encoding="utf-8")
    result = invoke(runner, ["--workspace", str(tmp_path), "resume", "--offline", "run-fixed"])

    text = combined(result)
    assert result.exit_code == EXIT_PROVENANCE_MISMATCH, text
    assert "provenance" in text.lower()
    assert "remedy:" in text


def test_resume_of_an_unknown_run_is_a_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    result = invoke(runner, ["--workspace", str(tmp_path), "resume", "--offline", "never-ran"])

    text = combined(result)
    assert result.exit_code == EXIT_USAGE, text
    assert "never-ran" in text


# --------------------------------------------------------------------------
# The library entry point the CLI resumes through
#
# These call ``amplifier_recipe_runner.resume`` directly, with an injected
# spawn backend. They sit beside the CLI tests deliberately: the CLI cannot
# prove the skip in-process -- executing for real needs Foundation, which this
# suite does not install -- so the behaviour the CLI depends on is proved here,
# against the same fixture recipe, rather than asserted by assumption.
# --------------------------------------------------------------------------


class Providers:
    """Provider port double. Never resolved: an injected backend needs none."""

    def roles(self) -> list[str]:
        return ["general"]

    def resolve(self, role: str) -> object:  # pragma: no cover - never reached
        return object()


class RecordingBackend:
    """Spawn backend double. Records every step that actually executed."""

    def __init__(self) -> None:
        self.canonicals: list[str] = []

    async def spawn(self, request: object) -> str:
        canonical = str(getattr(request, "canonical"))
        self.canonicals.append(canonical)
        return f"done:{canonical}"


def _library_resume(recipe: Path, workspace: Path, backend: RecordingBackend, completed: tuple[str, ...]):
    request = RunRequest(
        recipe=recipe,
        services=HostServices(provider_access=Providers(), workspace=workspace),  # type: ignore[arg-type]
        run_id="run-fixed",
    )
    return asyncio.run(
        pkg.resume(
            request,
            completed_steps=completed,
            resolver=LocalBundleResolver(),
            spawn_backend=backend,
        )
    )


def test_library_exports_resume_as_an_async_entry_point() -> None:
    """lib Core 2 names validate/plan/run/resume; the CLI drives this one."""
    assert pkg.resume is execution.resume
    assert "resume" in pkg.__all__
    assert inspect.iscoroutinefunction(pkg.resume)


def test_library_resume_runs_only_the_uncompleted_steps(tmp_path: Path) -> None:
    """The acceptance case: continue mid-run, re-running nothing."""
    recipe = write_recipe(tmp_path)
    backend = RecordingBackend()

    result = _library_resume(recipe, tmp_path, backend, ("review",))

    assert result.status is RunStatus.SUCCEEDED
    assert backend.canonicals == ["toolkit:packager"], "a completed step was re-run"
    # The result describes the run, not just this attempt.
    assert result.completed_steps == ("review", "package")
    # A skipped step's output is absent rather than invented (lib Core 8).
    assert "review" not in result.outputs
    assert result.outputs["package"] == "done:toolkit:packager"


def test_library_resume_with_nothing_completed_is_a_full_run(tmp_path: Path) -> None:
    """Resuming a run that never started is running it -- same path, no skips."""
    recipe = write_recipe(tmp_path)
    backend = RecordingBackend()

    result = _library_resume(recipe, tmp_path, backend, ())

    assert result.status is RunStatus.SUCCEEDED
    assert backend.canonicals == ["toolkit:reviewer", "toolkit:packager"]
    assert result.completed_steps == ("review", "package")


def test_library_resume_skips_by_id_not_by_position(tmp_path: Path) -> None:
    """A run that stopped with a gap must not redo the step past the gap."""
    recipe = write_recipe(tmp_path)
    backend = RecordingBackend()

    result = _library_resume(recipe, tmp_path, backend, ("package",))

    assert result.status is RunStatus.SUCCEEDED
    assert backend.canonicals == ["toolkit:reviewer"]
    assert result.completed_steps == ("package", "review")


def test_library_resume_refuses_a_step_id_the_recipe_does_not_declare(tmp_path: Path) -> None:
    """Neither silent reading is honest, so it refuses before anything runs."""
    recipe = write_recipe(tmp_path)
    backend = RecordingBackend()

    result = _library_resume(recipe, tmp_path, backend, ("review", "reviww"))

    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, UnknownCompletedStepError)
    assert "'reviww'" in str(result.error)
    assert getattr(result.error, "remedy", None)
    assert backend.canonicals == [], "nothing may run once the recorded step list is unusable"


def test_library_resume_refuses_a_step_id_the_recipe_declares_twice(tmp_path: Path) -> None:
    """Nothing enforces unique step ids, so resuming must not guess.

    ``run`` merely overwrites an output when two steps share an id; skipping
    both would drop real, unfinished work with no trace -- the silent skip lib
    Core 8 forbids.
    """
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        name: two-steps-one-name
        dependencies:
          - source: {TOOLKIT}
            kind: bundle
        steps:
          - id: review
            agent: "toolkit:reviewer"
            prompt: "review it"
          - id: review
            agent: "toolkit:packager"
            prompt: "review it again"
        """,
    )
    backend = RecordingBackend()

    result = _library_resume(recipe, tmp_path, backend, ("review",))

    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, AmbiguousCompletedStepError)
    assert "more than once" in str(result.error)
    assert backend.canonicals == [], "no step may run once a recorded id is ambiguous"


def test_a_refused_resume_still_reports_the_steps_it_was_given(tmp_path: Path) -> None:
    """Failing to resume must not un-complete a step that already ran.

    The CLI records ``completed_steps`` off the result verbatim, so a refusal
    that reported an empty list would erase the very history the next resume
    needs.
    """
    recipe = write_recipe(tmp_path)
    backend = RecordingBackend()

    result = _library_resume(recipe, tmp_path, backend, ("review", "reviww"))

    assert result.status is RunStatus.FAILED
    assert result.completed_steps == ("review", "reviww")


def test_a_refused_resume_leaves_the_recorded_run_resumable(runner: CliRunner, tmp_path: Path) -> None:
    """The same guarantee, observed where it actually matters: on disk."""
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)
    _set_outcome(tmp_path, "failed", ["review"])
    run_json = tmp_path / ".recipe-runner" / "runs" / "run-fixed" / "run.json"

    async def refusing_resume(request, *, completed_steps=(), resolver=None, **kwargs):
        return RunResult(
            run_id=str(request.run_id),
            status=RunStatus.FAILED,
            completed_steps=tuple(completed_steps),
            error=RuntimeError("the remaining step failed"),
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli_module, "resume_recipe", refusing_resume)
        result = invoke(runner, ["--workspace", str(tmp_path), "resume", "--offline", "run-fixed"])

    assert result.exit_code == EXIT_FAILURE, combined(result)
    assert json.loads(run_json.read_text("utf-8"))["completed_steps"] == ["review"]


def test_library_run_is_unaffected_by_the_resume_checks(tmp_path: Path) -> None:
    """``run`` passes no completed steps, so neither refusal can fire for it."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        name: two-steps-one-name
        dependencies:
          - source: {TOOLKIT}
            kind: bundle
        steps:
          - id: review
            agent: "toolkit:reviewer"
            prompt: "review it"
          - id: review
            agent: "toolkit:packager"
            prompt: "review it again"
        """,
    )
    backend = RecordingBackend()
    request = RunRequest(
        recipe=recipe,
        services=HostServices(provider_access=Providers(), workspace=tmp_path),  # type: ignore[arg-type]
        run_id="run-fixed",
    )

    result = asyncio.run(pkg.run(request, resolver=LocalBundleResolver(), spawn_backend=backend))

    assert result.status is RunStatus.SUCCEEDED
    assert backend.canonicals == ["toolkit:reviewer", "toolkit:packager"]


def test_library_resume_reports_every_skip_on_the_event_sink(tmp_path: Path) -> None:
    """A skipped step is a claim about earlier work; it is made visibly."""
    recipe = write_recipe(tmp_path)
    events: list[str] = []

    class Sink:
        def emit(self, event: object) -> None:
            events.append(f"{getattr(event, 'kind')}:{(getattr(event, 'data', {}) or {}).get('step_id')}")

    request = RunRequest(
        recipe=recipe,
        services=HostServices(  # type: ignore[arg-type]
            provider_access=Providers(),
            workspace=tmp_path,
            event_sink=Sink(),
        ),
        run_id="run-fixed",
    )
    asyncio.run(
        pkg.resume(
            request,
            completed_steps=("review",),
            resolver=LocalBundleResolver(),
            spawn_backend=RecordingBackend(),
        )
    )

    assert "step:skipped:review" in events
    assert "step:start:package" in events
    assert "step:start:review" not in events


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def test_help_lists_every_subcommand(runner: CliRunner) -> None:
    result = invoke(runner, ["--help"])

    assert result.exit_code == EXIT_OK
    for command in ("validate", "plan", "run", "lock", "resume"):
        assert command in result.output


def test_console_script_declaration_matches_the_entry_point() -> None:
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.is_file():  # pragma: no cover - installed without sources
        pytest.skip("pyproject.toml is not alongside the package")

    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["recipe-runner"] == "amplifier_recipe_runner.cli:main"
    assert callable(main)
    assert any(dep.startswith("click") for dep in data["project"]["dependencies"]), (
        "click must be a runtime dependency: the console script imports it unconditionally"
    )


def test_module_entry_point_is_the_same_command() -> None:
    result = _module(["--version"], cwd=REPO_ROOT)

    assert result.returncode == EXIT_OK, result.stderr
    assert "recipe-runner" in result.stdout


def _module(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """The documented dual entry point, in a real process with real streams.

    ``CliRunner`` may merge stdout and stderr, so a claim about which stream a
    line landed on can only be proved out of process.
    """
    return subprocess.run(
        [sys.executable, "-m", "amplifier_recipe_runner", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={"PYTHONPATH": str(SRC_DIR), "PATH": "/usr/bin:/bin"},
        check=False,
    )


def test_module_entry_point_plan_json_puts_only_json_on_stdout(tmp_path: Path) -> None:
    """The exact invocation a second host uses to compare resolved graphs.

    ``python -m amplifier_recipe_runner`` is the documented dual entry point;
    ``-m amplifier_recipe_runner.cli`` merely imports the module (it declares
    no ``__main__`` guard) and would exit 0 with empty stdout.
    """
    recipe = write_recipe(tmp_path)

    result = _module(["plan", "--json", "--offline", "--trust", "none", str(recipe)], cwd=tmp_path)

    assert result.returncode == EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["step_ids"] == ["review", "package"]
    assert [dep["uri"] for dep in payload["dependencies"]] == [str(TOOLKIT)]
    assert payload["agents"]["toolkit:reviewer"]["supplied_by"] == str(TOOLKIT)
    assert payload["policy"]["trust_policy"] is None


def test_run_json_is_one_document_with_every_human_line_on_stderr(tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)

    result = _module(
        [
            "--workspace",
            str(tmp_path),
            "run",
            "--json",
            "--offline",
            "--unlocked",
            "--dry-run",
            "--run-id",
            "run-json",
            str(recipe),
        ],
        cwd=tmp_path,
    )

    assert result.returncode == EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["run_id"] == "run-json"
    assert payload["dry_run"] is True
    # Nothing executed, so the outcome fields say exactly that.
    assert payload["status"] is None
    assert payload["completed_steps"] == []
    # The same resolved-graph shape `plan --json` reports.
    assert payload["plan"]["step_ids"] == ["review", "package"]
    assert [dep["uri"] for dep in payload["plan"]["dependencies"]] == [str(TOOLKIT)]

    for line in ("run_id:", "provenance:", "dry-run:", "lock:"):
        assert line not in result.stdout, f"{line!r} leaked onto stdout under --json"
        assert line in result.stderr, f"{line!r} vanished instead of moving to stderr"


# --------------------------------------------------------------------------
# Resume exit codes, out of process
#
# ``CliRunner`` never reaches ``main()`` or a real process exit status, so the
# exit-code contract is only actually proved through the dual entry point.
# --------------------------------------------------------------------------


def test_module_entry_point_mid_run_resume_no_longer_exits_unsupported(runner: CliRunner, tmp_path: Path) -> None:
    """The regression this work item closes, in a real process.

    ``--dry-run`` because a real mid-run continuation needs Foundation. The old
    refusal preceded the dry-run check, so this invocation exited 6 before and
    exits 0 now -- which is exactly the branch deletion, observed from outside.
    """
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)
    _set_outcome(tmp_path, "failed", ["review"])

    result = _module(["--workspace", str(tmp_path), "resume", "--offline", "--dry-run", "run-fixed"], cwd=tmp_path)

    assert result.returncode == EXIT_OK, result.stderr
    assert result.returncode != EXIT_UNSUPPORTED
    assert "1 remaining step(s), skipping 1 already completed" in result.stdout


def test_module_entry_point_completed_run_resume_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)
    _set_outcome(tmp_path, "succeeded", ["review", "package"])

    result = _module(["--workspace", str(tmp_path), "resume", "--offline", "run-fixed"], cwd=tmp_path)

    assert result.returncode == EXIT_OK, result.stderr
    assert "nothing to resume" in result.stdout


def test_module_entry_point_resume_of_an_edited_recipe_exits_provenance_mismatch(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Core 8, end to end: the check precedes execution, and it is exit 5."""
    recipe = write_recipe(tmp_path)
    _recorded_run(runner, tmp_path, recipe)
    _set_outcome(tmp_path, "failed", ["review"])
    recipe.write_text(recipe.read_text(encoding="utf-8").replace("review it", "review it twice"), encoding="utf-8")

    result = _module(["--workspace", str(tmp_path), "resume", "--offline", "run-fixed"], cwd=tmp_path)

    assert result.returncode == EXIT_PROVENANCE_MISMATCH, result.stdout + result.stderr
    assert "remedy:" in result.stderr
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------
# The CLI stays thin (lib Core 1)
# --------------------------------------------------------------------------


def test_cli_carries_no_execution_or_resolution_logic() -> None:
    """A second execution home is exactly what lib Core 1 forbids.

    This is a structural assertion, not a style check: every name below is a
    library internal whose appearance here would mean the CLI had started
    resolving, cataloguing, or executing on its own.
    """
    source = CLI_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # The library owns every coroutine. The CLI only drives them.
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)], (
        "cli.py defines a coroutine; execution belongs to the library"
    )

    forbidden = (
        "AgentCatalog",
        "PlanCatalog",
        "ResolvedBundle",
        "SpawnBackend",
        "create_execution_session",
        "parse_manifest",
        "plan_dependencies",
        "from .planner",
    )
    for name in forbidden:
        assert name not in source, f"cli.py references {name!r}; that logic belongs in the library"


def test_cli_imports_only_the_librarys_own_modules() -> None:
    tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))
    external = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            external.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            external.add(node.module.split(".")[0])

    # Standard library plus exactly two third-party packages.
    assert external <= {
        "__future__",
        "asyncio",
        "click",
        "collections",
        "dataclasses",
        "json",
        "pathlib",
        "typing",
        "uuid",
        "yaml",
    }, external


def test_exit_codes_are_all_distinct() -> None:
    codes = [
        EXIT_OK,
        EXIT_FAILURE,
        EXIT_USAGE,
        EXIT_PREFLIGHT,
        EXIT_LEGACY_RECIPE,
        EXIT_PROVENANCE_MISMATCH,
        EXIT_UNSUPPORTED,
    ]
    assert len(set(codes)) == len(codes)


def test_exit_code_mapping_prefers_the_most_specific_error() -> None:
    from amplifier_recipe_runner.errors import LegacyRecipeError
    from amplifier_recipe_runner.errors import ProvenanceMismatchError
    from amplifier_recipe_runner.errors import UndeclaredAgentError

    assert cli_module.exit_code_for(LegacyRecipeError("r.yaml")) == EXIT_LEGACY_RECIPE
    assert cli_module.exit_code_for(ProvenanceMismatchError("src")) == EXIT_PROVENANCE_MISMATCH
    assert cli_module.exit_code_for(UndeclaredAgentError("a:b")) == EXIT_PREFLIGHT
    assert cli_module.exit_code_for(RuntimeError("boom")) == EXIT_FAILURE
