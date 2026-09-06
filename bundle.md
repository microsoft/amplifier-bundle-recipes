---
bundle:
  name: recipes
  version: 1.0.1
  description: |
    Multi-step AI agent orchestration for repeatable workflows.

    Recipe work follows a required lifecycle -- author with
    `recipes:recipe-author`, verify with `recipes:result-validator`, document
    with the `recipes:generate-recipe-docs` recipe -- and never by writing
    recipe YAML by hand. That lifecycle, the schema-v2 dependency header, the
    tool operations and the worked examples are all instruction, so they live
    in `context/recipe-instructions.md` and reach the model through the
    @mention below rather than as documentation prose in this body.

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: recipes:behaviors/recipes
---

# Recipe System

@recipes:context/recipe-instructions.md

---

@foundation:context/shared/common-system-base.md
