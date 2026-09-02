#!/usr/bin/env bash
# recovery-yank-reverse.sh — reducer-side reversal of a FALSE recovery-gate demotion
# ( part 3; the 2026-09-01 fleet-wide rate-limited-alive kill).
#
# CALLER: core/scripts/stop-hook.sh Gate 1-pre — the turn-ending session found
# agent-state != RUNNING while a session/recovery-log.jsonl exists. A process that
# reaches its own stop hook is alive BY CONSTRUCTION, so if recovery-gate.sh demoted
# THIS sid (RUNNING->IDLE + runner manifest wiped) the demotion was false. Before this
# script the yanked-but-alive reducer died silently right there: the hook read IDLE,
# allowed the turn-end, and the loop was gone.
#
# GATE: every precondition in recovery_yank.py::evaluate_preconditions must hold —
# this sid IS the demoted runner (sid_recorded), bound autonomous BEFORE the yank,
# the yank is inside RECOVERY_YANK_REVERSE_WINDOW_MINUTES (default 360), not already
# reversed, no user-stop artifact post-dates it (stop-requested / stop-loop /
# stop-target-mode / a user-stop reason file / a newer handoff.yaml), and
# running-session-id is empty or already this sid. Any miss => rc=1, NO-OP. A wrong
# restore is the unrecoverable direction (dual reducers), so every ambiguity
# resolves to no-op.
#
# RESTORE (mirrors /start IDLE Step 3, the canonical runner claim):
#   1. runner triple-write: running-session-id + latest-session-id = sid, NEW runner-token
#   2. runner-claim.sh acquire — rc=4 (a peer holds the claim: a takeover happened)
#      ABORTS and rolls the triple back
#   3. heartbeat-tick.sh --bypass-state (seeds heartbeat + body carrier, so the next
#      recovery-gate pass reads fresh instead of absent)
#   4. session-state-set.sh RUNNING — an authorized caller; see
#      .claude/rules/user-interaction.md Script-Level Restrictions
#   5. fail-open bookkeeping: recovery-log `yank_reversed` entry + recovery-notice
#      rewrite (recovery_yank.py record-reversal), clear the yank's EXPECTED-IDLE
#      stop-reason, mirror `reversed_at` into team-state agent_status.<agent>
#      .last_recovery, board post.
#
# rc: 0 reversed (state RUNNING) | 1 preconditions not met, nothing written
#     | 2 restore attempted and failed (rolled back where possible; see stderr)
# Usage: recovery-yank-reverse.sh --agent <name> --sid <sid> [--dry-run] [--now <iso>]
#   --dry-run prints the precondition JSON and exits 0/1 without writing anything.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_paths.sh
source "$SCRIPT_DIR/_paths.sh"

AGENT=""; SID=""; DRY=0; NOW=""
while [ $# -gt 0 ]; do
    case "$1" in
        --agent)   AGENT="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --sid)     SID="${2:-}";   shift $(( $# >= 2 ? 2 : 1 )) ;;
        --now)     NOW="${2:-}";   shift $(( $# >= 2 ? 2 : 1 )) ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) sed -n '2,37p' "$0"; exit 0 ;;
        *) echo "[recovery-yank-reverse] unknown argument: $1" >&2; exit 1 ;;
    esac
done
if [ -z "$AGENT" ] || [ -z "$SID" ]; then
    echo "usage: recovery-yank-reverse.sh --agent <name> --sid <sid> [--dry-run] [--now <iso>]" >&2
    exit 1
fi
export MIND_AGENT="$AGENT"
AGENT_DIR="$(agent_dir "$AGENT")"
SESS="$AGENT_DIR/session"
PY="$SCRIPT_DIR/recovery_yank.py"
if command -v timeout >/dev/null 2>&1; then TMO="timeout 45"; else TMO=""; fi
log() { echo "[recovery-yank-reverse] $*" >&2; }
# Dotted-key extractor over JSON on stdin (guard-165: the key rides in ENV, never
# interpolated into the python source). Prints "" for a missing key.
json_field() {
    RYR_KEY="$1" python3 -c 'import json, os, sys
v = json.load(sys.stdin)
for k in os.environ["RYR_KEY"].split("."):
    v = v.get(k) if isinstance(v, dict) else None
print("" if v is None else (v if isinstance(v, str) else json.dumps(v)))' 2>/dev/null
}

# ── gate ─────────────────────────────────────────────────────────────────────
pre="$(python3 "$PY" preconditions --agent "$AGENT" --sid "$SID" ${NOW:+--now "$NOW"} 2>/dev/null)"
pre_rc=$?
case "$pre_rc" in
    0) ;;
    1) log "preconditions not met — no-op: $(printf '%s' "$pre" | json_field reasons)"
       [ "$DRY" = 1 ] && printf '%s\n' "$pre"
       exit 1 ;;
    *) log "precondition probe failed rc=$pre_rc — no-op (a failed probe never manufactures a verdict)"
       exit 1 ;;
