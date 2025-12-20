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

## Recipe Structure

Recipes define multi-step workflows:

```yaml
name: "workflow-name"
description: "What this workflow accomplishes"
version: "1.0.0"

context:
  input_var: "" # Required inputs from user

steps:
  - id: "step-1"
    agent: "agent-name"
    mode: "ANALYZE"
    prompt: "Task description using {{input_var}}"
    output: "step_result"
    timeout: 300

  - id: "step-2"
    agent: "another-agent"
    prompt: "Continue with {{step_result}}"
    output: "final_result"
```

## Recipe Authoring

For the complete recipe schema and all available options, see @recipes:docs/RECIPE_SCHEMA.md

For design patterns and best practices, see @recipes:docs/BEST_PRACTICES.md

For troubleshooting common issues, see @recipes:docs/TROUBLESHOOTING.md

## Examples

Example recipes are available in the `examples/` directory:

- `simple-analysis-recipe.yaml` - Basic sequential workflow
- `code-review-recipe.yaml` - Multi-stage code review with three analysis steps
- `dependency-upgrade-staged-recipe.yaml` - Workflow with human approval gates
- `parallel-analysis-recipe.yaml` - Concurrent step execution
- `conditional-workflow.yaml` - Branching based on results

For a catalog of all examples with descriptions, see @recipes:docs/EXAMPLES_CATALOG.md

---

@foundation:context/shared/common-profile-base.md
