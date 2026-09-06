"""The post-run drain is bounded, and says what it abandoned (recipes-8sr).

The defect these pin: a v2 recipe finished every step and wrote its outputs,
and the ``amplifier tool invoke recipes ... operation=execute`` process then
slept for 28 minutes with five sockets in CLOSE-WAIT, emitting nothing at all.
The run had succeeded; no caller could ever learn it.

So the property under test is not "nothing is ever left running" -- a Python
thread cannot be killed from outside, and pretending otherwise would be the
same lie in a new place. It is: **the wait is bounded, and what outlived the
bound is named.** "Completed, and <this> did not drain" is diagnosable.
Silence is not.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from amplifier_module_tool_recipes.shutdown import DEFAULT_DRAIN_TIMEOUT_S
from amplifier_module_tool_recipes.shutdown import BackgroundBaseline
from amplifier_module_tool_recipes.shutdown import DrainReport
from amplifier_module_tool_recipes.shutdown import attach_shutdown_warning
from amplifier_module_tool_recipes.shutdown import resolve_drain_timeout
from amplifier_module_tool_recipes.shutdown import shutdown_warning_of


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _unstoppable(released: threading.Event) -> None:
    """A task that ignores cancellation until explicitly released.

    This is the shape that makes a bound necessary at all: ``task.cancel()`` is
    a request, not a kill. A task whose cleanup blocks on a dead socket swallows
    it exactly like this.
    """
    while not released.is_set():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            if released.is_set():
                raise
            continue


async def _finish_task(task: asyncio.Task, released: threading.Event) -> None:
    """Let a deliberately-stuck task die, so the test suite leaves none behind."""
    released.set()
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


# ---------------------------------------------------------------------------
# Config: the bound is read, or refused -- never silently ignored
# ---------------------------------------------------------------------------


def test_default_timeout_is_thirty_seconds():
    assert resolve_drain_timeout(None) == DEFAULT_DRAIN_TIMEOUT_S
    assert DEFAULT_DRAIN_TIMEOUT_S == 30.0


@pytest.mark.parametrize("value,expected", [(0, 0.0), (5, 5.0), (2.5, 2.5)])
def test_valid_timeouts_are_accepted(value, expected):
    assert resolve_drain_timeout(value) == expected


@pytest.mark.parametrize("value", [-1, "30", "", True, False, object()])
def test_invalid_timeout_is_refused_not_defaulted(value):
    """A bound the operator believes they set, silently ignored, is no bound."""
    with pytest.raises(ValueError):
        resolve_drain_timeout(value)


def test_config_key_is_accepted_by_the_adapter_config_check():
    from amplifier_module_tool_recipes.runner_adapter import ACCEPTED_CONFIG_KEYS
    from amplifier_module_tool_recipes.runner_adapter import check_adapter_config

    assert "shutdown_drain_timeout" in ACCEPTED_CONFIG_KEYS
    check_adapter_config({"shutdown_drain_timeout": 5})  # does not raise


# ---------------------------------------------------------------------------
# Drain: the clean case costs nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_leftovers_drains_clean_and_instantly():
    baseline = BackgroundBaseline.capture()
    started = time.monotonic()
    report = await baseline.drain(timeout=30)
    assert report.clean
    assert report.undrained == ()
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_a_slow_but_finite_task_is_waited_for_not_abandoned():
    baseline = BackgroundBaseline.capture()
    task = asyncio.create_task(asyncio.sleep(0.2), name="slow-but-finite")

    report = await baseline.drain(timeout=10)

    assert report.clean, report.undrained
    assert task.done()
    assert any("slow-but-finite" in name for name in report.drained)


@pytest.mark.asyncio
async def test_a_cancellable_task_is_cancelled_and_counted_as_drained():
    baseline = BackgroundBaseline.capture()
    task = asyncio.create_task(asyncio.sleep(3600), name="cancellable")

    report = await baseline.drain(timeout=0.4)

    assert task.done()
    assert report.clean, report.undrained


@pytest.mark.asyncio
async def test_work_already_running_before_the_run_is_left_alone():
    """The host session's own machinery is not this run's to cancel."""
    pre_existing = asyncio.create_task(asyncio.sleep(3600), name="host-machinery")
    await asyncio.sleep(0)  # let it register

    baseline = BackgroundBaseline.capture()
    report = await baseline.drain(timeout=0.2)

    assert report.clean
    assert not pre_existing.done()
    assert not pre_existing.cancelled()

    pre_existing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pre_existing


# ---------------------------------------------------------------------------
# Drain: the bound actually binds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_task_that_ignores_cancellation_is_named_and_abandoned():
    released = threading.Event()
    baseline = BackgroundBaseline.capture()
    task = asyncio.create_task(_unstoppable(released), name="wont-stop")

    started = time.monotonic()
    report = await baseline.drain(timeout=0.6)
    elapsed = time.monotonic() - started

    assert not report.clean
    assert any("wont-stop" in name for name in report.undrained)
    # The whole point: the wait ENDED. Bounded by the configured ceiling.
    assert elapsed <= 0.6 + 0.5, elapsed

    await _finish_task(task, released)


@pytest.mark.asyncio
async def test_many_stuck_tasks_cost_one_timeout_not_one_each():
    released = threading.Event()
    baseline = BackgroundBaseline.capture()
    tasks = [
        asyncio.create_task(_unstoppable(released), name=f"stuck-{i}")
        for i in range(5)
    ]

    started = time.monotonic()
    report = await baseline.drain(timeout=0.6)
    elapsed = time.monotonic() - started

    assert len(report.undrained) == 5
    assert elapsed <= 0.6 + 0.5, elapsed

    for task in tasks:
        await _finish_task(task, released)


@pytest.mark.asyncio
async def test_timeout_zero_reports_immediately_without_waiting():
    released = threading.Event()
    baseline = BackgroundBaseline.capture()
    task = asyncio.create_task(_unstoppable(released), name="no-wait")

    started = time.monotonic()
    report = await baseline.drain(timeout=0)
    elapsed = time.monotonic() - started

    assert not report.clean
    assert any("no-wait" in name for name in report.undrained)
    assert elapsed < 0.3, elapsed

    await _finish_task(task, released)


@pytest.mark.asyncio
async def test_a_non_daemon_thread_that_outlives_the_bound_is_named():
    """A thread cannot be killed. Naming it is the whole remedy available."""
    stop = threading.Event()
    baseline = BackgroundBaseline.capture()
    thread = threading.Thread(
        target=stop.wait, name="exporter-flush", daemon=False
    )
    thread.start()

    started = time.monotonic()
    report = await baseline.drain(timeout=0.5)
    elapsed = time.monotonic() - started

    assert not report.clean
    assert any("exporter-flush" in name for name in report.undrained)
    assert any("non-daemon" in name for name in report.undrained)
    assert elapsed <= 0.5 + 0.5, elapsed

    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize(
    "name", ["asyncio_0", "AnyIO worker thread", "ThreadPoolExecutor-3_1"]
)
@pytest.mark.asyncio
async def test_library_worker_pool_threads_are_recorded_but_never_waited_on(name):
    """The false positive that would have made the warning worthless.

    Measured on a real recipe run: every clean run reported ``asyncio_0`` and
    ``AnyIO worker thread`` and paid the FULL 30s drain waiting for pools that
    park idle by design. A warning that fires on every success is a warning
    nobody reads.
    """
    stop = threading.Event()
    baseline = BackgroundBaseline.capture()
    thread = threading.Thread(target=stop.wait, name=name, daemon=False)
    thread.start()

    started = time.monotonic()
    report = await baseline.drain(timeout=10)
    elapsed = time.monotonic() - started

    assert report.clean, report.undrained
    assert any(name in entry for entry in report.pooled)
    assert elapsed < 0.5, elapsed  # it did NOT wait out the 10s budget

    stop.set()
    thread.join(timeout=5)


@pytest.mark.asyncio
async def test_a_non_daemon_thread_that_finishes_in_time_is_not_reported():
    stop = threading.Event()
    baseline = BackgroundBaseline.capture()
    thread = threading.Thread(target=stop.wait, name="quick", daemon=False)
    thread.start()
    threading.Timer(0.1, stop.set).start()

    report = await baseline.drain(timeout=10)

    assert report.clean, report.undrained
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# The warning that replaces the silence
# ---------------------------------------------------------------------------


def test_warning_names_what_did_not_drain():
    report = DrainReport(
        waited_seconds=30.1,
        undrained=("task 'exporter' (post_event)",),
        timeout_seconds=30.0,
    )
    warning = report.as_warning()
    assert "exporter" in warning["message"]
    assert "30" in warning["message"]
    assert warning["undrained"] == ["task 'exporter' (post_event)"]


def test_a_clean_drain_attaches_no_warning():
    class _Result:
        pass

    result = _Result()
    attach_shutdown_warning(result, DrainReport())
    assert shutdown_warning_of(result) is None


def test_an_unclean_drain_attaches_a_readable_warning():
    class _Result:
        pass

    result = _Result()
    attach_shutdown_warning(
        result, DrainReport(undrained=("thread 'flush' (non-daemon)",))
    )
    warning = shutdown_warning_of(result)
    assert warning is not None
    assert warning["undrained"] == ["thread 'flush' (non-daemon)"]


# ---------------------------------------------------------------------------
# The tool itself: `execute` returns, bounded, whatever the run left running
# ---------------------------------------------------------------------------


def _tool(config=None):
    from amplifier_module_tool_recipes import RecipesTool

    return RecipesTool(MagicMock(), MagicMock(), MagicMock(), config or {})


@pytest.mark.asyncio
async def test_execute_returns_within_the_bound_when_the_run_leaves_a_stuck_task():
    """The recipes-8sr regression, at the seam that owes the caller an answer."""
    from amplifier_core import ToolResult

    released = threading.Event()
    holder: dict[str, asyncio.Task] = {}
    tool = _tool({"shutdown_drain_timeout": 0.6})

    async def _run(_input):
        holder["task"] = asyncio.create_task(
            _unstoppable(released), name="orphaned-exporter"
        )
        return ToolResult(success=True, output={"status": "completed"})

    tool._execute_recipe = _run  # type: ignore[method-assign]

    started = time.monotonic()
    result = await tool.execute({"operation": "execute"})
    elapsed = time.monotonic() - started

    # It answered at all -- and inside the bound.
    assert result.success is True
    assert result.output == {"status": "completed"}
    assert elapsed <= 0.6 + 0.6, elapsed

    # And it said what it walked away from.
    warning = shutdown_warning_of(result)
    assert warning is not None
    assert any("orphaned-exporter" in name for name in warning["undrained"])

    await _finish_task(holder["task"], released)


@pytest.mark.asyncio
async def test_execute_attaches_no_warning_when_the_run_leaves_nothing():
    from amplifier_core import ToolResult

    tool = _tool()

    async def _run(_input):
        return ToolResult(success=True, output={"status": "completed"})

    tool._execute_recipe = _run  # type: ignore[method-assign]

    result = await tool.execute({"operation": "execute"})

    assert result.success is True
    assert shutdown_warning_of(result) is None


@pytest.mark.asyncio
async def test_the_warning_rides_beside_the_payload_not_inside_it():
    """``model_dump()`` is what the legacy-compat baselines pin, byte for byte."""
    from amplifier_core import ToolResult

    released = threading.Event()
    holder: dict[str, asyncio.Task] = {}
    tool = _tool({"shutdown_drain_timeout": 0.3})

    async def _run(_input):
        holder["task"] = asyncio.create_task(_unstoppable(released), name="leftover")
        return ToolResult(success=True, output={"status": "completed"})

    tool._execute_recipe = _run  # type: ignore[method-assign]

    result = await tool.execute({"operation": "execute"})

    assert shutdown_warning_of(result) is not None
    assert "shutdown_warning" not in result.model_dump()

    await _finish_task(holder["task"], released)


@pytest.mark.asyncio
async def test_a_failing_run_is_drained_too_and_its_error_is_unchanged():
    """A run that raised can leave exactly as much behind as one that didn't."""
    released = threading.Event()
    holder: dict[str, asyncio.Task] = {}
    tool = _tool({"shutdown_drain_timeout": 0.4})

    async def _run(_input):
        holder["task"] = asyncio.create_task(_unstoppable(released), name="after-boom")
        raise RuntimeError("boom")

    tool._execute_recipe = _run  # type: ignore[method-assign]

    started = time.monotonic()
    result = await tool.execute({"operation": "execute"})
    elapsed = time.monotonic() - started

    assert result.success is False
    assert result.error["message"] == "boom"
    assert result.error["type"] == "RuntimeError"
    assert elapsed <= 0.4 + 0.6, elapsed

    await _finish_task(holder["task"], released)


