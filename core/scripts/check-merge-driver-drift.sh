#!/usr/bin/env bash
# core/scripts/check-merge-driver-drift.sh — per-clone merge-driver drift detector.
#
# WHY THIS EXISTS (, 2026-07-26). `.git/info/attributes` is per-clone
# and UNTRACKED, and it OUTRANKS the tracked `.gitattributes`. So a stale local
# override silently reverts the  ayoai-ledger migration on ONE box
# while every other box looks correct, and git cannot propagate the fix — no
# commit, sync, or pull will ever repair it.
#
# Found live on cc-04: `.git/info/attributes` held
#     agents/alpha/*.jsonl merge=union
# dated 3 days BEFORE the ayoai-ledger driver landed — a stopgap nobody removed.
# It routed alpha's journal / experience / experience-archive / changelog /
# aspirations back to `union` while bravo and foxtrot correctly resolved
# ayoai-ledger.
#
# WHY THAT IS DANGEROUS, NOT COSMETIC: union merge "resurrects pruned/archived/
# edited records". On append-only ledgers that is duplicate lines. On
# aspirations.jsonl it means COMPLETED AND PRUNED GOALS COMING BACK on a
# cross-box merge — silent, and indistinguishable from real work.
#
# Exit 0 = clean. Exit 1 = drift found (prints the offending paths + the fix).
# Exit 0 also on "cannot determine" — this is a DETECTOR, and a false alarm on
# a fresh clone or a non-git checkout is worse than a miss.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || {
    echo "check-merge-driver-drift: cannot source _paths.sh — skipping (exit 0)" >&2
    exit 0
}

JSON=0
[ "${1:-}" = "--json" ] && JSON=1

cd "$PROJECT_ROOT" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# The RMW ledgers  routed to the record-aware driver. Line-append-safe
# ledgers (skill-invocations.jsonl) legitimately stay merge=union and are NOT
# checked here — flagging them would train the reader to ignore this probe.
LEDGERS="journal.jsonl experience.jsonl experience-archive.jsonl changelog.jsonl aspirations.jsonl"
EXPECTED="ayoai-ledger"

drift=""
checked=0
# Check EVERY agent dir on this box, not just the bound one: the override that
# motivated this was scoped to a single agent (`agents/alpha/*.jsonl`), so a
# bound-agent-only probe would have missed it from any other agent's session.
#
# ENUMERATE AGENT DIRS, NOT `*/local-paths.conf` (, fixed 2026-08-06).
# The conf glob is the enumerator this file's own intent forbids: on any given
# box only the RESIDENT agent has a local-paths.conf, so it silently degraded
# this probe to exactly the bound-agent-only scan the comment above rules out.
# Measured on cc-07 — 5 agent dirs, 1 conf, and every one of the other four
# carried 5 TRACKED ledger files resolving to ayoai-ledger: 5 of 25 paths
# checked, 20 invisible, reported as `OK`. The originating cc-04 override was
# scoped to `agents/alpha/*.jsonl` where alpha IS resident, which is the only
# reason it was ever caught; the identical override aimed at a non-resident
# agent would have been undetectable. CLAUDE.md already documents this exact
# enumerator as wrong for cross-agent sweeps (the owncloud-pull.sh row), and
# rb-5514 is the general form: enumerate the thing that actually varies, not a
# box-local artifact that happens to sit beside it.
for adir in "$(agents_root)"/*/; do
    [ -d "$adir" ] || continue
    agent="$(basename "$adir")"
    for led in $LEDGERS; do
        rel="agents/$agent/$led"
        # `git check-attr` is pattern-only and answers for paths that do not
        # exist, so without this guard a stray non-agent directory under
        # agents/ would inflate `checked` with paths git will never merge —
        # turning the count the goal relies on as an anti-vacuity signal into
        # a number that grows when coverage does NOT.
        [ -e "$PROJECT_ROOT/$rel" ] || continue
        got="$(git check-attr merge -- "$rel" 2>/dev/null | sed 's/.*: merge: //')"
        [ -z "$got" ] && continue
        checked=$((checked + 1))
        if [ "$got" != "$EXPECTED" ] && [ "$got" != "unspecified" ]; then
            drift="${drift}${rel}: merge=${got} (expected ${EXPECTED})"$'\n'
        fi
    done
done

if [ "$checked" = "0" ]; then
    exit 0   # nothing resolvable — fresh clone or unexpected layout; stay quiet
fi

if [ -z "$drift" ]; then
    if [ "$JSON" = "1" ]; then
        echo "{\"status\":\"clean\",\"checked\":$checked}"
    else
        echo "check-merge-driver-drift: OK — $checked ledger path(s) resolve to $EXPECTED"
    fi
    exit 0
fi

if [ "$JSON" = "1" ]; then
    printf '{"status":"drift","checked":%s,"detail":%s}\n' "$checked" \
        "$(printf '%s' "$drift" | tr '\n' ';' | sed 's/"/\\"/g; s/^/"/; s/$/"/')"
else
    echo "check-merge-driver-drift: DRIFT FOUND on this clone" >&2
    printf '%s' "$drift" >&2
    echo "" >&2
    echo "CAUSE: .git/info/attributes (per-clone, UNTRACKED) outranks the tracked" >&2
    echo "       .gitattributes. The tracked file being correct proves nothing." >&2
    echo "FIX:   inspect $(git rev-parse --git-dir)/info/attributes, archive it, then" >&2
    echo "       remove the offending merge=union line(s) for the paths above." >&2
    echo "RISK:  union merge resurrects pruned/archived/edited records — on" >&2
    echo "       aspirations.jsonl that is completed goals returning on a merge." >&2
fi
exit 1
