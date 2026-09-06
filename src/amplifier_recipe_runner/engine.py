"""The library's own step engine: the full recipe step vocabulary, standalone.

Contracts: ``recipe-runner-lib.v1`` Core 2, 3, 8.

Why this module exists
----------------------
Before it, the library executed exactly one step shape -- a sequential agent
step -- and refused everything else by name. Real recipes are mostly not agent
steps: ``recipes/repo-audit.yaml`` is 30 steps of which 3 are agent steps; the
rest are ``bash``, ``parse_json``, ``foreach``, conditions and staged
approvals. So *no real recipe* could run standalone, and in-session execution
had to be routed to the Amplifier-bound legacy engine
(``modules/tool-recipes/.../executor.py``) instead.

This module closes that gap. It implements the same vocabulary the legacy
engine implements -- bash, parse_json, foreach (sequential and parallel),
while/convergence loops, conditions, staged recipes with approval gates,
``type: recipe`` sub-recipes, templated timeouts, ``on_error``, retry -- with
**no** dependency on Amplifier, a coordinator, or a live session.

Parity, not resemblance
-----------------------
The semantics here are ported from the legacy engine deliberately and
literally, function by function: :func:`substitute_variables`,
:func:`resolve_dotted_path`, :func:`extract_json_aggressively`,
:func:`process_step_result`, :func:`resolve_step_timeout` and the loop bodies
all reproduce the legacy behaviour including its edge cases (whole-variable
type preservation, the three JSON-extraction strategies, empty-foreach
skipping, ``update_context`` running *after* the body, ``break_when`` running
after ``update_context``). Condition evaluation is not ported at all -- it is
*vendored verbatim* in :mod:`amplifier_recipe_runner.expressions`, because a
second implementation of an expression grammar is a second place for it to
drift.

Every place the two engines deliberately differ is written down in
``docs/EXECUTOR_PARITY.md`` with the reason. A difference that is not in that
matrix is a defect, not a design.

What this module refuses to do
------------------------------
It never fabricates a success (lib Core 8). A step shape it cannot run raises
:class:`UnsupportedStepError` naming the step; a bash step that exits non-zero
under the default ``on_error: fail`` fails the run; an approval gate with no
way to answer it *pauses* the run rather than passing itself.

This module imports nothing from Amplifier (lib Core 3).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Final
from typing import Protocol
from typing import runtime_checkable

import yaml

from .errors import RecipeRunnerError
from .expressions import ExpressionError
from .expressions import evaluate_condition
from .manifest import check_context_block

__all__ = [
    "ApprovalLedger",
    "ApprovalPaused",
    "ApprovalSpec",
    "BashOutcome",
    "EngineOutcome",
    "ExecutionError",
    "RecipeProgram",
    "RecursionLimits",
    "RecursionState",
    "ResumeState",
    "StageSpec",
    "StepEngine",
    "StepSpec",
    "SubRecipeRunner",
    "UnsupportedStepError",
    "coerce_timeout",
    "evaluate_condition",
    "extract_json_aggressively",
    "json_safe",
    "parse_program",
    "parse_step",
    "process_step_result",
    "resolve_dotted_path",
    "resolve_foreach_variable",
    "resolve_step_timeout",
    "substitute_recursive",
    "substitute_variables",
]


# --------------------------------------------------------------------------
# Constants ported from the legacy engine (executor.py)
# --------------------------------------------------------------------------

#: Keys ``execute_recipe`` injects into every context. Never a sub-recipe's
#: own output -- see :meth:`StepEngine._run_sub_recipe`.
RECIPE_INTERNAL_KEYS: Final[frozenset[str]] = frozenset({"recipe", "session", "step", "stage", "_skipped_steps"})

#: Values whose JSON form exceeds this are replaced by a placeholder in the
#: on-disk state. The live context is never trimmed.
CHECKPOINT_TRIM_THRESHOLD_BYTES: Final[int] = 100_000

#: Advisory ceiling on a rendered bash command, well under Linux's
#: ``MAX_ARG_STRLEN``. Exceeding the real cap fails at *exec* time, with no
#: exit code and no stderr, so warn before the cliff.
COMMAND_SIZE_WARN_BYTES: Final[int] = 100_000

#: Synthetic exit code for a command that could not be executed at all.
EXEC_FAILURE_EXIT_CODE: Final[int] = 126

#: Step keys carrying agent instruction text, in precedence order. The legacy
#: engine reads ``prompt``; the library's own recipes were written with
#: ``instruction``, so both are accepted and ``prompt`` wins where both appear.
INSTRUCTION_KEYS: Final[tuple[str, ...]] = ("prompt", "instruction", "message")

_WINDOWS_PROGRAM_ROOT_VARS: Final[tuple[str, ...]] = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
_GIT_BASH_RELATIVE: Final[str] = r"\bin\bash.exe"


# --------------------------------------------------------------------------
# Errors and internal signals
# --------------------------------------------------------------------------


class ExecutionError(RecipeRunnerError):
    """A failure *during* execution, after preflight passed.

    Deliberately distinct from
    :class:`~amplifier_recipe_runner.errors.PreflightError`: catching that one
    still means "nothing ran", and this one must not blur it.
    """


class UnsupportedStepError(ExecutionError):
    """A step shape this engine cannot run.

    Raised rather than skipped: a skipped step that reported success would be
    exactly the fabricated success lib Core 8 forbids.
    """

    def __init__(self, step_id: str | None, reason: str, *, remedy: str | None = None) -> None:
        self.step_id = step_id
        self.reason = reason
        where = f"Step {step_id!r}" if step_id else "A step"
        super().__init__(
            f"{where} cannot be executed by the sequential executor: {reason}.",
            remedy=remedy
            or (
                "Give the step an `agent:` and an `instruction:`, a `type: bash` with a "
                "`command:`, or a `type: recipe` with a `recipe:` path."
            ),
        )


class StepFailedError(ExecutionError):
    """A step ran and failed. Carries the step id and the underlying cause."""

    def __init__(self, step_id: str, message: str, *, remedy: str | None = None) -> None:
        self.step_id = step_id
        super().__init__(message, remedy=remedy)


class SkipRemaining(Exception):
    """Internal signal: ``on_error: skip_remaining`` fired.

    Not an error -- the run continues and reports success, exactly as the
    legacy engine's ``SkipRemainingError`` does.
    """


class RunCancelled(Exception):
    """Internal signal: the host's cancellation port asked the run to stop."""


class ApprovalPaused(Exception):
    """Internal signal: the run reached an approval gate it cannot answer.

    A pause is not a failure and not a success; it is a third outcome, which
    is why it is neither swallowed nor reported as either.
    """

    def __init__(self, stage: str, prompt: str) -> None:
        self.stage = stage
        self.prompt = prompt
        super().__init__(f"paused for approval at stage {stage!r}")


class ApprovalDeniedError(ExecutionError):
    """A stage's approval gate was answered ``deny``. The run stops there."""

    def __init__(self, stage: str, message: str | None = None) -> None:
        self.stage = stage
        detail = f": {message}" if message else ""
        super().__init__(
            f"Execution denied at stage {stage!r}{detail}",
            remedy="Approve the stage, or edit the recipe so the gate is not reached.",
        )


# --------------------------------------------------------------------------
# Pure helpers -- ported from executor.py
# --------------------------------------------------------------------------


