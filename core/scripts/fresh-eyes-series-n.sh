#!/usr/bin/env bash
# fresh-eyes-series-n.sh — the Phase 2.0(a) series-index probe of /fresh-eyes-review.
#
# Prints the highest series index N found in THIS agent's directive-lane series shard,
# read from the AUTHORITATIVE store copy. The next point is that value + 1. No arguments.
#
# WHY A SCRIPT AND NOT A LINE IN THE SKILL (, guard-5508): a skill body is
# rewritten with its invocation arguments before the model reads it. Under `--cadence`,
# the only automatic path, the awk positional parameter of the old inline probe became
# `match(--cadence, ...)`: valid awk, exit 0, no output — so branch 3 silently vanished
# and a table-only shard read 34 against a true 202. A script file is never rewritten.
# Edit the probe HERE; never inline it back into a SKILL.md
# (core/scripts/tests/test_fresh_eyes_series_n_probe.py pins both halves).
#
# THE THREE BRANCHES. Each is load-bearing and each filter has a measured reason in
# core/config/rationale/fresh-eyes-series-index-probe.md — read it BEFORE touching one:
#   1. heading lines carrying N=, minus "handoff to N=" forward references (any casing)
#   2. table rows whose FIRST cell is a bold N=k
#   3. table rows whose index sits in a LATER cell. A row's OWN index is the bold N=k that
#      OPENS a cell (`| **N=k`), leftmost in the row. The row's first N= anywhere is NOT its
#      index: a cell reading "carried from N=182" ahead of its own **N=183 returned 182, and
#      a value row reading "carry it to N=196" returned 196 (guard-5784). The match is on
#      the token, never a [^N]* prefix, so a capital-N word ahead of the index (NEW, NOT,
#      UNCHANGED) cannot drop the row ().
# A row whose own index is not bold contributes nothing: write your own index bold
# (`| **N=k`) and re-run this AFTER the write; it must return the N you wrote (guard-4614).
#
# FAIL LOUD, NEVER FALL BACK TO THE MIRROR: a failed authoritative read means N is
# unallocatable this pass (, guard-157). Exit 1, reason on stderr.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT="${MIND_AGENT:-}"
if [ -z "$AGENT" ]; then
    echo "FATAL: MIND_AGENT is unset — cannot name this agent's series shard, so N is UNALLOCATABLE this pass." >&2
    exit 1
fi

P="world/knowledge/tree/system/directive-lane-compliance/directive-lane-series-${AGENT}.md"
S="$(mktemp)"
ERR="$(mktemp)"
trap 'rm -f "$S" "$ERR"' EXIT

bash "$SCRIPT_DIR/backend-cat.sh" cat "$P" > "$S" 2> "$ERR" || {
    echo "FATAL: authoritative read of $P failed — N is UNALLOCATABLE this pass. Do NOT fall back to \$WORLD_PATH (g-115-8055). Reason: $(head -c 300 "$ERR" | tr '\n' ' ')" >&2
    exit 1
}
test -s "$S" || {
    echo "FATAL: authoritative read returned 0 bytes — refusing to allocate N from an empty file." >&2
    exit 1
}

{
    grep -E '^#{1,4} ' "$S" | grep -viE 'handoff to N=' | grep -oE 'N=[0-9]+'
    grep -oE '^\| \*\*N=[0-9]+' "$S"
    grep -viE 'handoff to N=' "$S" | grep -E '^\|' | awk 'match($0, /[|][ \t]*[*][*][ \t]*N=[0-9]+/) { print substr($0, RSTART, RLENGTH) }'
} | grep -oE '[0-9]+' | sort -n | tail -1
