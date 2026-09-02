#!/usr/bin/env python3
"""Legacy-compat harness for tool-recipes.

Records a golden baseline of TODAY's tool-recipes behavior for a set of
representative *legacy* recipes (recipes with no `schema_version` / no
`dependencies` block, resolving agents from the caller's agent map), and
re-asserts that baseline later.

This is the conformance fixture for recipe-dependency-manifest.v1's
"legacy identity pair" clause:

    A representative legacy recipe (agents present in caller) produces an
    identical outcome and identical agent provenance before and after the
    runner lands.

Usage:
    python3 harness.py --list
    python3 harness.py --record [--case NAME]
    python3 harness.py --assert [--case NAME]
    python3 harness.py --record --case bash-step-example --show

See README.md for the normalization rules and the fixture contract.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import getpass
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
BASELINE_DIR = HERE / "baselines"
CASES_FILE = HERE / "cases.yaml"

# Import the engine under test straight from the repo (it is not pip-installed
# in every environment). This is the same package the `recipes` tool mounts.
sys.path.insert(0, str(REPO_ROOT / "modules" / "tool-recipes"))

import yaml  # noqa: E402

from amplifier_module_tool_recipes import RecipesTool  # noqa: E402
from amplifier_module_tool_recipes.executor import RecipeExecutor  # noqa: E402
from amplifier_module_tool_recipes.session import SessionManager  # noqa: E402

HARNESS_VERSION = 1

# Maximum approve+resume round trips for a staged recipe before we give up.
MAX_APPROVAL_ROUNDS = 20


# ---------------------------------------------------------------------------
# Caller fixture: a stand-in for the calling session (the "coordinator")
# ---------------------------------------------------------------------------


class _CallerSession:
    """Opaque parent-session handle handed to spawn. Never introspected."""

    def __repr__(self) -> str:  # pragma: no cover - only for recorded output
        return "<caller-session>"


class HookRecorder:
    """Records the structured hook events the executor emits."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, name: str, data: dict[str, Any]) -> None:
        self.events.append({"event": name, "data": data})


