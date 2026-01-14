---
bundle:
  name: recipes
  version: 1.0.0
  description: Multi-step AI agent orchestration for repeatable workflows

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: recipes:behaviors/recipes
---

# Recipe System

@recipes:context/recipe-instructions.md

---

## Creating and Editing Recipes

### Required Workflow (Non-Negotiable)

| Phase | Agent | Purpose |
|-------|-------|---------|
| 1. Author | `recipes:recipe-author` | Create, edit, validate, debug |
| 2. Validate | `recipes:result-validator` | Verify recipe meets original intent |

**MUST delegate to `recipes:recipe-author`** for ALL recipe work. Do NOT write recipe YAML directly.

**MUST run `recipes:result-validator`** after creation/editing, providing the recipe AND conversation context.

### Why This Matters

- `recipe-author` has complete schema knowledge and asks clarifying questions
- `result-validator` provides unbiased verification that the recipe solves what the user asked for
- Skipping these steps results in recipes that are syntactically valid but semantically wrong

## Examples

Example recipes are available in `@recipes:examples/`:

- `simple-analysis-recipe.yaml` - Basic sequential workflow
- `code-review-recipe.yaml` - Multi-stage review with conditional execution
- `dependency-upgrade-staged-recipe.yaml` - Workflow with human approval gates

For a complete catalog, see @recipes:docs/EXAMPLES_CATALOG.md

---

@foundation:context/shared/common-system-base.md