@pytest.mark.asyncio
async def test_resume_is_bounded_too():
    """`resume` re-enters the same engine, so it can strand the same work."""
    from amplifier_core import ToolResult

    released = threading.Event()
    holder: dict[str, asyncio.Task] = {}
    tool = _tool({"shutdown_drain_timeout": 0.4})

    async def _run(_input):
        holder["task"] = asyncio.create_task(_unstoppable(released), name="resumed")
        return ToolResult(success=True, output={"status": "completed"})

    tool._resume_recipe = _run  # type: ignore[method-assign]

    started = time.monotonic()
    result = await tool.execute({"operation": "resume"})
    elapsed = time.monotonic() - started

    assert result.success is True
    assert elapsed <= 0.4 + 0.6, elapsed
    warning = shutdown_warning_of(result)
    assert warning is not None
    assert any("resumed" in name for name in warning["undrained"])

    await _finish_task(holder["task"], released)


@pytest.mark.asyncio
async def test_a_read_only_operation_is_not_wrapped():
    """`list` starts nothing, so it pays nothing -- no drain, no warning."""
    from amplifier_core import ToolResult

    tool = _tool({"shutdown_drain_timeout": 30})

    async def _list(_input):
        return ToolResult(success=True, output={"sessions": []})

    tool._list_sessions = _list  # type: ignore[method-assign]

    result = await tool.execute({"operation": "list"})

    assert result.success is True
    assert shutdown_warning_of(result) is None


