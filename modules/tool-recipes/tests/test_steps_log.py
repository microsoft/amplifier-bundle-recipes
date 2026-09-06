"""Tests for the append-only per-step run log (``steps.jsonl``).

The gap these cover: before this file existed, a finished recipe session held
exactly ``recipe.yaml`` and ``state.json``, and ``state.json`` recorded
finished steps as bare ids.  Nothing retained the post-substitution prompt, the
response, per-step timing, or per-step status, so a completed run could not be
audited from disk.

Four properties are load-bearing and each has its own test:

* the record shape actually carries the runtime-only facts,
* truncation is marked, never silent,
* the ``started`` line lands BEFORE the body runs, so a crash leaves a partial
  record, and
* nested work (foreach, sub-steps) names its parent step.

Plus the negative that keeps the change additive: ``state.json``'s shape is
untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from amplifier_module_tool_recipes import steps_log
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step
from amplifier_module_tool_recipes.session import SessionManager
from amplifier_module_tool_recipes.steps_log import STEPS_LOG_FILENAME


# ---------------------------------------------------------------------------
# Harness: a real SessionManager (so records land on a real disk) plus the
# smallest coordinator that satisfies the executor.
# ---------------------------------------------------------------------------


class FakeCoordinator:
    """Minimum coordinator the executor needs: a spawn capability and config."""

    def __init__(self, spawn: Any, agents: dict[str, Any] | None = None) -> None:
        self.session = object()
        self.config = {"agents": agents or {}, "providers": {}}
        self.hooks = None
        self._spawn = spawn

    def get_capability(self, name: str) -> Any:
        # Only session.spawn is wired; model_role_resolver / mention_resolver
        # are deliberately absent so the executor takes its no-resolver paths.
        return self._spawn if name == "session.spawn" else None


def make_spawn(reply: Any = "ok", on_call: Any = None):
    """Build a spawn stand-in that records what it was asked."""
    calls: list[dict[str, Any]] = []

    async def spawn(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if on_call is not None:
            result = on_call(len(calls), kwargs)
            if result is not None:
                return result
        if isinstance(reply, BaseException):
            raise reply
        return {"output": reply}

    spawn.calls = calls  # type: ignore[attr-defined]
    return spawn


def make_recipe(name: str, steps: list[Step]) -> Recipe:
    """Recipe() takes description/version positionally; keep the tests readable."""
    return Recipe(name=name, description=f"{name} fixture", version="1.0", steps=steps)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


@pytest.fixture
def sessions(tmp_path: Path) -> SessionManager:
    return SessionManager(base_dir=tmp_path / "sessions")


def session_dir_of(sessions: SessionManager, project: Path) -> Path:
    """The single session directory a one-run test produced."""
    root = sessions.get_sessions_dir(project)
    dirs = [p for p in root.iterdir() if p.is_dir()]
    assert len(dirs) == 1, f"expected one session dir, found {dirs}"
    return dirs[0]


def records_of(sessions: SessionManager, project: Path) -> list[dict[str, Any]]:
    return steps_log.read_steps_log(session_dir_of(sessions, project))


def finished(records: list[dict[str, Any]], step_id: str) -> list[dict[str, Any]]:
    return [
        r
        for r in records
        if r.get("step_id") == step_id and r.get("event") == "finished"
    ]


def started(records: list[dict[str, Any]], step_id: str) -> list[dict[str, Any]]:
    return [
        r for r in records if r.get("step_id") == step_id and r.get("event") == "started"
    ]


# ---------------------------------------------------------------------------
# Unit level: truncation and the writer
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_text_under_the_cap_is_untouched_and_marked_whole(self):
        text, truncated, original = steps_log.truncate_text("hello", 64)
        assert (text, truncated, original) == ("hello", False, 5)

    def test_text_over_the_cap_keeps_the_head_and_says_so(self):
        text, truncated, original = steps_log.truncate_text("abcdefghij", 4)
        assert text == "abcd"
        assert truncated is True
        assert original == 10

    def test_tail_mode_keeps_the_end_where_the_failure_is(self):
        text, truncated, _ = steps_log.truncate_text(
            "noise...the real error", 10, keep="tail"
        )
        assert text == "real error"
        assert truncated is True

    def test_a_cap_landing_mid_codepoint_drops_the_partial_character(self):
        # "€" is three UTF-8 bytes; a 2-byte cap must not emit half of one.
        text, truncated, original = steps_log.truncate_text("€€", 4)
        assert text == "€"
        assert truncated is True
        assert original == 6

    def test_zero_cap_means_no_cap(self):
        text, truncated, _ = steps_log.truncate_text("x" * 1000, 0)
        assert text == "x" * 1000
        assert truncated is False

    def test_capped_field_always_carries_a_truncation_marker(self):
        record: dict[str, Any] = {}
        steps_log.set_capped_field(record, "response", "short", cap=100)
        assert record["response"] == "short"
        # Present and False -- a reader never has to infer completeness from
        # the ABSENCE of a marker.
        assert record["response_truncated"] is False
        assert record["response_bytes"] == 5

    def test_a_none_value_writes_no_field_at_all(self):
        record: dict[str, Any] = {}
        steps_log.set_capped_field(record, "response", None)
        assert record == {}

    def test_non_string_payloads_are_rendered_not_dropped(self):
        record: dict[str, Any] = {}
        steps_log.set_capped_field(record, "response", {"a": 1}, cap=100)
        assert json.loads(record["response"]) == {"a": 1}


class TestWriter:
    def test_append_creates_the_file_and_writes_one_line_per_record(
        self, tmp_path: Path
    ):
        path = tmp_path / "nested" / STEPS_LOG_FILENAME
        assert steps_log.append_record(path, {"a": 1}) is True
        assert steps_log.append_record(path, {"a": 2}) is True
        lines = path.read_text().splitlines()
        assert [json.loads(line)["a"] for line in lines] == [1, 2]

    def test_a_write_failure_is_swallowed_not_raised(self, tmp_path: Path):
        # A directory where the file should be: opening it for write fails.
        path = tmp_path / STEPS_LOG_FILENAME
        path.mkdir()
        assert steps_log.append_record(path, {"a": 1}) is False

    def test_an_unserialisable_payload_still_produces_a_record(self, tmp_path: Path):
        path = tmp_path / STEPS_LOG_FILENAME

        class Opaque:
            def __repr__(self) -> str:
                return "<opaque>"

        assert steps_log.append_record(path, {"value": Opaque()}) is True
        assert "<opaque>" in path.read_text()

    def test_a_malformed_trailing_line_is_skipped_on_read(self, tmp_path: Path):
        path = tmp_path / STEPS_LOG_FILENAME
        path.write_text('{"a": 1}\n{"a": 2\n')  # second line half-written
        assert steps_log.read_steps_log(tmp_path) == [{"a": 1}]

    def test_an_inert_log_writes_nothing(self, tmp_path: Path):
        log = steps_log.StepLog(None)
        assert log.enabled is False
        attempt = log.begin(step_id="x")
        attempt.start()
        attempt.finish(steps_log.STATUS_COMPLETED)
        assert list(tmp_path.iterdir()) == []

    def test_finish_is_idempotent(self, tmp_path: Path):
        log = steps_log.StepLog(tmp_path / STEPS_LOG_FILENAME)
        attempt = log.begin(step_id="x")
        attempt.finish(steps_log.STATUS_COMPLETED)
        attempt.finish(steps_log.STATUS_FAILED)
        records = steps_log.read_steps_log(tmp_path)
        assert [r["status"] for r in records] == [steps_log.STATUS_COMPLETED]


class TestConfiguration:
    def test_the_cap_is_configurable_by_environment(self, monkeypatch):
        monkeypatch.setenv(steps_log.ENV_MAX_FIELD_BYTES, "128")
        assert steps_log.max_field_bytes() == 128

    def test_an_unparseable_cap_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv(steps_log.ENV_MAX_FIELD_BYTES, "banana")
        assert steps_log.max_field_bytes() == steps_log.DEFAULT_MAX_FIELD_BYTES

    def test_the_default_cap_is_64k(self, monkeypatch):
        monkeypatch.delenv(steps_log.ENV_MAX_FIELD_BYTES, raising=False)
        assert steps_log.max_field_bytes() == 64 * 1024

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
    def test_the_log_can_be_switched_off(self, monkeypatch, value):
        monkeypatch.setenv(steps_log.ENV_ENABLED, value)
        assert steps_log.steps_log_enabled() is False

    def test_it_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv(steps_log.ENV_ENABLED, raising=False)
        assert steps_log.steps_log_enabled() is True


# ---------------------------------------------------------------------------
# Integration: what a real run leaves behind
# ---------------------------------------------------------------------------


class TestRecordShape:
    @pytest.mark.asyncio
    async def test_an_agent_step_records_the_resolved_prompt_and_the_response(
        self, workspace, sessions
    ):
        spawn = make_spawn(reply="the answer")
        executor = RecipeExecutor(FakeCoordinator(spawn), sessions)
        recipe = make_recipe(
            "shape",
            steps=[
                Step(
                    id="ask",
                    agent="an-agent",
                    prompt="Summarise {{topic}}",
                    output="summary",
                )
            ],
        )

        await executor.execute_recipe(recipe, {"topic": "kelp"}, workspace)

        records = records_of(sessions, workspace)
        assert len(started(records, "ask")) == 1
        done = finished(records, "ask")
        assert len(done) == 1
        record = done[0]

        assert record["v"] == steps_log.STEPS_LOG_SCHEMA_VERSION
        assert record["status"] == steps_log.STATUS_COMPLETED
        assert record["step_type"] == "agent"
        assert record["attempt"] == 1
        assert record["agent"] == "an-agent"
        assert record["recipe_name"] == "shape"
        assert record["output_key"] == "summary"

        # The whole point: the prompt is post-substitution, not the template.
        assert record["prompt_resolved"] == "Summarise kelp"
        assert "{{" not in record["prompt_resolved"]
        assert record["response"] == "the answer"

        # Timing is real and ordered.
        assert record["started_at"] < record["finished_at"]
        assert isinstance(record["duration_s"], (int, float))
        assert record["duration_s"] >= 0

        # The two lines are joinable.
        assert started(records, "ask")[0]["record_id"] == record["record_id"]

    @pytest.mark.asyncio
    async def test_the_mode_prefix_is_part_of_the_recorded_prompt(
        self, workspace, sessions
    ):
        # The agent receives the mode rider, so the audit record must show it --
        # otherwise the log disagrees with what was actually sent.
        spawn = make_spawn()
        executor = RecipeExecutor(FakeCoordinator(spawn), sessions)
        recipe = make_recipe(
            "mode",
            steps=[Step(id="s", agent="a", prompt="Do it", mode="ANALYZE")],
        )

        await executor.execute_recipe(recipe, {}, workspace)

        record = finished(records_of(sessions, workspace), "s")[0]
        assert record["prompt_resolved"] == spawn.calls[0]["instruction"]
        assert record["prompt_resolved"].startswith("MODE: ANALYZE")
        assert record["mode"] == "ANALYZE"

    @pytest.mark.asyncio
    async def test_the_spawned_agent_session_is_recorded_as_the_link_to_full_detail(
        self, workspace, sessions
    ):
        """The log stays bounded; the agent's own session keeps everything.

        Recording the spawned session id is what makes the 64 KB cap safe --
        the whole transcript is one hop away instead of lost.
        """

        async def spawn(**kwargs: Any) -> Any:
            return {
                "output": "done",
                "session_id": "child-abc123",
                "status": "success",
                "turn_count": 3,
                "metadata": {"provider": "anthropic", "model": "claude-opus-5"},
            }

        executor = RecipeExecutor(FakeCoordinator(spawn), sessions)
        recipe = make_recipe("link", [Step(id="s", agent="a", prompt="p")])

        await executor.execute_recipe(recipe, {}, workspace)

        record = finished(records_of(sessions, workspace), "s")[0]
        assert record["agent_session_id"] == "child-abc123"
        assert record["turn_count"] == 3
        assert record["agent_status"] == "success"
        # No provider chain was pinned here, so the envelope is the only
        # witness to which model actually ran -- and it is recorded.
        assert record["provider"] == "anthropic"
        assert record["model"] == "claude-opus-5"

    @pytest.mark.asyncio
    async def test_what_the_step_pinned_wins_over_what_the_envelope_reports(
        self, workspace, sessions
    ):
        """`provider`/`model` mean "what this step asked for", consistently."""

        async def spawn(**kwargs: Any) -> Any:
            return {
                "output": "done",
                "session_id": "child",
                "metadata": {"provider": "somewhere-else", "model": "other"},
            }

        executor = RecipeExecutor(FakeCoordinator(spawn), sessions)
        recipe = make_recipe(
            "pin",
            [Step(id="s", agent="a", prompt="p", provider="anthropic", model="claude-x")],
        )

        await executor.execute_recipe(recipe, {}, workspace)

        record = finished(records_of(sessions, workspace), "s")[0]
        assert record["provider"] == "anthropic"
        assert record["model"] == "claude-x"

    @pytest.mark.asyncio
    async def test_a_bash_step_records_command_exit_code_and_output(
        self, workspace, sessions
    ):
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe(
            "bash",
            steps=[
                Step(
                    id="run",
                    type="bash",
                    command="echo hello-{{who}}; echo oops >&2",
                    output="out",
                )
            ],
        )

        await executor.execute_recipe(recipe, {"who": "world"}, workspace)

        record = finished(records_of(sessions, workspace), "run")[0]
        assert record["step_type"] == "bash"
        assert record["status"] == steps_log.STATUS_COMPLETED
        assert record["command"] == "echo hello-world; echo oops >&2"
        assert record["exit_code"] == 0
        assert record["stdout"].strip() == "hello-world"
        assert record["stderr"].strip() == "oops"
        assert record["cwd"] == str(workspace)

    @pytest.mark.asyncio
    async def test_a_failing_bash_step_is_recorded_as_failed_with_its_stderr(
        self, workspace, sessions
    ):
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe(
            "bash-fail",
            steps=[
                Step(
                    id="boom",
                    type="bash",
                    command="echo detail >&2; exit 3",
                    on_error="continue",
                )
            ],
        )

        await executor.execute_recipe(recipe, {}, workspace)

        record = finished(records_of(sessions, workspace), "boom")[0]
        # on_error=continue absorbs it for the RUN; the step still failed.
        assert record["status"] == steps_log.STATUS_FAILED
        assert record["exit_code"] == 3
        assert "detail" in record["stderr"]

    @pytest.mark.asyncio
    async def test_a_failing_agent_step_records_the_prompt_it_had_sent(
        self, workspace, sessions
    ):
        spawn = make_spawn(reply=RuntimeError("provider exploded"))
        executor = RecipeExecutor(FakeCoordinator(spawn), sessions)
        recipe = make_recipe(
            "fail",
            steps=[Step(id="s", agent="a", prompt="Ask about {{x}}", on_error="continue")],
        )

        await executor.execute_recipe(recipe, {"x": "kelp"}, workspace)

        record = finished(records_of(sessions, workspace), "s")[0]
        assert record["status"] == steps_log.STATUS_FAILED
        assert record["prompt_resolved"] == "Ask about kelp"
        assert "provider exploded" in record["error"]

    @pytest.mark.asyncio
    async def test_a_timeout_is_recorded_as_timed_out_not_merely_failed(
        self, workspace, sessions
    ):
        import asyncio

        async def slow_spawn(**kwargs: Any) -> Any:
            await asyncio.sleep(5)

        executor = RecipeExecutor(FakeCoordinator(slow_spawn), sessions)
        recipe = make_recipe(
            "slow",
            steps=[Step(id="s", agent="a", prompt="wait", timeout=0.05, on_error="continue")],
        )

        await executor.execute_recipe(recipe, {}, workspace)

        record = finished(records_of(sessions, workspace), "s")[0]
        assert record["status"] == steps_log.STATUS_TIMED_OUT

    @pytest.mark.asyncio
    async def test_every_step_of_a_multi_step_run_gets_its_own_pair_of_lines(
        self, workspace, sessions
    ):
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe(
            "three",
            steps=[
                Step(id="one", agent="a", prompt="1"),
                Step(id="two", type="bash", command="true"),
                Step(id="three", agent="a", prompt="3"),
            ],
        )

        await executor.execute_recipe(recipe, {}, workspace)

        records = records_of(sessions, workspace)
        for step_id in ("one", "two", "three"):
            assert len(started(records, step_id)) == 1, step_id
            assert len(finished(records, step_id)) == 1, step_id
        # Append-only: the file is in execution order.
        assert [r["step_id"] for r in records] == [
            "one",
            "one",
            "two",
            "two",
            "three",
            "three",
        ]


class TestTruncationInARun:
    @pytest.mark.asyncio
    async def test_an_oversized_response_is_capped_and_explicitly_marked(
        self, workspace, sessions, monkeypatch
    ):
        monkeypatch.setenv(steps_log.ENV_MAX_FIELD_BYTES, "64")
        huge = "R" * 5000
        executor = RecipeExecutor(FakeCoordinator(make_spawn(reply=huge)), sessions)
        recipe = make_recipe(
            "big",
            steps=[Step(id="s", agent="a", prompt="P" * 500)],
        )

        await executor.execute_recipe(recipe, {}, workspace)

        record = finished(records_of(sessions, workspace), "s")[0]
        assert record["response_truncated"] is True
        assert len(record["response"]) == 64
        # The size of what was dropped stays recoverable.
        assert record["response_bytes"] == 5000
        assert record["prompt_resolved_truncated"] is True
        assert record["prompt_resolved_bytes"] == 500

    @pytest.mark.asyncio
    async def test_a_small_response_is_marked_as_whole(self, workspace, sessions):
        executor = RecipeExecutor(FakeCoordinator(make_spawn(reply="tiny")), sessions)
        recipe = make_recipe("small", [Step(id="s", agent="a", prompt="p")])

        await executor.execute_recipe(recipe, {}, workspace)

        record = finished(records_of(sessions, workspace), "s")[0]
        assert record["response_truncated"] is False
        assert record["prompt_resolved_truncated"] is False


class TestCrashLeavesAPartialRecord:
    @pytest.mark.asyncio
    async def test_the_started_line_is_on_disk_while_the_step_is_still_running(
        self, workspace, sessions
    ):
        """The property a crash depends on, asserted at the instant it matters.

        `state.json` is rewritten only at step boundaries, so a process killed
        mid-step leaves no evidence the step was ever in flight.  This asserts
        the `started` line is already durable at the exact moment a crash would
        occur -- inside the step body, before any finished line exists.
        """
        seen: dict[str, Any] = {}

        async def spawn(**kwargs: Any) -> Any:
            # We are now "mid-step": exactly where a SIGKILL would land.
            records = records_of(sessions, workspace)
            seen["records"] = records
            return {"output": "done"}

        executor = RecipeExecutor(FakeCoordinator(spawn), sessions)
        recipe = make_recipe("crash", [Step(id="inflight", agent="a", prompt="go")])

        await executor.execute_recipe(recipe, {}, workspace)

        mid = seen["records"]
        assert len(mid) == 1, mid
        assert mid[0]["event"] == "started"
        assert mid[0]["step_id"] == "inflight"
        assert mid[0]["started_at"]
        # And nothing had settled yet -- this is the partial record.
        assert "status" not in mid[0]

        # After the run, the pair is complete.
        after = records_of(sessions, workspace)
        assert [r["event"] for r in after] == ["started", "finished"]

    @pytest.mark.asyncio
    async def test_a_hard_failure_still_leaves_both_lines(self, workspace, sessions):
        spawn = make_spawn(reply=RuntimeError("boom"))
        executor = RecipeExecutor(FakeCoordinator(spawn), sessions)
        recipe = make_recipe("hard", [Step(id="s", agent="a", prompt="go")])

        with pytest.raises(Exception):
            await executor.execute_recipe(recipe, {}, workspace)

        records = records_of(sessions, workspace)
        assert [r["event"] for r in records] == ["started", "finished"]
        assert records[1]["status"] == steps_log.STATUS_FAILED


class TestRetries:
    @pytest.mark.asyncio
    async def test_each_attempt_is_its_own_record_and_a_retry_says_so(
        self, workspace, sessions
    ):
        def on_call(call_number: int, kwargs: dict[str, Any]) -> Any:
            if call_number == 1:
                raise RuntimeError("flaky")
            return {"output": "second time lucky"}

        spawn = make_spawn(on_call=on_call)
        executor = RecipeExecutor(FakeCoordinator(spawn), sessions)
        recipe = make_recipe(
            "retry",
            steps=[
                Step(
                    id="s",
                    agent="a",
                    prompt="go",
                    retry={"max_attempts": 2, "initial_delay": 0},
                )
            ],
        )

        await executor.execute_recipe(recipe, {}, workspace)

        done = finished(records_of(sessions, workspace), "s")
        assert [(r["attempt"], r["status"]) for r in done] == [
            (1, steps_log.STATUS_RETRIED),
            (2, steps_log.STATUS_COMPLETED),
        ]
        # "It eventually worked" is now distinguishable from "it worked first
        # try", which completed_steps could never express.
        assert len(started(records_of(sessions, workspace), "s")) == 2


class TestSkippedSteps:
    @pytest.mark.asyncio
    async def test_a_condition_false_step_is_recorded_as_skipped_with_a_reason(
        self, workspace, sessions
    ):
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe(
            "skip",
            steps=[
                Step(id="ran", agent="a", prompt="go"),
                Step(id="not-ran", agent="a", prompt="go", condition="{{flag}} == 'yes'"),
            ],
        )

        await executor.execute_recipe(recipe, {"flag": "no"}, workspace)

        records = records_of(sessions, workspace)
        skipped = finished(records, "not-ran")
        assert len(skipped) == 1
        assert skipped[0]["status"] == steps_log.STATUS_SKIPPED
        assert "condition false" in skipped[0]["reason"]
        # A skip never ran, so it has no in-flight phase.
        assert started(records, "not-ran") == []

    @pytest.mark.asyncio
    async def test_skipped_is_distinguishable_from_never_reached(
        self, workspace, sessions
    ):
        """The ambiguity `completed_steps` could not resolve.

        A skipped step and a step the run never got to are both simply absent
        from `completed_steps`.  In steps.jsonl only one of them has a record.
        """
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe(
            "skip-vs-unreached",
            steps=[
                Step(id="skipped", agent="a", prompt="go", condition="false"),
                Step(id="stopper", type="bash", command="exit 1", on_error="skip_remaining"),
                Step(id="never-reached", agent="a", prompt="go"),
            ],
        )

        await executor.execute_recipe(recipe, {}, workspace)

        records = records_of(sessions, workspace)
        state = json.loads((session_dir_of(sessions, workspace) / "state.json").read_text())
        # Indistinguishable in the checkpoint...
        assert "skipped" not in state["completed_steps"]
        assert "never-reached" not in state["completed_steps"]
        # ...and clearly different in the log.
        assert finished(records, "skipped")[0]["status"] == steps_log.STATUS_SKIPPED
        assert finished(records, "never-reached") == []
        assert started(records, "never-reached") == []


class TestNestedSteps:
    @pytest.mark.asyncio
    async def test_foreach_iterations_name_their_parent_step_and_index(
        self, workspace, sessions
    ):
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe(
            "loop",
            steps=[
                Step(
                    id="each",
                    agent="a",
                    prompt="handle {{item}}",
                    foreach="{{items}}",
                    collect="all",
                )
            ],
        )

        await executor.execute_recipe(recipe, {"items": ["x", "y"]}, workspace)

        records = records_of(sessions, workspace)
        loop_records = [r for r in records if r.get("step_type") == "foreach"]
        assert len(loop_records) == 2  # started + finished for the loop itself
        assert loop_records[-1]["status"] == steps_log.STATUS_COMPLETED
        assert loop_records[-1]["parent_step_id"] is None

        body = [
            r
            for r in records
            if r.get("event") == "finished" and r.get("step_type") == "agent"
        ]
        assert len(body) == 2
        assert {r["parent_step_id"] for r in body} == {"each"}
        assert [r["iteration"] for r in body] == [0, 1]
        assert [r["prompt_resolved"] for r in body] == ["handle x", "handle y"]

    @pytest.mark.asyncio
    async def test_parallel_iterations_each_carry_their_own_index(
        self, workspace, sessions
    ):
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe(
            "par",
            steps=[
                Step(
                    id="each",
                    agent="a",
                    prompt="handle {{item}}",
                    foreach="{{items}}",
                    parallel=True,
                    collect="all",
                )
            ],
        )

        await executor.execute_recipe(recipe, {"items": ["a", "b", "c"]}, workspace)

        body = [
            r
            for r in records_of(sessions, workspace)
            if r.get("event") == "finished" and r.get("step_type") == "agent"
        ]
        assert len(body) == 3
        assert {r["parent_step_id"] for r in body} == {"each"}
        # Parallel iterations finish in any order, but each knows which it is.
        assert sorted(r["iteration"] for r in body) == [0, 1, 2]
        assert all(r["parallel_group_id"] for r in body)

    @pytest.mark.asyncio
    async def test_a_multi_step_foreach_body_links_every_sub_step_to_the_loop(
        self, workspace, sessions
    ):
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe(
            "multi",
            steps=[
                Step(
                    id="each",
                    foreach="{{items}}",
                    while_steps=[
                        {"id": "first", "type": "bash", "command": "echo {{item}}"},
                        {"id": "second", "agent": "a", "prompt": "on {{item}}"},
                    ],
                )
            ],
        )

        await executor.execute_recipe(recipe, {"items": ["only"]}, workspace)

        records = records_of(sessions, workspace)
        for sub in ("first", "second"):
            done = finished(records, sub)
            assert len(done) == 1, sub
            assert done[0]["parent_step_id"] == "each"
            assert done[0]["iteration"] == 0

    @pytest.mark.asyncio
    async def test_an_empty_foreach_is_recorded_as_skipped_not_completed(
        self, workspace, sessions
    ):
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe(
            "empty",
            steps=[
                Step(id="each", agent="a", prompt="p {{item}}", foreach="{{items}}", collect="c")
            ],
        )

        await executor.execute_recipe(recipe, {"items": []}, workspace)

        record = finished(records_of(sessions, workspace), "each")[0]
        assert record["status"] == steps_log.STATUS_SKIPPED
        assert "empty" in record["reason"]

    @pytest.mark.asyncio
    async def test_a_sub_recipe_step_is_recorded_in_the_parent_and_links_the_child(
        self, workspace, sessions, tmp_path
    ):
        child = tmp_path / "child.yaml"
        child.write_text(
            "name: child\n"
            "steps:\n"
            "  - id: inner\n"
            "    type: bash\n"
            "    command: echo inner\n"
        )
        parent = tmp_path / "parent.yaml"
        parent.write_text(
            "name: parent\n"
            "steps:\n"
            "  - id: compose\n"
            "    type: recipe\n"
            f"    recipe: {child.name}\n"
        )

        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        await executor.execute_recipe(
            Recipe.from_yaml(parent), {}, workspace, recipe_path=parent
        )

        root = sessions.get_sessions_dir(workspace)
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
        assert len(dirs) == 2, "parent and child each get their own session"

        by_recipe: dict[str, list[dict[str, Any]]] = {}
        for d in dirs:
            for record in steps_log.read_steps_log(d):
                by_recipe.setdefault(record.get("recipe_name", "?"), []).append(record)

        parent_done = [
            r for r in by_recipe["parent"] if r.get("event") == "finished"
        ]
        assert parent_done[0]["step_id"] == "compose"
        assert parent_done[0]["step_type"] == "recipe"
        assert parent_done[0]["status"] == steps_log.STATUS_COMPLETED
        assert parent_done[0]["sub_recipe_path"].endswith("child.yaml")

        # The child's own steps live in the child's own log, and name the
        # composing step as their parent.
        child_done = [r for r in by_recipe["child"] if r.get("event") == "finished"]
        assert child_done[0]["step_id"] == "inner"
        assert child_done[0]["parent_step_id"] == "compose"


class TestStagedRecipes:
    @pytest.mark.asyncio
    async def test_staged_steps_record_their_stage(self, workspace, sessions, tmp_path):
        recipe_file = tmp_path / "staged.yaml"
        recipe_file.write_text(
            "name: staged\n"
            "stages:\n"
            "  - name: first\n"
            "    steps:\n"
            "      - id: a\n"
            "        type: bash\n"
            "        command: echo a\n"
            "  - name: second\n"
            "    steps:\n"
            "      - id: b\n"
            "        type: bash\n"
            "        command: echo b\n"
        )

        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        await executor.execute_recipe(
            Recipe.from_yaml(recipe_file), {}, workspace, recipe_path=recipe_file
        )

        records = records_of(sessions, workspace)
        assert finished(records, "a")[0]["stage"] == "first"
        assert finished(records, "b")[0]["stage"] == "second"


class TestAdditiveOnly:
    """The change must not disturb what was already on disk."""

    @pytest.mark.asyncio
    async def test_state_json_keeps_exactly_its_previous_shape(
        self, workspace, sessions
    ):
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        recipe = make_recipe("shape", [Step(id="s", agent="a", prompt="p")])

        await executor.execute_recipe(recipe, {}, workspace)

        state = json.loads(
            (session_dir_of(sessions, workspace) / "state.json").read_text()
        )
        assert sorted(state) == [
            "completed_steps",
            "context",
            "current_step_index",
            "parent_session_id",
            "project_path",
            "recipe_name",
            "recipe_version",
            "session_id",
            "started",
        ]
        # Still bare strings -- the detail went into the new file, not here.
        assert state["completed_steps"] == ["s"]

    @pytest.mark.asyncio
    async def test_the_session_directory_gains_exactly_one_file(
        self, workspace, sessions, tmp_path
    ):
        recipe_file = tmp_path / "r.yaml"
        recipe_file.write_text(
            "name: r\nsteps:\n  - id: s\n    type: bash\n    command: 'true'\n"
        )
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        await executor.execute_recipe(
            Recipe.from_yaml(recipe_file), {}, workspace, recipe_path=recipe_file
        )

        names = sorted(
            p.name for p in session_dir_of(sessions, workspace).iterdir() if p.is_file()
        )
        assert names == ["recipe.yaml", "state.json", STEPS_LOG_FILENAME]

    @pytest.mark.asyncio
    async def test_disabling_the_log_leaves_the_old_two_file_session(
        self, workspace, sessions, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(steps_log.ENV_ENABLED, "0")
        recipe_file = tmp_path / "r.yaml"
        recipe_file.write_text(
            "name: r\nsteps:\n  - id: s\n    type: bash\n    command: 'true'\n"
        )
        executor = RecipeExecutor(FakeCoordinator(make_spawn()), sessions)
        await executor.execute_recipe(
            Recipe.from_yaml(recipe_file), {}, workspace, recipe_path=recipe_file
        )

        names = sorted(
            p.name for p in session_dir_of(sessions, workspace).iterdir() if p.is_file()
        )
        assert names == ["recipe.yaml", "state.json"]

    @pytest.mark.asyncio
    async def test_a_session_manager_without_the_new_method_still_runs(
        self, workspace, tmp_path
    ):
        """A duck-typed stand-in that predates this feature must not break a run."""

        class OldSessionManager:
            """Everything the executor used before `open_steps_log` existed."""

            def __init__(self, inner: SessionManager) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                if name == "open_steps_log":
                    raise AttributeError(name)
                return getattr(self._inner, name)

        inner = SessionManager(base_dir=tmp_path / "sessions")
        old = OldSessionManager(inner)
        assert not hasattr(old, "open_steps_log")

        executor = RecipeExecutor(FakeCoordinator(make_spawn(reply="fine")), old)
        recipe = make_recipe("old", [Step(id="s", agent="a", prompt="p")])

        result = await executor.execute_recipe(recipe, {}, workspace)
        assert result is not None
        # It fell back to the session directory it CAN resolve, so the log is
        # still written rather than silently lost.
        assert finished(records_of(inner, workspace), "s")[0]["status"] == (
            steps_log.STATUS_COMPLETED
        )
