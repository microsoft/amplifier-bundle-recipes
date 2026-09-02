# Contract: recipe-dependency-manifest.v1

**Status:** DRAFT
**Seam:** Recipe authors ↔ recipe runner. A recipe YAML file is a published
format consumed by the runner library, the standalone `recipe-runner` CLI, and
the Amplifier `recipes` tool. If its resolution semantics changed silently,
every recipe author and every host breaks.

Ratified basis: owner Phase-1 decisions, 2026-09-01 ("go").

---

## Core (numbered invariants)

1. **Versioned manifest.** A portable recipe declares `schema_version: 2` and a
   `dependencies` block. A recipe without them is a *legacy recipe* (see Core
   10). Unknown manifest keys are a parse ERROR, never silently ignored.

2. **Dependency form.** `dependencies` entries are Foundation-resolvable
   bundle/behavior source URIs (including partial behavior bundles via
   `#subdirectory=`), each optionally listing `required_agents`. v1 permits
   `kind: bundle` and `kind: behavior` only.

3. **Closed-world agent resolution.** A step's `agent:` reference — alias or
   canonical `namespace:name` — resolves ONLY from the recipe's declared
   dependency closure (plus runner baseline). It never resolves from the
   caller session's agent map.

4. **Isolation by default.** The runner builds the execution session
   exclusively from the declared dependency closure plus runner baseline. v1
   has NO host imports beyond explicit runner ports: workspace path, approved
   provider access, approval callback, event sink, cancellation.

5. **Collision is an error.** Duplicate agent names across the dependency
   closure are a preflight ERROR. A caller agent with a colliding name can
   never satisfy, alter, or override a recipe dependency.

6. **Preflight before side effects.** Trust policy is enforced BEFORE any
   remote fetch or module activation. Every declared dependency and every
   referenced agent is verified before any recipe step executes. A missing
   declaration fails naming the undeclared reference and the remedy.

7. **Provenance recorded per run.** Run state records: recipe digest, each
   declared URI/ref, resolved immutable revision/content digest, included
   partials, agent-to-dependency provenance map, runner/foundation versions,
   and effective trust and capability policy.

8. **Lock semantics.** Lockfile is optional/generated. `locked` mode (default
   for CI) requires exact lock entries; `update-lock` rewrites explicitly;
   `unlocked` is interactive-only with a warning. Locks are never updated
   silently on run. Resume uses recorded provenance; a provenance mismatch
   fails visibly, never silently re-resolves.

9. **Capability intersection.** Effective capabilities are
   host policy ∩ runner policy ∩ manifest-declared needs.

10. **Legacy mode is labeled and confined.** A legacy recipe runs ONLY through
    the embedded Amplifier tool adapter, in explicitly labeled caller-bound
    mode, with a deprecation warning. Its behavior there is byte-identical to
    pre-contract behavior (including failure when the caller lacks a referenced
    agent). The standalone `recipe-runner` CLI rejects legacy recipes with an
    actionable error.

11. **No namespace inference.** The runner never infers dependencies from
    agent-name namespaces (e.g. `foundation:*`).

12. **`agent_config` is resolved, not retained.** The historical
    parsed-but-ignored `agent_config` step field must be either implemented
    under this schema or rejected at parse — never silently retained inert.

## Backlogged (named promotion triggers)

- **Constrained inline agents** (instruction-only; no modules, hooks, or
  undeclared tools; `capabilities: []` mandatory). Trigger: first real recipe
  needing a recipe-private prompt-only role.
- **Narrowly named host-import mechanism.** Trigger: first real embedding that
  cannot express a need via declared dependencies.
- **Shared content-addressed cache dedup.** Trigger: measured duplicate-cache
  cost.
- **Signed archive / content-addressed dependency sources.** Trigger: first
  hermetic-distribution requirement.

## Conformance (fixture intent — discriminating pairs)

GOOD:
- A validator recipe declares the Foundation dependency supplying
  `foundation:zen-architect`; run from a caller bundle lacking that agent, it
  resolves and succeeds.
- The same recipe via library, CLI, and tool adapter yields identical
  resolved-graph identity.
- A behavior-partial dependency composes only its declared contribution.

BAD:
- An isolated recipe referencing an undeclared agent fails preflight before
  side effects, naming the missing declaration.
- A caller agent with a colliding name cannot alter the recipe result.
- A locked resume seeing a different resolved revision fails visibly.
- A dependency requiring a trust-policy-disallowed module refuses before
  activation.
- A legacy recipe under the standalone CLI is rejected with a remedy.
- A representative legacy recipe (agents present in caller) produces an
  identical outcome and identical agent provenance before and after the runner
  lands.

## Reserved

- `schema_version` values above 2.
- `dependencies[].kind` values beyond `bundle` / `behavior`.
- Lockfile `lock_version` above 1.

## Changelog

- 2026-09-01 — Initial DRAFT authored from owner-ratified Phase-1 decisions.
