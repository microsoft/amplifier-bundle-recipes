# AGENTS.md — working in this repo

Conventions for any agent (or human) changing `amplifier-bundle-recipes`.

## What lives where

| Path | What it is |
|------|------------|
| `modules/tool-recipes/` | The Amplifier **tool module** — the `recipes` tool, the legacy engine, session/steps.jsonl handling |
| `src/amplifier_recipe_runner/` | The **runner library** — the one execution home for `schema_version: 2` recipes |
| `conformance/kit/` | Structural fixtures pinning the library's contracts |
| `conformance/legacy-compat/` | Golden baselines pinning legacy recipe behaviour **byte-for-byte** |
| `scripts/live-proof.sh` | The sanctioned way to prove an engine change live (see below) |

## Gates

Run all four before opening a PR:

```bash
"${AMPLIFIER_PYTHON:-python3}" -m pytest modules/tool-recipes -q
PYTHONPATH=src uvx --with pyyaml --with click pytest src/amplifier_recipe_runner/tests -q
PYTHONPATH=src:modules/tool-recipes "${AMPLIFIER_PYTHON:-python3}" conformance/kit/kit.py --run
PYTHONPATH=src:modules/tool-recipes "${AMPLIFIER_PYTHON:-python3}" conformance/legacy-compat/harness.py --assert
```

**Never re-record the legacy-compat baselines to make a change pass.** They pin
the serialized `ToolResult` payload of `execute` / `resume` / `approve` for
legacy recipes. If your change needs to add something to a result, attach it
*beside* the payload (`object.__setattr__(result, ...)`, as
`runner_adapter.label_execution_mode`, `shutdown.attach_shutdown_warning` and
`engine_provenance.label_engine_provenance` all do), not inside `output`.

## Regenerating `bundle.dot` / `bundle.png`

Both are **generated artifacts**, not hand-maintained ones. They are produced
from the repo by `amplifier_foundation.bundle_docs.bundle_to_dot.bundle_repo_dot`,
and every node carries a token-cost estimate — so editing `bundle.md`, an agent
description, a context file, or a module's tool schema makes them wrong.

If you change any of those, regenerate **from the repo root** and commit both:

```bash
"${AMPLIFIER_PYTHON:-python3}" -c "from amplifier_foundation.bundle_docs.bundle_to_dot import bundle_repo_dot; open('bundle.dot','w').write(bundle_repo_dot('.'))"
dot -Tpng bundle.dot -o bundle.png
```

`modules/tool-recipes/tests/test_bundle_dot_freshness.py` is the gate: it
regenerates and fails when the checked-in `source_hash` (or the file's bytes —
the graph title carrying the bundle **version** sits outside the hash) no longer
matches, printing both hashes and the command above. It runs as part of the
first gate, so a stale diagram is a red test rather than silent rot.

## Proving an engine change LIVE

Passing tests prove your code is correct. They do not prove the **CLI ran your
code**. On a machine with several worktrees of this repo, a live run can execute
a different checkout entirely and report a clean pass (`recipes-669`,
`recipes-ecd`).

**THE way to prove it:**

```bash
./scripts/live-proof.sh "$PWD" -- recipes operation=validate \
    recipe_path=examples/bash-step-example.yaml -b anchors-amp-dev
```

The script puts `<worktree>/modules/tool-recipes` and `<worktree>/src` first on
`PYTHONPATH`, asks the engine who it is via `operation=engine_info`, and **exits
non-zero naming the path that actually answered** unless it is your worktree.
Only then does it run your invocation. Nothing on disk is modified, and no other
session on the host is affected.

You can ask directly at any time:

```bash
amplifier tool invoke recipes operation=engine_info -b anchors-amp-dev -o json
```

**FORBIDDEN — both leak your engine into every other session on the host:**

- ❌ **Symlinking the bundle cache**
  (`ln -s <worktree> ~/.amplifier/cache/amplifier-bundle-recipes-<hash>`).
  That path is the cache key *every* bundle mounts tool-recipes from. While the
  symlink exists, every `amplifier` run on the machine — other lanes, other
  agents, real user sessions — executes your code regardless of which bundle
  they asked for. Recorded in `recipes-669`: a lane's own warning never fired
  because the module came from someone else's symlinked tree, and the run still
  reported success.

- ❌ **`uv pip install -e` into the CLI's venv.** Running `amplifier`
  re-resolves editable installs and rewrites
  `.../site-packages/_editable_impl_amplifier_module_tool_recipes.pth` to a
  realpath. Removing the symlink afterwards does **not** undo it. Recorded in
  `recipes-ecd`: the cache directory was restored correctly and the CLI still
  imported the lane's worktree until the `.pth` was hand-edited.

Neither is needed. `PYTHONPATH` is consulted before `site-packages`, and the
tool-recipes editable install is a plain-path `.pth` (not an import-hook
finder), so the worktree wins for that process alone.

Details, failure-mode table, and the `steps.jsonl` header: see
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#which-engine-actually-ran-proving-a-change-live).

## Parallel work

Several lanes may be editing this repo at once. Keep a change **local to the
region it owns** — no drive-by reformatting, no moving unrelated code, no
renaming shared helpers unless that is the change.
