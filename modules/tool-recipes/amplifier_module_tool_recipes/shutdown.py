"""Bounded shutdown for the recipes tool's run path.

WHY THIS EXISTS (recipes-8sr). A v2 recipe completed every step, wrote its
outputs and checkpointed -- and then the ``amplifier tool invoke recipes ...
operation=execute`` process never returned. No result, no status, no error:
23 threads asleep and five sockets in CLOSE-WAIT, until SIGTERM. The recipe's
own work was correct and unrecoverable, because nothing downstream could learn
it had happened.

A hang after the last step is strictly worse than a failure. A failure names
itself; a hang is indistinguishable from slow work, so an automated caller
waits forever and a CI job burns its whole timeout.

WHAT THIS MODULE GUARANTEES. Whatever the recipe engine leaves running when it
returns -- an orphaned ``asyncio`` task, a telemetry exporter's thread, an
HTTP client's reader -- the tool waits a *bounded* time for it and then reports
by name what did not drain. The run's own outcome is logged before the wait
begins, so the outcome survives even a stage that hangs afterwards.

WHAT IT DELIBERATELY DOES NOT DO. It does not promise the *process* exits. A
non-daemon thread that ignores its own shutdown still blocks interpreter exit,
and a Python thread cannot be killed from outside; ``Thread.daemon`` cannot be
flipped after ``start()``. What this module converts is *silence* into a named
warning: "completed, and <this> did not drain" instead of nothing at all. The
name is what makes the residual owner findable -- see the module's tests and
``docs/TROUBLESHOOTING.md``.

Ownership note: this bound lives in the tool because the tool is what promises
a caller an answer. It is not a substitute for closing whatever leaks; it is
the backstop that keeps a leak from costing the caller its result.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from dataclasses import field

logger = logging.getLogger(__name__)

#: Default ceiling, in seconds, on the post-run drain. Config key
#: ``shutdown_drain_timeout``.
DEFAULT_DRAIN_TIMEOUT_S = 30.0

#: Of the drain budget, how long a cancelled task gets to actually finish
#: before it is named and abandoned. Cancellation is not completion: a task
#: whose ``finally`` blocks on a dead socket ignores it entirely.
_CANCEL_GRACE_S = 5.0

#: Worker-pool threads, by name prefix. These are library-owned pools --
#: asyncio's default ``ThreadPoolExecutor`` and anyio's ``to_thread`` workers --
#: that park IDLE for the life of the process and are reaped by their own
#: ``atexit`` shutdown. They are not this run's work and they do not hold the
#: process open.
#:
#: Measured, not assumed: without this exclusion every clean run of a real
#: recipe reported ``thread 'asyncio_0' (non-daemon), thread 'AnyIO worker
#: thread' (non-daemon)`` and paid the FULL drain timeout waiting for pools
#: that never finish by design -- turning a 6-second teardown into a 30-second
#: one and printing a warning on every successful run. A warning that fires
#: every time is a warning nobody reads, which would have buried the real one
#: this module exists to surface.
#:
#: They are still counted and logged at DEBUG (``DrainReport.pooled``), so the
#: information is available if a pool thread ever IS the problem.
_POOL_THREAD_PREFIXES = (
    "asyncio_",  # asyncio's default executor
    "AnyIO worker thread",  # anyio.to_thread
    "ThreadPoolExecutor-",  # concurrent.futures
    "ProcessPoolExecutor-",
)


def _is_pool_thread(thread: threading.Thread) -> bool:
    return any(thread.name.startswith(p) for p in _POOL_THREAD_PREFIXES)


def resolve_drain_timeout(value: object) -> float:
    """Read the ``shutdown_drain_timeout`` config value, or refuse it.

    Refused rather than silently defaulted: a bound the operator believes they
    set, and which is quietly ignored, is indistinguishable from no bound at
    all -- the same failure this module exists to end.

    ``0`` is a legitimate setting and means "do not wait": report immediately
    whatever is still running. Negative values and non-numbers are errors.
    """
    if value is None:
        return DEFAULT_DRAIN_TIMEOUT_S
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"shutdown_drain_timeout must be a number of seconds, got "
            f"{value!r} ({type(value).__name__}). Use 0 to report without waiting."
        )
    if value < 0:
        raise ValueError(
            f"shutdown_drain_timeout must be >= 0, got {value!r}. "
            "Use 0 to report without waiting."
        )
    return float(value)


def _task_name(task: asyncio.Task) -> str:
    """A name a human can act on: the task's name and the coroutine behind it."""
    try:
        name = task.get_name()
    except Exception:  # pragma: no cover - exotic Task subclasses
        name = repr(task)
    coro = getattr(task, "get_coro", lambda: None)()
    qualname = getattr(coro, "__qualname__", None)
    if qualname:
        return f"task {name!r} ({qualname})"
    return f"task {name!r}"


