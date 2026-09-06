"""Step-semantics tests for the library's own engine (lib.v1 Core 2, 8).

Scenario-for-scenario ports of the legacy engine's step tests
(``modules/tool-recipes/tests/test_bash_steps.py``,
``test_executor_loops.py``, ``test_executor_conditions.py``,
``test_json_parsing.py``, ``test_on_error_correctness.py``,
``test_templated_timeout.py``, ``test_approval_gates.py``,
``test_fix1_context_type_preservation.py``). Each class names the legacy file
it mirrors so a future reader can diff the two suites rather than guess
whether a behaviour was ported or invented.

Every row of ``docs/EXECUTOR_PARITY.md`` is exercised here at least once, and
the deliberate deltas recorded in that matrix are asserted *as deltas* -- a
test that pins the library's behaviour where it knowingly differs is what
stops the difference from being quietly "fixed" back into a surprise.

Nothing here spawns a model: agent steps run against an injected double.
"""

from __future__ import annotations

import asyncio
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from amplifier_recipe_runner.engine import ApprovalLedger
from amplifier_recipe_runner.engine import ExecutionError
from amplifier_recipe_runner.engine import RecipeProgram
from amplifier_recipe_runner.engine import ResumeState
from amplifier_recipe_runner.engine import StepEngine
from amplifier_recipe_runner.engine import StepSpec
from amplifier_recipe_runner.engine import UnsupportedStepError
from amplifier_recipe_runner.engine import extract_json_aggressively
from amplifier_recipe_runner.engine import parse_program
from amplifier_recipe_runner.engine import parse_step
from amplifier_recipe_runner.engine import process_step_result
from amplifier_recipe_runner.engine import resolve_step_timeout
from amplifier_recipe_runner.engine import substitute_recursive
from amplifier_recipe_runner.engine import substitute_variables

# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


class RecordingAgent:
    """Agent double. Records what it was asked; calls no model."""

    def __init__(self, reply: Any = "ok") -> None:
        self.calls: list[tuple[str, str]] = []
        self._reply = reply

    async def __call__(self, step: StepSpec, instruction: str, context: Mapping[str, Any]) -> Any:
        self.calls.append((step.id, instruction))
        if callable(self._reply):
            return self._reply(step, instruction, context)
        return self._reply


class ExplodingAgent:
    async def __call__(self, step: StepSpec, instruction: str, context: Mapping[str, Any]) -> Any:
        raise RuntimeError(f"agent step {step.id!r} blew up")


class SlowAgent:
    async def __call__(self, step: StepSpec, instruction: str, context: Mapping[str, Any]) -> Any:
        await asyncio.sleep(30)
        return "never"


class CollectingEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, Any]]] = []

    def __call__(self, kind: str, data: Mapping[str, Any]) -> None:
        self.events.append((kind, dict(data)))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


class Cancelled:
    cancelled = True

    def raise_if_cancelled(self) -> None:  # pragma: no cover - protocol shape
        raise RuntimeError("cancelled")


class Approves:
    """An approval callback that says yes, the way a host with a UI would."""

    def __init__(self, approved: bool = True, message: str | None = "fine") -> None:
        self.requests: list[Any] = []
        self._approved = approved
        self._message = message

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return _Decision(self._approved, self._message)


class _Decision:
    def __init__(self, approved: bool, message: str | None) -> None:
        self.approved = approved
        self.message = message


def program(body: str, *, path: Path | None = None) -> RecipeProgram:
    return parse_program(yaml.safe_load(textwrap.dedent(body)), path=path)


