"""Tests for templated step `timeout:` values.

A step's ``timeout:`` may be a literal number or a template string resolved
against the run context (e.g. ``"{{step_timeout}}"``). These tests pin the
three things that matter:

1. The RESOLVED NUMBER -- never the raw template -- reaches
   ``asyncio.wait_for`` on both the agent-spawn path and the bash path.
2. A literal number behaves exactly as it did before templates existed.
3. A template that cannot resolve, or resolves to something that is not a
   positive number, fails loudly and names the step, the template, and what
   it resolved to -- and fails BEFORE anything is spawned.

Point 1 is the whole reason this is not just a validation change:
``asyncio.wait_for`` accepts a str argument and only explodes later, deep in
the event loop, with a TypeError naming neither the step nor the field.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step


class _MockSessionManager:
    """Minimal SessionManager stand-in for direct bash-step calls."""

    def __init__(self):
        self.states = {}

    def save_state(self, session_id, project_path, state):
        self.states[session_id] = state


class _MockCoordinator:
    """Minimal Coordinator stand-in for direct bash-step calls."""


@pytest.fixture
def executor() -> RecipeExecutor:
    return RecipeExecutor(_MockCoordinator(), _MockSessionManager())  # type: ignore[arg-type]


@pytest.fixture
def spawn_coordinator():
    """Coordinator whose `session.spawn` capability is an AsyncMock."""
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock(return_value="ok")
    return coordinator


@pytest.fixture
def spawn_session_manager():
    manager = MagicMock()
    manager.create_session.return_value = "test-session-id"
    manager.load_state.return_value = {
        "current_step_index": 0,
        "context": {},
        "completed_steps": [],
        "started": "2025-01-01T00:00:00",
    }
    manager.is_cancellation_requested.return_value = False
    manager.is_immediate_cancellation.return_value = False
    return manager


# ---------------------------------------------------------------------------
# Validation: what is accepted, what is rejected, and with what message
# ---------------------------------------------------------------------------


class TestTimeoutValidation:
    def test_literal_int_validates_and_stays_int(self):
        """The pre-existing common case is untouched."""
        step = Step(id="s", agent="a", prompt="p", timeout=1800)
        assert step.timeout == 1800
        assert isinstance(step.timeout, int)
        assert not [e for e in step.validate() if "timeout" in e.lower()]

    def test_default_timeout_unchanged(self):
        """The default is still 600 seconds as an int."""
        step = Step(id="s", agent="a", prompt="p")
        assert step.timeout == 600
        assert isinstance(step.timeout, int)

    def test_template_string_passes_validation(self):
        """A template cannot be checked now -- its value is deferred."""
        step = Step(id="s", agent="a", prompt="p", timeout="{{step_timeout}}")
        assert not [e for e in step.validate() if "timeout" in e.lower()]

    def test_template_embedded_in_text_passes_validation(self):
        """Validation defers on any '{{' -- resolution decides at run time."""
        step = Step(id="s", agent="a", prompt="p", timeout="{{base}}0")
        assert not [e for e in step.validate() if "timeout" in e.lower()]

    @pytest.mark.parametrize("bad", [0, -1, -600])
    def test_non_positive_int_still_rejected(self, bad):
        """The original message is preserved for the original failure."""
        step = Step(id="s", agent="a", prompt="p", timeout=bad)
        errors = [e for e in step.validate() if "timeout" in e.lower()]
        assert errors == ["Step 's': timeout must be positive"]

    def test_non_numeric_non_template_string_rejected_with_clear_message(self):
        step = Step(id="s", agent="a", prompt="p", timeout="notanumber")
        errors = [e for e in step.validate() if "timeout" in e.lower()]
        assert len(errors) == 1
        message = errors[0]
        assert "Step 's'" in message
        assert "notanumber" in message  # names the offending value
        assert "template" in message.lower()  # names the escape hatch

    def test_bool_timeout_rejected(self):
        """True is an int in Python but is never a number of seconds."""
        step = Step(id="s", agent="a", prompt="p", timeout=True)  # type: ignore[arg-type]
        assert [e for e in step.validate() if "timeout" in e.lower()]

    def test_none_timeout_rejected(self):
        step = Step(id="s", agent="a", prompt="p", timeout=None)  # type: ignore[arg-type]
        assert [e for e in step.validate() if "timeout" in e.lower()]


class TestTimeoutParsing:
    def test_plain_numeric_string_coerced_to_int(self):
        """YAML quoting an int must not change its meaning."""
        step = Recipe._parse_step(
            {"id": "t", "type": "agent", "agent": "a", "prompt": "p", "timeout": "900"}
        )
        assert step.timeout == 900
        assert isinstance(step.timeout, int)

    def test_template_string_preserved_verbatim(self):
        step = Recipe._parse_step(
            {
                "id": "t",
                "type": "agent",
                "agent": "a",
                "prompt": "p",
                "timeout": "{{my_timeout}}",
            }
        )
        assert step.timeout == "{{my_timeout}}"

    def test_literal_int_passes_through_untouched(self):
        step = Recipe._parse_step(
            {"id": "t", "type": "agent", "agent": "a", "prompt": "p", "timeout": 42}
        )
        assert step.timeout == 42
        assert isinstance(step.timeout, int)

    def test_junk_string_left_for_validation_to_report(self):
        """_parse_step does not raise -- validate() names it by value."""
        step = Recipe._parse_step(
            {"id": "t", "type": "agent", "agent": "a", "prompt": "p", "timeout": "abc"}
        )
        assert step.timeout == "abc"
        assert [e for e in step.validate() if "timeout" in e.lower()]

    def test_from_yaml_end_to_end(self, temp_dir: Path):
        recipe_file = temp_dir / "templated.yaml"
        recipe_file.write_text(
            "name: templated\n"
            "description: templated timeout\n"
            "version: 1.0.0\n"
            "context:\n"
            "  step_timeout: 1800\n"
            "steps:\n"
            "  - id: work\n"
            "    agent: a\n"
            "    prompt: p\n"
            '    timeout: "{{step_timeout}}"\n'
        )
        recipe = Recipe.from_yaml(recipe_file)
        assert recipe.steps[0].timeout == "{{step_timeout}}"
        assert recipe.validate() == []


# ---------------------------------------------------------------------------
# Resolution helper
# ---------------------------------------------------------------------------


class TestResolveStepTimeout:
    def test_template_resolves_to_int(self, executor: RecipeExecutor):
        step = Step(id="s", agent="a", prompt="p", timeout="{{t}}")
        resolved = executor._resolve_step_timeout(step, {"t": 1800})
        assert resolved == 1800
        assert isinstance(resolved, int)

    def test_template_resolves_from_string_context_value(
        self, executor: RecipeExecutor
    ):
        step = Step(id="s", agent="a", prompt="p", timeout="{{t}}")
        assert executor._resolve_step_timeout(step, {"t": "1800"}) == 1800

    def test_template_resolves_from_dotted_path(self, executor: RecipeExecutor):
        step = Step(id="s", agent="a", prompt="p", timeout="{{limits.slow}}")
        assert executor._resolve_step_timeout(step, {"limits": {"slow": 45}}) == 45

    def test_literal_int_returned_unchanged(self, executor: RecipeExecutor):
        step = Step(id="s", agent="a", prompt="p", timeout=1800)
        resolved = executor._resolve_step_timeout(step, {})
        assert resolved == 1800
        assert isinstance(resolved, int)

    def test_literal_float_returned_unchanged(self, executor: RecipeExecutor):
        """Floats worked before templates existed; they still do."""
        step = Step(id="s", agent="a", prompt="p", timeout=1.5)
        assert executor._resolve_step_timeout(step, {}) == 1.5

    def test_undefined_variable_names_step_and_variable(self, executor: RecipeExecutor):
        step = Step(id="slow-step", agent="a", prompt="p", timeout="{{missing}}")
        with pytest.raises(ValueError) as exc:
            executor._resolve_step_timeout(step, {"other": 1})
        message = str(exc.value)
        assert "slow-step" in message
        assert "missing" in message
        assert "timeout" in message.lower()

    def test_non_numeric_resolution_names_what_it_resolved_to(
        self, executor: RecipeExecutor
    ):
        step = Step(id="slow-step", agent="a", prompt="p", timeout="{{t}}")
        with pytest.raises(ValueError) as exc:
            executor._resolve_step_timeout(step, {"t": "soon"})
        message = str(exc.value)
        assert "slow-step" in message
        assert "soon" in message
        assert "not a number" in message

    @pytest.mark.parametrize("value", [0, -30])
    def test_non_positive_resolution_rejected(
        self, executor: RecipeExecutor, value: int
    ):
        step = Step(id="s", agent="a", prompt="p", timeout="{{t}}")
        with pytest.raises(ValueError, match="must be positive"):
            executor._resolve_step_timeout(step, {"t": value})


# ---------------------------------------------------------------------------
# Bash path: the resolved number reaches asyncio.wait_for
# ---------------------------------------------------------------------------


class TestBashStepTimeout:
    @pytest.mark.asyncio
    async def test_templated_timeout_allows_command_to_finish(
        self, executor: RecipeExecutor, temp_dir: Path
    ):
        step = Step(
            id="s", type="bash", command="echo hello", timeout="{{step_timeout}}"
        )
        result = await executor._execute_bash_step(step, {"step_timeout": 30}, temp_dir)
        assert result.stdout.strip() == "hello"
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_templated_timeout_actually_fires(
        self, executor: RecipeExecutor, temp_dir: Path
    ):
        """A resolved 1s timeout kills the command -- and the message reports 1s."""
        step = Step(id="s", type="bash", command="sleep 10", timeout="{{t}}")
        with pytest.raises(ValueError) as exc:
            await executor._execute_bash_step(step, {"t": 1}, temp_dir)
        message = str(exc.value)
        assert "timed out after 1s" in message
        assert "{{t}}" not in message  # the resolved value, not the template

    @pytest.mark.asyncio
    async def test_literal_timeout_unchanged(
        self, executor: RecipeExecutor, temp_dir: Path
    ):
        step = Step(id="s", type="bash", command="sleep 10", timeout=1)
        with pytest.raises(ValueError, match="timed out after 1s"):
            await executor._execute_bash_step(step, {}, temp_dir)

    @pytest.mark.asyncio
    async def test_wait_for_receives_a_number_not_a_string(
        self, executor: RecipeExecutor, temp_dir: Path, monkeypatch
    ):
        """The load-bearing assertion: wait_for never sees the raw template."""
        seen: list = []
        real_wait_for = asyncio.wait_for

        async def spy(coro, timeout=None):
            seen.append(timeout)
            return await real_wait_for(coro, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", spy)

        step = Step(id="s", type="bash", command="echo hi", timeout="{{t}}")
        await executor._execute_bash_step(step, {"t": "1800"}, temp_dir)

        assert seen == [1800]
        assert isinstance(seen[0], int)

    @pytest.mark.asyncio
    async def test_unresolvable_template_fails_before_spawning_anything(
        self, executor: RecipeExecutor, temp_dir: Path, monkeypatch
    ):
        """An unresolvable timeout must not run the command first."""

        async def boom(*args, **kwargs):
            raise AssertionError("subprocess must not be spawned")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

        step = Step(id="s", type="bash", command="echo hi", timeout="{{never_defined}}")
        with pytest.raises(ValueError, match="never_defined"):
            await executor._execute_bash_step(step, {}, temp_dir)

    @pytest.mark.asyncio
    async def test_non_numeric_resolution_fails_before_spawning_anything(
        self, executor: RecipeExecutor, temp_dir: Path, monkeypatch
    ):
        async def boom(*args, **kwargs):
            raise AssertionError("subprocess must not be spawned")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

        step = Step(id="s", type="bash", command="echo hi", timeout="{{t}}")
        with pytest.raises(ValueError, match="not a number"):
            await executor._execute_bash_step(step, {"t": "later"}, temp_dir)


# ---------------------------------------------------------------------------
# Agent-spawn path: the same resolution, on the path PR #77 left uncovered
# ---------------------------------------------------------------------------


class TestAgentStepTimeout:
    @pytest.mark.asyncio
    async def test_wait_for_receives_resolved_number(
        self, spawn_coordinator, spawn_session_manager, temp_dir: Path, monkeypatch
    ):
        seen: list = []
        real_wait_for = asyncio.wait_for

        async def spy(coro, timeout=None):
            seen.append(timeout)
            return await real_wait_for(coro, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", spy)

        executor = RecipeExecutor(spawn_coordinator, spawn_session_manager)
        step = Step(id="work", agent="a", prompt="p", timeout="{{step_timeout}}")

        await executor.execute_step(step, {"step_timeout": 1800})

        assert 1800 in seen
        assert all(not isinstance(t, str) for t in seen)

    @pytest.mark.asyncio
    async def test_literal_timeout_reaches_wait_for_unchanged(
        self, spawn_coordinator, spawn_session_manager, temp_dir: Path, monkeypatch
    ):
        seen: list = []
        real_wait_for = asyncio.wait_for

        async def spy(coro, timeout=None):
            seen.append(timeout)
            return await real_wait_for(coro, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", spy)

        executor = RecipeExecutor(spawn_coordinator, spawn_session_manager)
        step = Step(id="work", agent="a", prompt="p", timeout=1800)

        await executor.execute_step(step, {})

        assert 1800 in seen

    @pytest.mark.asyncio
    async def test_templated_timeout_actually_fires_on_spawn(
        self, spawn_coordinator, spawn_session_manager
    ):
        """A slow agent is cancelled at the RESOLVED timeout, named in the error."""

        async def slow_spawn(**kwargs):
            await asyncio.sleep(10)

        spawn_coordinator.get_capability.return_value = slow_spawn

        executor = RecipeExecutor(spawn_coordinator, spawn_session_manager)
        step = Step(id="work", agent="slowpoke", prompt="p", timeout="{{t}}")

        with pytest.raises(ValueError) as exc:
            await executor.execute_step(step, {"t": 1})

        message = str(exc.value)
        assert "timed out after 1s" in message
        assert "{{t}}" not in message

    @pytest.mark.asyncio
    async def test_unresolvable_template_fails_before_spawn_is_called(
        self, spawn_coordinator, spawn_session_manager
    ):
        """An unresolvable timeout must not burn an agent invocation."""
        spawn = AsyncMock(return_value="ok")
        spawn_coordinator.get_capability.return_value = spawn

        executor = RecipeExecutor(spawn_coordinator, spawn_session_manager)
        step = Step(id="work", agent="a", prompt="p", timeout="{{never_defined}}")

        with pytest.raises(ValueError, match="never_defined"):
            await executor.execute_step(step, {})

        assert not spawn.called