def test_a_bad_bound_is_refused_when_the_tool_is_built_not_at_the_end_of_a_run():
    with pytest.raises(ValueError):
        _tool({"shutdown_drain_timeout": -1})


@pytest.mark.asyncio
async def test_the_one_warning_line_carries_both_the_outcome_and_the_leak(caplog):
    """WARNING is the only level a default CLI session shows.

    A warning that named only the leak would leave the user knowing what broke
    and *not* whether their recipe finished -- which is the half of the
    recipes-8sr symptom that actually cost them the run.
    """
    from amplifier_core import ToolResult

    released = threading.Event()
    holder: dict[str, asyncio.Task] = {}
    tool = _tool({"shutdown_drain_timeout": 0.3})

    async def _run(_input):
        holder["task"] = asyncio.create_task(_unstoppable(released), name="straggler")
        return ToolResult(
            success=True, output={"status": "completed", "recipe": "spec-to-bm"}
        )

    tool._execute_recipe = _run  # type: ignore[method-assign]

    with caplog.at_level("WARNING", logger="amplifier_module_tool_recipes"):
        await tool.execute({"operation": "execute"})

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "the run must not fall silent"
    line = warnings[-1]
    assert "completed" in line
    assert "spec-to-bm" in line  # the outcome
    assert "straggler" in line  # what did not drain

    await _finish_task(holder["task"], released)
