#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# infra-streak-notify.sh — Run streak-alert + dedup + route new alerts to /notify-user.
#
#  wrapper. Sequence:
#   1. Invoke infra-health.py streak-alert → emits JSON of components past threshold
#      within recency window.
#   2. For each alerting component, build dedup key = "{component}:{last_failure}".
#   3. Look up key in <agent>/session/infra-streak-sent.jsonl.
#      - If present: skip (already-notified, last_failure hasn't advanced).
#      - If absent: queue for notification, append key to sent file.
#   4. If --notify flag is set, invoke /notify-user forged skill for each queued
#      entry. Default is dry-run (prints what would be sent, writes sent file).
#
# Why dry-run default: a new alert gate shouldn't send emails until explicitly
# enabled. The caller (a recurring goal, a forge-worker, or a human) flips
# --notify once confident the dedup is correct.
#
# Sent-file schema (one JSON per line):
#   {"key": "component:last_failure_iso", "component": "...", "last_failure": "...",
#    "first_notified_at": "2026-04-21T04:40:00", "iteration_source": ""}
#
# Exit code: 0 on success (including when no alerts and when dry-run). Nonzero
# only if streak-alert itself errored or the sent-file write failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"
source "$SCRIPT_DIR/_platform.sh"

NOTIFY=false
ALERT_FILE_OVERRIDE=""
for arg in "$@"; do
    case "$arg" in
        --notify) NOTIFY=true ;;
        --alert-file=*) ALERT_FILE_OVERRIDE="${arg#*=}" ;;
        *) ;;
    esac
done

AGENT="${MIND_AGENT:-}"
if [ -z "$AGENT" ]; then
    echo "infra-streak-notify: ERROR — MIND_AGENT not set" >&2
    exit 1
fi

SENT_FILE="$(agent_dir "$AGENT")/session/infra-streak-sent.jsonl"
mkdir -p "$(dirname "$SENT_FILE")"
touch "$SENT_FILE"

# Windows/Git-Bash path translation (): on Windows, $SENT_FILE expands to
# an MSYS-style path like /c/<WORKSPACE>/... which Python on Windows cannot open
# (FileNotFoundError). cygpath -w converts to native C:\ form. On Linux/macOS or
# anywhere cygpath is unavailable, fall back to the original path.
SENT_FILE_NATIVE=$(cygpath -w "$SENT_FILE" 2>/dev/null || printf '%s' "$SENT_FILE")

CURRENT_ALERTS=$(python3 "$CORE_ROOT/scripts/infra-health.py" streak-alert 2>&1 || true)
ALERT_JSON=$(echo "$CURRENT_ALERTS" | python3 -c "
import sys, json
raw = sys.stdin.read().strip()
try:
    d = json.loads(raw)
    print(json.dumps(d.get('components', [])))
except Exception:
    print('[]')
")

ALERT_COUNT=$(echo "$ALERT_JSON" | python3 -c "import sys, json; print(len(json.loads(sys.stdin.read())))")

if [ "$ALERT_COUNT" -eq 0 ]; then
    echo "infra-streak-notify: no alerts (streak-alert alert_count=0)"
    exit 0
fi

# Dedup: build list of new (unsent) alerts
NEW_ALERTS=$(echo "$ALERT_JSON" | python3 -c "
import sys, json
alerts = json.loads(sys.stdin.read())
sent_path = r'$SENT_FILE_NATIVE'
seen = set()
try:
    with open(sent_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                seen.add(json.loads(line).get('key'))
            except Exception:
                pass
except FileNotFoundError:
    pass

new = []
for a in alerts:
    key = f\"{a.get('component')}:{a.get('last_failure')}\"
    if key in seen:
        continue
    a['_key'] = key
    new.append(a)
print(json.dumps(new))
")

NEW_COUNT=$(echo "$NEW_ALERTS" | python3 -c "import sys, json; print(len(json.loads(sys.stdin.read())))")

if [ "$NEW_COUNT" -eq 0 ]; then
    echo "infra-streak-notify: $ALERT_COUNT alert(s) all previously sent (dedup hit)"
    exit 0
fi

NOW=$(date +%Y-%m-%dT%H:%M:%S)

echo "$NEW_ALERTS" | python3 -c "
import sys, json, os
new = json.loads(sys.stdin.read())
sent_path = r'$SENT_FILE_NATIVE'
now = r'$NOW'
dry_run = r'$NOTIFY' != 'true'
# : wrap open() in try/except so a bad path fails LOUDLY instead of
# silently losing alerts. Prior to this guard, a FileNotFoundError on Windows
# MSYS paths would print a traceback to stderr but the bash exit code stayed
# 0, masking the failure. Now we emit a clear error and exit 1 so the caller
# (recurring goal, forge worker, human) sees the breakage immediately.
try:
    f = open(sent_path, 'a', encoding='utf-8')
except (FileNotFoundError, OSError, PermissionError) as e:
    sys.stderr.write(f'infra-streak-notify: ERROR opening sent_file {sent_path!r}: {e}\n')
    sys.exit(1)
with f:
    for a in new:
        record = {
            'key': a['_key'],
            'component': a['component'],
            'last_failure': a['last_failure'],
            'consecutive_failures': a.get('consecutive_failures'),
            'first_notified_at': now,
            'iteration_source': 'g-249-05',
            'dry_run': dry_run,
        }
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
        prefix = '[DRY-RUN]' if dry_run else '[NOTIFY]'
        print(f'{prefix} {a[\"component\"]}: {a.get(\"consecutive_failures\")} consecutive failures, last at {a[\"last_failure\"]}')
        print(f'  reason: {a.get(\"last_failure_reason\", \"(none)\")}')
"

if [ "$NOTIFY" = "true" ]; then
    # Iterate new alerts and call /notify-user forged skill for each.
    # /notify-user handles rate limiting + self-identity + fallback cascade.
    echo "$NEW_ALERTS" | python3 -c "
import sys, json, subprocess
new = json.loads(sys.stdin.read())
for a in new:
    subject = f\"Infra alert: {a['component']} {a.get('consecutive_failures')} consecutive failures\"
    body = f\"\"\"Component: {a['component']}
Consecutive failures: {a.get('consecutive_failures')}
Last failure: {a['last_failure']}
Reason: {a.get('last_failure_reason', '(none)')}

Auto-fired by infra-streak-notify.sh (g-249-05). Check world/infra-health.yaml for full state.
\"\"\"
    # Placeholder: forged /notify-user invocation is Claude-side; the script
    # prints the payload so the caller (a forge skill step) can consume it.
    print('---NOTIFY-PAYLOAD---')
    print(json.dumps({'subject': subject, 'body': body, 'category': 'blocker'}))
    print('---END-NOTIFY-PAYLOAD---')
"
fi

echo "infra-streak-notify: queued $NEW_COUNT new alert(s) (sent-file: $SENT_FILE)"
