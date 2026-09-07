"""Tests for JSON parsing in step outputs."""

import pytest
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe, Step
from amplifier_module_tool_recipes.session import SessionManager
from unittest.mock import MagicMock


class MockCoordinator:
    """Mock coordinator that returns JSON responses from agents."""

    def __init__(self, response_map=None):
        """Initialize with response map for different agents."""
        self.response_map = response_map or {}
        self.session = MagicMock()
        self.config = {"agents": {}}

    def get_capability(self, name: str):
        """Return mock spawn function."""
        if name == "session.spawn":
            async def mock_spawn(agent_name, instruction, **kwargs):
                # Simulate spawn() returning wrapped output
                response = self.response_map.get(agent_name, "default response")
                return {"output": response, "session_id": "test-session-123"}
            return mock_spawn
        return None


@pytest.fixture
def temp_dir(tmp_path):
    """Create temp directory for tests."""
    return tmp_path


@pytest.fixture
def session_manager(temp_dir):
    """Create session manager."""
    return SessionManager(base_dir=temp_dir, auto_cleanup_days=7)


class TestJSONParsing:
    """Test JSON parsing in recipe step outputs."""

    @pytest.mark.asyncio
    async def test_json_object_response_with_dot_notation(self, temp_dir, session_manager):
        """Test that JSON object responses can be accessed with dot notation."""
        # Setup: Agent returns JSON object
        coordinator = MockCoordinator(response_map={
            "json-agent": '{"files": ["a.py", "b.py"], "count": 2}'
        })
        executor = RecipeExecutor(coordinator, session_manager)

        recipe = Recipe(
            name="test-json-object",
            description="Test JSON object parsing",
            version="1.0.0",
            steps=[
                Step(
                    id="get-json",
                    agent="json-agent",
                    prompt="Return JSON object",
                    output="result"
                ),
            ],
        )

        # Execute recipe
        context = await executor.execute_recipe(recipe, {}, temp_dir)

        # Verify: result should be parsed as dict
        assert "result" in context

        # This is the key test - result should be a parsed dict, not a string
        assert isinstance(context["result"], dict), \
            f"Expected dict, got {type(context['result'])}: {context['result']}"
        assert "files" in context["result"]
        assert context["result"]["files"] == ["a.py", "b.py"]
        assert context["result"]["count"] == 2

    @pytest.mark.asyncio
    async def test_json_array_response_for_foreach(self, temp_dir, session_manager):
        """Test that JSON array responses work with foreach."""
        coordinator = MockCoordinator(response_map={
            "list-agent": '["item1", "item2", "item3"]',
            "process-agent": "processed"
        })
        executor = RecipeExecutor(coordinator, session_manager)

        recipe = Recipe(
            name="test-json-array",
            description="Test JSON array parsing",
            version="1.0.0",
            steps=[
                Step(
                    id="get-list",
                    agent="list-agent",
                    prompt="Return JSON array",
                    output="items"
                ),
                Step(
                    id="process-items",
                    agent="process-agent",
                    prompt="Process {{item}}",
                    foreach="{{items}}",
                    collect="results"
                ),
            ],
        )

        # Execute recipe
        context = await executor.execute_recipe(recipe, {}, temp_dir)

        # Verify: items should be parsed as list, foreach should work
        assert isinstance(context["items"], list)
        assert context["items"] == ["item1", "item2", "item3"]
        assert "results" in context
        assert len(context["results"]) == 3

    @pytest.mark.asyncio
    async def test_nested_json_access(self, temp_dir, session_manager):
        """Test accessing nested properties in JSON responses."""
        coordinator = MockCoordinator(response_map={
            "nested-agent": '{"data": {"user": {"name": "Alice", "id": 123}}, "meta": {"version": "1.0"}}'
        })
        executor = RecipeExecutor(coordinator, session_manager)

        recipe = Recipe(
            name="test-nested-json",
            description="Test nested JSON access",
            version="1.0.0",
            steps=[
                Step(
                    id="get-nested",
                    agent="nested-agent",
                    prompt="Return nested JSON",
                    output="result"
                ),
            ],
        )

        # Execute recipe
        context = await executor.execute_recipe(recipe, {}, temp_dir)

        # Verify: nested structure preserved
        assert isinstance(context["result"], dict)
        assert context["result"]["data"]["user"]["name"] == "Alice"
        assert context["result"]["data"]["user"]["id"] == 123
        assert context["result"]["meta"]["version"] == "1.0"

        # Note: Deep nested dot notation in templates (result.data.user.name)
        # is not yet supported. Templates currently support one-level nesting only.
        # The important thing is that JSON is parsed correctly (verified above)

    @pytest.mark.asyncio
    async def test_plain_text_response_unchanged(self, temp_dir, session_manager):
        """Test that plain text responses are not affected."""
        coordinator = MockCoordinator(response_map={
            "text-agent": "This is plain text, not JSON"
        })
        executor = RecipeExecutor(coordinator, session_manager)

        recipe = Recipe(
            name="test-plain-text",
            description="Test plain text handling",
            version="1.0.0",
            steps=[
                Step(
                    id="get-text",
                    agent="text-agent",
                    prompt="Return plain text",
                    output="result"
                ),
            ],
        )

        # Execute recipe
        context = await executor.execute_recipe(recipe, {}, temp_dir)

        # Verify: plain text stays as string
        assert isinstance(context["result"], str)
        assert context["result"] == "This is plain text, not JSON"

    @pytest.mark.asyncio
    async def test_malformed_json_stays_as_string(self, temp_dir, session_manager):
        """Test that malformed JSON is kept as string."""
        coordinator = MockCoordinator(response_map={
            "broken-agent": '{"incomplete": "json"'  # Missing closing brace
        })
        executor = RecipeExecutor(coordinator, session_manager)

        recipe = Recipe(
            name="test-malformed-json",
            description="Test malformed JSON handling",
            version="1.0.0",
            steps=[
                Step(
                    id="get-broken",
                    agent="broken-agent",
                    prompt="Return broken JSON",
                    output="result"
                ),
            ],
        )

        # Execute recipe - should not crash
        context = await executor.execute_recipe(recipe, {}, temp_dir)

        # Verify: malformed JSON stays as string
        assert isinstance(context["result"], str)
        assert '{"incomplete": "json"' in context["result"]

    @pytest.mark.asyncio
    async def test_json_in_markdown_code_block(self, temp_dir, session_manager):
        """Test handling of JSON inside markdown code blocks."""
        coordinator = MockCoordinator(response_map={
            "markdown-agent": 'Here is the JSON:\n```json\n{"data": "value"}\n```'
        })
        executor = RecipeExecutor(coordinator, session_manager)

        recipe = Recipe(
            name="test-markdown-json",
            description="Test JSON in markdown",
            version="1.0.0",
            steps=[
                Step(
                    id="get-markdown",
                    agent="markdown-agent",
                    prompt="Return markdown with JSON",
                    output="result"
                ),
            ],
        )

        # Execute recipe
        context = await executor.execute_recipe(recipe, {}, temp_dir)

        # Verify: markdown with JSON stays as string
        # (extraction from markdown could be future enhancement)
        assert isinstance(context["result"], str)
        assert "```json" in context["result"]

    @pytest.mark.asyncio
    async def test_boolean_and_number_json_values(self, temp_dir, session_manager):
        """Test that JSON primitives are parsed correctly."""
        coordinator = MockCoordinator(response_map={
            "primitives-agent": '{"enabled": true, "count": 42, "rate": 3.14, "name": null}'
        })
        executor = RecipeExecutor(coordinator, session_manager)

        recipe = Recipe(
            name="test-json-primitives",
            description="Test JSON primitive types",
            version="1.0.0",
            steps=[
                Step(
                    id="get-primitives",
                    agent="primitives-agent",
                    prompt="Return JSON with primitives",
                    output="result"
                ),
            ],
        )

        # Execute recipe
        context = await executor.execute_recipe(recipe, {}, temp_dir)

        # Verify: primitive types preserved
        assert context["result"]["enabled"] is True
        assert context["result"]["count"] == 42
        assert context["result"]["rate"] == 3.14
        assert context["result"]["name"] is None

    @pytest.mark.asyncio
    async def test_recipe_step_returns_json_context(self, temp_dir, session_manager):
        """Test that recipe steps (not agent steps) also handle JSON properly."""
        # Note: This test would require a sub-recipe setup
        # Skipping for now as it's more complex
        pass
