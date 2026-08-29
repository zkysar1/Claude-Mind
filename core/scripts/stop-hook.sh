#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/stop-hook
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/stop-hook" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# Stop hook — keeps the autonomous loop alive
#
# Gates: allow stop when appropriate (SID mismatch, not RUNNING, stop-loop, pending agents)
# Otherwise: BLOCK unconditionally. No counter. No tiers. No safety valve.
# The user has /stop and Ctrl+C. The hook's job is to keep the loop alive.

# `set -e` deliberately OMITTED. Under -e, a transient append to `$LOG`
# failure (disk full, OneDrive contention, antivirus scan) kills the script
# with a non-zero exit. Claude Code treats non-zero exit as ALLOW → loop dies
# on the very next turn-end. The old comment at the historical line 28-29
# acknowledged this risk but kept -e anyway, betting that "if the filesystem
# is broken, blocking would be worse." That bet is wrong for the random-stops
# problem we are trying to solve: a transient OneDrive lock is NOT a broken
# filesystem, and the cost of one missed log entry is FAR lower than the cost
# of the loop dying. Match session-save-id.sh and recovery-gate.sh's
# convention: -u + pipefail with explicit per-command `|| true` on log
# appends. DO NOT REINTRODUCE `set -e` here — see 2026-05-12 hardening pass.
set -uo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
# Owning-PROCESS identity predicate, shared with runner-identity-check.sh rather
# than copied (). MUST be sourced AFTER _paths.sh: runner_proc_foreign_live
# calls agent_dir(), which _paths.sh defines (guard-1885). Function definitions
# only — sets no shell options, exports nothing, mutates no paths and calls no
# python, so it is safe to pull into a latency-critical hook. Consumed once, at
# Gate 0b.
# shellcheck source=_runner_proc.sh
source "$CORE_ROOT/scripts/_runner_proc.sh"
cd "$PROJECT_ROOT"

# --- Per-step timing instrumentation () ---
# Captures msec-resolution timestamps at each major step. On BLOCK path we emit
# a single JSONL record to core/logs/stop-hook-timing.jsonl. The investigation
# question is WHY 2.1.133 kills hooks at 8.4s when standalone runs are 2.5s —
# without per-step timings we can't localize which step ballooned. Cheap (5
# timestamp captures) and runs unconditionally, but only emits on BLOCK path
# (the slow case we're investigating). Fail-open: any timing-write failure must
# not block the BLOCK decision.
_T0=$(date +%s%3N)

# --- Audit log (persistent across sessions — diagnose why sessions die) ---
# NOTE: Under set -e, a failed >> append kills the script (= fail open, allows stop).
# This is acceptable — if the filesystem is broken, blocking would be worse.
# 2026-05-19 (plan v1 step 0.15-0.16): relocated from PROJECT_ROOT/ to
# core/logs/ canonical telemetry sink. `mkdir -p` makes the first-call write
# safe on a fresh deployment. Legacy paths still readable by analysis scripts
# during transition (see stop-hook-analyze.sh, runner-recent-block.sh).
mkdir -p "$PROJECT_ROOT/core/logs" 2>/dev/null || true
LOG="$PROJECT_ROOT/core/logs/stop-hook.log"
TIMING_LOG="$PROJECT_ROOT/core/logs/stop-hook-timing.jsonl"

# --- Log rotation (2026-05-19, plan v1 step 0.5) ---
# Cap LOG and TIMING_LOG at ~500 KB each by truncating to the last 1000 lines.
# Without rotation the LOG grew to 434 KB and would keep growing forever —
# consumers tail recent records, so older entries have zero practical value
# once a session has aged out. 1000 lines preserves multi-day BLOCK history
# for diagnosis. Truncation uses tail+mv (race-tolerant — only one stop hook
# fires per turn-end). Truncation failure is swallowed so it never blocks the
# BLOCK decision (the hook's primary job).
for _RFILE in "$LOG" "$TIMING_LOG"; do
    [ -f "$_RFILE" ] || continue
    _RSIZE=$(stat -c%s "$_RFILE" 2>/dev/null || echo 0)
    if [ "$_RSIZE" -gt 500000 ]; then
        _RTMP="$_RFILE.rot.$$"
        tail -n 1000 "$_RFILE" > "$_RTMP" 2>/dev/null && mv "$_RTMP" "$_RFILE" 2>/dev/null || rm -f "$_RTMP" 2>/dev/null || true
    fi
done
unset _RFILE _RSIZE _RTMP

# --- Read stdin ONCE (sole Stop hook — no stdin sharing, no race) ---
STDIN_JSON=$(cat)

# --- Resolve a working Python launcher ONCE (, rb-370/guard-335) ---
# Every $PY site below (SID extraction at line 75 first, then insight capture,
# body-manifest, trailing-text detector, decision payload, timing) used a BARE
# `py -3`. On any Linux host with NO `py` shim (foxtrot 2026-07-14: WSL/Ubuntu,
# no shim installed) that is command-not-found; the trailing `2>/dev/null ||
# echo ""` swallowed it, HOOK_SID came back empty, the hook logged
# `ALLOW gate=no-sid` and the stop hook — the loop's life support — silently
# became a no-op, so the loop died on its first text-death and STAYED dead with
# no alarm. Resolve py-3-then-python3 ONCE here (test-execute, so a
# present-but-broken `py` still falls through), then use "$PY" at every site.
# Matches the established idiom in bring-up-doctor.sh:34 / permissions-add.sh:48.
# The python3 fallback is what makes a fresh Linux deploy (WSL, new container,
# seed plant, transplant land) boot with a LIVE stop hook instead of a dead one.
# Placed here — before line 75, which fires on EVERY hook invocation ahead of
# any gate — so the resolver is never wasted on an early-exit ALLOW path.
PY=""
if command -v py >/dev/null 2>&1 && py -3 -c "pass" >/dev/null 2>&1; then PY="py -3"
elif command -v python3 >/dev/null 2>&1 && python3 -c "pass" >/dev/null 2>&1; then PY="python3"
fi

