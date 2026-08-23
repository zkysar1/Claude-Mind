#!/usr/bin/env bash
# iteration-open.sh — thin wrapper for iteration-open.py (). The
# bash-driven loop ENTRY, twin of iteration-close.sh: entry checks + the
# always-run precheck battery under the budget meter, a per-stage rc table,
# FINDINGS ONLY, selection candidates, and a terminal NEXT ACTION imperative.
# See the .py docstring for the composition contract (it dispatches the existing
# batteries and owns NO lane registry of its own) and for why a quiet run is not
# the same as a clean one (guard-4093).
#
# Args pass straight through ("$@") — no bash-side arg PARSING, so there is no
# `shift 2` to get wrong (guard-1224) and exactly one parser owns the flag
# surface (the .py's argparse). Add flags THERE, never here.
#
# THE EXIT CODE IS MODE-DEPENDENT, AND THAT IS THE POINT:
#   run mode   — FAIL-OPEN, always exit 0. Loop entry must never be blocked; an
#                entry gate that can refuse entry is worse than the drift it
#                corrects. A wrapper-level failure still prints a structured
#                line (guard-614) naming the fallback.
#   --dry-run  — PRESERVE the rc. This mode is a VERIFICATION check (it asserts
#                the lane count matches the tier table), and a check that always
#                exits 0 can never fail — the exact defect found in ,
#                where a goal chartered to hunt proxy predicates was closing on a
#                predicate that proved nothing. Forcing 0 here would make the
#                unreadable-registry case indistinguishable from a clean parse.
# The loop below is a MODE SNIFF, not arg parsing: it consumes nothing, shifts
# nothing, and leaves "$@" untouched for the .py.
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true

_DRY=0
for _a in "$@"; do [ "$_a" = "--dry-run" ] && _DRY=1; done

# stdout is CAPTURED rather than streamed so the wrapper can COUNT it. A
# zero-byte run is the one failure this wrapper cannot otherwise see: _emit()
# prints the STAGE table unconditionally, so zero stdout PROVES the report was
# never emitted (killed mid-run, import-time death) — yet run mode forces exit 0,
# and rc=0 + silence is indistinguishable from "ran clean" to the caller, whose
# SKILL.md then disposes nothing and resumes. Measured on foxtrot
# (LAPTOP-3IOFCNEO, WSL2 6.18.33.2) 2026-08-21: --apply gave rc=0 / 0 bytes /
# ~370s while the standalone fallback returned two real findings minutes later.
# NOT reproducible on cc-07 (Linux 6.8.0-137-generic, worker Body): rc=0, 1988
# bytes, 65s. Root cause is NOT established, so this makes the failure LOUD
# instead of pretending to cure it (guard-4093 / guard-1715 — a quiet run is not
# a clean one). Capture costs no interactivity: this is a ~2 KB batch report and
# python block-buffers to a pipe regardless.
_OUT="$(mktemp 2>/dev/null)" || _OUT=""
if [ -n "$_OUT" ]; then
    python3 "$_SELF/iteration-open.py" "$@" > "$_OUT"
    _rc=$?
    cat "$_OUT"
    _bytes="$(wc -c < "$_OUT" 2>/dev/null || echo -1)"
    rm -f "$_OUT"
else
    # mktemp unavailable — run unchanged and DO NOT claim anything about the
    # byte count. -1 means "not measured", never "empty": a silence warning
    # invented from a failed measurement is the defect in the other direction.
    python3 "$_SELF/iteration-open.py" "$@"
    _rc=$?
    _bytes=-1
fi

if [ "$_DRY" = "1" ]; then exit "$_rc"; fi
[ "$_rc" -ne 0 ] && echo "[iteration-open] wrapper_failed — fall back to the batteries directly: orchestrator-entry-battery.sh, precheck-sentinel-battery.sh, precheck-always-run-battery.sh --apply, then goal-selector.sh"
[ "$_bytes" = "0" ] && echo "[iteration-open] SILENT RUN — ZERO bytes of output at rc=$_rc. This is NOT an all-clear: iteration-open.py always prints a STAGE table, so no output means the report was never emitted. Treat the always-run stage as BLIND and run the fallbacks directly: orchestrator-entry-battery.sh, precheck-sentinel-battery.sh, precheck-always-run-battery.sh --apply, then goal-selector.sh"
exit 0
