#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
CMD="${1:-select}"
shift 2>/dev/null || true
source "$CORE_ROOT/scripts/_platform.sh"

# --field <name>: wrapper-level field extraction ().
# WHY THIS EXISTS AND WHY IT IS HERE RATHER THAN IN goal-selector.py:
# The silent-empty guard below emits its FATAL on stderr and exits 7, and BOTH
# real ad-hoc call shapes destroy that signal because they PIPE:
#   `2>&1 | python3 -c json.load` -> the FATAL text merges into the JSON stream,
#      json.load raises, the caller's except-branch yields EMPTY, pipeline rc=0.
#   `2>/dev/null | python3 -c json.load` -> FATAL discarded, json.load raises on
#      empty stdin, caller yields EMPTY, pipeline rc=0.
# Both produce precisely the "no candidates" misreading the guard exists to
# prevent, on the MANDATORY selection path. Measured 103x across 8 live sessions.
# ROOT CAUSE of the piping: `select`/`blocked` take ZERO options (goal-selector.py
# :6391-6392 register both subparsers with no add_argument), so a caller wanting
# one field HAS to hand-roll a parser. This flag removes the NEED to parse, which
# removes the class -- rather than changing the stdout contract, which would risk
# turning a loud JSONDecodeError into a silent wrong answer for the two live
# programmatic callers (iteration-open.py:_selection and
# dependency-timeout-check.py:_read_blocked). Both already capture rc and stdout
# SEPARATELY and branch on rc, so they were never vulnerable and are untouched.
# Parsed HERE, not passed through: goal-selector.py's argparse would reject an
# unknown flag (rb-538 -- multi-layer parsers silently drop or hard-reject).
FIELD=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --field)
      FIELD="${2:-}"
      if [ -z "$FIELD" ]; then
        echo "[goal-selector.sh] FATAL: --field requires a name (e.g. --field goal_id)" >&2
        exit 2
      fi
      # `shift $(( $# >= 2 ? 2 : 1 ))`, not bare `shift 2` (guard-1224). The bare
      # form is out-of-range at $#=1 and bash then does NOT shift, so the loop
      # re-processes the same $1 forever. It is UNREACHABLE here today because the
      # `-z "$FIELD"` check above exits 2 first, and `${2:-}` already supplies the
      # set -u half the guardrail names -- measured: `select --field` (trailing,
      # valueless) exits 2 with the usage line, no hang. The defensive form costs
      # nothing and keeps that safety from depending on the validate-before-shift
      # ORDERING, which a later edit could reorder without noticing.
      shift $(( $# >= 2 ? 2 : 1 ))
      ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
# Silent-empty guard (): rc=0 with 0 bytes on BOTH streams was
# observed intermittently on 3 boxes, and NO code path in goal-selector.py can
# produce it — cmd_select/cmd_blocked end in an unconditional print, so an
# empty ranking is "[]", never "". An empty here is a failure to PRODUCE a
# result, and callers (aspirations Phase 2) cannot distinguish it from "no
# candidates" (guard-3440 class). Capture to an UNSYNCED temp file, assert
# non-empty, then emit — the empty becomes a loud nonzero exit. The temp file
# deliberately lives OUTSIDE the repo/agents tree: capture paths inside the
# own-cloud synced tree can be truncated mid-run by the sync (the 
# log-rewrite class), and this guard must not inherit that.
# Exit 7 on the empty — distinct from python tracebacks (1), argparse (2),
# daemon-unreachable (3), and timeout kills (124), so a caller's rc log alone
# identifies the signature. stderr passes through live (banners untouched).
OUT_TMP="$(mktemp "${TMPDIR:-/tmp}/goal-selector-out.XXXXXX")"
trap 'rm -f "$OUT_TMP"' EXIT
rc=0
python3 "$CORE_ROOT/scripts/goal-selector.py" "$CMD" ${ARGS[@]+"${ARGS[@]}"} > "$OUT_TMP" || rc=$?
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
if [ ! -s "$OUT_TMP" ]; then
  echo "[goal-selector.sh] FATAL: goal-selector.py exited 0 with EMPTY stdout — the g-115-6146 silent-empty signature, not a legitimate result (an empty ranking prints '[]'). Do NOT read this as 'no candidates'; re-run the selector." >&2
  exit 7
fi
# Field extraction runs AFTER the silent-empty guard, reading the captured FILE
# -- so the guard's exit 7 still fires first and no pipe exists to swallow it.
# Exit codes are deliberately distinct so a caller reading rc alone can tell the
# three cases apart, which is the whole point of the flag:
#   7 = silent-empty signature (re-run the selector; NOT "no candidates")
#   8 = ran fine, genuinely ZERO candidates  <- the reading 7 must never be given
#   9 = the requested field is absent from the payload
if [ -n "$FIELD" ]; then
  python3 - "$OUT_TMP" "$FIELD" <<'PYEXTRACT'
import json, sys
path, field = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path, encoding="utf-8"))
except Exception as e:
    sys.stderr.write("[goal-selector.sh] FATAL: captured output is not JSON (%s)\n" % e)
    sys.exit(1)
# cmd_select emits TWO legitimate top-level shapes (goal-selector.py:6197 a LIST
# of ranked rows; :6011 an all_blocked DICT). Handle both -- rejecting the dict
# would fail in exactly the state whose signal matters most ().
if isinstance(d, dict):
    if field not in d:
        sys.stderr.write("[goal-selector.sh] FATAL: field %r absent from the all-blocked object; keys: %s\n"
                         % (field, ",".join(sorted(d))))
        sys.exit(9)
    val = d[field]
elif isinstance(d, list):
    if not d:
        sys.stderr.write("[goal-selector.sh] ZERO candidates (a real, measured empty ranking -- "
                         "NOT the exit-7 silent-empty signature).\n")
        sys.exit(8)
    top = d[0]
    # Rows are FLAT: goal_id/score/title are top-level keys on each row. There is
    # no nested "goal" object -- confirmed against the emit site (guard-318).
    if not isinstance(top, dict) or field not in top:
        sys.stderr.write("[goal-selector.sh] FATAL: field %r absent from the top row; keys: %s\n"
                         % (field, ",".join(sorted(top)) if isinstance(top, dict) else type(top).__name__))
        sys.exit(9)
    val = top[field]
else:
    sys.stderr.write("[goal-selector.sh] FATAL: unexpected top-level type %s\n" % type(d).__name__)
    sys.exit(1)
print(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
PYEXTRACT
  exit $?
fi
cat "$OUT_TMP"
