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
3. **Prohibitions are checked by enumeration, not by a happy path.** Four
   contract clauses forbid something rather than requiring something, and no
   passing run can establish an absence. The **absence probes** (§2.3) instead
   enumerate the surface and compare it to an authored expectation, so an
   addition fails loud *by name*. Each carries a non-vacuity control: the same
   scanner is run over a deliberately tainted stand-in and must flag it — a
   scanner that matched nothing would report a clean surface for the same
   reason a correct one would.

## 2. Fixture inventory

Run `kit.py --list` for the authoritative list. As of authoring: **15 fixtures,
9 GOOD, 6 BAD** — 5 behavioural GOOD, 6 BAD, and 4 absence probes (§2.3).

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

### Absence probes — enumerated surface

GOOD in polarity, but a different genre: each enumerates a surface and asserts
that a named construct is **absent** from it.

| Probe | Enumerates | Rows |
|---|---|---|
| `probe-host-surface-is-exactly-the-five-ports` | `HostServices` ≡ `HOST_PORTS`; `RunRequest`'s host-facing fields; all 7 host entry points pinned parameter-for-parameter; `run`'s three injectables proved library-owned and free of foreign types; and a **measured** fact — a fresh interpreter importing the library pulls in zero Amplifier modules. | RCP-004, RCP-104 |
| `probe-no-dependency-inferred-from-an-agent-namespace` | The sources actually handed to the resolver vs the sources the recipe declares, across one refused plan and two clean ones. | RCP-011 |
| `probe-no-coordinator-in-the-public-api` | Every name in `__all__` (all asserted library-owned) plus ~127 authored members, field annotations, parameters and return types — scanned for `coordinator`/Amplifier-session vocabulary **and** for any type resolving outside the library and the standard library. | RCP-103 |
| `probe-ports-are-the-five-contract-names-and-carry-no-agent-map` | `HOST_PORTS` against the contract's own five names in contract order; `HostServices` one field per port; `ports.__all__`; every port protocol and payload scanned for agent-map vocabulary and foreign types. | RCP-104, RCP-004 |

Three of these are built so they cannot pass vacuously:

- The **namespace** probe's undeclared reference is `lean-caller:packager`, and
  `bundles/lean-caller` really is a resolvable bundle that really supplies it —
  asserted as a control. A namespace-inferring runner would therefore
  *succeed*. Without that control, "no inference happened" could just mean
  "inference would have failed anyway".
- The **coordinator** and **port** probes run their scanner over
  `_TaintedStandIn`, a class that does carry a `coordinator`, an
  `agent_configs`, and an `agent_catalog(parent_session)`. If the scanner
  reports it clean, the probe fails — the instrument is broken, so its real
  result would be meaningless.
- The **host surface** probe measures Amplifier imports in a second process
  against a before/after `sys.modules` snapshot, so the answer is what the
  import *adds* — not what the interpreter started with, and not a reading of
  import statements.

One deliberate non-choice: the port scan bans agent-map *names*
(`agent`, `catalog`, `roster`, …) but the injectable scan does **not**.
`SessionFactory.create` legitimately takes the plan's own `PlanCatalog`, and
banning the word would ban the conforming design along with the violation.
There, provenance is the discriminator — where the type comes from.

## 3. The discrimination proof

`discriminate.sh` reintroduces known violations into the runner, runs the kit,
and requires at least one fixture to fail. Then it reverts.

```
$ PYTHONPATH="$PWD/src" ./conformance/kit/discriminate.sh
=== BASELINE (unmutated implementation) ===
15/15 fixtures passed
...
RESULT: DISCRIMINATING -- every mutation was caught, and the baseline passes.
```

| Mutation | Reintroduces | Caught by |
|---|---|---|
| `caller-map-fallback` | an undeclared reference resolved from the caller session (manifest Core 3) | `bad-undeclared-agent-...` |
| `host-agent-precedence` | the host's `agent_configs` taking precedence over the plan catalog (manifest Core 5) | `bad-colliding-caller-agent-...` |
| `sixth-host-port` | a **sixth** port handing the host's agent map to the recipe (manifest Core 4) | both port probes |
| `port-carries-agent-map` | still exactly five ports, but an **existing** one widened to grant the agent map (lib Core 4) | `probe-ports-...` **only** |
| `namespace-inferred-dependency` | a dependency guessed from an agent name's namespace (manifest Core 11) | `probe-no-dependency-inferred-...` |
| `coordinator-on-public-session` | the coordinator re-exposed through the public session (lib Core 3) | `probe-no-coordinator-...` |

The last four are the reason the probes exist: the behavioural fixtures sleep
through three of them entirely. `port-carries-agent-map` is the sharpest case —
it keeps the port *count* correct, so only a probe that reads port
*signatures* can see it.

Mutations live in `mutations/*.patch` as reviewable unified diffs against the
runner source. The script:

- **refuses to run against a dirty runner checkout** — it will not risk
  reverting someone's uncommitted work;
- **refuses to mutate a checkout outside this repo.** `_bootstrap` prefers an
  already-importable copy, so an editable install pointing at a sibling
  checkout silently wins over this repo's `src/` — and this script *mutates*
  what it finds. It now names both paths and stops, printing the `PYTHONPATH`
  pin to re-run correctly. `ALLOW_EXTERNAL_RUNNER=1` overrides, loudly;
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
| `full` | RCP-003, RCP-004, RCP-005, RCP-006, RCP-011, RCP-103, RCP-104, RCP-106, RCP-108 |
| `partial` | RCP-001, RCP-002, RCP-007, RCP-008, RCP-010, RCP-101, RCP-102, RCP-105, RCP-107 |
| `none` | RCP-000, RCP-009, RCP-012 |

RCP-011 is `full` for the *clause* while still naming what the probe does not
settle: the row's OPEN-PINNED interpretive ruling is the reconciler's call, and
the probe takes no position on it. `ledger.yaml` itself is untouched by this
lane.

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
| R6 | ~~Four prohibition rows want enumerated absence probes~~ **RESOLVED** (`recipes-cuo`): RCP-004, RCP-011, RCP-103 and RCP-104 now have absence probes (§2.3), each discrimination-proved by its own mutation. | `recipes-cuo` |
| R8 | An editable install of `amplifier-recipe-runner` pointing at a sibling checkout shadows this repo's `src/`, so the kit — and, before this lane, `discriminate.sh` — silently exercised (and would have *mutated*) a different repo. `discriminate.sh` now refuses; `kit.py` already labels every run `[in-repo]`/`[external]`. The `_bootstrap` search order itself is unchanged, being library-adjacent and out of this lane's scope. | not filed |
| R7 | RCP-012 (`agent_config` retained inert) is a VIOLATION on the shipped **tool module**, a surface this kit does not drive. Recorded as `coverage: none` with that reason. | ledger row already carries `recipes-yh0` |

## 6. Layout

```
kit.py              the checker: fixture registry, assertions, --list/--run/--ledger-map
_bootstrap.py       locates amplifier-recipe-runner; the shared graph-identity serializer
host_adapter.py     a SECOND host -- a standalone process, for the cross-host fixture
discriminate.sh     the discrimination proof
mutations/*.patch   known regressions, as reviewable diffs (6)
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
