# Contract: recipe-runner-lib.v1

**Status:** DRAFT
**Seam:** Embedders ↔ runner library. Consumers: the standalone
`recipe-runner` CLI, the Amplifier `recipes` tool module, and future hosts
(services, IDEs, CI harnesses). If the library's public surface changed
silently, every adapter breaks.

Ratified basis: owner Phase-1 decisions, 2026-09-01 ("go").

---

## Core (numbered invariants)

1. **One execution home.** The library (working name `amplifier-recipe-runner`)
   owns manifest parsing/validation, dependency planning and collision
   detection, resolution, provenance recording, run state, and execution
   orchestration. Every host surface is a thin adapter; no adapter carries
   workflow, resolution, or agent-catalog logic of its own.

2. **Public API surface.** The library exposes, at minimum: `validate`
   (manifest + plan checks, no side effects), `plan` (resolved dependency plan
   and agent provenance, no execution), `run`, and `resume`. All are usable
   without a UI and without the Amplifier CLI.

3. **Neutral session abstraction.** The library defines its own execution-
   session abstraction. `coordinator` (and any Amplifier-internal session
   object) is NOT public API.

4. **Host ports.** Hosts integrate exclusively through narrow, explicitly named
   ports: provider access, approval callback, event sink, workspace path,
   cancellation. No port grants the host's ambient agent map to the recipe.

5. **Injectable resolver/cache policy.** The library owns a resolver interface
   with injectable policy. The default implementation uses Foundation's
   `BundleRegistry` under a runner namespace within the standard Amplifier
   cache root. Cache location is NOT part of the public semantic contract;
   embedders may inject registry/cache policy (mirrors, offline, isolation).

6. **Trust policy is a required input for remote resolution.** Arbitrary
   explicit URIs are permitted only WITH a caller-provided trust policy;
   CI-mode execution requires locked immutable refs. The library refuses
   remote fetch/activation that the policy disallows, before side effects.

7. **Run manifest schema.** `plan`/`run` results expose the resolved graph
   (recipe digest, canonical URIs, immutable revisions/content digests,
   agent-to-dependency provenance, versions, effective policy) in a stable,
   documented shape.

8. **Error model.** Preflight failures (undeclared agent, collision, trust
   refusal, provenance mismatch) are distinct, typed, and occur before recipe
   steps run. A missing artifact or refused dependency is a real result, never
   a fabricated success.

## Backlogged (named promotion triggers)

- **Approval/resume port for non-interactive hosts** beyond the minimal
  callback. Trigger: first service embedding needing queued approvals.
- **Streaming/event schema stabilization.** Trigger: first external consumer
  parsing events programmatically.

## Conformance (fixture intent)

GOOD:
- The same recipe run through the library API, the CLI adapter, and the
  Amplifier tool adapter yields identical resolved-graph identity and agent
  provenance.
- `plan` on a recipe with a fully declared closure reports every agent's
  supplying dependency without executing anything.
- An embedder-injected resolver (e.g. offline cache) satisfies a locked run
  with no network access.

BAD:
- An adapter passing its caller session's agent map into execution is a
  violation.
- `run` proceeding past a trust-policy refusal or an undeclared agent is a
  violation.
- Two hosts producing different provenance for the same locked recipe is a
  violation.

## Reserved

- Additional host port names.
- Run-manifest schema versions above 1.

## Changelog

- 2026-09-01 — Initial DRAFT authored from owner-ratified Phase-1 decisions.
