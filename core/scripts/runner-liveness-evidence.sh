#!/usr/bin/env bash
# runner-liveness-evidence.sh — POSITIVE life/death evidence for an agent's
# autonomous runner, independent of the heartbeat and the diary ().
#
# WHY THIS EXISTS (2026-09-01). The crashed-runner gate's six conditions are all
# ABSENCE-of-activity signals: no heartbeat tick, no diary write, no stop-hook
# BLOCK, no stop signal, no background job. A multi-hour provider rate-limit
# backoff produces exactly that shape on a LIVE loop — the process is asleep in
# a retry, so nothing writes — and on 2026-09-01 the gate demoted a live,
# rate-limited reducer to IDLE and wiped its runner manifest while it was
# demonstrably executing. This probe asks the complementary question the gate
# never asked: is there POSITIVE evidence the runner PROCESS is alive, or
# POSITIVE evidence it is dead? Silence is neither.
#
# Consumers:
#   runner-dead-check.sh  — condition 2 when the heartbeat is ABSENT (a missing
#                           file is inert and needs a positive DEATH signal to
#                           stand in for it), and condition 5, the pre-kill
#                           re-check, where any positive LIFE signal vetoes the
#                           kill (guard-2364: re-derive before destructive
#                           automation acts).
#   recovery-gate.sh      — indirectly, through runner-dead-check.sh; the JSON
#                           rides into recovery-log.jsonl as the firing's evidence.
#
# Inputs:
#   MIND_AGENT (env, required)
#   Optional env overrides (tests and per-deployment wiring):
#     RUNNER_TRANSCRIPTS_DIR   forwarded to assistant-turn-freshness.py --transcripts-dir
#     ZAKCODE_HOME, ASSISTANT_TURN_FRESH_MINUTES   honored by assistant-turn-freshness.py
#     SIDECAR_MARKER_FILE      zakcode sidecar ".current-session" marker
#                              (default: $PROJECT_ROOT/.current-session)
#     SIDECAR_HEALTH_URL       when set, a 2xx from it (curl) while the marker names
#                              the runner SID is life evidence
#     PROVIDER_RETRY_LOG       a log file; a recent tail mentioning rate-limit/retry
#                              activity is life evidence
#     PROVIDER_RETRY_WINDOW_MINUTES  freshness window for that log (default 240)
#
# Probes (each independently fail-open to "nothing to say"; none may raise):
#   runner_proc     session/runner-proc stamp "<pid>:<starttime>" (written by
#                   runner-identity-check.sh each iteration on Linux). Owner alive
#                   -> LIFE; stamp present but owner gone or recycled -> DEATH
#                   (guard-5056: pid AND start time, never pid alone).
#   assistant_turn  assistant-turn-freshness.py (Claude Code transcript or zakcode
#                   session document). Recent turn -> LIFE; verdict
#                   no_recent_assistant_turn (an EXISTING transcript measured stale)
#                   -> DEATH; unreadable (rc=2) -> UNREADABLE; absent -> nothing.
#   sidecar         the sidecar marker names the runner SID AND (health URL 2xx OR a
#                   live zakcode process whose cwd is this project root) -> LIFE.
#   provider_retry  PROVIDER_RETRY_LOG modified inside the window and its tail shows
#                   rate-limit/retry activity -> LIFE.
#
# Output: one JSON object on stdout, always. Exit codes:
#   0  ALIVE      >= 1 positive life signal            (callers VETO recovery)
#   1  UNKNOWN    no positive signal either way         (callers decide on the
#                                                        other conditions)
#   2  UNREADABLE no life signal, but a probe hit an artifact it could not read
#                 (callers SUPPRESS recovery — guard-487 fail-closed-as-suppressed)
#   3  DEAD       no life, no unreadable, >= 1 positive death signal
# A missing MIND_AGENT or agent dir is exit 1 with an "error" key — a broken
# probe must never manufacture a death verdict (guard-4220).

set -u

agent="${MIND_AGENT:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_paths.sh
source "$SCRIPT_DIR/_paths.sh"
# shellcheck source=_runner_proc.sh
source "$SCRIPT_DIR/_runner_proc.sh"

if [[ -z "$agent" ]]; then
    printf '{"error": "MIND_AGENT not set", "verdict": "unknown"}\n'
    exit 1
fi
adir="$(agent_dir "$agent")"
if [[ ! -d "$adir/session" ]]; then
    printf '{"error": "agent session dir not initialized", "agent": "%s", "verdict": "unknown"}\n' "$agent"
    exit 1
fi

sid="$(cat "$adir/session/running-session-id" 2>/dev/null | tr -d '\r\n')"

life=()        # names of positive life signals
death=()       # names of positive death signals
unreadable=()  # names of probes that hit an unreadable artifact
d_proc=""; d_turn=""; d_sidecar=""; d_retry=""

# ── Probe 1: runner-proc stamp (pid:starttime) ────────────────────────────────
stamp="$(cat "$adir/session/runner-proc" 2>/dev/null | tr -d '\r\n' | head -n1)"
if [[ -n "$stamp" && -d /proc ]]; then
    if _owner_alive "$stamp"; then
        life+=("runner_proc_alive"); d_proc="stamp $stamp: process alive (same pid + start time)"
    else
        death+=("runner_proc_dead"); d_proc="stamp $stamp: process gone or pid recycled"
    fi
elif [[ -n "$stamp" ]]; then
    d_proc="stamp $stamp present but no /proc on this box - not evaluated"
else
    d_proc="no stamp"
fi

