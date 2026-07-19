#!/usr/bin/env bash
# stranded-checkout-check.sh — refuse a `git checkout <remote> -- <path>` that
# would SILENTLY DISCARD local-only work.
#
# THE HAZARD
# ----------
# On a push-blocked box (an agent that may commit locally but may not push —
# see the zeta/cc-02 policy), local commits accumulate that origin has never
# seen. `git checkout origin/main -- <file>` is routinely treated as a safe,
# read-only-ish, "non-push, therefore compliant" operation. It is NOT safe.
#
# It does not UPDATE the file. It REPLACES the file with origin's version —
# a version that, BY CONSTRUCTION, lacks every local-only commit that ever
# touched that path. The commits survive in history (the content is
# recoverable via `git show <sha>:<path>`), but their content vanishes from
# the working tree, and git says NOTHING. There is no conflict, no warning,
# no non-zero exit.
#
# CANONICAL INCIDENT (2026-07-14, 9 / commit c5814933)
#   zeta ran an origin-checkout of core/scripts/owncloud_{backend,sync}.py to
#   "activate post-07-03 own-cloud CAS fixes" that lived on origin. cc-02 was
#   ~200 commits ahead. Three fixes had landed LOCALLY in those exact files and
#   had never been pushed:
#       db6ff7ee  6  _stamp_manifest_baseline (persistent sync baseline)
#       4e82f120      serialize the baseline manifest RMW
#       008f0f82  6  _merge_reconcile_sweep (sweep-side merge reconcile)
#   The checkout brought CAS in and wiped all three out: 273 deletions, 46
#   insertions, ZERO test files touched. Eight tests went red and STAYED red —
#   and were then repeatedly dismissed across later sessions as "baseline
#   failures, not mine." The regression broke the alarm, and the alarm's silence
#   was then used as evidence that nothing was wrong.
#
#   Real-world effect: merge-registered append-only stores (evolution-log,
#   board, changelog...) lost their sweep-side reconcile. One peer append
#   between flushes wedged them PERMANENTLY — local appends never reached S3,
#   peer appends never reached local. Conservative (nothing clobbered) but the
#   fleet's shared stores silently stopped converging.
#
# THE CHECK
#   For each <path>, list commits reachable from HEAD but NOT from the remote
#   ref that touched it. Non-empty => the checkout is DESTRUCTIVE for that path.
#
# CONTRACT
#   $1        = remote ref to check against (e.g. origin/main)
#   $2..$N    = paths the checkout would overwrite
#   exit 0    = safe (no stranded commit touches any path)
#   exit 1    = DESTRUCTIVE (stranded commits would be discarded); the offending
#               commits are printed per-path on stderr
#   exit 0    = also returned, fail-open, when the remote ref cannot be resolved
#               (nothing to compare against => not our call to block)
#
# OVERRIDE
#   STRANDED_CHECKOUT_OVERRIDE="<justification>" — proceed anyway. Use ONLY when
#   discarding the local work is the actual intent (e.g. deliberately abandoning
#   a bad local change). Echoed to stderr for audit.
#
# RECOVERY (if a destructive checkout already happened)
#   The content is NOT lost — the commits are still in local history:
#       git log --oneline <remote>..HEAD -- <path>   # what was dropped
#       git show <sha>:<path>                        # the pre-checkout content
#   Re-apply the dropped hunks ON TOP of the newly-checked-out version. Do NOT
#   `git revert` the checkout wholesale if it also brought in wanted changes —
#   that is how you trade one silent regression for another.

set -uo pipefail

REMOTE="${1:-}"
shift || true

if [ -z "$REMOTE" ] || [ "$#" -eq 0 ]; then
    echo "usage: stranded-checkout-check.sh <remote-ref> <path> [<path>...]" >&2
    exit 0   # fail-open: malformed call is not a licence to block real work
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && cd .. && pwd)"
GIT=(git -C "$PROJECT_ROOT")

# Unresolvable remote => nothing to compare => fail open.
"${GIT[@]}" rev-parse --verify --quiet "${REMOTE}^{commit}" >/dev/null 2>&1 || exit 0

destructive=0
for path in "$@"; do
    # Skip flags/separators that may be passed through from the caller's argv.
    case "$path" in
        --|-*) continue ;;
    esac
    stranded="$("${GIT[@]}" log --oneline "${REMOTE}..HEAD" -- "$path" 2>/dev/null)"
    if [ -n "$stranded" ]; then
        n=$(printf '%s\n' "$stranded" | grep -c .)
        if [ "$destructive" -eq 0 ]; then
            echo "" >&2
            echo "═══ STRANDED-CHECKOUT REFUSED ═══════════════════════════════" >&2
            echo "A checkout from '${REMOTE}' would SILENTLY DISCARD local-only" >&2
            echo "work. '${REMOTE}' has never seen these commits, so its version" >&2
            echo "of the path does not contain them. git will not warn you." >&2
            echo "" >&2
        fi
        destructive=1
        echo "  ${path}" >&2
        echo "    ${n} stranded commit(s) touch this path and would be discarded:" >&2
        printf '%s\n' "$stranded" | head -8 | sed 's/^/      /' >&2
        [ "$n" -gt 8 ] && echo "      ... and $((n - 8)) more" >&2
        echo "" >&2
    fi
done

if [ "$destructive" -eq 1 ]; then
    if [ -n "${STRANDED_CHECKOUT_OVERRIDE:-}" ]; then
        echo "  OVERRIDE ACCEPTED: ${STRANDED_CHECKOUT_OVERRIDE}" >&2
        echo "  Proceeding — the local work above WILL be discarded from the" >&2
        echo "  working tree (still recoverable via \`git show <sha>:<path>\`)." >&2
        echo "" >&2
        exit 0
    fi
    echo "  SAFE PATH: do not check out the whole file. Instead, apply only the" >&2
    echo "  hunks you actually want ON TOP of the current version — then the" >&2
    echo "  local commits above survive." >&2
    echo "" >&2
    echo "  To discard deliberately:" >&2
    echo "    STRANDED_CHECKOUT_OVERRIDE=\"<why>\" <your command>" >&2
    echo "═════════════════════════════════════════════════════════════" >&2
    exit 1
fi

exit 0
