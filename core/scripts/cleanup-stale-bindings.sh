#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# cleanup-stale-bindings.sh — Single writer for stale .active-agent-* cleanup.
#
# Called from stop-hook.sh (every turn end) and session-save-id.sh (every
# SessionStart). Both surfaces previously held an inline copy of the same
# 3-signal predicate — extracted here to eliminate drift risk between them.
#
# DELETE PREDICATE (ALL must hold; any single signal "live" skips):
#   0. The agent owns a local-paths.conf on THIS box (i.e. this box runs it).
#      Both signals 2 and 3 read machine_local files that are absent BY DESIGN
#      for a non-resident agent, so for one they are unanswerable, not "dead"
#      (). Refuse rather than read absence as evidence.
#   1. File mtime > 24h old (gentle TTL — recent files are obviously live)
#   2. EITHER running-session-id absent OR its content != THIS binding's SID
#      (autonomous mode writes running-session-id; observer modes don't)
#   3. runner-heartbeat is stale per heartbeat-stale.sh (canonical liveness
#      gate; only autonomous mode ticks the heartbeat)
#
# WHY all three: the old single-signal predicate {mtime>24h && no
# running-session-id-file} deleted zeta's binding on 2026-05-12T08:47 while
# zeta was actively running (running-session-id momentarily absent during a
# graceful-stop write window). That deletion opened the door for the bravo
# `claude --continue` collision at 09:56 — bravo's /start saw no binding at
# the inherited SID and silently took it. SID-content match + heartbeat
# freshness are independent signals; requiring BOTH eliminates the
# spurious-delete class.
#
# DO NOT WEAKEN BACK to a single-signal check.
# DO NOT INLINE BACK into stop-hook.sh or session-save-id.sh — drift between
# the two copies was the bug class this extraction prevents.
#
# OBSERVER-MODE NOTE: assistant/reader sessions write neither
# running-session-id (signal 2) nor a runner heartbeat (signal 3), so for
# them signal 1 (mtime>24h) is effectively the sole signal. The CALLER is
# responsible for refreshing mtime on its own binding before invoking this
# script — see stop-hook.sh's `touch -c` immediately above its invocation.
# Without that touch, a 24h+ observer session would delete its own binding.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Phase 2.5.C: sync with _paths.sh AGENTS_PARENT_DIR
_APD="agents"
# Phase 2.6: sync with _paths.sh SESSIONS_DIRNAME
_SDN="sessions"
_agents_root() { if [ -n "$_APD" ]; then printf '%s/%s' "$PROJECT_ROOT" "$_APD"; else printf '%s' "$PROJECT_ROOT"; fi; }
_agent_dir() { if [ -n "$_APD" ]; then printf '%s/%s/%s' "$PROJECT_ROOT" "$_APD" "$1"; else printf '%s/%s' "$PROJECT_ROOT" "$1"; fi; }

# Residency marker set (). Returns 0 when ANY marker says "this box
# runs $1". Gating on ONE filename let a supported migration revoke residency
# fleet-wide: migrate-to-mind-data.sh renames agents/*/local-paths.conf -> .bak
# for EVERY agent as its default final step (--no-backup opts out), declaring
# the self-contained .mind-data end-state needs no conf. Where that ran, no
# agent satisfied Signal 0, both sweep loops reaped nothing, and the script
# still exited 0 printing nothing.
#
# The rename is not the only route, and this is the wider finding (measured
# 2026-08-04, hostname cc-04, uname -r 6.8.0-136-generic): this box has ZERO
# .bak files yet bravo — resident, and in_flight at measurement time — carries
# no conf AT ALL, because /start under .mind-data never writes one. So the
# population is every agent started on self-contained storage, not just
# migrated ones. Same run, the opposite error: dbg-attr-agent has a conf and
# no session markers, i.e. conf alone also OVER-reports residency.
#
# Every marker here is sync_tier: machine_local in core/config/session-manifest.yaml
# (verified: agent-state, agent-mode; local-paths.conf is gitignored and
# classifies machine_local), so none can materialize from the authoritative
# store and fake residency — the property the old single-marker check relied on
# is preserved, not traded away. agent-state and agent-mode are additionally
# written by /start in EVERY mode, so like the conf they are not
# observer-mode-blind the way signals 2/3 are (guard-530).
#
# Widening this gate cannot over-sweep on its own: Signal 0 only decides
# whether the liveness question is ANSWERABLE. Signals 1/2/3 are untouched and
# remain the actual delete predicate.
_has_residency_marker() {
    local _RA="$1" _AD
    _AD="$(_agent_dir "$_RA")"
    [ -f "$_AD/local-paths.conf" ] && return 0
    [ -f "$_AD/session/agent-state" ] && return 0
    [ -f "$_AD/session/agent-mode" ] && return 0
    return 1
}

