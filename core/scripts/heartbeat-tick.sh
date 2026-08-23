#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Per-iteration heartbeat tick: local file mtime + cross-agent team-state write.
#
# Both heartbeats advance together — single source of truth for "this agent is
# alive at NOW." Called once per aspirations-loop iteration from Phase -0.5,
# once from /start autonomous seed, and every 60s from interruptible-sleep.sh
# during long B7 waits so mtime can't cross the staleness threshold.
#
# Liveness model: pure mtime. heartbeat-stale.sh compares file age against
# runner_heartbeat.stale_minutes and returns fresh/stale. No writer-identity
# check — the heartbeat-tick cadence during waits + the single-caller invariant
# (aspirations loop + /start + interruptible-sleep) is the liveness contract.
#
# Why a script and not inline in aspirations/SKILL.md:
# SKILL.md pseudocode is loaded into LLM context at prime/boot — changes don't
# take effect for currently-running loops until autocompact reloads or
# /start --recover. Scripts re-exec on every call, so the LATEST version on
# disk is always the one that runs. See rb-399.
#
# Cross-agent writes (team-state.yaml agent_status.<self>.*):
#   - last_active (ISO timestamp) — partner liveness signal
#   - live_phase (diary-tail derived) — informational, see live-phase-emit.sh
#   - Read by partner agents via team-state-read.sh; used by /prime, status
#     displays, fresh-eyes-review, and anything that infers partner liveness
#     or current activity.
#   - Fail-open via `|| true` — a team-state write failure (lock contention,
#     disk full) must NEVER block the iteration. The local heartbeat touch is
#     unconditional; if THAT fails the filesystem is broken.
#
# stderr is NOT suppressed (no 2>/dev/null) — see rb-400. Real errors must
# surface to the LLM-readable log so silent failures can't hide.

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# --- Empty-AGENT_DIR gate ---------------------------------------------------
# Refuse to tick when the agent env var was not injected — _paths.sh fail-opens
# AGENT_DIR to empty string in that case (the loud-failure-on-next-FS-op
# design from plan v1 step 0.1). Without this gate, `touch "$AGENT_DIR/...`
# expands to `touch "/session/runner-heartbeat"` which fails with rc=1; set
# -e then kills the script before team-state-update.sh and live-phase-emit.sh
# run — both local + cross-agent heartbeats go stale silently.
#
# Root cause class: intermittent bash-agent-inject hook misses
# (no_active_agent_binding telemetry confirms the class). Until the upstream
# hook timing is fixed, this gate at least prevents the silent-skip downstream.
#
# Exit 2 (not 1) — matches the state-gate convention below: "refused, but
# not a hard failure." Aspirations Phase -0.5 calls heartbeat-tick.sh
# unconditionally and does not check rc; exit 2 keeps the loop alive.
# stderr surfaces the diagnostic so the LLM-readable log captures the miss.
if [ -z "${AGENT_DIR:-}" ] || [ -z "${MIND_AGENT:-}" ]; then
    echo "heartbeat-tick: REFUSED — MIND_AGENT empty (AGENT_DIR=\"${AGENT_DIR:-}\"). bash-agent-inject hook likely did not fire for this Bash call. Heartbeat skipped (no /session write, no team-state update, no live-phase emit)." >&2
    exit 2
fi

