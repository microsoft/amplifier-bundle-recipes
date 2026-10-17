target: contracts/recipe-dependency-manifest.v1.md

## Exact change — ratified Bash compatibility policy

The owner ratified the narrow policy below on 2026-10-14. This record replaces
an unpublished, broader refusal proposal; that earlier wording was not ratified
unchanged. Ratification does not lock the surrounding DRAFT contracts, activate
a new contract or recipe-schema version, approve an implementation, or waive a
verification gate. The exact conversational decision is retained separately;
this public record states its date, scope and exact target delta.

### Change 1 — Core 10, legacy compatibility and initial Bash source confinement

Current text at the comparison revision
`3a8a0915464f41c70d81bc6a881a9d3bcc9a0a71`:

```text
10. **Legacy mode is labeled and confined.** A legacy recipe runs ONLY through
    the embedded Amplifier tool adapter, in explicitly labeled caller-bound
    mode, with a deprecation warning. Its behavior there is byte-identical to
    pre-contract behavior (including failure when the caller lacks a referenced
    agent). The standalone `recipe-runner` CLI rejects legacy recipes with an
    actionable error.
```

Replacement:

```text
10. **Legacy mode is labeled and confined.** A legacy recipe runs ONLY through
    the embedded Amplifier tool adapter, in explicitly labeled caller-bound
    mode, with a deprecation warning. Except for the Bash rule below, its
    behavior there is byte-identical to pre-contract behavior (including
    failure when the caller lacks a referenced agent). The standalone
    `recipe-runner` CLI rejects legacy recipes with an actionable error.

    **Bash rule (legacy and schema-v2).** Context interpolation uses the
    existing converters and must prevent context text from introducing
    executable Bash syntax into the initial shell source. Preserve benign
    SP/TAB argument splitting, simple globbing and tilde behavior without
    recipe migrations: unquoted red blue remains two arguments; context text
    supplied as quoted data, such as $(printf INJECTED), remains literal
    rather than introducing an executed command. Benign compatibility is
    required for existing recipe patterns, not only the examples listed
    here. An unsupported required benign pattern is an implementation
    blocker; changing the recipe is not a remedy. Only command-source and
    associated diagnostic/log-record changes needed for this confinement
    are permitted, without changing existing serialized successful result
    payloads or golden baselines. Unknown cases fail closed before the Bash
    step launches, through existing step error policies, with no raw-
    substitution fallback. Such refusal is not a compatibility exception
    for required benign behavior. This does not promise support for every
    possible Bash value or adopt changes to conversion or newline semantics;
    existing converters and actual pre-contract behavior remain the basis.
    The guarantee is initial shell-source confinement, not a sandbox for
    trusted eval, bash -c, builtins or application re-evaluation.
```

No other numbered invariant changes. The rule applies to both Bash execution
paths; the exception to the legacy byte-identical promise remains legacy-only.
Its placement here does not permit dropping schema-v2 confinement if legacy
mode is later retired. The quoted-data example illustrates the data boundary;
it is not a claim that every placeholder context accepts every value.

## Evidence — observed failures, not implementation acceptance

