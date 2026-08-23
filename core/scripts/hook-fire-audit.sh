#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# hook-fire-audit.sh — report last-fire timestamps for each entry-sentinel hook.
#
# Reads <PROJECT_ROOT>/core/logs/hook-fires/<hook-name> (one file per hook, mtime =
# last fire time) and produces a structured report. Companion to the
# entry-sentinel writes added to 6 critical hooks under .
#
# Usage:
#   bash core/scripts/hook-fire-audit.sh           # JSON output (default)
#   bash core/scripts/hook-fire-audit.sh --plain   # human-readable table
#   bash core/scripts/hook-fire-audit.sh --stale-minutes 60   # flag entries older than 60min
#
# Exits 0 in all cases. Missing core/logs/hook-fires dir => all hooks "NEVER" (sentinel never written).
# Per-hook missing sentinel is normal for hooks that haven't fired since
# the sentinel was added.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

# Six hooks instrumented by . Order matches investigate-2.1.139 risk
# severity (HIGH first).  / zeta allowlist audit S1: this is a DELIBERATE
# instrumentation subset, NOT a mirror of the .claude/settings.json hook set
# (25+ hooks) -- a parity test against settings.json would false-fail, and 2 of
# these 6 (recovery-gate, session-save-id) are not even settings.json string
# tokens (the names are fire-log sentinel filenames under core/logs/hook-fires/).
# Growing this list is a judgment call about which hooks warrant fire-auditing,
# not a sync obligation. => document, no test.
HOOKS=(
    path-resolution-hook
    session-manifest-write-gate-hook
    recovery-gate
    stop-hook
    session-save-id
    context-reads-gate
)

HOOKDIR="$PROJECT_ROOT/core/logs/hook-fires"
PLAIN=0
STALE_MIN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --plain) PLAIN=1; shift ;;
        --stale-minutes) STALE_MIN="${2:-0}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        *) echo "Unknown arg: $1" >&2; exit 0 ;;
    esac
done

if [[ "$PLAIN" -eq 1 ]]; then
    printf "%-40s %-21s %s\n" "HOOK" "LAST_FIRE" "AGE"
    for hook in "${HOOKS[@]}"; do
        f="$HOOKDIR/$hook"
        if [[ -f "$f" ]]; then
            mtime=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0)
            now=$(date +%s)
            age=$(( now - mtime ))
            ts=$(HF_MTIME="$mtime" py -3 -c "import os,time; print(time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(int(os.environ['HF_MTIME']))))" 2>/dev/null || echo "?")
            if [[ "$STALE_MIN" -gt 0 ]] && [[ "$age" -gt $((STALE_MIN * 60)) ]]; then
                printf "%-40s %-21s %ds ago STALE\n" "$hook" "$ts" "$age"
            else
                printf "%-40s %-21s %ds ago\n" "$hook" "$ts" "$age"
            fi
        else
            printf "%-40s %-21s %s\n" "$hook" "NEVER" "(no sentinel)"
        fi
    done
    exit 0
fi

# JSON output (default).
HOOKS_LIST="$(printf '%s\n' "${HOOKS[@]}")"
HOOKDIR="$HOOKDIR" HOOKS_LIST="$HOOKS_LIST" STALE_MIN="$STALE_MIN" \
    AGENT_NAME="${MIND_AGENT:-}" \
    py -3 - <<'PYEOF'
import json
import os
import time
import sys

hookdir = os.environ['HOOKDIR']
hooks = [h.strip() for h in os.environ['HOOKS_LIST'].strip().split('\n') if h.strip()]
stale_min = int(os.environ.get('STALE_MIN', '0') or '0')
agent = os.environ.get('AGENT_NAME', '')

now = time.time()
result = []
for hook in hooks:
    f = os.path.join(hookdir, hook)
    if os.path.exists(f):
        mtime = os.path.getmtime(f)
        age_s = int(now - mtime)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))
        is_stale = (stale_min > 0) and (age_s > stale_min * 60)
        result.append({
            "hook": hook,
            "last_fire": ts,
            "age_seconds": age_s,
            "sentinel_exists": True,
            "stale": is_stale,
        })
    else:
        result.append({
            "hook": hook,
            "last_fire": None,
            "age_seconds": None,
            "sentinel_exists": False,
            "stale": None,
        })

print(json.dumps({
    "audited_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "audited_for_agent": agent or None,
    "hookdir": hookdir,
    "stale_threshold_minutes": stale_min if stale_min > 0 else None,
    "hooks": result,
}, indent=2))
PYEOF
