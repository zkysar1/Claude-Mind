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
#                ONE EXCEPTION, exit 4: a stage was dispatched and never returned
#                (). That refuses nothing -- no caller branches on it to
#                stop -- it is what keeps a half-run from reading as rc=0 success.
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
_JSON=0
for _a in "$@"; do
    [ "$_a" = "--dry-run" ] && _DRY=1
    # : --json emits ONE machine-parsed object on stdout, so the
    # human-facing report-path line below must not be appended to it. The
    # pre-existing wrapper_failed / SILENT RUN echoes have the same hazard but
    # fire only on failure; the report-path line fires on EVERY success, so
    # without this it would corrupt every --json run.
    [ "$_a" = "--json" ] && _JSON=1
done

# ⛔ ROOT CAUSE ESTABLISHED 2026-08-23 — READ THIS BEFORE THE NARRATIVE BELOW.
# The narrative that follows says "Root cause is NOT established". That was true
# when written and is NOT true now; it is kept because the CAPTURE it justifies is
# still correct and still earns its keep. The rc=0/zero-byte signature on
# LAPTOP-3IOFCNEO is CALLER-SIDE, not a defect in this wrapper or in
# iteration-open.py: at the Bash-tool default 120s bound the HARNESS BACKGROUNDS
# the command and the caller captures rc=0 with 0 bytes on BOTH streams. Measured
# in  (completed 2026-08-23, bravo, carrying this box's 2026-08-21
# measurement), same command at three bounds: 120s -> backgrounded, rc=0/0 bytes;
# 170s -> rc=124, 0 bytes; 480s -> rc=0, 2,669,416 bytes. The box is SLOW, not
# broken — goal-selector select ran 176s here against 42.3s on cc-03 (4.2x), which
# is exactly why cc-07/cc-03 never reproduce it: 42s fits inside every default
# bound and 176s fits inside none.
#
# THREE CONSEQUENCES, all counter-intuitive enough to be worth stating:
#   1. It is TRANSIENT because it is a DURATION race against a fixed bound, not
#      because anything is flaky. Re-running "bare" appears to fix it; what
#      actually changed is the runtime.
#   2. It reproduces in OTHER wrappers calling OTHER .py files
#      (precheck-always-run-battery.sh, pending-deploys-gate.sh ,
#      scar-tissue-check ) precisely BECAUSE it is not about this .py.
#      Do not file it per-wrapper; that population is already 4+ goals deep.
#   3. THE SILENT-RUN WARNING BELOW CANNOT FIRE IN THIS CASE. When the harness
#      backgrounds the process, the wrapper never reaches its own byte check —
#      so the absence of that warning is NOT evidence the run was fine. Measured
#      on this box 2026-08-25: rc=0, zero bytes, no warning emitted.
# REMEDY when you see it: raise the caller's timeout (480s clears it here), or
# run the standalone fallbacks. Do NOT re-diagnose the wrapper.
#
# --- historical narrative, retained for the capture rationale ---
# stdout is CAPTURED rather than streamed so the wrapper can COUNT it. A
# zero-byte run is the one failure this wrapper cannot otherwise see: _emit()
# prints the STAGE table unconditionally, so zero stdout PROVES the report was
# never emitted (killed mid-run, import-time death) — yet run mode forces exit 0,
# and rc=0 + silence is indistinguishable from "ran clean" to the caller, whose
# SKILL.md then disposes nothing and resumes. Measured on foxtrot
# (LAPTOP-3IOFCNEO, WSL2 6.18.33.2) 2026-08-21: --apply gave rc=0 / 0 bytes /
# ~370s while the standalone fallback returned two real findings minutes later.
# NOT reproducible on cc-07 (Linux 6.8.0-137-generic, worker Body): rc=0, 1988
# bytes, 65s. [SUPERSEDED — root cause established 2026-08-23, ; see the
# header above. The cc-07 non-reproduction is EXPLAINED by that box being ~4x
# faster, not by a box-specific defect.] The capture still makes a genuine
# mid-run death LOUD instead of pretending to cure it (guard-4093 / guard-1715 — a quiet run is not
# a clean one). Capture costs no interactivity: this is a ~2 KB batch report and
# python block-buffers to a pipe regardless.
# : the .py writes the stage in flight to this file and clears it when
# the stage returns (_mark_in_flight). Read back only AFTER the process exits, so
# it reports what the program did, not what a caller's capture happened to keep.
# Empty path (mktemp unavailable) = the check is not armed, and nothing is claimed.
_MARK="$(mktemp 2>/dev/null)" || _MARK=""
export ITERATION_OPEN_STAGE_MARKER="$_MARK"
_OUT="$(mktemp 2>/dev/null)" || _OUT=""
# : WHERE THE REPORT SURVIVES. The capture below already lands
# outside the synced tree (mktemp -> /tmp), but it was deleted at the end of the
# run, so the only copy an agent ever read was the one flowing through the
# CALLER's redirect. When that redirect points into agents/<agent>/temp/, world/
# or meta/ on an own-cloud box, the sync layer swaps the inode mid-run and the
# TAIL is lost -- COVERAGE, FINDINGS, SELECTION and the terminal imperative are
# all printed last (guard-3789 / guard-4045 / guard-4200). Measured with a
# one-variable control on two boxes: cc-04 cut at 40-50s, cc-03 at 54s, and the
# /tmp twin of the same command completed both times. Keeping a copy at a stable
# non-synced path makes the report readable whatever the caller redirected to.
# MIND_AGENT is injected into every Bash call by the PreToolUse hook, so the
# fallback here is a filename of last resort, not a semantic default (guard-3970).
# ITERATION_OPEN_REPORT_PATH overrides the destination. It exists so a TEST can
# point this somewhere private: the default path is keyed only by agent name, so
# a test run under a live agent's name would otherwise clobber that agent's real
# report -- a test that destroys the artifact it is checking.
_REPORT="${ITERATION_OPEN_REPORT_PATH:-/tmp/iteration-open-report-${MIND_AGENT:-unknown}.log}"
_SAVED=0
if [ -n "$_OUT" ]; then
    python3 "$_SELF/iteration-open.py" "$@" > "$_OUT"
    _rc=$?
    cat "$_OUT"
    _bytes="$(wc -c < "$_OUT" 2>/dev/null || echo -1)"
    # Copy BEFORE the delete, and from _OUT rather than from the caller's
    # stream: _OUT is what the program actually wrote, upstream of any
    # truncation the caller's redirect may suffer.
    #  defect 2: --dry-run's lane table is NOT "the report". Writing it
    # here silently REPLACED a report a preceding --apply had written, at the same
    # path, with nothing on stdout saying so — the dry-run branch returns below
    # before both the announcement and the completeness check. --dry-run is the
    # natural next command after a confusing --apply, which put the clobber
    # directly on the recovery path this persisted copy exists to serve.
    #  defect 3: write aside and rename. _REPORT is keyed by agent name
    # only, and this file's own header documents the overlap case: at the 120s Bash
    # bound the harness BACKGROUNDS the run and the stated remedy is to re-run at a
    # higher bound, i.e. two same-agent runs by design. Measured: 8 trials of two
    # concurrent `cp -f` of two distinguishable 2,040,000 B sources to one
    # destination produced 1 INTERLEAVED file. The temp sits in the SAME directory
    # as _REPORT whatever ITERATION_OPEN_REPORT_PATH points at, so the rename stays
    # within one filesystem and is atomic. A failed copy leaves no ".$$" residue.
    if [ "$_DRY" != "1" ]; then
        cp -f "$_OUT" "$_REPORT.$$" 2>/dev/null \
            && mv -f "$_REPORT.$$" "$_REPORT" 2>/dev/null && _SAVED=1
        [ "$_SAVED" = "1" ] || rm -f "$_REPORT.$$" 2>/dev/null
    fi
    rm -f "$_OUT"
