# Conformance ledger — amplifier-bundle-recipes

`ledger.yaml` is this repo's clause-granular conformance ledger, seeded from the
two contracts in `contracts/`. Convention: `@converge:docs/LEDGER-FORMAT.md`
(DRAFT starter format; deviations below are data for its v1).

## 1. What this ledger tracks

| | |
|---|---|
| Contracts pinned | `contracts/recipe-dependency-manifest.v1.md`, `contracts/recipe-runner-lib.v1.md` |
| Contract status | **DRAFT** (neither is FROZEN) |
| Rows | 21 — one SYNC row + one per checkable Core clause (manifest Core 1–12, lib Core 1–8) |
| Seeded at | repo sha `42d628f`, 2026-09-01 |

Seed dispositions: **18 GAP · 1 VIOLATION · 1 OPEN-PINNED · 1 CONFORMS (SYNC)**.

The heavy GAP count is the honest, expected state. The subject of both contracts
— the `amplifier-recipe-runner` library — does not exist yet. A GAP row is not a
bug in the ledger; it is the ledger doing its job.

Two rows are worth reading before anything else:

- **RCP-012 — VIOLATION.** `agent_config` is declared on the step model
  (`models.py:232`), validated only to forbid it on bash steps
  (`models.py:383-385`), and read nowhere for agent steps. That is exactly the
  "silently retained inert" state manifest Core 12 names and forbids, and it is
  shipped today. Two legal exits: implement it under schema v2, or reject it at
  parse.
- **RCP-011 — OPEN-PINNED.** A returned need, not a guess. See §4.

## 2. Baseline caveat: these contracts are DRAFT

Rows are seeded against a spec that may still change. Two events demand a
**full-ledger re-review**, never a silent edit:

1. A SYNC hash mismatch (LEDGER-FORMAT §4).
2. A FROZEN stamp on either contract — freezing changes what the rows bind to.

## 3. Deviations from LEDGER-FORMAT.md (named, not silent)

| # | Deviation | Why |
|---|---|---|
| 1 | Ledger lives at `conformance/ledger.yaml`, not `ledger/rows.yaml` | The seeding lane's sole file ownership was `conformance/`. Placement only; the row schema and top-level-list shape are unchanged. |
| 2 | `assertion.status: planned` on every row | No `checks/` directory exists and this lane could not create one. Each row names the probe it *will* have; none resolve yet. |
| 3 | `work:` is always a **list** of tracker ids | Several clauses are genuinely satisfied by more than one item (e.g. RCP-010 spans `recipes-aew`, `recipes-8yo`, `recipes-o6f`). A uniform list beats a mixed string/list type. |
| 4 | SYNC row (`RCP-000`) carries a `sync:` list and no `contract:`/`quote:` | It asserts no clause. LEDGER-FORMAT §4 specifies what SYNC pins but not its row shape. |
| 5 | NOT-ASSERTABLE **sub-claims** are recorded in `notes:` with a greppable `NOT-ASSERTABLE (sub-claim):` marker, rather than as their own rows | The format is one row per clause. Splitting a clause into rows would invent rows no clause backs. Affected: RCP-101 ("thin adapter" — architectural judgment), RCP-105 ("cache location is not part of the public semantic contract" — unfalsifiable by construction). |

### Tripwire status (LEDGER-FORMAT §6)

| Tripwire | Status |
|---|---|
| 1. Every REQUIRED clause of every FROZEN contract cited by ≥1 row | **N/A** — no FROZEN contract yet. All 20 Core clauses of both DRAFTs are cited. |
| 2. Every ledgered divergence/amendment cited by ≥1 row | **N/A** — no divergences or amendments exist. |
| 3a. Every row's quote verifies against contract bytes | **PASS** — all 20 clause quotes verified as whitespace-collapsed contiguous substrings at seed time; both SYNC hashes matched. |
| 3b. Every assertion ref resolves | **NOT YET RUNNABLE** — see deviation 2. Blocked on probe authoring (`recipes-7ex`). |
| 3c. Every GAP/VIOLATION row carries a live `work` ref | **PASS** — all 19 carry at least one open `recipes-*` item. |

Tripwires are not wired into CI by this lane. Until 3b is satisfied, this is a
seeded ledger, not yet a running drift detector.

## 4. Returned needs (open, for the owner / protocol-authority)

1. **RCP-011 — how does a prohibition-only clause dispose while its subject is
   absent?** Manifest Core 11 forbids namespace inference. Nothing infers today,
   but only because no runner and no dependency concept exist. GAP is wrong
   (nothing is required to be built); VIOLATION is wrong (nothing contradicts
   it); NOT-ASSERTABLE is wrong (an `absence` probe checks it fine). That leaves
   CONFORMS-vacuously or stay-pinned, and the contract text does not settle it.
   Pinned rather than guessed. **This ruling generalizes** — future prohibition-
   only clauses will hit it again.
2. **Probe authoring is unowned by this lane.** Every row names a probe that does
   not exist. Routing to `recipes-7ex` (conformance fixture kit) is this lane's
   suggestion, not a decision.

No contract defects were found while seeding. RCP-012's VIOLATION is a defect in
the **code**, not in the contract.

## 5. Working on this ledger

- **Row ids are stable forever.** Never renumber, never reuse — rows are cited
  from tracker items and amendments.
- **Quotes are the binding anchor.** They must stay verbatim (whitespace-collapsed
  contiguous match). If a contract's wording changes, the row is re-reviewed —
  the quote is not quietly re-typed to match.
- **Drift is bidirectional.** A GAP row that silently becomes satisfied is just as
  much a ledger that lies as a CONFORMS row that silently regresses. Either
  direction demands a row update in the same change, with its probe.
