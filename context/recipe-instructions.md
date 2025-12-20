# Recipe System Instructions

You have access to the **recipes** tool for multi-step AI agent orchestration.

## When to Use Recipes

**Use recipes when:**
- Tasks have multiple sequential or parallel steps that benefit from structured orchestration
- Human approval gates are needed at specific checkpoints
- Context must accumulate and flow across agent handoffs
- Workflows need to be resumable after interruption
- You want declarative, repeatable workflow definitions

**Direct agent delegation is better when:**
- Tasks are single-step or simple
- Real-time interactive iteration is needed
- The workflow is ad-hoc and won't be repeated
- You need maximum flexibility in response to results

## Quick Reference

| Operation | Description |
|-----------|-------------|
| `execute` | Run a recipe from a YAML file |
| `resume` | Continue a paused or interrupted recipe |
| `list` | Show recipe sessions and their status |
| `validate` | Check recipe YAML before execution |
| `approvals` | Show pending approval gates |
| `approve` | Approve a pending gate to continue |
| `deny` | Deny a pending gate to stop execution |

## Key Features

- **Context Accumulation**: Each step's output is available to subsequent steps via `{{variable}}` syntax
- **Approval Gates**: Pause execution for human review with `requires_approval: true`
- **Resumability**: Sessions are checkpointed after each step for recovery
- **Foreach Loops**: Iterate over collections with `foreach:` syntax
- **Conditional Execution**: Branch based on results with `condition:` expressions

## Specialized Agents

- **recipes:recipe-author** - Conversational assistance for creating and validating recipes
- **recipes:result-validator** - Objective pass/fail assessment of step outcomes

## Documentation

- Schema: @recipes:docs/RECIPE_SCHEMA.md
- Best practices: @recipes:docs/BEST_PRACTICES.md
- Examples catalog: @recipes:docs/EXAMPLES_CATALOG.md
- Troubleshooting: @recipes:docs/TROUBLESHOOTING.md