else
    # mktemp unavailable — run unchanged and DO NOT claim anything about the
    # byte count. -1 means "not measured", never "empty": a silence warning
    # invented from a failed measurement is the defect in the other direction.
    python3 "$_SELF/iteration-open.py" "$@"
    _rc=$?
    _bytes=-1
fi
_unfinished=""
if [ -n "$_MARK" ]; then
    _unfinished="$(cat "$_MARK" 2>/dev/null)"
    rm -f "$_MARK"
fi

if [ "$_DRY" = "1" ]; then
    # . --dry-run PRESERVES the rc because it is a VERIFICATION check,
    # and a check that always exits 0 can never fail (, see header).
    # THE BYTE COUNT IS PART OF THAT VERDICT, and until now it was not consulted
    # here at all: this branch returned _rc before ever reaching the byte check
    # below, so a zero-byte dry-run reported success. dry_run() prints its lane
    # table (text) or its JSON object on EVERY success path, and its one early
    # return prints to stderr and returns 1 — so ZERO bytes at rc=0 is not a
    # clean parse, it is a check that produced NO VERDICT AT ALL, and calling
    # that a pass is the same defect in a new place.
    # Exit 3 is deliberate and distinct: 0 clean, 1 tier table unreadable (the
    # .py's own hard error), 2 argparse usage, 3 no verdict — so a caller can
    # tell them apart instead of matching a bare non-zero (guard-2066).
    # _bytes=-1 means NOT MEASURED (mktemp unavailable) and must never raise this
    # alarm — an alarm invented from a failed measurement is the defect in the
    # other direction (guard-1091). Only a literal 0 qualifies.
    if [ "$_rc" = "0" ] && [ "$_bytes" = "0" ]; then
        echo "[iteration-open] DRY-RUN PRODUCED NO VERDICT — ZERO bytes of output at rc=0. dry_run() prints its lane table on every success path, so this is a check that never ran, not a clean parse. Exiting 3." >&2
        exit 3
    fi
    exit "$_rc"
