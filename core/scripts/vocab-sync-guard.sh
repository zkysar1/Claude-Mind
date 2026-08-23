#!/usr/bin/env bash
# vocab-sync-guard.sh — step 2 of the cross-repo shared-vocabulary contract.
#
# Compares a canonical vocabulary declaration against byte-identical mirrors in
# consumer repos, normalizing line endings first so a CRLF checkout does not
# read as drift.
#
# THE DISTINCTION THIS SCRIPT EXISTS FOR (gap-059, encounter 2):
#   repo absent  -> SKIP loudly, exit 0. CI usually clones ONE repo, so a hard
#                   failure here would make the guard unrunnable in the exact
#                   environment it needs to run in — and a guard that cannot run
#                   gets deleted.
#   repo present, mirror file missing -> FAIL. That is real drift, not a
#                   checkout artifact, and folding it into the skip would make
#                   the guard vacuous for the case it was written to catch.
# Collapsing those two into one verdict is the defect. They are different facts.
#
# Exit codes:
#   0 = all present mirrors match (or every mirror was skipped)
#   1 = drift: a present repo's mirror differs from canonical, or is missing
#   2 = usage error, or canonical declaration not readable
#
# Deliberately NOT covered here: step 6's mutation proof. That is already
# mechanized by the `mutation-proof-regression-test` forged skill
# (core/scripts/mutation-proof-test.sh) — invoke it rather than reimplementing.
set -uo pipefail

CANONICAL=""
MIRRORS=()
SKIPPED=0
CHECKED=0
DRIFTED=0

usage() {
    cat >&2 <<'USAGE'
usage: vocab-sync-guard.sh --canonical <path> --mirror <repo-root>:<relpath> [--mirror ...]

  --canonical <path>              the authoritative declaration file
  --mirror <repo-root>:<relpath>  a consumer mirror. <repo-root> is the sibling
                                  repo directory; <relpath> is the mirror path
                                  INSIDE it. If <repo-root> does not exist the
                                  mirror is SKIPPED (exit 0 contribution); if it
                                  exists but <relpath> does not, that is DRIFT.

exit: 0 = in sync (or all skipped) | 1 = drift | 2 = usage / unreadable canonical
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --canonical) [ $# -ge 2 ] || usage; CANONICAL="$2"; shift 2 ;;
        --mirror)    [ $# -ge 2 ] || usage; MIRRORS+=("$2"); shift 2 ;;
        -h|--help)   usage ;;
        *) echo "vocab-sync-guard: unknown argument '$1'" >&2; usage ;;
    esac
done

[ -n "$CANONICAL" ] || usage
[ ${#MIRRORS[@]} -gt 0 ] || usage

# -f, not just -r: `[ -r dir ]` is TRUE for a directory, so a directory argument
# passed the old check, made `tr` emit "Is a directory", left canon_norm EMPTY,
# and every mirror then compared against nothing and reported DRIFT (exit 1).
# That is a false positive naming the wrong cause — it sends the caller hunting a
# mirror divergence that does not exist when the real fault is the argument.
# A bad argument must exit 2 (usage), never 1 (drift).
if [ ! -f "$CANONICAL" ] || [ ! -r "$CANONICAL" ]; then
    echo "vocab-sync-guard: FAIL — canonical declaration is not a readable file: $CANONICAL" >&2
    exit 2
fi

# LF-normalize to a temp file so a CRLF checkout on one side is not read as
# drift. tr -d '\r' is sufficient and avoids a sed portability difference.
canon_norm="$(mktemp)"
trap 'rm -f "$canon_norm" "${mirror_norm:-}"' EXIT
tr -d '\r' < "$CANONICAL" > "$canon_norm"

for spec in "${MIRRORS[@]}"; do
    case "$spec" in
        *:*) repo_root="${spec%%:*}"; rel="${spec#*:}" ;;
        *)   echo "vocab-sync-guard: FAIL — malformed --mirror '$spec' (want <repo-root>:<relpath>)" >&2
             exit 2 ;;
    esac

    if [ ! -d "$repo_root" ]; then
        # LOUD skip. Silence here is what lets a permanently-unrun guard look
        # like a permanently-passing one.
        echo "vocab-sync-guard: SKIP — sibling repo not checked out: $repo_root (mirror $rel unverified)" >&2
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    mirror_path="$repo_root/$rel"
    if [ ! -f "$mirror_path" ]; then
        echo "vocab-sync-guard: DRIFT — repo $repo_root is present but mirror is MISSING: $rel" >&2
        DRIFTED=$((DRIFTED + 1))
        CHECKED=$((CHECKED + 1))
        continue
    fi

    mirror_norm="$(mktemp)"
    tr -d '\r' < "$mirror_path" > "$mirror_norm"
    if cmp -s "$canon_norm" "$mirror_norm"; then
        echo "vocab-sync-guard: ok — $mirror_path matches canonical"
    else
        echo "vocab-sync-guard: DRIFT — $mirror_path differs from $CANONICAL (after LF normalization)" >&2
        DRIFTED=$((DRIFTED + 1))
    fi
    rm -f "$mirror_norm"; mirror_norm=""
    CHECKED=$((CHECKED + 1))
done

echo "vocab-sync-guard: checked=$CHECKED skipped=$SKIPPED drifted=$DRIFTED"
[ "$DRIFTED" -eq 0 ] || exit 1
exit 0
