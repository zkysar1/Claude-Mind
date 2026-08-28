#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# wm-append — daemon-aware wrapper. Appends a JSON item to an array slot
# in working memory.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Read stdin body (JSON item to append)
#   3. POST /v1/wm/append?slot=<name>
#   4. On 200, print nothing (CLI printed nothing on success)
#
# Usage: echo '{"key":"value"}' | bash core/scripts/wm-append.sh <slot>
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
# EXACTLY ONE positional — the slot name. No flags, no second argument.
#
# : this was a bare catch-all (`case "$1" in *) SLOT="$1"; shift;;`)
# that ASSIGNED on every token, so the LAST one won and any token at all became
# the slot name. Both halves have to be refused, and only one of them is a flag:
#
#   `wm-append.sh spark_capture --json`        -> slot=--json. Caught downstream
#       since , but the wrapper discards the daemon's stderr, so the
#       operator sees rc=1 with EMPTY stdout and stderr (measured 2026-08-19,
#       alpha worker Body, hostname cc-07, uname -r 6.8.0-137-generic).
#   `wm-append.sh spark_capture exp_capture`   -> slot=exp_capture. BOTH names
#       are registered, so the unknown-lane refusal has no basis to object: the
#       entry lands in the WRONG lane at HTTP 200. Nothing downstream can catch
#       this one — it is the half a leading-dash-only fix misses.
#
# Refuse HERE rather than leaning on the daemon: this is the call site, the
# offending token can be named verbatim, and no round trip is spent. guard-4437
# is the general form — last-wins arg handling silently voids the earlier value
# and still exits 0.
SLOT=""
_seen=0
for arg in "$@"; do
    case "$arg" in
        -*)
            echo "Error: '$arg' starts with '-', so it is a command-line flag, not a slot name. wm-append.sh accepts no flags — pass the slot as the only argument. Usage: wm-append.sh <slot>" >&2
            exit 1;;
    esac
    if [ "$_seen" -ne 0 ]; then
        echo "Error: unexpected second argument '$arg' — the slot is already '$SLOT'. wm-append.sh takes exactly one positional, and silently preferring one over the other is the defect this refusal exists to prevent. Usage: wm-append.sh <slot>" >&2
        exit 1
    fi
    SLOT="$arg"
    _seen=1
done

if [ -z "$SLOT" ]; then
    echo "Error: slot name required. Usage: wm-append.sh <slot>" >&2
    exit 1
fi

# Read stdin (the JSON item) BEFORE invoking the daemon.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# : capture the response instead of discarding it. The daemon has
# reported `evicted` since , and this `> /dev/null` is why no operator
# ever saw it — that field had never once reached a caller in the whole time it
# existed. A fix is not shipped when the producer emits it; it is shipped when a
# consumer displays it (guard-742/547 one layer further out — daemon-vs-wrapper
# rather than CLI-vs-daemon).
#
# WHAT AN EVICTION MEANS HERE, post-: the newcomer is now protected, so
# the entry destroyed is always an OLD one. WHICH old one is NOT fixed, and this
# comment asserted the wrong half of it until . Victim selection became
# floor-aware in  (wm_write.py::append_slot):
#   flagged > (limit - _unflagged_floor)  -> the oldest FLAGGED entry goes
#   otherwise                             -> the oldest UNFLAGGED entry goes
# Those two victims have OPPOSITE recoverability. A flagged entry was mirrored to
# this Body's session/-rooted carrier at append time, which is uncapped and lives
# in the store, and capture_fast_lane delivers from there — so evicting it drops a
# redundant second copy. An unflagged entry was never mirrored anywhere: the WM
# slot is its only existence, so its eviction is the unrecoverable one.
# THE WRAPPER CANNOT TELL THEM APART. The daemon reports `evicted` as a bare
# COUNT, so this layer must not name a victim class it did not observe
# (guard-2947). Saying "the one that has waited longest for the reducer"
# unconditionally is false in the flagged branch, and it is not a harmless
# imprecision: a prior Body read that line, concluded that relaying through this
# lane would destroy another goal's undelivered observation, and routed around the
# lane entirely — when the append it declined to make would have evicted a
# carrier-backed peer. Measured cc-08 2026-08-22 on the live path: spark_capture at
# 50/50 with flagged=40 == limit-floor, one flagged append, unflagged held at 10.
# That is worth one stderr line.
# STDOUT stays silent on success (the documented contract above, and callers
# parse it); diagnostics go to STDERR, which is the channel a non-blocking
# notice belongs on.
rc=0
# RELAY THE DAEMON'S STDERR ON FAILURE (). This call used to end in
# `2>/dev/null`, which discarded the one thing a caller needs when the append is
# REFUSED. wm.py's _validate_knowledge_debt_entry rejects an unresolvable
# node_key with a 250-byte diagnostic naming the three valid forms; through this
# wrapper the caller saw rc=1 and ZERO bytes, so a correct, deliberate refusal
# was indistinguishable from an unexplained failure -- and the comment at the
# top of this file already recorded that the stderr was being discarded.
# guard-3662 exactly: `2>/dev/null` on a deliberately-hardened script re-buries
# the refusal that was built to save you. guard-114: wrappers invoking external
# commands must not suppress stderr.
# Relayed ONLY on non-zero rc, so the success path stays as quiet as before and
# the stdout contract (silent on success; callers parse it) is untouched.
_WM_ERR="$(mktemp 2>/dev/null || echo /tmp/wm-append-err.$$)"
RESP="$(rt_call POST /v1/wm/append \
    --query "slot=$(rt_url_encode "$SLOT")" \
    --body-string "$BODY" 2>"$_WM_ERR")" || rc=$?
