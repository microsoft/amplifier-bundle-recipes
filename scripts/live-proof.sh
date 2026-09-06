#!/usr/bin/env bash
#
# live-proof.sh -- run the real `amplifier` CLI against THIS worktree's engine,
# and refuse to report a pass unless the engine that answered really was this
# worktree's.
#
# Usage:
#   scripts/live-proof.sh <worktree> -- <amplifier tool invoke args...>
#   scripts/live-proof.sh <worktree>            # assertion only, runs nothing
#
# Example:
#   scripts/live-proof.sh "$PWD" -- recipes operation=validate \
#       recipe_path=examples/hello.yaml -b anchors-amp-dev
#
# WHY THIS EXISTS (recipes-669, recipes-ecd)
# ------------------------------------------
# The two procedures a lane reaches for first are both unsafe on a machine
# running several lanes at once, and both fail SILENTLY:
#
#   * Symlinking ~/.amplifier/cache/amplifier-bundle-recipes-*/ at a worktree
#     repoints the cache key EVERY bundle mounts tool-recipes from. While it is
#     in place, every `amplifier` run on the host -- other lanes, real user
#     sessions -- executes that one lane's engine. A lane can "prove" its own
#     fix and have actually proven someone else's code, with a clean pass.
#
#   * `uv pip install -e` (or merely running `amplifier` while that symlink is
#     up) REWRITES the CLI's editable .pth to the lane's realpath, and removing
#     the symlink does not undo it. The leak outlives the procedure.
#
# This script needs neither. `PYTHONPATH` is consulted before site-packages, and
# the tool-recipes editable install is a plain-path .pth (not an import-hook
# finder), so putting the worktree first on PYTHONPATH wins for THIS process
# only and changes nothing on disk for anyone else.
#
# It then proves that it won: `operation=engine_info` reports the running
# engine's own `module_dir_real`, and this script exits non-zero -- naming the
# path that actually answered -- if that is not the worktree under test.
set -uo pipefail

PROG="$(basename "$0")"
EXIT_USAGE=3
EXIT_MISMATCH=2

die() {
	printf '%s: %s\n' "$PROG" "$1" >&2
	exit "${2:-1}"
}

usage() {
	sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//' >&2
	exit "$EXIT_USAGE"
}

[ $# -ge 1 ] || usage
case "$1" in
-h | --help) usage ;;
esac

WORKTREE_RAW="$1"
shift
[ -d "$WORKTREE_RAW" ] || die "not a directory: $WORKTREE_RAW" "$EXIT_USAGE"
WORKTREE="$(cd "$WORKTREE_RAW" && pwd -P)"

MODULE_ROOT="$WORKTREE/modules/tool-recipes"
LIB_ROOT="$WORKTREE/src"
[ -d "$MODULE_ROOT/amplifier_module_tool_recipes" ] ||
	die "no tool-recipes package under $MODULE_ROOT -- is $WORKTREE an amplifier-bundle-recipes worktree?" "$EXIT_USAGE"
EXPECTED="$MODULE_ROOT/amplifier_module_tool_recipes"

