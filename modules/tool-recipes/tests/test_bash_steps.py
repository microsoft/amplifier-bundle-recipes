"""Tests for bash step type - direct shell execution without LLM overhead."""

import os
import sys
from pathlib import Path

import pytest

from amplifier_module_tool_recipes import executor as executor_mod
from amplifier_module_tool_recipes.executor import BashResult, RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe, Step


class MockSessionManager:
    """Minimal mock for SessionManager."""

    def __init__(self):
        self.calls = []
        self.states = {}

    def save_state(self, session_id, project_path, state):
        self.states[session_id] = state


class MockCoordinator:
    """Minimal mock for Coordinator."""

    def __init__(self):
        self.calls = []


class TestBashStepModel:
    """Tests for bash step model validation."""

    def test_bash_step_creation_minimal(self):
        """Bash step can be created with required fields only."""
        step = Step(id="test", type="bash", command="echo hello")
        assert step.id == "test"
        assert step.type == "bash"
        assert step.command == "echo hello"
        assert step.cwd is None
        assert step.env is None
        assert step.output_exit_code is None

    def test_bash_step_creation_full(self):
        """Bash step can be created with all fields."""
        step = Step(
            id="test",
            type="bash",
            command="echo $VAR",
            cwd="/tmp",
            env={"VAR": "hello"},
            output="result",
            output_exit_code="exit_code",
            timeout=30,
        )
        assert step.command == "echo $VAR"
        assert step.cwd == "/tmp"
        assert step.env == {"VAR": "hello"}
        assert step.output == "result"
        assert step.output_exit_code == "exit_code"
        assert step.timeout == 30

    def test_bash_step_validation_valid(self):
        """Valid bash step should have no errors."""
        step = Step(id="test", type="bash", command="echo hello")
        errors = step.validate()
        assert errors == []

    def test_bash_step_validation_missing_command(self):
        """Bash step without command should fail validation."""
        step = Step(id="test", type="bash", command=None)
        errors = step.validate()
        assert any("command" in e.lower() for e in errors)

    def test_bash_step_validation_empty_command(self):
        """Bash step with empty command should fail validation."""
        step = Step(id="test", type="bash", command="")
        errors = step.validate()
        assert any("command" in e.lower() for e in errors)

    def test_bash_step_validation_whitespace_command(self):
        """Bash step with whitespace-only command should fail validation."""
        step = Step(id="test", type="bash", command="   ")
        errors = step.validate()
        assert any("whitespace" in e.lower() for e in errors)

    def test_bash_step_cannot_have_agent(self):
        """Bash step cannot have agent field."""
        step = Step(id="test", type="bash", command="echo hello", agent="some-agent")
        errors = step.validate()
        assert any("agent" in e.lower() for e in errors)

    def test_bash_step_cannot_have_prompt(self):
        """Bash step cannot have prompt field."""
        step = Step(id="test", type="bash", command="echo hello", prompt="some prompt")
        errors = step.validate()
        assert any("prompt" in e.lower() for e in errors)

    def test_bash_step_cannot_have_mode(self):
        """Bash step cannot have mode field."""
        step = Step(id="test", type="bash", command="echo hello", mode="ANALYZE")
        errors = step.validate()
        assert any("mode" in e.lower() for e in errors)

    def test_bash_step_cannot_have_agent_config(self):
        """Bash step cannot have agent_config field."""
        step = Step(
            id="test", type="bash", command="echo hello", agent_config={"key": "value"}
        )
        errors = step.validate()
        assert any("agent_config" in e.lower() for e in errors)

    def test_bash_step_cannot_have_recipe(self):
        """Bash step cannot have recipe field."""
        step = Step(
            id="test", type="bash", command="echo hello", recipe="some-recipe.yaml"
        )
        errors = step.validate()
        assert any("recipe" in e.lower() for e in errors)

    def test_bash_step_output_exit_code_validation(self):
        """output_exit_code must be valid variable name."""
        step = Step(
            id="test", type="bash", command="echo hello", output_exit_code="valid_name"
        )
        errors = step.validate()
        assert not any("output_exit_code" in e.lower() for e in errors)

    def test_bash_step_output_exit_code_invalid_name(self):
        """output_exit_code with invalid chars should fail."""
        step = Step(
            id="test",
            type="bash",
            command="echo hello",
            output_exit_code="invalid-name!",
        )
        errors = step.validate()
        assert any("output_exit_code" in e.lower() for e in errors)

    def test_bash_step_output_exit_code_reserved_name(self):
        """output_exit_code cannot use reserved names."""
        for reserved in ["recipe", "session", "step"]:
            step = Step(
                id="test", type="bash", command="echo hello", output_exit_code=reserved
            )
            errors = step.validate()
            assert any("reserved" in e.lower() for e in errors)


