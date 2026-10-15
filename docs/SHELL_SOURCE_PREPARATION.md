# Bash template source preparation

Recipe context values must remain data when a trusted Bash template is prepared.
Both recipe engines now use the same source-preparation implementation instead
of deciding quoting from the characters immediately surrounding a replacement.
For example, a value containing `$(...)` remains literal data; it cannot create a
new command substitution, redirect, command name, or here-document boundary.
Trusted authored shell code can still intentionally evaluate strings; this
confines context data in source and is not a shell sandbox.

## Preparation and ownership

The owning engine retains its existing STRING conversion. The compiler binds
only references present in the original template, once per occurrence. It then
assigns those occurrences to original Bash syntax and lexical owners using the
qualified parser. Context text is never rescanned for additional references.

Ordinary arguments retain the admitted space/tab field cardinality and simple
glob behavior. Quoted values retain their literal text and empty-field intent.
Scalar, numeric, assignment, test, comment, and here-document positions have
separate rules. Executable names, redirect syntax, delimiters, and unsupported
source positions are refused rather than guessed. Source-only neighboring
operands keep their exact authored spelling.

The compiler verifies what it emits. A separate word reader checks decoded
literal text, protection masks, expansion order, and field boundaries. The
whole-program witness reparses the emitted source and checks executable
structure, nested substitutions, redirects, lexical ownership, and document
ordering. Here-document and tilde readers independently verify their emitted
representations. A failed witness prevents the existing subprocess launch.

Tilde qualification preserves the ordered request history of the admitted GNU
Bash callers, including assignment context, quoted boundaries, POSIX mode,
brace expansion, and consumed source continuations. It does not execute lookups
during preparation. Here-document handling preserves logical delimiters,
continuations, and terminal newlines; delimiter rotation cannot create a new
executable source boundary.

## Qualified dependencies and runtime

Both packages declare `tree-sitter==0.25.2` and `tree-sitter-bash==0.25.1`. The
reader checks the expected parser profile and fails closed if it is unavailable
or differs. Preparation does not install or fetch a parser.

Interpolated Bash steps currently require the qualified **GNU Bash 5.2.21**
caller profile. Before the existing launch, an in-process bounded read verifies
the selected executable image, one matching GNU version marker, and stable file
metadata. Unknown versions, marker-bearing scripts, ambiguous images, and
changed files report `BASH_RUNTIME_PROFILE`. This is version evidence under the
existing trusted executable/package-integrity boundary, not authentication of
an arbitrary binary. No new installer, helper subprocess, command prelude, or
environment mutation is introduced. Scripts without context references retain
their existing execution contract.

Preparation/runtime refusals follow the existing step failure policy, including
`continue` and `skip_remaining`. Existing successful result payloads, working
directory, environment, timeout, event storage, and scratch ownership are
unchanged. The legacy compatibility goldens remain byte-identical.

The canonical implementation lives in
`modules/tool-recipes/amplifier_module_tool_recipes/shell_source`. The standalone
runner carries a byte-identical package-local mirror so it remains independently
installable. Regenerate with `scripts/sync-shell-source.py`; its `--check` mode
and tests detect changed, missing, or stale mirrored modules.

## Qualification and limits

The source-bound receipt is
[`validation/shell-source-qualification-20261015.json`](validation/shell-source-qualification-20261015.json).
The frozen implementation passed all four repository gates, the sanctioned
actual-CLI source-identity proof, and fourteen actual-engine/independent-checker
pairs with safe Bash effects. No compatibility golden or independent checker was
changed to make the repair pass.
The original gate skips remain recorded: their missing historical Git metadata,
isolated counterpart import, and author-specific example path were qualified by
focused follow-ups rather than silently counted as passes.

All 519 distinct retained command templates, representing 1,227 recorded rows,
received explicit source qualification through both actual engine converters.
The 3,492 cases include role-admitted values and recovered literal recipe
defaults; all 6,984 preparations passed their emitted-source witnesses. This
does **not** mean that the 519 original recipes were executed. Original corpus
commands were not run, historical runtime inputs were not invented, and the
safe effect controls remain a separate evidence tier. The private original
corpus and checker are identified by hashes in the receipt without publishing
their contents.

Other Bash versions, additional executable formats/profiles, and unrepresented
syntax require their own qualification. Installed Windows/macOS acceptance and
live-provider recipe execution are not established by these Linux checks. The
change does not adopt a new dependency revision into Amplifier Unified or alter
an existing shared installation.