esac
yank_ts="$(printf '%s' "$pre" | json_field yank.ts)"
yank_path="$(printf '%s' "$pre" | json_field yank.path)"
if [ "$DRY" = 1 ]; then
    log "DRY RUN: would reverse the ${yank_path:-?} demotion of ${yank_ts:-?} for sid $SID"
    printf '%s\n' "$pre"
    exit 0
fi

# ── restore ──────────────────────────────────────────────────────────────────
token="$(python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null)"
if [ -z "$token" ]; then
    log "runner-token generation failed — abort before any write"
    exit 2
fi
mkdir -p "$SESS"
claimed=0
rollback() {
    local f
    for f in running-session-id latest-session-id runner-token; do
        if [ -f "$SESS/$f.pre-reverse" ]; then
            mv -f "$SESS/$f.pre-reverse" "$SESS/$f"
        else
            rm -f "$SESS/$f"
        fi
    done
    if [ "$claimed" = 1 ]; then
        $TMO bash "$SCRIPT_DIR/runner-claim.sh" release --agent "$AGENT" >/dev/null 2>&1 || true
    fi
}
for f in running-session-id latest-session-id runner-token; do
    [ -f "$SESS/$f" ] && cp -f "$SESS/$f" "$SESS/$f.pre-reverse"
done
if ! {  printf '%s\n' "$SID"   > "$SESS/running-session-id.tmp" && mv -f "$SESS/running-session-id.tmp" "$SESS/running-session-id" \
     && printf '%s\n' "$SID"   > "$SESS/latest-session-id.tmp"  && mv -f "$SESS/latest-session-id.tmp"  "$SESS/latest-session-id" \
     && printf '%s\n' "$token" > "$SESS/runner-token.tmp"       && mv -f "$SESS/runner-token.tmp"       "$SESS/runner-token"; }; then
    log "runner triple-write failed — rolling back"
    rollback
    exit 2
fi
if [ -f "$SCRIPT_DIR/runner-claim.sh" ]; then
    $TMO bash "$SCRIPT_DIR/runner-claim.sh" acquire --agent "$AGENT" >/dev/null 2>&1
    crc=$?
    case "$crc" in
        0) claimed=1 ;;
        4) log "runner claim is held by a peer — a takeover happened since the yank; abort + roll back"
           rollback
           exit 1 ;;
        *) log "runner-claim acquire rc=$crc (non-fatal: local backend or daemon blip) — continuing" ;;
    esac
fi
MIND_SID="$SID" $TMO bash "$SCRIPT_DIR/heartbeat-tick.sh" --bypass-state >/dev/null 2>&1 \
    || log "heartbeat seed failed (non-fatal — the next tick writes it)"
if ! MIND_SID="$SID" bash "$SCRIPT_DIR/session-state-set.sh" RUNNING >/dev/null 2>&1; then
    log "session-state-set RUNNING failed — rolling back"
    rollback
    exit 2
fi
rm -f "$SESS/running-session-id.pre-reverse" "$SESS/latest-session-id.pre-reverse" "$SESS/runner-token.pre-reverse" 2>/dev/null || true

# ── record (fail-open from here: the restore is done) ────────────────────────
rec="$(python3 "$PY" record-reversal --agent "$AGENT" --sid "$SID" ${NOW:+--now "$NOW"} 2>/dev/null)" \
    || log "record-reversal failed — state restored but the yank_reversed audit entry is missing"
marker="$(printf '%s' "${rec:-}" | json_field team_state_marker)"
python3 "$SCRIPT_DIR/stop-reason-record.py" --clear --agent "$AGENT" >/dev/null 2>&1 || true
if [ -n "$marker" ] && [ -f "$SCRIPT_DIR/team-state-update.sh" ]; then
    $TMO bash "$SCRIPT_DIR/team-state-update.sh" --field "agent_status.$AGENT.last_recovery" --value "$marker" >/dev/null 2>&1 || true
fi
if [ -f "$SCRIPT_DIR/board-post.sh" ]; then
    printf 'recovery-yank REVERSED for %s: recovery-gate.sh path %s demoted the live reducer (sid %s) at %s; the session reached its stop hook alive and restored RUNNING (runner-token rotated, claim re-acquired). If this reversal is wrong: /stop %s. Audit: session/recovery-log.jsonl action=yank_reversed.' \
        "$AGENT" "${yank_path:-?}" "$SID" "${yank_ts:-?}" "$AGENT" \
        | $TMO bash "$SCRIPT_DIR/board-post.sh" --channel coordination --type finding --tags "recovery-yank,reversed,g-357-51" >/dev/null 2>&1 || true
fi
log "REVERSED: ${yank_path:-?} demotion of ${yank_ts:-?} — sid $SID restored to RUNNING"
exit 0
