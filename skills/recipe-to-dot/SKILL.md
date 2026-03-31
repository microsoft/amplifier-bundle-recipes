---
name: recipe-to-dot
description: Convention for generating DOT flowchart diagrams from Amplifier recipe YAML files
version: 1.0.0
---

# Recipe-to-DOT Convention

## Purpose

Recipe YAML files define multi-step AI agent workflows, but they're hard to understand at a glance. DOT flowchart diagrams make recipes visually understandable. This skill documents the standard mapping from recipe YAML structure to DOT visual elements.

## Visual Convention Mapping

| Recipe Concept | DOT Shape | Fill Color | Style |
|---|---|---|---|
| Start/End nodes | oval | #e0e0e0 (gray) | filled,rounded |
| Bash step (type: bash) | box | #bbdefb (blue) | filled,rounded |
| Agent step (type: agent / default) | box | #c8e6c9 (green) | filled,rounded |
| Sub-recipe call (type: recipe) | box | #e0e0e0 (gray) | filled,rounded,dashed |
| Approval gate | diamond | #ffe0b2 (orange) | filled |
| Condition decision | diamond | #fff9c4 (yellow) | filled |
| Data/Output | note or cylinder | #e1bee7 (purple) | filled |

## Graph Structure Rules

- Graph flows top-to-bottom (rankdir=TB)
- Every diagram has Start and Done nodes
- Flat mode: steps flow sequentially, conditions get preceding diamonds
- Staged mode: each stage is a cluster subgraph
- Approval gates between stages when approval.required=true
- Foreach/parallel get annotations on edges or node labels
- While loops get dashed loop-back edges
- Legend at bottom showing only step types actually used

## Auto-labeling

Step IDs are converted to readable labels:
- Split on hyphens, title-case each word
- 3+ words split across 2 lines
- Sub-recipe steps append "(sub-recipe)"

## File Conventions

- Recipe DOT files are **co-located next to their source YAML file**
- Naming: same filename, different extension — `.yaml` → `.dot` and `.png`
  - `recipes/validate-recipes.yaml` → `recipes/validate-recipes.dot` + `.png`
  - `examples/code-review-recipe.yaml` → `examples/code-review-recipe.dot` + `.png`
- Rendered with: `dot -Tpng recipe-name.dot -o recipe-name.png`

## Programmatic Generation

The `recipe_to_dot` Python function in `amplifier_module_tool_recipes.recipe_to_dot` generates DOT deterministically from YAML:

```python
from amplifier_module_tool_recipes.recipe_to_dot import recipe_to_dot, recipe_dot_hash

# Generate DOT string
dot = recipe_to_dot("path/to/recipe.yaml")

# Get hash for freshness checking
hash = recipe_dot_hash("path/to/recipe.yaml")
```

## Freshness

DOT files embed a `source_hash` graph attribute that tracks the YAML's structural fingerprint.
The `validate-recipes` recipe (Phase 7) compares this hash against the current YAML to detect
stale diagrams — even when labels have been LLM-enhanced.

## LLM Voice Enhancement (Default)

The standard generation pipeline (`generate-recipe-docs` recipe) includes an LLM pass that
rewrites node labels so a non-technical person can understand them:
- "Read Profile" → "Read Your Settings — What repos do we have?"
- "Determine Tiers" → "Check For Changes — Look at git history"
- "Structural Validation" → "Validate Structure — Are the YAML files well-formed?"

This is the default stance — visual documentation should be accessible to anyone.
The LLM preserves all structural elements (node IDs, edges, shapes, colors) and only
rewrites label text and the graph title.

To generate or regenerate flow diagrams for a repo:
```
amplifier tool invoke recipes operation=execute \
  recipe_path=recipes:recipes/generate-recipe-docs.yaml \
  context='{"repo_path": "/path/to/bundle-repo"}'
```