class TestBashStepExecution:
    """Tests for bash step execution."""

    @pytest.fixture
    def executor(self) -> RecipeExecutor:
        """Create executor with mock dependencies."""
        return RecipeExecutor(MockCoordinator(), MockSessionManager())  # type: ignore[arg-type]

    @pytest.fixture
    def project_path(self, tmp_path: Path) -> Path:
        """Create a temporary project directory."""
        return tmp_path

    @pytest.mark.asyncio
    async def test_execute_simple_command(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Simple echo command should return stdout."""
        step = Step(id="test", type="bash", command="echo hello")
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        assert isinstance(result, BashResult)
        assert result.stdout.strip() == "hello"
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_execute_with_variable_substitution(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Variables in command should be substituted."""
        step = Step(id="test", type="bash", command="echo {{message}}")
        context = {"message": "world"}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stdout.strip() == "world"

    @pytest.mark.asyncio
    async def test_execute_with_env_variables(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Environment variables should be passed to command."""
        step = Step(
            id="test", type="bash", command="echo $MY_VAR", env={"MY_VAR": "from_env"}
        )
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stdout.strip() == "from_env"

    @pytest.mark.asyncio
    async def test_execute_with_env_variable_substitution(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Variables in env values should be substituted."""
        step = Step(
            id="test", type="bash", command="echo $MY_VAR", env={"MY_VAR": "{{value}}"}
        )
        context = {"value": "substituted"}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stdout.strip() == "substituted"

    @pytest.mark.asyncio
    async def test_execute_with_cwd(self, executor: RecipeExecutor, project_path: Path):
        """Command should run in specified working directory."""
        subdir = project_path / "subdir"
        subdir.mkdir()

        step = Step(id="test", type="bash", command="pwd", cwd=str(subdir))
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stdout.strip() == str(subdir)

    @pytest.mark.asyncio
    async def test_execute_with_cwd_variable_substitution(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Variables in cwd should be substituted."""
        subdir = project_path / "mydir"
        subdir.mkdir()

        step = Step(id="test", type="bash", command="pwd", cwd="{{dir_path}}")
        context = {"dir_path": str(subdir)}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stdout.strip() == str(subdir)

    @pytest.mark.asyncio
    async def test_execute_with_relative_cwd(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Relative cwd should be resolved from project path."""
        subdir = project_path / "relative"
        subdir.mkdir()

        step = Step(id="test", type="bash", command="pwd", cwd="relative")
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stdout.strip() == str(subdir)

    @pytest.mark.asyncio
    async def test_execute_nonexistent_cwd_fails(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Command with non-existent cwd should fail."""
        step = Step(id="test", type="bash", command="pwd", cwd="/nonexistent/path")
        context: dict = {}

        with pytest.raises(ValueError) as exc_info:
            await executor._execute_bash_step(step, context, project_path)

        assert "cwd does not exist" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_nonzero_exit_code(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Non-zero exit code should raise error with on_error=fail."""
        step = Step(id="test", type="bash", command="exit 1", on_error="fail")
        context: dict = {}

        with pytest.raises(ValueError) as exc_info:
            await executor._execute_bash_step(step, context, project_path)

        assert "exit code 1" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_nonzero_exit_code_continue(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Non-zero exit code with on_error=continue should return result."""
        step = Step(id="test", type="bash", command="exit 42", on_error="continue")
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.exit_code == 42

    @pytest.mark.asyncio
    async def test_execute_captures_stderr(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Stderr should be captured."""
        step = Step(
            id="test", type="bash", command="echo error >&2", on_error="continue"
        )
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stderr.strip() == "error"

    @pytest.mark.asyncio
    async def test_execute_timeout(self, executor: RecipeExecutor, project_path: Path):
        """Command exceeding timeout should be killed."""
        step = Step(id="test", type="bash", command="sleep 10", timeout=1)
        context: dict = {}

        with pytest.raises(ValueError) as exc_info:
            await executor._execute_bash_step(step, context, project_path)

        assert "timed out" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_multiline_command(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Multiline commands should work."""
        step = Step(
            id="test",
            type="bash",
            command="""
            A=1
            B=2
            echo $((A + B))
            """,
        )
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stdout.strip() == "3"

    @pytest.mark.asyncio
    async def test_execute_pipe_command(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Piped commands should work."""
        step = Step(id="test", type="bash", command="echo 'a\nb\nc' | wc -l")
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stdout.strip() == "3"

    @pytest.mark.asyncio
    async def test_execute_inherits_environment(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Command should inherit parent environment."""
        # Set a unique env var to test inheritance
        os.environ["TEST_BASH_STEP_VAR"] = "inherited"
        try:
            step = Step(id="test", type="bash", command="echo $TEST_BASH_STEP_VAR")
            context: dict = {}

            result = await executor._execute_bash_step(step, context, project_path)

            assert result.stdout.strip() == "inherited"
        finally:
            del os.environ["TEST_BASH_STEP_VAR"]

    @pytest.mark.asyncio
    async def test_execute_injects_amplifier_python(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """AMPLIFIER_PYTHON env var should be injected with the current Python executable."""
        step = Step(id="test", type="bash", command="echo $AMPLIFIER_PYTHON")
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        amplifier_python = result.stdout.strip()
        assert amplifier_python != "", "AMPLIFIER_PYTHON must be set (not empty)"
        assert "python" in amplifier_python.lower(), (
            f"AMPLIFIER_PYTHON should point to a Python executable, got: {amplifier_python}"
        )

    @pytest.mark.asyncio
    async def test_amplifier_python_is_current_interpreter(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """AMPLIFIER_PYTHON should match the current interpreter.

        On POSIX this is byte-identical to ``sys.executable``. On Windows the
        path is normalised to forward slashes so bash can execute it, so
        compare against the normalised form rather than raw ``sys.executable``.
        """
        step = Step(id="test", type="bash", command="echo $AMPLIFIER_PYTHON")
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        assert result.stdout.strip() == executor_mod._resolve_amplifier_python()

    @pytest.mark.asyncio
    async def test_amplifier_python_not_overridden_by_step_env(
        self, executor: RecipeExecutor, project_path: Path
    ):
        """Step-level env should be able to override AMPLIFIER_PYTHON if explicitly set."""
        step = Step(
            id="test",
            type="bash",
            command="echo $AMPLIFIER_PYTHON",
            env={"AMPLIFIER_PYTHON": "/custom/python"},
        )
        context: dict = {}

        result = await executor._execute_bash_step(step, context, project_path)

        # Step env overrides the injected value (step env is applied after injection)
        assert result.stdout.strip() == "/custom/python"


class TestBashStepYamlParsing:
    """Tests for parsing bash steps from YAML."""

    def test_parse_minimal_bash_step(self, tmp_path: Path):
        """Minimal bash step should parse correctly."""
        yaml_content = """
name: test-recipe
description: Test bash steps
version: "1.0.0"

steps:
  - id: echo-test
    type: bash
    command: echo hello
"""
        recipe_file = tmp_path / "recipe.yaml"
        recipe_file.write_text(yaml_content)
        recipe = Recipe.from_yaml(recipe_file)
        assert len(recipe.steps) == 1
        step = recipe.steps[0]
        assert step.type == "bash"
        assert step.command == "echo hello"

    def test_parse_full_bash_step(self, tmp_path: Path):
        """Full bash step with all fields should parse correctly."""
        yaml_content = """
name: test-recipe
description: Test bash steps
version: "1.0.0"

steps:
  - id: full-bash
    type: bash
    command: echo $VAR
    cwd: /tmp
    env:
      VAR: hello
      OTHER: world
    output: result
    output_exit_code: exit_code
    timeout: 30
    on_error: continue
"""
        recipe_file = tmp_path / "recipe.yaml"
        recipe_file.write_text(yaml_content)
        recipe = Recipe.from_yaml(recipe_file)
        step = recipe.steps[0]
        assert step.type == "bash"
        assert step.command == "echo $VAR"
        assert step.cwd == "/tmp"
        assert step.env == {"VAR": "hello", "OTHER": "world"}
        assert step.output == "result"
        assert step.output_exit_code == "exit_code"
        assert step.timeout == 30
        assert step.on_error == "continue"

    def test_parse_bash_step_with_variables(self, tmp_path: Path):
        """Bash step with variable references should parse."""
        yaml_content = """
name: test-recipe
description: Test bash steps
version: "1.0.0"

context:
  api_url: https://api.example.com

steps:
  - id: fetch-data
    type: bash
    command: curl {{api_url}}/data
    output: data
"""
        recipe_file = tmp_path / "recipe.yaml"
        recipe_file.write_text(yaml_content)
        recipe = Recipe.from_yaml(recipe_file)
        step = recipe.steps[0]
        assert "{{api_url}}" in step.command

    def test_parse_mixed_step_types(self, tmp_path: Path):
        """Recipe with mixed step types should parse correctly."""
        yaml_content = """
name: test-recipe
description: Test mixed steps
version: "1.0.0"

steps:
  - id: agent-step
    agent: foundation:zen-architect
    prompt: Analyze something
    output: analysis

  - id: bash-step
    type: bash
    command: echo {{analysis}}
    output: processed

  - id: another-agent
    agent: foundation:modular-builder
    prompt: Build based on {{processed}}
"""
        recipe_file = tmp_path / "recipe.yaml"
        recipe_file.write_text(yaml_content)
        recipe = Recipe.from_yaml(recipe_file)
        assert len(recipe.steps) == 3
        assert recipe.steps[0].type == "agent"
        assert recipe.steps[1].type == "bash"
        assert recipe.steps[2].type == "agent"


class TestBashResolution:
    """Tests for cross-platform bash executable resolution.

    These patch platform state rather than requiring a real Windows host, so
    the Windows behaviour is exercised on every CI platform.
    """

    def test_posix_returns_bin_bash(self, monkeypatch: pytest.MonkeyPatch):
        """On POSIX, resolution is unchanged: always /bin/bash."""
        monkeypatch.setattr(executor_mod.os, "name", "posix")
        assert executor_mod._resolve_bash() == "/bin/bash"

    def test_posix_ignores_windows_env(self, monkeypatch: pytest.MonkeyPatch):
        """POSIX path must not consult Program Files or PATH at all."""
        monkeypatch.setattr(executor_mod.os, "name", "posix")
        monkeypatch.setenv("ProgramFiles", r"C:\Program Files")

        def _fail(*_args, **_kwargs):
            raise AssertionError("POSIX resolution must not probe for git bash")

        monkeypatch.setattr(executor_mod.shutil, "which", _fail)
        assert executor_mod._resolve_bash() == "/bin/bash"

    def test_windows_prefers_git_bash_over_wsl(self, monkeypatch: pytest.MonkeyPatch):
        """Git Bash on disk wins even when WSL's bash.exe is first on PATH."""
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        monkeypatch.setattr(executor_mod.os, "name", "nt")
        monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
        monkeypatch.setattr(
            executor_mod.os.path, "isfile", lambda p: p == git_bash
        )
        # PATH lookup would find the WSL shim first -- must be ignored.
        monkeypatch.setattr(
            executor_mod.shutil,
            "which",
            lambda name: r"C:\Windows\System32\bash.exe" if name == "bash" else None,
        )

        assert executor_mod._resolve_bash() == git_bash

    def test_windows_finds_per_user_install(self, monkeypatch: pytest.MonkeyPatch):
        """Per-user (winget) Git install under LOCALAPPDATA is found."""
        git_bash = r"C:\Users\dev\AppData\Local\Programs\Git\bin\bash.exe"
        monkeypatch.setattr(executor_mod.os, "name", "nt")
        monkeypatch.delenv("ProgramFiles", raising=False)
        monkeypatch.delenv("ProgramW6432", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\dev\AppData\Local")
        monkeypatch.setattr(
            executor_mod.os.path, "isfile", lambda p: p == git_bash
        )
        monkeypatch.setattr(executor_mod.shutil, "which", lambda _name: None)

        assert executor_mod._resolve_bash() == git_bash

    def test_windows_derives_bash_from_git_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Non-standard install location is derived from git.exe on PATH."""
        git_exe = r"D:\tools\Git\cmd\git.exe"
        git_bash = r"D:\tools\Git\bin\bash.exe"
        monkeypatch.setattr(executor_mod.os, "name", "nt")
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(
            executor_mod.os.path, "isfile", lambda p: p == git_bash
        )
        monkeypatch.setattr(
            executor_mod.shutil,
            "which",
            lambda name: git_exe if name == "git" else None,
        )
        # dirname twice on a Windows path -- emulate ntpath on POSIX hosts.
        monkeypatch.setattr(
            executor_mod.os.path,
            "dirname",
            lambda p: p.rsplit("\\", 1)[0] if "\\" in p else "",
        )

        assert executor_mod._resolve_bash() == git_bash

    def test_windows_rejects_wsl_only_with_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """WSL-only is rejected loudly: it cannot reach the Amplifier interpreter."""
        monkeypatch.setattr(executor_mod.os, "name", "nt")
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(executor_mod.os.path, "isfile", lambda _p: False)
        monkeypatch.setattr(
            executor_mod.shutil,
            "which",
            lambda name: r"C:\Windows\System32\bash.exe" if name == "bash" else None,
        )

        with pytest.raises(ValueError) as exc_info:
            executor_mod._resolve_bash()

        message = str(exc_info.value)
        assert "WSL" in message, "error must name WSL as the cause"
        assert "git-scm.com" in message, "error must give an actionable fix"

    def test_windows_no_bash_at_all_errors(self, monkeypatch: pytest.MonkeyPatch):
        """No bash anywhere produces a clear install instruction."""
        monkeypatch.setattr(executor_mod.os, "name", "nt")
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(executor_mod.os.path, "isfile", lambda _p: False)
        monkeypatch.setattr(executor_mod.shutil, "which", lambda _name: None)

        with pytest.raises(ValueError, match="git-scm.com"):
            executor_mod._resolve_bash()

    def test_wsl_shim_detection(self):
        """System32/Sysnative bash.exe is the WSL shim; Git Bash is not."""
        assert executor_mod._is_wsl_bash(r"C:\Windows\System32\bash.exe")
        assert executor_mod._is_wsl_bash(r"C:\Windows\Sysnative\bash.exe")
        assert executor_mod._is_wsl_bash("C:/Windows/System32/bash.exe")
        assert not executor_mod._is_wsl_bash(r"C:\Program Files\Git\bin\bash.exe")


class TestAmplifierPythonNormalization:
    """Tests for making AMPLIFIER_PYTHON safe for the resolved bash."""

    def test_posix_passes_executable_through_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """POSIX must be byte-identical to sys.executable."""
        monkeypatch.setattr(executor_mod.os, "name", "posix")
        assert executor_mod._resolve_amplifier_python() == sys.executable

    def test_windows_converts_backslashes_to_forward_slashes(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Backslashes are an escape char in bash; Git Bash accepts forward slashes."""
        monkeypatch.setattr(executor_mod.os, "name", "nt")
        monkeypatch.setattr(
            executor_mod.sys, "executable", r"C:\Users\dev\.venv\Scripts\python.exe"
        )

        result = executor_mod._resolve_amplifier_python()

        assert result == "C:/Users/dev/.venv/Scripts/python.exe"
        assert "\\" not in result, "no backslashes may survive into a bash command"