# ── Per-BODY heartbeat () ──────────────────────────────────────────
# DELIBERATELY ABOVE THE STATE GATE. This block used to sit below it, which
# meant it never ran on the one box where it matters most (): a
# cross-box worker Body is IDLE *by design* — the worker never flips
# agent-state — so the state gate's `exit 2` fired first and the per-SID
# liveness signal was never written on exactly the box exposed to a claim pop.
# Textbook guard-1479: a write placed below an early short-circuit passes
# syntax, unit tests and CI, and still never executes in production.
#
# Hoisting is safe because this is NOT the signal the state gate protects.
# The gate exists to stop the agent-WIDE `runner-heartbeat` going fresh while
# agent-state=IDLE (the alpha-2026-05-13 `heartbeat_without_running` desync,
# guard-543) — that touch stays BELOW the gate, unchanged. This file is
# per-SID and its only reader
# (mind_api/src/endpoints/aspirations_write.py::_holder_session_is_live_runner)
# consults it to answer "is THIS Body alive", never "is the agent RUNNING".
# On an IDLE worker box that answer is legitimately yes, so writing it here
# creates no desync — it removes a false negative.
#
# WHY HERE and not in the loop's SKILL.md or the claim path (rb-4589): a
# liveness heartbeat must be supervisor-emitted and UNCONDITIONAL, never
# piggybacked on a discretionary step. Sitting below a state gate made it
# conditional on a state the worker never enters, which is the same defect
# rb-4589 names, reached from the other side. This script IS that supervisor
# path — one tick per iteration from Phase -0.5, one from /start, and one every
# 60s from interruptible-sleep.sh during long waits — so a Body that is alive
# but idle still reports fresh, which is the case `body_state` in
# body-manifest.yaml cannot cover (a crashed worker never clears it, so state
# alone would convert a transient crash into a permanent wedge — rb-4081, the
# stale-status class).
#
# The agent-WIDE touch below is a different signal: `running-session-id` names
# only the REDUCER, so under the Mind/Body split a non-reducer worker has no
# per-session liveness signal at all, and the claim CAS in
# aspirations_write.py::_holder_session_is_live_runner read every live worker as
# a "dormant/previous session" and permitted takeover of its claim.
#
# Deliberately written for EVERY Body including the reducer: a per-SID signal
# with a uniform writer has no branch to get wrong. The reader only consults it
# for a non-reducer holder, so the reducer's copy is simply unused.
#
# Guarded on the session dir ALREADY EXISTING — never `mkdir -p`. /start owns
# session-dir creation (session-binding-write.py); creating one here would
# invent a dir for an unbound SID, which .claude/rules/path-resolution.md L1
# refuses. Absent dir -> no write -> reader sees no file -> permits the claim,
# which is the fail-open direction. `|| true` for the same reason the
# team-state write below has it: a body-heartbeat failure must never block an
# iteration. Contrast the agent-wide touch below, which is unconditional once
# the state gate passes.
# Rooted at $AGENT_DIR — the SAME base the agent-wide touch below uses — so the
# two heartbeats can never resolve to different agent dirs. Deliberately NOT
# agent_session_dir(), which re-derives from PROJECT_ROOT and would ignore the
# sanctioned _AGENT_DIR_OVERRIDE test seam that $AGENT_DIR honors. The
# `sessions` segment still comes from the SESSIONS_DIRNAME constant, never a
# literal (CLAUDE.md "Agent-dir Resolution": literal copies are invisible to
# the audit greps).
# ── The SYNCABLE twin ( outcome 2) ─────────────────────────────────
# The `sessions/<SID>/body-heartbeat` touch above is the SAME-BOX signal and is
# correct as-is. It can never serve the CROSS-BOX reader, for two independent
# reasons, either of which alone is fatal:
#
#   1. `sessions` (PLURAL) is a member of owncloud_sync._EXCLUDE_DIRS — it is
#      walk-pruned (owncloud_sync.py:1207) and any path carrying a pruned
#      segment is rejected (:240). Nothing under it EVER reaches the store, so
#      a store-routed read of it returns nothing on every call, forever — a
#      PERMANENT false "holder is dead", which would make the sweep MORE likely
#      to pop a live claim: the exact inverse of this goal.
#   2. Object mtime does not survive the sync, and that file is pure-mtime.
#
# So the carrier is COPIED to `session/` (SINGULAR, syncable) — the same move,
# for the same reason, that the staged-Body-WM transport already makes
# (session-manifest.yaml:532-535 states the constraint verbatim).
#
# CONTENT, not mtime — and the content carries the WRITER IDENTITY. guard-358:
# "mtime alone cannot distinguish designated writer is alive from wrong writer
# is touching", and it requires a THREE-state probe (fresh-correct / fresh-wrong
# / stale) rather than two. That matters here specifically because the sweep is
# deciding about ONE named `claimed_by_sid`: a carrier written by a DIFFERENT
# body must not be allowed to vouch for it. The filename alone cannot carry that
# — anything can write any filename — so `sid` is in the body.
#
# This does NOT re-introduce the writer-identity scheme removed 2026-04-21 (see
# the runner-heartbeat comment below). That removal was correct for the
# AGENT-WIDE signal, which is single-writer per box and read by mtime. This is a
# different signal in a SHARED namespace where mtime does not survive at all.
#
# `.json` is load-bearing twice over. Registering the glob in session-manifest
# is what makes it sync_tier continuity — but an UNREGISTERED basename falls to
# _session_file_machine_local's fail-safe extension heuristic
# (owncloud_sync.py:314-316), and an EXTENSIONLESS name is not in
# _SESSION_DATA_EXTS, so `body-heartbeat-<SID>` would classify machine_local and
# silently never leave the box. That is precisely the measured `.hash` incident
# in session-manifest.yaml:537-543. Registered AND synced-extension, so neither
# mechanism alone has to hold.
#
# Written under the SAME bound-session guard as the touch above: no bound
# session dir -> no carrier -> reader sees nothing -> falls through to today's
# 120m behavior, which is the fail-open direction.
if [ -n "${MIND_SID:-}" ]; then
    _HB_BODY_DIR="$AGENT_DIR/$SESSIONS_DIRNAME/$MIND_SID"
    if [ -d "$_HB_BODY_DIR" ]; then
        touch "$_HB_BODY_DIR/body-heartbeat" || true
        # Atomic: write-then-rename, so a concurrent reader never sees a
        # half-written object and decode-fails into a false "holder is dead".
        #
        # BARE `|| true`, NEVER `2>/dev/null` (rb-400, and the header of THIS
        # file at L31-32 states the same invariant). `|| true` alone gives
        # fail-open; adding `2>/dev/null` additionally SUPPRESSES the
        # diagnostic, which is the rb-391 silent-boundary pattern. That
        # combination is especially bad here: on Windows `mv -f` over a file
        # another process holds open can fail persistently, and a silenced
        # failure would leave a permanently-stale carrier with no signal —
        # the sweep would then fall back to the flat grace forever while
        # looking healthy, which is the exact class this carrier exists to
        # close. (Caught by fresh-eyes-code on this goal's own diff: the
        # suppression was added three lines below the comment block warning
        # against it.)
        _HB_CARRIER="$AGENT_DIR/session/body-heartbeat-$MIND_SID.json"
        # : carry this Body's CURRENT body_state alongside the
        # timestamp. The peer-side stall probe (worker_stall.classify_body)
        # could not tell a Body that CLOSED from one that DIED between units --
        # both go stale holding no claim -- so it called both benign and a
        # worker that text-died after releasing unit N and before claiming N+1
        # was invisible. The structured state that distinguishes them already
        # exists and is already populated, but body-manifest.yaml lives under
        # `sessions/`, which is in owncloud_sync._EXCLUDE_DIRS (walk-pruned,
        # never pushed), so a peer STRUCTURALLY cannot read it. This carrier is
        # published (the  exemption), so mirroring the field here is
        # what makes it reachable at all.
        #
        # Read with a file test rather than `2>/dev/null` -- L31-32 of this file
        # and rb-400 forbid suppressing the diagnostic, and a manifest that
        # fails to parse should still print why. An empty value is the fail-open
        # direction: the reader renders it `stale_state_unknown`, which never
        # alerts, so a Body whose manifest is unreadable can never be reported
        # as a stall on the strength of that failure alone.
        _HB_STATE=""
        if [ -f "$_HB_BODY_DIR/body-manifest.yaml" ]; then
            _HB_STATE="$(sed -n 's/^body_state:[[:space:]]*//p' \
                "$_HB_BODY_DIR/body-manifest.yaml" | head -1 | tr -d '"'\'' \t\r')"
        fi
        printf '{"sid":"%s","agent":"%s","host":"%s","ts":"%s","body_state":"%s"}\n' \
            "$MIND_SID" "${MIND_AGENT:-}" "$(hostname || echo unknown)" \
            "$(date +%Y-%m-%dT%H:%M:%S)" "$_HB_STATE" > "$_HB_CARRIER.tmp" \
            && mv -f "$_HB_CARRIER.tmp" "$_HB_CARRIER" || true
    fi