The comparison head is the public security change in
[PR #120](https://github.com/microsoft/amplifier-bundle-recipes/pull/120).

Prior isolated helper-level probes of both execution paths observed harmless
marker execution from context text in a nested command substitution, after a
comment resembling a heredoc opener, and after a preceding backslash. For
example, `printf '%s\n' "$(echo {{payload}})"` with
`ok; printf RECIPE_PROBE >&2; :` executed the marker rather than preserving it
as data. A heredoc opener-line placeholder also selected the literal
`{{payload}}` destination instead of the supplied `expected.txt`. Detailed
execution evidence is retained separately, not represented as a committed
public artifact or as full-recipe proof.

The head's whole-value `shlex.quote(value)` branches are visible in
`modules/tool-recipes/amplifier_module_tool_recipes/executor.py:133` and
`src/amplifier_recipe_runner/engine.py:134`. Quoting an entire unquoted value
suppresses the splitting, globbing and tilde behavior this policy preserves;
command syntax checks alone cannot establish behavioral compatibility.

Existing immutable compatibility fixtures include unquoted paths and quoted
JSON at
`conformance/legacy-compat/fixtures/legacy-recipes/repo-activity-analysis.yaml:550`
and `:553`, and quoted paths within command substitution at `:591`. A blanket
ban on these existing contexts or a literal-only supported subset is not an
acceptable substitute for supporting benign recipe behavior.

No corrected implementation is qualified by this policy record. The partial
checker cannot certify the required conditional forms, mixed double-quoted
environment expansions or complete output-origin transport. Matching
existing-version observations and manually prepared benign commands do not
establish candidate compatibility, injection prevention or workflow behavior.

## What does NOT change

- The target, `contracts/recipe-runner-lib.v1.md` and `docs/VISION.md` remain
  DRAFT. All other manifest invariants and execution ownership stay unchanged:
  one schema-v2 execution home, thin host adapters, explicit legacy caller
  binding, warnings, missing-agent failure and standalone legacy rejection.
- Recipe files are compatibility evidence, not migration targets. Successful
  serialized `execute` / `resume` / `approve` result payloads, golden baselines,
  fixture coverage and normalization rules remain unchanged. No baseline
  re-recording or masking of differences is authorized.
- Existing converters and actual pre-contract behavior remain the basis for
  representation and newline reconciliation. PR #120 is not authority for
  adopting a changed newline rule by assertion. Non-shell substitution and
  existing step error policies are not redesigned.
- All four repository gates and both required independent implementation
  reviews remain mandatory. Policy ratification does not supply production
  implementation or merge approval, reset consumed attempts, or authorize an
  unrelated implementation.

### Required technical qualification — pending, not passed

1. Qualify unchanged found recipe patterns through both actual engine paths,
   including benign splitting, globbing, tilde, conditionals, expanding
   heredocs and nested substitutions where required. Use realistic typed
   contexts; retain each unresolved case and applicable whole-recipe
   foreach/checkpoint/resume/error/timeout/cancellation/parallel behavior.
   Finite local evidence does not certify every unseen repository; it also
   cannot justify migrating or refusing required benign patterns.
2. Exercise malicious values and the prior boundary failures, including
   discriminating controls that detect vulnerable or deliberately weakened
   behavior. For unknown/refused positions, observe zero Bash step subprocess
   launches through existing error policies, not merely absent marker output.
3. Reconcile converter-rendered representations and newline behavior against
   existing converters, original pre-contract behavior and unchanged fixtures.
   Record actual executed command source honestly without altering successful
   serialized payloads or re-recording their goldens. If these requirements
   conflict in a real case, it remains a blocker rather than an implied waiver.
4. Run all four exact commands in `AGENTS.md:19`, retaining every existing
   golden, and complete clean activation rather than counting a skip. Prove
   independent distribution/source operation, including legacy execution with
   the runner package absent and the runner without the legacy tool package.
   Complete sanctioned CLI provenance via `scripts/live-proof.sh`; forbidden
   shared-cache and shared-CLI-venv modifications remain forbidden.
5. Obtain both required independent reviews of the exact final implementation,
   public support/refusal boundaries, package isolation, compatibility records,
   negative controls, no-launch behavior and activation/CLI evidence. Existing
   tests, author CI, a component specification or this policy decision are not
   substitutes for those observations.

## Decision and publication scope

**2026-10-14 — owner ratified the shown successor policy:** preserve benign
existing recipe behavior without migrations or a required-benign-refusal
waiver, exclude data-origin Bash syntax injection from compatibility, and
permit only the necessary command-source and associated record differences.
The exact before/after delta above is its transcription, not a fresh policy
choice or an approval request. Both execution paths are in scope; no recipe
schema or full-contract version activation is inferred.

Publication must verify the comparison target, this payload and the recorded
decision, retain this proposal, and apply only the accepted delta with its dated
changelog through the applicable document guard. A guard refusal stops that
publication route. Required technical qualification remains incomplete;
ratification does not turn the partial checks into passing acceptance.

## Changelog

- **2026-10-14** — Recorded the owner-ratified Bash compatibility policy;
  replaced the unpublished literal-only/refusal-waiver proposal with benign
  recipe-pattern preservation and initial source confinement. Publication
  precision clarifies that the named mechanisms are examples, permitted record
  changes do not alter successful payloads/goldens, and pre-contract behavior
  is the comparison basis. Retained every technical qualification gate.
