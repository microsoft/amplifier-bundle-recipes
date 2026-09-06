"""A declarative `context:` entry binds its default -- or is refused by name.

Defect (recipes-u2f): `context:` is a variable -> VALUE mapping, and the
executor merged it as `{**recipe.context, **context_vars}`. An entry written in
the declarative schema form every other tool uses --

    context:
      continue_from:
        type: string
        default: ""

-- was therefore bound AS THE SCHEMA DICT. `{{continue_from}}` substituted
`{'type': 'string', 'default': '', ...}` into prompts (as noise) and into
conditions (as a hard failure: `Invalid expression: Unexpected character '{'
at position 0`). Nothing diagnosed it.

These tests pin all three legs of the fix: the default is bound, a
`required: true` with no value is refused BY NAME, and the declaration mapping
is never bound as a value.
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from amplifier_module_tool_recipes.context_schema import ContextDeclarationError
from amplifier_module_tool_recipes.context_schema import is_context_declaration
from amplifier_module_tool_recipes.context_schema import merge_recipe_context
from amplifier_module_tool_recipes.context_schema import resolve_context
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.models import Recipe
from amplifier_module_tool_recipes.models import Step
from amplifier_module_tool_recipes.validator import validate_recipe


@pytest.fixture
def mock_coordinator():
    coordinator = MagicMock()
    coordinator.session = MagicMock()
    coordinator.config = {"agents": {}}
    coordinator.hooks = None
    coordinator.get_capability.return_value = AsyncMock()
    return coordinator


@pytest.fixture
def mock_session_manager():
    manager = MagicMock()
    manager.create_session.return_value = "test-session-id"
    manager.is_cancellation_requested.return_value = False
    manager.is_immediate_cancellation.return_value = False
    return manager


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


class TestRecognition:
    def test_schema_form_is_a_declaration(self):
        assert is_context_declaration({"type": "string", "default": ""})
        assert is_context_declaration({"required": True})
        assert is_context_declaration({"description": "doc only"})

    def test_mapping_with_any_other_key_is_a_plain_value(self):
        # Guards shipped recipes with dict-valued context entries
        # (examples/repo-activity-analysis.yaml `_precomputed`).
        assert not is_context_declaration({"type": "string", "repo_owner": "x"})
        assert not is_context_declaration({"by_impact": {}, "themes": []})

    def test_empty_mapping_is_a_plain_value(self):
        assert not is_context_declaration({})

    def test_non_mapping_is_a_plain_value(self):
        assert not is_context_declaration("plain")
        assert not is_context_declaration(["a"])
        assert not is_context_declaration(None)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolution:
    def test_default_is_bound(self):
        merged = merge_recipe_context({"topic": {"type": "string", "default": "auth"}}, {})
        assert merged == {"topic": "auth"}

    def test_declaration_dict_is_never_the_value(self):
        merged = merge_recipe_context({"topic": {"type": "string", "default": "auth"}}, {})
        assert not isinstance(merged["topic"], dict)

    def test_caller_value_beats_the_default(self):
        merged = merge_recipe_context(
            {"topic": {"type": "string", "default": "auth"}}, {"topic": "sessions"}
        )
        assert merged == {"topic": "sessions"}

    def test_required_without_a_value_is_refused_by_name(self):
        with pytest.raises(ContextDeclarationError) as excinfo:
            merge_recipe_context({"topic": {"type": "string", "required": True}}, {})
        assert "topic" in str(excinfo.value)
        assert excinfo.value.variables == ("topic",)

    def test_required_with_a_caller_value_is_fine(self):
        merged = merge_recipe_context(
            {"topic": {"type": "string", "required": True}}, {"topic": "sessions"}
        )
        assert merged == {"topic": "sessions"}

    def test_optional_and_defaultless_is_left_unbound(self):
        # Deliberately absent rather than None or {}: referencing it then fails
        # through the engine's own "variable not found" path.
        merged = merge_recipe_context({"note": {"type": "string"}}, {})
        assert merged == {}

    def test_plain_values_are_untouched(self):
        block = {"a": "x", "n": 3, "d": {"by_impact": {}, "themes": []}, "e": {}}
        assert merge_recipe_context(block, {}) == block

    def test_required_beside_a_default_warns_and_binds_the_default(self):
        resolved = resolve_context({"topic": {"required": True, "default": "auth"}})
        assert resolved.values == {"topic": "auth"}
        assert resolved.required == ()
        assert resolved.errors == ()
        assert any("topic" in w for w in resolved.warnings)


# ---------------------------------------------------------------------------
# Malformed declarations
# ---------------------------------------------------------------------------


class TestMalformed:
    @pytest.mark.parametrize(
        "declaration",
        [
            {"required": "yes"},
            {"type": "str"},
            {"type": 3},
            {"description": ["not", "a", "string"]},
            {"enum": []},
            {"enum": "a,b"},
            {"enum": ["a", "b"], "default": "c"},
        ],
    )
    def test_reported_by_variable_name(self, declaration):
        resolved = resolve_context({"topic": declaration})
        assert resolved.errors, f"{declaration!r} should be reported"
        assert all("topic" in message for message in resolved.errors)
        # Never bound -- not as the dict, not at all.
        assert "topic" not in resolved.values

    def test_merge_refuses_a_malformed_declaration(self):
        with pytest.raises(ContextDeclarationError) as excinfo:
            merge_recipe_context({"topic": {"type": "str"}}, {})
        assert "topic" in str(excinfo.value)
        assert excinfo.value.variables == ("topic",)

    def test_validator_reports_it_by_name(self):
        recipe = Recipe(
            name="ctx",
            description="d",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="{{topic}}", output="o")],
            context={"topic": {"type": "str", "required": True}},
        )
        result = validate_recipe(recipe)
        assert not result.is_valid
        assert any("topic" in error for error in result.errors)

    def test_validator_accepts_a_well_formed_declaration(self):
        recipe = Recipe(
            name="ctx",
            description="d",
            version="1.0.0",
            steps=[Step(id="s1", agent="a", prompt="{{topic}}", output="o")],
            context={"topic": {"type": "string", "required": True}},
        )
        result = validate_recipe(recipe)
        assert result.is_valid, result.errors


# ---------------------------------------------------------------------------
# End to end, through the executor -- the original failure
# ---------------------------------------------------------------------------


class TestThroughTheExecutor:
    @pytest.mark.asyncio
    async def test_default_reaches_the_step_as_a_value(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """`{{var}}` renders the default, not `{'type': ..., 'default': ...}`."""
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="ctx",
            description="d",
            version="1.0.0",
            steps=[
                Step(
                    id="echo",
                    type="bash",
                    command="echo '{{continue_from}}'",
                    output="rendered",
                )
            ],
            context={
                "continue_from": {
                    "type": "string",
                    "default": "prior.md",
                    "description": "Path to a previously verified document",
                }
            },
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        assert result["rendered"].strip() == "prior.md"
        assert "{'type'" not in result["rendered"]
        assert result["continue_from"] == "prior.md"

    @pytest.mark.asyncio
    async def test_condition_on_a_declared_variable_evaluates(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        """The exact live failure: `{{continue_from}} != ''` used to die on the
        dict repr with `Invalid expression: Unexpected character '{'`."""
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="ctx",
            description="d",
            version="1.0.0",
            steps=[
                Step(
                    id="setup_continuation",
                    type="bash",
                    command="echo continued",
                    condition="{{continue_from}} != ''",
                    output="continued",
                ),
                Step(id="always", type="bash", command="echo done", output="done"),
            ],
            context={"continue_from": {"type": "string", "default": ""}},
        )

        result = await executor.execute_recipe(recipe, {}, temp_dir)

        # Condition is false, so the step is skipped -- and, crucially, it is
        # skipped rather than exploding on an unparseable expression.
        assert "setup_continuation" in result.get("_skipped_steps", [])
        assert result["done"].strip() == "done"

    @pytest.mark.asyncio
    async def test_required_variable_stops_the_run_by_name(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="ctx",
            description="d",
            version="1.0.0",
            steps=[Step(id="echo", type="bash", command="echo '{{topic}}'", output="o")],
            context={"topic": {"type": "string", "required": True}},
        )

        with pytest.raises(ContextDeclarationError) as excinfo:
            await executor.execute_recipe(recipe, {}, temp_dir)
        assert "topic" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_required_variable_supplied_by_the_caller_runs(
        self, mock_coordinator, mock_session_manager, temp_dir
    ):
        executor = RecipeExecutor(mock_coordinator, mock_session_manager)
        recipe = Recipe(
            name="ctx",
            description="d",
            version="1.0.0",
            steps=[Step(id="echo", type="bash", command="echo '{{topic}}'", output="o")],
            context={"topic": {"type": "string", "required": True}},
        )

        result = await executor.execute_recipe(recipe, {"topic": "sessions"}, temp_dir)
        assert result["o"].strip() == "sessions"
