#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit ().
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/sessionstart-orchestrator" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# sessionstart-orchestrator.sh — SINGLE entry point for all SessionStart hooks.
#
# WHY THIS EXISTS
# ---------------
# Claude Code fires hooks listed in settings.json IN PARALLEL, not in
# registration order. Two hooks listed sequentially can complete in either
# order depending on system load. Observed evidence:
#   - 2026-05-12T08:45:54.237  recovery-gate.sh fired
#   - 2026-05-12T08:45:54.370  session-save-id.sh fired  (133ms LATER)
#   - 2026-05-12T09:54:39.943  recovery-gate.sh fired
#   - 2026-05-12T09:54:39.968  session-save-id.sh fired  (25ms LATER)
# (Source: core/logs/hook-fires/ sentinel mtimes.)
#
# When recovery-gate's Path B (state-corruption detection) runs BEFORE
# session-save-id has had a chance to consume the compact-pending breadcrumb
# and restore running-session-id, Path B observes (state=RUNNING,
# running-session-id missing, no stop-requested) and demotes the agent —
# false-positive recovery. zeta 2026-05-12T08:45:54 incident.
#
# This orchestrator REPLACES the parallel-fire registration with a SINGLE
# deterministically-ordered call chain. The previous registrations in
# settings.json (session-save-id.sh + recovery-gate.sh in one array, then
# postcompact-restore.sh + idle-tick.sh in another) all collapse to ONE
# entry pointing at this script.
#
# ORDERING RATIONALE
# ------------------
# 1. session-save-id.sh — establishes runner identity. Consumes the
#    compact-pending breadcrumb (autocompact resume), atomic-writes
#    running-session-id + latest-session-id. Everything downstream depends
#    on this having run first.
#
# 2. recovery-gate.sh — crashed-runner / state-corruption / hung-compact
#    detection. Reads the state established by Step 1. Path B's
#    "running-session-id missing" check is now reliable because Step 1
#    either restored the SID (legit autocompact) or left it absent (genuine
#    corruption). Belt-and-suspenders: Path B ALSO checks compact-pending
#    as a second guard (see recovery-gate.sh:_check_state_corruption).
#
# 3. (source=compact only) postcompact-restore.sh — re-inject context after
#    autocompact. Reads running-session-id to discriminate runner vs
#    observer. Requires Step 1 to have updated it.
#
# 4. (source=compact only) idle-tick.sh — sleep-vs-proceed decision. Reads
#    working memory (blocked_sleep_until). Requires the agent to be
#    resolvable from SID, which Step 1's binding work enables.
#
# FAIL-OPEN DISCIPLINE
# --------------------
# Each downstream script's own fail-open contract is preserved. The
# orchestrator NEVER exits non-zero — it would block session start otherwise.
# `set -uo pipefail` (NOT -e) so individual command failures surface in
# logs but do not abort the chain.
#
# STDIN HANDLING
# --------------
# Claude Code delivers a single JSON payload on stdin. Downstream scripts
# parse `session_id` and `source` independently. We capture stdin once and
# re-pipe to each downstream call so every script sees the same payload.
#
# WHAT NOT TO DO (canonical traps)
# --------------------------------
# - Do NOT add parallel execution back. The whole point of this script is
#   serialization. If a downstream script is slow, make IT faster — don't
#   parallelize it with its dependencies.
# - Do NOT add a "skip session-save-id if recovery-gate already ran"
#   short-circuit. The ordering matters in ONE direction: save-id MUST
#   run before recovery-gate. The reverse skip would silently disable
#   crashed-runner detection.
# - Do NOT remove the postcompact-restore/idle-tick source=compact gate.
#   Those two scripts have observer-protection logic that depends on
#   knowing the SessionStart was triggered by autocompact, not by a fresh
#   terminal.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

# Capture stdin once — each downstream script parses it independently.
STDIN_JSON=$(cat 2>/dev/null || echo "{}")
# Use `py -3` first (always works on Windows, avoids MS Store stub when
# bash-agent-inject PATH shim is not yet active during SessionStart),
# fall back to `python3` on POSIX systems. Per rb-370/guard-335/guard-144.
SOURCE=$(printf '%s' "$STDIN_JSON" | (py -3 -c "import sys,json; print(json.load(sys.stdin).get('source',''))" 2>/dev/null || python3 -c "import sys,json; print(json.load(sys.stdin).get('source',''))" 2>/dev/null) || echo "")

