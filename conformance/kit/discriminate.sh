#!/usr/bin/env bash
#
# DISCRIMINATION PROOF -- the reason this kit exists.
#
# A conformance kit that passes is worth nothing on its own; a kit that cannot
# fail is decoration. This script proves the kit discriminates: it temporarily
# reintroduces a known contract violation into the runner, runs the kit, and
# requires that at least one fixture FAILS. Then it reverts.
#
# Mutations live in mutations/*.patch. Each one is a real regression the
# contracts name:
#
#   caller-map-fallback.patch    -- the planner resolves an undeclared agent
#                                   from the caller session (manifest.v1 Core 3)
#   host-agent-precedence.patch  -- the spawn adapter honours the host's
#                                   agent_configs (manifest.v1 Core 5)
#
# Exit 0 only if EVERY mutation was caught. A mutation the kit sleeps through
# is reported as a HOLE, loudly, and fails this script.
#
# Usage:  ./discriminate.sh [mutation-name ...]
set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MUTATIONS_DIR="$KIT_DIR/mutations"

# ---------------------------------------------------------------------------
# Locate the runner checkout (the same search order kit.py uses)
# ---------------------------------------------------------------------------
runner_src="$(python3 -c "
import pathlib, sys
sys.path.insert(0, '$KIT_DIR')
import _bootstrap
_bootstrap.ensure_runner_importable()
import amplifier_recipe_runner as runner
print(pathlib.Path(runner.__file__).resolve().parents[1])
")" || {
  echo "FATAL: could not locate amplifier_recipe_runner. Set AMPLIFIER_RECIPE_RUNNER_SRC." >&2
  exit 1
}

RUNNER_REPO="$(cd "$runner_src" && git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "FATAL: the runner source at $runner_src is not in a git checkout;" >&2
  echo "       this script refuses to mutate a tree it cannot revert." >&2
  exit 1
}

echo "kit:          $KIT_DIR"
echo "runner repo:  $RUNNER_REPO"
echo

# ---------------------------------------------------------------------------
# Refuse to mutate a dirty tree -- reverting would clobber real work
# ---------------------------------------------------------------------------
dirty="$(cd "$RUNNER_REPO" && git status --porcelain -- '*.py' | grep -v '__pycache__' || true)"
if [[ -n "$dirty" ]]; then
  echo "FATAL: the runner checkout has uncommitted Python changes:" >&2
  echo "$dirty" >&2
  echo "       Commit or stash them first. This script will not risk reverting your work." >&2
  exit 1
fi

revert_all() {
  (cd "$RUNNER_REPO" && git checkout -- 'src/amplifier_recipe_runner/*.py' 2>/dev/null || git checkout -- src)
}
trap 'echo; echo "reverting runner source..."; revert_all' EXIT

# ---------------------------------------------------------------------------
# Baseline: the kit must PASS before any mutation
# ---------------------------------------------------------------------------
echo "=== BASELINE (unmutated implementation) ==="
if ! python3 "$KIT_DIR/kit.py" --run; then
  echo
  echo "FATAL: the kit does not pass against the unmutated implementation." >&2
  echo "       Fix that first -- a red baseline makes the proof meaningless." >&2
  exit 1
fi
echo

# ---------------------------------------------------------------------------
# Each mutation must be CAUGHT
# ---------------------------------------------------------------------------
if [[ $# -gt 0 ]]; then
  patches=()
  for name in "$@"; do patches+=("$MUTATIONS_DIR/${name%.patch}.patch"); done
else
  patches=("$MUTATIONS_DIR"/*.patch)
fi

holes=0
caught=0
for patch in "${patches[@]}"; do
  name="$(basename "$patch" .patch)"
  echo "=== MUTATION: $name ==="
  sed -n 's/^+.*MUTATION (conformance kit): \(.*\)/  intent: \1/p' "$patch"
  echo

  if ! (cd "$RUNNER_REPO" && git apply "$patch"); then
    echo "FATAL: could not apply $patch -- the implementation has moved." >&2
    echo "       Regenerate the mutation against the current source." >&2
    exit 1
  fi

  python3 "$KIT_DIR/kit.py" --run
  status=$?
  revert_all

  if [[ $status -eq 0 ]]; then
    echo
    echo "HOLE: the kit PASSED against a knowingly-broken implementation ($name)."
    echo "      No fixture discriminates this violation. The kit needs a new one."
    holes=$((holes + 1))
  else
    echo
    echo "CAUGHT: the kit failed against $name, as it must."
    caught=$((caught + 1))
  fi
  echo
done

echo "=== DISCRIMINATION SUMMARY ==="
echo "mutations caught: $caught"
echo "holes:            $holes"
if [[ $holes -gt 0 ]]; then
  echo
  echo "RESULT: NOT DISCRIMINATING -- $holes mutation(s) went unnoticed."
  exit 1
fi
echo
echo "RESULT: DISCRIMINATING -- every mutation was caught, and the baseline passes."