if [ "$rc" != "0" ] && [ -s "$_WM_ERR" ]; then cat "$_WM_ERR" >&2; fi
rm -f "$_WM_ERR"

_emit_notice() {
    # : WHERE the write landed. wm-append routes some slots to the YAML
    # TOP LEVEL (TOP_LEVEL_KEYS in wm_write.py) and the rest under the `slots:`
    # mapping, and until now nothing at the call site said which. That is not a
    # cosmetic detail: a hand-rolled read of the wrong level returns a clean,
    # plausible 0 that is byte-identical to "this slot is empty", so the caller
    # gets no error to notice. Measured in ONE session, in BOTH directions
    # () — top-level `goals_completed_this_session` read as 0 under
    # `slots:`, and `spark_capture` read as 0 at the top level, each immediately
    # after a successful append. Placement is also consulted by the PRUNE path,
    # so it is not merely a cosmetic routing detail: wm.py's eviction loop
    # iterates the `slots:` mapping (`slots[slot_name] = None`), and
    # `_is_cadence_tracker` special-cases TOP_LEVEL_KEYS explicitly. The
    # eviction ASYMMETRY between a top-level key and a field nested inside a
    # slot is guard-1544's subject — read it there rather than inferring a
    # mechanism from this line; the two senses of "top-level" in play (YAML top
    # level vs slot-rather-than-nested-field) are easy to conflate, and an
    # earlier draft of this comment did exactly that.
    #
    # Printed on EVERY append, not just the surprising case: which case is
    # surprising depends on the reader's prior, and a notice that fires only when
    # someone already guessed right is the defect restated. Extract with
    # [^"]* rather than a greedy .* — the value is a short enum, and a greedy
    # match would swallow the rest of the JSON object.
    _p="$(printf '%s\n' "$RESP" \
        | sed -n 's/.*"placement"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
    # `if`/`case`, never an `&&` chain: this file runs under `set -e`, so a
    # trailing `&&` list whose final test fails returns non-zero from the
    # function and aborts the wrapper before its `exit 0` (the lesson already
    # learned one branch below). A null/absent placement prints nothing — the
    # resolver never ran, which is a third fact, not a placement.
    case "$_p" in
        top-level)
            echo "[wm-append] '$SLOT' lives at the YAML TOP LEVEL of working-memory.yaml, NOT under slots: — a hand-rolled read under slots: returns a clean, wrong 0. Use wm-read.sh, which resolves both." >&2
            ;;
        slots)
            echo "[wm-append] '$SLOT' lives under slots: in working-memory.yaml, NOT at the top level — a hand-rolled read of the top level returns a clean, wrong 0. Use wm-read.sh, which resolves both." >&2
            ;;
    esac
    # The captured group is a JSON string BODY, so its inner quotes are still
    # backslash-escaped; un-escape them or the operator reads `\"load_bearing\"`
    # in advice they are meant to copy.
    case "$RESP" in
        *'"warning"'*)
            printf '%s\n' "$RESP" \
                | sed -n 's/.*"warning"[[:space:]]*:[[:space:]]*"\(.*\)".*/[wm-append] \1/p' \
                | sed 's/\\"/"/g' \
                | head -1 >&2
            ;;
    esac
    # `evicted` is a plain int and carries no `warning`, so it needs its own
    # branch. EXTRACT then compare numerically. A `case` glob combining
    # [[:space:]] with an optional-zero prefix was tried first and matched
    # NEITHER `"evicted": 1` NOR `"evicted": 0` — i.e. it was silent always.
    # That defect is only visible if you positive-control the NON-ZERO shape
    # too (guard-2421): probing the zero shape alone returns "no match", which
    # is exactly what correct filtering looks like, so an always-quiet emitter
    # would have shipped looking right.
    _n="$(printf '%s\n' "$RESP" \
        | sed -n 's/.*"evicted"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)"
    # `if`, not an `&&` chain: this file runs under `set -e`, so a trailing
    # `&&` list whose final test fails returns non-zero from the function and
    # aborts the wrapper BEFORE its `exit 0` — turning a successful append into
    # a failed one on the quiet path, which is the common path.
    if [ -n "${_n:-}" ] && [ "$_n" != "0" ]; then
        _plural="entries"
        [ "$_n" = "1" ] && _plural="entry"
        echo "[wm-append] '$SLOT' is at its cap — $_n older $_plural evicted to make room. Victim selection is floor-aware (g-306-316) and this wrapper sees only the COUNT, not the class: if flagged entries exceed (cap - unflagged_floor) the oldest FLAGGED one goes, and that one is carrier-backed so it still reaches the reducer; otherwise the oldest UNFLAGGED one goes, and that IS an unrecoverable loss (the WM slot is its only copy). Do not route around this lane on the assumption that every eviction destroys undelivered mail — on a lane saturated with flagged entries it does not (g-306-353)." >&2
    fi
}

case $rc in
    0) _emit_notice; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            # Same stderr relay as the primary call site above ().
            _WM_ERR2="$(mktemp 2>/dev/null || echo /tmp/wm-append-err2.$$)"
            RESP="$(rt_call POST /v1/wm/append \
                --query "slot=$(rt_url_encode "$SLOT")" \
                --body-string "$BODY" 2>"$_WM_ERR2")" || rc=$?
            if [ "$rc" != "0" ] && [ -s "$_WM_ERR2" ]; then cat "$_WM_ERR2" >&2; fi
            rm -f "$_WM_ERR2"
            if [ "$rc" = "0" ]; then _emit_notice; exit 0; fi
        fi
        rt_no_daemon_error "wm-append.sh";;
    *) exit $rc;;
esac