# Shared predicate: should we delete the binding for (agent=$1, sid=$2)?
# Returns 0 (yes-delete) when all three signals fail; non-zero (keep) otherwise.
# The mtime test is OUTSIDE because mtime source differs per layout.
_should_delete_binding() {
    local _BA="$1"
    local _BIND_SID="$2"
    # Signal 0 — RESIDENCY (). Both signals below read machine_local
    # files under agents/$_BA/session/ (running-session-id, runner-heartbeat).
    # Those never sync, so for an agent this box does NOT RUN they are absent
    # BY DESIGN — and both absences advance toward DELETE. Neither absence is
    # evidence; both are UNANSWERABLE, so refuse (guard-2418 class).
    #
    # The marker SET is _has_residency_marker () — see its header for
    # why one filename was not enough and why every accepted marker is
    # machine_local. Measured 2026-08-03 on cc-03: 4 local-paths.conf keys exist
    # in the authoritative store (all from one 2026-06-06T10:3x legacy bulk push
    # that predates the exclusion enforcement) yet only 1 is on disk here — the
    # pull side filters the same _EXCLUDE_DIRS/_is_machine_local set, so a
    # foreign conf cannot materialize and fake residency. That argument is what
    # the whole set must satisfy, and does.
    #
    # Fails CLOSED, matching check-upstream.sh:170-178 (missing foreign
    # agent-state => UNKNOWN => refuse) and the risk asymmetry: over-sweeping
    # rm -rf's a live session dir plus its co-located checkpoint/scratch,
    # under-sweeping leaves a stale dir for 's lane to reap.
    _has_residency_marker "$_BA" || return 1
    # Signal 2: running-session-id absent OR doesn't match the bound SID
    local _CUR_SID
    _CUR_SID=$(cat "$(_agent_dir "$_BA")/session/running-session-id" 2>/dev/null | tr -d '\r\n')
    [ "$_CUR_SID" = "$_BIND_SID" ] && return 1
    # Signal 3: runner-heartbeat is stale
    local _HB
    _HB=$(MIND_AGENT=$_BA bash "$SCRIPT_DIR/heartbeat-stale.sh" 2>/dev/null || echo fresh)
    [ "$_HB" = "fresh" ] && return 1
    return 0
}

