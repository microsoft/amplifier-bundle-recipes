# Attractor

A non-interactive software factory recipe for [Amplifier](https://github.com/microsoft/amplifier).

Give it a spec and scenarios. It generates code, validates it against end-to-end
scenarios, analyzes failures, and iterates until all scenarios pass. Code is
treated as opaque — correctness is inferred exclusively from externally
observable behavior.

## Origin

The [StrongDM Attractor](https://github.com/strongdm/attractor) README instructs:

> Supply the following prompt to a modern coding agent:
> `Implement Attractor as described by https://factory.strongdm.ai/`

This project is our response — adapted for the Amplifier ecosystem:

> `Implement Attractor as described by https://factory.strongdm.ai/, but as an Amplifier recipe.`

The result is a declarative YAML recipe that implements the
[Software Factory](https://factory.strongdm.ai/) pattern using Amplifier's
recipe engine: convergence loops, sub-recipe composition, agent delegation,
and approval gates. We've since been using it to add new features, create
new code, and improve the recipe engine itself.

## How It Works

```
Seed                    Scaffold                 Factory                    Report
parse spec ──────────► scaffold project ───────► ┌─────────────────────┐ ──► LLM-as-judge
parse scenarios         build harness            │ generate code       │     convergence report
                        verify harness           │ run scenarios       │
                                                 │ assess satisfaction │
                                                 │ feedback on failures│
                                                 └────────┬───────────┘
                                                          │ loop until
                                                          │ satisfaction
                                                          │ >= threshold
                                                          ▼
```

Four stages, each with an approval gate:

1. **Seed** — Parse the spec and scenarios. Detect language, components,
   requirements. Structure scenarios into a machine-readable format.
2. **Scaffold** — Generate the project skeleton. Build an end-to-end
   validation harness that runs scenarios independently and reports results
   as JSON.
3. **Factory** — The convergence loop. Each iteration: generate/refine code,
   run the harness, assess satisfaction, and (if not converged) produce
   targeted feedback for the next iteration. Loops until satisfaction
   reaches the threshold or max iterations is hit.
4. **Report** — LLM-as-judge evaluation for semantic scenarios. Convergence
   report with iteration history, per-scenario results, and technique
   analysis.

## Quick Start

```bash
amplifier tool invoke recipes operation=execute \
  recipe_path=./attractor.yaml \
  context='{"spec_path": "./examples/todo-api/spec.md", "scenarios_path": "./examples/todo-api/scenarios.md"}'
```

The included example builds a Todo REST API (Python, stdlib only, 10
scenarios). It converges on the first iteration.

## Files

| File | Purpose |
|------|---------|
| `attractor.yaml` | Main recipe — 4 staged phases with convergence loop |
| `factory-iteration.yaml` | Sub-recipe — one generate/validate/assess/feedback cycle |
| `AUTHORING-GUIDE.md` | How to write specs and scenarios that converge fast |
| `examples/todo-api/spec.md` | Example: Todo API specification |
| `examples/todo-api/scenarios.md` | Example: 10 end-to-end scenarios |

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `spec_path` | (required) | Path to the specification file |
| `scenarios_path` | (required) | Path to the scenarios file or directory |
| `project_dir` | `./project` | Where generated code is written |
| `reference_path` | `""` | Path to a reference implementation (Gene Transfusion) |
| `existing_code_path` | `""` | Existing codebase to evolve instead of greenfield |
| `max_iterations` | `5` | Maximum convergence loop iterations |
| `satisfaction_threshold` | `1.0` | Target fraction of scenarios that must pass (0.0–1.0) |

## Principles

From [factory.strongdm.ai](https://factory.strongdm.ai/):

- **Code is opaque.** Correctness is inferred from externally observable
  behavior, never from inspecting source.
- **Scenarios are a holdout set.** The generating agent never sees them.
  This prevents reward hacking.
- **Satisfaction, not pass/fail.** A fractional measure across all scenarios,
  not a boolean.
- **Shift Work.** When intent is fully specified (spec + scenarios), the
  agent runs end-to-end without back-and-forth.

## Techniques

| Technique | How it's used |
|-----------|---------------|
| **Shift Work** | Fully specified seed drives non-interactive end-to-end execution |
| **Gene Transfusion** | Optional `reference_path` feeds working patterns to the generator |
| **Pyramid Summaries** | Later iterations compress all prior feedback into trend analysis |
| **The Validation Constraint** | Harness tests external behavior only; code internals are never inspected |

## Writing Good Specs and Scenarios

See [AUTHORING-GUIDE.md](AUTHORING-GUIDE.md) for detailed guidance. The short
version:

**Specs:** Be concrete about interfaces, data formats, error cases, and entry
points. Omit implementation details. Declare what NOT to build.

**Scenarios:** Make every assertion machine-checkable. Keep scenarios
independent. Prefer deterministic over semantic. Test behavior, not structure.

## Requirements

- [Amplifier](https://github.com/microsoft/amplifier) with the
  [recipes bundle](https://github.com/microsoft/amplifier-bundle-recipes)
  (includes convergence loop support from v1.7.0)

## License

MIT