def coerce_timeout(value: Any) -> int | float | None:
    """Coerce a step ``timeout:`` to a number, or return ``None``.

    Verbatim port of ``models.coerce_timeout``: booleans are ints in Python
    but never a meaningful number of seconds, and a template string is not
    known until execution time.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _sanitize_default(obj: Any) -> Any:
    """``json.dumps`` *default* hook: never lose the fact something was there."""
    for attribute in ("model_dump", "dict"):
        method = getattr(obj, attribute, None)
        if callable(method):
            try:
                return method()
            except Exception:  # noqa: BLE001 - a hook must not break a write
                break
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, Path):
        return str(obj)
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict) and data:
        return dict(data)
    return f"[non-serializable: {type(obj).__name__}]"


def json_safe(value: Any) -> Any:
    """Deep-convert ``value`` into something ``json.dump`` cannot choke on."""
    try:
        return json.loads(json.dumps(value, default=_sanitize_default))
    except (TypeError, ValueError, RecursionError):
        return f"[non-serializable: {type(value).__name__}]"


def resolve_dotted_path(var_ref: str, context: Mapping[str, Any]) -> Any:
    """Resolve a dotted variable reference, preserving the leaf's native type.

    Ported from ``RecipeExecutor._resolve_dotted_path``, error text included:
    the "it's a str, not a dict" message is the one that tells a recipe author
    their bash step forgot ``parse_json: true``.
    """
    parts = var_ref.split(".")
    current: Any = context
    path_so_far: list[str] = []
    for part in parts:
        path_so_far.append(part)
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, dict):
            raise ValueError(
                f"Undefined variable: {{{{{var_ref}}}}}. "
                f"Key '{part}' not found. "
                f"Available keys at "
                f"'{'.'.join(path_so_far[:-1]) or 'root'}': "
                f"{', '.join(sorted(current.keys()))}"
            )
        else:
            parent_path = ".".join(path_so_far[:-1])
            raise ValueError(
                f"Cannot access '{part}' on "
                f"{{{{{parent_path}}}}} - "
                f"it's a {type(current).__name__}, not a dict. "
                f"Hint: The step producing '{parent_path}' may have "
                f"failed to parse JSON. "
                f"Check that the bash command outputs clean JSON "
                f"or add 'parse_json: true'."
            )
    return current


def substitute_variables(template: str, context: Mapping[str, Any]) -> str:
    """Replace ``{{variable}}`` references with context values.

    Ported from ``RecipeExecutor.substitute_variables``. Booleans render as
    ``true``/``false`` (not Python's ``True``) and dicts/lists render as JSON
    (not Python ``repr``), because the overwhelming consumer is a shell
    command or a JSON payload.
    """
    pattern = r"\{\{(\w+(?:\.\w+)*)\}\}"

    def replace(match: re.Match[str]) -> str:
        var_ref = match.group(1)

        if "." in var_ref:
            value = resolve_dotted_path(var_ref, context)
            return _render(value)

        if var_ref not in context:
            available = ", ".join(sorted(context.keys()))
            raise ValueError(f"Undefined variable: {{{{{var_ref}}}}}. Available variables: {available}")

        return _render(context[var_ref])

    return re.sub(pattern, replace, template)


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=_sanitize_default)
    return str(value)


def substitute_recursive(value: Any, context: Mapping[str, Any]) -> Any:
    """Substitute through nested structures, preserving native types.

    Ported from ``RecipeExecutor._substitute_variables_recursive``. The
    load-bearing case: a string that is *exactly* one whole-variable reference
    resolves to the native object, so a dict forwarded into a sub-recipe's
    ``context:`` block stays a dict instead of becoming a JSON string.
    """
    if isinstance(value, str):
        whole_var = re.fullmatch(r"\s*\{\{(\w+(?:\.\w+)*)\}\}\s*", value)
        if whole_var:
            var_ref = whole_var.group(1)
            if "." in var_ref:
                return resolve_dotted_path(var_ref, context)
            if var_ref in context:
                return context[var_ref]
            # Not found -- fall through so substitute_variables raises the
            # descriptive error rather than this function inventing one.
        return substitute_variables(value, context)
    if isinstance(value, dict):
        return {key: substitute_recursive(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_recursive(item, context) for item in value]
    return value


def resolve_foreach_variable(foreach: str, context: Mapping[str, Any]) -> Any:
    """Resolve a ``foreach: "{{var}}"`` reference to its value."""
    pattern = r"\{\{(\w+(?:\.\w+)*)\}\}"
    match = re.match(pattern, foreach.strip())
    if not match:
        raise ValueError(f"Invalid foreach syntax: {foreach}")

    var_path = match.group(1)
    parts = var_path.split(".")
    value: Any = context
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise ValueError(f"Undefined variable in foreach: {foreach}")
    return value


def extract_json_aggressively(output: str) -> Any:
    """Extract JSON from text using the legacy engine's three strategies.

    1. the whole string parses;
    2. the first fenced block parses (via ``raw_decode``, so balanced braces
       are the JSON parser's problem, not a regex's);
    3. the first non-trivial ``{``/``[`` in document order parses.

    A trivial ``{}``/``[]`` is remembered but not preferred, so a meaningful
    structure later in the text wins. Returns the input unchanged when nothing
    parses -- never ``None``, which would be indistinguishable from a JSON
    ``null``.
    """
    output_stripped = output.strip()

    if not output_stripped:
        return output

    try:
        return json.loads(output_stripped)
    except (json.JSONDecodeError, ValueError):
        pass

    fence_match = re.search(r"```(?:json)?\s*", output_stripped)
    if fence_match:
        fence_start = fence_match.end()
        end_fence_idx = output_stripped.find("```", fence_start)
        if end_fence_idx != -1:
            fenced_content = output_stripped[fence_start:end_fence_idx].strip()
            if fenced_content:
                try:
                    parsed_s2, _ = json.JSONDecoder().raw_decode(fenced_content)
                    if parsed_s2 != {} and parsed_s2 != []:
                        return parsed_s2
                except (json.JSONDecodeError, ValueError):
                    pass

    decoder = json.JSONDecoder()
    first_parsed = None
    idx = 0
    while idx < len(output_stripped):
        idx_bracket = output_stripped.find("[", idx)
        idx_brace = output_stripped.find("{", idx)
        candidates = [i for i in (idx_bracket, idx_brace) if i != -1]
        if not candidates:
            break
        next_idx = min(candidates)
        try:
            parsed, _end_idx = decoder.raw_decode(output_stripped, next_idx)
            if first_parsed is None:
                first_parsed = parsed
            if parsed != {} and parsed != []:
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        idx = next_idx + 1

    if first_parsed is not None:
        return first_parsed

    return output


def process_step_result(result: Any, step: StepSpec) -> Any:
    """Unwrap a spawn envelope and optionally parse JSON.

    Ported from ``RecipeExecutor._process_step_result``: conservative by
    default (only a *whole* clean-JSON string parses), aggressive under
    ``parse_json: true``, with a bash-only aggressive fallback because a
    command routinely prints progress lines before its JSON.
    """
    if isinstance(result, Mapping) and "output" in result:
        output = result["output"]
    else:
        output = result

    if isinstance(output, str) and step.parse_json:
        return extract_json_aggressively(output)

    if isinstance(output, str):
        output_stripped = output.strip()
        if output_stripped:
            try:
                return json.loads(output_stripped)
            except (json.JSONDecodeError, ValueError):
                if step.type == "bash":
                    extracted = extract_json_aggressively(output)
                    if extracted != output:
                        return extracted

    return output


def resolve_step_timeout(step: StepSpec, context: Mapping[str, Any]) -> int | float:
    """Resolve a step's ``timeout:`` to a number of seconds.

    ``asyncio.wait_for`` accepts a string and then compares it against a
    float, so an unresolved template blows up deep in the event loop naming
    neither the step nor the field. This resolves it once, up front, and fails
    with a message that names both. A literal number is returned untouched.
    """
    raw = step.timeout
    literal = coerce_timeout(raw)
    if literal is not None and not isinstance(raw, str):
        return literal

    try:
        rendered = substitute_variables(str(raw), context)
    except ValueError as exc:
        raise ValueError(f"Step '{step.id}': timeout template {raw!r} could not be resolved: {exc}") from None

    resolved = coerce_timeout(rendered)
    if resolved is None:
        raise ValueError(
            f"Step '{step.id}': timeout template {raw!r} resolved to {rendered!r}, which is not a number of seconds"
        )
    if resolved <= 0:
        raise ValueError(f"Step '{step.id}': timeout template {raw!r} resolved to {resolved}, but timeout must be positive")
    return resolved


def _is_wsl_bash(path: str) -> bool:
    lowered = path.lower().replace("/", "\\")
    return "\\system32\\" in lowered or "\\sysnative\\" in lowered


def _find_git_bash() -> str | None:
    candidates: list[str] = []
    for var in _WINDOWS_PROGRAM_ROOT_VARS:
        root = os.environ.get(var)
        if root:
            candidates.append(root + r"\Git" + _GIT_BASH_RELATIVE)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(local_app_data + r"\Programs\Git" + _GIT_BASH_RELATIVE)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    git_exe = shutil.which("git")
    if git_exe:
        derived = os.path.dirname(os.path.dirname(git_exe)) + _GIT_BASH_RELATIVE
        if os.path.isfile(derived):
            return derived
    path_bash = shutil.which("bash")
    if path_bash and not _is_wsl_bash(path_bash):
        return path_bash
    return None


def resolve_bash() -> str:
    """Resolve a real bash for ``type: bash`` steps.

    Recipe bash steps use pipefail, arrays, brace expansion and ``&>``, none
    of which ``/bin/sh`` (dash on Ubuntu) has. On Windows the WSL launcher is
    rejected outright rather than used: a Windows ``AMPLIFIER_PYTHON`` path is
    meaningless inside WSL, so recipes would fall back to a *different* Python
    and fail later with a confusing ImportError.
    """
    if os.name != "nt":
        return "/bin/bash"

    git_bash = _find_git_bash()
    if git_bash:
        return git_bash

    path_bash = shutil.which("bash")
    if path_bash and _is_wsl_bash(path_bash):
        raise ExecutionError(
            "Recipe bash steps require Git for Windows bash, but the only bash "
            f"found is the WSL launcher ({path_bash}).",
            remedy="Install Git for Windows (https://git-scm.com/download/win), which provides a compatible bash.",
        )

    raise ExecutionError(
        "Recipe bash steps require a bash executable, but none was found.",
        remedy="Install Git for Windows (https://git-scm.com/download/win), which provides bash.",
    )


def _amplifier_python() -> str:
    if os.name != "nt":
        return sys.executable
    return sys.executable.replace("\\", "/")


# --------------------------------------------------------------------------
# Parsed recipe shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepSpec:
    """One step, parsed. Field names mirror the legacy ``Step`` dataclass.

    YAML key remapping matches ``Recipe._parse_step`` exactly: ``as`` ->
    :attr:`as_var`, ``context`` -> :attr:`step_context`, ``steps`` ->
    :attr:`body_steps`.
    """

    id: str
    type: str = "agent"
    agent: str | None = None
    prompt: str | None = None
    mode: str | None = None

    recipe: str | None = None
    step_context: Mapping[str, Any] | None = None

    command: str | None = None
    cwd: str | None = None
    env: Mapping[str, Any] | None = None
    output_exit_code: str | None = None

    output: str | None = None
    condition: str | None = None
    foreach: str | None = None
    as_var: str | None = None
    collect: str | None = None
    parallel: bool | int = False
    max_iterations: int = 100
    timeout: int | float | str = 600
    retry: Mapping[str, Any] | None = None
    on_error: str = "fail"
    parse_json: bool = False

    while_condition: str | None = None
    max_while_iterations: int = 100
    break_when: str | None = None
    update_context: Mapping[str, Any] | None = None
    body_steps: tuple[Mapping[str, Any], ...] | None = None

    recursion: RecursionLimits | None = None

    provider: str | None = None
    model: str | None = None
    provider_preferences: tuple[Mapping[str, Any], ...] | None = None
    model_role: str | None = None

    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_loop(self) -> bool:
        return bool(self.foreach) or bool(self.while_condition)


@dataclass(frozen=True, slots=True)
class ApprovalSpec:
    """A stage's approval gate, parsed."""

    required: bool = False
    prompt: str = ""
    timeout: int = 0
    default: str = "deny"
    when: str = "after_stage"

    @property
    def gates_before_stage(self) -> bool:
        return self.required and self.when == "before_stage"


@dataclass(frozen=True, slots=True)
class StageSpec:
    name: str
    steps: tuple[StepSpec, ...]
    approval: ApprovalSpec | None = None


@dataclass(frozen=True, slots=True)
class RecursionLimits:
    max_depth: int = 5
    max_total_steps: int = 100


@dataclass(frozen=True, slots=True)
class RecipeProgram:
    """A recipe body, parsed into the shapes the engine executes."""

    name: str
    version: str | None
    description: str | None
    path: Path | None
    context: Mapping[str, Any] = field(default_factory=dict)
    """The recipe's declared context, already RESOLVED: a declarative entry
    (``type:``/``required:``/``default:``) contributes its ``default:``, never
    the declaration mapping itself (recipes-u2f)."""

    steps: tuple[StepSpec, ...] = ()
    stages: tuple[StageSpec, ...] = ()
    recursion: RecursionLimits | None = None
    schema_version: int | None = None
    required_context: tuple[str, ...] = ()
    """Variables declared ``required: true`` with no ``default:``. They are
    deliberately absent from :attr:`context`; the caller supplies them, and
    :mod:`.execution` refuses the run by name if it does not."""

    @property
    def is_staged(self) -> bool:
        return bool(self.stages)

    @property
    def all_steps(self) -> tuple[StepSpec, ...]:
        """Top-level steps, flat then staged, in declaration order."""
        if self.stages:
            return tuple(step for stage in self.stages for step in stage.steps)
        return self.steps


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_step(data: Mapping[str, Any], *, index: int = 0) -> StepSpec:
    """Parse one step mapping.

    Positional ids (``step-<n>``) match
    :func:`amplifier_recipe_runner.execution._step_ids` so that a resumed run
    and the run that recorded it cannot disagree about which step is which.
    """
    if not isinstance(data, Mapping):
        raise UnsupportedStepError(None, "it is not a mapping")

    step_id = data.get("id") if isinstance(data.get("id"), str) else f"step-{index}"

    prompt: str | None = None
    for key in INSTRUCTION_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            prompt = value
            break

    body = data.get("steps")
    body_steps = tuple(item for item in body if isinstance(item, Mapping)) if isinstance(body, list) else None

    recursion_data = data.get("recursion")
    recursion = None
    if isinstance(recursion_data, Mapping):
        recursion = RecursionLimits(
            max_depth=_int_or(recursion_data.get("max_depth"), 5),
            max_total_steps=_int_or(recursion_data.get("max_total_steps"), 100),
        )

    preferences = data.get("provider_preferences")
    provider_preferences = (
        tuple(item for item in preferences if isinstance(item, Mapping)) if isinstance(preferences, list) else None
    )

    timeout: Any = data.get("timeout", 600)
    if isinstance(timeout, str) and "{{" not in timeout:
        coerced = coerce_timeout(timeout)
        if coerced is not None:
            timeout = coerced

    parallel = data.get("parallel", False)
    if not isinstance(parallel, (bool, int)):
        parallel = False

    return StepSpec(
        id=step_id,
        type=str(data.get("type") or "agent"),
        agent=data.get("agent") if isinstance(data.get("agent"), str) else None,
        prompt=prompt,
        mode=data.get("mode") if isinstance(data.get("mode"), str) else None,
        recipe=data.get("recipe") if isinstance(data.get("recipe"), str) else None,
        step_context=data.get("context") if isinstance(data.get("context"), Mapping) else None,
        command=data.get("command") if isinstance(data.get("command"), str) else None,
        cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else None,
        env=data.get("env") if isinstance(data.get("env"), Mapping) else None,
        output_exit_code=data.get("output_exit_code") if isinstance(data.get("output_exit_code"), str) else None,
        output=data.get("output") if isinstance(data.get("output"), str) else None,
        condition=data.get("condition") if isinstance(data.get("condition"), str) else None,
        foreach=data.get("foreach") if isinstance(data.get("foreach"), str) else None,
        as_var=data.get("as") if isinstance(data.get("as"), str) else None,
        collect=data.get("collect") if isinstance(data.get("collect"), str) else None,
        parallel=parallel,
        max_iterations=_int_or(data.get("max_iterations"), 100),
        timeout=timeout,
        retry=data.get("retry") if isinstance(data.get("retry"), Mapping) else None,
        on_error=str(data.get("on_error") or "fail"),
        parse_json=bool(data.get("parse_json", False)),
        while_condition=data.get("while_condition") if isinstance(data.get("while_condition"), str) else None,
        max_while_iterations=_int_or(data.get("max_while_iterations"), 100),
        break_when=data.get("break_when") if isinstance(data.get("break_when"), str) else None,
        update_context=data.get("update_context") if isinstance(data.get("update_context"), Mapping) else None,
        body_steps=body_steps,
        recursion=recursion,
        provider=data.get("provider") if isinstance(data.get("provider"), str) else None,
        model=data.get("model") if isinstance(data.get("model"), str) else None,
        provider_preferences=provider_preferences,
        model_role=data.get("model_role") if isinstance(data.get("model_role"), str) else None,
        raw=dict(data),
    )


def _parse_approval(data: Any) -> ApprovalSpec | None:
    if not isinstance(data, Mapping):
        return None
    return ApprovalSpec(
        required=bool(data.get("required", False)),
        prompt=str(data.get("prompt") or ""),
        timeout=_int_or(data.get("timeout"), 0),
        default=str(data.get("default") or "deny"),
        when=str(data.get("when") or "after_stage"),
    )


def parse_program(body: Mapping[str, Any], *, path: Path | None = None) -> RecipeProgram:
    """Parse a recipe body into a :class:`RecipeProgram`."""
    if body.get("stages") and body.get("steps"):
        # Refused, not merged: a staged run and a flat run schedule work
        # differently (approval gates, stage state), so silently picking one
        # would drop the other's steps while reporting success.
        raise ExecutionError(
            "Recipe declares both 'stages' and 'steps' - use one or the other.",
            remedy="Move the flat steps into a stage, or delete the `stages:` block.",
        )

    steps: list[StepSpec] = []
    stages: list[StageSpec] = []

    raw_steps = body.get("steps")
    if isinstance(raw_steps, list):
        for item in raw_steps:
            if isinstance(item, Mapping):
                # Positional ids count only real steps, matching the order
                # `execution._step_ids` records -- so a resumed run and the run
                # that recorded it cannot disagree about which step is which.
                steps.append(parse_step(item, index=len(steps)))

    raw_stages = body.get("stages")
    if isinstance(raw_stages, list):
        index = len(steps)
        for stage_data in raw_stages:
            if not isinstance(stage_data, Mapping):
                continue
            stage_steps: list[StepSpec] = []
            for item in stage_data.get("steps") or ():
                if isinstance(item, Mapping):
                    stage_steps.append(parse_step(item, index=index))
                    index += 1
            stages.append(
                StageSpec(
                    name=str(stage_data.get("name") or ""),
                    steps=tuple(stage_steps),
                    approval=_parse_approval(stage_data.get("approval")),
                )
            )

    recursion_data = body.get("recursion")
    recursion = None
    if isinstance(recursion_data, Mapping):
        recursion = RecursionLimits(
            max_depth=_int_or(recursion_data.get("max_depth"), 5),
            max_total_steps=_int_or(recursion_data.get("max_total_steps"), 100),
        )

    schema_version = body.get("schema_version")

    # `context:` is a variable -> VALUE mapping. An entry written in the
    # declarative schema form binds its `default:` here; one that is malformed
    # raises by name rather than reaching a prompt as `{'type': ...}`
    # (recipes-u2f). Sub-recipes are parsed through this same function, so they
    # get the identical treatment without a second code path.
    resolved_context = check_context_block(body.get("context"), source=str(path) if path else None)

    return RecipeProgram(
        name=str(body.get("name") or (path.stem if path else "recipe")),
        version=str(body["version"]) if body.get("version") is not None else None,
        description=str(body["description"]) if body.get("description") is not None else None,
        path=path,
        context=dict(resolved_context.values),
        steps=tuple(steps),
        stages=tuple(stages),
        recursion=recursion,
        schema_version=schema_version if isinstance(schema_version, int) else None,
        required_context=resolved_context.required,
    )


def load_program(path: Path) -> RecipeProgram:
    """Read and parse a recipe file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        return RecipeProgram(name=Path(path).stem, version=None, description=None, path=Path(path))
    return parse_program(data, path=Path(path))


# --------------------------------------------------------------------------
# Recursion accounting (ported from executor.RecursionState)
# --------------------------------------------------------------------------


@dataclass
class RecursionState:
    current_depth: int = 0
    total_steps: int = 0
    max_depth: int = 5
    max_total_steps: int = 100
    recipe_stack: list[str] = field(default_factory=list)

    def check_depth(self) -> None:
        if self.current_depth >= self.max_depth:
            raise ExecutionError(
                f"Recipe recursion depth {self.current_depth} exceeds limit {self.max_depth}. "
                f"Stack: {' -> '.join(self.recipe_stack)}",
                remedy="Raise `recursion.max_depth`, or flatten the sub-recipe chain.",
            )

    def check_total_steps(self) -> None:
        if self.total_steps >= self.max_total_steps:
            raise ExecutionError(
                f"Total steps {self.total_steps} exceeds limit {self.max_total_steps}",
                remedy="Raise `recursion.max_total_steps`, or reduce the number of agent steps.",
            )

    def increment_steps(self) -> None:
        self.total_steps += 1
        self.check_total_steps()

    def enter_recipe(self, recipe_name: str, override: RecursionLimits | None = None) -> RecursionState:
        return RecursionState(
            current_depth=self.current_depth + 1,
            total_steps=self.total_steps,
            max_depth=override.max_depth if override else self.max_depth,
            max_total_steps=override.max_total_steps if override else self.max_total_steps,
            recipe_stack=[*self.recipe_stack, recipe_name],
        )


# --------------------------------------------------------------------------
# Outcome and resumable state
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BashOutcome:
    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class ResumeState:
    """Where a paused or interrupted run left off.

    Persisted by the host (the CLI writes it under the run directory) and
    handed back on resume. Everything here is a *recorded fact*: nothing is
    re-derived, so a resumed run cannot disagree with the run that recorded it.
    """

    completed_steps: tuple[str, ...] = ()
    completed_stages: tuple[str, ...] = ()
    stage_index: int = 0
    step_in_stage: int = 0
    context: Mapping[str, Any] = field(default_factory=dict)
    outputs: Mapping[str, Any] = field(default_factory=dict)
    pending_approval: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "completed_steps": list(self.completed_steps),
            "completed_stages": list(self.completed_stages),
            "stage_index": self.stage_index,
            "step_in_stage": self.step_in_stage,
            "context": json_safe(_trim_for_state(self.context)),
            "outputs": json_safe(_trim_for_state(self.outputs)),
            "pending_approval": self.pending_approval,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ResumeState:
        return cls(
            completed_steps=tuple(str(step) for step in (data.get("completed_steps") or ())),
            completed_stages=tuple(str(stage) for stage in (data.get("completed_stages") or ())),
            stage_index=_int_or(data.get("stage_index"), 0),
            step_in_stage=_int_or(data.get("step_in_stage"), 0),
            context=dict(data.get("context") or {}),
            outputs=dict(data.get("outputs") or {}),
            pending_approval=str(data["pending_approval"]) if data.get("pending_approval") else None,
        )


def _trim_for_state(context: Mapping[str, Any]) -> dict[str, Any]:
    """Summarise oversized values for the on-disk copy. Live context untouched."""
    trimmed: dict[str, Any] = {}
    for key, value in context.items():
        try:
            serialised = json.dumps(value, ensure_ascii=False, default=_sanitize_default)
        except (TypeError, ValueError):
            trimmed[key] = json_safe(value)
            continue
        if len(serialised) > CHECKPOINT_TRIM_THRESHOLD_BYTES:
            trimmed[key] = f"[trimmed: {len(serialised) // 1024}KB - omitted from checkpoint to reduce write pressure]"
        else:
            trimmed[key] = value
    return trimmed


@dataclass(frozen=True, slots=True)
class EngineOutcome:
    """What the engine did. Honest in all four directions (lib Core 8)."""

    status: str
    """One of ``succeeded``, ``failed``, ``cancelled``, ``paused``."""

    context: Mapping[str, Any] = field(default_factory=dict)
    outputs: Mapping[str, Any] = field(default_factory=dict)
    completed_steps: tuple[str, ...] = ()
    pending_approval: str | None = None
    approval_prompt: str | None = None
    error: BaseException | None = None
    state: ResumeState | None = None


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------


@dataclass
class ApprovalLedger:
    """Recorded verdicts per stage, keyed by stage name.

    A ledger is what makes pause -> approve -> resume work across *separate
    processes*: the approving process writes a verdict here, and the resuming
    process reads it. Nothing about the verdict is inferred from timing or
    from the presence of a file.
    """

    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def verdict(self, stage: str) -> str | None:
        entry = self.decisions.get(stage)
        if not entry:
            return None
        return "approved" if entry.get("approved") else "denied"

    def message(self, stage: str) -> str:
        entry = self.decisions.get(stage) or {}
        return str(entry.get("message") or "")

    def record(self, stage: str, *, approved: bool, message: str | None = None) -> None:
        self.decisions[stage] = {"approved": bool(approved), "message": message or ""}

    def to_mapping(self) -> dict[str, Any]:
        return {stage: dict(entry) for stage, entry in self.decisions.items()}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ApprovalLedger:
        ledger = cls()
        for stage, entry in (data or {}).items():
            if isinstance(entry, Mapping):
                ledger.record(str(stage), approved=bool(entry.get("approved")), message=entry.get("message"))
        return ledger


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


@runtime_checkable
class AgentInvoker(Protocol):
    """Runs one agent step. The engine never resolves an agent name itself."""

    async def __call__(self, step: StepSpec, instruction: str, context: Mapping[str, Any]) -> Any: ...


@runtime_checkable
class SubRecipeRunner(Protocol):
    """Runs a ``type: recipe`` step's sub-recipe and returns its output delta.

    Injected rather than implemented here because *how* a sub-recipe gets its
    agent catalog is a closure question (a ``schema_version: 2`` sub-recipe
    resolves from its own declared closure), and closures live in
    :mod:`amplifier_recipe_runner.execution`, not in the step vocabulary.
    """

    async def __call__(
        self,
        path: Path,
        context: dict[str, Any],
        step: StepSpec,
        recursion: RecursionState,
    ) -> Mapping[str, Any]: ...


ProgressHook = Callable[[str, Mapping[str, Any]], None]


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


class StepEngine:
    """Executes a :class:`RecipeProgram` against a mutable context dict."""

    def __init__(
        self,
        program: RecipeProgram,
        *,
        invoke_agent: AgentInvoker,
        workspace: Path,
        run_id: str,
        recipe_path: Path | None = None,
        emit: ProgressHook | None = None,
        cancellation: Any | None = None,
        approval_callback: Any | None = None,
        approvals: ApprovalLedger | None = None,
        sub_recipe_runner: SubRecipeRunner | None = None,
        recursion: RecursionState | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        self._program = program
        self._invoke_agent = invoke_agent
        self._workspace = Path(workspace)
        self._run_id = run_id
        self._recipe_path = Path(recipe_path) if recipe_path else program.path
        self._emit = emit or (lambda kind, data: None)
        self._cancellation = cancellation
        self._approval_callback = approval_callback
        self._approvals = approvals if approvals is not None else ApprovalLedger()
        self._sub_recipe_runner = sub_recipe_runner
        self._scratch_dir = scratch_dir
        limits = program.recursion or RecursionLimits()
        self._recursion = recursion or RecursionState(
            max_depth=limits.max_depth,
            max_total_steps=limits.max_total_steps,
            recipe_stack=[program.name],
        )
        self._outputs: dict[str, Any] = {}
        self._completed: list[str] = []

    # -- public entry ------------------------------------------------------

    @property
    def approvals(self) -> ApprovalLedger:
        return self._approvals

    @property
    def recursion(self) -> RecursionState:
        return self._recursion

    async def execute(
        self,
        context: dict[str, Any],
        *,
        resume: ResumeState | None = None,
    ) -> EngineOutcome:
        """Run the program. Returns an outcome; raises only for a bug here."""
        self._outputs = dict(resume.outputs) if resume else {}
        self._completed = list(resume.completed_steps) if resume else []

        stage_index = resume.stage_index if resume else 0
        step_in_stage = resume.step_in_stage if resume else 0
        completed_stages = list(resume.completed_stages) if resume else []

        try:
            if self._program.is_staged:
                await self._run_stages(
                    context,
                    stage_index=stage_index,
                    step_in_stage=step_in_stage,
                    completed_stages=completed_stages,
                    resumed_gate=resume.pending_approval if resume else None,
                )
            else:
                await self._run_flat(context, skip=set(self._completed))
        except RunCancelled:
            return self._outcome("cancelled", context, completed_stages, stage_index, step_in_stage)
        except ApprovalPaused as paused:
            return EngineOutcome(
                status="paused",
                context=dict(context),
                outputs=dict(self._outputs),
                completed_steps=tuple(self._completed),
                pending_approval=paused.stage,
                approval_prompt=paused.prompt,
                state=ResumeState(
                    completed_steps=tuple(self._completed),
                    completed_stages=tuple(completed_stages),
                    stage_index=self._paused_stage_index,
                    step_in_stage=self._paused_step_in_stage,
                    context=dict(context),
                    outputs=dict(self._outputs),
                    pending_approval=paused.stage,
                ),
            )
        except SkipRemaining:
            # `on_error: skip_remaining` stops the run early and reports
            # success -- the recipe asked for exactly that.
            pass
        except Exception as exc:  # noqa: BLE001 - every failure is a failure
            return EngineOutcome(
                status="failed",
                context=dict(context),
                outputs=dict(self._outputs),
                completed_steps=tuple(self._completed),
                error=exc,
                state=ResumeState(
                    completed_steps=tuple(self._completed),
                    completed_stages=tuple(completed_stages),
                    stage_index=stage_index,
                    step_in_stage=step_in_stage,
                    context=dict(context),
                    outputs=dict(self._outputs),
                ),
            )

        return self._outcome("succeeded", context, completed_stages, len(self._program.stages), 0)

    def _outcome(
        self,
        status: str,
        context: Mapping[str, Any],
        completed_stages: Sequence[str],
        stage_index: int,
        step_in_stage: int,
    ) -> EngineOutcome:
        return EngineOutcome(
            status=status,
            context=dict(context),
            outputs=dict(self._outputs),
            completed_steps=tuple(self._completed),
            state=ResumeState(
                completed_steps=tuple(self._completed),
                completed_stages=tuple(completed_stages),
                stage_index=stage_index,
                step_in_stage=step_in_stage,
                context=dict(context),
                outputs=dict(self._outputs),
            ),
        )

    # -- flat execution ----------------------------------------------------

    async def _run_flat(self, context: dict[str, Any], *, skip: set[str]) -> None:
        for index, step in enumerate(self._program.steps):
            if step.id in skip:
                # Visible, not silent: a skipped step is a claim about earlier work.
                self._emit("step:skipped", {"step_id": step.id})
                continue
            self._check_cancelled()
            context["step"] = {"id": step.id, "index": index}
            try:
                await self._run_top_level_step(step, context)
            except SkipRemaining:
                return

    # -- staged execution --------------------------------------------------

    _paused_stage_index: int = 0
    _paused_step_in_stage: int = 0

    async def _run_stages(
        self,
        context: dict[str, Any],
        *,
        stage_index: int,
        step_in_stage: int,
        completed_stages: list[str],
        resumed_gate: str | None,
    ) -> None:
        # The stage whose gate this resume just came through, if any. A
        # `when: before_stage` gate parks the run ON its own stage, so without
        # this the very next loop iteration would re-park at the same gate.
        approved_gate_stage: str | None = None
        if resumed_gate is not None:
            verdict = self._approvals.verdict(resumed_gate)
            if verdict == "denied":
                raise ApprovalDeniedError(resumed_gate, self._approvals.message(resumed_gate))
            if verdict == "approved":
                approved_gate_stage = resumed_gate
                context["_approval_message"] = self._approvals.message(resumed_gate)
            else:
                # Still pending. Report the pause again rather than proceeding.
                self._paused_stage_index = stage_index
                self._paused_step_in_stage = step_in_stage
                raise ApprovalPaused(resumed_gate, self._pending_prompt(resumed_gate, context))

        for index in range(stage_index, len(self._program.stages)):
            stage = self._program.stages[index]
            self._check_cancelled()
            context["stage"] = {"name": stage.name, "index": index}
            self._emit("stage:start", {"stage": stage.name, "index": index})

            start_step = step_in_stage if index == stage_index else 0

            if (
                stage.approval
                and stage.approval.gates_before_stage
                and start_step == 0
                and stage.name != approved_gate_stage
            ):
                decided = self._approvals.verdict(stage.name)
                if decided == "denied":
                    raise ApprovalDeniedError(stage.name, self._approvals.message(stage.name))
                if decided != "approved":
                    await self._gate(stage, context, stage_index=index, step_in_stage=0)
                    approved_gate_stage = stage.name

            for position in range(start_step, len(stage.steps)):
                step = stage.steps[position]
                self._check_cancelled()
                context["step"] = {"id": step.id, "index": position, "stage": stage.name}
                self._paused_stage_index = index
                self._paused_step_in_stage = position
                try:
                    await self._run_top_level_step(step, context)
                except SkipRemaining:
                    break

            completed_stages.append(stage.name)

            if stage.approval and stage.approval.required and not stage.approval.gates_before_stage:
                await self._gate(stage, context, stage_index=index + 1, step_in_stage=0)
            else:
                context.setdefault("_approval_message", "")

    def _pending_prompt(self, stage_name: str, context: Mapping[str, Any]) -> str:
        for stage in self._program.stages:
            if stage.name == stage_name and stage.approval:
                raw = stage.approval.prompt or f"Approve completion of stage '{stage_name}'?"
                try:
                    return substitute_variables(raw, context)
                except ValueError:
                    return raw
        return f"Approve stage '{stage_name}'?"

    async def _gate(
        self,
        stage: StageSpec,
        context: dict[str, Any],
        *,
        stage_index: int,
        step_in_stage: int,
    ) -> None:
        """Ask the gate. Pause the run when there is no way to answer it.

        An absent approval callback means "no gate may pass"
        (:mod:`amplifier_recipe_runner.ports`), which is a *pause*, not a
        denial: the run is resumable once a verdict is recorded.
        """
        assert stage.approval is not None
        approval = stage.approval
        raw_prompt = approval.prompt or f"Approve completion of stage '{stage.name}'?"
        prompt = substitute_variables(raw_prompt, context)

        verdict = self._approvals.verdict(stage.name)
        if verdict == "denied":
            raise ApprovalDeniedError(stage.name, self._approvals.message(stage.name))
        if verdict == "approved":
            context["_approval_message"] = self._approvals.message(stage.name)
            return

        if self._approval_callback is not None:
            decision = await self._approval_callback(
                _ApprovalRequestShim(run_id=self._run_id, stage=stage.name, prompt=prompt)
            )
            approved = bool(getattr(decision, "approved", False))
            message = getattr(decision, "message", None)
            self._approvals.record(stage.name, approved=approved, message=message)
            if not approved:
                raise ApprovalDeniedError(stage.name, message)
            context["_approval_message"] = message or ""
            return

        self._paused_stage_index = stage_index
        self._paused_step_in_stage = step_in_stage
        self._emit("approval:pending", {"stage": stage.name, "prompt": prompt})
        raise ApprovalPaused(stage.name, prompt)

    # -- one top-level step ------------------------------------------------

    async def _run_top_level_step(self, step: StepSpec, context: dict[str, Any]) -> None:
        if step.condition:
            try:
                passes = evaluate_condition(step.condition, context)
            except ExpressionError as exc:
                raise StepFailedError(step.id, f"Step '{step.id}': condition error: {exc}") from exc
            if not passes:
                skipped = context.get("_skipped_steps", [])
                skipped.append(step.id)
                context["_skipped_steps"] = skipped
                self._emit("step:skipped", {"step_id": step.id, "reason": f"condition false: {step.condition}"})
                return

        self._emit("step:start", {"step_id": step.id, "type": step.type, "agent": step.agent})
        try:
            await self._run_declared_step(step, context)
        except (SkipRemaining, RunCancelled, ApprovalPaused):
            raise
        except Exception as exc:  # noqa: BLE001 - announce, then let it fail the run
            # A step that failed the run says so on the event sink as well as
            # in the result. Absence from `completed_steps` alone cannot tell a
            # failed step from one that was never reached.
            self._emit("step:failed", {"step_id": step.id, "absorbed": False, "error": str(exc)})
            raise

    async def _run_declared_step(self, step: StepSpec, context: dict[str, Any]) -> None:
        if step.is_loop:
            await self._run_loop(step, context)
            # A loop writes its results into the context under `collect:`/
            # `output:`; mirroring them onto the step-output map means a host
            # reading `RunResult.outputs` sees the loop as a step that
            # produced something, rather than as a gap.
            sink = step.collect or step.output
            if sink and sink in context:
                self._outputs[step.id] = context[sink]
            self._completed.append(step.id)
            self._emit("step:complete", {"step_id": step.id})
            return

        result = await self._run_step_body(step, context)
        result = process_step_result(result, step)
        if step.output:
            context[step.output] = result
        self._outputs[step.id] = result
        self._completed.append(step.id)
        self._emit("step:complete", {"step_id": step.id})

    async def _run_step_body(self, step: StepSpec, context: dict[str, Any]) -> Any:
        """Dispatch one non-loop step by type. Shared by loops and sub-steps."""
        if step.type == "recipe":
            return await self._run_sub_recipe(step, context)
        if step.type == "bash":
            outcome = await self._run_bash(step, context)
            if step.output_exit_code:
                context[step.output_exit_code] = str(outcome.exit_code)
            return outcome.stdout
        if step.type not in ("agent", "", None):
            raise UnsupportedStepError(step.id, f"its type {step.type!r} is not one of agent, bash, recipe")
        return await self._run_agent(step, context)

    # -- agent steps -------------------------------------------------------

    async def _run_agent(self, step: StepSpec, context: dict[str, Any]) -> Any:
        if step.body_steps is not None and not step.is_loop:
            # The legacy engine silently ignores a nested body on a non-loop
            # step. Discarding declared work while reporting success is the
            # fabricated success lib Core 8 forbids, so this refuses instead.
            # Documented as a deliberate delta in docs/EXECUTOR_PARITY.md.
            raise UnsupportedStepError(step.id, "it nests further steps under 'steps' without `foreach:` or `while_condition:`")
        if not step.agent or not step.agent.strip():
            raise UnsupportedStepError(step.id, "it declares no `agent`")
        if "{{" in step.agent:
            raise UnsupportedStepError(step.id, f"its agent reference {step.agent!r} is templated")
        if not step.prompt:
            raise UnsupportedStepError(step.id, f"it declares no instruction ({', '.join(INSTRUCTION_KEYS)})")

        self._recursion.increment_steps()

        retry = dict(step.retry or {})
        max_attempts = _int_or(retry.get("max_attempts"), 1)
        backoff = str(retry.get("backoff") or "exponential")
        delay = float(retry.get("initial_delay", 5))
        max_delay = float(retry.get("max_delay", 300))

        attempts = max(1, max_attempts)
        last_error: BaseException | None = None
        for attempt in range(attempts):
            self._check_cancelled()
            # Resolved before the spawn, so an unresolvable timeout template
            # never burns an agent invocation -- and so the timeout message
            # can name the value without re-resolving it.
            timeout = resolve_step_timeout(step, context)
            try:
                instruction = self._render_instruction(step, context)
                return await asyncio.wait_for(self._invoke_agent(step, instruction, context), timeout=timeout)
            except asyncio.TimeoutError:
                last_error = StepFailedError(
                    step.id, f"Step '{step.id}': agent '{step.agent}' timed out after {timeout}s"
                )
            except RunCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc

            if attempt == attempts - 1:
                if step.on_error == "continue":
                    self._emit("step:failed", {"step_id": step.id, "absorbed": True, "error": str(last_error)})
                    return None
                if step.on_error == "skip_remaining":
                    raise SkipRemaining() from last_error
                assert last_error is not None
                raise last_error

            await asyncio.sleep(min(delay, max_delay))
            if backoff == "exponential":
                delay *= 2

        if step.on_error == "fail" and last_error is not None:
            raise last_error
        return None

    def _render_instruction(self, step: StepSpec, context: Mapping[str, Any]) -> str:
        assert step.prompt is not None
        instruction = substitute_variables(step.prompt, context)
        if step.mode:
            instruction = f"MODE: {step.mode}\n\n" + instruction
        if step.parse_json:
            instruction = instruction + _JSON_OUTPUT_RIDER
        return instruction

    # -- bash steps --------------------------------------------------------

    async def _run_bash(self, step: StepSpec, context: dict[str, Any]) -> BashOutcome:
        if not step.command:
            raise UnsupportedStepError(step.id, "it is a bash step with no `command`")

        command = substitute_variables(step.command, context)
        command_size = len(command.encode("utf-8", errors="replace"))
        oversized = command_size > COMMAND_SIZE_WARN_BYTES
        if oversized:
            self._emit(
                "step:warning",
                {"step_id": step.id, "reason": "oversized command", "bytes": command_size},
            )

        if step.cwd:
            cwd = Path(substitute_variables(step.cwd, context))
            if not cwd.is_absolute():
                cwd = self._workspace / cwd
            if not cwd.exists():
                raise StepFailedError(step.id, f"Step '{step.id}': cwd does not exist: {cwd}")
            if not cwd.is_dir():
                raise StepFailedError(step.id, f"Step '{step.id}': cwd is not a directory: {cwd}")
        else:
            cwd = self._workspace

        env = os.environ.copy()
        env["AMPLIFIER_PYTHON"] = _amplifier_python()
        scratch = self._bash_scratch_dir()
        if scratch is not None:
            env["AMPLIFIER_RECIPE_SCRATCH_DIR"] = str(scratch)
        for key, value in (step.env or {}).items():
            env[str(key)] = substitute_variables(str(value), context)

        effective_timeout = resolve_step_timeout(step, context)

        try:
            process = await asyncio.create_subprocess_exec(
                resolve_bash(),
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
            )
        except OSError as exec_error:
            # The command never started: the OS refused the exec itself. There
            # is no exit code to inspect, but this is still THIS STEP failing,
            # so it honours on_error exactly like a non-zero exit.
            error_msg = f"Step '{step.id}': failed to execute command: {exec_error}"
            if step.on_error == "fail":
                raise StepFailedError(step.id, error_msg) from exec_error
            if step.on_error == "skip_remaining":
                raise SkipRemaining() from exec_error
            self._emit("step:failed", {"step_id": step.id, "absorbed": True, "error": error_msg})
            return BashOutcome(stdout="", stderr=error_msg, exit_code=EXEC_FAILURE_EXIT_CODE)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise StepFailedError(
                step.id, f"Step '{step.id}': command timed out after {effective_timeout}s"
            ) from None

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = process.returncode or 0

        if exit_code != 0:
            error_msg = f"Step '{step.id}': command failed with exit code {exit_code}"
            if stderr.strip():
                error_msg += f"\nstderr: {stderr.strip()}"
            if step.on_error == "fail":
                raise StepFailedError(step.id, error_msg)
            if step.on_error == "skip_remaining":
                raise SkipRemaining()
            self._emit("step:failed", {"step_id": step.id, "absorbed": True, "error": error_msg})

        return BashOutcome(stdout=stdout, stderr=stderr, exit_code=exit_code)

    def _bash_scratch_dir(self) -> Path | None:
        """Somewhere run-scoped for a step to spill a payload too big for argv."""
        if self._scratch_dir is None:
            return None
        try:
            self._scratch_dir.mkdir(parents=True, exist_ok=True)
            return self._scratch_dir
        except OSError:
            return None

    # -- sub-recipes -------------------------------------------------------

    async def _run_sub_recipe(self, step: StepSpec, context: dict[str, Any]) -> Any:
        if not step.recipe:
            raise UnsupportedStepError(step.id, "it is a recipe step with no `recipe` path")
        if self._sub_recipe_runner is None:
            raise UnsupportedStepError(
                step.id,
                "it is a `type: recipe` step but no sub-recipe runner was supplied",
                remedy="Run the recipe through `amplifier_recipe_runner.execution.run`, which supplies one.",
            )

        rendered = substitute_variables(step.recipe, context)
        if rendered.startswith("@"):
            raise UnsupportedStepError(
                step.id,
                f"its sub-recipe path {rendered!r} is an @mention, which needs a host mention resolver",
                remedy="Use a path relative to the parent recipe, which resolves without a host.",
            )

        base_dir = self._recipe_path.parent if self._recipe_path else self._workspace
        sub_path = (base_dir / rendered).resolve()
        if not sub_path.exists():
            raise StepFailedError(step.id, f"Sub-recipe not found: {sub_path}")

        sub_context: dict[str, Any] = {}
        for key, value in (step.step_context or {}).items():
            sub_context[str(key)] = substitute_recursive(value, context)

        child = self._recursion.enter_recipe(rendered, step.recursion)
        child.check_depth()

        delta = await self._sub_recipe_runner(sub_path, sub_context, step, child)
        self._recursion.total_steps = child.total_steps
        return dict(delta)

    # -- loops -------------------------------------------------------------

    async def _run_loop(self, step: StepSpec, context: dict[str, Any]) -> None:
        if step.while_condition:
            await self._run_while(step, context)
            return
        await self._run_foreach(step, context)

    async def _run_foreach(self, step: StepSpec, context: dict[str, Any]) -> None:
        assert step.foreach is not None
        items = resolve_foreach_variable(step.foreach, context)

        if not isinstance(items, list):
            raise StepFailedError(
                step.id, f"Step '{step.id}': foreach variable must be a list, got {type(items).__name__}"
            )

        if not items:
            # Empty list: skip the body, but still define the collect variable
            # so a downstream `{{results}}` is not an undefined-variable error.
            skipped = context.get("_skipped_steps", [])
            skipped.append(step.id)
            context["_skipped_steps"] = skipped
            if step.collect:
                context[step.collect] = []
            self._emit("step:skipped", {"step_id": step.id, "reason": f"foreach list '{step.foreach}' is empty"})
            return

        if len(items) > step.max_iterations:
            raise StepFailedError(
                step.id,
                f"Step '{step.id}': foreach exceeds max_iterations ({len(items)} > {step.max_iterations})",
            )

        loop_var = step.as_var or "item"

        if step.parallel:
            results = await self._foreach_parallel(step, context, items, loop_var)
        else:
            results = await self._foreach_sequential(step, context, items, loop_var)

        if step.collect:
            context[step.collect] = results
        elif step.output and results:
            context[step.output] = results[-1]

    async def _foreach_sequential(
        self, step: StepSpec, context: dict[str, Any], items: list[Any], loop_var: str
    ) -> list[Any]:
        results: list[Any] = []
        for idx, item in enumerate(items):
            self._check_cancelled()
            context[loop_var] = item
            try:
                if step.body_steps:
                    results.append(await self._run_sub_steps(step.body_steps, context, parent_step_id=step.id))
                else:
                    result = await self._run_step_body(step, context)
                    results.append(process_step_result(result, step))
            except (SkipRemaining, RunCancelled, ApprovalPaused):
                raise
            except Exception as exc:  # noqa: BLE001
                if step.on_error == "continue":
                    self._emit("iteration:failed", {"step_id": step.id, "index": idx, "error": str(exc)})
                    results.append(None)
                elif step.on_error == "skip_remaining":
                    raise SkipRemaining() from exc
                else:
                    raise StepFailedError(step.id, f"Step '{step.id}' iteration {idx} failed: {exc}") from exc
            finally:
                context.pop(loop_var, None)
        return results

    async def _foreach_parallel(
        self, step: StepSpec, context: dict[str, Any], items: list[Any], loop_var: str
    ) -> list[Any]:
        self._check_cancelled()

        if step.type == "agent":
            projected = self._recursion.total_steps + len(items)
            if projected > self._recursion.max_total_steps:
                raise StepFailedError(
                    step.id,
                    f"Parallel loop would exceed max_total_steps "
                    f"({self._recursion.total_steps} + {len(items)} > {self._recursion.max_total_steps})",
                )

        max_concurrent = step.parallel if isinstance(step.parallel, int) and step.parallel is not True else None
        semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent else None
        group_id = str(uuid.uuid4())

        async def iteration(idx: int, item: Any) -> Any:
            # Each iteration gets its own context copy, so a sibling cannot see
            # this one's loop variable and results stay input-ordered.
            iter_context = {**context, loop_var: item, "_parallel_group_id": group_id}
            try:
                if step.body_steps:
                    return await self._run_sub_steps(step.body_steps, iter_context, parent_step_id=step.id)
                result = await self._run_step_body(step, iter_context)
                return process_step_result(result, step)
            except (SkipRemaining, RunCancelled, ApprovalPaused):
                raise
            except Exception as exc:  # noqa: BLE001
                raise StepFailedError(step.id, f"Step '{step.id}' iteration {idx} failed: {exc}") from exc

        async def bounded(idx: int, item: Any) -> Any:
            if semaphore:
                async with semaphore:
                    return await iteration(idx, item)
            return await iteration(idx, item)

        # return_exceptions=True: without it, one failed iteration raises
        # immediately but does NOT cancel the rest, which then run as orphans.
        raw = await asyncio.gather(*(bounded(idx, item) for idx, item in enumerate(items)), return_exceptions=True)

        results: list[Any] = []
        failures: list[tuple[int, BaseException]] = []
        for idx, value in enumerate(raw):
            if isinstance(value, BaseException):
                if isinstance(value, (RunCancelled, ApprovalPaused)):
                    raise value
                self._emit("iteration:failed", {"step_id": step.id, "index": idx, "error": str(value)})
                failures.append((idx, value))
                results.append(None)
            else:
                results.append(value)

        if failures and step.on_error != "continue":
            if step.on_error == "skip_remaining":
                raise SkipRemaining()
            summary = "; ".join(f"iteration {i}: {str(exc)[:100]}" for i, exc in failures)
            raise StepFailedError(
                step.id, f"Step '{step.id}': {len(failures)}/{len(results)} iterations failed: {summary}"
            )

        return results

    async def _run_while(self, step: StepSpec, context: dict[str, Any]) -> None:
        assert step.while_condition is not None
        results: list[Any] = []
        iteration = 0

        try:
            while True:
                if iteration >= step.max_while_iterations:
                    break
                self._check_cancelled()

                resolved_condition = substitute_variables(step.while_condition, context)
                if not evaluate_condition(resolved_condition, context):
                    break

                context["_loop_index"] = iteration
                context["_loop_iteration"] = iteration + 1
                self._emit(
                    "loop:iteration",
                    {"step_id": step.id, "iteration": iteration + 1, "max_iterations": step.max_while_iterations},
                )

                try:
                    if step.body_steps:
                        results.append(await self._run_sub_steps(step.body_steps, context, parent_step_id=step.id))
                    else:
                        result = process_step_result(await self._run_step_body(step, context), step)
                        results.append(result)
                        if step.output:
                            context[step.output] = result
                except (SkipRemaining, RunCancelled, ApprovalPaused):
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise StepFailedError(step.id, f"Step '{step.id}' iteration {iteration} failed: {exc}") from exc

                # After the body, before break_when: a convergence loop's exit
                # test reads what this iteration just wrote.
                for key, value in (step.update_context or {}).items():
                    context[str(key)] = substitute_variables(str(value), context)

                if step.break_when:
                    try:
                        if evaluate_condition(substitute_variables(step.break_when, context), context):
                            break
                    except ExpressionError as exc:
                        self._emit("step:warning", {"step_id": step.id, "reason": f"break_when: {exc}"})

                iteration += 1
        finally:
            context.pop("_loop_index", None)
            context.pop("_loop_iteration", None)

        self._emit("loop:complete", {"step_id": step.id, "iterations": iteration, "results": len(results)})

        if step.collect:
            context[step.collect] = results
        elif step.output and results:
            context[step.output] = results[-1]

    async def _run_sub_steps(
        self, body: Sequence[Mapping[str, Any]], context: dict[str, Any], *, parent_step_id: str
    ) -> Any:
        """Run a compound loop body in sequence, returning the last result.

        Sub-steps that are themselves loops route back through the loop
        executor so nesting works to arbitrary depth.
        """
        last_result: Any = None
        for index, data in enumerate(body):
            sub_step = parse_step(data, index=index)

            if sub_step.condition:
                # Sub-steps pre-substitute before evaluating; the top level
                # does not. That asymmetry is the legacy engine's, reproduced
                # here rather than tidied away -- see docs/EXECUTOR_PARITY.md.
                resolved = substitute_variables(sub_step.condition, context)
                if not evaluate_condition(resolved, context):
                    skipped = context.get("_skipped_steps", [])
                    skipped.append(sub_step.id)
                    context["_skipped_steps"] = skipped
                    self._emit("step:skipped", {"step_id": sub_step.id, "reason": f"condition false: {resolved}"})
                    continue

            if sub_step.is_loop:
                await self._run_loop(sub_step, context)
                if sub_step.output:
                    last_result = context.get(sub_step.output)
                elif sub_step.collect:
                    last_result = context.get(sub_step.collect)
            else:
                result = process_step_result(await self._run_step_body(sub_step, context), sub_step)
                if sub_step.output:
                    context[sub_step.output] = result
                last_result = result

        return last_result

    # -- cancellation ------------------------------------------------------

    def _check_cancelled(self) -> None:
        token = self._cancellation
        if token is None:
            return
        if bool(getattr(token, "cancelled", False)):
            raise RunCancelled()


@dataclass(frozen=True, slots=True)
class _ApprovalRequestShim:
    """The shape :class:`~amplifier_recipe_runner.ports.ApprovalCallback` expects.

    Built here rather than imported so the engine keeps no import edge to the
    ports module; the callback only reads attributes.
    """

    run_id: str
    stage: str
    prompt: str
    details: Mapping[str, Any] = field(default_factory=dict)


_JSON_OUTPUT_RIDER: Final[str] = """

---

**CRITICAL: JSON OUTPUT REQUIRED**

Your response MUST end with valid JSON (object or array as required by the prompt above). The recipe system will parse your final JSON output.

Requirements:
1. Your response MUST contain a JSON code block or raw JSON
2. The JSON must be valid (proper quotes, no trailing commas, etc.)
3. If you include explanation, put the JSON block LAST in your response
4. Use ```json fences or return raw JSON - both work

Example valid endings:
```json
{"key": "value", "count": 5}
```

Or a JSON array:
```json
[{"id": 1, "name": "first"}, {"id": 2, "name": "second"}]
```

Or raw JSON at the end:
{"key": "value", "count": 5}

DO NOT return the JSON as a string or with escape characters. Return actual JSON structure.
"""
