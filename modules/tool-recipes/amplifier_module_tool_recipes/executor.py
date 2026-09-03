"""Recipe execution engine."""

import asyncio
import datetime
import fnmatch
import gc
import json
import logging
import os
import re
import shutil
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from .expression_evaluator import ExpressionError
from .expression_evaluator import evaluate_condition
from amplifier_foundation import ProviderPreference
from amplifier_foundation import resolve_model_pattern
from .models import BackoffConfig
from .models import OrchestratorConfig
from .models import RateLimitingConfig
from .models import Recipe
from .models import RecursionConfig
from .models import Step
from .session import ApprovalStatus
from .session import SessionManager

# Relative path from a Git for Windows install root to its bash executable.
_GIT_BASH_RELATIVE = r"\bin\bash.exe"

# Environment variables that may point at a Program Files root on Windows.
# ProgramW6432 is the 64-bit root even from a 32-bit process; the other two
# vary by process bitness. We probe all of them plus the per-user install root.
_WINDOWS_PROGRAM_ROOT_VARS = (
    "ProgramW6432",
    "ProgramFiles",
    "ProgramFiles(x86)",
)


def _is_wsl_bash(path: str) -> bool:
    """True if ``path`` is the WSL launcher rather than a real Windows bash.

    ``C:\\Windows\\System32\\bash.exe`` (and its Sysnative alias) is not a bash
    at all -- it is a shim that starts a Linux VM. Recipe bash steps cannot use
    it; see ``_resolve_bash`` for why.
    """
    lowered = path.lower().replace("/", "\\")
    return "\\system32\\" in lowered or "\\sysnative\\" in lowered


def _find_git_bash() -> str | None:
    """Locate a Git for Windows bash by probing install locations on disk.

    Deliberately does *not* use ``shutil.which``: on a default Windows install
    the first ``bash`` on PATH is the WSL launcher in System32, so PATH lookup
    finds precisely the wrong thing. We probe the known install roots directly,
    and only fall back to PATH for non-standard installs.
    """
    candidates: list[str] = []

    for var in _WINDOWS_PROGRAM_ROOT_VARS:
        root = os.environ.get(var)
        if root:
            candidates.append(root + r"\Git" + _GIT_BASH_RELATIVE)

    # Per-user install (winget / "install for me only")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(local_app_data + r"\Programs\Git" + _GIT_BASH_RELATIVE)

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    # Non-standard install location: derive it from git.exe, which users with a
    # working Git install reliably have on PATH. <root>\cmd\git.exe -> <root>\bin\bash.exe
    git_exe = shutil.which("git")
    if git_exe:
        git_root = os.path.dirname(os.path.dirname(git_exe))
        derived = git_root + _GIT_BASH_RELATIVE
        if os.path.isfile(derived):
            return derived

    # Last resort: a bash on PATH that is not the WSL shim.
    path_bash = shutil.which("bash")
    if path_bash and not _is_wsl_bash(path_bash):
        return path_bash

    return None


def _resolve_bash() -> str:
    """Resolve a bash executable for recipe ``type: bash`` steps.

    Recipe bash steps require real bash (pipefail, arrays, brace expansion,
    ``&>`` redirects). On POSIX that is ``/bin/bash``, unchanged.

    On Windows there is no ``/bin/bash``, and the two available bash-like
    programs are *not* interchangeable here:

    - **Git for Windows bash** is a normal Windows process. It inherits the
      Windows environment, so the ``AMPLIFIER_PYTHON`` we inject (a Windows
      path to the Amplifier venv interpreter) resolves and runs. This works.
    - **WSL** is effectively a separate machine with its own filesystem and an
      environment firewall. A Windows interpreter path is meaningless inside
      it and the variable does not cross without explicit ``WSLENV`` plumbing.
      Recipes that rely on ``${AMPLIFIER_PYTHON:-python3}`` would silently fall
      back to a *different* Python that lacks the recipe modules, and fail
      later with a confusing ImportError instead of a clear message here.

    So WSL is not a lower-priority option for recipe steps -- it is never
    correct. We reject it explicitly rather than let it produce a puzzling
    downstream failure.
    """
    if os.name != "nt":
        return "/bin/bash"

    git_bash = _find_git_bash()
    if git_bash:
        return git_bash

    path_bash = shutil.which("bash")
    if path_bash and _is_wsl_bash(path_bash):
        raise ValueError(
            "Recipe bash steps require Git for Windows bash, but the only bash "
            f"found is the WSL launcher ({path_bash}).\n"
            "WSL cannot be used here: recipe steps run the Amplifier interpreter "
            f"at {sys.executable}, which is a Windows path that does not exist "
            "inside WSL.\n"
            "Fix: install Git for Windows (https://git-scm.com/download/win), "
            "which provides a compatible bash."
        )

    raise ValueError(
        "Recipe bash steps require a bash executable, but none was found.\n"
        "Fix: install Git for Windows (https://git-scm.com/download/win), "
        "which provides bash at "
        r"C:\Program Files\Git\bin\bash.exe."
    )


def _resolve_amplifier_python() -> str:
    """Return ``sys.executable`` in a form the resolved bash can execute.

    On Windows ``sys.executable`` uses backslashes (``C:\\...\\python.exe``).
    Git Bash handles forward slashes reliably in every quoting context, while
    backslashes are an escape character in bash and survive only inside double
    quotes. Recipes are not required to quote defensively, so normalise here.
    """
    if os.name != "nt":
        return sys.executable
    return sys.executable.replace("\\", "/")


# Keys injected by execute_recipe() itself into every sub-recipe context.
# These are never meaningful "outputs" from a sub-recipe — they are infrastructure
# metadata that the parent recipe already has (or doesn't need).
_RECIPE_INTERNAL_KEYS: frozenset[str] = frozenset(
    {"recipe", "session", "step", "stage", "_skipped_steps"}
)

# Checkpoint trimming: values serialised larger than this threshold (bytes) are
# replaced with a human-readable placeholder in the on-disk checkpoint file.
# The *live* context is never modified — only the serialised copy is trimmed.
# 100 KB per value is generous for typical recipe outputs.
_CHECKPOINT_TRIM_THRESHOLD_BYTES: int = 100_000

# Foreach progress size warning threshold (bytes).  When the foreach_progress
# dict (including accumulated collected_results) exceeds this size, a WARNING
# is logged to alert users to O(N²) write amplification.  No data is dropped —
# this is a warn-only signal.  10 MB is chosen as a generous ceiling; a typical
# foreach with 100 iterations and 10 KB results writes only ~50 MB total, but
# results that push past 10 MB per checkpoint write indicate a recipe design
# that should be revisited (e.g. use 'output' instead of 'collect').
_FOREACH_PROGRESS_WARN_BYTES: int = 10_000_000  # 10 MB

# Deduplication set for depends_on advisory warnings.
# Keyed by recipe_name so each recipe emits at most one warning per process
# lifetime regardless of how many steps declare depends_on or how many times
# the recipe is executed or resumed.
_warned_depends_on_recipes: set[str] = set()


@dataclass
class BashResult:
    """Result of a bash command execution."""

    stdout: str
    stderr: str
    exit_code: int


class SkipRemainingError(Exception):
    """Raised when step fails with on_error='skip_remaining'."""

    pass


class ApprovalGatePausedError(Exception):
    """Raised when execution pauses at an approval gate.

    This is not a failure - it signals that the recipe has paused
    waiting for human approval before continuing to the next stage.
    Callers should catch this and handle it appropriately (e.g., notify user).
    """

    def __init__(
        self,
        session_id: str,
        stage_name: str,
        approval_prompt: str,
        resume_session_id: str | None = None,
    ):
        self.session_id = session_id
        self.stage_name = stage_name
        self.approval_prompt = approval_prompt
        self.resume_session_id = resume_session_id
        super().__init__(f"Execution paused at stage '{stage_name}' awaiting approval")


class CancellationRequestedError(Exception):
    """Raised when cancellation is requested and execution should stop.

    This is similar to ApprovalGatePausedError - it signals that execution
    has been interrupted, but in this case due to a cancellation request.
    The recipe can be resumed later from the last checkpoint.
    """

    def __init__(
        self,
        session_id: str,
        is_immediate: bool,
        current_step: str | None = None,
        message: str | None = None,
    ):
        self.session_id = session_id
        self.is_immediate = is_immediate
        self.current_step = current_step
        level = "immediate" if is_immediate else "graceful"
        step_info = f" at step '{current_step}'" if current_step else ""
        self.message = (
            message or f"Recipe {session_id} cancellation ({level}){step_info}"
        )
        super().__init__(self.message)


@dataclass
class RecursionState:
    """Track recursion across nested recipe executions."""

    current_depth: int = 0
    total_steps: int = 0
    max_depth: int = 5
    max_total_steps: int = 100
    recipe_stack: list[str] = field(default_factory=list)

    def check_depth(self, recipe_name: str) -> None:
        """Raise if depth limit exceeded."""
        if self.current_depth >= self.max_depth:
            raise ValueError(
                f"Recipe recursion depth {self.current_depth} exceeds limit {self.max_depth}. "
                f"Stack: {' -> '.join(self.recipe_stack)}"
            )

    def check_total_steps(self) -> None:
        """Raise if total steps limit exceeded."""
        if self.total_steps >= self.max_total_steps:
            raise ValueError(
                f"Total steps {self.total_steps} exceeds limit {self.max_total_steps}"
            )

    def increment_steps(self) -> None:
        """Increment total steps counter and check limit."""
        self.total_steps += 1
        self.check_total_steps()

    def enter_recipe(
        self, recipe_name: str, override_config: RecursionConfig | None = None
    ) -> "RecursionState":
        """
        Create child state for sub-recipe.

        Args:
            recipe_name: Name of recipe being entered
            override_config: Optional per-step recursion config override
        """
        # Use override config if provided, otherwise inherit current limits
        max_depth = override_config.max_depth if override_config else self.max_depth
        max_total_steps = (
            override_config.max_total_steps if override_config else self.max_total_steps
        )

        return RecursionState(
            current_depth=self.current_depth + 1,
            total_steps=self.total_steps,
            max_depth=max_depth,
            max_total_steps=max_total_steps,
            recipe_stack=[*self.recipe_stack, recipe_name],
        )


@dataclass
class BackoffState:
    """Tracks current backoff state for rate limiting."""

    config: BackoffConfig
    current_delay_ms: int = 0
    consecutive_successes: int = 0

    def increase(self) -> None:
        """Increase backoff delay after rate limit hit."""
        if not self.config.enabled:
            return
        if self.current_delay_ms == 0:
            self.current_delay_ms = self.config.initial_delay_ms
        else:
            self.current_delay_ms = min(
                int(self.current_delay_ms * self.config.multiplier),
                self.config.max_delay_ms,
            )
        self.consecutive_successes = 0

    def record_success(self) -> None:
        """Record successful call, potentially reset backoff."""
        if not self.config.enabled:
            return
        self.consecutive_successes += 1
        if self.consecutive_successes >= self.config.reset_after_success:
            self.current_delay_ms = 0
            self.consecutive_successes = 0


class RateLimiter:
    """Global rate limiter shared across recipe tree.

    Controls concurrency and pacing of LLM calls to prevent overwhelming
    provider APIs. Sub-recipes inherit the parent's rate limiter.
    """

    def __init__(self, config: RateLimitingConfig):
        self.config = config
        # Semaphore for concurrency control (high value if None/unlimited)
        max_concurrent = config.max_concurrent_llm or 999999
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.min_delay_ms = config.min_delay_ms
        self.backoff = BackoffState(config=config.backoff)
        self._last_completion: float = 0.0
        self._lock = asyncio.Lock()
        # Stats for observability
        self.stats = {
            "total_acquisitions": 0,
            "total_wait_time_ms": 0,
            "rate_limit_hits": 0,
        }

    async def acquire(self) -> None:
        """Acquire a slot before making LLM call."""
        start = asyncio.get_event_loop().time()
        await self.semaphore.acquire()
        await self._apply_pacing()
        await self._apply_backoff()
        elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000
        self.stats["total_acquisitions"] += 1
        self.stats["total_wait_time_ms"] += int(elapsed_ms)

    def release(self) -> None:
        """Release slot after LLM call completes."""
        self._last_completion = asyncio.get_event_loop().time()
        self.semaphore.release()

    def record_rate_limit(self) -> None:
        """Called when 429 received - increase backoff."""
        self.stats["rate_limit_hits"] += 1
        self.backoff.increase()

    def record_success(self) -> None:
        """Called on success - potentially decrease backoff."""
        self.backoff.record_success()

    async def _apply_pacing(self) -> None:
        """Ensure min_delay_ms between completions."""
        if self.min_delay_ms <= 0:
            return
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed_ms = (now - self._last_completion) * 1000
            if elapsed_ms < self.min_delay_ms:
                await asyncio.sleep((self.min_delay_ms - elapsed_ms) / 1000)

    async def _apply_backoff(self) -> None:
        """Apply current backoff delay if any."""
        delay = self.backoff.current_delay_ms
        if delay > 0:
            await asyncio.sleep(delay / 1000)