fi

# --- State gate (g-115-NEW, 2026-05-13) -------------------------------------
# Refuse to tick when agent-state=IDLE. Without this gate, a stop-hook-
# cancelled loop death + recovery-gate's RUNNING→IDLE flip leaves callers
# (e.g. /aspirations Phase -0.5) free to write a fresh heartbeat AGAINST an
# IDLE state. End state: agent-state=IDLE + runner-heartbeat=fresh +
# loop-active=set — exactly the `heartbeat_without_running` desync that
# session-manifest.yaml already flags as a warning. This gate prevents the
# write at the source instead of catching it after the fact.
#
# Canonical incident: alpha session cbb27ab3 on 2026-05-13. After hung-
# autocompact recovery flipped state RUNNING→IDLE at 05:17, the LLM resumed
# via "continue" and batched 4 Bash calls (state-get + stop-check +
# loop-active-set + heartbeat-tick) into one. By the time the state-get
# result arrived, loop-active was set + heartbeat was ticked against IDLE.
#
# Bypass: --bypass-state — used by /start IDLE→RUNNING transition where
# heartbeat-tick MUST seed mtime BEFORE state-set RUNNING per rb-323/guard-403
# to close the observer-probe race (state=RUNNING with stale heartbeat).
# Only /start (IDLE Step 3 + UNINITIALIZED Phase C8) is authorized to pass it.
#
# Why state=IDLE is the only bad case: state=RUNNING is the normal autonomous
# path (per-iteration tick + interruptible-sleep B7 tick); state=UNINITIALIZED
# happens only on the very first /start of an agent (heartbeat file is absent
# anyway — no desync possible); state=NO_AGENT means MIND_AGENT is unset and
# _paths.sh would have already exited.
if [ "${1:-}" != "--bypass-state" ]; then
    # rb-400 — no 2>/dev/null (silent-boundary anti-pattern). `|| true` falls
    # open: a hypothetical state-get crash → empty STATE → STATE != "IDLE" →
    # tick proceeds. Aspirations Phase -1.5 is the primary gate; this is
    # defense-in-depth, so fail-open is correct here.
    STATE=$(bash "$(cd "$(dirname "$0")" && pwd)/session-state-get.sh" || true)
    if [ "$STATE" = "IDLE" ]; then
        echo "heartbeat-tick: REFUSED — agent-state=IDLE. Caller is writing a heartbeat against a non-running agent (this is the alpha-2026-05-13 desync class). Use --bypass-state from /start IDLE→RUNNING only." >&2
        exit 2
    fi
