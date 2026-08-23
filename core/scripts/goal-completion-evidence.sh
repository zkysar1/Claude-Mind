#!/usr/bin/env bash
# goal-completion-evidence.sh <goal-id>
#
# Answers: "is goal <id> already durably done?" for the Phase -1.4 staleness guard.
# Prints JSON: {"status":"...", "journal_entries":N, "experience_entries":N, "has_evidence":bool}
#
# CRITICAL: has_evidence=true causes Phase -1.4 to SKIP the stale-revert and
# run reconstruction instead. The rule is:
#
#   has_evidence := (status == "completed")
#
# Journal/experience counts are DIAGNOSTIC ONLY — they are reported for log
# visibility but MUST NOT widen the gate. Recurring goals (e.g. recurring email
# checks) have many historical journal entries but status=pending; treating
# those as evidence would silently resurrect the old iteration.
#
# No fallbacks: if aspirations-query, experience-read, or the journal parse
# fails, set -e kills this script. Phase -1.4 then errors out loudly —
# preferable to silently returning has_evidence=false and reverting a completed
# goal (which was the whole bug we're fixing).

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

GOAL_NORMALIZE_TARGET=positional source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"
GOAL_ID="${1:?Usage: goal-completion-evidence.sh <goal-id>}"

# Goal status — aspirations-query exits 0 with [] when not found.
STATUS=$(bash "$CORE_ROOT/scripts/aspirations-query.sh" \
  --goal-field id "$GOAL_ID" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d[0].get('status','') if isinstance(d, list) and d else '')
")

# Journal entries — journal-read.sh has no --goal filter, so we parse the
# JSONL directly. Missing file is a legitimate empty state for fresh agents.
# A malformed JSONL line is a real bug and we want it to surface (no try).
JOURNAL_FILE="$AGENT_DIR/journal.jsonl"
JCOUNT=$(python3 -c "
import sys, json, os
path, needle = sys.argv[1], sys.argv[2]
if not os.path.exists(path):
    print(0); sys.exit(0)
count = 0
with open(path, 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get('goal_id') == needle or needle in (rec.get('goals_completed') or []):
            count += 1
print(count)
" "$JOURNAL_FILE" "$GOAL_ID")

# Experience entries — --goal returns [] on unknown, exit 0.
ECOUNT=$(bash "$CORE_ROOT/scripts/experience-read.sh" --goal "$GOAL_ID" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
entries = d if isinstance(d, list) else d.get('entries', [])
print(len(entries))
")

HAS="false"
if [ "$STATUS" = "completed" ]; then
  HAS="true"
fi

printf '{"status":"%s","journal_entries":%s,"experience_entries":%s,"has_evidence":%s}\n' \
  "${STATUS:-unknown}" "$JCOUNT" "$ECOUNT" "$HAS"