# : before reaping a stale Body dir, preserve its forked working-memory
# to staging so the reducer's generalize-down (body-merge.py) can still reclaim
# it (the Body closed/crashed before its WM was consolidated). Only a NON-reducer
# worker Body forks a WM file; a reducer/observer never does -> nothing to stage.
# A Body whose WM was ALREADY merged (manifest body_state == merged) is skipped
# to avoid a double-merge on the next generalize-down. Staged files:
# session/pending-body-merges/<unitKey>-wm.yaml (the WM),
# session/pending-body-merges/<unitKey>-wm.hash (: the forked_wm_hash
# from the manifest), and session/pending-body-merges/<unitKey>-wm-baseline.yaml
# (-c: the fork-time WM snapshot); body-merge._consume_staged reads the
# hash to (a) no-op a never-diverged orphan instead of merging it as if
# divergent, and (b) combined with its already-merged set, close the
# cleanup/generalize-down double-merge window -- and reads the BASELINE to do a
# true 3-way delta so a counter the worker never touched is not summed in twice.
# Without this baseline copy the reducer's 3-way branch has no producer and is
# dead code, so the two MUST ship together. body-merge.py consumes+deletes all
# three. The -wm.hash / -wm-baseline.yaml suffixes mirror body-merge.py
# _STAGED_HASH_SUFFIX / _STAGED_BASELINE_SUFFIX (bash/python boundary -- kept in
# sync by hand). -wm-baseline.yaml is deliberately NOT matched by body-merge's
# `*-wm.yaml` drain glob, so it can never be drained as if it were a staged WM.
# IRREDUCIBLY LOCAL: body_state + forked_wm_hash read via grep + bash
# param-expansion, no python3.
_preserve_unmerged_body_wm() {
    local _BA="$1" _SD="$2" _SID="$3"
    local _WMF="$_SD/working-memory.yaml"
    [ -f "$_WMF" ] || return 0  # reducer/observer never forked a WM -> nothing to preserve
    local _STATE="" _FHASH=""
    if [ -f "$_SD/body-manifest.yaml" ]; then
        _STATE=$(grep -m1 'body_state:' "$_SD/body-manifest.yaml" 2>/dev/null || true)
        _STATE="${_STATE#*:}"
        _STATE="${_STATE//[[:space:]]/}"
        _STATE="${_STATE//\"/}"
        _STATE="${_STATE//\'/}"
        _FHASH=$(grep -m1 'forked_wm_hash:' "$_SD/body-manifest.yaml" 2>/dev/null || true)
        _FHASH="${_FHASH#*:}"
        _FHASH="${_FHASH//[[:space:]]/}"
        _FHASH="${_FHASH//\"/}"
        _FHASH="${_FHASH//\'/}"
    fi
    [ "$_STATE" = "merged" ] && return 0  # already consolidated -> no double-merge
    local _STAGE_DIR
    _STAGE_DIR="$(_agent_dir "$_BA")/session/pending-body-merges"
    mkdir -p "$_STAGE_DIR" 2>/dev/null || return 0
    # ORDER IS LOAD-BEARING: SIDECARS FIRST, TRIGGER LAST (-c fresh-eyes).
    # body-merge._consume_staged globs `*-wm.yaml` -- so the WM file is the
    # CONSUMER'S TRIGGER, and every sidecar must already exist when it appears.
    # Writing the WM first opens a window where a concurrent generalize_down sees
    # the WM without its sidecars and SILENTLY takes a degraded path: no baseline
    # -> 2-way union+SUM (the counter double-count this goal exists to remove); no
    # hash -> guard 2's no-op short-circuit is skipped and a never-diverged orphan
    # merges as if divergent. Both degraded paths are LEGITIMATE for a genuinely
    # sidecar-less staging, so nothing distinguishes "never staged" from "not
    # copied yet" -- no error, no log, wrong number. Writing the trigger LAST makes
    # the WM's presence imply its sidecars' presence.
    #
    # Stage the fork-time BASELINE when the Body wrote one (-b). It is the
    # 3-way-delta common ancestor; without it the reducer falls back to 2-way
    # union+SUM, which double-counts counters. Same contract as
    # close_body_on_genuine -- the '-wm-baseline.yaml' suffix deliberately does NOT
    # match body-merge's '*-wm.yaml' glob, so it cannot be mis-consumed as a Body
    # WM. Absent is NORMAL and must stay silent: a pre- fork, or a
    # crash-preserve that never captured one, has no baseline and is exactly the
    # case body-merge's retained 2-way fallback exists to serve.
    if [ -f "$_SD/forked-wm-baseline.yaml" ]; then
        cp "$_SD/forked-wm-baseline.yaml" \
           "$_STAGE_DIR/${_SID}-wm-baseline.yaml" 2>/dev/null || true
    fi
    # Stage the forked_wm_hash only when it is a real value. A reducer/observer's
    # null hash never reaches here (no WM file above); a worker fork always sets it.
    if [ -n "$_FHASH" ] && [ "$_FHASH" != "null" ]; then
        printf '%s' "$_FHASH" > "$_STAGE_DIR/${_SID}-wm.hash" 2>/dev/null || true
    fi
    # TRIGGER LAST -- see the ordering note above. Best-effort like the sidecars:
    # a Body dir being reaped must never fail the sweep.
    cp "$_WMF" "$_STAGE_DIR/${_SID}-wm.yaml" 2>/dev/null || true
    # EXPLICIT push, AFTER the trigger (-b FIX 2, re-ordered by the
    # -c merge so the push carries a COMPLETE staging set rather than a
    # sidecar-only one). Staging alone strands the WM on a box holding no DDB
    # claim -- H4a makes _owned_agents() return the empty set, so the periodic
    # sweep AND owncloud-flush both push zero agent dirs. This is the one place
    # this IRREDUCIBLY LOCAL function spends a python3 subprocess, and only on the
    # rare path: the `[ -f "$_WMF" ]` guard above already returned for every Body
    # that never forked a WM, so the common sweep is unaffected. stderr is
    # deliberately NOT silenced -- its diagnostic is the only signal that a Body's
    # WM failed to reach the reducer.
    py -3 "$PROJECT_ROOT/core/scripts/body-manifest.py" push-staged \
        --sid "$_SID" --agent "$_BA" >/dev/null || \
        echo "[cleanup-stale-bindings] WARN: staged Body WM for ${_SID} was NOT pushed; it is on local disk only" >&2
}

