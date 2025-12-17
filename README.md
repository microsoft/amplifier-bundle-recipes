# Amplifier Recipes Bundle

**Multi-step AI agent orchestration for repeatable workflows**

The Recipes Bundle provides a tool and agents for creating, executing, and managing multi-step AI agent workflows. Define once, run anywhere, resume anytime.

## What Are Recipes?

**Recipes** are declarative YAML specifications that define multi-step agent workflows with:

- **Sequential execution** - Steps run in order, each building on the previous
- **Agent delegation** - Each step spawns a sub-agent with specific capabilities
- **State persistence** - Sessions automatically checkpoint for resumability
- **Context accumulation** - Later steps access earlier results via `{{variable}}` syntax
- **Approval gates** - Pause for human review with `requires_approval: true`
- **Foreach loops** - Iterate over collections with parallel execution support

**Use cases:**

- Code review workflows (analyze → identify issues → suggest fixes)
- Dependency upgrades (audit → plan → validate → apply)
- Test generation (analyze code → generate tests → validate coverage)
- Documentation evolution (analyze → simulate learner → improve)
- Research synthesis (extract → compare → synthesize → validate)

## Components

This bundle provides:

1. **tool-recipes** - Tool module for executing recipes
2. **recipe-author** - Agent for conversational recipe creation
3. **result-validator** - Agent for objective pass/fail validation
4. **Complete documentation** - Schema, guide, best practices, troubleshooting
5. **Examples** - 11 working recipes across domains
6. **Templates** - Starter recipes for common patterns

## Installation

### Using the Bundle

Load the bundle directly with Amplifier:

```bash
# Load from local path
amplifier run --bundle ./bundle.md

# Load from git URL
amplifier run --bundle git+https://github.com/microsoft/amplifier-bundle-recipes@main
```

### Including in Another Bundle

Add to your bundle's `includes:` section:

```yaml
includes:
  - bundle: foundation
  - bundle: git+https://github.com/microsoft/amplifier-bundle-recipes@main
```

### Verify Installation

```bash
# Validate a recipe to confirm the tool is available
amplifier run --bundle ./bundle.md "validate recipe examples/simple-analysis-recipe.yaml"
```

## Quick Start

### Execute a Recipe

```bash
amplifier run --bundle ./bundle.md "execute examples/code-review-recipe.yaml with file_path=src/auth.py"
```

### Create a Recipe

Use the recipe-author agent conversationally:

```bash
amplifier run --bundle ./bundle.md "I need to create a recipe for upgrading Python dependencies"
```

The agent guides you through:

1. Understanding your workflow
2. Defining steps and agent capabilities
3. Generating the YAML specification
4. Validating and saving the recipe

### Recipe Example

```yaml
name: "code-review-flow"
description: "Multi-stage code review with analysis, feedback, and validation"
version: "1.0.0"

context:
  file_path: ""  # Required input

steps:
  - id: "analyze"
    agent: "zen-architect"
    mode: "ANALYZE"
    prompt: "Analyze the code at {{file_path}} for complexity and maintainability"
    output: "analysis"

  - id: "suggest-improvements"
    agent: "zen-architect"
    mode: "ARCHITECT"
    prompt: "Based on this analysis: {{analysis}}, suggest concrete improvements"
    output: "improvements"

  - id: "validate-suggestions"
    agent: "zen-architect"
    mode: "REVIEW"
    prompt: "Review these suggestions: {{improvements}} for feasibility"
    output: "validation"
```

## Session Management

### Persistence

Recipe sessions persist to:

```
~/.amplifier/projects/<project>/recipe-sessions/
  recipe_20251118_143022_a3f2/
    state.json        # Current state and step outputs
    recipe.yaml       # The recipe being executed
```

### Resumability

If execution is interrupted, resume from last checkpoint:

```bash
amplifier run "resume recipe session recipe_20251118_143022_a3f2"
```

