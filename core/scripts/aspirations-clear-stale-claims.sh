#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-clear-stale-claims — daemon-aware wrapper (PR 50).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source / --dry-run flags
#   3. POST /v1/aspirations/clear-stale-claims with source & dry_run as query params
#   4. On 200, print "cleared N records" + per-goal IDs to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
source "$CORE_ROOT/scripts/_argv_strict.sh"

# ONE literal, shared by the help text and the refusal message — never two
# copies (see argv_strict_refuse_unknown's header in _argv_strict.sh).
_ACCEPTED_FLAGS="--source <world|agent> | --dry-run"

SOURCE_VAL="world"
DRY_RUN_VAL="false"
# PASSTHROUGH_SOURCE / PASSTHROUGH_DRY_RUN deleted () — both were
# write-only. QUERY below is built from SOURCE_VAL and DRY_RUN_VAL alone, there
# is no fallback exec, and neither array was ever passed to anything.
# NOTE for the rest of this rollout: "dead" is NOT a property of the NAME.
# core/scripts/tree-read.sh's identically-named array IS live (it is tree.py's
# argv), and aspirations-add.sh's is PARTIALLY live (read for the single literal
# "--schema"). Three different verdicts so far — check for a reader per wrapper
# before deleting one.

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --dry-run)
            DRY_RUN_VAL="true"
            shift;;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0. This wrapper never
            # reads stdin, so there is no hang class here.
            argv_strict_help "$(basename "$0")" "[flags]" \
                "$_ACCEPTED_FLAGS" \
"  This command MUTATES: without --dry-run it CLEARS the stale claims it finds.
  --dry-run is the preview, and it is opt-IN — the default is a real clear.
  There is no positional argument; --source selects WHICH queue is swept and
  defaults to world."
            ;;
        -*)
            # REFUSE (). The most dangerous swallow measured in this
            # rollout: the discarded token here is a SAFETY flag, and dropping it
            # fails OPEN into destruction rather than into a wrong answer. The old
            # catch-all arm did nothing but shift — no passthrough at all, the
            # token was simply discarded. (Deliberately NOT writing that arm out
            # verbatim here: a literal double-semicolon inside a comment used to
            # truncate test_no_silent_passthrough_arm_remains' body walk, which
            # then reported this very arm as non-refusing. The walk is now
            # comment-aware, and this wording no longer depends on that.)
            # MEASURED on this box before the fix, with the
            # daemon call stubbed so nothing was actually cleared:
            #   --dry-run    -> source=world&dry_run=true    (preview)
            #   --dryrun     -> source=world&dry_run=false   <- REAL CLEAR
            #   --dry_run    -> source=world&dry_run=false   <- REAL CLEAR
            #   --dry-runn   -> source=world&dry_run=false   <- REAL CLEAR
            #   --sorce agent --dry-run -> source=world&...  <- WRONG QUEUE swept
            # Three plausible misspellings of --dry-run each turn a preview into a
            # live destructive mutation, with an identical exit status and no
            # complaint. Both are now rc=2 and no request is sent at all.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # KNOWN RESIDUAL, matching the rest of this rollout: a stray
            # POSITIONAL is still discarded silently. It is inert here — this
            # wrapper reads no positional at all — but it is not refused either.
            # _argv_strict.sh carries the remedy
            # (argv_strict_refuse_extra_positional, maxpos 0); not adopted because
            # guard-1562 requires enumerating what would NEWLY fire, and this unit
            # measured the FLAG surface only.
            shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="source=${SOURCE_VAL}&dry_run=${DRY_RUN_VAL}"

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/clear-stale-claims \
    --query "$QUERY")" || rc=$?

case $rc in
    0)
        # 200: parse response. Print "cleared N records" + per-goal IDs
        # (matches legacy CLI output shape).
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
dry = resp.get('dry_run', False)
prefix = 'would clear' if dry else 'cleared'
cleared = resp.get('cleared_ids', [])
print(f'{prefix} {len(cleared)} records')
for gid in cleared:
    print(f'  {gid}')
"
        exit 0;;
    2)
        # Daemon answered 4xx/5xx; body already written to stderr by rt_curl.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/clear-stale-claims \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
dry = resp.get('dry_run', False)
prefix = 'would clear' if dry else 'cleared'
cleared = resp.get('cleared_ids', [])
print(f'{prefix} {len(cleared)} records')
for gid in cleared:
    print(f'  {gid}')
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-clear-stale-claims.sh";;
    *)
        exit $rc;;
esac
