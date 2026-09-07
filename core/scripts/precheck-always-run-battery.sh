#!/usr/bin/env bash
# precheck-always-run-battery.sh — thin wrapper for precheck-always-run-battery.py
# (). One call running every STANDALONE always-run precheck lane under
# the budget meter and printing FINDINGS ONLY; see the .py docstring for the lane
# set, the meter-name-vs-script-name trap, and why a quiet run is not the same as
# a clean one (guard-4093).
#
# Fail-open by design: the battery must never block the loop, so any wrapper-level
# failure still exits 0 with a structured line (guard-614).
#
# Args pass straight through ("$@") — deliberately NO bash-side arg parsing, so
# there is no `shift 2` to get wrong (guard-1224) and exactly one parser owns the
# flag surface (the .py's argparse). Add flags THERE, never here.
#
# stdout is CAPTURED rather than streamed so the wrapper can COUNT it (,
# porting the proven pattern from the sibling iteration-open.sh). Until then this
# wrapper keyed its ONLY alarm on `rc != 0`, which structurally cannot see the one
# failure shape that was actually observed: the .py exiting ZERO having printed
# NOTHING. That case is byte-identical to a clean run in rc AND in stdout, so it
# reached the caller as an indistinguishable pass — guard-1091's exit-zero-and-
# empty shape, and the reason a wrapper whose honest-unknown branch keys only on
# rc does not cover it. The count is the second signal that makes it detectable.
#
# It PROVES something because _emit() in the .py is unconditional: every branch
# (findings, all-clean, NO-FINDINGS-REACHED) prints a footer, and main()'s
# fail-open handler routes even an unhandled exception back through _emit(). So
# there is no legitimate path to zero bytes in text mode, and zero bytes means the
# report was never emitted (killed mid-run, import-time death) — a malfunction,
# never a result (guard-3707, guard-602).
#
# NOT COVERED, and stated so the absence of the warning is never read as an
# all-clear: when the CALLER's bound expires (or the harness backgrounds the
# command at its 120s default) this wrapper is killed too, so every check below is
# downstream of the kill and none of them execute. That case is caller-side and
# already diagnosed — see iteration-open.sh's header and . On THIS class
# of box the run costs ~206s against ~25s on cc-04 (measured 2026-09-06), i.e. it
# does not fit inside a default bound at all.
#
# Fail-open is preserved: the wrapper still exits 0 on every path, because the
# battery must never block loop entry. The alarms are LOUD, not blocking.
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true

# MODE SNIFF, not arg parsing — it consumes nothing, shifts nothing, and leaves
# "$@" untouched for the .py (twin of iteration-open.sh's --dry-run sniff). Under
# --json the .py emits ONE JSON object on stdout, so an alarm printed there would
# corrupt it for a parsing consumer; in that mode the alarms go to stderr instead
# (guard-424: precheck/gate scripts fail LOUD on stderr, never silent).
_JSON=0
for _a in "$@"; do [ "$_a" = "--json" ] && _JSON=1; done

_say() { if [ "$_JSON" = "1" ]; then echo "$1" >&2; else echo "$1"; fi; }

_OUT="$(mktemp 2>/dev/null)" || _OUT=""
if [ -n "$_OUT" ]; then
    python3 "$_SELF/precheck-always-run-battery.py" "$@" > "$_OUT"
    _rc=$?
    cat "$_OUT"
    _bytes="$(wc -c < "$_OUT" 2>/dev/null || echo -1)"
    rm -f "$_OUT"
else
    # mktemp unavailable — run unchanged and DO NOT claim anything about the byte
    # count. -1 means "not measured", never "empty": a silence warning invented
    # from a failed measurement is the defect in the other direction (guard-1091).
    python3 "$_SELF/precheck-always-run-battery.py" "$@"
    _rc=$?
    _bytes=-1
fi

[ "$_rc" -ne 0 ] && _say '[always-run-battery] wrapper_failed — fall back to the per-phase lane calls (0.5b.1b, 0.5b.1c, 0.5b.2, 0.5b.2b, 0.5g.7)'
[ "$_bytes" = "0" ] && _say "[always-run-battery] SILENT RUN — ZERO bytes of output at rc=$_rc. This is NOT an all-clear: _emit() prints a footer on every branch, so no output means the report was never emitted and the always-run tier did not report. Treat this tier as BLIND and run the per-phase lane calls directly (0.5b.1b, 0.5b.1c, 0.5b.2, 0.5b.2b, 0.5g.7)."
exit 0