def _warn_depends_on_unenforced(recipe: "Recipe") -> None:
    """Log a single WARNING per recipe when any step declares depends_on.

    ``Step.depends_on`` is validated and documented but the executor runs
    steps in declaration order — it does **not** reorder or gate steps based
    on the ``depends_on`` list.  Callers relying on it for ordering would
    experience silent mis-ordering.

    One warning is emitted per recipe per process lifetime (controlled by the
    module-level ``_warned_depends_on_recipes`` set).  The message names the
    recipe and the count of steps that declare depends_on, giving enough
    context for debugging without flooding logs on recipes with many steps.
    Both flat steps and steps nested inside stages are checked.

    Args:
        recipe: The Recipe object about to be executed.
    """
    # Dedup: warn at most once per recipe per process lifetime.
    if recipe.name in _warned_depends_on_recipes:
        return

    # Collect all steps (flat recipes have recipe.steps; staged recipes put
    # steps inside recipe.stages[n].steps — check both).
    all_steps = list(recipe.steps or [])
    for stage in recipe.stages or []:
        all_steps.extend(stage.steps or [])

    dep_count = sum(1 for step in all_steps if step.depends_on)
    if dep_count == 0:
        return

    _warned_depends_on_recipes.add(recipe.name)
    logger.warning(
        "Recipe '%s' has %d step(s) that declare depends_on, but the "
        "recipe engine does not currently enforce dependency ordering. "
        "Steps execute in declaration order. This may become enforced "
        "in a future version.",
        recipe.name,
        dep_count,
    )


# ---------------------------------------------------------------------------
# Provider-instance pinning for resolved model roles
# ---------------------------------------------------------------------------
#
# A routing matrix declares its candidates by provider MODULE ("anthropic",
# "openai", ...), because a matrix cannot know what any given host named its
# instances.  A host, however, mounts provider *instances* -- `opus`, `sonnet`,
# `haiku`, `fable` may all be `provider-anthropic` -- and the two consumers of
# a spawn's `provider_preferences` disagree about what a bare module name means:
#
#   * `amplifier_foundation.spawn_utils._build_provider_lookup` (spawn_utils.py
#     :649-674) indexes module ids, short names and instance ids into one flat
#     dict by enumeration, so a module name resolves to the LAST declared
#     instance of that module -- an arbitrary pick, and the reason a
#     `reasoning` role landed on `fable` in the field.
#   * the child session's own routing re-assert (hooks-routing `role_pin`)
#     matches preferences against the MOUNTED providers, which are keyed by
#     instance id only.  There, "anthropic" matches nothing at all -- while a
#     module name that happens to equal an instance id ("gemini") matches
#     literally, promoting an unrelated provider to priority 0.
#
# So the engine resolves the ambiguity here, once, before spawning: every
# preference is rewritten to name the instance this session would actually
# resolve for it.  A preference no installed instance can serve is dropped
# rather than emitted as a name something else might answer to, and a chain
# that ends up empty becomes `None` -- inherit the parent, exactly as a
# `delegate` of the same agent does.


def _provider_name_variants(name: str) -> set[str]:
    """Every spelling the host's provider lookup indexes a module under.

    Mirrors ``spawn_utils._build_provider_lookup``: the module id, that id with
    a leading ``provider-`` stripped, and it with ``provider-`` prepended.
    """
    short = name.replace("provider-", "")
    return {name, short, f"provider-{short}"}