### Auto-Cleanup

Sessions older than 7 days are automatically cleaned up (configurable via tool config).

## Documentation

- **[Recipe Schema Reference](docs/RECIPE_SCHEMA.md)** - Complete YAML specification
- **[Recipes Guide](docs/RECIPES_GUIDE.md)** - Concepts and patterns
- **[Best Practices](docs/BEST_PRACTICES.md)** - Design guidelines
- **[Troubleshooting](docs/TROUBLESHOOTING.md)** - Common issues and solutions
- **[Examples Catalog](docs/EXAMPLES_CATALOG.md)** - Browse all example recipes

## Examples

The `examples/` directory includes working recipes for:

- **Code Review** - Multi-stage analysis and improvement suggestions
- **Dependency Upgrade** - Audit, plan, validate, and apply (with optional approval gates)
- **Test Generation** - Analyze code and generate comprehensive tests
- **Security Audit** - Multi-perspective security analysis
- **Parallel Analysis** - Concurrent multi-file processing

See [Examples Catalog](docs/EXAMPLES_CATALOG.md) for complete descriptions.

## Templates

The `templates/` directory provides starter recipes:

- **simple-recipe.yaml** - Basic sequential workflow
- **multi-step-recipe.yaml** - Complex multi-stage processing
- **error-handling-recipe.yaml** - Retry and error handling patterns

Copy, customize, and run.

## Advanced Features

### Context Variables

Steps access previous outputs via template variables:

```yaml
steps:
  - id: "analyze"
    prompt: "Analyze {{file_path}}"
    output: "analysis"

  - id: "improve"
    prompt: "Given this analysis: {{analysis}}, suggest improvements"
    output: "improvements"
```

### Approval Gates

Pause for human review:

```yaml
steps:
  - id: "plan-changes"
    agent: "zen-architect"
    prompt: "Plan dependency upgrades"
    output: "upgrade_plan"
    requires_approval: true  # Pauses here for human review
    approval_message: "Review the upgrade plan before applying"

  - id: "apply-changes"
    agent: "modular-builder"
    prompt: "Apply these upgrades: {{upgrade_plan}}"
```

### Parallel foreach

Run iterations concurrently:

```yaml
context:
  perspectives: ["security", "performance", "maintainability"]

steps:
  - id: "multi-analysis"
    foreach: "{{perspectives}}"
    as: "perspective"
    parallel: true
    collect: "analyses"
    agent: "zen-architect"
    prompt: "Analyze from {{perspective}} perspective"
```

## Tool Configuration

Configure via bundle config:

```yaml
tools:
  - module: tool-recipes
    source: ./modules/tool-recipes
    config:
      session_dir: ~/.amplifier/projects/{project}/recipe-sessions
      auto_cleanup_days: 7
      checkpoint_frequency: per_step
```

## Philosophy

Recipes follow Amplifier's core principles:

- **Mechanism, not policy** - Tool executes recipes; recipes define policy
- **Composable** - Steps are independent, reusable across recipes
- **Observable** - Full event logging of execution
- **Resumable** - Checkpointing enables recovery from failures
- **Declarative** - YAML specification separates intent from execution

## Troubleshooting

**Issue: "Recipe session not found"**

- Session may have been auto-cleaned (>7 days old)
- Check session dir: `~/.amplifier/projects/<slug>/recipe-sessions/`
- List active sessions: `amplifier run "list recipe sessions"`

**Issue: "Agent not found: agent-name"**

- Ensure the bundle includes foundation or defines the required agents
- List available agents in your session

**Issue: "Step failed: connection timeout"**

- Recipe resumes from last checkpoint
- Re-run: `amplifier run "resume recipe session <session-id>"`

See [Troubleshooting Guide](docs/TROUBLESHOOTING.md) for complete solutions.

## Contributing

This project welcomes contributions and suggestions. Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit https://cla.opensource.microsoft.com.

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

---

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
