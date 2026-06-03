#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/session-save-id
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/session-save-id" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# SessionStart hook — carry forward agent binding across autocompact.
#
# Behavior depends on stdin "source" field:
#   - source=compact: process compact-pending breadcrumb (autocompact resume)
#   - other (startup, resume): prune stale bindings only
#
# WHY source-gated (rb-348): non-compact SessionStart events were hijacking
# breadcrumbs for unrelated agents and swapping their running-session-id files.
# Symptom: cross-agent SID swap → stop hook always sid-mismatch → loops die
# silently. Confirmed swap event 2026-04-19T00:54 between alpha and bravo.
# DO NOT REMOVE the source-gate at line ~61 ("if [ \"$SOURCE\" = \"compact\" ]").
# Without it, every new Claude Code window will hijack pending breadcrumbs.
#
# SID delivery to the LLM is via the PreToolUse[Bash] hook's $MIND_SID export
# (core/scripts/bash-agent-inject.py). No project-root bridge file exists —
# /start reads $MIND_SID directly. See guard-341 and rb-386.
#
# `set -e` deliberately OMITTED. State-writing lines below use
# `echo > .tmp && mv` patterns; an echo failure (OneDrive contention,
# antivirus scan, disk full) under -e exits the script with partial state on
# disk — running-session-id missing, compact-pending already consumed.
# recovery-gate.sh Path B then sees that intermediate state and may
# false-positive demote a healthy runner. The orchestrator's `|| true` hides
# the early exit, making the bug invisible. Match recovery-gate.sh's
# convention: -u + pipefail, explicit per-command error handling.
set -uo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Warm Python once per session. This is the FIRST hook to fire on a new
# session, so it pays Windows Python cold-start (py launcher + DLL load).
# All subsequent hooks (PreToolUse, postcompact-restore) see a warm shim,
# which keeps them under their per-hook timeout.
python3 -c "pass" 2>/dev/null || true

