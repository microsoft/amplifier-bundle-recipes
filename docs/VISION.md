# VISION — Recipe Execution

**Status:** DRAFT

## The end state

A recipe is a declarative, lockable dependency root. Any conforming host — the
runner library API, the standalone `recipe-runner` CLI, or the Amplifier
`recipes` tool — resolves the same recipe to the same dependency provenance and
the same agent catalog, independent of whichever bundle the caller happens to
be running. The caller supplies host services (credentials, approvals, UI,
workspace); it never supplies accidental dependencies.

## Operating principles

1. A recipe's agent catalog comes from its declared dependency closure, never
   from ambient caller state.
2. Dependency resolution is supply-chain input: trust-gated before activation,
   provenance-recorded after.
3. One library is the execution home; every surface (CLI, tool module) is a
   thin adapter over it.
4. Undeclared is unresolved: a missing declaration fails loudly in preflight,
   before side effects.
5. Legacy compatibility is explicit and labeled, never silent.

## What this repo deliberately resists

- Inferring dependencies from agent-name namespaces (e.g. `foundation:*`).
- Embedding arbitrary executable bundle/module source inline in recipe YAML.
- Merging recipe dependencies into the caller's live session.
- Silent lock updates on run.
- Making cache location or coordinator internals part of the public contract.
- Caller-bundle composition silently influencing recipe results.

## Governing contracts

This vision is a thin pointer to the seam contracts; the contracts are
authoritative:

- `contracts/recipe-dependency-manifest.v1.md` — recipe authors ↔ runner.
- `contracts/recipe-runner-lib.v1.md` — embedders ↔ runner library.

## Changelog

- 2026-09-01 — Initial DRAFT authored from owner-ratified Phase-1 decisions
  (recipe execution decoupling).