fi

# Pure mtime heartbeat. Content is irrelevant — only the file's mtime is read
# by heartbeat-stale.sh. DO NOT reintroduce a writer-identity check here (prior
# token-based scheme was removed 2026-04-21 after pure-mtime proved sufficient
# once interruptible-sleep.sh began ticking during B7 waits).
#
# DO NOT add a running-session-id refresh here either. running-session-id and
# latest-session-id must be written atomically as a pair under runner-legitimacy
# gates — see session-save-id.sh:127-142. Refreshing one without the other from
# heartbeat-tick desyncs the pair and re-introduces the /stop runner/observer
# misclassification class (2026-04-20 hang). Autocompact rotation is handled
# upstream by session-save-id.sh's four-witness gate. If that gate fails, fix
# the gate — do not add a defensive write here.
touch "$AGENT_DIR/session/runner-heartbeat"

# ── Per-BODY heartbeat: MOVED ABOVE THE STATE GATE () ──────────────
# The per-SID body-heartbeat write used to live HERE. It now sits above the
# state gate, because a cross-box worker Body is IDLE by design and the gate's
# `exit 2` meant this line never ran on the box it exists to protect. See the
# block above for the full rationale. Do NOT move it back down.

bash "$(dirname "$0")/team-state-update.sh" \
    --field "agent_status.$MIND_AGENT.last_active" \
    --value "\"$(date +%Y-%m-%dT%H:%M:%S)\"" || true