# Read full stdin once — need both session_id AND source
STDIN_JSON=$(cat 2>/dev/null || echo "{}")
SID=$(printf '%s' "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
SOURCE=$(printf '%s' "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('source',''))" 2>/dev/null || echo "")
[ -n "$SID" ] || exit 0

# --- 1. Proactive stale-binding cleanup ---
# Sessions that exited without triggering Stop or StopFailure (terminal
# closed, Claude Code crashed, etc.) leave .active-agent-<SID> behind.
# Without proactive cleanup these accumulate until the next /stop fires —
# observed: 6 stale files at session 50.
#
# The 3-signal delete predicate lives in cleanup-stale-bindings.sh, shared
# with stop-hook.sh. See that script for the full predicate + the
# 2026-05-12 spurious-delete trace that motivated the 3-signal design.
bash "$(dirname "$0")/cleanup-stale-bindings.sh" 2>/dev/null || true

# --- Stale compact-pending cleanup ---
# Mirrors the .active-agent stale-cleanup above. Closes the fail-open residual
# risk in the 5th-witness fix (/): when precompact-serialize.sh
# fail-opens at the 60s timeout, the holder file is absent and the breadcrumb
# iteration falls back to original 4-witness behavior. A stale breadcrumb from
# a crashed prior session would 4-witness-pass and produce a cross-agent SID
# swap. Predicate: file > 24h old AND owning agent's runner-heartbeat is stale
# (mtime > runner_heartbeat.stale_minutes from aspirations.yaml) → delete.
# Removes stale breadcrumbs BEFORE the iteration that consumes them, so the
# fail-open path can no longer encounter pre-existing-stale data.
#
# WHY freshness probe (not file-existence): heartbeat-tick.sh only `touch`es
# the file (never deletes); /stop never deletes it; crashed agents leave it
# behind. An existence check sees the file present in the canonical
# crashed-runner scenario this pattern was designed to handle and skips
# cleanup — exactly when cleanup must fire. heartbeat-stale.sh routes through
# the canonical liveness signal (mtime vs runner_heartbeat.stale_minutes,
# default 30) and correctly returns "stale" for crashed agents and missing-file
# alike. (Fresh-eyes finding msg-20260502-014409, R1=, R2=.)
# Phase 2.5.D: agent dirs live under PROJECT_ROOT/agents/. Source _paths.sh
# to pick up AGENTS_PARENT_DIR — fresh-eyes-code F3 found the bare glob
# `"$PROJECT_ROOT"/*/session/compact-pending` matched ZERO files post-
# relocation. Sourcing here is safe even if a parent already sourced it
# (idempotent). DO NOT revert to the bare glob; downstream consumers of
# this stale-cleanup loop silently lose breadcrumb consumption if it does.
# shellcheck disable=SC1091
source "$(dirname "$0")/_paths.sh"
for _CP in "$PROJECT_ROOT/$AGENTS_PARENT_DIR"/*/session/compact-pending; do
    [ -f "$_CP" ] || continue
    [ -z "$(find "$_CP" -maxdepth 0 -mmin +1440 2>/dev/null)" ] && continue
    _CP_AGENT=$(basename "$(dirname "$(dirname "$_CP")")")
    [ "$(MIND_AGENT=$_CP_AGENT bash "$(dirname "$0")/heartbeat-stale.sh" 2>/dev/null)" = "fresh" ] && continue
    rm -f "$_CP"
done
unset _CP _CP_AGENT

# --- Breadcrumb from stop hook — ONLY processed on actual autocompact ---
# The stop hook wrote the OLD SID to <agent>/session/compact-pending before blocking.
# Consumption gate (3+3 = six checks, all must pass; see witness comments):
#   Three filter-witnesses (5/6/7) chosen BEFORE mv-claim:
#     5. lock holder agent == breadcrumb's agent dir
#     6. breadcrumb's owning-agent heartbeat fresh
#     7. lock sid == breadcrumb content (subsumes 5 when present)
#   Three content-witnesses (the "must agree" check after mv-claim):
#     compact-pending content == running-session-id; .active-agent-<old-SID>
#     binding == agent dir; breadcrumb non-empty.
# Witness mismatch → restore breadcrumb, skip (rightful owner can claim later).
#
# Atomic claim via mv (not cat+rm): only ONE concurrent hook can claim each
# breadcrumb file. The losing hook gets ENOENT and moves on.
#
# Cross-agent race protection has TWO layers:
#   1. precompact-serialize.sh (upstream gate, rb-356) serializes source=compact
#      events via a global mkdir lock — two agents' SessionStarts almost never
#      enter this code path concurrently in production.
#   2. The witness check below is the last-line defense if the gate fails
#      open (60 s wait exceeded) or is bypassed.
COMPACT_AGENT=""
if [ "$SOURCE" = "compact" ]; then
    # POISON gate (2026-05-12 hardening Tier 4a). precompact-serialize.sh
    # writes .autocompact-serialize-poison when its 60s acquire timeout fires.
    # That timeout means concurrent compacts can't be serialized for this
    # cycle — the 5th/7th witnesses are gone and 4-witness fallback re-opens
    # the cross-agent stomp class (rb-348). Skip breadcrumb consumption
    # entirely and let recovery-gate handle the aftermath. Consume the
    # poison file (delete on read) so next clean compact isn't blocked.
    if [ -f "$PROJECT_ROOT/.autocompact-serialize-poison" ]; then
        printf '{"ts":"%s","source":"session-save-id-compact","reason":"poison","sid":"%s"}\n' \
            "$(date +%Y-%m-%dT%H:%M:%S)" "$SID" \
            >> "$PROJECT_ROOT/core/logs/sid-collisions.jsonl" 2>/dev/null || true
        rm -f "$PROJECT_ROOT/.autocompact-serialize-poison" 2>/dev/null || true
        # Release the precompact lock as the original code does on every
        # source=compact event (see L209), preserving the gate's lifecycle.
        rm -rf "$PROJECT_ROOT/.autocompact-serialize-lock" 2>/dev/null || true
        exit 0
    fi

    # Witnesses 5 (lock holder = agent) and 7 (lock sid = breadcrumb sid)
    # filter which breadcrumb THIS hook is authorized to consume. Both files
    # are written by precompact-serialize.sh from the same session, so 7th
    # strictly subsumes 5th when present. 5th remains as a fallback when
    # the sid file is missing (precompact 60s timeout). Heartbeat (6th)
    # below catches stranded breadcrumbs from dead runners.
    #
    # Incident lineage — each filter closes a distinct stomp class. DO NOT
    # remove any of the three:  (2026-05-01 cross-agent stomp =>
    # 5th); 2026-05-05 alpha-RSID stomp (holder-missing fail-open => 6th);
    # 2026-05-10 bravo same-agent stomp (5th cannot tell observer from
    # runner of same agent => 7th).
    _LOCK_HOLDER=""
    _LOCK_SID=""
    if [ -f "$PROJECT_ROOT/.autocompact-serialize-lock/holder" ]; then
        _LOCK_HOLDER=$(cat "$PROJECT_ROOT/.autocompact-serialize-lock/holder" 2>/dev/null | tr -d '\r\n')
    fi
    if [ -f "$PROJECT_ROOT/.autocompact-serialize-lock/sid" ]; then
        _LOCK_SID=$(cat "$PROJECT_ROOT/.autocompact-serialize-lock/sid" 2>/dev/null | tr -d '\r\n')
    fi
    # Phase 2.5.D: see stale-cleanup loop above for the AGENTS_PARENT_DIR
    # rationale (fresh-eyes-code F3, 2026-05-19). _paths.sh already sourced
    # above; reuse the same prefix here so the breadcrumb consumption path
    # actually finds the per-agent compact-pending files.
    for _CP in "$PROJECT_ROOT/$AGENTS_PARENT_DIR"/*/session/compact-pending; do
        [ -f "$_CP" ] || continue
        _SESSION_DIR=$(dirname "$_CP")
        _AGENT_NAME=$(basename "$(dirname "$_SESSION_DIR")")
        # Fifth-witness check (when available). When the lock holder is
        # known, only the holder's breadcrumb may be consumed by THIS hook.
        # Other agents' breadcrumbs stay on disk for their own SessionStart
        # to claim. DO NOT mv-claim before this filter — atomic claim must
        # happen only for breadcrumbs THIS hook is authorized to consume,
        # otherwise restoring on mismatch races with the rightful owner.
        if [ -n "$_LOCK_HOLDER" ] && [ "$_AGENT_NAME" != "$_LOCK_HOLDER" ]; then
            continue
        fi
        # Seventh-witness SID pre-read. Filters SAME-AGENT observer-vs-runner
        # races that the 5th witness (agent name) cannot. DO NOT mv-claim
        # before this peek — pre-read is race-safe under the source=compact
        # lock and avoids the orphan .claimed-PID files that mv-claim's
        # restore-mv leaves behind when restore-mv fails (OneDrive contention).
        if [ -n "$_LOCK_SID" ]; then
            _PEEK_SID=$(cat "$_CP" 2>/dev/null | tr -d '\r\n')
            if [ "$_PEEK_SID" != "$_LOCK_SID" ]; then
                continue
            fi
        fi
        # Sixth-witness heartbeat guard. Closes the holder-missing fail-open
        # hole that produced the 2026-05-05 alpha-RSID stomp: refuse to
        # consume a breadcrumb whose owning agent's runner-heartbeat is
        # stale. The agent actually autocompacting has a fresh heartbeat
        # (last ticked at the BLOCK that triggered the autocompact, seconds
        # before this hook fires); a stranded breadcrumb's owning agent
        # does not. Same probe used by L67-73 stale-breadcrumb cleanup —
        # single source of truth for "is this agent live".
        if [ "$(MIND_AGENT=$_AGENT_NAME bash "$(dirname "$0")/heartbeat-stale.sh" 2>/dev/null)" = "stale" ]; then
            continue
        fi
        _CLAIMED="$_CP.claimed-$$"
        if mv "$_CP" "$_CLAIMED" 2>/dev/null; then
            _OLD_SID=$(cat "$_CLAIMED" 2>/dev/null | tr -d '\r\n')
            _RUNNER_SID=$(cat "$_SESSION_DIR/running-session-id" 2>/dev/null | tr -d '\r\n')
            _BOUND_AGENT=""
            [ -n "$_OLD_SID" ] && [ -f "$PROJECT_ROOT/.active-agent-$_OLD_SID" ] && \
                _BOUND_AGENT=$(cat "$PROJECT_ROOT/.active-agent-$_OLD_SID" 2>/dev/null | tr -d '\r\n')
            # Two witnesses must agree: breadcrumb SID matches
            # running-session-id, and the .active-agent binding for that SID
            # points to this agent dir. Empty _OLD_SID is implicitly rejected
            # because it produces empty _BOUND_AGENT (the [ -n "$_OLD_SID" ]
            # short-circuit above), which cannot equal the non-empty
            # _AGENT_NAME. latest-session-id is NOT checked here — it is
            # pair-written with running-session-id by every legitimate writer,
            # so the comparison adds no independent signal.
            if [ "$_OLD_SID" = "$_RUNNER_SID" ] \
                && [ "$_BOUND_AGENT" = "$_AGENT_NAME" ]; then
                # Eighth-witness NEW-SID collision check (2026-05-12 hardening).
                # All 7 prior witnesses validate the OLD_SID side — that the
                # breadcrumb's data is self-consistent for this agent's prior
                # runner. None of them verify the NEW_SID (Claude Code's
                # post-compact session_id) is FREE. When Claude Code reuses a
                # SID across two terminals (--continue / --resume in a second
                # window), the new SID arrives already bound to a different
                # live agent. Writing running-session-id with the colliding
                # SID then breaks both agents' stop-hook routing. Detect HERE,
                # at the binding-write boundary, instead of post-hoc forensics.
                # Failure mode this closes: 2026-05-12 zeta-bravo cross-binding
                # incident (78MB shared transcript, 48+29 interleaved BLOCKs).
                # DO NOT REMOVE — the OLD_SID witness chain is silent about
                # the NEW_SID and that gap is the entire bug.
                if bash "$(dirname "$0")/sid-collision-check.sh" "$_AGENT_NAME" "$SID" 2>/dev/null; then
                    COMPACT_AGENT="$_AGENT_NAME"
                    rm -f "$_CLAIMED"
                    break
                else
                    # NEW_SID collision — do NOT claim. Restore breadcrumb so
                    # next SessionStart with a non-colliding SID (e.g., after
                    # user runs claude --fork-session) can re-attempt. Log
                    # loudly to core/logs/sid-collisions.jsonl for post-hoc
                    # inspection (relocated 2026-05-19 from PROJECT_ROOT/).
                    # Runner's loop will die naturally on next stop-hook
                    # (HOOK_SID != running-session-id → sid-mismatch → ALLOW),
                    # and recovery-gate next SessionStart will demote to IDLE.
                    _COLL_EXISTING=$(cat "$PROJECT_ROOT/.active-agent-$SID" 2>/dev/null | tr -d '\r\n')
                    printf '{"ts":"%s","source":"session-save-id-compact","new_sid":"%s","compact_agent":"%s","existing_bind":"%s","old_sid":"%s"}\n' \
                        "$(date +%Y-%m-%dT%H:%M:%S)" "$SID" "$_AGENT_NAME" "$_COLL_EXISTING" "$_OLD_SID" \
                        >> "$PROJECT_ROOT/core/logs/sid-collisions.jsonl" 2>/dev/null || true
                    unset _COLL_EXISTING
                    mv "$_CLAIMED" "$_CP" 2>/dev/null || rm -f "$_CLAIMED"
                fi
            else
                # Witness mismatch — restore breadcrumb (best-effort; if it
                # fails, the rightful owner will retry on its own SessionStart).
                mv "$_CLAIMED" "$_CP" 2>/dev/null || rm -f "$_CLAIMED"
            fi
        fi
    done
    unset _CP _CLAIMED _SESSION_DIR _OLD_SID _RUNNER_SID _BOUND_AGENT _AGENT_NAME _LOCK_HOLDER _LOCK_SID _PEEK_SID

    # Release the PreCompact serialization gate (rb-356) — our compact has
    # resumed, the gate's job for this cycle is done. Fires for ALL
    # source=compact events regardless of witness-match outcome, because
    # assistant/reader sessions DO autocompact but never write a
    # compact-pending breadcrumb (the stop hook only writes one when the
    # autonomous loop BLOCKs). If we only released on witness success, those
    # sessions would strand the lock until 5-min stale cleanup, blocking
    # subsequent autonomous compacts for up to a minute each.
    # DO NOT move this back inside the witness-match block.
    # DO NOT move this outside the if [ "$SOURCE" = "compact" ] block —
    # releasing on startup/resume would let new windows clear locks held by
    # mid-compact autonomous sessions and defeat the gate.
    rm -rf "$PROJECT_ROOT/.autocompact-serialize-lock" 2>/dev/null || true

    # POST_RECOVERY_EDIT_OVERRIDE="User-directed framework fix for hung-autocompact false-positive recovery; implementing before /start delta to prevent immediate repeat."
    # --- Clear compact-in-flight sentinels for any agent whose compact just
    # finished. Written by precompact-serialize.sh; mtime <10min means "compact
    # just resumed via this SessionStart event" — the autocompact roundtrip
    # is complete and the sentinel has served its purpose. Older files are
    # genuine hangs and stay for recovery-gate Path C to act on. Cross-agent
    # iteration mirrors the stale-cleanup pattern at line 86. Fail-open.
    for _CIF in "$PROJECT_ROOT/$AGENTS_PARENT_DIR"/*/session/compact-in-flight; do
        [ -f "$_CIF" ] || continue
        [ -n "$(find "$_CIF" -maxdepth 0 -mmin -10 2>/dev/null)" ] || continue
        rm -f "$_CIF" 2>/dev/null || true
    done
    unset _CIF
fi

# --- 2. Resolve agent binding for the new SID ---
# Priority: existing binding > breadcrumb
# All file writes use atomic .tmp + mv to prevent torn reads from concurrent
# readers (bash-agent-inject, stop-hook). echo > file is NOT atomic on Windows.
ACTIVE_FILE="$PROJECT_ROOT/.active-agent-$SID"
RESTORED_AGENT=""
if [ -f "$ACTIVE_FILE" ] && [ -s "$ACTIVE_FILE" ]; then
    # Session reconnect: .active-agent-$SID already exists
    RESTORED_AGENT=$(cat "$ACTIVE_FILE" | tr -d '\r\n')
elif [ -n "$COMPACT_AGENT" ] && [ -d "$(agent_dir "$COMPACT_AGENT")" ]; then
    # Breadcrumb: stop hook identified the correct agent for this autocompact
    echo "$COMPACT_AGENT" > "$ACTIVE_FILE.tmp" && mv "$ACTIVE_FILE.tmp" "$ACTIVE_FILE"
    RESTORED_AGENT="$COMPACT_AGENT"
fi

# --- 3. Write SID to agent session dir (atomic) ---
# BOTH writes are gated on runner-legitimacy. latest-session-id and
# running-session-id must stay in lockstep — any path that updates one
# without the other causes /stop's runner/observer detection to desync.
# Legitimacy = one of:
#   1. COMPACT_AGENT set (four-witness match confirms this IS the runner
#      resuming from autocompact — new SID is the runner's new identity)
#   2. Existing running-session-id equals our NEW SID (same-session reconnect
#      after /start already wrote both files atomically)
# Observer and assistant sessions hit NEITHER — they resolve via
# RESTORED_AGENT from .active-agent-$SID but do not own the runner files.
# The empty-running-session-id case is intentionally NOT in the gate:
# /start (IDLE→autonomous Step 3 and UNINITIALIZED C8) is the ONLY legitimate
# first-writer of running-session-id, and it does the atomic pair-write itself —
# SessionStart has no mandate to create one. DO NOT add a fallback that writes
# latest-session-id without this gate — the observer-clobber bug re-appears
# immediately.
RESOLVED_AGENT="${COMPACT_AGENT:-${MIND_AGENT:-${RESTORED_AGENT:-}}}"
if [ -n "$RESOLVED_AGENT" ]; then
    AGENT_SESSION_DIR="$(agent_dir "$RESOLVED_AGENT")/session"
    if [ -d "$AGENT_SESSION_DIR" ]; then
        _SAVED_RUNNER=$(cat "$AGENT_SESSION_DIR/running-session-id" 2>/dev/null | tr -d '\r\n')
        if [ -n "$COMPACT_AGENT" ] || [ "$SID" = "$_SAVED_RUNNER" ]; then
            # Verify-after-write loop: `echo > .tmp && mv` is atomic on POSIX
            # and NTFS, BUT individual command failures (OneDrive lock,
            # antivirus interception, disk full at the moment of write) can
            # leave .tmp orphaned and the target unchanged. Without verify,
            # the stale running-session-id then breaks stop-hook routing
            # (HOOK_SID != stale value → sid-mismatch → ALLOW → loop dies).
            # One retry covers transient locks; persistent failure goes to
            # .write-failures.jsonl for recovery-gate / human follow-up.
            # DO NOT skip the verify — the silent-stale-SID class is exactly
            # what motivated this block.
            for _SID_TARGET in latest-session-id running-session-id; do
                # latest-session-id is unconditional under the gate.
                # running-session-id is gated to COMPACT_AGENT + pre-existing
                # file (preserves the "/start is sole first-writer" invariant
                # at lines 290-294).
                [ "$_SID_TARGET" = "running-session-id" ] && \
                    { [ -z "$COMPACT_AGENT" ] || [ ! -f "$AGENT_SESSION_DIR/running-session-id" ]; } && continue
                _SID_PATH="$AGENT_SESSION_DIR/$_SID_TARGET"
                _SID_ATTEMPT=0
                while [ "$_SID_ATTEMPT" -lt 2 ]; do
                    echo "$SID" > "$_SID_PATH.tmp" 2>/dev/null && mv "$_SID_PATH.tmp" "$_SID_PATH" 2>/dev/null
                    _SID_ACTUAL=$(cat "$_SID_PATH" 2>/dev/null | tr -d '\r\n')
                    [ "$_SID_ACTUAL" = "$SID" ] && break
                    _SID_ATTEMPT=$((_SID_ATTEMPT + 1))
                done
                # Persistent failure → log for diagnostics. Do NOT abort the
                # script; the other file may still write correctly, and
                # recovery-gate Path B will catch the resulting state mismatch.
                if [ "$_SID_ACTUAL" != "$SID" ]; then
                    printf '{"ts":"%s","source":"session-save-id","target":"%s","expected":"%s","actual":"%s","agent":"%s"}\n' \
                        "$(date +%Y-%m-%dT%H:%M:%S)" "$_SID_PATH" "$SID" "$_SID_ACTUAL" "$RESOLVED_AGENT" \
                        >> "$PROJECT_ROOT/.write-failures.jsonl" 2>/dev/null || true
                fi
            done
            unset _SID_TARGET _SID_PATH _SID_ATTEMPT _SID_ACTUAL
            # Runner-heartbeat is pure mtime — no content to rewrite. DO NOT
            # add an $SID write here. Any write would just advance the mtime
            # that heartbeat-tick already advances every iteration + every 60s
            # during B7 waits, and it would re-introduce the identity-drift
            # bug class (heartbeat content diverging from running-session-id)
            # that motivated removing the content-comparison path entirely.
        fi
        unset _SAVED_RUNNER
    fi
fi
