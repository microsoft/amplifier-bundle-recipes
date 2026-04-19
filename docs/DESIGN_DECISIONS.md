# Design Decisions

This document captures engineering decisions made for the recipe engine — specifically features that were **considered and declined** after analysis. Future recipe authors and contributors can read this to understand "why doesn't the engine support X?" without re-litigating.

## WONTFIX: Template Variable Defaults (`{{var|default('x')}}`)

**Decision:** We will NOT add Jinja2-style `|default()` filter syntax to recipe variable substitution.

**Rationale:**

Recipe variables do not support Jinja2-style `|default()` filter syntax and this is intentional. The recipe engine's variable substitution is deliberately minimal — `{{variable}}` and `{{variable.path}}` only. Adding filter syntax would require integrating a template engine (or building one), introducing a new category of parse errors, and creating two parallel ways to set defaults (context declarations vs inline filters).

The correct pattern is to declare defaults in the recipe's `context:` block or to use an `init` bash step that sets variables conditionally. The Superpowers SDD recipe demonstrates this effectively: `context: { impl_status: "SKIP", review_tier: "trivial" }` declares defaults that downstream steps override. This is explicit, visible in the recipe header, and debuggable. Inline template defaults hide control flow inside prompt strings where they are invisible to validation and harder to reason about.

**Alternative:** Use `context:` defaults in recipe header, or `init` bash steps for conditional initialization.

## WONTFIX: Declarative Output Regex Extraction (`output_extract:`)

**Decision:** We will NOT add an `output_extract` field to Step for declarative regex-based output parsing.

**Rationale:**

Declarative regex output extraction (e.g., `output_extract: "STATUS: (\\w+)"`) is not planned for the recipe engine. Bash steps with `grep`, `sed`, or `python3 -c` already provide this capability with full language power, clear error handling, and zero new engine complexity.

The Superpowers SDD `extract-status` step demonstrates the pattern: a bash step that parses agent output with a Python one-liner and writes a clean value. A declarative `output_extract` field would cover only simple regex cases, would need its own error reporting for match failures, and would inevitably grow toward supporting capture groups, multiple extractions, and fallback values — reimplementing `sed` badly. Bash is the right tool for text extraction because it already exists, handles edge cases the recipe author can see and debug, and does not add surface area to the engine's step model.

**Alternative:** Use a bash step with `grep`, `sed`, `awk`, or `python3 -c`.

---

## Meta

This file is maintained alongside the engine. When a feature is considered and declined, add it here with the rationale. When a feature previously declined becomes actually needed, remove it from this file and implement it.