def run_program(
    body: str,
    *,
    workspace: Path,
    context: dict[str, Any] | None = None,
    agent: Any = None,
    path: Path | None = None,
    **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    """Execute ``body`` and return ``(outcome, final context)``."""
    prog = program(body, path=path)
    ctx = dict(context or {})
    engine = StepEngine(
        prog,
        invoke_agent=agent or RecordingAgent(),
        workspace=workspace,
        run_id="run-test",
        recipe_path=path,
        **kwargs,
    )
    outcome = asyncio.run(engine.execute(ctx))
    return outcome, ctx


# --------------------------------------------------------------------------
# substitution -- mirrors test_fix1_context_type_preservation.py
# --------------------------------------------------------------------------


class TestSubstitution:
    def test_dotted_path_reaches_a_nested_value(self) -> None:
        assert substitute_variables("{{a.b.c}}", {"a": {"b": {"c": "deep"}}}) == "deep"

    def test_booleans_render_as_json_not_python(self) -> None:
        # `True` in a shell command or a JSON payload is a bug; `true` is not.
        assert substitute_variables("{{flag}}", {"flag": True}) == "true"
        assert substitute_variables("{{flag}}", {"flag": False}) == "false"

    def test_structures_render_as_json_not_repr(self) -> None:
        assert substitute_variables("{{d}}", {"d": {"k": "v"}}) == '{"k": "v"}'
        assert substitute_variables("{{items}}", {"items": [1, 2]}) == "[1, 2]"

    def test_undefined_variable_names_what_was_available(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            substitute_variables("{{missing}}", {"present": 1})
        assert "missing" in str(excinfo.value)
        assert "present" in str(excinfo.value)

    def test_a_non_dict_intermediate_names_the_likely_cause(self) -> None:
        # The legacy message points at the real culprit -- a bash step that
        # produced text where the recipe expected parsed JSON.
        with pytest.raises(ValueError) as excinfo:
            substitute_variables("{{payload.key}}", {"payload": "not json"})
        assert "not a dict" in str(excinfo.value)
        assert "parse_json" in str(excinfo.value)

    def test_a_whole_variable_reference_keeps_its_native_type(self) -> None:
        payload = {"task": {"id": 7}}
        assert substitute_recursive("{{task}}", payload) == {"id": 7}
        assert substitute_recursive({"nested": "{{task}}"}, payload) == {"nested": {"id": 7}}

    def test_a_composite_string_is_still_a_string(self) -> None:
        assert substitute_recursive("id={{task.id}}", {"task": {"id": 7}}) == "id=7"


# --------------------------------------------------------------------------
# JSON handling -- mirrors test_json_parsing.py / test_json_extraction.py
# --------------------------------------------------------------------------


class TestJsonHandling:
    def test_whole_string_json(self) -> None:
        assert extract_json_aggressively('{"a": 1}') == {"a": 1}

    def test_fenced_block_with_inner_braces_is_not_truncated(self) -> None:
        text = 'preamble\n```json\n{"outer": {"inner": 1}}\n```\ntrailing'
        assert extract_json_aggressively(text) == {"outer": {"inner": 1}}

    def test_embedded_json_after_prose(self) -> None:
        assert extract_json_aggressively('Checking...\n{"ok": true}') == {"ok": True}

    def test_a_trivial_structure_does_not_beat_a_real_one(self) -> None:
        assert extract_json_aggressively('empty {} then {"real": 1}') == {"real": 1}

    def test_unparseable_text_comes_back_unchanged(self) -> None:
        # Not None: a caller cannot tell an invented None from a JSON null.
        assert extract_json_aggressively("just prose") == "just prose"

    def test_conservative_default_only_parses_clean_json(self) -> None:
        step = parse_step({"id": "s", "agent": "a", "prompt": "p"})
        assert process_step_result("Here you go: {\"a\": 1}", step) == 'Here you go: {"a": 1}'
        assert process_step_result('{"a": 1}', step) == {"a": 1}

    def test_bash_steps_fall_back_to_aggressive_extraction(self) -> None:
        # A command routinely prints progress before its JSON; an agent does not.
        step = parse_step({"id": "s", "type": "bash", "command": "true"})
        assert process_step_result('progress\n{"a": 1}', step) == {"a": 1}

    def test_parse_json_opts_into_aggressive_extraction(self) -> None:
        step = parse_step({"id": "s", "agent": "a", "prompt": "p", "parse_json": True})
        assert process_step_result("noise ```json\n{\"a\": 1}\n```", step) == {"a": 1}

    def test_parse_json_adds_the_output_rider_to_the_instruction(self, tmp_path: Path) -> None:
        agent = RecordingAgent()
        run_program(
            """
            steps:
              - id: ask
                agent: x:y
                prompt: Do the thing
                parse_json: true
            """,
            workspace=tmp_path,
            agent=agent,
        )
        _, instruction = agent.calls[0]
        assert "JSON OUTPUT REQUIRED" in instruction

    def test_mode_prefixes_the_instruction(self, tmp_path: Path) -> None:
        agent = RecordingAgent()
        run_program(
            """
            steps:
              - id: ask
                agent: x:y
                prompt: Do the thing
                mode: careful
            """,
            workspace=tmp_path,
            agent=agent,
        )
        assert agent.calls[0][1].startswith("MODE: careful\n\n")


# --------------------------------------------------------------------------
# bash -- mirrors test_bash_steps.py
# --------------------------------------------------------------------------


class TestBashSteps:
    def test_stdout_becomes_the_step_output(self, tmp_path: Path) -> None:
        outcome, ctx = run_program(
            """
            steps:
              - id: say
                type: bash
                command: echo hello
                output: greeting
            """,
            workspace=tmp_path,
        )
        assert outcome.status == "succeeded"
        assert ctx["greeting"] == "hello\n"

    def test_variables_are_substituted_into_the_command(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: say
                type: bash
                command: echo {{who}}
                output: greeting
            """,
            workspace=tmp_path,
            context={"who": "world"},
        )
        assert ctx["greeting"] == "world\n"

    def test_a_nonzero_exit_fails_the_run_by_default(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: boom
                type: bash
                command: "echo bad >&2; exit 3"
              - id: after
                type: bash
                command: echo unreachable
                output: after
            """,
            workspace=tmp_path,
        )
        assert outcome.status == "failed"
        assert "exit code 3" in str(outcome.error)
        assert outcome.completed_steps == ()
        assert "after" not in outcome.context

    def test_on_error_continue_absorbs_a_nonzero_exit(self, tmp_path: Path) -> None:
        outcome, ctx = run_program(
            """
            steps:
              - id: boom
                type: bash
                command: "exit 3"
                on_error: continue
                output_exit_code: code
              - id: after
                type: bash
                command: echo reached
                output: after
            """,
            workspace=tmp_path,
        )
        assert outcome.status == "succeeded"
        assert ctx["code"] == "3"
        assert ctx["after"] == "reached\n"

    def test_on_error_skip_remaining_stops_without_failing(self, tmp_path: Path) -> None:
        outcome, ctx = run_program(
            """
            steps:
              - id: boom
                type: bash
                command: "exit 3"
                on_error: skip_remaining
              - id: after
                type: bash
                command: echo unreachable
                output: after
            """,
            workspace=tmp_path,
        )
        # The recipe asked for exactly this: stop early, do not fail.
        assert outcome.status == "succeeded"
        assert "after" not in ctx

    def test_output_exit_code_is_recorded_as_a_string(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: ok
                type: bash
                command: "true"
                output_exit_code: code
            """,
            workspace=tmp_path,
        )
        assert ctx["code"] == "0"

    def test_a_missing_cwd_is_refused_by_name(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            f"""
            steps:
              - id: nope
                type: bash
                cwd: {tmp_path / "absent"}
                command: "true"
            """,
            workspace=tmp_path,
        )
        assert outcome.status == "failed"
        assert "cwd does not exist" in str(outcome.error)

    def test_env_values_are_substituted(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: show
                type: bash
                env:
                  GREETING: "hi {{who}}"
                command: echo "$GREETING"
                output: shown
            """,
            workspace=tmp_path,
            context={"who": "there"},
        )
        assert ctx["shown"] == "hi there\n"

    def test_amplifier_python_is_injected(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: show
                type: bash
                command: echo "${AMPLIFIER_PYTHON:-missing}"
                output: interpreter
            """,
            workspace=tmp_path,
        )
        assert ctx["interpreter"].strip().endswith(("python", "python3", "python3.11", "python3.12", "python3.13"))

    def test_a_command_timeout_fails_the_step(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: slow
                type: bash
                command: sleep 5
                timeout: 1
            """,
            workspace=tmp_path,
        )
        assert outcome.status == "failed"
        assert "timed out after 1s" in str(outcome.error)

    def test_a_bash_step_with_no_command_is_refused(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: empty
                type: bash
            """,
            workspace=tmp_path,
        )
        assert isinstance(outcome.error, UnsupportedStepError)


# --------------------------------------------------------------------------
# conditions -- mirrors test_executor_conditions.py
# --------------------------------------------------------------------------


class TestConditions:
    def test_a_false_condition_skips_the_step_visibly(self, tmp_path: Path) -> None:
        emitter = CollectingEmitter()
        outcome, ctx = run_program(
            """
            steps:
              - id: gated
                type: bash
                condition: "{{mode}} == 'full'"
                command: echo ran
                output: ran
            """,
            workspace=tmp_path,
            context={"mode": "quick"},
            emit=emitter,
        )
        assert outcome.status == "succeeded"
        assert "ran" not in ctx
        # Absence from completed_steps cannot tell "skipped" from "never
        # reached", so the skip is recorded in both places.
        assert ctx["_skipped_steps"] == ["gated"]
        assert "step:skipped" in emitter.kinds()
        assert outcome.completed_steps == ()

    def test_a_true_condition_runs_the_step(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: gated
                type: bash
                condition: "{{mode}} == 'full'"
                command: echo ran
                output: ran
            """,
            workspace=tmp_path,
            context={"mode": "full"},
        )
        assert ctx["ran"] == "ran\n"

    def test_a_broken_condition_fails_the_step_by_name(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: gated
                type: bash
                condition: "{{absent}} == 'x'"
                command: echo ran
            """,
            workspace=tmp_path,
        )
        assert outcome.status == "failed"
        assert "gated" in str(outcome.error)
        assert "condition error" in str(outcome.error)


# --------------------------------------------------------------------------
# foreach -- mirrors test_executor_loops.py
# --------------------------------------------------------------------------


class TestForeach:
    def test_collects_one_result_per_item_in_order(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                type: bash
                command: echo v-{{it}}
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": ["a", "b", "c"]},
        )
        assert ctx["seen"] == ["v-a\n", "v-b\n", "v-c\n"]

    def test_the_loop_variable_does_not_leak_after_the_loop(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                type: bash
                command: echo {{it}}
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": ["a"]},
        )
        assert "it" not in ctx

    def test_an_empty_list_skips_the_body_but_defines_collect(self, tmp_path: Path) -> None:
        # Leaving `seen` undefined would turn an empty input into an
        # undefined-variable crash three steps later.
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                type: bash
                command: echo never
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": []},
        )
        assert ctx["seen"] == []
        assert ctx["_skipped_steps"] == ["loop"]

    def test_a_non_list_is_refused_by_type(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                type: bash
                command: echo x
            """,
            workspace=tmp_path,
            context={"items": {"not": "a list"}},
        )
        assert outcome.status == "failed"
        assert "must be a list" in str(outcome.error)

    def test_max_iterations_is_enforced(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                max_iterations: 2
                type: bash
                command: echo x
            """,
            workspace=tmp_path,
            context={"items": [1, 2, 3]},
        )
        assert outcome.status == "failed"
        assert "max_iterations" in str(outcome.error)

    def test_output_records_the_last_iteration(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                type: bash
                command: echo {{it}}
                output: last
            """,
            workspace=tmp_path,
            context={"items": ["a", "b"]},
        )
        assert ctx["last"] == "b\n"

    def test_on_error_continue_keeps_a_slot_for_the_failed_iteration(self, tmp_path: Path) -> None:
        # A slot, not a missing entry: the collected list must stay
        # index-aligned with the input list or a downstream zip silently pairs
        # the wrong rows. For a BASH body the failure is absorbed inside the
        # step, so the slot holds its (empty) stdout rather than None -- the
        # legacy engine does the same, and the distinction matters because
        # `""` says "ran, produced nothing" while None says "did not run".
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                on_error: continue
                type: bash
                command: "test {{it}} != 2 && echo ok-{{it}}"
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": [1, 2, 3]},
        )
        assert ctx["seen"] == ["ok-1\n", "", "ok-3\n"]

    def test_an_agent_iteration_absorbed_by_the_loop_records_none(self, tmp_path: Path) -> None:
        # The other half of the pair above: an agent body has no exit code to
        # absorb, so the LOOP absorbs it and the slot is None.
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                on_error: continue
                agent: x:y
                prompt: go
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": ["a", "b"]},
            agent=ExplodingAgent(),
        )
        assert ctx["seen"] == [None, None]

    def test_a_failed_iteration_fails_the_step_by_default(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                type: bash
                command: "exit {{it}}"
            """,
            workspace=tmp_path,
            context={"items": [0, 1]},
        )
        assert outcome.status == "failed"
        assert "iteration 1 failed" in str(outcome.error)

    def test_a_multi_step_body_runs_each_sub_step(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                steps:
                  - id: first
                    type: bash
                    command: echo a-{{it}}
                    output: first_out
                  - id: second
                    type: bash
                    command: echo b-{{first_out}}
                    output: second_out
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": ["x"]},
        )
        # The body's last sub-step result is what the iteration contributes.
        assert ctx["seen"] == ["b-a-x\n"]

    def test_a_sub_step_condition_can_skip_within_the_body(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                steps:
                  - id: only-even
                    condition: "{{it}} == 2"
                    type: bash
                    command: echo even
                    output: even_out
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": [1, 2]},
        )
        assert ctx["seen"] == [None, "even\n"]


class TestForeachParallel:
    def test_results_stay_in_input_order(self, tmp_path: Path) -> None:
        # Deliberately reversed sleeps: if ordering came from completion time
        # rather than input position this assertion flips.
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                parallel: true
                type: bash
                command: "sleep 0.{{it}}; echo v-{{it}}"
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": [3, 2, 1]},
        )
        assert ctx["seen"] == ["v-3\n", "v-2\n", "v-1\n"]

    def test_bounded_parallelism_still_runs_every_item(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                parallel: 2
                type: bash
                command: echo {{it}}
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": [1, 2, 3, 4, 5]},
        )
        # Ints, not strings: a step whose whole stdout is clean JSON parses,
        # even without `parse_json`. That is the legacy engine's conservative
        # default, pinned here because it is surprising exactly once.
        assert ctx["seen"] == [1, 2, 3, 4, 5]

    def test_one_failure_fails_the_step_and_names_every_failure(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                parallel: true
                type: bash
                command: "exit {{it}}"
            """,
            workspace=tmp_path,
            context={"items": [0, 1, 2]},
        )
        assert outcome.status == "failed"
        assert "2/3 iterations failed" in str(outcome.error)

    def test_on_error_continue_keeps_the_successes(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: loop
                foreach: "{{items}}"
                as: it
                parallel: true
                on_error: continue
                type: bash
                command: "test {{it}} != 1 && echo ok-{{it}}"
                collect: seen
            """,
            workspace=tmp_path,
            context={"items": [0, 1, 2]},
        )
        assert ctx["seen"] == ["ok-0\n", "", "ok-2\n"]


# --------------------------------------------------------------------------
# while / convergence -- mirrors test_executor_loops.py
# --------------------------------------------------------------------------


class TestWhileLoop:
    def test_update_context_runs_after_the_body_and_drives_the_condition(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: converge
                while_condition: "{{n}} < 3"
                update_context:
                  n: "{{_loop_iteration}}"
                type: bash
                command: echo tick
                collect: ticks
            """,
            workspace=tmp_path,
            context={"n": 0},
        )
        assert ctx["ticks"] == ["tick\n", "tick\n", "tick\n"]
        assert ctx["n"] == "3"

    def test_break_when_is_checked_after_update_context(self, tmp_path: Path) -> None:
        # Order matters: a break test that ran before the mutation would read
        # the previous iteration's state and loop one extra time.
        _outcome, ctx = run_program(
            """
            steps:
              - id: converge
                while_condition: "{{n}} < 10"
                update_context:
                  n: "{{_loop_iteration}}"
                break_when: "{{n}} >= 2"
                type: bash
                command: echo tick
                collect: ticks
            """,
            workspace=tmp_path,
            context={"n": 0},
        )
        assert len(ctx["ticks"]) == 2

    def test_loop_metadata_is_injected_and_then_cleaned_up(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: converge
                while_condition: "{{n}} < 2"
                update_context:
                  n: "{{_loop_iteration}}"
                type: bash
                command: echo iter-{{_loop_index}}
                collect: ticks
            """,
            workspace=tmp_path,
            context={"n": 0},
        )
        assert ctx["ticks"] == ["iter-0\n", "iter-1\n"]
        assert "_loop_index" not in ctx
        assert "_loop_iteration" not in ctx

    def test_max_while_iterations_bounds_a_condition_that_never_falsifies(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: forever
                while_condition: "true"
                max_while_iterations: 4
                type: bash
                command: echo tick
                collect: ticks
            """,
            workspace=tmp_path,
        )
        assert len(ctx["ticks"]) == 4

    def test_a_false_condition_runs_the_body_zero_times(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: never
                while_condition: "{{n}} > 5"
                type: bash
                command: echo tick
                collect: ticks
            """,
            workspace=tmp_path,
            context={"n": 0},
        )
        assert ctx["ticks"] == []

    def test_a_multi_step_while_body_runs_in_sequence(self, tmp_path: Path) -> None:
        _outcome, ctx = run_program(
            """
            steps:
              - id: converge
                while_condition: "{{n}} < 2"
                update_context:
                  n: "{{_loop_iteration}}"
                steps:
                  - id: a
                    type: bash
                    command: echo a{{_loop_index}}
                    output: a_out
                  - id: b
                    type: bash
                    command: echo b-{{a_out}}
                    output: b_out
                collect: rounds
            """,
            workspace=tmp_path,
            context={"n": 0},
        )
        assert ctx["rounds"] == ["b-a0\n", "b-a1\n"]


# --------------------------------------------------------------------------
# timeouts -- mirrors test_templated_timeout.py
# --------------------------------------------------------------------------


class TestTimeouts:
    def test_a_literal_number_passes_through_untouched(self) -> None:
        step = parse_step({"id": "s", "timeout": 42})
        assert resolve_step_timeout(step, {}) == 42

    def test_a_numeric_string_is_normalised_at_parse_time(self) -> None:
        step = parse_step({"id": "s", "timeout": "900"})
        assert step.timeout == 900

    def test_a_template_resolves_against_the_run_context(self) -> None:
        step = parse_step({"id": "s", "timeout": "{{budget}}"})
        assert resolve_step_timeout(step, {"budget": 120}) == 120

    def test_an_unresolvable_template_names_the_step_and_the_field(self) -> None:
        step = parse_step({"id": "slow", "timeout": "{{budget}}"})
        with pytest.raises(ValueError) as excinfo:
            resolve_step_timeout(step, {})
        assert "slow" in str(excinfo.value)
        assert "timeout" in str(excinfo.value)

    def test_a_template_resolving_to_prose_is_refused(self) -> None:
        step = parse_step({"id": "slow", "timeout": "{{budget}}"})
        with pytest.raises(ValueError) as excinfo:
            resolve_step_timeout(step, {"budget": "soon"})
        assert "not a number of seconds" in str(excinfo.value)

    def test_a_non_positive_timeout_is_refused(self) -> None:
        step = parse_step({"id": "slow", "timeout": "{{budget}}"})
        with pytest.raises(ValueError) as excinfo:
            resolve_step_timeout(step, {"budget": 0})
        assert "must be positive" in str(excinfo.value)

    def test_an_agent_step_timeout_names_the_agent(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: slow
                agent: x:y
                prompt: wait
                timeout: 1
            """,
            workspace=tmp_path,
            agent=SlowAgent(),
        )
        assert outcome.status == "failed"
        assert "x:y" in str(outcome.error)
        assert "timed out after 1s" in str(outcome.error)


