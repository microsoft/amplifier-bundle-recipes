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