# Legacy sweep: .active-agent-<SID> at PROJECT_ROOT (pre-Phase-2.6).
for _AF in "$PROJECT_ROOT"/.active-agent-*; do
    [ -f "$_AF" ] || continue
    [ -z "$(find "$_AF" -maxdepth 0 -mmin +1440 2>/dev/null)" ] && continue
    _BA=$(cat "$_AF" 2>/dev/null | tr -d '\r\n')
    [ -n "$_BA" ] || { rm -f "$_AF"; continue; }
    _BIND_SID="${_AF##*/.active-agent-}"
    if _should_delete_binding "$_BA" "$_BIND_SID"; then
        rm -f "$_AF"
    fi
done

# Phase 2.6 sweep: agents/<name>/sessions/<sid>/binding.yaml.
# Same 3-signal predicate. On delete, removes the entire per-session dir
# so co-located scratch / iteration-checkpoint / watchdog-prev-state stale
# crumbs go with it (they were the per-session-dir purpose — none survives
# a stale-binding sweep).
for _ASR in "$(_agents_root)"/*; do
    [ -d "$_ASR" ] || continue
    _BA="${_ASR##*/}"
    [ -d "$_ASR/$_SDN" ] || continue
    for _SD in "$_ASR/$_SDN"/*; do
        [ -d "$_SD" ] || continue
        _BFILE="$_SD/binding.yaml"
        # ADMISSION, not predicate (). This check decides which dirs
        # REACH the 3-signal predicate below; it is not itself a liveness signal.
        # Keep that distinction — the header's "DO NOT WEAKEN BACK to a
        # single-signal check" governs signals 0/1/2/3, all of which still run
        # unchanged on everything admitted here.
        #
        # WAS: `[ -f "$_BFILE" ] || continue` — a bare existence test. A
        # per-session dir whose binding.yaml is GONE but whose other artifacts
        # remain was skipped on EVERY sweep, permanently, because it never got
        # as far as the predicate that would have reaped it. Measured on cc-04
        # 2026-08-06: agents/bravo/sessions/aae8287f-… survived 29 days (702h)
        # holding only body-manifest.yaml + session-summary.yaml, with all three
        # reap signals firing the whole time.
        #
        # THE HARM IS NOT DISK. The surviving body-manifest.yaml reads
        # role=reducer, body_state=active — so during a fleet fresh-eyes sweep an
        # orphan presents as a stale ACTIVE REDUCER body for an agent that does
        # not live on that box, indistinguishable at a glance from the real
        # 3-reducer incident of 2026-08-05. Orphaned manifests manufacture false
        # anomalies in exactly the audits that matter most, and ruling one out
        # costs real triage time. (Nothing globs manifests for ROLE decisions —
        # role is derived locally from the forked per-session WM, guard-2445 —
        # so the cost is misleading humans and audits, not misrouting the loop.)
        #
        # WHY session-summary.yaml IS THE ADMISSION TICKET, and why it is a
        # STRONGER signal than the predicate it admits to: every writer of that
        # file writes it at session CLOSE and nowhere else — graceful-stop D6.5
        # via session-summary-write.py, and the zak-code SessionEnd hook
        # (zakcode-session-end-hook.sh, "Mode 1 Step 2.5, always"). Two
        # independent writers, both terminal. binding.yaml is written at ENTRY;
        # a summary therefore cannot precede a binding in a live session, so
        # "summary present AND binding absent" is proof the Body ran to a clean
        # exit and something later removed its binding. That is a CLOSURE proof,
        # which the 3-signal predicate only ever infers.
        #
        # The mid-creation race stays covered without special-casing: the 24h
        # per-session-activity check below still runs on everything admitted
        # here, before any delete.
        #
        # DELIBERATELY NOT WIDENED to every binding-less dir. The abrupt-death
        # shape (binding gone, no summary) carries no closure proof, and the risk
        # asymmetry in the header is one-directional — over-sweeping rm -rf's a
        # live session dir plus its co-located checkpoint and scratch, while
        # under-sweeping merely leaves cruft for 's lane. When in
        # doubt, leave it. The inverse residual (binding PRESENT, nothing else
        # written) is the separate one named at the mtime check below.
        if [ ! -f "$_BFILE" ]; then
            [ -f "$_SD/session-summary.yaml" ] || continue
        fi
        # Signal 1 — PER-SESSION ACTIVITY (). WAS: binding.yaml mtime
        # alone. binding.yaml is written ONCE by /start and never touched again,
        # so its mtime is the session START time, not session ACTIVITY. For an
        # observer session that is exactly wrong: signals 2 and 3 are
        # observer-mode-blind (see the OBSERVER-MODE NOTE at the top), so mtime is
        # the sole protection, and it expires at 24h no matter how hard the
        # session is working. On a box where two agents are BOTH resident the
        # caller's `touch -c` does not help either — it refreshes only its OWN
        # binding, never a co-resident partner's.
        #
        # Test the NEWEST FILE anywhere in the per-session dir instead. The dir is
        # the L1-sanctioned scratch home, so a session doing anything keeps a file
        # fresh, in EVERY mode — a per-session signal, not a per-agent one, and
        # purely local (no network read; the IRREDUCIBLY LOCAL annotation holds).
        #
        # `-type f` IS LOAD-BEARING: a directory's mtime is refreshed by entry
        # create/delete (and by mkdir at fixture-setup time), so matching the dir
        # itself would keep nearly everything and flips scenario 2 of
        # test-cleanup-stale-bindings-residency.sh into a false keep.
        #
        # Strictly MORE conservative than the old check — it keeps a superset —
        # which is the sanctioned direction per the risk asymmetry above:
        # over-sweeping rm -rf's a live session dir, under-sweeping merely leaves
        # a stale dir for 's lane to reap.
        #
        # RESIDUAL, stated rather than papered over: a session that writes NOTHING
        # into its own dir still presents only binding.yaml and stays invisible.
        # Closing that needs a session-side cadence touch (the goal's second
        # suggested shape) and is deliberately NOT built here.
        [ -n "$(find "$_SD" -type f -mmin -1440 2>/dev/null)" ] && continue
        _BIND_SID="${_SD##*/}"
        if _should_delete_binding "$_BA" "$_BIND_SID"; then
            _preserve_unmerged_body_wm "$_BA" "$_SD" "$_BIND_SID"
            rm -rf "$_SD"
        fi
    done