fi
[ "$_rc" -ne 0 ] && echo "[iteration-open] wrapper_failed — fall back to the batteries directly: orchestrator-entry-battery.sh, precheck-sentinel-battery.sh, precheck-always-run-battery.sh --apply, then goal-selector.sh"
[ "$_bytes" = "0" ] && echo "[iteration-open] SILENT RUN — ZERO bytes of output at rc=$_rc. This is NOT an all-clear: iteration-open.py always prints a STAGE table, so no output means the report was never emitted. Treat the always-run stage as BLIND and run the fallbacks directly: orchestrator-entry-battery.sh, precheck-sentinel-battery.sh, precheck-always-run-battery.sh --apply, then goal-selector.sh"
# : WIDEN THE COMPLETENESS CHECK PAST ZERO BYTES. The guard above
# fires only at exactly 0, so a report that stopped PART-WAY sails through: a
# non-empty prefix at rc=0 is byte-indistinguishable from a short clean run.
# iteration-open.py ends every non-dry-run report with the NEXT ACTION
# imperative (pinned by test_terminal_line_is_the_next_action_imperative), so
# its ABSENCE from what the program actually wrote means the report was cut off.
#
# SCOPE, stated so the next reader does not over-trust this line: it inspects
# _REPORT, a copy of what the PROGRAM wrote. It therefore CANNOT detect the
# guard-3789 truncation of the CALLER's redirect -- that happens downstream of
# this process, after it has exited, and nothing inside this script can observe
# it. The _REPORT copy above REMOVES that exposure rather than detecting it,
# which is why outcome 3 is answered by a narrower check plus this note, not by
# a predicate that would quietly claim coverage it does not have.
# _JSON is excluded because --json emits a JSON OBJECT, which by design carries
# no terminal imperative -- without this the check would fire on every single
# --json run. A detector whose false-positive rate on a whole mode is 100% gets
# ignored, taking its true positives with it.
if [ "$_SAVED" = "1" ] && [ "$_JSON" = "0" ] \
   && [ "$_bytes" != "0" ] && [ "$_bytes" != "-1" ] \
   && ! grep -q 'NEXT ACTION REQUIRED' "$_REPORT" 2>/dev/null; then
    echo "[iteration-open] REPORT INCOMPLETE — $_bytes bytes at rc=$_rc, but the terminal 'NEXT ACTION REQUIRED' imperative is ABSENT, so iteration-open.py stopped before finishing its report. This is NOT an all-clear: the FINDINGS list above is PARTIAL. Read precheck-budget-state.json and skip every lane recording decision=executed before re-running anything — a stage that dies mid-run can leave lanes that already applied (guard-6634)."
fi
# Announce only a report with CONTENT, and never into --json. `cp` of an empty
# file succeeds, so a bare _SAVED test pointed the reader at a zero-byte copy
# immediately after SILENT RUN had correctly told them there was nothing to
# read -- a pointer to an empty artifact is worse than no pointer, because it
# reads as a recovery path.
if [ "$_SAVED" = "1" ] && [ "$_JSON" = "0" ] && [ "${_bytes:-0}" -gt 0 ] 2>/dev/null; then
    echo "[iteration-open] full report also saved to $_REPORT — read THAT copy if your capture looks short: it is written outside the synced tree, so it survives a caller redirect the sync layer truncates (guard-3789/guard-4045, g-115-10295)."
fi
if [ -n "$_unfinished" ]; then
    echo "[iteration-open] STAGE UNFINISHED -- '$_unfinished' was dispatched and never returned (python rc=$_rc). '$_unfinished' and every stage after it are BLIND: this run is PARTIAL, not clean. Run the fallbacks from that stage on, skipping any lane precheck-budget-state.json already records as executed -- a stage that dies mid-run can leave lanes that already applied (guard-6634). Exiting 4."
    exit 4
fi
exit 0
