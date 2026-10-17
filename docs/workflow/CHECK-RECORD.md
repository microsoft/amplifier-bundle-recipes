# Verification record

## 2026-10-14 — ratified Bash policy publication only

**Scope.** The owner-ratified Core 10 policy delta and its retained proposal;
not an implementation change, full-contract lock, schema activation or merge
approval. Comparison revision: `3a8a0915464f41c70d81bc6a881a9d3bcc9a0a71`.

**Checks performed independently after authoring.** A static Python assertion
read the proposal's two fenced before/after blocks, required exactly one match
of the before block in the comparison revision, reconstructed the target using
only the replacement plus the dated policy changelog, and compared the complete
target bytes. It also checked the DRAFT status, unchanged runner contract and
VISION, unchanged runtime/recipe/test/golden files, and removal of private
conversation, reviewer aliases, host paths and session identifiers from the
public proposal.

Observed result:

```text
PASS: exact candidate payload applied; only Core 10 plus dated policy changelog changed; originals remain DRAFT; runner/VISION and all runtime/recipe/test/golden files unchanged.
```

Published-document content digests checked at that boundary:

- `contracts/recipe-dependency-manifest.v2-candidate.md`:
  `4302debc1f5051fc064d57b19da67855036804f9473586368d103fa391b6aa58`
- `contracts/recipe-dependency-manifest.v1.md`:
  `1b673e785ac890b57e6da9eeeffe2f405a524b7ede8591c1226055f88c7bebbd`

**Review disposition.** The two independent policy reviews agreed on the
substance and retained the engineering hold. One approved the supplied delta;
the other withheld publication as written until existing-pattern compatibility
was explicit and private process details were removed, while explicitly asking
for no additional review round for those corrections. The corrections were
applied and checked. This is not a claim of two unconditional approvals of a
new implementation or of an independently reviewed executable security fix.

**Guard.** The direct document edit succeeded through the normal write path;
the target was DRAFT. No locked-document escape receipt or whole-document freeze
is claimed.

**Installed consumer journey.** N/A for this documentation-only policy record;
no installed executable changed. Engine security, unchanged-recipe compatibility,
package isolation, activation, sanctioned CLI provenance and both final code
reviews remain required and uncompleted. The four repository runtime gates were
not rerun for this record, and no prior attempt budget was reset. Consequently
this publication is a policy branch, not a new product PR or a merge of the
vulnerable implementation.

## 2026-10-17 — conditional-regex correction, independent verification

**Scope and revisions.** Quoted and unquoted `[[ ... =~ RHS ]]` preparation in
both engines, compared with `0a1f76026d26cae206f0acc9c7e586ffc65fd72d`.
Implementation commits `9d83f61` and `aed0383` were independently inspected and
tested, with an additional correction for quoted apostrophes and empty-quote
scaffolding. The existing 22 converter cases were moved into the mandatory
module gate; their original function bodies, decorators and assertions remain
AST-identical. Recipe files, contracts, VISION, runtime support profiles and
golden baselines are unchanged.

**Checks independently rerun.** In an isolated Ubuntu 24.04 arm64 environment,
GNU Bash 5.2.21, tree-sitter 0.25.2 and tree-sitter-bash 0.25.1:

```text
PYTHONPATH=src:modules/tool-recipes python -B -m pytest modules/tool-recipes -q
  Python 3.12.3: 1858 passed; Python 3.11.17: 1858 passed.
PYTHONPATH=src python -B -m pytest src/amplifier_recipe_runner/tests -q
  596 passed.
PYTHONPATH=src:modules/tool-recipes python -B conformance/kit/kit.py --run
  20/20 fixtures passed.
PYTHONPATH=src:modules/tool-recipes python -B conformance/legacy-compat/harness.py --assert
  Five cases byte-identical to the existing baselines.
python -B scripts/sync-shell-source.py --check
  Passed; all 19 canonical/mirrored Python modules byte-identical.
```

The commands used prepared isolated interpreters, rather than the literal
`uvx` resolution command. Six existing deprecation warnings remained in each
module-suite run. The focused converter/witness selection passed 185 cases,
including six apostrophe comparisons and an independent forged-protection
control. Benign comparisons check actual Bash stdout, stderr and status;
confinement tests never execute malicious raw-reference substitutions.

**Installed consumer evidence.** A fresh noneditable runner wheel was built and
installed in a separate environment, with `PYTHONPATH` unset and the canonical
tool module absent. All 19 shell-source modules agreed between source, wheel,
RECORD and installed bytes. Six installed owning-helper/Bash comparisons passed:
active dot and anchors, quoted literal dot, and three apostrophe variants.
The installed runner suite passed 595 tests with one inherited counterpart-only
comparison skipped; that comparison passed in the 596-test source run above.
The installed suite used pytest's `--import-mode=importlib`, because tests are
deliberately excluded from the wheel; the initial default-mode collection
failure was retained as a test-harness failure, not a product defect.

**Limits and disposition.** This verifies the scoped correction, not every
unseen recipe pattern. The hard Bash 5.2.21 support boundary remains unchanged
and unresolved for other intended consumers. No arbitrary retained recipes,
live provider, installed Windows/macOS or sanctioned Amplifier host-CLI journey
was run. Independent final model reviews and publication remain separate;
this entry is not merge, release or production-adoption approval.