# Live_phase mirror from execution-diary tail. The `|| true` below is the
# SINGLE fail-open boundary for this signal — DO NOT add internal fallbacks
# inside live-phase-emit.sh. Failures there must crash loudly so stderr
# surfaces the problem and the next tick retries naturally.
bash "$(dirname "$0")/live-phase-emit.sh" || true

# ── DDB runner-claim heartbeat (single-runner lock §4). Advances the cross-machine
# DDB heartbeat_at alongside the local mtime above so a peer machine's
# stale-lock-break (OWNERSHIP_STALE_SECONDS) never reclaims a LIVE runner. Gated on
# STORAGE_BACKEND=own-cloud — the ONLY signal (no OWNERSHIP_MODE flag; removed
# 2026-07-02 ) — so the local backend keeps THIS script IRREDUCIBLY
# LOCAL: NO subprocess, NO daemon hop. STORAGE_BACKEND is not exported into the
# agent shell, so resolve it the same cheap way check-prerequisites.sh does: live
# env first, else one grepped .env.local line (no secret sourcing). When own-cloud,
# runner-claim.sh routes through the LOCALHOST daemon (the annotation's stated
# maximum), fail-open (|| true) so a DDB hiccup can NEVER block an iteration.
_HB_BACKEND="${STORAGE_BACKEND:-}"
if [ -z "$_HB_BACKEND" ] && [ -f "$PROJECT_ROOT/.env.local" ]; then
    _HB_BACKEND="$(grep -E '^[[:space:]]*STORAGE_BACKEND[[:space:]]*=' "$PROJECT_ROOT/.env.local" 2>/dev/null \
        | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