class DisplayRecorder:
    """Records the human-facing progress messages the executor shows."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def show_message(self, message: str, level: str = "info", source: str = "") -> None:
        self.messages.append({"level": level, "source": source, "message": message})


class CallerFixture:
    """A caller configuration that HAS the agents the legacy recipe references.

    Legacy resolution semantics: a step's `agent:` string is handed verbatim to
    the caller's `session.spawn` capability and resolved against the caller's
    agent map. This fixture makes that map explicit and declared per-case, so
    the recorded provenance is a statement about resolution, not about a
    particular developer's installed bundles.

    Deliberately provider-free: `get("providers")` returns None, so
    `resolve_model_pattern` leaves model globs (e.g. `claude-sonnet-*`)
    unresolved. A live provider catalog is not reproducible and could never be
    a byte-identical baseline; the glob itself is the provenance we assert on.
    """

    def __init__(self, agents: dict[str, Any], workspace: Path, spawn_fn: Any):
        self._agents = dict(agents)
        self.config: dict[str, Any] = {"agents": self._agents}
        self.session = _CallerSession()
        self.mount_points: dict[str, dict[str, Any]] = {"tools": {}}
        self.hooks = HookRecorder()
        self.display_system = DisplayRecorder()
        self._workspace = workspace
        self._spawn_fn = spawn_fn
        self._capabilities: dict[str, Any] = {}

    @property
    def available_agents(self) -> list[str]:
        return sorted(self._agents)

    def get_capability(self, name: str) -> Any:
        if name == "session.spawn":
            return self._spawn_fn
        if name == "session.working_dir":
            return str(self._workspace)
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value

    def get(self, key: str) -> Any:
        # No providers, no display registry, nothing else. See docstring.
        return None


class SpawnRecorder:
    """Stands in for the caller's `session.spawn` capability.

    Records exactly what the engine asked the caller to spawn (the agent
    provenance) and replays a per-step scripted response declared in
    cases.yaml. The engine's own behavior -- substitution, conditions, JSON
    handling, checkpointing, approval gates -- runs for real against it.
    """

    def __init__(self, responses: dict[str, Any], agents: dict[str, Any]):
        self._responses = responses
        self._agents = agents
        self.spawns: list[dict[str, Any]] = []

    def _response_for(self, step_id: str, agent_name: str) -> str:
        if step_id in self._responses:
            template = self._responses[step_id]
        else:
            template = self._responses.get(
                "_default", "STUB AGENT OUTPUT (step={step}, agent={agent})"
            )
        # Plain replacement, never str.format: scripted responses legitimately
        # contain JSON braces, and .format() would choke on them.
        return str(template).replace("{step}", step_id).replace("{agent}", agent_name)

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        agent_name = kwargs.get("agent_name")
        metadata = kwargs.get("session_metadata") or {}
        step_id = metadata.get("recipe_step", "")
        prefs = kwargs.get("provider_preferences")
        self.spawns.append(
            {
                "index": len(self.spawns),
                "recipe_step": step_id,
                "recipe_name": metadata.get("recipe_name"),
                "agent_name": agent_name,
                "agent_present_in_caller": agent_name in self._agents,
                "caller_agent_config": self._agents.get(agent_name),
                "provider_preferences": _prefs_to_json(prefs),
                "orchestrator_config": kwargs.get("orchestrator_config"),
                "use_subprocess": kwargs.get("use_subprocess"),
                "session_metadata": metadata,
                "instruction": kwargs.get("instruction"),
            }
        )
        # Shape matches what the executor's _process_step_result unwraps:
        # a dict carrying an "output" key.
        return {"output": self._response_for(str(step_id), str(agent_name))}


def _prefs_to_json(prefs: Any) -> Any:
    if prefs is None:
        return None
    out = []
    for p in prefs:
        if isinstance(p, dict):
            out.append(p)
        else:
            out.append(
                {
                    "provider": getattr(p, "provider", None),
                    "model": getattr(p, "model", None),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_RE_SESSION_ID = re.compile(r"[0-9a-f]{16}-\d{8}-\d{6}_recipe")
_RE_ISO_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_RE_UNIX_DATE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\w{3}\s+\d{1,2}\s+"
    r"\d{1,2}:\d{2}:\d{2}(?:\s+[AP]M)?\s+[A-Za-z]{2,5}\s+\d{4}\b"
)
_RE_BARE_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_RE_CLOCK = re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b")
_RE_TMPDIR = re.compile(r"/tmp/[A-Za-z0-9_.\-]+")


class Normalizer:
    """Replaces run-specific values with stable placeholders.

    Every rule here is a documented, mechanical substitution -- see README.md.
    Order matters: longer / more specific paths are replaced before shorter
    ones so `<WORKSPACE>` never degrades into `<HOME>`.
    """

    def __init__(self, workspace: Path):
        self._pairs: list[tuple[str, str]] = []
        # Most specific path first.
        for value, token in (
            (str(workspace.resolve()), "<WORKSPACE>"),
            (str(REPO_ROOT), "<REPO>"),
            (str(Path.home()), "<HOME>"),
        ):
            if value:
                self._pairs.append((value, token))
        self._pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        self._host = socket.gethostname()
        try:
            self._user = getpass.getuser()
        except Exception:  # pragma: no cover - unusual environments
            self._user = ""

    def text(self, value: str) -> str:
        for needle, token in self._pairs:
            value = value.replace(needle, token)
        value = _RE_SESSION_ID.sub("<SESSION_ID>", value)
        value = _RE_ISO_TS.sub("<TIMESTAMP>", value)
        value = _RE_UNIX_DATE.sub("<DATE>", value)
        value = _RE_BARE_DATE.sub("<DATE>", value)
        value = _RE_CLOCK.sub("<TIME>", value)
        value = _RE_TMPDIR.sub("<TMP>", value)
        if self._host:
            value = value.replace(self._host, "<HOSTNAME>")
        if self._user:
            value = re.sub(rf"\b{re.escape(self._user)}\b", "<USER>", value)
        return value

    def walk(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {self.walk(k): self.walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v) for v in obj]
        if isinstance(obj, tuple):
            return [self.walk(v) for v in obj]
        if isinstance(obj, (int, float, bool)) or obj is None:
            return obj
        return self.text(str(obj))


def _jsonable(obj: Any) -> Any:
    """Coerce anything the engine hands back into JSON-safe structure."""
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump())
    if hasattr(obj, "__dict__"):
        return _jsonable(vars(obj))
    return repr(obj)


# ---------------------------------------------------------------------------
# Case running
# ---------------------------------------------------------------------------


class Case:
    def __init__(self, raw: dict[str, Any]):
        self.name: str = raw["name"]
        self.recipe: str = raw["recipe"]
        self.covers: list[str] = list(raw.get("covers", []))
        self.context: dict[str, Any] = raw.get("context", {}) or {}
        self.caller_agents: dict[str, Any] = raw.get("caller_agents", {}) or {}
        self.agent_responses: dict[str, Any] = raw.get("agent_responses", {}) or {}
        self.volatile_outputs: list[str] = list(raw.get("volatile_outputs", []))
        self.approvals: str = raw.get("approvals", "none")
        self.approval_message: str = raw.get("approval_message", "")
        self.fixture: str | None = raw.get("fixture")
        self.path_prepend: str | None = raw.get("path_prepend")
        self.notes: str = raw.get("notes", "")


def load_cases() -> list[Case]:
    raw = yaml.safe_load(CASES_FILE.read_text())
    return [Case(c) for c in raw["cases"]]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def run_case(case: Case) -> dict[str, Any]:
    """Run one case against the current engine and return its normalized record."""
    recipe_path = REPO_ROOT / case.recipe
    if not recipe_path.exists():
        raise FileNotFoundError(f"case '{case.name}': recipe not found: {recipe_path}")

    workspace = Path(tempfile.mkdtemp(prefix=f"legacy-compat-{case.name}-"))
    saved_path = os.environ.get("PATH", "")
    try:
        if case.fixture:
            src = HERE / "fixtures" / case.fixture
            if not src.is_dir():
                raise FileNotFoundError(
                    f"case '{case.name}': fixture dir not found: {src}"
                )
            shutil.copytree(src, workspace, dirs_exist_ok=True)

        if case.path_prepend:
            # Bash steps inherit os.environ; prepending a fixture bin/ makes a
            # network-dependent CLI (gh, git) hermetic and reproducible.
            shim_dir = workspace / case.path_prepend
            if not shim_dir.is_dir():
                raise FileNotFoundError(
                    f"case '{case.name}': path_prepend dir not found: {shim_dir}"
                )
            for entry in shim_dir.iterdir():
                entry.chmod(0o755)
            os.environ["PATH"] = f"{shim_dir}{os.pathsep}{saved_path}"

        spawn = SpawnRecorder(case.agent_responses, case.caller_agents)
        coordinator = CallerFixture(case.caller_agents, workspace, spawn)
        session_manager = SessionManager(
            base_dir=workspace / ".recipe-sessions", auto_cleanup_days=7
        )
        executor = RecipeExecutor(coordinator, session_manager)

        # Observation-only wrapper: the tool path returns a compact summary, but
        # a baseline wants every step output the engine actually produced.
        captured: dict[str, Any] = {}
        inner = executor.execute_recipe

        async def capturing_execute_recipe(*args: Any, **kwargs: Any) -> Any:
            result = await inner(*args, **kwargs)
            captured["final_context"] = result
            return result

        executor.execute_recipe = capturing_execute_recipe  # type: ignore[method-assign]

        tool = RecipesTool(executor, session_manager, coordinator, {})

        calls: list[dict[str, Any]] = []

        first_input = {
            "operation": "execute",
            "recipe_path": str(recipe_path),
            "context": case.context,
        }
        result = await tool.execute(first_input)
        calls.append(_record_call(first_input, result, recipe_path))

        # Drive a staged recipe through its approval gates using the tool's own
        # approve + resume operations -- the real caller-facing path.
        rounds = 0
        while (
            case.approvals == "approve_all"
            and result.success
            and isinstance(result.output, dict)
            and result.output.get("status") == "paused_for_approval"
        ):
            rounds += 1
            if rounds > MAX_APPROVAL_ROUNDS:
                raise RuntimeError(
                    f"case '{case.name}': exceeded {MAX_APPROVAL_ROUNDS} approval rounds"
                )
            session_id = result.output["session_id"]
            stage_name = result.output["stage_name"]

            approve_input = {
                "operation": "approve",
                "session_id": session_id,
                "stage_name": stage_name,
                "message": case.approval_message,
            }
            approve_result = await tool.execute(approve_input)
            calls.append(_record_call(approve_input, approve_result, recipe_path))

            resume_input = {"operation": "resume", "session_id": session_id}
            result = await tool.execute(resume_input)
            calls.append(_record_call(resume_input, result, recipe_path))

        final_context = _jsonable(captured.get("final_context"))
        if isinstance(final_context, dict):
            for key in case.volatile_outputs:
                if key in final_context:
                    final_context[key] = "<VOLATILE>"

        norm = Normalizer(workspace)
        record: dict[str, Any] = {
            "harness_version": HARNESS_VERSION,
            "case": case.name,
            "recipe": case.recipe,
            "recipe_sha256": _sha256_file(recipe_path),
            "covers": case.covers,
            "notes": case.notes,
            "caller_agents": sorted(case.caller_agents),
            "volatile_outputs": sorted(case.volatile_outputs),
            "outcome": _outcome(calls),
            "tool_calls": norm.walk(calls),
            "final_context": norm.walk(final_context),
            "provenance": norm.walk(_provenance(spawn)),
            "events": norm.walk(
                [
                    {"event": e["event"], "data": _jsonable(e["data"])}
                    for e in coordinator.hooks.events
                ]
            ),
            "progress": norm.walk(coordinator.display_system.messages),
        }
        return record
    finally:
        os.environ["PATH"] = saved_path
        shutil.rmtree(workspace, ignore_errors=True)


def _record_call(
    tool_input: dict[str, Any], result: Any, recipe_path: Path
) -> dict[str, Any]:
    payload = _jsonable(result)
    scrubbed_input = dict(tool_input)
    if "recipe_path" in scrubbed_input:
        scrubbed_input["recipe_path"] = str(recipe_path)
    return {
        "input": _jsonable(scrubbed_input),
        "success": bool(getattr(result, "success", False)),
        "output": payload.get("output") if isinstance(payload, dict) else None,
        "error": payload.get("error") if isinstance(payload, dict) else None,
    }


def _outcome(calls: list[dict[str, Any]]) -> dict[str, Any]:
    last = calls[-1]
    out = last.get("output") or {}
    return {
        "tool_call_count": len(calls),
        "final_success": last["success"],
        "final_status": out.get("status") if isinstance(out, dict) else None,
        "final_error": (last.get("error") or {}).get("message")
        if isinstance(last.get("error"), dict)
        else None,
    }


def _provenance(spawn: SpawnRecorder) -> dict[str, Any]:
    by_step: dict[str, str] = {}
    for entry in spawn.spawns:
        step = str(entry.get("recipe_step") or f"#{entry['index']}")
        by_step[step] = str(entry.get("agent_name"))
    return {
        "agent_spawn_count": len(spawn.spawns),
        "agents_by_step": by_step,
        "agent_spawns": spawn.spawns,
    }


# ---------------------------------------------------------------------------
# Record / assert
# ---------------------------------------------------------------------------


def baseline_path(case: Case) -> Path:
    return BASELINE_DIR / f"{case.name}.json"


def dumps(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def cmd_list(cases: list[Case]) -> int:
    for case in cases:
        marker = "recorded" if baseline_path(case).exists() else "NO BASELINE"
        print(f"{case.name:34s} {case.recipe:48s} [{marker}]")
        if case.covers:
            print(f"{'':34s} covers: {', '.join(case.covers)}")
    return 0


def cmd_record(cases: list[Case], show: bool) -> int:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for case in cases:
        print(f"[record] {case.name} ({case.recipe})", flush=True)
        try:
            record = asyncio.run(run_case(case))
        except Exception as exc:
            failures += 1
            print(f"[record] {case.name}: FAILED to run: {type(exc).__name__}: {exc}")
            continue
        path = baseline_path(case)
        text = dumps(record)
        path.write_text(text)
        outcome = record["outcome"]
        print(
            f"[record] {case.name}: status={outcome['final_status']} "
            f"spawns={record['provenance']['agent_spawn_count']} "
            f"tool_calls={outcome['tool_call_count']} -> {path.relative_to(REPO_ROOT)}"
        )
        if show:
            print(text)
    return 1 if failures else 0


def cmd_assert(cases: list[Case], show: bool) -> int:
    drift = 0
    for case in cases:
        path = baseline_path(case)
        if not path.exists():
            drift += 1
            print(f"[assert] {case.name}: FAIL - no baseline at {path}")
            continue
        try:
            record = asyncio.run(run_case(case))
        except Exception as exc:
            drift += 1
            print(f"[assert] {case.name}: FAIL - run error: {type(exc).__name__}: {exc}")
            continue
        actual = dumps(record)
        expected = path.read_text()
        if actual == expected:
            print(f"[assert] {case.name}: PASS")
            if show:
                print(actual)
            continue
        drift += 1
        print(f"[assert] {case.name}: FAIL - drift against baseline")
        diff = difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"baseline/{case.name}.json",
            tofile=f"current/{case.name}.json",
            n=3,
        )
        sys.stdout.writelines(diff)
        print()
    if drift:
        print(f"\nLEGACY-COMPAT DRIFT: {drift} case(s) differ from baseline.")
        return 1
    print(f"\nLEGACY-COMPAT OK: {len(cases)} case(s) byte-identical to baseline.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list cases")
    group.add_argument("--record", action="store_true", help="record baselines")
    group.add_argument(
        "--assert",
        dest="do_assert",
        action="store_true",
        help="re-run and diff against baselines (non-zero exit on drift)",
    )
    parser.add_argument("--case", action="append", help="limit to named case(s)")
    parser.add_argument(
        "--show", action="store_true", help="print the full record to stdout"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # Keep the recipe engine's own chatter out of the way; it is not recorded.
    logging.getLogger("amplifier_module_tool_recipes").setLevel(logging.ERROR)

    cases = load_cases()
    if args.case:
        wanted = set(args.case)
        unknown = wanted - {c.name for c in cases}
        if unknown:
            parser.error(f"unknown case(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.name in wanted]

    if args.list:
        return cmd_list(cases)
    if args.record:
        return cmd_record(cases, args.show)
    return cmd_assert(cases, args.show)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONHASHSEED", "0")
    raise SystemExit(main())
