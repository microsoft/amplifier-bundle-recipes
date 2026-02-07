# Recipe System

You have access to the **recipes** tool for multi-step AI agent orchestration.

## When to Use Recipes

**Use recipes when:**
- Tasks have multiple sequential steps requiring different agents
- The workflow will be repeated (worth encoding as reusable YAML)
- Human approval checkpoints are needed between phases
- Work might be interrupted and needs resumption

**Use direct agent delegation when:**
- Tasks are single-step or simple
- Real-time interactive iteration is needed
- The workflow is exploratory or ad-hoc

## Recipe Authoring Lifecycle (REQUIRED)

All recipe work MUST follow this lifecycle. Do NOT write recipe YAML directly.

| Phase | Agent | Purpose |
|-------|-------|---------|
| 1. Author | `recipes:recipe-author` | Create, edit, validate, debug recipes |
| 2. Validate | `recipes:result-validator` | Verify recipe meets user's original intent |

**Anti-pattern**: Writing recipe YAML directly. Always delegate to `recipes:recipe-author`.

## Tool Operations

| Operation | Purpose |
|-----------|---------|
| `execute` | Run a recipe from YAML file |
| `resume` | Continue an interrupted session |
| `validate` | Check recipe YAML before execution |
| `list` | Show active sessions |
| `approvals` | Show pending approval gates |
| `approve/deny` | Respond to approval gates |

For detailed usage, schema knowledge, and best practices, delegate to `recipes:recipe-author`.

## While Loops and Enhanced Expressions

Recipes support convergence-based iteration:

- **`while_condition`** — loop until a condition becomes false
- **`break_when`** — exit loop early when a condition becomes true
- **`update_context`** — mutate context variables after each iteration
- **Expression operators** — `<`, `>`, `>=`, `<=`, `not`, parentheses, numeric comparison

For details, delegate to `recipes:recipe-author`.