fi
_HB_BACKEND="$(printf '%s' "$_HB_BACKEND" | tr '[:upper:]' '[:lower:]')"
if [ "$_HB_BACKEND" = "own-cloud" ]; then
    # STILL fail-open (a DDB hiccup must never block an iteration) but NO LONGER
    # SILENT. `|| true` alone discarded the rc, so this leg could die while the
    # team-state leg above kept succeeding — two legs of ONE tick failing
    # independently, with only the surviving one visible. Measured 2026-08-05
    # (): alpha's DDB leg stopped ~105 min before the agent did; the
    # claim aged past OWNERSHIP_STALE_SECONDS while the reducer was healthily
    # executing goals, so two later `/start`s each saw a "free" claim and came up
    # as REDUCERS instead of workers. Role derivation did not malfunction — it
    # was handed a stale fact. A silenced command is ZERO signals, not one
    # (verify-before-assuming.md rule 4).
    #
    # guard-775 (verified before wiring this rc branch): runner-claim.sh DOES
    # exit non-zero on the failures that matter — daemon error -> exit 1,
    # daemon unreachable -> rt_no_daemon_error, unexpected rc -> exit rc. The
    # branch is real, not a no-op against an internally-fail-open script.
    #
    # guard-772: a stderr-only warning is INVISIBLE when the tick runs inside a
    # backgrounded Bash call, which is the normal case. The durable marker file
    # is therefore the primary signal and stderr is the convenience copy.
    _HB_MARK="$AGENT_DIR/session/claim-heartbeat-failure"
    _HB_RC=0
    _HB_ERR="$(bash "$(dirname "$0")/runner-claim.sh" heartbeat --agent "$MIND_AGENT" 2>&1)" || _HB_RC=$?
    if [ "$_HB_RC" -eq 0 ]; then
        # Recovered (or never broken): clear the marker so `count` always means
        # CONSECUTIVE failures. rb-4842 — a stability signal must reset on any
        # success, else one old blip reads as an ongoing outage forever.
        rm -f "$_HB_MARK" 2>/dev/null || true
    else
        _HB_NOW="$(date +%s)"
        _HB_FIRST="$_HB_NOW"; _HB_CNT=0
        if [ -r "$_HB_MARK" ]; then
            _HB_FIRST="$(sed -n 's/^first_failed_at=//p' "$_HB_MARK" 2>/dev/null | head -1)"
            _HB_CNT="$(sed -n 's/^count=//p' "$_HB_MARK" 2>/dev/null | head -1)"
            # A corrupt/half-written marker must not crash the tick under
            # `set -euo pipefail`, nor silently poison the arithmetic below.
            case "$_HB_FIRST" in ''|*[!0-9]*) _HB_FIRST="$_HB_NOW";; esac
            case "$_HB_CNT"   in ''|*[!0-9]*) _HB_CNT=0;; esac
        fi
        _HB_CNT=$(( _HB_CNT + 1 ))
        _HB_ELAPSED=$(( _HB_NOW - _HB_FIRST ))
        printf 'first_failed_at=%s\ncount=%s\nlast_rc=%s\nlast_error=%s\n' \
            "$_HB_FIRST" "$_HB_CNT" "$_HB_RC" \
            "$(printf '%s' "$_HB_ERR" | tr '\n' ' ' | cut -c1-300)" \
            > "$_HB_MARK" 2>/dev/null || true
        # Mirrors the OWNERSHIP_STALE_SECONDS knob (default 3900s = 65min;
        # aspirations.yaml + owncloud_backend.py carry the SSOT). Warn at half
        # the window so there is runway to act BEFORE a peer may legally take
        # the claim.
        _HB_STALE="${OWNERSHIP_STALE_SECONDS:-3900}"
        case "$_HB_STALE" in ''|*[!0-9]*) _HB_STALE=3900;; esac
        if [ "$_HB_ELAPSED" -ge $(( _HB_STALE / 2 )) ]; then
            echo "[heartbeat-tick] ═══ CLAIM HEARTBEAT FAILING ${_HB_ELAPSED}s (${_HB_CNT} consecutive) ═══" >&2
            echo "[heartbeat-tick] This runner's DDB claim is NOT being renewed. At ${_HB_STALE}s any /start on" >&2
            echo "[heartbeat-tick] another box will see a STALE claim and come up as a SECOND REDUCER — while this" >&2
            echo "[heartbeat-tick] one keeps running. Fix the daemon/DDB path now, or stop this runner." >&2
            echo "[heartbeat-tick] last_rc=${_HB_RC} detail: ${_HB_ERR}" >&2
            echo "[heartbeat-tick] marker: $_HB_MARK" >&2
        else
            echo "[heartbeat-tick] WARN: DDB claim heartbeat failed (rc=${_HB_RC}, ${_HB_CNT} consecutive, ${_HB_ELAPSED}s) — claim ages out at ${_HB_STALE}s. detail: ${_HB_ERR}" >&2
        fi
        unset _HB_NOW _HB_FIRST _HB_CNT _HB_ELAPSED _HB_STALE
    fi
    unset _HB_MARK _HB_RC _HB_ERR

    # ── Reducer self-fencing (). The block above makes a renewal
    # failure VISIBLE; visibility tells a HUMAN, and this tells the SYSTEM to
    # defend itself. The lease had T_takeover=3900s and T_stepdown=INFINITY —
    # inverted from safe — so any renewal gap past T_takeover produced a zombie
    # leader instead of a clean handover (cc-04, 2026-08-05: claim lost at 14:38,
    # kept executing as reducer 2.5+ hours while two peers acquired it).
    #
    # Deliberately OUTSIDE the rc branch above: the decisive signal is
    # "a DIFFERENT machine holds the live claim", which is read from `status` and
    # is fully compatible with THIS box's heartbeat call having just returned 0.
    # Gating the fence on a local failure would miss exactly the case that
    # motivated it.
    #
    # Fail-open (|| true) on the same reasoning as the heartbeat leg: a fence
    # that cannot decide must never block an iteration. It is NOT silenced,
    # though — the script writes a durable marker and loud stderr before it
    # stops anything, and every internal failure path exits 0 without writing.
    bash "$(dirname "$0")/reducer-self-fence.sh" || true
fi
