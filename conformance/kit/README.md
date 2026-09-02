# Conformance kit — executable discriminating pairs

This directory implements the `## Conformance` sections of both DRAFT contracts
as **runnable fixtures**:

- `contracts/recipe-dependency-manifest.v1.md`
- `contracts/recipe-runner-lib.v1.md`

It is the Freeze Bar prerequisite (protocol pillar 4: *freeze requires a
discriminating example*).

```bash
python3 conformance/kit/kit.py --list         # fixtures, clauses, ledger rows
python3 conformance/kit/kit.py --run          # run all; exit 1 if any fail
python3 conformance/kit/kit.py --run --json   # machine-readable
./conformance/kit/discriminate.sh             # THE PROOF -- see §3
```

No network, no model call, no Foundation install. Everything resolves from
local fixture bundles under `fixtures/` through injected spawn backends.

### Pointing the kit at a specific runner checkout

`_bootstrap` uses an already-importable `amplifier_recipe_runner` when there is
one, so with the library **pip-installed** (including `-e`)
`AMPLIFIER_RECIPE_RUNNER_SRC` is never consulted — the install wins. To run the
kit against a different checkout, put it ahead of site-packages:

```bash
PYTHONPATH=/path/to/amplifier-recipe-runner/src python3 conformance/kit/kit.py --run
```

The cross-host fixture forwards whatever source `_bootstrap` settled on into
its second host process, so both hosts are provably the same implementation
rather than two versions being compared as if they were one.

## 1. The claim this kit makes

A conformance kit that passes proves nothing on its own. The load-bearing
property is that it **fails against a knowingly-broken implementation**.

Two things follow, and both are enforced here:

1. **Every BAD fixture asserts a specific typed error**, never merely a
   non-zero exit. `expect_raises` fails if the call raises the *wrong* type,
   and fails if it *succeeds*. "Something went wrong" would accept a typo as
   conformance.
2. **Several fixtures carry an explicit control** — a nearby case that must
   still pass. A resume fixture where *every* resume fails proves nothing; so
   the faithful resume is asserted to succeed before the drifted one is
   asserted to fail.

## 2. Fixture inventory

Run `kit.py --list` for the authoritative list. As of authoring: **11 fixtures,
5 GOOD, 6 BAD.**

### GOOD

| Fixture | Asserts |
|---|---|
| `good-declared-dependency-runs-from-lean-caller` | A recipe declaring its dependency runs and succeeds *from a caller whose roster lacks that agent* — the caller roster is measured from a real bundle, not asserted in prose. |
| `good-identical-resolved-graph-across-hosts` | The in-process library and a **separate OS process** produce byte-identical resolved-graph identity. |
| `good-behavior-partial-composes-only-declared-contribution` | A `#subdirectory=` behavior partial contributes only `supplier:reviewer`. Control: the same bundle *whole* contributes `summarizer` too, so the narrowing is real. |
| `good-plan-reports-provenance-without-executing-anything` | `plan()` records the full Core 7 field set, leaves the workspace empty, and works with no host services at all. |
| `good-injected-offline-resolver-satisfies-a-locked-run` | An embedder-injected offline resolver satisfies `update-lock` then `locked` verification with no network; `locked` does not rewrite; `unlocked` warns. |

### BAD — each names its typed error

| Fixture | Typed error |
|---|---|
| `bad-undeclared-agent-fails-preflight-before-side-effects` | `UndeclaredAgentError` — plus 0 steps completed, 0 spawns, no session built |
| `bad-colliding-declared-dependencies-fail-preflight` | `AgentCollisionError` — naming **both** sources, no precedence applied |
| `bad-colliding-caller-agent-cannot-alter-the-result` | no error: the host's `agent_configs` is discarded *visibly* and resolution is unchanged |
| `bad-locked-resume-with-changed-revision-fails-visibly` | `ProvenanceMismatchError` — on the resume path *and* the locked path |
| `bad-trust-disallowed-dependency-refused-before-any-fetch` | `TrustRefusedError` — with **zero** resolver calls |
| `bad-legacy-recipe-rejected-by-the-standalone-surface` | `LegacyRecipeError` — in-process and from a standalone host process |

Two fixtures are built so a broken implementation cannot slip past them:

- The **undeclared** recipe references `lean-caller:packager` — a name the
  *caller* bundle really does supply. A caller-map fallback would silently
  satisfy it, which is exactly the regression this fixture exists to catch.
- The **trust** recipe declares a permitted *local* dependency **first**, then
  the blocked remote one. Zero resolver calls is what proves the refusal
  preceded every fetch, rather than following the first one.

## 3. The discrimination proof

`discriminate.sh` reintroduces known violations into the runner, runs the kit,
and requires at least one fixture to fail. Then it reverts.

```
$ ./conformance/kit/discriminate.sh
=== BASELINE (unmutated implementation) ===
11/11 fixtures passed

=== MUTATION: caller-map-fallback ===
  intent: resolve an undeclared reference from the caller session's agent map
[FAIL] BAD  bad-undeclared-agent-fails-preflight-before-side-effects
10/11 fixtures passed
CAUGHT

=== MUTATION: host-agent-precedence ===
  intent: give the HOST's agent_configs precedence over the plan catalog
[FAIL] BAD  bad-colliding-caller-agent-cannot-alter-the-result
10/11 fixtures passed
CAUGHT

RESULT: DISCRIMINATING -- every mutation was caught, and the baseline passes.
```

