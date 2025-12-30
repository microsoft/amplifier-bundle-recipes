# Recipe System

You have access to the **recipes** tool for multi-step AI agent orchestration.

## What Recipes Solve

Recipes are declarative YAML workflows that provide:
- **Orchestration** - Coordinate multiple agents in sequence or parallel
- **Resumability** - Automatic checkpointing; resume if interrupted
- **Approval Gates** - Pause for human review at critical checkpoints
- **Context Flow** - Results from each step available to subsequent steps

## When to Use Recipes

**Use recipes when:**
- Tasks have multiple sequential steps requiring different agents
- The workflow will be repeated (worth encoding as reusable YAML)
- Human approval checkpoints are needed between phases
- Work might be interrupted and needs resumption
- Context must accumulate across agent handoffs

**Use direct agent delegation when:**
- Tasks are single-step or simple
- Real-time interactive iteration is needed
- The workflow is exploratory or ad-hoc

## Tool Operations

| Operation | Use For |
|-----------|---------|
| `execute` | Run a recipe from YAML file |
| `resume` | Continue an interrupted session |
| `validate` | Check recipe YAML before execution |
| `list` | Show active sessions |
| `approvals` | Show pending approval gates |
| `approve/deny` | Respond to approval gates |

## Quick Gotchas

- **Field access requires parsing**: Use `parse_json: true` on bash/agent steps if you need `{{result.field}}` access
- **Bash JSON construction**: Use `jq` to build JSON, never shell variable interpolation (breaks on quotes/newlines)
- **Nested context**: Template variables in nested objects are resolved recursively

## Getting Help

**Delegate to `recipes:recipe-author`** when users need to:
- Create new recipes (conversational workflow design)
- Validate, debug, or improve existing recipes
- Learn about recipe capabilities or patterns
- Add advanced features (loops, conditions, parallel execution, approval gates)
- Decide whether to extract sub-recipes vs keep steps inline (modularization)

The recipe-author agent has complete schema knowledge and will ask clarifying questions to design the right workflow. Don't attempt to write recipe YAML directly—delegate to this expert.

**Use `recipes:result-validator`** for:
- Objective pass/fail assessment of step outcomes or workflow results
- Systematic evaluation against criteria or rubrics
- Formal verdicts needed for automation decisions or approval gates
