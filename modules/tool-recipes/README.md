# tool-recipes

**Amplifier tool module for executing multi-step AI agent recipes**

This module provides the execution engine for Amplifier recipes - declarative YAML specifications that orchestrate multi-step AI agent workflows.

## Module Type

**Tool Module** - Provides capabilities to agent sessions

## Installation

This module is typically installed as part of the amplifier-collection-recipes collection:

```bash
amplifier collection add git+https://github.com/microsoft/amplifier-collection-recipes@main
```

For standalone development:

```bash
cd modules/tool-recipes
uv pip install -e .
```

## Tool Operations

The tool-recipes module provides four operations:

### recipe_execute

Execute a recipe from YAML file.

**Parameters:**
- `recipe_path` (string, required): Path to recipe YAML file
- `context` (object, optional): Context variables for recipe

**Returns:**
- `status`: "success" or "error"
- `message`: Result message
- `context`: Final context with all step outputs
- `session_id`: Session identifier for resumption

**Example:**
```json
{
  "recipe_path": "examples/code-review-recipe.yaml",
  "context": {
    "file_path": "src/auth.py"
  }
}
```

### recipe_resume

Resume an interrupted recipe session.

**Parameters:**
- `session_id` (string, required): Session ID to resume

**Returns:**
- Same as recipe_execute

**Example:**
```json
{
  "session_id": "recipe_20251118_143022_a3f2"
}
```

### recipe_list

List active recipe sessions for current project.

**Parameters:** None

**Returns:**
- `status`: "success"
- `sessions`: List of session info dicts
- `count`: Number of sessions

**Example response:**
```json
{
  "status": "success",
  "sessions": [
    {
      "session_id": "recipe_20251118_143022_a3f2",
      "recipe_name": "code-review",
      "started": "2025-11-18T14:30:22Z",
      "current_step_index": 2,
      "completed_steps": ["analyze", "identify-issues"]
    }
  ],
  "count": 1
}
```

### recipe_validate

Validate recipe YAML without executing.

**Parameters:**
- `recipe_path` (string, required): Path to recipe file

**Returns:**
- `status`: "success" or "error"
- `message`: Validation result
- `errors`: List of validation errors (if any)
- `warnings`: List of warnings (if any)

## Configuration

Configure tool-recipes in your profile:

```yaml
tools:
  - module: tool-recipes
    source: git+https://github.com/microsoft/amplifier-collection-recipes@main
    config:
      session_dir: ~/.amplifier/projects  # Base directory for sessions
      auto_cleanup_days: 7                # Auto-delete sessions after N days
```

## Working Directory Resolution

The tool determines the project working directory using this priority:

1. **Coordinator capability** - `session.working_dir` if registered (enables server/web deployments)
2. **Fallback** - `Path.cwd()` for CLI backward compatibility

This affects:
- Recipe session storage location (`~/.amplifier/projects/<project-slug>/recipe-sessions/`)
- Bash step `cwd` resolution (relative paths resolve from this directory)
- Sub-recipe path resolution when using relative paths

## Session Persistence

Sessions persist to:
```
~/.amplifier/projects/<project-slug>/recipe-sessions/
  recipe_20251118_143022_a3f2/
    recipe.yaml         # Copy of recipe being executed
    state.json          # Current execution state
    events.jsonl        # Event log (via hooks-logging)
```

## Public API

```python
from amplifier_module_tool_recipes import (
    mount,              # Module mount function
    Recipe,             # Recipe data class
    Step,               # Step data class
    RecipeExecutor,     # Execution engine
    SessionManager,     # Session persistence
    validate_recipe,    # Validation function
    ValidationResult,   # Validation result
)
```

## Development

```bash
# Install dependencies
cd tools/tool-recipes
uv pip install -e .

# Run tests (when available)
uv run pytest

# Type check
uv run pyright amplifier_module_tool_recipes/
```

## Architecture

**Components:**

- **models.py** - Recipe and Step data classes, YAML parsing
- **validator.py** - Comprehensive recipe validation
- **session.py** - Session persistence and cleanup
- **executor.py** - Recipe execution with checkpointing
- **__init__.py** - Tool mount point and operation routing

**Execution flow:**
1. Load and validate recipe
2. Create or resume session
3. Execute steps sequentially with checkpointing
4. Each step spawns sub-agent via coordinator
5. Context accumulates across steps
6. Session persists after each step
7. Auto-cleanup old sessions

## See Also

- [Collection README](../../README.md) - Collection overview
- [Recipe Schema](../../docs/RECIPE_SCHEMA.md) - YAML specification
- [Recipes Guide](../../docs/RECIPES_GUIDE.md) - Conceptual guide
- [Examples](../../examples/) - Working recipe files