# ── Probe 2: assistant-turn freshness (transcript / zakcode session doc) ──────
at_args=(--agent-dir "$adir")
[[ -n "${RUNNER_TRANSCRIPTS_DIR:-}" ]] && at_args+=(--transcripts-dir "$RUNNER_TRANSCRIPTS_DIR")
# stdout captured BARE (guard-4129) — the JSON verdict is the value; stderr is left alone.
at_json="$(python3 "$SCRIPT_DIR/assistant-turn-freshness.py" "${at_args[@]}")"
at_rc=$?
at_verdict="$(RLE_AT_JSON="$at_json" python3 -c 'import json,os
try:
    print(json.loads(os.environ.get("RLE_AT_JSON") or "{}").get("verdict",""))
except Exception:
    print("")' 2>/dev/null)"
case "$at_rc:$at_verdict" in
    0:*)                          life+=("recent_assistant_turn");  d_turn="$at_verdict" ;;
    2:*)                          unreadable+=("assistant_turn");   d_turn="unreadable: $at_verdict" ;;
    1:no_recent_assistant_turn)   death+=("assistant_turn_stale");  d_turn="transcript exists, newest assistant turn past threshold" ;;
    *)                            d_turn="${at_verdict:-probe rc=$at_rc} (nothing to say)" ;;
esac

# ── Probe 3: zakcode sidecar marker + live sidecar ────────────────────────────
marker="${SIDECAR_MARKER_FILE:-$PROJECT_ROOT/.current-session}"
marker_sid="$(cat "$marker" 2>/dev/null | tr -d '\r\n' | head -n1)"
if [[ -n "$sid" && -n "$marker_sid" && "$marker_sid" == "$sid" ]]; then
    sidecar_live=""
    if [[ -n "${SIDECAR_HEALTH_URL:-}" ]] && command -v curl >/dev/null 2>&1; then
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$SIDECAR_HEALTH_URL" 2>/dev/null || echo 000)"
        [[ "$code" =~ ^2[0-9][0-9]$ ]] && sidecar_live="health url $SIDECAR_HEALTH_URL -> $code"
    fi
    if [[ -z "$sidecar_live" && -d /proc ]]; then
        # A zakcode process whose cwd IS this project root is the driver that
        # writes the marker — a name match alone would be too loose (guard-5056).
        for cl in /proc/[0-9]*/cmdline; do
            pid_dir="${cl%/cmdline}"
            if tr '\0' ' ' < "$cl" 2>/dev/null | grep -q 'zakcode'; then
                cwd="$(readlink "$pid_dir/cwd" 2>/dev/null || true)"
                if [[ -n "$cwd" && "$cwd" == "$PROJECT_ROOT" ]]; then
                    sidecar_live="zakcode process ${pid_dir#/proc/} cwd=$cwd"
                    break
                fi
            fi
        done
    fi
    if [[ -n "$sidecar_live" ]]; then
        life+=("sidecar_active_session"); d_sidecar="marker names $sid; $sidecar_live"
    else
        d_sidecar="marker names $sid but no live sidecar found (nothing to say)"
    fi
elif [[ -n "$marker_sid" ]]; then
    d_sidecar="marker names a different session (${marker_sid:0:8})"
else
    d_sidecar="no marker"
fi

# ── Probe 4: provider-retry activity in a configured log ─────────────────────
plog="${PROVIDER_RETRY_LOG:-}"
if [[ -n "$plog" && -f "$plog" ]]; then
    win="${PROVIDER_RETRY_WINDOW_MINUTES:-240}"
    if [[ -n "$(find "$plog" -maxdepth 0 -mmin "-$win" 2>/dev/null)" ]]; then
        if tail -c 65536 "$plog" 2>/dev/null | grep -qiE 'rate.?limit|retry'; then
            life+=("provider_retry_activity"); d_retry="$plog modified within ${win}min and shows retry activity"
        else
            d_retry="$plog fresh but no retry lines in tail"
        fi
    else
        d_retry="$plog older than ${win}min"
    fi
else
    d_retry="no log configured"
fi

# ── Verdict ───────────────────────────────────────────────────────────────────
if   [[ ${#life[@]} -gt 0 ]];       then verdict="alive";      rc=0
elif [[ ${#unreadable[@]} -gt 0 ]]; then verdict="unreadable"; rc=2
elif [[ ${#death[@]} -gt 0 ]];      then verdict="dead";       rc=3
else                                     verdict="unknown";    rc=1
fi

RLE_AGENT="$agent" RLE_SID="$sid" RLE_VERDICT="$verdict" \
RLE_LIFE="$(IFS=,; echo "${life[*]-}")" RLE_DEATH="$(IFS=,; echo "${death[*]-}")" \
RLE_UNREADABLE="$(IFS=,; echo "${unreadable[*]-}")" \
RLE_D_PROC="$d_proc" RLE_D_TURN="$d_turn" RLE_D_SIDECAR="$d_sidecar" RLE_D_RETRY="$d_retry" \
python3 -c 'import json, os
def lst(k):
    v = os.environ.get(k, "")
    return [x for x in v.split(",") if x]
print(json.dumps({
    "agent": os.environ["RLE_AGENT"],
    "sid": os.environ["RLE_SID"] or None,
    "verdict": os.environ["RLE_VERDICT"],
    "life": lst("RLE_LIFE"),
    "death": lst("RLE_DEATH"),
    "unreadable": lst("RLE_UNREADABLE"),
    "probes": {
        "runner_proc": os.environ["RLE_D_PROC"],
        "assistant_turn": os.environ["RLE_D_TURN"],
        "sidecar": os.environ["RLE_D_SIDECAR"],
        "provider_retry": os.environ["RLE_D_RETRY"],
    },
}))'
exit "$rc"