INVOKE_ARGS=()
if [ $# -gt 0 ]; then
	[ "$1" = "--" ] || die "expected '--' before the amplifier tool invoke args, got: $1" "$EXIT_USAGE"
	shift
	INVOKE_ARGS=("$@")
fi

AMPLIFIER_BIN="${AMPLIFIER_BIN:-amplifier}"
command -v "$AMPLIFIER_BIN" >/dev/null 2>&1 ||
	die "amplifier CLI not found on PATH (set AMPLIFIER_BIN to override)"

# Reuse the caller's bundle for the probe, so the assertion runs in the same
# bundle context as the invocation it is vouching for.
BUNDLE_ARGS=()
for ((i = 0; i < ${#INVOKE_ARGS[@]}; i++)); do
	case "${INVOKE_ARGS[$i]}" in
	-b | --bundle)
		if [ $((i + 1)) -lt ${#INVOKE_ARGS[@]} ]; then
			BUNDLE_ARGS=(-b "${INVOKE_ARGS[$((i + 1))]}")
		fi
		;;
	esac
done

export PYTHONPATH="$MODULE_ROOT:$LIB_ROOT${PYTHONPATH:+:$PYTHONPATH}"

PY="${AMPLIFIER_PYTHON:-python3}"

# ---------------------------------------------------------------------------
# Assertion: ask the engine who it is, through the real CLI.
# ---------------------------------------------------------------------------

probe_output="$("$AMPLIFIER_BIN" tool invoke recipes operation=engine_info \
	"${BUNDLE_ARGS[@]}" -o json 2>&1)"
probe_status=$?

actual="$(printf '%s' "$probe_output" | "$PY" -c '
import ast, json, sys

raw = sys.stdin.read()
# The CLI prints human preamble ("Bundle ... prepared successfully") before the
# JSON envelope; start at the first line that opens an object.
start = None
for i, line in enumerate(raw.splitlines()):
    if line.lstrip().startswith("{"):
        start = i
        break
if start is None:
    sys.exit(1)
body = "\n".join(raw.splitlines()[start:])
try:
    envelope = json.loads(body)
except Exception:
    sys.exit(1)
result = envelope.get("result")
if isinstance(result, str):
    # `-o json` renders the tool payload with repr(), not json.dumps().
    for parse in (json.loads, ast.literal_eval):
        try:
            result = parse(result)
            break
        except Exception:
            continue
if not isinstance(result, dict):
    sys.exit(1)
value = result.get("module_dir_real") or result.get("module_dir")
if not value:
    sys.exit(1)
print(value)
' 2>/dev/null)"

# The engine that answers may be too old to know `engine_info` at all -- which
# is itself the failure this script exists to catch. Name whose code it would
# have been, using the CLI's own interpreter, rather than shrugging.
fallback_engine() {
	local launcher resolved candidate shebang cli_py
	launcher="$(command -v "$AMPLIFIER_BIN")" || return 1
	resolved="$(readlink -f "$launcher" 2>/dev/null || printf '%s' "$launcher")"

	# uv/pipx install the console script beside the venv's own interpreter.
	for candidate in "$(dirname "$resolved")/python" "$(dirname "$resolved")/python3"; do
		if [ -x "$candidate" ]; then
			cli_py="$candidate"
			break
		fi
	done

	# Otherwise fall back to the launcher's shebang, when it names one.
	if [ -z "${cli_py:-}" ]; then
		shebang="$(head -n 1 "$resolved" 2>/dev/null)"
		cli_py="${shebang#\#!}"
		cli_py="${cli_py%% *}"
		case "$cli_py" in
		*python*) ;;
		*) return 1 ;;
		esac
	fi
	[ -x "$cli_py" ] || return 1
	"$cli_py" -c 'import amplifier_module_tool_recipes as m, os; print(os.path.realpath(os.path.dirname(m.__file__)))' 2>/dev/null
}

expected_real="$(cd "$EXPECTED" && pwd -P)"

if [ -z "$actual" ]; then
	fb="$(fallback_engine || true)"
	{
		printf '%s: NO ENGINE PROVENANCE -- refusing to call this a live proof.\n' "$PROG"
		printf '  expected : %s\n' "$expected_real"
		if [ -n "$fb" ]; then
			printf '  actual   : %s (best-effort probe of the CLI interpreter)\n' "$fb"
		else
			printf '  actual   : unknown (could not probe the CLI interpreter)\n'
		fi
		printf '  probe exit status : %s\n' "$probe_status"
		printf '  PYTHONPATH: %s\n' "$PYTHONPATH"
		printf '  The engine that answered does not implement `operation=engine_info`,\n'
		printf '  so it is NOT this worktree -- this worktree does. Raw probe output:\n'
		printf '%s\n' "$probe_output" | sed 's/^/  | /'
	} >&2
	exit "$EXIT_MISMATCH"
fi

if [ "$actual" != "$expected_real" ]; then
	{
		printf '%s: WRONG ENGINE -- refusing to call this a live proof.\n' "$PROG"
		printf '  expected : %s\n' "$expected_real"
		printf '  actual   : %s\n' "$actual"
		printf '  PYTHONPATH: %s\n' "$PYTHONPATH"
		printf '  The run would have exercised the engine at "actual", not this\n'
		printf '  worktree. Do NOT symlink the bundle cache or `uv pip install -e`\n'
		printf '  to force it -- see docs/TROUBLESHOOTING.md (recipes-669/ecd).\n'
	} >&2
	exit "$EXIT_MISMATCH"
fi

printf '%s: engine verified: %s\n' "$PROG" "$actual"

if [ ${#INVOKE_ARGS[@]} -eq 0 ]; then
	exit 0
fi

# ---------------------------------------------------------------------------
# The invocation itself, in the same proven environment.
# ---------------------------------------------------------------------------

printf '%s: running: %s tool invoke %s\n' "$PROG" "$AMPLIFIER_BIN" "${INVOKE_ARGS[*]}"
"$AMPLIFIER_BIN" tool invoke "${INVOKE_ARGS[@]}"
exit $?