# ─── Step 0: mind-api-start.sh ───────────────────────────────────────────────
# Ensure the runtime daemon is running. Idempotent — no-op if already up.
# Runs FIRST so all downstream scripts (and the agent session that follows)
# have a responsive daemon. Fail-open: daemon failure must not block session
# start; wrapper-layer auto-spawn (rt_ensure_running) is the fallback.
bash "$SCRIPT_DIR/mind-api-start.sh" || true

# ─── Step 0.5: install-git-hooks.sh ────────────────────────────────────────
# Idempotent: ensures core.hooksPath -> core/githooks so the Layer B
# pre-commit gate (daemon-only drift defense) is active in this clone.
# Fail-open: a hook-install failure must never block session start.
bash "$SCRIPT_DIR/install-git-hooks.sh" || true

# ─── Step 1: session-save-id.sh ────────────────────────────────────────────
# Establishes runner identity. Must run before recovery-gate so Path B's
# "running-session-id missing" check is not a false positive during a
# normal autocompact resume.
printf '%s' "$STDIN_JSON" | bash "$SCRIPT_DIR/session-save-id.sh" || true

# ─── Step 2: recovery-gate.sh ──────────────────────────────────────────────
# Crashed-runner / state-corruption / hung-compact detection. Reads state
# established by Step 1.
printf '%s' "$STDIN_JSON" | bash "$SCRIPT_DIR/recovery-gate.sh" || true

# ─── Step 2.5: wm-contamination-check.sh ───────────────────────────────────
# REMEDIAL cross-agent WM contamination detector (sibling to the PREVENTIVE
# sid-collision-check.sh). Runs AFTER session-save-id + recovery-gate so the
# binding is resolvable, BEFORE /prime so a contaminated WM is scrubbed before
# the loop restores loop_state from it. Unconditional (NOT source=compact-gated)
# because residual contamination persists in the WM regardless of how this
# SessionStart was triggered. Daemon-independent (reads files directly) and
# fail-open: a detector failure must never block session start. Its stdout is
# silent on a clean WM, a loud quarantine block on detection.
printf '%s' "$STDIN_JSON" | bash "$SCRIPT_DIR/wm-contamination-check.sh" || true

# ─── Step 2.6: local-backend-staleness-check.sh ────────────────────────────
# Warn when a STORAGE_BACKEND=local clone is behind origin, or when another
# machine pushed recently. Under the local backend the git remote is the ONLY
# cross-machine sync point and nothing consulted it at session start; a box was
# measured running two full sessions 457 commits behind, reading every world
# store from a week-stale tree, silently ().
#
# Unconditional, NOT source=compact-gated: staleness is a property of the clone,
# not of how this SessionStart was triggered — a fresh terminal on a stale box is
# exactly the case that went unreported.
#
# Placed AFTER recovery-gate deliberately. It is advisory and must never be able
# to perturb runner identity or crashed-runner detection, so it runs once those
# have settled. It reads nothing they write and writes nothing at all.
#
# Own-cloud boxes return before the fetch, so they pay nothing. Fail-open by
# construction (no network / no upstream / detached HEAD all exit 0 silently),
# and `|| true` keeps the chain's contract even if the script itself is edited
# badly later.
bash "$SCRIPT_DIR/local-backend-staleness-check.sh" || true

# ─── Step 2.7: housekeeping tick (P1, 2026-08-21) ──────────────────────────
# Per-box temp-purge + scratchpad-GC cadence (housekeeping-tick.py). Assistant
# boxes never reach iteration-close's productivity-check, so session start is
# their ONLY cadence surface — this box's whole temp/scratchpad backlog
# accrued exactly that way. The inline cost is a decide (one JSON read +
# mtime compare, milliseconds); when due it stamps and spawns its worker
# DETACHED, so session start never waits on a sweep. Self-gating (6h marker
# in <agent>/session/housekeeping-tick-state.json) makes the double wiring
# with iteration-close a no-op race at worst (idempotent dry-run/find).
# Subshell-sources _paths.sh for the python3 shim guarantee (CLAUDE.md Python
# Invocation rule); cygpath for the native-Python file arg. Fail-open + the
# chain's `|| true` contract: advisory hygiene must never perturb session
# start (this runs AFTER recovery-gate for the same reason the staleness
# check does).
(
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true
  _hk="$SCRIPT_DIR/housekeeping-tick.py"
  command -v cygpath >/dev/null 2>&1 && _hk="$(cygpath -w "$_hk" 2>/dev/null || echo "$_hk")"
  python3 "$_hk" --tick --source session-start
) >/dev/null 2>&1 || true

