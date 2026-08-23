#!/usr/bin/env bash
# core/scripts/check-merge-driver-registered.sh — per-clone merge-driver
# REGISTRATION assertion (, 2026-08-20).
#
# SIBLING, NOT DUPLICATE, of check-merge-driver-drift.sh. That script asks
# "does any ledger resolve to the WRONG driver" (a `.git/info/attributes`
# override pointing at merge=union). This one asks the question NOTHING asked:
# "does the driver every ledger resolves to actually EXIST on this clone?"
#
# WHY THE TWO CANNOT BE ONE CHECK. `git check-attr` is answered entirely from
# the attributes files; it has NO knowledge of git config. MEASURED 2026-08-20
# in a throwaway repo with `.gitattributes` mapping a file to
# merge=ayoai-ledger and NO driver registered:
#     git config --get merge.ayoai-ledger.driver  ->  rc=1, empty
#     git check-attr merge -- ledger.jsonl        ->  "merge: ayoai-ledger"
# So a check-attr-only probe reports OK on precisely the broken clone. The
# driver is registered by install-git-hooks.sh into `.git/config`, which is
# per-clone and NOT version-controlled — a clone where that script was never
# run has correct-looking attributes pointing at nothing.
#
# WHAT ACTUALLY HAPPENS THEN, measured rather than assumed (the originating
# goal predicted silent corruption; it is worth being precise, because the real
# shape is both more likely and already instrumented):
#   - NON-overlapping edits merge cleanly and CORRECTLY without the driver.
#     rc=0, 12 records in, 12 records out, no duplicate ids.
#   - OVERLAPPING edits (the common ledger case: two boxes appending at the
#     tail) CONFLICT — rc=1 with conflict markers written into the .jsonl.
# A conflict is loud at the git layer but silent at the fleet layer: it is the
#  REPEATING-MERGE-CONFLICT WEDGE, where iteration-push.sh defers the
# integrate every cycle and the box goes permanently stale (measured there: a
# Body ran 85 commits behind, retrying an identical conflict, with a peer fix
# concealed behind the blocked merge). This check turns that recurring,
# hard-to-attribute wedge into one diagnostic line.
#
# GROUND TRUTH, not pattern-parsing: the driver names come from resolving every
# TRACKED path through git's own attribute machinery, so the set checked is
# exactly the set git will use. Cost measured on this clone: 27ms for 10,116
# tracked files.
#
# Exit 0 = clean (SILENT unless --json). Exit 1 = a referenced driver is not
# registered, or a registered driver is referenced by nothing.
# Exit 0 also on "cannot determine" — this is a DETECTOR, and a false alarm on
# a fresh clone or a non-git checkout is worse than a miss.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || {
    echo "check-merge-driver-registered: cannot source _paths.sh — skipping (exit 0)" >&2
    exit 0
}

JSON=0
# --repo is not decoration: iteration-push.sh takes its own --repo/
# ITERATION_PUSH_REPO and merges THAT tree, which need not be PROJECT_ROOT. A
# check that silently asserts against a different repo than the one being merged
# is the exact defect class this script exists to catch, so the caller passes the
# repo it is about to merge.
TARGET=""
while [ $# -gt 0 ]; do
    case "$1" in
        --json) JSON=1; shift ;;
        --repo) TARGET="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        *) shift ;;
    esac
done
[ -n "$TARGET" ] || TARGET="$PROJECT_ROOT"

cd "$TARGET" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# Values git resolves itself — these need no `merge.<name>.driver` entry.
# `unspecified`/`unset`/`set` are check-attr's own non-values.
is_builtin() {
    case " union text binary ours theirs unspecified unset set " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

# Resolve EVERY tracked path in one pass. check-attr --stdin -z emits NUL
# separated (path, attr, value) triples; `paste - - -` reassembles them.
resolved="$(git ls-files -z 2>/dev/null \
    | git check-attr --stdin -z merge 2>/dev/null \
    | tr '\0' '\n' | paste - - - 2>/dev/null)"

[ -n "$resolved" ] || exit 0   # empty/fresh clone — nothing resolvable, stay quiet

# name -> one representative path, and the count of paths resolving to it.
names="$(printf '%s\n' "$resolved" | awk -F'\t' '{print $3}' | sort -u)"

missing=""
orphan=""
checked=0
custom=0

for name in $names; do
    is_builtin "$name" && continue
    custom=$((custom + 1))
    rep="$(printf '%s\n' "$resolved" | awk -F'\t' -v n="$name" '$3==n{print $1; exit}')"
    cnt="$(printf '%s\n' "$resolved" | awk -F'\t' -v n="$name" '$3==n' | wc -l | tr -d ' ')"
    checked=$((checked + cnt))
    if ! git config --get "merge.$name.driver" >/dev/null 2>&1; then
        missing="${missing}${name}: ${cnt} tracked path(s) resolve to it (e.g. ${rep}) but merge.${name}.driver is NOT configured on this clone"$'\n'
    fi
done

# The inverse half: a driver registered in .git/config that NOTHING resolves to
# means the attributes side went missing (or the driver was renamed). Harmless
# to git, but it is the other way this pairing breaks, and the goal asks for
# both directions.
while IFS= read -r cfg; do
    [ -n "$cfg" ] || continue
    name="${cfg#merge.}"; name="${name%.driver}"
    if ! printf '%s\n' "$names" | grep -qx -- "$name"; then
        orphan="${orphan}${name}: merge.${name}.driver is configured but NO tracked path resolves to it — the .gitattributes mapping is missing or renamed"$'\n'
    fi
done <<EOF
$(git config --name-only --get-regexp '^merge\..*\.driver$' 2>/dev/null)
EOF

if [ "$custom" = "0" ] && [ -z "$orphan" ]; then
    # No custom drivers referenced anywhere — nothing this check can assert.
    [ "$JSON" = "1" ] && echo '{"status":"clean","custom_drivers":0,"checked":0}'
    exit 0
fi

if [ -z "$missing" ] && [ -z "$orphan" ]; then
    if [ "$JSON" = "1" ]; then
        echo "{\"status\":\"clean\",\"custom_drivers\":$custom,\"checked\":$checked}"
    fi
    # Silent on success by design — this runs before every integrate.
    exit 0
fi

if [ "$JSON" = "1" ]; then
    printf '{"status":"unregistered","custom_drivers":%s,"checked":%s,"detail":%s}\n' \
        "$custom" "$checked" \
        "$(printf '%s%s' "$missing" "$orphan" | tr '\n' ';' | sed 's/"/\\"/g; s/^/"/; s/$/"/')"
else
    echo "check-merge-driver-registered: MERGE DRIVER NOT USABLE ON THIS CLONE" >&2
    printf '%s' "$missing" >&2
    printf '%s' "$orphan" >&2
    echo "" >&2
    echo "CAUSE: the driver is registered per-clone by core/scripts/install-git-hooks.sh" >&2
    echo "       into .git/config, which is NOT version-controlled. A clone where that" >&2
    echo "       script never ran has correct .gitattributes pointing at nothing, and" >&2
    echo "       git check-attr reports the driver name anyway — so the sibling probe" >&2
    echo "       check-merge-driver-drift.sh reads OK on this exact clone." >&2
    echo "FIX:   bash core/scripts/install-git-hooks.sh" >&2
    echo "RISK:  git falls back to its default text merge on record stores. Measured:" >&2
    echo "       concurrent tail appends CONFLICT rather than resolving by record id," >&2
    echo "       which strands this box behind a merge that retrying can never clear" >&2
    echo "       (the g-306-315 repeating-conflict wedge)." >&2
fi
exit 1