# --------------------------------------------------------------------------
# agent steps, retry and on_error -- mirrors test_on_error_correctness.py
# --------------------------------------------------------------------------


class TestAgentSteps:
    def test_an_agent_failure_fails_the_run_by_default(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: ask
                agent: x:y
                prompt: go
            """,
            workspace=tmp_path,
            agent=ExplodingAgent(),
        )
        assert outcome.status == "failed"
        assert "blew up" in str(outcome.error)
        assert outcome.completed_steps == ()

    def test_on_error_continue_records_none_and_carries_on(self, tmp_path: Path) -> None:
        outcome, ctx = run_program(
            """
            steps:
              - id: ask
                agent: x:y
                prompt: go
                on_error: continue
                output: answer
              - id: after
                type: bash
                command: echo reached
                output: after
            """,
            workspace=tmp_path,
            agent=ExplodingAgent(),
        )
        assert outcome.status == "succeeded"
        assert ctx["answer"] is None
        assert ctx["after"] == "reached\n"

    def test_retry_reattempts_before_giving_up(self, tmp_path: Path) -> None:
        attempts = {"n": 0}

        async def flaky(step: StepSpec, instruction: str, context: Mapping[str, Any]) -> Any:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient")
            return "recovered"

        outcome, ctx = run_program(
            """
            steps:
              - id: ask
                agent: x:y
                prompt: go
                output: answer
                retry:
                  max_attempts: 3
                  initial_delay: 0
            """,
            workspace=tmp_path,
            agent=flaky,
        )
        assert outcome.status == "succeeded"
        assert ctx["answer"] == "recovered"
        assert attempts["n"] == 3

    def test_a_step_with_no_agent_and_no_command_is_refused(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: nothing
                description: no agent, no command
            """,
            workspace=tmp_path,
        )
        assert isinstance(outcome.error, UnsupportedStepError)
        assert outcome.error.step_id == "nothing"

    def test_a_templated_agent_reference_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(
            """
            steps:
              - id: ask
                agent: "{{which}}"
                prompt: go
            """,
            workspace=tmp_path,
            context={"which": "x:y"},
        )
        assert isinstance(outcome.error, UnsupportedStepError)
        assert "templated" in str(outcome.error)