# --- Resolve agent for THIS session (not from shared files) ---
# .active-agent-$SID is the only per-session binding; written by /start.
# No shared-file fallback exists — that path was retired with the bridge file
# (rb-386). Use the per-session binding directly.
HOOK_SID=$(printf '%s' "$STDIN_JSON" | $PY -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

# Can't identify this session — don't risk blocking the wrong window
if [ -z "$HOOK_SID" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-sid" >> "$LOG" 2>/dev/null || true
    # LOUD degradation signal (). gate=no-sid = the SID could not be
    # extracted, so the stop hook (the loop's life support) is a NO-OP this fire
    # and CANNOT force the Skill(aspirations) re-entry — the loop dies silently
    # on its next text-only turn-end (foxtrot 2026-07-14 ran dead for HOURS this
    # way; the only trace was a log line nobody reads). Emit to STDERR so it
    # surfaces in the Claude Code pane immediately. STDERR, not a per-agent
    # session-signal file, because the agent is UNRESOLVABLE here — resolution
    # needs the very SID we just failed to extract. Does NOT change the
    # fail-open ALLOW (blocking the wrong window is the worse hazard — HARD
    # CONSTRAINT of ); it only makes the silent degradation loud.
    echo "[stop-hook] DEGRADED gate=no-sid: session_id could not be extracted from the Stop event — the stop hook is a NO-OP this fire and CANNOT keep the autonomous loop alive; the loop will die on its next text-only turn-end with no other alarm. Likely cause: no Python launcher (py/python3) resolvable in the hook env, or malformed Stop-event JSON. Fix the launcher / hook env (see g-115-2205, g-115-2204)." >&2
    exit 0
fi

HOOK_AGENT=""

# Phase 2.6 binding (preferred): agents/<name>/sessions/<SID>/binding.yaml.
# Added 2026-05-20 — previously this script only checked the legacy
# .active-agent-<SID> file at PROJECT_ROOT, which /start no longer writes
# after Phase 2.6. Every autonomous session post-Phase-2.6 was therefore
# unresolvable at stop-hook time → ALLOW gate=no-agent → loop dies on
# first turn-end (observed 2026-05-20, alpha/bravo/charlie/delta/zeta
# all stopping mid-iteration). The Phase 2.6 dir name IS the SID, so the
# agent name is just the parent path segment — pure-bash glob, no file
# read needed.
for _BF in "$PROJECT_ROOT/${AGENTS_PARENT_DIR}"/*/sessions/"$HOOK_SID"/binding.yaml; do
    [ -f "$_BF" ] || continue
    # Extract <name> from .../agents/<name>/sessions/<SID>/binding.yaml
    _BD="${_BF%/sessions/*}"
    HOOK_AGENT="${_BD##*/}"
    break
done

# Legacy fallback: .active-agent-<SID> at PROJECT_ROOT (pre-Phase-2.6).
# Still relevant during the migration window — sessions started before
# Phase 2.6 cutover have this file, not the Phase 2.6 binding.yaml.
if [ -z "$HOOK_AGENT" ] && [ -f "$PROJECT_ROOT/.active-agent-$HOOK_SID" ]; then
    HOOK_AGENT=$(cat "$PROJECT_ROOT/.active-agent-$HOOK_SID" 2>/dev/null | tr -d '\r\n')
fi

# Do NOT fall back to $AGENT_NAME from _paths.sh here. _paths.sh resolves
# via MIND_AGENT env, which may be unset at hook time. Historically a
# shared-file fallback caused cross-agent contamination (a terminal whose
# binding file was missing silently adopted another agent's identity and
# was told to run that agent's aspirations loop; rb-386). Strict-match via
# binding file OR reverse-lookup is the only safe path. If neither resolves,
# exit 0 with gate=no-agent — identical to the canonical resolver's
# contract in _resolve_agent_from_sid.py and idle-tick.sh:40.

# Reverse-lookup fallback + RESOLUTION_GAP defense in ONE pass.
# Single scan of agents/*/session/running-session-id files: (a) try to
# match THIS SID to identify the agent, (b) build _LIVE_SIDS list of
# OTHER running agents for the defense-layer diagnostic if (a) fails.
# Glob path uses $AGENTS_PARENT_DIR (exported by sourced _paths.sh) so
# a future rename of the agents-parent dir updates this glob via the
# single sync constant — same failure mode the 2026-05-20 incident
# demonstrated for hardcoded paths.
if [ -z "$HOOK_AGENT" ]; then
    _LIVE_SIDS=""
    for _RSF in "$PROJECT_ROOT/${AGENTS_PARENT_DIR}"/*/session/running-session-id; do
        [ -f "$_RSF" ] || continue
        _RSID=$(cat "$_RSF" 2>/dev/null | tr -d '\r\n')
        [ -n "$_RSID" ] || continue
        _RAGENT=$(basename "$(dirname "$(dirname "$_RSF")")")
        if [ "$_RSID" = "$HOOK_SID" ]; then
            HOOK_AGENT="$_RAGENT"
            # Self-heal: recreate the legacy binding so next time is O(1)
            # (Phase 2.6 binding.yaml is the canonical write path; the
            # legacy file is kept here only as a cheap second-tier cache.)
            echo "$HOOK_AGENT" > "$PROJECT_ROOT/.active-agent-$HOOK_SID"
            # Don't break — finish building _LIVE_SIDS for parity with
            # the defense diagnostic. Cost is bounded by agent count.
            # (Defense path doesn't fire when HOOK_AGENT is set, but
            # keeping the full scan symmetric means future readers don't
            # wonder why _LIVE_SIDS is partial on match.)
        fi
        _LIVE_SIDS="$_LIVE_SIDS $_RAGENT:$_RSID"
    done

    # No agent resolved through any tier — nothing to BLOCK for.
    # DEFENSE LAYER (added 2026-05-20 after Phase 2.5.D + 2.6 silent-loss
    # incident): if _LIVE_SIDS is non-empty, OTHER agents are running but
    # THIS SID couldn't be resolved — likely indicator the resolver tiers
    # are broken for a new layout. Log loudly so the next investigator
    # has the evidence within one hook fire instead of waiting for 100%
    # of agents to silently stop.
    if [ -z "$HOOK_AGENT" ]; then
        if [ -n "$_LIVE_SIDS" ]; then
            echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-agent-RESOLUTION_GAP sid=$HOOK_SID live_sids=$_LIVE_SIDS" >> "$LOG" 2>/dev/null || true
        else
            echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-agent sid=$HOOK_SID" >> "$LOG" 2>/dev/null || true
        fi
        unset _LIVE_SIDS _RSF _RSID _RAGENT
        exit 0
    fi
    unset _LIVE_SIDS _RSF _RSID _RAGENT
fi

HOOK_AGENT_DIR="$(agent_dir "$HOOK_AGENT")"

# --- Read runner-token (framework-owned UUID for SID-collision audit) ---
# 2026-05-12 hardening Tier 3a. Logged in every BLOCK / sid-mismatch ALLOW so
# a Claude Code SID collision shows up as "same SID, different runner_token"
# in core/logs/stop-hook.log. WITHOUT this signal, the 2026-05-12 zeta-bravo cross-
# binding was invisible until forensic analysis. Read-only; empty token logs
# as "runner_token=" (clean signal that the agent started before tokens were
# introduced, OR token-write failed at /start).
RUNNER_TOKEN_LOG=""
if [ -f "$HOOK_AGENT_DIR/session/runner-token" ]; then
    RUNNER_TOKEN_LOG=$(cat "$HOOK_AGENT_DIR/session/runner-token" 2>/dev/null | tr -d '\r\n')
fi

# --- Insight capture (non-critical — must not affect blocking decision) ---
printf '%s' "$STDIN_JSON" | MIND_AGENT="$HOOK_AGENT" $PY "$CORE_ROOT/scripts/capture-insights.py" 2>/dev/null || true
_T_AFTER_INSIGHTS=$(date +%s%3N)

# --- Housekeeping: stale session files + legacy artifacts ---
# DO NOT REMOVE the touch — refreshes own-binding mtime so observer modes
# (assistant/reader) which emit no other liveness signal aren't deleted by
# the helper below. See cleanup-stale-bindings.sh OBSERVER-MODE NOTE.
touch -c "$PROJECT_ROOT/.active-agent-$HOOK_SID" 2>/dev/null || true
bash "$CORE_ROOT/scripts/cleanup-stale-bindings.sh" 2>/dev/null || true
rm -f "$PROJECT_ROOT/.stop-hook-stdin.json"
_T_AFTER_HOUSEKEEPING=$(date +%s%3N)

# running-session-id is set by /start (autonomous mode) and kept in sync by
# session-save-id.sh (on compact). Read it up front; Gate 0 itself now runs
# AFTER the per-Body branch below.
RUNNER_FILE="$HOOK_AGENT_DIR/session/running-session-id"
RUNNER_SID=""
# Conditional read so a box with NO runner file still spawns no subprocess here
# (the dormant-case guarantee the per-Body block documents). `set -e` is NOT in
# effect (`set -uo pipefail`, L25) so the failing test is not fatal, and
# RUNNER_SID is pre-initialised because `set -u` IS.
[ -f "$RUNNER_FILE" ] && RUNNER_SID=$(cat "$RUNNER_FILE" 2>/dev/null | tr -d '\r\n' || echo "")

# --- Per-Body branch — HOISTED ABOVE Gate 0's early exit () ---
# WHY IT MOVED. The Phase 2B close producer and the worker resurrection net
# below were BOTH nested inside the sid-MISMATCH branch, reachable only when
# running-session-id EXISTS. A cross-box worker on a remote-reducer box
# deliberately has NO local running-session-id, so every worker turn-end hit
# Gate 0's `exit 0` first and got NEITHER. Measured (soak #2, 2026-08-05, SID
# 5c55002c): three consecutive worker turn-ends logged ALLOW gate=no-runner —
# two were mid-loop text-deaths this net exists to BLOCK (the worker survived
# only on ~600s deadman wakeups), and the third was a GENUINE close whose
# learning payload was stranded (manifest left body_state=active, zero staging).
# Soak #1 passed only because that box still carried ex-reducer residue — a
# stale local running-session-id — which made the mismatch branch reachable BY
# ACCIDENT; cleaning the residue (correct) removed the accident.
#
# This is rb-662(1) in bash: a SINGLE-SIGNAL trigger (the per-Body WM fork
# signature) was wired INSIDE an AGGREGATE gate (runner-file-exists AND
# sid-differs), which silently drops the single-signal-only path. The remedy it
# prescribes is exactly this — hoist above the aggregate gate and keep a
# signal-level guard (`[ -f "$_BODY_WM" ]`, unchanged below).
#
# The guard is the UNION of the two non-reducer cases and NOTHING more:
#   no runner file            -> cross-box worker      (the newly-covered case)
#   runner file, SID differs  -> today's sid-mismatch  (unchanged)
#   runner file, SID matches  -> the REDUCER: NOT taken. Its own perpetuity
#                                layers own that turn-end, and a worker-net
#                                BLOCK here would hand it the wrong re-entry
#                                skill (Skill(worker-loop) vs Skill(aspirations)).
#   runner file present but EMPTY -> NOT taken, exactly as before. The `-n`
#                                test is preserved DELIBERATELY: this fix adds
#                                the no-file case without changing the
#                                empty-file case it was not scoped to touch.
# Cost on a box that never forked a Body: one extra bash file test, zero py-3.
if [ ! -f "$RUNNER_FILE" ] || { [ -n "$RUNNER_SID" ] && [ "$HOOK_SID" != "$RUNNER_SID" ]; }; then
    # --- Phase 2B producer (, refines ): close a worker Body on a
    # GENUINE close ---
    # This session is NOT the runner (sid-mismatch). If it is a forked non-reducer
    # WORKER Body — i.e. it has a per-Body WM file (only non-reducer workers fork;
    # the reducer and observers never do) — AND it has written a `body-closing`
    # sentinel signalling its loop GENUINELY terminated (no more work / final
    # STOP), flip its manifest active->closed-pending-merge so the reducer's next
    # aspirations-consolidate Step -1 (body-merge generalize-down) merges its WM
    # back. The sentinel is the Phase-2 refinement of 's "first not-runner
    # turn-end" heuristic: a worker doing MULTIPLE work-units across turns must NOT
    # be queued for merge after turn 1 (that would lose turns 2+ of divergence —
    # the reducer merges + marks `merged`, then later turns diverge into a
    # now-merged manifest the sessions-pass never revisits). The bash pre-guard
    # ([ -f WM ] AND [ -f sentinel ]) keeps the dormant single-runner case at ZERO
    # py-3 calls (no sentinel ever exists there) and collapses the prior
    # read+set-state pair into ONE delegated, unit-tested call
    # (body-manifest.close_body_on_genuine — re-checks state, marks only an active
    # Body, and consumes the sentinel so a re-fire cannot re-mark). FAIL-OPEN:
    # never affects the (already-decided) ALLOW below. Design SSOT: tree node
    # mind-engine-identity-bridge.
    _BODY_WM="$HOOK_AGENT_DIR/sessions/$HOOK_SID/working-memory.yaml"
    _CLOSE_SENTINEL="$HOOK_AGENT_DIR/sessions/$HOOK_SID/body-closing"
    if [ -f "$_BODY_WM" ] && [ -f "$_CLOSE_SENTINEL" ]; then
        _CLOSE_RESULT=$($PY "$CORE_ROOT/scripts/body-manifest.py" close-body-on-genuine --sid "$HOOK_SID" --agent "$HOOK_AGENT" 2>/dev/null || echo "")
        echo "$(date +%Y-%m-%dT%H:%M:%S) BODY-CLOSE sid=$HOOK_SID agent=$HOOK_AGENT genuine-close result=$_CLOSE_RESULT" >> "$LOG" 2>/dev/null || true
        # -d: a worker claims through the SAME contract the reducer uses
        # (aspirations-claim.sh, worker-loop Phase 2), and that claim WRITES
        # team-state in_flight — but the worker loop STOPS at Phase 4 and never
        # runs iteration-close do_verify Step 3 (L629), the only place the normal
        # path clears it. recovery-gate.sh's clear (via session-manifest-clear.sh,
        # rb-671) fires only behind its 6-condition zombie AND-gate, which a
        # CLEANLY-closed worker never trips; stranded-claim-sweep enumerates
        # status=in-progress only, so a worker that COMPLETED its goal is invisible
        # to it. Gated on a GENUINE close (marked*) so a between-turns turn-end
        # never clears — and the helper itself refuses unless the goal's
        # claimed_by_sid matches THIS Body, because in_flight is agent-keyed with
        # no sid and an unconditional clear would blank a live REDUCER's row.
        # Runs only inside the existing worker-only guard, so the dormant
        # single-runner case still pays ZERO extra py-3 calls. FAIL-OPEN.
        case "$_CLOSE_RESULT" in
            marked|marked-push-failed)
                # stderr -> the log, NOT /dev/null ( defect B). The
                # helper catches broadly and prints {"verdict":"error"} on
                # stdout, so ordinary runtime failures are already visible; what
                # was being eaten is everything UPSTREAM of that handler — a bad
                # $PY resolution, an ImportError on _rt, a syntax error — which
                # writes only to stderr and left `result=` empty. On a hook path
                # nobody watches, a permanently-broken invocation then looks
                # exactly like a clean nothing-to-clear run
                # (verify-before-assuming Rule 4: a silenced command is ZERO
                # signals, not one).
                _IF_RESULT=$($PY "$CORE_ROOT/scripts/worker_close_in_flight_clear.py" --agent "$HOOK_AGENT" --sid "$HOOK_SID" 2>>"$LOG" || echo "")
                echo "$(date +%Y-%m-%dT%H:%M:%S) BODY-CLOSE-INFLIGHT sid=$HOOK_SID agent=$HOOK_AGENT result=$_IF_RESULT" >> "$LOG" 2>/dev/null || true
                unset _IF_RESULT
                ;;
        esac
        unset _CLOSE_RESULT
    elif [ -f "$_BODY_WM" ]; then
        # --- Worker-body resurrection net () ---
        # Chosen over the ScheduleWakeup analog; the rejected option and the
        # measurement behind the choice are recorded in the goal + rb.
        #
        # The reducer has THREE perpetuity layers (terminal Skill re-entry +
        # this hook's unconditional BLOCK + the deadman ScheduleWakeup). A
        # worker had exactly ONE: the Phase 5 Skill(worker-loop) re-entry added
        # 2026-08-03 (aa4b8de8a). A text-death between work units — the
        # rb-629/guard-454 class the reducer survives precisely BECAUSE of the
        # BLOCK below — silently ended an unattended worker, because Gate 0
        # ALLOWs every sid-mismatched turn-end. This branch is that missing net.
        #
        # WHY HERE AND NOT A NEW GATE: every discriminator is already computed
        # two lines up. `_BODY_WM` is the fork signature — only non-reducer
        # workers fork a per-Body working-memory.yaml; the reducer and observers
        # never do (see the Phase 2B comment above, which already relies on
        # exactly this). So the observer-trap risk the goal names is excluded
        # STRUCTURALLY, not heuristically. Verified on this box: the reducer
        # session has body-manifest.yaml and NO per-session working-memory.yaml.
        #
        # THE ELIF IS LOAD-BEARING: a GENUINE close (body-closing sentinel
        # present) takes the branch above and still reaches the ALLOW, so the
        # close path can never be trapped. `body-closing` is therefore also the
        # per-worker escape hatch — no new sentinel was invented for it.
        #
        # SAFETY VALVES (guard-1813 — a refusal is not side-effect-free):
        #   1. body-closing present  -> branch above, ALLOW (genuine close)
        #   2. stop-requested set    -> ALLOW (user asked the agent to stop)
        #   3. no per-Body WM        -> not a worker; reducer/observer untouched
        #   4. manifest body_state already closed -> ALLOW (the Body closed in
        #      a PRIOR turn; see the worker-net-body-closed branch below)
        # Cost when no worker has ever forked (measured: 0 on this box across
        # 572 hook decisions) is ONE bash file test — no py-3 call, matching the
        # dormant-case guarantee the Phase 2B block above already documents.
        # Direct file test, NOT session-signal-exists.sh: MIND_AGENT is not
        # exported until AFTER Gate 0 (see the export comment below), so the
        # wrapper would resolve the agent from an env this hook has not set yet
        # — the guard-1742 class, where a hand-run shell has the var and the
        # real caller does not. $HOOK_AGENT_DIR is already resolved here and
        # names the same file the wrapper reads, so the test is both cheaper
        # and correct. Same style as _BODY_WM / _CLOSE_SENTINEL above.
        if [ -f "$HOOK_AGENT_DIR/session/stop-requested" ]; then
            echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=worker-net-stop-requested sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG" 2>/dev/null || true
        elif grep -Eq "^body_state: '?parked'?[[:space:]]*$" \
                "$HOOK_AGENT_DIR/sessions/$HOOK_SID/body-manifest.yaml" 2>/dev/null; then
            # THIS IS THE ONE PARK VALVE ( reducer ruling, 2026-08-17,
            # zeta holding the runner claim on cc-02). A second valve keyed on a
            # `body-parked` FILE with a 70-minute freshness bound briefly existed
            # further down this chain — built concurrently for  by two
            # Bodies that could not see each other, git-auto-merged CLEANLY so
            # nothing announced the collision. Both emitted THIS gate name from
            # different predicates, so the log could not say which mechanism
            # parked a Body. The sentinel valve was removed, not left inert.
            #
            # WHY THE MANIFEST WON, measured rather than argued: (a) nothing ever
            # wrote the sentinel file — the valve was unreachable in production
            # from the day it landed (positive control: `body-closing`, the
            # sibling sentinel, has six producers); (b) its stated reason to
            # exist — "worker-loop Phase -0 refuses any state but 'active', so a
            # parked Body must stay 'active' or it can never resume" — was true
            # when written and is now FALSE: Phase -0 keys on the CLOSED SET, and
            # worker-loop/SKILL.md says so explicitly ("NOT merely 'not active',
            # since `parked` is non-active and resumable"); (c) the whole park
            # lifecycle is already written against the manifest — worker-loop
            # calls `body-manifest.py park` / `resume` / `park-expired`, and
            # `deadman-directive.sh` branches on `body_state: parked` as
            # RESUMABLE. The manifest is also externally legible to Phase -0, the
            # deadman prompt and the fleet sweeper, which is 's
            # constraint 2; a file only this hook reads is not.
            #
            # THE 70-MINUTE BOUND WAS DELIBERATELY NOT PORTED, and this is the
            # part worth carrying: the two bounds measure DIFFERENT CLOCKS, so
            # "port it" was never well-defined. The sentinel's mtime measured
            # TIME SINCE LAST POLL (a live park re-touched the file every cycle)
            # — a liveness clock. `parked_at` measures TOTAL PARK DURATION and is
            # deliberately preserved across re-parks (body-manifest.py L451), so
            # it is a patience cap. Copying 70 minutes onto the patience cap
            # would force-close a correctly-waiting Body after roughly ONE poll
            # cycle, because the park wakeup is armed at 3600s: a 70-minute cap
            # leaves ten minutes of margin. That converts every park into a
            # close, which is the exact outcome  exists to prevent.
            # Park already expires by AGE via `park-expired` (parked_at vs
            # PARK_MAX_HOURS=60.0), so the ruling's literal ask was already
            # satisfied; what was declined is SHORTENING it. The liveness clock
            # is a real second axis and is not free here — see rb/guard from
            #  and the follow-up Idea goal.
            #
            # 5th safety valve (): a PARKED Body's turn-end is
            # legitimate and must not be trapped. A park is what a worker does
            # when its reducer is gone — it holds an armed re-poll wakeup and
            # ends the turn deliberately, having written NO body-closing
            # sentinel (it intends to resume, so it must never be queued for
            # merge). Without this valve every one of those turn-ends hits the
            # BLOCK below, whose instruction is "write the body-closing sentinel
            # and end the turn" — which would durably CLOSE the Body and defeat
            # the entire point of parking. This was the open implementation risk
            # the goal named; the answer is that it lands on the BLOCK, so the
            # valve is required rather than optional.
            #
            # DELIBERATELY ITS OWN BRANCH, not `parked` bolted into the
            # closed-state alternation below. Both branches produce the same
            # ALLOW, so folding them would be invisible in behaviour — and that
            # is exactly the argument FOR splitting them: this log line is the
            # only durable record of WHY a worker turn-end was let go, and
            # "parked, coming back on its own" vs "closed, done" is the one
            # distinction the fleet sweeper and the user actually need
            # (constraint 2 of the goal). A merged branch would report every
            # parked Body as finished. Same fail-toward-protection property as
            # the closed valve: a missing or unreadable manifest matches
            # neither and falls through to the BLOCK.
            echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=worker-net-body-parked sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG" 2>/dev/null || true
            # ARM THE PARK RE-POLL FROM HERE (, Zak-Code ADR-0102). The
            # park turn is supposed to end on the Body's own
            # ScheduleWakeup(<park-resume prompt>, 3600) — and measured across 26
            # zc-03 sessions on 2026-08-29 the model armed a wake-up ONCE, so a
            # parked Body sat at its prompt exactly like a closed one. This hook
            # is the one process that positively knows the Body is parked, at
            # the last moment before the prompt, so it arms the re-poll itself:
            # a Zak-Code harness honours the `wakeup` key (replace-slot, clamped
            # to 3600s, persisted on the session); Claude Code ignores unknown
            # keys, so the line is harmless there and the Body's own arm stands.
            # Emitted INSTEAD of falling through to the gates below, which would
            # ALLOW anyway for a worker SID and could never be reached with a
            # second JSON document on stdout without turning this into a
            # fail-open parse. The prompt is a natural-language line, never a
            # slash command (schedule-wakeup-correctness.md).
            printf '%s\n' '{"wakeup": {"prompt": "Parked worker Body: re-enter /worker-loop at Phase -0 (the manifest reads parked = RESUMABLE), re-run the Phase 0.5 reducer poll and SELECT; a claim resumes this Body, no eligible goal re-parks it.", "delay_seconds": 3600}}'
            exit 0
        elif grep -Eq "^body_state: '?(closed-pending-merge|merged|closed-stale)'?[[:space:]]*$" \
                "$HOOK_AGENT_DIR/sessions/$HOOK_SID/body-manifest.yaml" 2>/dev/null; then
            # 4th safety valve (2026-08-09, cc-08 04:39->04:49): a GENUINELY-
            # CLOSED Body's later turn-ends must stand the net down. After a
            # genuine close the branch above CONSUMED the body-closing sentinel
            # and the fork WM SURVIVES the close by design, so this elif's own
            # discriminators read exactly like a between-units text-death —
            # while the DURABLE closure record, body-manifest.yaml body_state,
            # says closed. The deadman wakeup armed by the last work unit still
            # fires ~600s post-close, so this shape occurs after EVERY genuine
            # worker close; without this valve that firing was BLOCKed into a
            # pointless second sentinel ceremony (close-body-on-genuine returns
            # 'not-active'). Matched quoted or unquoted; a missing or
            # unreadable manifest falls through to the BLOCK, so the net fails
            # toward protection and only a positively-read closed state stands
            # it down.
            echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=worker-net-body-closed sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG" 2>/dev/null || true
            # A CLOSED Body needs no net: drop whatever wake-up the last work unit
            # armed so it does not fire a resurrection prompt into a finished
            # session (Zak-Code ADR-0102 `cancel`; Claude Code ignores the key,
            # where the resurrection prompt's own closed-set read is the guard).
            printf '%s\n' '{"wakeup": {"cancel": true}}'
            exit 0
        else
            echo "$(date +%Y-%m-%dT%H:%M:%S) BLOCK gate=worker-net sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG" 2>/dev/null || true
            unset _BODY_WM _CLOSE_SENTINEL
            printf '%s\n' '{"decision": "block", "reason": "Worker Body turn ended without a Skill(worker-loop) re-entry (a text summary or autocompact terminated the turn). Your FIRST action MUST be: Skill('"'"'worker-loop'"'"') — NOT Skill('"'"'aspirations'"'"'), which is the REDUCER-only re-entry (guard-517/guard-463). Do NOT emit a text summary first. If this Body genuinely has no more work, write the body-closing sentinel in your per-session dir and end the turn — that is the sanctioned close path and this net will stand down."}'
            exit 0
        fi
    fi
    unset _BODY_WM _CLOSE_SENTINEL
fi

# --- Gate 0: Session identity — only block the runner session ---
# Behaviour UNCHANGED; it simply now runs AFTER the per-Body branch above, so a
# worker on a no-runner box reaches its net / close producer first instead of
# exiting here. A GENUINE close still falls through to an ALLOW (the close path
# can never be trapped), and a box that never forked a Body arrives here having
# paid one extra bash file test and zero py-3 calls.
if [ ! -f "$RUNNER_FILE" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-runner sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG" 2>/dev/null || true
    exit 0
fi
if [ -n "$RUNNER_SID" ] && [ "$HOOK_SID" != "$RUNNER_SID" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=sid-mismatch sid=$HOOK_SID runner=$RUNNER_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0  # Different session — not the autonomous loop runner, allow stop
fi

# --- Gate 0b: same SID, DIFFERENT owning process () ---
# THE WEDGE THIS CLOSES. Gate 0 above ALLOWs a turn-end only on a SID MISMATCH.
#  taught runner-identity-check.sh to eject a duplicate instance that
# SHARES the runner's SID, using the owning-PROCESS identity ("<pid>:<starttime>"
# of the nearest `claude` ancestor) — the only signal that differs between two
# concurrent sessions. Nothing taught THIS hook the same thing, so for the
# ejected process HOOK_SID == RUNNER_SID and the block above never fires: the
# pid-based gate ejects it from the loop on every re-entry while the SID-based
# hook BLOCKs its every turn-end. It can neither iterate nor stop. Measured on
# zeta / cc-02 2026-08-22 over 3 consecutive turns; it does not self-resolve.
#
# THE ALLOW IS EXACTLY AS BROAD AS THE EJECT, AND NO BROADER. runner-identity-check
# ejects when (SID matches) AND (a stamped runner-proc names a DIFFERENT process)
# AND (that process is still live). The last two are runner_proc_foreign_live —
# the same function, sourced, not a second copy, because the wedge IS a
# disagreement between two predicates and a copy re-arms it one level down.
#
# THE SID-MATCH TEST IS RE-STATED HERE, NOT INHERITED FROM FALLING THROUGH THE
# BLOCK ABOVE. That fall-through also carries the RUNNER_SID-EMPTY case, where
# runner-identity-check FAIL-OPENS (`[ -n "$RUNNER_SID" ] || exit 0`) and ejects
# NOBODY. An allowance there would have no matching eject — a licence for the
# REAL runner to end its turn quietly, which is the one direction this hook must
# never widen in (guard-4315).
#
# IT COMPOSES WITH THE CROSS-BOX WORKER BRANCH ABOVE RATHER THAN SHADOWING IT —
# checked as a truth table over (runner-file, RUNNER_SID, HOOK_SID), not by
# reading. That branch is TAKEN on file-absent and on SID-differs; this one is
# evaluated ONLY on file-present + SID-matches, which is the single row that
# branch documents as deliberately NOT taken ("the REDUCER"). Zero overlap. What
# changes is that the row's "SID matches => this IS the reducer" assumption is
# the very one  falsified; Gate 0b handles the falsified subset and
# leaves the worker-net's re-entry contract untouched.
#
# FAIL-CLOSED TOWARD THE STATUS QUO: an unresolvable identity, an absent stamp, a
# dead stamped owner, or a stamp naming THIS process all return 1, leaving every
# pre-existing turn-end decision untouched. The allowance is added ONLY on
# positive evidence that some OTHER live process holds the role.
#
# COST, measured here (Linux 6.8, 10 reps): ~2ms/call on a box that has never
# stamped (one failed open, no /proc walk), ~12ms when the walk runs — against a
# hook whose latency budget is measured in seconds.
if [ -n "$RUNNER_SID" ] && [ "$HOOK_SID" = "$RUNNER_SID" ] && runner_proc_foreign_live "$HOOK_AGENT"; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=same-sid-not-owner sid=$HOOK_SID runner=$RUNNER_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0  # Duplicate instance ejected by runner-identity-check — let it stop
fi

# DO NOT use "$_A bash ..." — variable expansion is not recognized as an env
# assignment prefix by bash. Use export so all child processes inherit the agent.
export MIND_AGENT="$HOOK_AGENT"

# --- Gate 1: Not RUNNING → allow stop ---
STATE=$(bash "$CORE_ROOT/scripts/session-state-get.sh" 2>/dev/null || echo "UNINITIALIZED")
if [ "$STATE" != "RUNNING" ]; then
    # SG-c (-c): the runner session is ending. A graceful /stop sets
    # IDLE at D1 BEFORE stop-loop at D2, so THIS not-RUNNING gate — not Gate 2 —
    # is the allow-path that actually fires on a graceful stop (and it also
    # covers a crash/recovery-to-IDLE end). Before allowing the stop, roll any
    # unresolved deploy obligations into handoff.yaml so they are SURFACED in the
    # next session's boot summary instead of silently crossing the stop boundary.
    # Roll-then-ALLOW backstop: NEVER blocks the stop (an un-clearable framework-CI
    # obligation must not wedge a session), idempotent (dedup by repo+sha), and
    # fail-open. pending-deploys.yaml is NOT cleared — it lives in the agent-wide
    # session dir and persists for the next session's SG-b all-sweep (the source
    # of truth); handoff carries the visibility mirror. Also covers an
    # autocompact-interrupted graceful-stop D-flow.
    if [ -n "$HOOK_AGENT" ] && [ -n "$PY" ]; then
        _PDROLL=$($PY "$CORE_ROOT/scripts/pending-deploys.py" --agent "$HOOK_AGENT" roll-handoff 2>/dev/null || echo '')
        [ -n "$_PDROLL" ] && echo "$(date +%Y-%m-%dT%H:%M:%S) pending-deploys-roll agent=$HOOK_AGENT sid=$HOOK_SID $_PDROLL" >> "$LOG" 2>/dev/null || true
    fi
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=not-running sid=$HOOK_SID agent=$HOOK_AGENT state=$STATE runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0
fi

# --- Gate 2: stop-loop signal (set by /stop) → allow stop + cleanup ---
if bash "$CORE_ROOT/scripts/session-signal-exists.sh" stop-loop 2>/dev/null; then
    bash "$CORE_ROOT/scripts/session-signal-clear.sh" stop-loop
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=stop-loop sid=$HOOK_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0
fi

# --- Gate 2.5: Pending background agents → allow stop ---
# --body-sid scopes the check to THIS body (). The store is agent-wide
# (session/ singular), so without it a sibling WORKER body's dispatched agent
# ALLOWs the REDUCER's turn-end and removes its text-death net. HOOK_SID (parsed
# from the hook payload above) is the authoritative body id here — MIND_SID is
# NOT set in the hook environment (it is injected only into Bash tool calls), so
# the flag must be passed explicitly rather than read from env.
if bash "$CORE_ROOT/scripts/pending-agents.sh" has-pending --body-sid "$HOOK_SID" 2>/dev/null; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=pending-agents sid=$HOOK_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0
fi

# --- Gate 2.6: Pending long-running background jobs → allow stop ---
# pending-agents.sh (Gate 2.5) tracks short-lived Claude sub-agents (~10-min).
# background-jobs.sh tracks long-running OS processes (hours: SSH tunnels,
# Processor runs, etc.). They are SEPARATE data stores with SEPARATE schemas;
# checking only one was the asymmetry that let stop-hook BLOCK during a live
# long-running job while recovery-gate's Condition 4 (Path A) checked the
# correct one. Mirror recovery-gate so both surfaces agree.
# Fail-open via 2>/dev/null + exit-1-fallthrough: any script error treated as
# "no pending jobs" (BLOCK proceeds) — never the wrong direction for liveness.
# --body-sid: same reasoning as Gate 2.5 above (). An older
# background-jobs.py that does not know the flag exits 2 on the unknown
# argument, which this fail-open shape reads as "no pending jobs" — so a
# partial deploy in either order lands on BLOCK-proceeds, never on a wrong ALLOW.
if bash "$CORE_ROOT/scripts/background-jobs.sh" has-pending --body-sid "$HOOK_SID" 2>/dev/null; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=background-jobs sid=$HOOK_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0
fi
_T_AFTER_GATES=$(date +%s%3N)

# --- BLOCK: Agent is RUNNING, no stop signal — keep the loop alive ---
# WHY this file: Autocompact changes the session ID. session-save-id.sh (the
# SessionStart hook) needs to know which agent just compacted so it can update
# running-session-id with the new SID. This file contains the OLD SID — if it
# matches running-session-id, session-save-id.sh knows this agent just compacted.
# Lives in the agent's session dir (not project root) to avoid multi-agent races.
echo "$HOOK_SID" > "$HOOK_AGENT_DIR/session/compact-pending"
echo "$(date +%Y-%m-%dT%H:%M:%S) BLOCK sid=$HOOK_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true

# --- Trailing-text detection (Layer-C, ) — fail-open observability ---
# Run detector on the Stop-hook event to identify trailing-prose patterns that
# would have killed the loop if the hook had not fired (rb-629, guard-454).
# On severity=high, enrich BLOCK message with a marker diagnostic AND append
# a record to loop-death-detections.jsonl for post-hoc analysis. Detector
# errors must not break Stop hook (fail-open via 2>/dev/null + || true).
TTD_MARKER=""
TTD_SEVERITY=""
TTD_EVIDENCE=""
TTD_DETECTOR="$WORLD_DIR/scripts/trailing-text-detector.py"
if [ -f "$TTD_DETECTOR" ]; then
    # Hard timeout (, sq-011 forward-prediction). A hanging detector
    # (slow disk read on transcript_path, infinite loop on malformed JSONL,
    # network mount latency) would hang the Stop hook itself, so the agent's
    # Stop event never resolves and the loop dies a different way. SIGTERM at
    # 2s; SIGKILL 1s later if SIGTERM didn't take. timeout exit=124 → empty
    # TTD_RESULT → fail-open (no marker emitted, BLOCK still fires correctly).
    TTD_RESULT=$(printf '%s' "$STDIN_JSON" | timeout --kill-after=1 2 $PY "$TTD_DETECTOR" --stop-hook 2>/dev/null || true)
    if [ -n "$TTD_RESULT" ]; then
        TTD_MARKER=$(printf '%s' "$TTD_RESULT" | $PY -c "import sys,json;d=json.loads(sys.stdin.read() or '{}');print(d.get('marker') or '')" 2>/dev/null || echo "")
        TTD_SEVERITY=$(printf '%s' "$TTD_RESULT" | $PY -c "import sys,json;d=json.loads(sys.stdin.read() or '{}');print(d.get('severity') or '')" 2>/dev/null || echo "")
        TTD_EVIDENCE=$(printf '%s' "$TTD_RESULT" | $PY -c "import sys,json;d=json.loads(sys.stdin.read() or '{}');print(d.get('evidence') or '')" 2>/dev/null || echo "")
    fi
fi

# Append to detection log on high severity (fail-open)
# Cross-agent concurrency: alpha and bravo stop-hooks can fire simultaneously
# (e.g., parallel autocompact). Bare open(a) interleaves records and corrupts
# the line-delimited format. Route through _fileops.locked_append_jsonl which
# acquires .lock + snapshots history + atomic-writes. Fix per alpha F-001
# finding (msg-20260504-223622-alpha-727,  cross-agent fresh-eyes).
if [ "$TTD_SEVERITY" = "high" ] && [ -n "$TTD_MARKER" ]; then
    TTD_NOW="$(date +%Y-%m-%dT%H:%M:%S)"
    TTD_MARKER="$TTD_MARKER" TTD_SEVERITY="$TTD_SEVERITY" TTD_EVIDENCE="$TTD_EVIDENCE" \
    TTD_NOW="$TTD_NOW" TTD_AGENT="$HOOK_AGENT" TTD_SID="$HOOK_SID" \
    TTD_LOG="$WORLD_DIR/loop-death-detections.jsonl" \
    TTD_CORE_SCRIPTS="$PROJECT_ROOT/core/scripts" \
    $PY -c "
import json, os, sys
sys.path.insert(0, os.environ['TTD_CORE_SCRIPTS'])
from _fileops import locked_append_jsonl
rec = {
    'timestamp': os.environ['TTD_NOW'],
    'agent': os.environ['TTD_AGENT'],
    'session_id': os.environ['TTD_SID'],
    'marker': os.environ['TTD_MARKER'],
    'evidence_snippet': os.environ.get('TTD_EVIDENCE', ''),
    'severity': os.environ['TTD_SEVERITY'],
}
locked_append_jsonl(os.environ['TTD_LOG'], rec)
" 2>/dev/null || true
fi
_T_AFTER_TTD=$(date +%s%3N)

# Context-aware BLOCK payload (session 58 alpha-stopping follow-up). Stop
# fires for two distinct reasons: (1) autocompact end-of-turn, (2) a text
# summary terminated the turn without Skill(aspirations). Both need the same
# re-entry instruction, but the checkpoint snapshot helps the LLM reconcile
# phase_completed without re-reading the file.
#
# CRITICAL — DO NOT add per-phase hint strings here (tried and reverted
# 2026-04-24). phase_completed only takes three values: verify, state_update,
# learning_gate — written by _checkpoint_refresh in iteration-close.sh at
# lines 202/446/720. Anything else (productivity, selected, unknown) is not
# produced by this codebase. Per-phase messaging (a) implied coverage of
# non-existent states and (b) added cognitive load for future readers. The
# uniform "call Skill(aspirations); here is the checkpoint" form is enough —
# the LLM resolves phase semantics from the checkpoint + Phase -1 logic.
#
# Python generates the full JSON so string content from the checkpoint (goal
# IDs, phase values) cannot corrupt the decision payload via shell escaping.
HOOK_AGENT_DIR="$HOOK_AGENT_DIR" HOOK_AGENT="$HOOK_AGENT" \
TTD_MARKER="$TTD_MARKER" TTD_SEVERITY="$TTD_SEVERITY" \
$PY - <<'PYEOF'
import json, os, pathlib

agent_dir = pathlib.Path(os.environ["HOOK_AGENT_DIR"])
agent     = os.environ["HOOK_AGENT"]

# Checkpoint context — just the raw goal_id + phase_completed. Let the LLM
# interpret; do not add per-phase narrative. Fail-open on corrupt checkpoint.
cp_context = ""
cp_path = agent_dir / "session" / "iteration-checkpoint.json"
if cp_path.exists():
    try:
        cp = json.loads(cp_path.read_text(encoding="utf-8"))
        gid = cp.get("goal_id", "")
        pc  = cp.get("phase_completed", "") or "unknown"
        if gid:
            cp_context = f" Last checkpoint: goal={gid} phase_completed={pc}."
    except Exception:
        pass

# Compact-checkpoint note — pre-existing behavior preserved.
compact_msg = ""
if (agent_dir / "session" / "compact-checkpoint.yaml").exists():
    compact_msg = " Encoding checkpoint saved -- Phase -0.5c will process it on re-entry."

# Trailing-text detector diagnostic (, rb-629, guard-454, rb-670, guard-462).
# Layer-C observability — surface the specific anti-pattern marker the
# detector flagged in the assistant turn that triggered this Stop. Marker
# names live in world/scripts/trailing-text-detector.py:
# user_directive_rationalization (Shape α′, 2026-05-01 — see guard-462),
# insight_block, phase_summary, next_step_narration, trailing_prose
# (the original four — see guard-454). Only emitted on
# severity=high so low/medium signals stay out of the BLOCK message.
ttd_msg = ""
ttd_marker = os.environ.get("TTD_MARKER", "")
ttd_severity = os.environ.get("TTD_SEVERITY", "")
if ttd_severity == "high" and ttd_marker:
    ttd_msg = f" TRAILING-TEXT PATTERN: {ttd_marker} -- see guard-454."

reason = (
    "Turn ended without a Skill(aspirations) re-entry (autocompact OR a text "
    "summary terminated the turn). Your FIRST action MUST be: Skill('aspirations') "
    "with args='loop'. Do NOT manually select goals. Do NOT run Bash commands "
    "first. Call the Skill tool IMMEDIATELY."
    + cp_context
    + f" Agent: {agent}. Prefix all Bash with MIND_AGENT={agent}."
    + compact_msg
    + ttd_msg
)

print(json.dumps({"decision": "block", "reason": reason}))
PYEOF

# --- Emit timing record (, fail-open) ---
# One JSONL record per BLOCK invocation. Step deltas localize the slow step
# under Claude Code 2.1.133 (8.4s observed vs 2.5s on standalone runs). The
# decision payload above has already been printed to stdout via PYEOF, so
# the hook's decision is committed before this fires. Failure here is
# silent (2>/dev/null + || true) and cannot affect the BLOCK decision.
_T_END=$(date +%s%3N)
T0="$_T0" T_AI="${_T_AFTER_INSIGHTS:-0}" T_AH="${_T_AFTER_HOUSEKEEPING:-0}" \
T_AG="${_T_AFTER_GATES:-0}" T_AT="${_T_AFTER_TTD:-0}" T_END="$_T_END" \
TIMING_LOG="$TIMING_LOG" HOOK_AGENT="$HOOK_AGENT" HOOK_SID="$HOOK_SID" \
TTD_SEVERITY="${TTD_SEVERITY:-}" TTD_MARKER="${TTD_MARKER:-}" \
$PY -c "
import json, os, datetime
def _i(k):
    try: return int(os.environ.get(k,'0') or 0)
    except: return 0
T0=_i('T0'); T_AI=_i('T_AI'); T_AH=_i('T_AH'); T_AG=_i('T_AG')
T_AT=_i('T_AT'); T_END=_i('T_END')
rec = {
    'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
    'agent': os.environ.get('HOOK_AGENT',''),
    'session_id': os.environ.get('HOOK_SID',''),
    'decision': 'block',
    'ttd_severity': (os.environ.get('TTD_SEVERITY','') or None),
    'ttd_marker': (os.environ.get('TTD_MARKER','') or None),
    'capture_insights_ms': (T_AI - T0) if T_AI else None,
    'housekeeping_ms': (T_AH - T_AI) if (T_AH and T_AI) else None,
    'gates_ms': (T_AG - T_AH) if (T_AG and T_AH) else None,
    'ttd_ms': (T_AT - T_AG) if (T_AT and T_AG) else None,
    'decision_payload_ms': (T_END - T_AT) if T_AT else None,
    'total_ms': T_END - T0,
}
with open(os.environ['TIMING_LOG'], 'a', encoding='utf-8') as f:
    f.write(json.dumps(rec) + '\n')
" 2>/dev/null || true

exit 0
