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

## 2026-10-18 — legacy arithmetic and document-boundary follow-up

**Scope and provenance.** The cumulative implementation through `a00fdc9`,
compared with merged `2e14948`, fixes active legacy arithmetic hidden by a regex
operand, preserves parameter-pattern boundaries inside arithmetic, and retains
authored nested-document content in the independent program witness. The final
integration correction derives header-LF authority from the original final
field, not an entire argument group or emitted field claims. No recipe,
contract, dependency, support-profile, generator or golden baseline changed.
The generated copies remain byte-identical to the canonical helpers.

The returned implementation was `c273d72`; verification exposed an additional
multiargument header-LF refusal. The integrating author implemented and tested
the small `a00fdc9` correction. A separate source reviewer inspected that
correction and closed the finding; this was independent inspection, not
independent execution. Two final model reviews of the same supplied evidence
approved the scoped repair, one with the recording conditions addressed below.
Neither certified whole-product, release or production acceptance.

**Executed checks on the final implementation.** Isolated Ubuntu 24.04 arm64,
Python 3.12.3, Bash 5.2.21, tree-sitter 0.25.2 and tree-sitter-bash 0.25.1:

```text
Prepared module interpreter: 2317 passed, 6 existing warnings.
Prepared native-source interpreter: 596 passed.
Structural conformance: 20/20 fixtures passed.
Legacy compatibility: 5 cases byte-identical; no baselines re-recorded.
Generated shell-source mirror: --check passed.
Focused final selection: 644 passed.
Installed native runner: 595 passed, 1 counterpart-only import skipped.
```

The skipped installed comparison passed in the source run. A fresh normal wheel
was installed separately; all 19 helpers agreed between canonical source,
generated source, wheel, RECORD and installed bytes. Eleven fixed helper/Bash
checks passed in each module-only legacy and installed-native environment.
The exact repaired parameter-pattern command produced `2:z` through both
helpers, both real engine Bash-step methods and the installed native package.
The unchanged nested-document program verified; body-only synthetic corruption
with cleared field certificates raised `PROGRAM_WITNESS`, and the independent
local witness still raised `WORD_WITNESS`. Corrupted programs were never run.
The multiargument header-LF case produced `3`, matching the raw reference.
All 410 tracked source hashes matched the tested and retained source.

**Documented-command limitation, not a green gate.** The literal documented
`PYTHONPATH=src uvx --with pyyaml --with click pytest ...` runner command was
also run on the unchanged comparison and final source. Both produced
71 failures, 521 passes and 4 skips because the isolated command omits pinned
parser dependencies (`PARSER_PROFILE`). Adding those existing parser pins to a
supplementary invocation produced 592 passes and 4 skips: three missing
Foundation, one missing legacy counterpart in that minimal environment. The
prepared 596-test source run closed those cases. These results are distinct;
the prepared passes do not make the literal command green.

**Residual comparison condition.** Fresh, source-bound comparisons used raw Bash
and both owning helpers on original merged `2e14948`, intermediate `b909379`,
and final `a00fdc9`. No candidate checkout was imported as a baseline.

| Fixed residual | Raw reference | Original merge, both helpers | Intermediate and final, both helpers |
|---|---|---|---|
| `${v//]/1}` inside modern or bare arithmetic, fixed `v=']'` | `2:z`, exit 0 | `INVALID_TEMPLATE_SYNTAX` | Same refusal |
| Same literal pattern inside legacy arithmetic | Empty stdout, exit 1 | `INVALID_TEMPLATE_SYNTAX` | Same refusal |
| Nested arithmetic DATA document plus adjacent outer ARG DATA | `3z`, exit 0 | `INVALID_TEMPLATE_SYNTAX` | `WORD_WITNESS` |

The last template is
`printf '%s' $(($(cat <<FIRST <<DOC\n7\nFIRST\n{{n}}\nDOC\n)+1)){{p}}`
with `n="2"`, `p="z"`. These refusals predate this follow-up; their error category
is not uniformly unchanged. They are retained compatibility gaps, not newly
granted support exceptions or evidence that required benign behavior is kept.

**Six initial new-test failures, with reference justification.** The first two
were implementation failures, not corrections to expected Bash output. The last
four had incorrectly expected successful raw legacy arithmetic. Their new
expectations follow the independently rerun raw Bash command, not candidate
output. Parameter spellings and the closing-pattern helper refusal stayed intact.

| New-test case | Original failure | Correction and measured reference |
|---|---|---|
| Bare authored-body descriptor, canonical namespace | Bodies `2` and `4` had equal signatures | Recognize the pinned `compound_statement` bounded by `((`/`))`; final signatures differ; inequality assertion unchanged |
| Same descriptor, native namespace | Same | Generated implementation correction; assertion unchanged |
| Legacy `${v//[/1}`, legacy helper | Expected raw `2:z`, exit 0 | Raw stdout empty, exit 2, matching-`]` EOF diagnostic |
| Legacy `${v//[/1}`, native helper | Same | Same independently measured raw reference |
| Legacy `${v//]/1}`, legacy helper | Expected raw `2:z`, exit 0 | Raw stdout empty, exit 1, no-closing-`}` diagnostic |
| Legacy `${v//]/1}`, native helper | Same | Same independently measured raw reference |

The corresponding tests are
`test_boundary_arithmetic_body_descriptor_keeps_unmasked_authored_bytes` and
`test_boundary_fixed_parameter_reference_matrix`. Initial failed outputs were
retained. All 27 existing `b909379` test functions and all 41 returned `c273d72`
test functions, including decorators, are unchanged in the final increment.
These are function counts, not parameterized case counts.

**Lessons and remaining limits.** An original final-field cooker determines
handoff authorization; future cooker changes must preserve the positive and
negative authority tests. Metadata and a local field certificate do not replace
program-level traversal of enclosing executable arguments. Green standard suites
did not find every intermediate boundary regression, so the fixed differential
counterexamples remain regression tests. The hard Bash 5.2.21 support boundary,
actual-value qualification of the retained recipe corpus, full Amplifier
CLI/provider journey and installed Windows/macOS acceptance remain open. No
general compatibility waiver, release or production-adoption claim follows from
this scoped correction.