Mutations live in `mutations/*.patch` as reviewable unified diffs against the
runner source. The script:

- **refuses to run against a dirty runner checkout** — it will not risk
  reverting someone's uncommitted work;
- reverts on **any** exit path (`trap ... EXIT`);
- fails loudly with `HOLE:` if a mutation goes **unnoticed**. A kit that sleeps
  through a violation is a defect in the kit, and is reported as one.

Adding a mutation is the right way to widen the kit: write the regression as a
patch, run `discriminate.sh`, and if it is not caught, you have found the
missing fixture.

## 4. Ledger wiring

`ledger-map.yaml` is **generated** (`kit.py --ledger-map`) and is what the
reconciler consumes. This lane does not edit `conformance/ledger.yaml`.

The map is deliberately pessimistic. Fixture→row wiring is derived from the
registry so it cannot drift, but *how much of a clause the kit actually checks*
is an authored judgement in `LEDGER_COVERAGE`, and **a `partial` row must name
what is NOT covered**. A map that only lists what is covered reads as full
coverage — precisely the overclaim a conformance ledger exists to prevent.
Citing a row with no authored judgement is a hard error, not a warning.

Coverage at authoring time:

| | Rows |
|---|---|
| `full` | RCP-003, RCP-005, RCP-006, RCP-106, RCP-108 |
| `partial` | RCP-001, RCP-002, RCP-004, RCP-007, RCP-008, RCP-010, RCP-101, RCP-102, RCP-104, RCP-105, RCP-107 |
| `none` | RCP-000, RCP-009, RCP-011, RCP-012, RCP-103 |

## 5. Findings — implementation gaps, not kit gaps

The kit asserts against the implementation; it never supplies one. Where a
fixture could not be written at full strength, the cause is recorded here and
filed, **not** worked around:

| # | Finding | Filed |
|---|---|---|
| R1 | The library exposes no `validate` and no `resume`, though `api.RecipeRunner` declares both. RCP-102 is only partially checkable. | `recipes-akb` |
| R2 | ~~no `cli` module exists~~ **RESOLVED** (`recipes-8yo` shipped it; `recipes-d7e` gave `plan`/`run` a `--json` flag and wired the kit to it). The cross-host identity fixture now drives the real CLI through its documented dual entry point, `python -m amplifier_recipe_runner plan --json`. Two notes remain: `-m amplifier_recipe_runner.cli` is **not** an entry point (`cli.py` declares no `__main__` guard — it imports, exits 0, prints nothing), and the "standalone CLI rejects legacy recipes" fixture still asserts on the library surface plus `host_adapter.py` rather than the CLI. | `recipes-akb` |
| R3 | `RunRequest.legacy_mode` is accepted and never read — `execution.plan()` raises `LegacyRecipeError` regardless. The labeled caller-bound adapter mode does not exist, so neither the deprecation warning nor the byte-identical half of manifest Core 10 is checkable. | `recipes-akb` |
| R4 | The Amplifier tool adapter is not a runner host, so the cross-host identity fixture compares two hosts, not the three lib.v1 names. | `recipes-akb` |
| R5 | RECIPE_SCHEMA v2 has no capability-declaration field, so the third term of the Core 9 intersection has no source. RCP-009 is `coverage: none`. | `recipes-54n` |
| R6 | Four prohibition rows (RCP-004, RCP-011, RCP-103, RCP-104) want enumerated absence probes over the runner's exported surface; the kit covers them behaviourally only. | `recipes-cuo` |
| R7 | RCP-012 (`agent_config` retained inert) is a VIOLATION on the shipped **tool module**, a surface this kit does not drive. Recorded as `coverage: none` with that reason. | ledger row already carries `recipes-yh0` |

## 6. Layout

```
kit.py              the checker: fixture registry, assertions, --list/--run/--ledger-map
_bootstrap.py       locates amplifier-recipe-runner; the shared graph-identity serializer
host_adapter.py     a SECOND host -- a standalone process, for the cross-host fixture
discriminate.sh     the discrimination proof
mutations/*.patch   known regressions, as reviewable diffs
ledger-map.yaml     GENERATED -- fixture -> ledger row wiring, for the reconciler
fixtures/bundles/   supplier (declared), impostor (colliding), lean-caller (the caller)
fixtures/recipes/   one recipe per fixture
```

## 7. Locating the runner

`kit.py` imports the real library and **refuses to assert against a stand-in** —
a kit that passes without the implementation present would prove nothing. Search
order: already importable → `$AMPLIFIER_RECIPE_RUNNER_SRC` → conventional sibling
checkouts. Every run prints which implementation it exercised.

```bash
AMPLIFIER_RECIPE_RUNNER_SRC=/path/to/amplifier-recipe-runner/src \
  python3 conformance/kit/kit.py --run
```
