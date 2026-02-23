"""Tests for robust JSON extraction from real-world agent responses."""

import pytest

from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe, Step
from amplifier_module_tool_recipes.session import SessionManager


class MockSession:
    """Mock session object."""

    def __init__(self):
        self.session_id = "test-session"
        self.profile_name = "test-profile"


class MockCoordinator:
    """Mock coordinator that returns predefined responses."""

    def __init__(self):
        self.responses = []
        self.call_count = 0
        self.session = MockSession()
        self.config = {"agents": {}}

    def set_responses(self, responses):
        self.responses = responses
        self.call_count = 0

    def get_capability(self, name: str):
        """Return mock spawn function."""
        if name == "session.spawn":

            async def mock_spawn(agent_name, instruction, **kwargs):
                if self.call_count < len(self.responses):
                    response = self.responses[self.call_count]
                    self.call_count += 1
                    return {"output": response, "session_id": "test"}
                return {"output": "error", "session_id": "test"}

            return mock_spawn
        return None


class TestRealWorldJSONExtraction:
    """Test JSON extraction from realistic agent response patterns."""

    @pytest.fixture
    def session_manager(self, tmp_path):
        """Create session manager."""
        return SessionManager(base_dir=tmp_path, auto_cleanup_days=7)

    @pytest.fixture
    def coordinator(self):
        """Create mock coordinator."""
        return MockCoordinator()

    @pytest.fixture
    def executor(self, coordinator, session_manager):
        """Create executor with mock coordinator."""
        return RecipeExecutor(coordinator, session_manager)

    @pytest.mark.asyncio
    async def test_json_in_explanatory_text_default_preserves(self, executor, tmp_path):
        """Test that default behavior preserves prose with embedded JSON."""
        # Simulate agent response with JSON embedded in text
        response = """Here's what I found about the repository:

{
  "repo_url": "https://github.com/microsoft/amplifier",
  "owner": "microsoft",
  "repo_name": "amplifier"
}

I've successfully extracted the repository information."""

        executor.coordinator.set_responses([response])

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="extract",
                    agent="test-agent",
                    prompt="Extract repo info",
                    output="repo_info",
                    # parse_json=False (default) - should preserve prose
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # Default: should preserve the full prose text
        assert isinstance(context["repo_info"], str)
        assert "Here's what I found" in context["repo_info"]
        assert "microsoft" in context["repo_info"]

    @pytest.mark.asyncio
    async def test_json_in_explanatory_text_with_parse_flag(self, executor, tmp_path):
        """Test extraction when agent wraps JSON in explanation (opt-in with parse_json=true)."""
        # Simulate agent response with JSON embedded in text
        executor.coordinator.set_responses(
            [
                """Here's what I found about the repository:

{
  "repo_url": "https://github.com/microsoft/amplifier",
  "owner": "microsoft",
  "repo_name": "amplifier"
}

I've successfully extracted the repository information."""
            ]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="extract",
                    agent="test-agent",
                    prompt="Extract repo info",
                    output="repo_info",
                    parse_json=True,  # Opt-in to aggressive extraction
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # With parse_json=True: should extract JSON from the text
        assert isinstance(context["repo_info"], dict)
        assert context["repo_info"]["owner"] == "microsoft"
        assert context["repo_info"]["repo_name"] == "amplifier"

    @pytest.mark.asyncio
    async def test_json_in_markdown_code_block_default_preserves(
        self, executor, tmp_path
    ):
        """Test that markdown code blocks are preserved by default."""
        response = """I've analyzed the data. Here's the structured output:

```json
{
  "files": ["test1.py", "test2.py"],
  "count": 2,
  "status": "complete"
}
```

The analysis is complete."""

        executor.coordinator.set_responses([response])

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="analyze",
                    agent="test-agent",
                    prompt="Analyze files",
                    output="result",
                    # parse_json=False (default)
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # Default: should preserve markdown formatting
        assert isinstance(context["result"], str)
        assert "```json" in context["result"]
        assert "analysis is complete" in context["result"]

    @pytest.mark.asyncio
    async def test_json_in_markdown_code_block_with_parse_flag(
        self, executor, tmp_path
    ):
        """Test extraction from markdown ```json code blocks with parse_json=true."""
        executor.coordinator.set_responses(
            [
                """I've analyzed the data. Here's the structured output:

```json
{
  "files": ["test1.py", "test2.py"],
  "count": 2,
  "status": "complete"
}
```

The analysis is complete."""
            ]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="analyze",
                    agent="test-agent",
                    prompt="Analyze files",
                    output="result",
                    parse_json=True,  # Opt-in to extraction
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # With parse_json=True: should extract JSON from markdown block
        assert isinstance(context["result"], dict)
        assert context["result"]["count"] == 2
        assert len(context["result"]["files"]) == 2

    @pytest.mark.asyncio
    async def test_json_in_plain_code_block_with_parse_flag(self, executor, tmp_path):
        """Test extraction from markdown ``` code blocks (no json label) with parse_json=true."""
        executor.coordinator.set_responses(
            [
                """The data structure is:

```
{"name": "test", "value": 42}
```

That's the result."""
            ]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="get-data",
                    agent="test-agent",
                    prompt="Get data",
                    output="data",
                    parse_json=True,  # Opt-in to extraction
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # With parse_json=True: should extract JSON from plain code block
        assert isinstance(context["data"], dict)
        assert context["data"]["name"] == "test"
        assert context["data"]["value"] == 42

    @pytest.mark.asyncio
    async def test_json_array_in_text_with_parse_flag(self, executor, tmp_path):
        """Test extraction of JSON arrays from text with parse_json=true."""
        executor.coordinator.set_responses(
            [
                """I found these files:

["file1.py", "file2.py", "file3.py"]

That's all the Python files."""
            ]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="list-files",
                    agent="test-agent",
                    prompt="List files",
                    output="files",
                    parse_json=True,  # Opt-in to extraction
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # With parse_json=True: should extract JSON array
        assert isinstance(context["files"], list)
        assert len(context["files"]) == 3
        assert "file1.py" in context["files"]

    @pytest.mark.asyncio
    async def test_nested_json_in_text_with_parse_flag(self, executor, tmp_path):
        """Test extraction of nested JSON structures with parse_json=true."""
        executor.coordinator.set_responses(
            [
                """Analysis complete:

{
  "summary": {
    "total": 10,
    "passed": 8,
    "failed": 2
  },
  "details": {
    "failures": ["test1", "test2"]
  }
}

Review the failures."""
            ]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="analyze",
                    agent="test-agent",
                    prompt="Run tests",
                    output="test_results",
                    parse_json=True,  # Opt-in to extraction
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # With parse_json=True: should preserve nested structure
        assert isinstance(context["test_results"], dict)
        assert context["test_results"]["summary"]["total"] == 10
        assert len(context["test_results"]["details"]["failures"]) == 2

    @pytest.mark.asyncio
    async def test_multiline_json_in_text_with_parse_flag(self, executor, tmp_path):
        """Test extraction of multiline JSON (common agent format) with parse_json=true."""
        executor.coordinator.set_responses(
            [
                """Here's the configuration:

{
  "name": "my-app",
  "version": "1.0.0",
  "dependencies": {
    "react": "^18.0.0",
    "typescript": "^5.0.0"
  },
  "scripts": {
    "build": "tsc",
    "test": "jest"
  }
}

That should work for your project."""
            ]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="get-config",
                    agent="test-agent",
                    prompt="Generate config",
                    output="config",
                    parse_json=True,  # Opt-in to extraction
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # With parse_json=True: should extract complete multiline JSON
        assert isinstance(context["config"], dict)
        assert context["config"]["name"] == "my-app"
        assert "react" in context["config"]["dependencies"]
        assert context["config"]["scripts"]["build"] == "tsc"

    @pytest.mark.asyncio
    async def test_clean_json_still_works(self, executor, tmp_path):
        """Test that clean JSON responses still work (backward compatibility)."""
        executor.coordinator.set_responses(['{"status": "success", "count": 5}'])

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="get-status",
                    agent="test-agent",
                    prompt="Get status",
                    output="status",
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # Clean JSON should parse immediately (Strategy 1)
        assert isinstance(context["status"], dict)
        assert context["status"]["status"] == "success"
        assert context["status"]["count"] == 5

    @pytest.mark.asyncio
    async def test_plain_text_without_json(self, executor, tmp_path):
        """Test that plain text without JSON stays as string."""
        executor.coordinator.set_responses(
            ["This is just plain text with no JSON structure at all."]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="get-text", agent="test-agent", prompt="Get text", output="text"
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # Should stay as string
        assert isinstance(context["text"], str)
        assert "plain text" in context["text"]

    @pytest.mark.asyncio
    async def test_foreach_with_extracted_array(self, executor, tmp_path):
        """Test that foreach works with extracted JSON arrays when parse_json=true."""
        executor.coordinator.set_responses(
            [
                # First response: list of items
                'I found these items:\n\n["item1", "item2", "item3"]\n\nProcess each one.',
                # Subsequent responses for foreach iterations
                "Processed item1",
                "Processed item2",
                "Processed item3",
            ]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="get-items",
                    agent="test-agent",
                    prompt="Get items",
                    output="items",
                    parse_json=True,  # Extract array from text
                ),
                Step(
                    id="process-each",
                    agent="test-agent",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    as_var="item",
                    collect="results",
                ),
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # Should extract array and use it for foreach
        assert isinstance(context["items"], list)
        assert len(context["items"]) == 3
        assert len(context["results"]) == 3

    @pytest.mark.asyncio
    async def test_json_with_special_characters(self, executor, tmp_path):
        """Test extraction of JSON containing special characters with parse_json=true."""
        executor.coordinator.set_responses(
            [
                """Here's the data:

{
  "message": "Hello, world! This has \\"quotes\\" and special chars: @#$%",
  "path": "/home/user/file.txt",
  "regex": "\\\\w+\\\\s*"
}

Done."""
            ]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="get-data",
                    agent="test-agent",
                    prompt="Get data",
                    output="data",
                    parse_json=True,  # Opt-in to extraction
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # With parse_json=True: should handle special characters correctly
        assert isinstance(context["data"], dict)
        assert '"quotes"' in context["data"]["message"]
        assert context["data"]["path"] == "/home/user/file.txt"

    @pytest.mark.asyncio
    async def test_multiple_json_objects_takes_first(self, executor, tmp_path):
        """Test that when multiple JSON objects exist, first one is extracted with parse_json=true."""
        executor.coordinator.set_responses(
            [
                """Here's the primary data:

{"primary": true, "value": 1}

And here's some extra data:

{"secondary": true, "value": 2}

Use the primary data."""
            ]
        )

        recipe = Recipe(
            name="test",
            description="test",
            version="1.0.0",
            steps=[
                Step(
                    id="get-data",
                    agent="test-agent",
                    prompt="Get data",
                    output="data",
                    parse_json=True,  # Opt-in to extraction
                )
            ],
        )

        context = await executor.execute_recipe(recipe, {}, tmp_path)

        # With parse_json=True: should extract the first JSON object
        assert isinstance(context["data"], dict)
        assert context["data"]["primary"] is True
        assert context["data"]["value"] == 1


class TestArrayVsObjectExtraction:
    """Tests for the array-before-object extraction priority fix.

    The bug: _extract_json_aggressively() had a {-before-[ bias that caused
    it to prefer extracting JSON objects (dicts) over JSON arrays (lists).
    This broke foreach steps which need arrays.
    """

    @pytest.fixture
    def session_manager(self, tmp_path):
        """Create session manager."""
        return SessionManager(base_dir=tmp_path, auto_cleanup_days=7)

    @pytest.fixture
    def coordinator(self):
        """Create mock coordinator."""
        return MockCoordinator()

    @pytest.fixture
    def executor(self, coordinator, session_manager):
        """Create executor with mock coordinator."""
        return RecipeExecutor(coordinator, session_manager)

    def test_extract_json_array_when_braces_in_prose(self, executor):
        """Array extraction works when prose contains curly braces before the array."""
        text = 'The function signature is process() and config = {} by default.\n\n["task1", "task2", "task3"]'
        result = executor._extract_json_aggressively(text)
        assert isinstance(result, list), f"Expected list, got {type(result).__name__}"
        assert result == ["task1", "task2", "task3"]

    def test_extract_json_array_in_markdown_fences(self, executor):
        """Array in markdown code fences is extracted correctly."""
        text = 'Here are the tasks:\n\n```json\n[{"task_id": "1", "name": "first"}, {"task_id": "2", "name": "second"}]\n```\n\nProcess each one.'
        result = executor._extract_json_aggressively(text)
        assert isinstance(result, list), f"Expected list, got {type(result).__name__}"
        assert len(result) == 2
        assert result[0]["task_id"] == "1"
        assert result[1]["task_id"] == "2"

    def test_extract_json_bare_array_in_text(self, executor):
        """A bare JSON array embedded in prose is extracted."""
        text = 'I found these items:\n\n[{"id": 1}, {"id": 2}, {"id": 3}]\n\nDone.'
        result = executor._extract_json_aggressively(text)
        assert isinstance(result, list), f"Expected list, got {type(result).__name__}"
        assert len(result) == 3

    def test_extract_json_object_still_works(self, executor):
        """JSON object extraction still works when no array is present."""
        text = 'Here is the result:\n\n{"status": "ok", "count": 5}\n\nAll done.'
        result = executor._extract_json_aggressively(text)
        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
        assert result["status"] == "ok"
        assert result["count"] == 5

    def test_extract_clean_array_string(self, executor):
        """A clean JSON array string (Strategy 1) parses correctly."""
        text = '[{"id": 1, "name": "first"}, {"id": 2, "name": "second"}]'
        result = executor._extract_json_aggressively(text)
        assert isinstance(result, list), f"Expected list, got {type(result).__name__}"
        assert len(result) == 2

    def test_extract_clean_object_string(self, executor):
        """A clean JSON object string (Strategy 1) still parses correctly."""
        text = '{"key": "value", "count": 5}'
        result = executor._extract_json_aggressively(text)
        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
        assert result["key"] == "value"