# --------------------------------------------------------------------------
# deliberate deltas from the legacy engine (docs/EXECUTOR_PARITY.md)
# --------------------------------------------------------------------------


class TestDeliberateDeltas:
    def test_a_nested_body_without_a_loop_is_refused_not_ignored(self, tmp_path: Path) -> None:
        # The legacy engine silently drops this body and runs the step as a
        # plain agent step. Discarding declared work while reporting success
        # is the fabricated success lib Core 8 forbids, so this refuses.
        outcome, _ctx = run_program(
            """
            steps:
              - id: loop
                agent: x:y
                prompt: go
                steps:
                  - id: inner
                    agent: x:y
                    prompt: inner
            """,
            workspace=tmp_path,
        )
        assert isinstance(outcome.error, UnsupportedStepError)
        assert outcome.error.step_id == "loop"

    def test_declaring_both_steps_and_stages_is_refused(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            program(
                """
                steps:
                  - id: flat
                    type: bash
                    command: "true"
                stages:
                  - name: only
                    steps:
                      - id: staged
                        type: bash
                        command: "true"
                """
            )
        assert "both 'stages' and 'steps'" in str(excinfo.value)

    def test_an_mention_sub_recipe_path_is_refused_by_name(self, tmp_path: Path) -> None:
        # A host mention resolver is Amplifier machinery the standalone runner
        # does not have. Saying so beats resolving it wrongly.
        recipe = tmp_path / "parent.yaml"
        recipe.write_text("steps: []\n", encoding="utf-8")
        outcome, _ctx = run_program(
            """
            steps:
              - id: sub
                type: recipe
                recipe: "@recipes:examples/thing.yaml"
            """,
            workspace=tmp_path,
            path=recipe,
            sub_recipe_runner=_null_sub_recipe_runner,
        )
        assert isinstance(outcome.error, UnsupportedStepError)
        assert "@mention" in str(outcome.error)


async def _null_sub_recipe_runner(path: Path, context: dict[str, Any], step: StepSpec, recursion: Any) -> Any:
    raise AssertionError("should not be reached")  # pragma: no cover


# --------------------------------------------------------------------------
# sub-recipes -- mirrors test_executor_composition.py
# --------------------------------------------------------------------------


class TestSubRecipes:
    def test_only_the_keys_the_sub_recipe_added_come_back(self, tmp_path: Path) -> None:
        seen: dict[str, Any] = {}

        async def runner(path: Path, context: dict[str, Any], step: StepSpec, recursion: Any) -> Any:
            seen["path"] = path
            seen["context"] = dict(context)
            return {"produced": 1}

        parent = tmp_path / "parent.yaml"
        parent.write_text("steps: []\n", encoding="utf-8")
        (tmp_path / "child.yaml").write_text("steps: []\n", encoding="utf-8")

        _outcome, ctx = run_program(
            """
            steps:
              - id: sub
                type: recipe
                recipe: child.yaml
                context:
                  task: "{{task}}"
                output: delta
            """,
            workspace=tmp_path,
            context={"task": {"id": 9}},
            path=parent,
            sub_recipe_runner=runner,
        )
        assert seen["path"] == (tmp_path / "child.yaml").resolve()
        # The dict survived as a dict: a JSON string here is the bug this
        # whole-variable rule exists to prevent.
        assert seen["context"] == {"task": {"id": 9}}
        assert ctx["delta"] == {"produced": 1}

    def test_a_missing_sub_recipe_is_named(self, tmp_path: Path) -> None:
        parent = tmp_path / "parent.yaml"
        parent.write_text("steps: []\n", encoding="utf-8")
        outcome, _ctx = run_program(
            """
            steps:
              - id: sub
                type: recipe
                recipe: absent.yaml
            """,
            workspace=tmp_path,
            path=parent,
            sub_recipe_runner=_null_sub_recipe_runner,
        )
        assert outcome.status == "failed"
        assert "Sub-recipe not found" in str(outcome.error)

    def test_recursion_depth_is_enforced(self, tmp_path: Path) -> None:
        parent = tmp_path / "parent.yaml"
        parent.write_text("steps: []\n", encoding="utf-8")
        (tmp_path / "child.yaml").write_text("steps: []\n", encoding="utf-8")
        outcome, _ctx = run_program(
            """
            recursion:
              max_depth: 1
              max_total_steps: 10
            steps:
              - id: sub
                type: recipe
                recipe: child.yaml
            """,
            workspace=tmp_path,
            path=parent,
            sub_recipe_runner=_null_sub_recipe_runner,
        )
        assert outcome.status == "failed"
        assert "recursion depth" in str(outcome.error)


# --------------------------------------------------------------------------
# staged recipes and approval gates -- mirrors test_approval_gates.py
# --------------------------------------------------------------------------


STAGED = """
stages:
  - name: prepare
    steps:
      - id: greet
        type: bash
        command: echo hello
        output: greeting
    approval:
      required: true
      prompt: "Continue after {{greeting}}?"
  - name: finish
    steps:
      - id: report
        type: bash
        command: echo done-{{_approval_message}}
        output: report
"""


class TestApprovalGates:
    def test_a_gate_with_no_way_to_answer_pauses_rather_than_passing(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(STAGED, workspace=tmp_path)

        assert outcome.status == "paused"
        assert outcome.pending_approval == "prepare"
        assert outcome.completed_steps == ("greet",)
        # The prompt is rendered, so the human sees the value they are
        # approving rather than the template that produced it.
        assert outcome.approval_prompt == "Continue after hello\n?"

    def test_a_paused_run_records_where_to_pick_up(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(STAGED, workspace=tmp_path)
        state = outcome.state
        assert state is not None
        assert state.pending_approval == "prepare"
        assert state.completed_stages == ("prepare",)
        assert state.context["greeting"] == "hello\n"

    def test_a_recorded_approval_lets_a_later_run_continue(self, tmp_path: Path) -> None:
        first, _ = run_program(STAGED, workspace=tmp_path)
        ledger = ApprovalLedger()
        ledger.record("prepare", approved=True, message="looks good")

        engine = StepEngine(
            program(STAGED),
            invoke_agent=RecordingAgent(),
            workspace=tmp_path,
            run_id="run-test",
            approvals=ledger,
        )
        context = dict(first.state.context)  # type: ignore[union-attr]
        outcome = asyncio.run(engine.execute(context, resume=first.state))

        assert outcome.status == "succeeded"
        assert context["report"] == "done-looks good\n"
        # The first stage's step is not re-run: the recorded list carries it.
        assert outcome.completed_steps == ("greet", "report")

    def test_a_recorded_denial_stops_the_run_and_names_the_stage(self, tmp_path: Path) -> None:
        first, _ = run_program(STAGED, workspace=tmp_path)
        ledger = ApprovalLedger()
        ledger.record("prepare", approved=False, message="not yet")

        engine = StepEngine(
            program(STAGED),
            invoke_agent=RecordingAgent(),
            workspace=tmp_path,
            run_id="run-test",
            approvals=ledger,
        )
        outcome = asyncio.run(engine.execute(dict(first.state.context), resume=first.state))  # type: ignore[union-attr]

        assert outcome.status == "failed"
        assert "denied at stage 'prepare'" in str(outcome.error)

    def test_a_still_pending_gate_pauses_again_rather_than_proceeding(self, tmp_path: Path) -> None:
        first, _ = run_program(STAGED, workspace=tmp_path)
        engine = StepEngine(
            program(STAGED),
            invoke_agent=RecordingAgent(),
            workspace=tmp_path,
            run_id="run-test",
        )
        outcome = asyncio.run(engine.execute(dict(first.state.context), resume=first.state))  # type: ignore[union-attr]

        assert outcome.status == "paused"
        assert outcome.pending_approval == "prepare"

    def test_an_approval_callback_answers_the_gate_inline(self, tmp_path: Path) -> None:
        callback = Approves(approved=True, message="ship it")
        outcome, ctx = run_program(STAGED, workspace=tmp_path, approval_callback=callback)

        assert outcome.status == "succeeded"
        assert ctx["report"] == "done-ship it\n"
        assert callback.requests[0].stage == "prepare"

    def test_a_callback_denial_fails_the_run(self, tmp_path: Path) -> None:
        outcome, _ctx = run_program(STAGED, workspace=tmp_path, approval_callback=Approves(approved=False, message="no"))

        assert outcome.status == "failed"
        assert "denied at stage 'prepare'" in str(outcome.error)

    def test_a_before_stage_gate_pauses_before_its_first_step_runs(self, tmp_path: Path) -> None:
        body = """
        stages:
          - name: dangerous
            approval:
              required: true
              when: before_stage
              prompt: "May we?"
            steps:
              - id: act
                type: bash
                command: echo acted
                output: acted
        """
        outcome, ctx = run_program(body, workspace=tmp_path)

        assert outcome.status == "paused"
        assert outcome.pending_approval == "dangerous"
        # Denying it must mean the work never happened, which is only true if
        # the gate fires before the step.
        assert "acted" not in ctx
        assert outcome.completed_steps == ()

    def test_an_approved_before_stage_gate_does_not_re_park_on_resume(self, tmp_path: Path) -> None:
        body = """
        stages:
          - name: dangerous
            approval:
              required: true
              when: before_stage
              prompt: "May we?"
            steps:
              - id: act
                type: bash
                command: echo acted
                output: acted
        """
        first, _ = run_program(body, workspace=tmp_path)
        ledger = ApprovalLedger()
        ledger.record("dangerous", approved=True, message="go")

        engine = StepEngine(
            program(body),
            invoke_agent=RecordingAgent(),
            workspace=tmp_path,
            run_id="run-test",
            approvals=ledger,
        )
        context = dict(first.state.context)  # type: ignore[union-attr]
        outcome = asyncio.run(engine.execute(context, resume=first.state))

        assert outcome.status == "succeeded"
        assert context["acted"] == "acted\n"

    def test_a_stage_without_a_gate_runs_straight_through(self, tmp_path: Path) -> None:
        outcome, ctx = run_program(
            """
            stages:
              - name: one
                steps:
                  - id: a
                    type: bash
                    command: echo a
                    output: a_out
              - name: two
                steps:
                  - id: b
                    type: bash
                    command: echo b-{{a_out}}
                    output: b_out
            """,
            workspace=tmp_path,
        )
        assert outcome.status == "succeeded"
        assert ctx["b_out"] == "b-a\n"


# --------------------------------------------------------------------------
# cancellation and resume plumbing
# --------------------------------------------------------------------------


class TestCancellationAndResume:
    def test_cancellation_stops_before_the_next_step(self, tmp_path: Path) -> None:
        outcome, ctx = run_program(
            """
            steps:
              - id: never
                type: bash
                command: echo ran
                output: ran
            """,
            workspace=tmp_path,
            cancellation=Cancelled(),
        )
        assert outcome.status == "cancelled"
        assert "ran" not in ctx

    def test_a_resumed_flat_run_skips_the_recorded_steps_by_id(self, tmp_path: Path) -> None:
        body = """
        steps:
          - id: first
            type: bash
            command: echo first
            output: first_out
          - id: second
            type: bash
            command: echo second
            output: second_out
        """
        engine = StepEngine(
            program(body),
            invoke_agent=RecordingAgent(),
            workspace=tmp_path,
            run_id="run-test",
        )
        context: dict[str, Any] = {}
        outcome = asyncio.run(engine.execute(context, resume=ResumeState(completed_steps=("first",))))

        assert outcome.status == "succeeded"
        assert "first_out" not in context
        assert context["second_out"] == "second\n"
        # The result describes the RUN, not just this attempt.
        assert outcome.completed_steps == ("first", "second")