done

# ─── Structural-inertness surface () ────────────────────────────────
# "Nothing to sweep" and "Signal 0 refuses every candidate" were the SAME
# observable — exit 0, no output — which is what let the residency regression
# hide. This makes them distinguishable.
#
# TWO CHANNELS ON PURPOSE, and the file is the load-bearing one. BOTH production
# callers invoke this script as `... 2>/dev/null || true` (stop-hook.sh:221,
# session-save-id.sh:57), so a stderr line is swallowed in production 100% of
# the time — shipping only stderr would reproduce the very invisibility this
# fixes (the guard-1680 class: a diagnostic on a channel its caller discards).
# stderr therefore serves the hand-run diagnosing case; the flag file is what
# survives the real call shape and is what a monitor should test.
#
# Self-healing and O(1): written only while inert, removed as soon as any agent
# qualifies, so it never accumulates and its mtime says whether the inertness is
# CURRENT rather than historical. Purely local — no python, no network — so the
# IRREDUCIBLY LOCAL annotation at the top still holds.
_RESIDENT_N=0
_AGENTDIR_N=0
for _ASR in "$(_agents_root)"/*; do
    [ -d "$_ASR" ] || continue
    _AGENTDIR_N=$((_AGENTDIR_N + 1))
    _has_residency_marker "${_ASR##*/}" && _RESIDENT_N=$((_RESIDENT_N + 1))