def _provider_instance_id(entry: dict[str, Any]) -> str:
    """The instance id a mounted provider entry answers to, or ``""``."""
    for key in ("instance_id", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _provider_entry_config(entry: dict[str, Any]) -> dict[str, Any]:
    config = entry.get("config")
    return config if isinstance(config, dict) else {}


def _provider_priority(entry: dict[str, Any]) -> float:
    """A provider's declared priority; unranked sorts last. Lower wins."""
    priority = _provider_entry_config(entry).get("priority")
    if isinstance(priority, bool) or not isinstance(priority, (int, float)):
        return float("inf")
    return float(priority)


def _pin_preference_to_instance(
    pref: Any, entries: list[dict[str, Any]], instance_ids: set[str]
) -> Any | None:
    """One preference, rewritten to name a provider instance. ``None`` = drop.

    Selection, in order:

    1. A preference already naming a mounted instance id is returned untouched
       -- an explicit pin is the caller's decision, not ours to re-pick.
    2. Instances of the named module whose configured ``default_model`` matches
       the preference's model pattern are preferred, and the winner's own
       default model replaces the pattern (a concrete name both consumers can
       apply without re-globbing). A preference naming no model at all ("use
       the provider's default") likewise gets the chosen instance's default
       model, rather than an empty string the spawner would write over that
       provider's configured model.
    3. Failing that, every instance of the module is a candidate and the
       preference's model pattern rides through unchanged, for the host to
       resolve against that instance's model list.
    4. Among candidates the lowest priority number wins -- the instance this
       session resolves for that module anyway -- ties broken by declaration
       order.
    5. No instance of the module is mounted: dropped.  A name this host cannot
       serve must not be handed on, because downstream it can still collide
       with an unrelated instance that happens to share the spelling.
    """
    provider = getattr(pref, "provider", "") or ""
    model = getattr(pref, "model", "") or ""
    config = getattr(pref, "config", None)

    if provider in instance_ids:
        return pref

    variants = _provider_name_variants(provider)
    candidates = [
        entry
        for entry in entries
        if variants & _provider_name_variants(str(entry.get("module", "")))
    ]
    if not candidates:
        logger.warning(
            "provider preference %r names a provider this session has not "
            "mounted (mounted: %s) - dropping it rather than passing a name "
            "another provider could answer to",
            provider,
            ", ".join(sorted(instance_ids)) or "none",
        )
        return None

    matching = [
        entry
        for entry in candidates
        if model
        and fnmatch.fnmatchcase(
            str(_provider_entry_config(entry).get("default_model", "")), model
        )
    ]
    pool = matching or candidates
    chosen = min(pool, key=_provider_priority)
    chosen_id = _provider_instance_id(chosen)
    if not chosen_id:
        # Nothing better to say than what the caller said: this host's provider
        # entries carry no instance id to pin to.
        return pref

    chosen_default = str(_provider_entry_config(chosen).get("default_model", ""))
    chosen_model = chosen_default or model if (matching or not model) else model
    if chosen_id != provider:
        logger.debug(
            "provider preference %r/%r pinned to instance %r (model %r): the "
            "mounted instance of that module this session resolves first",
            provider,
            model,
            chosen_id,
            chosen_model,
        )
    return ProviderPreference(
        provider=chosen_id,
        model=chosen_model,
        config=dict(config) if isinstance(config, dict) else {},
    )


def pin_preferences_to_instances(
    preferences: list[Any] | None, providers: Any
) -> list[Any] | None:
    """Rewrite a preference chain to name mounted provider INSTANCES.

    Returns ``None`` when nothing survives -- the child then inherits the
    parent's provider ordering, which is what a ``delegate`` of the same agent
    already does, rather than being pinned to a provider nobody chose.

    ``providers`` that is not a non-empty list of mappings means this host
    exposes no instance information, so there is nothing to translate against
    and the chain is returned untouched.
    """
    if not preferences:
        return preferences

    entries = (
        [entry for entry in providers if isinstance(entry, dict)]
        if isinstance(providers, list)
        else []
    )
    if not entries:
        return preferences

    instance_ids = {_provider_instance_id(entry) for entry in entries} - {""}

    pinned: list[Any] = []
    for pref in preferences:
        resolved = _pin_preference_to_instance(pref, entries, instance_ids)
        if resolved is not None:
            pinned.append(resolved)

    if not pinned:
        logger.warning(
            "no provider preference could be pinned to a mounted provider "
            "instance; spawning with the parent session's provider ordering "
            "instead of an unrelated provider"
        )
        return None
    return pinned


# ---------------------------------------------------------------------------
# The spawned agent's OWN overlay must say the same thing
# ---------------------------------------------------------------------------
#
# Pinning the `provider_preferences` ARGUMENT is only half a spawn.  A spawn
# also carries the agent's overlay, and an agent definition file may declare
# `provider_preferences` of its own (``foundation:zen-architect`` does).  The
# host merges that overlay straight into the child's session config
# (``session_spawner.spawn_sub_session`` -> ``agent_config.merge_configs``,
# where a list value simply overrides), and NOTHING downstream rewrites it:
# ``apply_provider_preferences_with_resolution`` edits only the mount plan's
# ``providers``.  So the child starts up declaring the *untranslated* chain.
#
# That declaration is live, not decorative.  Two consumers read it back:
#
#   * the child's own routing re-assert, ``hooks-routing``'s
#     ``role_pin._declared_pins`` (role_pin.py:266), reads exactly this
#     session-level key at ``session:start`` and re-pins priority from it --
#     against MOUNTED providers, keyed by instance id.  Measured on the fixed
#     engine (child session ...a0a049acf77d43c7): the spawn argument correctly
#     promoted `opus` to priority 0, then the child read its own config's
#     "anthropic"/"openai" (matching no instance), hit "gemini" (an instance id
#     that happens to equal a module's short name), promoted gemini to 0 and
#     demoted opus to 1.  The agent ran on gemini-3.1-flash-image-preview and
#     took the same 65,536-token 400 as before the pin existed.
#   * ``resume_sub_session`` rebuilds the promotion from the persisted agent
#     overlay, then the persisted config (session_spawner.py:1500-1508), so an
#     untranslated overlay also mis-promotes every resumed leg.
#
# Hence: whatever chain the engine emits as the argument, the overlay it emits
# alongside declares the same chain -- by instance id, so both matchers agree.
# When the engine emits no chain at all, the overlay's own unservable one is
# removed rather than left behind, because "inherit the parent's ordering" and
# "let the child re-pin itself onto a name collision" are not the same thing.
#
# `model_role` is deliberately still forwarded.  It is what the declaration
# MEANS, and on this host it is inert at session level: `role_pin` reads only
# `provider_preferences`, the routing hook resolves `model_role` only for
# entries under `config["agents"]` (children this session may spawn, not the
# session itself), and the spawner never re-derives preferences from it
# (`model_role` reaches `session_spawner` only as an explicit resume argument).
# Dropping it would delete the provenance of a pin without changing a byte of
# behaviour -- see tests/test_provider_instance_pinning.py, which asserts that
# inertness rather than assuming it.


def _preference_as_dict(pref: Any) -> dict[str, Any] | None:
    """One preference in the dict form a config/overlay carries."""
    to_dict = getattr(pref, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return value if isinstance(value, dict) else None
    if isinstance(pref, Mapping):
        return dict(pref)
    provider = getattr(pref, "provider", None)
    if not isinstance(provider, str) or not provider:
        return None
    entry: dict[str, Any] = {"provider": provider, "model": getattr(pref, "model", "")}
    config = getattr(pref, "config", None)
    if isinstance(config, dict) and config:
        entry["config"] = dict(config)
    return entry


def align_overlay_preferences(
    agent_configs: Any, agent_name: str, preferences: list[Any] | None
) -> Any:
    """Make ``agent_name``'s overlay declare exactly the chain being promoted.

    Returns the mapping to hand ``session.spawn``. The caller's mapping is
    never mutated: a changed overlay is written into a fresh copy, so a host
    agent map (or a catalog reused by the next step) cannot be edited from
    here.

    When ``preferences`` is empty/``None`` the overlay's own
    ``provider_preferences`` is *removed*, so the child inherits the parent's
    provider ordering rather than re-pinning itself from a chain this session
    already found it could not serve.

    When nothing needs changing -- no such agent, or the overlay already says
    precisely this -- the caller's own object is returned unchanged, so a
    recipe with no provider intent produces a byte-identical spawn.
    """
    if not isinstance(agent_configs, Mapping):
        return agent_configs
    overlay = agent_configs.get(agent_name)
    if not isinstance(overlay, Mapping):
        return agent_configs

    declared: list[dict[str, Any]] | None = None
    if preferences:
        entries = [_preference_as_dict(pref) for pref in preferences]
        declared = [entry for entry in entries if entry is not None] or None

    current = overlay.get("provider_preferences")
    if declared is None:
        if "provider_preferences" not in overlay:
            return agent_configs
        logger.debug(
            "agent %r declares provider_preferences this session cannot pin to "
            "a mounted instance; dropping them from its spawn overlay so the "
            "child inherits the parent's provider ordering",
            agent_name,
        )
    elif current == declared:
        return agent_configs

    aligned_overlay = dict(overlay)
    if declared is None:
        aligned_overlay.pop("provider_preferences", None)
    else:
        aligned_overlay["provider_preferences"] = declared
        logger.debug(
            "agent %r spawn overlay now declares the pinned chain %s (was %s)",
            agent_name,
            [entry["provider"] for entry in declared],
            [
                entry.get("provider")
                for entry in (current if isinstance(current, list) else [])
            ]
            or "nothing",
        )

    aligned = dict(agent_configs)
    aligned[agent_name] = aligned_overlay
    return aligned


class RecipeExecutor:
    """Executes recipe workflows with checkpointing and resumption."""

    def __init__(self, coordinator: Any, session_manager: SessionManager):
        """
        Initialize executor.

        Args:
            coordinator: Amplifier coordinator for agent spawning
            session_manager: Session persistence manager
        """
        self.coordinator = coordinator
        self.session_manager = session_manager

    async def _show_progress(
        self,
        message: str,
        level: str = "info",
        event_name: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> None:
        """
        Show progress message to user and emit structured hook events.

        Displays a text message via the coordinator's display system (for CLI/terminal)
        and, when event_name and event_data are provided, emits a structured hook event
        (for UI integration and hooks-logging).

        Args:
            message: Progress message to display
            level: Message level (info, warning, error)
            event_name: Optional hook event name (e.g., "recipe:start")
            event_data: Optional structured data for the hook event
        """
        # Text display for CLI/terminal
        display_system = getattr(self.coordinator, "display_system", None)
        if display_system is not None:
            display_system.show_message(message=message, level=level, source="recipe")

        # Structured event for hooks (enables UI integration like Canvas)
        if event_name and event_data:
            hooks = getattr(self.coordinator, "hooks", None)
            if hooks is not None:
                await hooks.emit(event_name, event_data)

    async def _emit_iteration_failed(
        self, step_id: str, idx: int, exc: BaseException
    ) -> None:
        """Emit recipe:iteration_failed so observers see exceptions swallowed by on_error=continue."""
        hooks = getattr(self.coordinator, "hooks", None)
        if hooks is None:
            return
        await hooks.emit(
            "recipe:iteration_failed",
            {
                "step_id": step_id,
                "iteration": idx,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )

    def _build_steps_status(
        self,
        steps: list[Any],
        current_index: int,
        completed_steps: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build steps status list for recipe events.

        Args:
            steps: List of recipe steps
            current_index: Index of currently executing step
            completed_steps: Optional list of completed step IDs

        Returns:
            List of step status dictionaries for event emission
        """
        completed = set(completed_steps or [])
        return [
            {
                "id": step.id,
                "name": step.id,
                "status": (
                    "completed"
                    if step.id in completed or i < current_index
                    else "running"
                    if i == current_index
                    else "pending"
                ),
            }
            for i, step in enumerate(steps)
        ]

    def _build_recipe_event_data(
        self,
        recipe: Recipe,
        current_step: int,
        steps_status: list[dict[str, Any]],
        status: str,
        **extra: Any,
    ) -> dict[str, Any]:
        """
        Build standardized recipe event data.

        Args:
            recipe: The recipe being executed
            current_step: Current step index (0-based)
            steps_status: List of step status dictionaries
            status: Recipe status ('running', 'waiting_approval', 'completed', 'failed')
            **extra: Additional fields to include

        Returns:
            Event data dictionary
        """
        return {
            "name": recipe.name,
            "description": recipe.description,
            "current_step": current_step,
            "total_steps": len(steps_status),
            "steps": steps_status,
            "status": status,
            **extra,
        }

    def _check_cancellation(
        self,
        session_id: str,
        project_path: Path,
        current_step: str | None = None,
        allow_graceful_completion: bool = False,
    ) -> None:
        """Check if cancellation requested and raise if so.

        This method should be called at loop boundaries (before each step,
        before each loop iteration, etc.) to enable responsive cancellation.

        Args:
            session_id: Current session identifier
            project_path: Project path for session lookup
            current_step: Current step ID for error context
            allow_graceful_completion: If True, only raise on IMMEDIATE cancellation.
                                       Use this when a step is in progress and should
                                       be allowed to complete for graceful cancellation.

        Raises:
            CancellationRequestedError: If cancellation has been requested
        """
        if not self.session_manager.is_cancellation_requested(session_id, project_path):
            return

        is_immediate = self.session_manager.is_immediate_cancellation(
            session_id, project_path
        )

        # Graceful cancellation allows current step to complete
        if allow_graceful_completion and not is_immediate:
            return

        raise CancellationRequestedError(
            session_id=session_id,
            is_immediate=is_immediate,
            current_step=current_step,
        )

    def _check_coordinator_cancellation(
        self,
        session_id: str,
        project_path: Path,
    ) -> None:
        """Check if coordinator has cancellation requested (e.g., from SIGINT).

        This integrates with amplifier-core's CancellationToken, allowing
        cancellation signals from the CLI (Ctrl+C) to propagate to recipes.

        Args:
            session_id: Current session identifier
            project_path: Project path for session lookup
        """
        # Check if coordinator has a cancellation token
        cancellation = getattr(self.coordinator, "cancellation", None)
        if cancellation is None:
            return

        if not cancellation.is_cancelled:
            return

        # Propagate coordinator cancellation to session state
        is_immediate = cancellation.is_immediate
        self.session_manager.request_cancellation(
            session_id, project_path, immediate=is_immediate
        )

    async def execute_recipe(
        self,
        recipe: Recipe,
        context_vars: dict[str, Any],
        project_path: Path,
        session_id: str | None = None,
        recipe_path: Path | None = None,
        recursion_state: RecursionState | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        parent_session_id: str
        | None = None,  # optional: keyword-passed at call sites per Python convention
    ) -> dict[str, Any]:
        """
        Execute recipe with checkpointing and resumption.

        Args:
            recipe: Recipe to execute
            context_vars: Initial context variables (merged with recipe.context)
            project_path: Current project directory
            session_id: Optional session ID to resume
            recipe_path: Optional path to recipe file (saved to session)
            recursion_state: Optional recursion tracking state (for nested recipes)
            rate_limiter: Optional rate limiter (inherited from parent recipe)
            orchestrator_config: Optional orchestrator config (inherited from parent recipe)
            parent_session_id: Parent session ID for cancellation checks in sub-recipes

        Returns:
            Final context dict with all step outputs
        """
        # Initialize or inherit recursion state
        if recursion_state is None:
            # Top-level recipe: create initial state from recipe config
            config = recipe.recursion or RecursionConfig()
            recursion_state = RecursionState(
                current_depth=0,
                total_steps=0,
                max_depth=config.max_depth,
                max_total_steps=config.max_total_steps,
                recipe_stack=[recipe.name],
            )
        else:
            # Sub-recipe: check depth before entering
            recursion_state.check_depth(recipe.name)

        # Initialize or inherit rate limiter
        # Rate limiter is created at root recipe and inherited by sub-recipes
        # Sub-recipes CANNOT override parent's rate limits (parent wins)
        if rate_limiter is None and recipe.rate_limiting:
            rate_limiter = RateLimiter(recipe.rate_limiting)

        # Initialize or inherit orchestrator config
        # Like rate_limiter, created at root recipe and inherited by sub-recipes
        if orchestrator_config is None and recipe.orchestrator:
            orchestrator_config = recipe.orchestrator

        # Warn (once per unique step, per process lifetime) when a step declares
        # depends_on — the field is validated and documented but the executor does
        # NOT enforce it; steps always execute in declaration order.
        _warn_depends_on_unenforced(recipe)

        # Create or resume session
        is_resuming = session_id is not None

        # Route to staged execution EARLY (staged recipes have different state structure)
        if recipe.is_staged:
            # For staged recipes, load minimal state for metadata, let _execute_staged_recipe handle the rest
            if is_resuming:
                state = self.session_manager.load_state(session_id, project_path)
                context = state["context"]
                session_started = state["started"]
            else:
                session_id = self.session_manager.create_session(
                    recipe,
                    project_path,
                    recipe_path,
                    parent_session_id=parent_session_id,
                )
                context = {**recipe.context, **context_vars}
                session_started = datetime.datetime.now().isoformat()

            # Add metadata to context
            context["recipe"] = {
                "name": recipe.name,
                "version": recipe.version,
                "description": recipe.description,
            }
            context["session"] = {
                "id": session_id,
                "started": session_started,
                "project": str(project_path.resolve()),
            }

            return await self._execute_staged_recipe(
                recipe=recipe,
                context=context,
                project_path=project_path,
                session_id=session_id,
                recipe_path=recipe_path,
                recursion_state=recursion_state,
                is_resuming=is_resuming,
                rate_limiter=rate_limiter,
                orchestrator_config=orchestrator_config,
                parent_session_id=parent_session_id,
            )

        # Flat recipe state loading (uses current_step_index)
        if is_resuming:
            state = self.session_manager.load_state(session_id, project_path)
            current_step_index = state["current_step_index"]
            context = state["context"]
            completed_steps = state.get("completed_steps", [])
            session_started = state["started"]

            # Check if we're resuming from a pending child approval
            if state.get("pending_child_approval"):
                pending = self.session_manager.get_pending_approval(
                    session_id, project_path
                )
                if pending:
                    stage_name = pending["stage_name"]
                    approval_status = self.session_manager.get_stage_approval_status(
                        session_id, project_path, stage_name
                    )

                    # Check for timeout
                    timeout_result = self.session_manager.check_approval_timeout(
                        session_id, project_path
                    )
                    if timeout_result == ApprovalStatus.TIMEOUT:
                        raise ValueError(
                            f"Approval for stage '{stage_name}' timed out and was denied"
                        )
                    if timeout_result == ApprovalStatus.APPROVED:
                        # Auto-approved on timeout, clear and continue
                        self.session_manager.clear_pending_approval(
                            session_id, project_path
                        )
                    elif approval_status == ApprovalStatus.PENDING:
                        # Still pending - raise to indicate waiting
                        raise ApprovalGatePausedError(
                            session_id=session_id,
                            stage_name=stage_name,
                            approval_prompt=pending["approval_prompt"],
                        )
                    elif approval_status == ApprovalStatus.DENIED:
                        raise ValueError(f"Execution denied at stage '{stage_name}'")
                    elif approval_status == ApprovalStatus.APPROVED:
                        # Approved - clear pending, reload state, inject approval
                        # message, remove pending_child_approval from state, and save
                        self.session_manager.clear_pending_approval(
                            session_id, project_path
                        )
                        state = self.session_manager.load_state(
                            session_id, project_path
                        )
                        context["_approval_message"] = state.get(
                            "_approval_message", ""
                        )
                        state.pop("pending_child_approval", None)
                        self.session_manager.save_state(session_id, project_path, state)
        else:
            session_id = self.session_manager.create_session(
                recipe, project_path, recipe_path, parent_session_id=parent_session_id
            )
            current_step_index = 0
            context = {**recipe.context, **context_vars}
            completed_steps = []
            session_started = datetime.datetime.now().isoformat()

        # Effective session ID for cancellation checks
        # For sub-recipes (session_id=None), use parent_session_id to inherit cancellation state
        cancellation_session_id = session_id or parent_session_id

        # Show recipe start progress
        total_steps = len(recipe.steps)
        steps_status = self._build_steps_status(recipe.steps, 0, [])
        extra = {"parent_session_id": parent_session_id} if parent_session_id else {}
        await self._show_progress(
            f"📋 Starting recipe: {recipe.name} ({total_steps} steps)",
            event_name="recipe:start",
            event_data=self._build_recipe_event_data(
                recipe, 0, steps_status, "running", **extra
            ),
        )

        # Add metadata to context
        context["recipe"] = {
            "name": recipe.name,
            "version": recipe.version,
            "description": recipe.description,
        }
        context["session"] = {
            "id": session_id,
            "started": session_started,
            "project": str(project_path.resolve()),
        }

        # Initialize state for exception handler (will be set during execution)
        state: dict[str, Any] | None = None

        # Flat mode execution (staged recipes already returned above)
        try:
            # Execute remaining steps
            for i in range(current_step_index, len(recipe.steps)):
                step = recipe.steps[i]

                # Check for cancellation before starting each step
                # Use cancellation_session_id to support both root recipes and sub-recipes
                if cancellation_session_id:
                    self._check_coordinator_cancellation(
                        cancellation_session_id, project_path
                    )
                    self._check_cancellation(
                        cancellation_session_id, project_path, current_step=step.id
                    )

                # Add step metadata to context
                context["step"] = {"id": step.id, "index": i}

                # Show step progress
                step_num = i + 1
                step_type = step.type or "agent"
                steps_status = self._build_steps_status(
                    recipe.steps, i, completed_steps
                )
                await self._show_progress(
                    f"  [{step_num}/{total_steps}] {step.id} ({step_type})",
                    event_name="recipe:step",
                    event_data=self._build_recipe_event_data(
                        recipe, i, steps_status, "running"
                    ),
                )

                # Check condition if present
                if step.condition:
                    try:
                        condition_result = evaluate_condition(step.condition, context)
                    except ExpressionError as e:
                        raise ValueError(
                            f"Step '{step.id}': condition error: {e}"
                        ) from e

                    if not condition_result:
                        # Skip this step - record in state but don't execute
                        skipped_steps = context.get("_skipped_steps", [])
                        skipped_steps.append(step.id)
                        context["_skipped_steps"] = skipped_steps
                        continue

                # Handle foreach loops and while loops
                if step.foreach or step.while_condition:
                    try:
                        await self._execute_loop(
                            step,
                            context,
                            project_path,
                            recursion_state,
                            recipe_path,
                            rate_limiter,
                            orchestrator_config,
                            session_id=cancellation_session_id,
                        )
                        # Update completed steps and session state after loop completes
                        completed_steps.append(step.id)
                        state = {
                            "session_id": session_id,
                            "recipe_name": recipe.name,
                            "recipe_version": recipe.version,
                            "started": context["session"]["started"],
                            "current_step_index": i + 1,
                            "context": self._trim_context_for_checkpoint(context),
                            "completed_steps": completed_steps,
                            "project_path": str(project_path.resolve()),
                            "parent_session_id": parent_session_id,
                        }
                        self.session_manager.save_state(session_id, project_path, state)
                        continue
                    except SkipRemainingError:
                        break

                # Execute step based on type (agent, recipe, or bash)
                try:
                    if step.type == "recipe":
                        result = await self._execute_recipe_step(
                            step,
                            context,
                            project_path,
                            recursion_state,
                            recipe_path,
                            rate_limiter,
                            orchestrator_config,
                            parent_session_id=cancellation_session_id,
                        )
                    elif step.type == "bash":
                        # Bash steps don't count against agent recursion limits
                        bash_result = await self._execute_bash_step(
                            step, context, project_path
                        )
                        # Store exit code if requested
                        if step.output_exit_code:
                            context[step.output_exit_code] = str(bash_result.exit_code)
                        result = bash_result.stdout
                    else:
                        # Agent step - track for recursion limits
                        recursion_state.increment_steps()
                        result = await self.execute_step_with_retry(
                            step,
                            context,
                            rate_limiter,
                            orchestrator_config,
                            session_id=cancellation_session_id,
                            project_path=project_path,
                        )

                    # Process result: unwrap spawn() output and optionally parse JSON
                    result = self._process_step_result(result, step)

                    # Store output if specified
                    if step.output:
                        context[step.output] = result

                    # Update completed steps and session state
                    completed_steps.append(step.id)

                    state = {
                        "session_id": session_id,
                        "recipe_name": recipe.name,
                        "recipe_version": recipe.version,
                        "started": context["session"]["started"],
                        "current_step_index": i + 1,
                        "context": self._trim_context_for_checkpoint(context),
                        "completed_steps": completed_steps,
                        "project_path": str(project_path.resolve()),
                        "parent_session_id": parent_session_id,
                    }

                    # Checkpoint after each step
                    self.session_manager.save_state(session_id, project_path, state)

                except SkipRemainingError:
                    # Skip remaining steps
                    break
                except ApprovalGatePausedError as e:
                    # Child recipe step paused at an approval gate.
                    # Mirror the approval onto the parent session so the parent
                    # also appears paused, then re-raise with parent session_id.
                    compound_stage = e.stage_name

                    # (1) Save parent state at the current step (don't advance)
                    state = {
                        "session_id": session_id,
                        "recipe_name": recipe.name,
                        "recipe_version": recipe.version,
                        "started": context["session"]["started"],
                        "current_step_index": i,
                        "context": self._trim_context_for_checkpoint(context),
                        "completed_steps": completed_steps,
                        "project_path": str(project_path.resolve()),
                        "parent_session_id": parent_session_id,
                        "pending_child_approval": {
                            "child_session_id": e.session_id,
                            "child_stage_name": e.stage_name,
                            "parent_step_id": step.id,
                        },
                    }
                    self.session_manager.save_state(session_id, project_path, state)

                    # (2) Mirror the child's approval gate on the parent session
                    self.session_manager.set_pending_approval(
                        session_id=session_id,
                        project_path=project_path,
                        stage_name=compound_stage,
                        prompt=e.approval_prompt,
                        timeout=0,
                        default="deny",
                    )

                    # (3) Re-raise a new APE with the parent's session_id
                    raise ApprovalGatePausedError(
                        session_id=session_id,
                        stage_name=compound_stage,
                        approval_prompt=e.approval_prompt,
                        resume_session_id=e.session_id,
                    ) from e
                except CancellationRequestedError:
                    # Cancellation requested - save state and re-raise
                    raise

        except CancellationRequestedError as e:
            # Mark session as cancelled and save state for later resumption
            self.session_manager.mark_cancelled(
                session_id,
                project_path,
                cancelled_at_step=e.current_step,
            )
            if state is not None:
                self.session_manager.save_state(session_id, project_path, state)
            await self._show_progress(
                f"⚠️ Recipe cancelled at step: {e.current_step or 'unknown'}",
                level="warning",
            )
            raise

        except Exception:
            # Save state even on error for resumption
            if state is not None:
                self.session_manager.save_state(session_id, project_path, state)
            raise

        # Cleanup old sessions
        self.session_manager.cleanup_old_sessions(project_path)

        # Show completion
        steps_status = self._build_steps_status(
            recipe.steps, total_steps, completed_steps
        )
        await self._show_progress(
            f"✅ Recipe completed: {recipe.name}",
            event_name="recipe:complete",
            event_data=self._build_recipe_event_data(
                recipe, total_steps, steps_status, "completed", success=True
            ),
        )

        return context

    async def _execute_staged_recipe(
        self,
        recipe: Recipe,
        context: dict[str, Any],
        project_path: Path,
        session_id: str,
        recipe_path: Path | None,
        recursion_state: RecursionState,
        is_resuming: bool,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        parent_session_id: str
        | None = None,  # optional: keyword-passed at call sites per Python convention
    ) -> dict[str, Any]:
        """
        Execute a staged recipe with approval gates.

        Args:
            recipe: Staged recipe to execute
            context: Current context variables
            project_path: Current project directory
            session_id: Session identifier
            recipe_path: Optional path to recipe file
            recursion_state: Recursion tracking state
            is_resuming: Whether resuming an existing session

        Returns:
            Final context dict with all step outputs

        Raises:
            ApprovalGatePausedError: When execution pauses at an approval gate
        """
        # Load state for resumption
        if is_resuming:
            state = self.session_manager.load_state(session_id, project_path)
            current_stage_index = state.get("current_stage_index", 0)
            current_step_in_stage = state.get("current_step_in_stage", 0)
            completed_stages = state.get("completed_stages", [])
            completed_steps = state.get("completed_steps", [])
            pending_child = state.get("pending_child_approval")

            # Check if we're resuming from a pending approval
            pending = self.session_manager.get_pending_approval(
                session_id, project_path
            )
            if pending:
                stage_name = pending["stage_name"]
                approval_status = self.session_manager.get_stage_approval_status(
                    session_id, project_path, stage_name
                )

                # Check for timeout
                timeout_result = self.session_manager.check_approval_timeout(
                    session_id, project_path
                )
                if timeout_result == ApprovalStatus.TIMEOUT:
                    raise ValueError(
                        f"Approval for stage '{stage_name}' timed out and was denied"
                    )
                if timeout_result == ApprovalStatus.APPROVED:
                    # Auto-approved on timeout, clear and continue
                    self.session_manager.clear_pending_approval(
                        session_id, project_path
                    )
                elif approval_status == ApprovalStatus.PENDING:
                    # Still pending - raise to indicate waiting
                    raise ApprovalGatePausedError(
                        session_id=session_id,
                        stage_name=stage_name,
                        approval_prompt=pending["approval_prompt"],
                    )
                elif approval_status == ApprovalStatus.DENIED:
                    raise ValueError(f"Execution denied at stage '{stage_name}'")
                elif approval_status == ApprovalStatus.APPROVED:
                    # Approved, clear pending and continue
                    self.session_manager.clear_pending_approval(
                        session_id, project_path
                    )
                    # Inject approval message into context for subsequent steps
                    state = self.session_manager.load_state(session_id, project_path)
                    context["_approval_message"] = state.get("_approval_message", "")
                    # Clear pending_child_approval if it was present
                    if pending_child:
                        state.pop("pending_child_approval", None)
                        self.session_manager.save_state(session_id, project_path, state)
        else:
            current_stage_index = 0
            current_step_in_stage = 0
            completed_stages = []
            completed_steps = []

        try:
            # Execute stages
            total_stages = len(recipe.stages)
            for stage_idx in range(current_stage_index, len(recipe.stages)):
                stage = recipe.stages[stage_idx]

                # Check for cancellation before starting each stage
                self._check_coordinator_cancellation(session_id, project_path)
                self._check_cancellation(
                    session_id, project_path, current_step=f"stage:{stage.name}"
                )

                # Show stage progress
                await self._show_progress(
                    f"📦 Stage {stage_idx + 1}/{total_stages}: {stage.name}"
                )

                # Add stage metadata to context
                context["stage"] = {
                    "name": stage.name,
                    "index": stage_idx,
                }

                # Determine starting step within this stage
                start_step = (
                    current_step_in_stage if stage_idx == current_stage_index else 0
                )

                # Execute steps within this stage
                for step_idx in range(start_step, len(stage.steps)):
                    step = stage.steps[step_idx]

                    # Check for cancellation before starting each step
                    self._check_coordinator_cancellation(session_id, project_path)
                    self._check_cancellation(
                        session_id, project_path, current_step=step.id
                    )

                    # Add step metadata to context
                    context["step"] = {
                        "id": step.id,
                        "index": step_idx,
                        "stage": stage.name,
                    }

                    # Check condition if present
                    if step.condition:
                        try:
                            condition_result = evaluate_condition(
                                step.condition, context
                            )
                        except ExpressionError as e:
                            raise ValueError(
                                f"Step '{step.id}': condition error: {e}"
                            ) from e

                        if not condition_result:
                            skipped_steps = context.get("_skipped_steps", [])
                            skipped_steps.append(step.id)
                            context["_skipped_steps"] = skipped_steps
                            continue

                    # Handle foreach loops and while loops
                    if step.foreach or step.while_condition:
                        try:
                            await self._execute_loop(
                                step,
                                context,
                                project_path,
                                recursion_state,
                                recipe_path,
                                rate_limiter,
                                orchestrator_config,
                                session_id=session_id,
                            )
                            completed_steps.append(step.id)
                            self._save_staged_state(
                                session_id,
                                project_path,
                                recipe,
                                context,
                                stage_idx,
                                step_idx + 1,
                                completed_stages,
                                completed_steps,
                                recipe_path=recipe_path,
                                parent_session_id=parent_session_id,
                            )
                            continue
                        except SkipRemainingError:
                            break

                    # Execute step based on type (agent, recipe, or bash)
                    try:
                        if step.type == "recipe":
                            result = await self._execute_recipe_step(
                                step,
                                context,
                                project_path,
                                recursion_state,
                                recipe_path,
                                rate_limiter,
                                orchestrator_config,
                                parent_session_id=session_id,
                            )
                        elif step.type == "bash":
                            # Bash steps don't count against agent recursion limits
                            bash_result = await self._execute_bash_step(
                                step, context, project_path
                            )
                            # Store exit code if requested
                            if step.output_exit_code:
                                context[step.output_exit_code] = str(
                                    bash_result.exit_code
                                )
                            result = bash_result.stdout
                        else:
                            # Agent step - track for recursion limits
                            recursion_state.increment_steps()
                            result = await self.execute_step_with_retry(
                                step,
                                context,
                                rate_limiter,
                                orchestrator_config,
                                session_id=session_id,
                                project_path=project_path,
                            )

                        # Process result: unwrap spawn() output and optionally parse JSON
                        result = self._process_step_result(result, step)

                        if step.output:
                            context[step.output] = result

                        completed_steps.append(step.id)
                        self._save_staged_state(
                            session_id,
                            project_path,
                            recipe,
                            context,
                            stage_idx,
                            step_idx + 1,
                            completed_stages,
                            completed_steps,
                            recipe_path=recipe_path,
                            parent_session_id=parent_session_id,
                        )

                    except SkipRemainingError:
                        break
                    except ApprovalGatePausedError as e:
                        # (1) Save staged state at current step (don't advance)
                        self._save_staged_state(
                            session_id,
                            project_path,
                            recipe,
                            context,
                            stage_idx,
                            step_idx,
                            completed_stages,
                            completed_steps,
                            recipe_path=recipe_path,
                            parent_session_id=parent_session_id,
                        )
                        # (2) Create compound stage name
                        compound_stage = f"{stage.name}/{e.stage_name}"
                        # (3) Mirror approval on parent session
                        self.session_manager.set_pending_approval(
                            session_id=session_id,
                            project_path=project_path,
                            stage_name=compound_stage,
                            prompt=e.approval_prompt,
                            timeout=0,
                            default="deny",
                        )
                        # (4) Add pending_child_approval metadata to saved state
                        state = self.session_manager.load_state(
                            session_id, project_path
                        )
                        state["pending_child_approval"] = {
                            "child_session_id": e.session_id,
                            "child_stage_name": e.stage_name,
                            "parent_step_id": step.id,
                        }
                        self.session_manager.save_state(session_id, project_path, state)
                        # (5) Re-raise new APE with parent's session_id
                        raise ApprovalGatePausedError(
                            session_id=session_id,
                            stage_name=compound_stage,
                            approval_prompt=e.approval_prompt,
                            resume_session_id=e.session_id,
                        ) from e
                    except CancellationRequestedError:
                        # Cancellation requested - re-raise to outer handler
                        raise

                # Stage completed - check for approval gate
                completed_stages.append(stage.name)

                if stage.approval and stage.approval.required:
                    # Save state with next stage as target FIRST
                    # (set_pending_approval will load, add approval fields, and save)
                    self._save_staged_state(
                        session_id,
                        project_path,
                        recipe,
                        context,
                        stage_idx + 1,
                        0,
                        completed_stages,
                        completed_steps,
                        recipe_path=recipe_path,
                        parent_session_id=parent_session_id,
                    )

                    # Set pending approval AFTER saving state (this loads, modifies, saves)
                    # Resolve template variables in approval prompt before display
                    raw_approval_prompt = (
                        stage.approval.prompt
                        or f"Approve completion of stage '{stage.name}'?"
                    )
                    resolved_approval_prompt = self.substitute_variables(
                        raw_approval_prompt, context
                    )

                    self.session_manager.set_pending_approval(
                        session_id=session_id,
                        project_path=project_path,
                        stage_name=stage.name,
                        prompt=resolved_approval_prompt,
                        timeout=stage.approval.timeout,
                        default=stage.approval.default,
                    )

                    # Emit approval event for UI
                    all_steps = [s for stg in recipe.stages for s in stg.steps]
                    current_step_in_stage = len(stage.steps) - 1  # Last step in stage
                    steps_status = self._build_steps_status(
                        all_steps, current_step_in_stage, completed_steps
                    )
                    # Mark current step as waiting for approval
                    for i, step_stat in enumerate(steps_status):
                        if step_stat["id"] == stage.steps[-1].id:
                            steps_status[i]["status"] = "waiting_approval"
                            steps_status[i]["is_approval_gate"] = True
                            break
                    approval_prompt = (
                        stage.approval.prompt
                        or f"Approve completion of stage '{stage.name}'?"
                    )
                    await self._show_progress(
                        f"⏸️ Waiting for approval: {stage.name}",
                        event_name="recipe:approval",
                        event_data=self._build_recipe_event_data(
                            recipe,
                            current_step_in_stage,
                            steps_status,
                            "waiting_approval",
                            prompt=approval_prompt,
                            stage_name=stage.name,
                        ),
                    )

                    # Raise to indicate paused state
                    raise ApprovalGatePausedError(
                        session_id=session_id,
                        stage_name=stage.name,
                        approval_prompt=resolved_approval_prompt,
                    )

                # No approval needed - save progress and continue
                context.setdefault("_approval_message", "")
                self._save_staged_state(
                    session_id,
                    project_path,
                    recipe,
                    context,
                    stage_idx + 1,
                    0,
                    completed_stages,
                    completed_steps,
                    recipe_path=recipe_path,
                    parent_session_id=parent_session_id,
                )

        except ApprovalGatePausedError:
            # Re-raise approval pause (not an error)
            raise
        except CancellationRequestedError as e:
            # Mark session as cancelled and save state for later resumption
            self.session_manager.mark_cancelled(
                session_id,
                project_path,
                cancelled_at_step=e.current_step,
            )
            self._save_staged_state(
                session_id,
                project_path,
                recipe,
                context,
                current_stage_index,
                current_step_in_stage,
                completed_stages,
                completed_steps,
                recipe_path=recipe_path,
                parent_session_id=parent_session_id,
            )
            await self._show_progress(
                f"⚠️ Recipe cancelled at step: {e.current_step or 'unknown'}",
                level="warning",
            )
            raise
        except Exception:
            # Save state for resumption on error
            self._save_staged_state(
                session_id,
                project_path,
                recipe,
                context,
                current_stage_index,
                current_step_in_stage,
                completed_stages,
                completed_steps,
                recipe_path=recipe_path,
                parent_session_id=parent_session_id,
            )
            raise

        # Cleanup old sessions
        self.session_manager.cleanup_old_sessions(project_path)

        # Show completion - gather all steps from all stages for status
        all_steps = [step for stage in recipe.stages for step in stage.steps]
        total_steps = len(all_steps)
        steps_status = self._build_steps_status(all_steps, total_steps, completed_steps)
        await self._show_progress(
            f"✅ Recipe completed: {recipe.name}",
            event_name="recipe:complete",
            event_data=self._build_recipe_event_data(
                recipe, total_steps, steps_status, "completed", success=True
            ),
        )

        return context

    def _save_staged_state(
        self,
        session_id: str,
        project_path: Path,
        recipe: Recipe,
        context: dict[str, Any],
        stage_index: int,
        step_in_stage: int,
        completed_stages: list[str],
        completed_steps: list[str],
        recipe_path: Path | None = None,
        parent_session_id: str
        | None = None,  # optional: keyword-passed at call sites per Python convention
    ) -> None:
        """Save state for staged recipe execution."""
        state = {
            "session_id": session_id,
            "recipe_name": recipe.name,
            "recipe_version": recipe.version,
            "started": context["session"]["started"],
            "current_stage_index": stage_index,
            "current_step_in_stage": step_in_stage,
            "context": self._trim_context_for_checkpoint(context),
            "completed_stages": completed_stages,
            "completed_steps": completed_steps,
            "project_path": str(project_path.resolve()),
            "parent_session_id": parent_session_id,
            "is_staged": True,
            "recipe_path": str(recipe_path) if recipe_path else None,
        }
        self.session_manager.save_state(session_id, project_path, state)

    async def execute_step_with_retry(
        self,
        step: Step,
        context: dict[str, Any],
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
        project_path: Path | None = None,
    ) -> Any:
        """
        Execute step with retry logic.

        Args:
            step: Step to execute
            context: Current context variables
            rate_limiter: Optional rate limiter for pacing
            orchestrator_config: Optional orchestrator config for spawned sessions
            session_id: Session identifier for cancellation checks
            project_path: Project path for cancellation checks

        Returns:
            Step result

        Raises:
            Exception if all retries fail and on_error='fail'
            SkipRemainingError if on_error='skip_remaining'
            CancellationRequestedError if cancellation requested
        """
        retry_config = step.retry if isinstance(step.retry, dict) else {}
        max_attempts = retry_config.get("max_attempts", 1)
        backoff = retry_config.get("backoff", "exponential")
        delay = retry_config.get("initial_delay", 5)
        max_delay = retry_config.get("max_delay", 300)

        last_error = None

        for attempt in range(max_attempts):
            # Check for cancellation before each attempt
            if session_id and project_path:
                self._check_coordinator_cancellation(session_id, project_path)
                self._check_cancellation(session_id, project_path, current_step=step.id)
            try:
                # Acquire rate limiter slot if configured
                if rate_limiter:
                    await rate_limiter.acquire()

                try:
                    result = await self.execute_step(step, context, orchestrator_config)
                    # Record success for backoff tracking
                    if rate_limiter:
                        rate_limiter.record_success()
                    return result
                finally:
                    # Always release rate limiter slot
                    if rate_limiter:
                        rate_limiter.release()

            except Exception as e:
                last_error = e

                # Check if this is a rate limit error (429)
                error_str = str(e).lower()
                is_rate_limit = "429" in error_str or "rate limit" in error_str
                if is_rate_limit and rate_limiter:
                    rate_limiter.record_rate_limit()

                # If final attempt or not retryable
                if attempt == max_attempts - 1:
                    # Handle based on on_error strategy
                    if step.on_error == "fail":
                        raise
                    if step.on_error == "continue":
                        return None  # Continue with None result
                    if step.on_error == "skip_remaining":
                        raise SkipRemainingError() from e

                # Wait before retry
                await asyncio.sleep(min(delay, max_delay))

                # Adjust delay for next attempt
                if backoff == "exponential":
                    delay *= 2
                # Linear backoff keeps same delay

        # Shouldn't reach here, but handle just in case
        if step.on_error == "fail" and last_error:
            raise last_error
        return None

    def _extract_json_aggressively(self, output: str) -> Any:
        """
        Aggressively extract JSON from output using multiple strategies.

        Only called when parse_json: true is set on the step.

        Strategies (in order):
        1. Entire string is valid JSON
        2. Extract from markdown code block (```json ... ```)
        3. Find JSON object/array embedded in text

        Args:
            output: String output from agent

        Returns:
            Parsed JSON object/array, or original string if no JSON found
        """
        output_stripped = output.strip()

        if not output_stripped:
            return output

        # Strategy 1: Entire string is valid JSON
        try:
            return json.loads(output_stripped)
        except (json.JSONDecodeError, ValueError):
            pass

        # Strategy 2: Extract from markdown code block.
        # The old approach used a non-greedy regex (r"```(?:json)?\s*(\[.*?\]|
        # \{.*?\})\s*```") whose .*? could truncate at the first inner } / ]
        # instead of matching the full balanced structure.  We now find the
        # opening fence, locate the closing fence, and apply JSONDecoder.
        # raw_decode on the fenced content so balanced-brace handling is done
        # correctly by the JSON parser itself.
        fence_match = re.search(r"```(?:json)?\s*", output_stripped)
        if fence_match:
            fence_start = fence_match.end()
            end_fence_idx = output_stripped.find("```", fence_start)
            if end_fence_idx != -1:
                fenced_content = output_stripped[fence_start:end_fence_idx].strip()
                if fenced_content:
                    try:
                        s2_decoder = json.JSONDecoder()
                        parsed_s2, _ = s2_decoder.raw_decode(fenced_content)
                        if parsed_s2 not in ({}, []):
                            return parsed_s2
                        # Trivial structure ({} / []) — fall through to
                        # Strategy 3 in case something richer comes later.
                    except (json.JSONDecodeError, ValueError):
                        pass  # fall through to Strategy 3

        # Strategy 3: Find JSON embedded in text (position-ordered, skip trivial)
        # Scan for [ and { in document order so the first real JSON wins,
        # regardless of whether it is an array or object.  Skip trivially
        # empty structures ({} / []) that commonly appear in prose so that a
        # meaningful structure later in the text is preferred.
        decoder = json.JSONDecoder()
        first_parsed = None
        idx = 0
        while idx < len(output_stripped):
            # Jump to the next potential JSON start character
            idx_bracket = output_stripped.find("[", idx)
            idx_brace = output_stripped.find("{", idx)
            candidates = [i for i in (idx_bracket, idx_brace) if i != -1]
            if not candidates:
                break
            next_idx = min(candidates)
            try:
                parsed, end_idx = decoder.raw_decode(output_stripped, next_idx)
                if first_parsed is None:
                    first_parsed = parsed
                # Return first non-trivial JSON found
                if parsed not in ({}, []):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            idx = next_idx + 1
        # Only trivial JSON found ({} or []) – return it rather than raw text
        if first_parsed is not None:
            return first_parsed

        # All strategies failed - return as-is
        return output

    def _trim_context_for_checkpoint(self, context: dict[str, Any]) -> dict[str, Any]:
        """Return a checkpoint-safe copy of context with oversized values summarised.

        The *live* context dict is never modified — only the serialised copy written
        to disk is trimmed.  Any value whose JSON representation exceeds
        ``_CHECKPOINT_TRIM_THRESHOLD_BYTES`` is replaced with a human-readable
        placeholder string.  This keeps checkpoint files small and avoids the
        O(n²) serialisation cost that compounds across many steps.

        If a recipe needs to be *resumed* from a checkpoint that contains trimmed
        values, those keys will be missing from the restored context.  In practice
        Fix 1 (sub-recipe output trimming) ensures that large intermediate blobs
        never accumulate in the first place; Fix 3 is a last-resort safety net.

        Args:
            context: The full in-memory execution context dict.

        Returns:
            A new shallow dict where large values are replaced by placeholder strings.
        """
        trimmed: dict[str, Any] = {}
        for key, value in context.items():
            try:
                serialised = json.dumps(value, ensure_ascii=False)
                if len(serialised) > _CHECKPOINT_TRIM_THRESHOLD_BYTES:
                    size_kb = len(serialised) // 1024
                    trimmed[key] = (
                        f"[trimmed: {size_kb}KB — omitted from checkpoint to reduce serialisation pressure]"
                    )
                    logger.debug(
                        "Checkpoint trim: key '%s' (%dKB) replaced with placeholder",
                        key,
                        size_kb,
                    )
                else:
                    trimmed[key] = value
            except (TypeError, ValueError):
                # Value is not JSON-serialisable — keep as-is so save_state can
                # surface the error naturally rather than silently dropping data.
                trimmed[key] = value
        return trimmed

    def _process_step_result(self, result: Any, step: Step) -> Any:
        """
        Process step result: unwrap spawn() output and optionally parse JSON.

        By default, preserves output as-is (prose, markdown, formatting).
        Only parses JSON if:
        - The ENTIRE output is clean JSON (no markdown, no prose), OR
        - The step has parse_json: true set (aggressive extraction)

        Args:
            result: Raw result from step execution
            step: Step configuration (to check parse_json flag)

        Returns:
            Processed result (unwrapped and/or parsed)
        """
        # Step 1: Unwrap spawn() result if it's a dict with "output" key
        if isinstance(result, dict) and "output" in result:
            output = result["output"]
        else:
            output = result

        # Step 2: Parse JSON if requested
        if isinstance(output, str) and step.parse_json:
            # Opt-in aggressive JSON extraction
            return self._extract_json_aggressively(output)

        # Step 3: Conservative default - only parse clean JSON
        if isinstance(output, str):
            output_stripped = output.strip()
            if output_stripped:
                try:
                    return json.loads(output_stripped)
                except (json.JSONDecodeError, ValueError):
                    # Step 4: For bash steps, try aggressive parsing as fallback
                    # Bash commands often print status messages before JSON output
                    if step.type == "bash":
                        extracted = self._extract_json_aggressively(output)
                        if extracted != output:  # Successfully extracted JSON
                            return extracted

        return output

    async def execute_step(
        self,
        step: Step,
        context: dict[str, Any],
        orchestrator_config: OrchestratorConfig | None = None,
    ) -> Any:
        """
        Execute single step by spawning sub-agent.

        Args:
            step: Step to execute
            context: Current context variables
            orchestrator_config: Optional orchestrator config for spawned sessions

        Returns:
            Step result from agent
        """
        # Get spawn capability from coordinator (registered by app layer)
        # This follows kernel philosophy: modules request capabilities, apps provide them
        spawn_fn = self.coordinator.get_capability("session.spawn")
        if spawn_fn is None:
            raise RuntimeError(
                f"Step '{step.id}' requires agent spawning but 'session.spawn' capability not registered. "
                "Ensure the app layer registers session spawning capabilities."
            )

        # Agent steps must have prompt and agent (validated by models)
        if not step.prompt or not step.agent:
            raise ValueError(
                f"Step '{step.id}' is an agent step but missing prompt or agent"
            )

        # Substitute variables in prompt
        instruction = self.substitute_variables(step.prompt, context)

        # Add mode if specified
        if step.mode:
            mode_instruction = f"MODE: {step.mode}\n\n"
            instruction = mode_instruction + instruction

        # Add JSON output instruction if parse_json is enabled
        if step.parse_json:
            json_instruction = """

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
            instruction = instruction + json_instruction

        # Get parent session and agents config from coordinator
        parent_session = self.coordinator.session
        agents = self.coordinator.config.get("agents", {})

        # Build orchestrator config dict for spawn if present
        orchestrator_dict = orchestrator_config.config if orchestrator_config else None

        # Build provider preferences from step configuration
        provider_preferences = None

        # Resolve model_role via the model_role_resolver capability (takes priority
        # over legacy fields, but provider_preferences on the step is more
        # explicit and wins). The capability is duck-typed:
        #     async def resolve(model_role) -> list[ProviderPreference]
        # whichever routing bundle (matrix-based, cost-aware, etc.) is active
        # registers it.
        if step.model_role and not step.provider_preferences:
            resolver = (
                self.coordinator.get_capability("model_role_resolver")
                if hasattr(self.coordinator, "get_capability")
                else None
            )
            if resolver is None:
                logger.warning(
                    "step '%s' set model_role '%s' but no model_role_resolver "
                    "capability is registered",
                    step.id,
                    step.model_role,
                )
            else:
                resolved = await resolver.resolve(step.model_role)
                if resolved:
                    provider_preferences = list(resolved)

        if step.provider_preferences:
            # New: Use explicit provider_preferences list with fallback order
            provider_preferences = []
            for pref in step.provider_preferences:
                if getattr(pref, "model_class", ""):
                    # Class entry (YAML `class:`) - provider-agnostic. Resolve it
                    # through the same model_role_resolver capability that backs
                    # step.model_role, splicing the result in at this position so
                    # later explicit provider entries remain the fallback chain.
                    resolver = (
                        self.coordinator.get_capability("model_role_resolver")
                        if hasattr(self.coordinator, "get_capability")
                        else None
                    )
                    if resolver is None:
                        logger.warning(
                            "step '%s' set provider_preferences class '%s' but no "
                            "model_role_resolver capability is registered - "
                            "skipping this entry",
                            step.id,
                            pref.model_class,
                        )
                        continue
                    resolved = await resolver.resolve(pref.model_class)
                    if resolved:
                        provider_preferences.extend(resolved)
                    continue

                # Explicit provider/model preference
                resolved_model = pref.model
                if pref.model:
                    model_resolution = await resolve_model_pattern(
                        model_hint=pref.model,
                        provider_name=pref.provider,
                        coordinator=self.coordinator,
                    )
                    resolved_model = model_resolution.resolved_model
                provider_preferences.append(
                    ProviderPreference(provider=pref.provider, model=resolved_model)
                )
        elif step.provider and step.model:
            # Legacy: Single provider + model fields
            resolved_model = step.model
            model_resolution = await resolve_model_pattern(
                model_hint=step.model,
                provider_name=step.provider,
                coordinator=self.coordinator,
            )
            resolved_model = model_resolution.resolved_model
            provider_preferences = [
                ProviderPreference(provider=step.provider, model=resolved_model)
            ]
        elif step.provider:
            # Legacy: Provider without model - use provider's default
            provider_preferences = [
                ProviderPreference(provider=step.provider, model="")
            ]

        # Fallback: apply agent-level default provider_preferences if no step-level config.
        # Mirrors tool-delegate's pattern (amplifier-foundation lines 834-841).
        if provider_preferences is None:
            agent_cfg = agents.get(step.agent, {})
            if not isinstance(agent_cfg, dict):
                # Guard: agent config may be stored as a plain string (e.g. a description
                # or a bare agent-name reference).  Treat non-dict values as "no config"
                # so that the .get() calls below don't raise AttributeError.
                agent_cfg = {}
            agent_default_prefs = agent_cfg.get("provider_preferences", [])
            if agent_default_prefs:
                provider_preferences = [
                    ProviderPreference.from_dict(p) for p in agent_default_prefs
                ]

        # Fallback 2: if agent has model_role but neither step-level config nor
        # agent-level provider_preferences resolved, ask the model_role_resolver
        # capability directly. This used to walk the matrix dict by hand
        # (duplicating routing-matrix resolver logic); under the new contract
        # the resolver handles strategy details (matrix, cost-aware, etc.) and
        # we just call .resolve().
        if provider_preferences is None:
            agent_cfg = agents.get(step.agent, {})
            if not isinstance(agent_cfg, dict):
                # Guard: a non-dict agent config value (e.g. a plain string)
                # has no model_role.
                agent_cfg = {}
            agent_model_role = agent_cfg.get("model_role")
            if agent_model_role:
                resolver = (
                    self.coordinator.get_capability("model_role_resolver")
                    if hasattr(self.coordinator, "get_capability")
                    else None
                )
                if resolver is not None:
                    resolved = await resolver.resolve(agent_model_role)
                    if resolved:
                        provider_preferences = list(resolved)

        # Whatever produced the chain above -- step, agent config, or the
        # routing matrix behind the resolver -- it names provider MODULES, and
        # this host mounts provider INSTANCES. Resolve that ambiguity here, so
        # the spawner and the child's own routing re-assert both land on the
        # same instance instead of each guessing differently (see
        # `pin_preferences_to_instances`).
        host_config = getattr(self.coordinator, "config", None)
        provider_preferences = pin_preferences_to_instances(
            provider_preferences,
            host_config.get("providers") if isinstance(host_config, dict) else None,
        )

        # ...and the overlay this spawn carries declares that same chain, so
        # the child's own routing re-assert reads instance ids rather than the
        # module names its definition file was written with (see
        # `align_overlay_preferences`). Without this the argument's promotion
        # is undone by the child at session:start.
        agents = align_overlay_preferences(agents, step.agent, provider_preferences)

        # Build session metadata for child session tracking (navigation graph support)
        recipe_info = context.get("recipe", {})
        step_info = context.get("step", {})
        session_metadata: dict[str, Any] = {
            "agent_name": step.agent,
            "recipe_name": recipe_info.get("name", ""),
            "recipe_step": step.id,
            "recipe_step_index": step_info.get("index"),
        }
        # Include parallel_group_id if this spawn is part of a parallel batch
        parallel_group_id = context.get("_parallel_group_id")
        if parallel_group_id:
            session_metadata["parallel_group_id"] = parallel_group_id

        # Spawn sub-session with agent via capability (with step timeout)
        spawn_coro = spawn_fn(
            agent_name=step.agent,
            instruction=instruction,
            parent_session=parent_session,
            agent_configs=agents,
            sub_session_id=None,  # Let spawner generate ID
            orchestrator_config=orchestrator_dict,
            provider_preferences=provider_preferences,
            session_metadata=session_metadata,
            use_subprocess=step.spawn_mode == "subprocess",
        )
        try:
            result = await asyncio.wait_for(spawn_coro, timeout=step.timeout)
        except asyncio.TimeoutError:
            raise ValueError(
                f"Step '{step.id}': agent '{step.agent}' timed out after {step.timeout}s"
            ) from None

        # Give the cyclic GC a chance to reclaim PyO3 / Rust-backed objects and
        # break reference cycles held by the completed AmplifierSession.  This is
        # especially effective inside foreach loops where many sessions are spawned
        # sequentially — Python's allocator won't return pages to the OS, but at
        # least the objects are freed promptly rather than waiting for a future GC
        # pass.
        gc.collect()

        return result

    async def _execute_loop(
        self,
        step: Step,
        context: dict[str, Any],
        project_path: Path,
        recursion_state: RecursionState,
        recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
    ) -> None:
        """
        Execute a step with foreach iteration.

        Simple, fail-fast implementation per philosophy:
        - No checkpointing (restart on failure)
        - No partial completion (fail-fast)
        - Minimal state tracking
        - Optional parallel execution (all iterations concurrently)

        Args:
            step: Step with foreach field
            context: Current context variables
            project_path: Current project directory
            recursion_state: Recursion tracking state
            orchestrator_config: Optional orchestrator config for spawned sessions
            session_id: Session identifier for cancellation checks

        Raises:
            ValueError: If foreach variable invalid or iteration fails
            SkipRemainingError: If on_error='skip_remaining' and iteration fails
            CancellationRequestedError: If cancellation requested
        """
        # Route to while-loop if step has while_condition
        if step.while_condition:
            await self._execute_while_loop(
                step,
                context,
                project_path,
                recursion_state,
                recipe_path,
                rate_limiter,
                orchestrator_config,
                session_id,
            )
            return

        # Resolve foreach variable (step.foreach is guaranteed non-None by caller)
        assert step.foreach is not None
        items = self._resolve_foreach_variable(step.foreach, context)

        if not isinstance(items, list):
            raise ValueError(
                f"Step '{step.id}': foreach variable must be a list, got {type(items).__name__}"
            )

        if not items:
            # Empty list - skip step execution but still set output variables
            # This prevents "undefined variable" errors in downstream steps
            skipped_steps = context.get("_skipped_steps", [])
            skipped_steps.append(step.id)
            context["_skipped_steps"] = skipped_steps

            # Set collect variable to empty array so downstream steps can check length
            if step.collect:
                context[step.collect] = []

            return

        if len(items) > step.max_iterations:
            raise ValueError(
                f"Step '{step.id}': foreach exceeds max_iterations ({len(items)} > {step.max_iterations})"
            )

        # Get loop variable name
        loop_var = step.as_var or "item"

        # Check for checkpoint if checkpoint_iterations enabled
        start_index = 0
        pre_results: list = []
        if step.checkpoint_iterations and session_id:
            state = self.session_manager.load_state(session_id, project_path)
            foreach_progress = state.get("foreach_progress")
            if foreach_progress and foreach_progress.get("step_id") == step.id:
                start_index = foreach_progress.get("completed_iterations", 0)
                pre_results = foreach_progress.get("collected_results", []) or []
                saved_total = foreach_progress.get("total_items", len(items))
                if saved_total != len(items):
                    logger.warning(
                        "Foreach items count changed (was %d, now %d). "
                        "Resuming from iteration %d — verify item ordering is stable.",
                        saved_total,
                        len(items),
                        start_index,
                    )
                if start_index >= len(items):
                    logger.info(
                        "Foreach '%s': all %d iterations already completed (resume)",
                        step.id,
                        len(items),
                    )

        if step.parallel:
            # Parallel execution: run all iterations concurrently
            results = await self._execute_loop_parallel(
                step,
                context,
                items,
                loop_var,
                project_path,
                recursion_state,
                recipe_path,
                rate_limiter,
                orchestrator_config,
                session_id,
            )
        else:
            # Sequential execution: run iterations one at a time
            results = await self._execute_loop_sequential(
                step,
                context,
                items,
                loop_var,
                project_path,
                recursion_state,
                recipe_path,
                rate_limiter,
                orchestrator_config,
                session_id,
                start_index=start_index,
                pre_results=pre_results,
            )

        # Store results
        if step.collect:
            context[step.collect] = results
        elif step.output and results:
            context[step.output] = results[-1]  # Last iteration result

    async def _execute_loop_sequential(
        self,
        step: Step,
        context: dict[str, Any],
        items: list[Any],
        loop_var: str,
        project_path: Path,
        recursion_state: RecursionState,
        recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
        start_index: int = 0,
        pre_results: list | None = None,
    ) -> list[Any]:
        """Execute loop iterations sequentially."""
        results = list(pre_results) if pre_results else []

        for idx, item in enumerate(items):
            # Skip iterations already completed in a previous run
            if idx < start_index:
                continue

            # Check for cancellation before each iteration
            if session_id and project_path:
                self._check_coordinator_cancellation(session_id, project_path)
                self._check_cancellation(
                    session_id, project_path, current_step=f"{step.id}[{idx}]"
                )

            # Set loop variable in context
            context[loop_var] = item

            try:
                if step.while_steps:
                    # Multi-step foreach body: execute each sub-step per iteration.
                    # Mirrors the while-loop multi-step handling in _execute_while_loop.
                    last_result = await self._execute_sub_steps(
                        step.while_steps,
                        context,
                        project_path,
                        recursion_state,
                        recipe_path,
                        rate_limiter,
                        orchestrator_config,
                        session_id,
                        parent_step_id=step.id,
                    )
                    results.append(last_result)
                else:
                    # Single-step body: execute the step itself per iteration
                    result = await self._execute_single_step_body(
                        step,
                        context,
                        project_path,
                        recursion_state,
                        recipe_path,
                        rate_limiter,
                        orchestrator_config,
                        session_id,
                    )

                    # Process result: unwrap spawn() output and optionally parse JSON
                    result = self._process_step_result(result, step)
                    results.append(result)
            except SkipRemainingError:
                # Propagate skip_remaining
                raise
            except CancellationRequestedError:
                # Propagate cancellation
                raise
            except Exception as e:
                if step.on_error == "continue":
                    logger.warning(
                        "Step '%s' iteration %d failed (on_error=continue): %s",
                        step.id,
                        idx,
                        str(e)[:200],
                    )
                    await self._emit_iteration_failed(step.id, idx, e)
                    results.append(None)
                elif step.on_error == "skip_remaining":
                    raise SkipRemainingError() from e
                else:
                    raise ValueError(
                        f"Step '{step.id}' iteration {idx} failed: {e}"
                    ) from e
            finally:
                # Clean up loop variable (scoped to loop only)
                if loop_var in context:
                    del context[loop_var]

            # Save per-iteration checkpoint so the loop is resumable on restart
            if step.checkpoint_iterations and session_id and project_path:
                self._save_foreach_checkpoint(
                    session_id,
                    project_path,
                    step,
                    idx + 1,
                    results,
                    len(items),
                    context,
                )

        return results

    def _save_foreach_checkpoint(
        self,
        session_id: str,
        project_path: Path,
        step: Step,
        completed_iterations: int,
        results: list[Any],
        total_items: int,
        context: dict[str, Any],
    ) -> None:
        """Save per-iteration checkpoint for a foreach step.

        Writes ``foreach_progress`` into the session state so that a resumed
        run can skip already-completed iterations and restore collected results.

        Args:
            session_id: Active session identifier.
            project_path: Project directory (used to locate state file).
            step: The foreach Step being executed.
            completed_iterations: How many iterations have finished (1-based count).
            results: Accumulated results list at this point in the loop.
            total_items: Total number of items in the foreach list.
            context: Current execution context (snapshot saved alongside progress).
        """
        state = self.session_manager.load_state(session_id, project_path)
        progress: dict[str, Any] = {
            "step_id": step.id,
            "completed_iterations": completed_iterations,
            "total_items": total_items,
        }
        if step.collect:
            progress["collected_results"] = results
            # Warn on large accumulated results to alert users about O(N²) write amplification
            try:
                progress_size = len(json.dumps(progress, ensure_ascii=False))
                if progress_size > _FOREACH_PROGRESS_WARN_BYTES:
                    size_mb = progress_size / (1024 * 1024)
                    logger.warning(
                        "Foreach '%s': checkpoint foreach_progress is %.1f MB "
                        "after %d/%d iterations. This causes O(N\u00b2) write amplification. "
                        "Consider removing 'collect' (use 'output' for last result only) "
                        "or reducing iteration count.",
                        step.id,
                        size_mb,
                        completed_iterations,
                        total_items,
                    )
            except (TypeError, ValueError):
                pass  # Non-serializable results — save_state will surface the error
        state["foreach_progress"] = progress
        # Update context snapshot so a resumed run has the latest variable state
        state["context"] = self._trim_context_for_checkpoint(context)
        self.session_manager.save_state(session_id, project_path, state)

    async def _execute_single_step_body(
        self,
        step: Step,
        context: dict[str, Any],
        project_path: Path,
        recursion_state: RecursionState,
        recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Execute a single step body (agent, recipe, or bash).

        Shared helper used by both sequential and parallel loop executors
        for the single-step-per-iteration case.
        """
        if step.type == "recipe":
            return await self._execute_recipe_step(
                step,
                context,
                project_path,
                recursion_state,
                recipe_path,
                rate_limiter,
                orchestrator_config,
                parent_session_id=session_id,
            )
        elif step.type == "bash":
            bash_result = await self._execute_bash_step(step, context, project_path)
            if step.output_exit_code:
                context[step.output_exit_code] = str(bash_result.exit_code)
            return bash_result.stdout
        else:
            # Agent step - track for recursion limits
            recursion_state.increment_steps()
            return await self.execute_step_with_retry(
                step,
                context,
                rate_limiter,
                orchestrator_config,
                session_id=session_id,
                project_path=project_path,
            )

    async def _execute_sub_steps(
        self,
        sub_steps_data: list[dict[str, Any]],
        context: dict[str, Any],
        project_path: Path,
        recursion_state: RecursionState,
        recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
        parent_step_id: str = "",
    ) -> Any:
        """Execute a list of sub-step dicts in sequence.

        Used as the multi-step body for both foreach and while-loop compound
        steps.  Each sub-step dict is parsed, validated and dispatched.
        Sub-steps that are themselves loops (foreach / while_condition) are
        routed through ``_execute_loop`` so nesting works to arbitrary depth.

        Returns the result of the last executed sub-step.
        """
        from .models import Recipe  # local to avoid circular at module level

        last_result: Any = None
        for sub_step_data in sub_steps_data:
            sub_step = Recipe._parse_step(sub_step_data)
            errors = sub_step.validate()
            if errors:
                raise ValueError(
                    f"Sub-step '{sub_step.id}' (in '{parent_step_id}') "
                    f"validation failed: {'; '.join(errors)}"
                )

            # Evaluate condition on sub-step (skip if false)
            if sub_step.condition:
                resolved_cond = self.substitute_variables(sub_step.condition, context)
                from .expression_evaluator import evaluate_condition

                if not evaluate_condition(resolved_cond, context):
                    continue

            # Route loops through the main loop executor for proper nesting
            if sub_step.foreach or sub_step.while_condition:
                await self._execute_loop(
                    sub_step,
                    context,
                    project_path,
                    recursion_state,
                    recipe_path,
                    rate_limiter,
                    orchestrator_config,
                    session_id,
                )
                # The loop stores its own results in context via output/collect
                if sub_step.output:
                    last_result = context.get(sub_step.output)
                elif sub_step.collect:
                    last_result = context.get(sub_step.collect)
            else:
                sub_result = await self._execute_single_step_body(
                    sub_step,
                    context,
                    project_path,
                    recursion_state,
                    recipe_path,
                    rate_limiter,
                    orchestrator_config,
                    session_id,
                )
                sub_result = self._process_step_result(sub_result, sub_step)
                if sub_step.output:
                    context[sub_step.output] = sub_result
                last_result = sub_result

        return last_result

    async def _execute_loop_parallel(
        self,
        step: Step,
        context: dict[str, Any],
        items: list[Any],
        loop_var: str,
        project_path: Path,
        recursion_state: RecursionState,
        recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
    ) -> list[Any]:
        """
        Execute loop iterations in parallel using asyncio.gather.

        Each iteration gets its own context copy to avoid conflicts.
        Results are returned in the same order as input items.
        Fail-fast: if any iteration fails, the entire step fails.

        Supports bounded parallelism:
        - parallel: true -> unbounded (all at once)
        - parallel: N (int) -> max N concurrent iterations

        Rate limiting is applied via the rate_limiter if configured.
        """
        # Check for cancellation before starting parallel execution
        if session_id and project_path:
            self._check_coordinator_cancellation(session_id, project_path)
            self._check_cancellation(
                session_id, project_path, current_step=f"{step.id}[parallel]"
            )

        # For agent steps, pre-check total steps limit (all will run in parallel)
        if step.type == "agent":
            if (
                recursion_state.total_steps + len(items)
                > recursion_state.max_total_steps
            ):
                raise ValueError(
                    f"Parallel loop would exceed max_total_steps "
                    f"({recursion_state.total_steps} + {len(items)} > {recursion_state.max_total_steps})"
                )
            # Pre-increment for all iterations
            recursion_state.total_steps += len(items)

        # Determine concurrency limit
        # parallel: true -> None (unbounded)
        # parallel: N (int) -> N concurrent
        if step.parallel is True:
            max_concurrent = None
        elif isinstance(step.parallel, int):
            max_concurrent = step.parallel
        else:
            max_concurrent = None  # Shouldn't reach here after validation

        # Create semaphore for bounded concurrency (None = unbounded)
        semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent else None

        # Generate a single group ID shared by all iterations in this parallel batch
        parallel_group_id = str(uuid.uuid4())

        async def execute_iteration(idx: int, item: Any) -> Any:
            """Execute a single iteration with isolated context."""
            # Copy context and set loop variable for this iteration
            # _parallel_group_id marks all spawns in this batch for the navigation graph
            iter_context = {
                **context,
                loop_var: item,
                "_parallel_group_id": parallel_group_id,
            }

            try:
                # Execute based on step type (agent, recipe, or bash)
                if step.type == "recipe":
                    result = await self._execute_recipe_step(
                        step,
                        iter_context,
                        project_path,
                        recursion_state,
                        recipe_path,
                        rate_limiter,
                        orchestrator_config,
                        parent_session_id=session_id,
                    )
                elif step.type == "bash":
                    # Bash steps don't count against agent recursion limits
                    bash_result = await self._execute_bash_step(
                        step, iter_context, project_path
                    )
                    # Store exit code if requested (in iteration context)
                    if step.output_exit_code:
                        iter_context[step.output_exit_code] = str(bash_result.exit_code)
                    result = bash_result.stdout
                else:
                    # Agent step - rate limiting handled inside execute_step_with_retry
                    result = await self.execute_step_with_retry(
                        step,
                        iter_context,
                        rate_limiter,
                        orchestrator_config,
                        session_id=session_id,
                        project_path=project_path,
                    )

                # Process result: unwrap spawn() output and optionally parse JSON
                return self._process_step_result(result, step)
            except SkipRemainingError:
                raise
            except CancellationRequestedError:
                raise
            except Exception as e:
                raise ValueError(f"Step '{step.id}' iteration {idx} failed: {e}") from e

        async def bounded_iteration(idx: int, item: Any) -> Any:
            """Execute iteration with optional semaphore for bounded concurrency."""
            if semaphore:
                async with semaphore:
                    return await execute_iteration(idx, item)
            return await execute_iteration(idx, item)

        # Create tasks for all iterations (semaphore controls actual concurrency)
        tasks = [bounded_iteration(idx, item) for idx, item in enumerate(items)]

        # Run all tasks concurrently with return_exceptions=True.
        # Without it, a single failed iteration raises immediately but does NOT
        # cancel remaining tasks — they continue as orphans and asyncio.gather
        # never returns if any orphaned task hangs. With return_exceptions=True,
        # all iterations run to completion (or timeout) and we handle failures
        # after all are done.
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Separate successes from failures
        results = []
        failures = []
        for idx, result in enumerate(raw_results):
            if isinstance(result, BaseException):
                logger.warning(
                    "Step '%s' iteration %d failed: %s",
                    step.id,
                    idx,
                    str(result)[:200],
                )
                await self._emit_iteration_failed(step.id, idx, result)
                failures.append((idx, result))
                results.append(None)
            else:
                results.append(result)

        if failures:
            if step.on_error == "continue":
                logger.info(
                    "Step '%s': %d/%d iterations succeeded (on_error=continue)",
                    step.id,
                    len(results) - len(failures),
                    len(results),
                )
            else:
                failure_summary = "; ".join(
                    f"iteration {i}: {str(e)[:100]}" for i, e in failures
                )
                raise ValueError(
                    f"Step '{step.id}': {len(failures)}/{len(results)} "
                    f"iterations failed: {failure_summary}"
                )

        return list(results)

    async def _execute_while_loop(
        self,
        step: Step,
        context: dict[str, Any],
        project_path: Path,
        recursion_state: RecursionState,
        recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        session_id: str | None = None,
    ) -> None:
        """Execute a step with while-loop iteration (convergence-based workflow).

        Evaluates while_condition each iteration, executes the step body (or
        while_steps multi-step body), applies update_context mutations, checks
        break_when, and injects loop metadata (_loop_index, _loop_iteration).

        Safety: respects max_while_iterations, cancellation checks between
        iterations, and session checkpointing for resumability.

        Args:
            step: Step with while_condition field set
            context: Current context variables (mutated in place)
            project_path: Current project directory
            recursion_state: Recursion tracking state
            recipe_path: Optional path to recipe file (for sub-recipe resolution)
            rate_limiter: Optional rate limiter for pacing
            orchestrator_config: Optional orchestrator config for spawned sessions
            session_id: Session identifier for cancellation checks

        Raises:
            ValueError: If while_condition evaluation fails or limits exceeded
            SkipRemainingError: If on_error='skip_remaining' and body fails
            CancellationRequestedError: If cancellation requested between iterations
        """
        results = []
        iteration = 0

        try:
            while True:
                # Safety limit check
                if iteration >= step.max_while_iterations:
                    break

                # Check for cancellation before each iteration
                if session_id and project_path:
                    self._check_coordinator_cancellation(session_id, project_path)
                    self._check_cancellation(
                        session_id, project_path, current_step=f"{step.id}[{iteration}]"
                    )

                # Evaluate while_condition with variable substitution
                assert step.while_condition is not None
                resolved_condition = self.substitute_variables(
                    step.while_condition, context
                )
                condition_result = evaluate_condition(resolved_condition, context)
                if not condition_result:
                    break

                # Inject loop metadata before each iteration body
                context["_loop_index"] = iteration
                context["_loop_iteration"] = iteration + 1

                # Emit per-iteration event for convergence dashboards
                await self._show_progress(
                    f"  ↻ {step.id} iteration {iteration + 1}"
                    f" / {step.max_while_iterations}",
                    event_name="recipe:loop_iteration",
                    event_data={
                        "step_id": step.id,
                        "iteration": iteration + 1,
                        "max_iterations": step.max_while_iterations,
                        "context_snapshot": {
                            k: v
                            for k, v in context.items()
                            if k not in ("recipe", "session", "step", "stage")
                            and not k.startswith("_")
                            and isinstance(v, (str, int, float, bool))
                        },
                    },
                )

                try:
                    if step.while_steps:
                        # Multi-step body: execute each sub-step in sequence
                        last_result = await self._execute_sub_steps(
                            step.while_steps,
                            context,
                            project_path,
                            recursion_state,
                            recipe_path,
                            rate_limiter,
                            orchestrator_config,
                            session_id,
                            parent_step_id=step.id,
                        )
                        results.append(last_result)
                    else:
                        # Single-step body: dispatch based on step type
                        result = await self._execute_single_step_body(
                            step,
                            context,
                            project_path,
                            recursion_state,
                            recipe_path,
                            rate_limiter,
                            orchestrator_config,
                            session_id,
                        )

                        result = self._process_step_result(result, step)
                        results.append(result)
                        if step.output:
                            context[step.output] = result

                except SkipRemainingError:
                    raise
                except CancellationRequestedError:
                    raise
                except Exception as e:
                    raise ValueError(
                        f"Step '{step.id}' iteration {iteration} failed: {e}"
                    ) from e

                # Apply update_context mutations after each iteration body
                if step.update_context:
                    for key, value in step.update_context.items():
                        resolved_value = self.substitute_variables(value, context)
                        context[key] = resolved_value

                # Evaluate break_when after each iteration
                if step.break_when:
                    try:
                        resolved_break = self.substitute_variables(
                            step.break_when, context
                        )
                        if evaluate_condition(resolved_break, context):
                            break
                    except ExpressionError as e:
                        await self._show_progress(
                            f"Step '{step.id}': break_when expression error: {e}",
                            level="warning",
                        )

                iteration += 1

        finally:
            # Clean up loop metadata from context after loop exits by any path
            context.pop("_loop_index", None)
            context.pop("_loop_iteration", None)

        # Emit loop completion event
        await self._show_progress(
            f"  ✓ {step.id} completed after {iteration} iteration(s)",
            event_name="recipe:loop_complete",
            event_data={
                "step_id": step.id,
                "iterations_completed": iteration,
                "max_iterations": step.max_while_iterations,
                "results_count": len(results),
            },
        )

        # Store results
        if step.collect:
            context[step.collect] = results
        elif step.output and results:
            context[step.output] = results[-1]

    async def _execute_recipe_step(
        self,
        step: Step,
        context: dict[str, Any],
        project_path: Path,
        recursion_state: RecursionState,
        parent_recipe_path: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
        parent_session_id: str
        | None = None,  # optional: keyword-passed at call sites per Python convention
    ) -> dict[str, Any]:
        """
        Execute a recipe composition step by loading and running a sub-recipe.

        Args:
            step: Step with type="recipe" and recipe path
            context: Current context variables
            project_path: Current project directory
            recursion_state: Recursion tracking state
            parent_recipe_path: Path to parent recipe file (for relative resolution)
            rate_limiter: Optional rate limiter (inherited from parent recipe)
            orchestrator_config: Optional orchestrator config (inherited from parent recipe)
            parent_session_id: Parent's session ID for cancellation checks

        Returns:
            Sub-recipe's final context dict
        """
        assert step.recipe is not None, "Recipe step must have recipe path"

        # Substitute variables in recipe path (e.g., {{test_recipe}} in foreach loops)
        recipe_path_str = self.substitute_variables(step.recipe, context)

        # Handle @mention paths (e.g., @recipes:examples/code-review.yaml)
        if recipe_path_str.startswith("@"):
            mention_resolver = self.coordinator.get_capability("mention_resolver")
            if mention_resolver is None:
                raise FileNotFoundError(
                    f"Cannot resolve @mention path '{recipe_path_str}': mention_resolver capability not available"
                )
            sub_recipe_path = mention_resolver.resolve(recipe_path_str)
            if sub_recipe_path is None:
                raise FileNotFoundError(
                    f"Sub-recipe @mention not found: {recipe_path_str}"
                )
        else:
            # Resolve sub-recipe path relative to parent recipe's directory (not project_path)
            # This allows recipes to reference sibling recipes naturally
            if parent_recipe_path is not None:
                base_dir = parent_recipe_path.parent
            else:
                base_dir = project_path

            sub_recipe_path = base_dir / recipe_path_str
            if not sub_recipe_path.exists():
                raise FileNotFoundError(f"Sub-recipe not found: {sub_recipe_path}")

        # Load sub-recipe
        sub_recipe = Recipe.from_yaml(sub_recipe_path)

        # Build sub-recipe context from step's context field (with variable substitution)
        # Context isolation: sub-recipe gets ONLY explicitly passed context
        sub_context: dict[str, Any] = {}
        if step.step_context:
            for key, value in step.step_context.items():
                # Recursively substitute variables in all values (strings, dicts, lists)
                sub_context[key] = self._substitute_variables_recursive(value, context)

        # Create child recursion state (with step-level override if present)
        child_state = recursion_state.enter_recipe(sub_recipe.name, step.recursion)

        # Check for a saved child session to resume (from a previous approval gate pause)
        child_session_key = f"_child_session_{step.id}"
        saved_child_session_id = context.get(child_session_key)

        # Execute sub-recipe recursively
        # Note: rate_limiter and orchestrator_config are inherited from parent (sub-recipes cannot override)
        # parent_session_id is passed so sub-recipes can check for cancellation
        try:
            result = await self.execute_recipe(
                recipe=sub_recipe,
                context_vars=sub_context,
                project_path=project_path,
                session_id=saved_child_session_id,  # Resume child session if saved, else None
                recipe_path=sub_recipe_path,
                recursion_state=child_state,
                rate_limiter=rate_limiter,  # Inherit parent's rate limiter
                orchestrator_config=orchestrator_config,  # Inherit parent's orchestrator config
                parent_session_id=parent_session_id,  # For cancellation checks
            )
        except ApprovalGatePausedError as e:
            # Save child's session ID so we can resume it on the next attempt
            context[child_session_key] = e.session_id
            raise

        # On successful completion, clean up the saved child session key
        context.pop(child_session_key, None)

        # Propagate total steps back to parent state
        recursion_state.total_steps = child_state.total_steps

        # --- Fix: trim sub-recipe return value (memory fix) ---
        # execute_recipe() returns the *entire* sub-recipe context dict, which
        # includes every intermediate variable from every step.  Returning that
        # whole dict to the parent causes it to accumulate in the parent context
        # (e.g. via step.output or foreach collect lists), leading to unbounded
        # memory growth proportional to iterations × sub-recipe step count.
        #
        # Instead, return only the keys that the sub-recipe *added* — i.e. keys
        # not present in the input (sub_context) and not injected internally by
        # execute_recipe() itself (_RECIPE_INTERNAL_KEYS).  This is what the
        # parent recipe actually cares about; the rest is implementation detail
        # of the sub-recipe and should not escape its scope.
        input_key_set = set(sub_context.keys()) | _RECIPE_INTERNAL_KEYS
        output_delta = {
            k: v
            for k, v in result.items()
            if k not in input_key_set and not k.startswith("_child_session_")
        }

        # Give the cyclic GC a chance to release PyO3 / Rust-backed objects and
        # break reference cycles from the completed sub-recipe session before the
        # next iteration of a foreach loop.
        gc.collect()

        return output_delta

    def _resolve_foreach_variable(self, foreach: str, context: dict[str, Any]) -> Any:
        """
        Resolve {{variable}} to its value.

        Args:
            foreach: String containing {{variable}} reference
            context: Current context variables

        Returns:
            The resolved value

        Raises:
            ValueError: If variable syntax invalid or undefined
        """
        pattern = r"\{\{(\w+(?:\.\w+)*)\}\}"
        match = re.match(pattern, foreach.strip())
        if not match:
            raise ValueError(f"Invalid foreach syntax: {foreach}")

        var_path = match.group(1)
        parts = var_path.split(".")
        value = context
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise ValueError(f"Undefined variable in foreach: {foreach}")
        return value

    def _resolve_dotted_path(self, var_ref: str, context: dict[str, Any]) -> Any:
        """Resolve a dotted variable reference against a context dict.

        Walks dot-separated segments through nested dicts and returns the
        native Python value at the leaf.

        Args:
            var_ref: Dotted path, e.g. ``"current_task.task_id"``.
            context: Root context dict.

        Returns:
            The value at the resolved path (preserving native type).

        Raises:
            ValueError: If a key is missing or an intermediate value is not a dict.
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

    def _substitute_variables_recursive(
        self, value: Any, context: dict[str, Any]
    ) -> Any:
        """
        Recursively substitute {{variable}} references in nested structures.

        When a string consists of exactly one whole-variable reference (e.g.
        ``"{{current_task}}"`` or ``"{{a.b.c}}"`` with no surrounding text),
        the native Python value is returned instead of a string — preserving
        dicts, lists, ints, booleans, etc.  This prevents dict→JSON-string
        coercion when passing structured context values to sub-recipes via
        foreach loops and fixes the "Cannot access 'x' on str, not dict"
        error that occurred when a dict variable was forwarded through a
        ``context:`` block.

        Handles:
        - Strings: Type-preserving substitution for whole-variable refs;
          normal string substitution for composite/partial strings.
        - Dicts: Recursively process all values
        - Lists: Recursively process all items
        - Other types: Pass through unchanged

        Args:
            value: Value to process (string, dict, list, or other)
            context: Dict with variable values

        Returns:
            Value with all variables substituted, preserving native types
            for whole-variable string references.
        """
        if isinstance(value, str):
            # When the string is exactly one whole-variable reference (e.g.
            # "{{current_task}}" or "{{a.b.c}}" — optional surrounding
            # whitespace only), resolve and return the native Python object so
            # that dicts, lists, ints, etc. are NOT serialised to JSON strings.
            whole_var = re.fullmatch(r"\s*\{\{(\w+(?:\.\w+)*)\}\}\s*", value)
            if whole_var:
                var_ref = whole_var.group(1)
                if "." in var_ref:
                    return self._resolve_dotted_path(var_ref, context)
                else:
                    # Simple (non-dotted) variable reference
                    if var_ref in context:
                        return context[var_ref]  # native Python type preserved
                    # Variable not found — fall through to substitute_variables
                    # which will raise a descriptive ValueError.
            # Composite string (surrounding text, multiple refs, or unknown var)
            return self.substitute_variables(value, context)
        elif isinstance(value, dict):
            return {
                k: self._substitute_variables_recursive(v, context)
                for k, v in value.items()
            }
        elif isinstance(value, list):
            return [
                self._substitute_variables_recursive(item, context) for item in value
            ]
        else:
            # Numbers, booleans, None, etc. - pass through unchanged
            return value

    def substitute_variables(self, template: str, context: dict[str, Any]) -> str:
        """
        Replace {{variable}} references with context values.

        Args:
            template: String with {{variable}} placeholders
            context: Dict with variable values

        Returns:
            String with variables substituted

        Raises:
            ValueError if variable undefined
        """
        # Support multi-level access: {{a.b.c.d}} - use * not ? for unlimited depth
        pattern = r"\{\{(\w+(?:\.\w+)*)\}\}"

        def replace(match: re.Match) -> str:
            var_ref = match.group(1)

            # Handle nested references (recipe.name, session.id, etc.)
            if "." in var_ref:
                value = self._resolve_dotted_path(var_ref, context)
                if isinstance(value, bool):
                    return "true" if value else "false"
                if isinstance(value, (dict, list)):
                    return json.dumps(value)
                return str(value)

            # Handle direct references
            if var_ref not in context:
                available = ", ".join(sorted(context.keys()))
                raise ValueError(
                    f"Undefined variable: {{{{{var_ref}}}}}. Available variables: {available}"
                )

            # Use json.dumps for dict/list to produce valid JSON, not Python repr
            value = context[var_ref]
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return str(value)

        return re.sub(pattern, replace, template)

    async def _execute_bash_step(
        self,
        step: Step,
        context: dict[str, Any],
        project_path: Path,
    ) -> BashResult:
        """
        Execute a bash step by running shell command directly.

        No LLM overhead - command is executed via subprocess.

        Args:
            step: Step with type="bash" and command
            context: Current context variables
            project_path: Current project directory

        Returns:
            BashResult with stdout, stderr, and exit_code

        Raises:
            ValueError: If command fails and on_error="fail"
            asyncio.TimeoutError: If command exceeds timeout
        """
        assert step.command is not None, "Bash step must have command"

        # Substitute variables in command
        command = self.substitute_variables(step.command, context)

        # Determine working directory
        if step.cwd:
            cwd = Path(self.substitute_variables(step.cwd, context))
            if not cwd.is_absolute():
                cwd = project_path / cwd
            if not cwd.exists():
                raise ValueError(f"Step '{step.id}': cwd does not exist: {cwd}")
            if not cwd.is_dir():
                raise ValueError(f"Step '{step.id}': cwd is not a directory: {cwd}")
        else:
            cwd = project_path

        # Build environment variables
        env = os.environ.copy()
        # Inject current Python interpreter so recipe bash heredocs can use
        # ${AMPLIFIER_PYTHON:-python3} to reliably reach the Amplifier venv's
        # Python (which has recipe modules like recipe_to_dot installed), rather
        # than the bare `python3` that resolves to the system Python.
        env["AMPLIFIER_PYTHON"] = _resolve_amplifier_python()
        if step.env:
            for key, value in step.env.items():
                # Substitute variables in env values
                env[key] = self.substitute_variables(str(value), context)

        # Execute command with timeout.
        # Spawn a resolved bash explicitly rather than the platform default
        # shell: recipe bash steps may use bash-specific features like
        # pipefail, &> redirects, brace expansion and arrays, and /bin/sh is
        # often dash on Ubuntu, which lacks these. See _resolve_bash() for how
        # this is resolved per-platform (and why WSL is rejected on Windows).
        try:
            process = await asyncio.create_subprocess_exec(
                _resolve_bash(),
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=step.timeout,
                )
            except asyncio.TimeoutError:
                # Kill the process on timeout
                process.kill()
                await process.wait()
                raise ValueError(
                    f"Step '{step.id}': command timed out after {step.timeout}s"
                ) from None

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = process.returncode or 0

            result = BashResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

            # Check for non-zero exit code
            if exit_code != 0:
                error_msg = (
                    f"Step '{step.id}': command failed with exit code {exit_code}"
                )
                if stderr.strip():
                    error_msg += f"\nstderr: {stderr.strip()}"

                if step.on_error == "fail":
                    raise ValueError(error_msg)
                elif step.on_error == "skip_remaining":
                    raise SkipRemainingError(error_msg)
                # For "continue", return the result as-is

            return result

        except OSError as e:
            raise ValueError(f"Step '{step.id}': failed to execute command: {e}") from e