# ─── Step 2.75: history-store vacuum tick () ─────────────────────
# Per-box .history GC cadence. history-vacuum-tick.sh had EXACTLY ONE caller in
# the tree — iteration-close.sh, inside the reducer-only productivity-check sink
# — and .history is machine-local BY DESIGN (the tick's own header: "every box
# vacuums ITS OWN store"), so no reducer and no partner can ever GC a worker
# box's store. Same structural class as Step 2.7 above,  and :
# a maintenance capability wired only into the reducer's close path.
# MEASURED 2026-08-29 from the zakbox1 LXD host: 231 GB of world/.history across
# 10 Ayoai containers — >=43% of their total container space, a quarter of the
# physical NVMe — while cc-08, the one box the vacuum had ever run on, was the
# SMALLEST at 15 GB. The mechanism works; it did not reach the other nine boxes.
# Session start is the right surface for the same reason Step 2.7 gives, and it
# fires on SOURCE=compact too, so a long-running worker session keeps ticking at
# every autocompact instead of only at startup.
# Scoped CALL to the shared tick (guard-2676), never a reimplementation: the
# tick owns its 24h gate, per-box lock, archive-before-delete receipt and its own
# backgrounding, so the inline cost here is one config probe. It writes its
# verdicts to core/logs/history-vacuum-tick.log, which is why the console output
# is sunk like its sibling. Fail-open + the chain's `|| true` contract: advisory
# hygiene must never perturb session start.
bash "$SCRIPT_DIR/history-vacuum-tick.sh" >/dev/null 2>&1 || true

# ─── Steps 3+4: source=compact only ────────────────────────────────────────
if [ "$SOURCE" = "compact" ]; then
    # ─── Step 2.9: clear the context-reads manifest () ───────────
    # THE GUARANTEED CLEAR. The compaction that just happened evicted file
    # contents from context, so every "already in context" assertion in the
    # tracker is now false: the re-read dedup gate refuses a needed re-read and
    # the skill-dedup gate refuses a needed skill invocation, both silently.
    #
    # WHY HERE AND NOT ONLY IN PreCompact. PreCompact's clear is pre-hoc and
    # cannot be relied on alone — its matcher in settings.json is 'auto', so a
    # manual /compact fires no PreCompact hook at all, and the hook can time out
    # mid-sequence (measured on the filing session: SessionStart reported
    # compact-checkpoint.yaml missing). SessionStart source=compact is the
    # resume event itself: if it does not fire there is no post-compaction turn
    # to protect, so it strictly dominates as the place that must not be missed.
    # It is also the semantically correct moment — clear the manifest BECAUSE
    # the content was evicted, after it was evicted.
    #
    # SOURCE-GATED, and that is load-bearing (guard-404): this runs for compact
    # ONLY, never startup or resume. A startup-side clear would wipe a manifest
    # whose content genuinely IS in context.
    #
    # SESSION-SCOPED, equally load-bearing. --session-id routes through
    # tracker_path(), so a worker Body clears its own
    # sessions/<SID>/body-context-reads.txt and a reducer clears the agent-wide
    # file — never each other's. Dropping the flag would make a co-resident
    # worker's compaction wipe the reducer's manifest: a cross-session
    # shared-state mutation, the exact thing guard-404 forbids.
    #
    # FIRST in the block, before postcompact-restore, so nothing restore does
    # can be undone by the wipe. Fail-open (`|| true`) and stdout suppressed —
    # this block's other two scripts deliberately keep stdout for the LLM, and a
    # bookkeeping line does not belong in that channel.
    _CR_SID=$(printf '%s' "$STDIN_JSON" | (py -3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null) || echo "")
    if [ -n "$_CR_SID" ]; then
        _CR_AGENT=$(python3 "$SCRIPT_DIR/_resolve_agent_from_sid.py" "$_CR_SID" 2>/dev/null || echo "")
        if [ -n "$_CR_AGENT" ]; then
            MIND_AGENT="$_CR_AGENT" MIND_SID="$_CR_SID" \
                bash "$SCRIPT_DIR/context-reads-clear.sh" --session-id "$_CR_SID" \
                >/dev/null 2>&1 || true
        fi
    fi

    # postcompact-restore.sh — re-inject context. Writes its restoration
    # output to stdout for the LLM to read. DO NOT swallow stdout.
    printf '%s' "$STDIN_JSON" | bash "$SCRIPT_DIR/postcompact-restore.sh" || true

    # idle-tick.sh — sleep vs proceed. Also writes ADDITIONAL-CONTEXT
    # directives to stdout. DO NOT swallow stdout.
    printf '%s' "$STDIN_JSON" | bash "$SCRIPT_DIR/idle-tick.sh" || true
fi

exit 0