def _thread_name(thread: threading.Thread) -> str:
    return f"thread {thread.name!r} (non-daemon)"


@dataclass(frozen=True)
class DrainReport:
    """What the bounded wait found.

    ``undrained`` is the payload that matters: the names of everything the
    recipe run started and left running past the bound. Empty means the run
    left nothing behind.
    """

    waited_seconds: float = 0.0
    drained: tuple[str, ...] = ()
    undrained: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_S
    #: Library-owned worker-pool threads seen but deliberately not waited on
    #: (see ``_POOL_THREAD_PREFIXES``). Recorded, never warned about.
    pooled: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.undrained

    def as_warning(self) -> dict[str, object]:
        """The machine-readable form attached beside a tool result."""
        return {
            "message": (
                "The recipe finished, but background work started during the run "
                f"did not drain within {self.timeout_seconds:g}s and was abandoned: "
                + ", ".join(self.undrained)
                + ". The run's own result above is complete and correct; the "
                "process may still be held open by the work named here."
            ),
            "timeout_seconds": self.timeout_seconds,
            "waited_seconds": round(self.waited_seconds, 3),
            "undrained": list(self.undrained),
        }


@dataclass
class BackgroundBaseline:
    """What was already running before a recipe run started.

    Captured so the drain can tell *this run's* leftovers from the host
    session's own long-lived machinery -- cancelling the latter would break the
    caller that is still using it.
    """

    task_ids: frozenset[int] = field(default_factory=frozenset)
    thread_idents: frozenset[int] = field(default_factory=frozenset)

    @classmethod
    def capture(cls) -> "BackgroundBaseline":
        try:
            tasks = frozenset(id(t) for t in asyncio.all_tasks())
        except RuntimeError:  # no running loop (sync caller)
            tasks = frozenset()
        threads = frozenset(
            t.ident for t in threading.enumerate() if t.ident is not None
        )
        return cls(task_ids=tasks, thread_idents=threads)

    def _new_tasks(self) -> list[asyncio.Task]:
        try:
            current = asyncio.current_task()
            alive = asyncio.all_tasks()
        except RuntimeError:  # pragma: no cover - drain always runs in a loop
            return []
        return [
            t
            for t in alive
            if t is not current and id(t) not in self.task_ids and not t.done()
        ]

    def _new_threads(self) -> tuple[list[threading.Thread], list[threading.Thread]]:
        """New non-daemon threads, split into (this run's, library pools').

        Only the first list is waited on or warned about -- see
        ``_POOL_THREAD_PREFIXES`` for why joining a pool thread is a wait that
        can never end well.
        """
        current = threading.current_thread()
        fresh = [
            t
            for t in threading.enumerate()
            if t is not current
            and not t.daemon
            and t.ident is not None
            and t.ident not in self.thread_idents
            and t.is_alive()
        ]
        pooled = [t for t in fresh if _is_pool_thread(t)]
        owned = [t for t in fresh if not _is_pool_thread(t)]
        return owned, pooled

    async def drain(
        self, timeout: float = DEFAULT_DRAIN_TIMEOUT_S
    ) -> DrainReport:
        """Wait at most ``timeout`` seconds for this run's leftovers.

        ``timeout`` bounds the drain as a WHOLE, not each item: a run that
        leaves ten stuck tasks behind must not cost ten timeouts. The budget is
        split in two, so the ceiling the operator configured is the ceiling
        they get:

        * a *natural* phase, where leftovers are simply awaited (and threads
          joined) on the chance they are merely slow, and
        * a *grace* phase reserved out of the same budget, where whatever is
          still pending is cancelled and given a last moment to unwind --
          because a task that respects cancellation dies at once, and one that
          does not is exactly what the caller needs named.

        Threads are only ever *joined*. There is no way to kill a Python
        thread, so the honest outcomes are "it finished" or "here is its name".
        """
        started = time.monotonic()
        budget = max(float(timeout), 0.0)
        # Reserved for the cancel phase; never more than half the budget, so a
        # small configured bound still spends most of itself waiting honestly.
        grace_reserve = min(_CANCEL_GRACE_S, budget / 2.0)
        natural_deadline = started + max(budget - grace_reserve, 0.0)

        def natural_remaining() -> float:
            return max(natural_deadline - time.monotonic(), 0.0)

        pending_tasks = self._new_tasks()
        pending_threads, pool_threads = self._new_threads()
        pooled = tuple(_thread_name(t) for t in pool_threads)
        if pooled:
            logger.debug(
                "Not waiting on library worker-pool thread(s): %s", ", ".join(pooled)
            )
        watched = [_task_name(t) for t in pending_tasks]
        watched += [_thread_name(t) for t in pending_threads]

        if not pending_tasks and not pending_threads:
            return DrainReport(
                waited_seconds=0.0, timeout_seconds=float(timeout), pooled=pooled
            )

        if pending_tasks and natural_remaining() > 0:
            await asyncio.wait(pending_tasks, timeout=natural_remaining())

        for thread in pending_threads:
            if not thread.is_alive():
                continue
            left = natural_remaining()
            if left <= 0:
                break
            thread.join(timeout=left)

        stuck_tasks = [t for t in pending_tasks if not t.done()]
        if stuck_tasks and grace_reserve > 0:
            for task in stuck_tasks:
                task.cancel()
            await asyncio.wait(stuck_tasks, timeout=grace_reserve)
        elif stuck_tasks:
            for task in stuck_tasks:
                task.cancel()

        undrained = [_task_name(t) for t in pending_tasks if not t.done()]
        undrained += [_thread_name(t) for t in pending_threads if t.is_alive()]
        undrained_set = set(undrained)
        drained = [name for name in watched if name not in undrained_set]

        return DrainReport(
            waited_seconds=time.monotonic() - started,
            drained=tuple(drained),
            undrained=tuple(undrained),
            timeout_seconds=float(timeout),
            pooled=pooled,
        )


def attach_shutdown_warning(result: object, report: DrainReport) -> object:
    """Attach the drain warning *beside* a tool result's payload.

    Same mechanism, and same reason, as ``runner_adapter.label_execution_mode``:
    ``ToolResult``'s serialized payload (``success``/``output``/``error``) is
    what the legacy-compat baselines pin byte-for-byte, so a diagnostic must
    ride alongside it rather than inside it.
    """
    if report.clean:
        return result
    try:
        object.__setattr__(result, "shutdown_warning", report.as_warning())
    except (AttributeError, TypeError):  # pragma: no cover - exotic result types
        logger.debug("Could not attach shutdown_warning to %r", type(result))
    return result


def shutdown_warning_of(result: object) -> dict[str, object] | None:
    """Read back the warning set by :func:`attach_shutdown_warning`."""
    warning = getattr(result, "shutdown_warning", None)
    return warning if isinstance(warning, dict) else None