done
_INERT_FLAG="$PROJECT_ROOT/core/logs/stale-binding-sweep-inert"
if [ "$_AGENTDIR_N" -gt 0 ] && [ "$_RESIDENT_N" -eq 0 ]; then
    echo "[cleanup-stale-bindings] INERT: 0 of $_AGENTDIR_N agent dir(s) on this box carry a residency marker (local-paths.conf | session/agent-state | session/agent-mode) — Signal 0 refuses every candidate, so BOTH sweep loops are structurally disabled, not merely idle. See $_INERT_FLAG" >&2
    mkdir -p "$PROJECT_ROOT/core/logs" 2>/dev/null || true
    printf '%s inert: 0 of %s agent dir(s) carry a residency marker; both sweep loops structurally disabled (g-306-154)\n' \
        "$(date +%Y-%m-%dT%H:%M:%S)" "$_AGENTDIR_N" > "$_INERT_FLAG" 2>/dev/null || true
else
    rm -f "$_INERT_FLAG" 2>/dev/null || true
fi

# ─── bash-inject sentinel sweep (plan v1 step 0.3, 2026-05-19) ───────────────
# Companion sweep for the bash-agent-inject.py one-shot sentinels at
# core/logs/bash-inject-sentinels/<sid>. These zero-byte files track SIDs
# that hit the PreToolUse[Bash] hook without a resolvable agent binding —
# the sentinel suppresses log-line spam (one record per unique NO_AGENT SID,
# not one per Bash call). Without a sweep they accumulate forever.
#
# TTL: 24h (matches the .active-agent-* sweep above). A SID with no agent
# binding that hasn't fired the hook in 24h is functionally dead — the
# Claude Code session is no longer active, so the log-spam-prevention
# purpose has lapsed. If the SID resumes activity tomorrow it will simply
# re-create the sentinel on its next Bash call (the file is one-shot).
#
# Predicate is SIMPLER than the .active-agent sweep above: bash-inject
# sentinels have NO associated running-session-id or heartbeat (they're
# precisely the "no agent" case). The 24h mtime threshold IS the only
# signal — same conservative-on-error pattern (a SID still active today
# has touched its sentinel within the last 24h or hasn't created one yet).
_SENTINEL_DIR="$PROJECT_ROOT/core/logs/bash-inject-sentinels"
if [ -d "$_SENTINEL_DIR" ]; then
    for _S in "$_SENTINEL_DIR"/*; do
        [ -f "$_S" ] || continue
        [ -z "$(find "$_S" -maxdepth 0 -mmin +1440 2>/dev/null)" ] && continue
        rm -f "$_S"
    done
fi

# ─── Legacy-location bash-inject sentinels at PROJECT_ROOT ───────────────────
# Pre-step-0.13 (2026-05-19), bash-agent-inject.py wrote sentinels to
# `PROJECT_ROOT/.bash-inject-no-binding-<sid>`. Any stragglers from before
# the relocation get swept here on the same 24h TTL — eventually they all
# expire and this loop can be retired (target: Phase 1 deletion after a
# week of zero legacy stragglers observed).
for _S in "$PROJECT_ROOT"/.bash-inject-no-binding-*; do
    [ -f "$_S" ] || continue
    [ -z "$(find "$_S" -maxdepth 0 -mmin +1440 2>/dev/null)" ] && continue
    rm -f "$_S"
done
