#!/usr/bin/env bash
# rename-agent.sh — Rename an agent directory and update all world/ references
#
# The agent's directory name IS its identity. Renaming requires updating
# all claimed_by/completed_by references in world/aspirations.jsonl.
#
# Usage:
#   bash core/scripts/rename-agent.sh <old-name> <new-name>

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

OLD_NAME="${1:-}"
NEW_NAME="${2:-}"

if [ -z "$OLD_NAME" ] || [ -z "$NEW_NAME" ]; then
    echo "Usage: bash core/scripts/rename-agent.sh <old-name> <new-name>" >&2
    exit 1
fi

OLD_DIR="$PROJECT_ROOT/$OLD_NAME"
NEW_DIR="$PROJECT_ROOT/$NEW_NAME"

# Validate old directory exists
if [ ! -d "$OLD_DIR" ]; then
    echo "ERROR: Agent directory '$OLD_NAME/' does not exist." >&2
    exit 1
fi

# Validate new name doesn't conflict
RESERVED_NAMES="core meta world node_modules .git .claude .github"
for reserved in $RESERVED_NAMES; do
    if [ "$NEW_NAME" = "$reserved" ]; then
        echo "ERROR: '$NEW_NAME' is a reserved name." >&2
        exit 1
    fi
done

if [ -d "$NEW_DIR" ]; then
    echo "ERROR: Directory '$NEW_NAME/' already exists." >&2
    exit 1
fi

# Validate kebab-case
if ! echo "$NEW_NAME" | grep -qE '^[a-z][a-z0-9-]*$'; then
    echo "ERROR: Agent name must be lowercase kebab-case." >&2
    exit 1
fi

echo "Renaming agent: $OLD_NAME → $NEW_NAME"

# --- 1. Rename directory ---
mv "$OLD_DIR" "$NEW_DIR"
echo "  Renamed directory: $OLD_NAME/ → $NEW_NAME/"

# --- 2. Update claimed_by/completed_by in world/aspirations.jsonl ---
WORLD_ASP="$WORLD_DIR/aspirations.jsonl"
if [ -f "$WORLD_ASP" ]; then
    python3 -c "
import json, sys

old_name = '$OLD_NAME'
new_name = '$NEW_NAME'
path = '$WORLD_ASP'

with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

changed = 0
updated = []
for line in lines:
    stripped = line.strip()
    if not stripped:
        updated.append(line)
        continue
    rec = json.loads(stripped)
    for goal in rec.get('goals', []):
        if goal.get('claimed_by') == old_name:
            goal['claimed_by'] = new_name
            changed += 1
        if goal.get('completed_by') == old_name:
            goal['completed_by'] = new_name
            changed += 1
    updated.append(json.dumps(rec, ensure_ascii=False) + '\n')

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(updated)

print(f'  Updated {changed} reference(s) in world/aspirations.jsonl')
"
fi

# --- 3. Update world/aspirations-archive.jsonl ---
WORLD_ASP_ARCHIVE="$WORLD_DIR/aspirations-archive.jsonl"
if [ -f "$WORLD_ASP_ARCHIVE" ] && [ -s "$WORLD_ASP_ARCHIVE" ]; then
    python3 -c "
import json

old_name = '$OLD_NAME'
new_name = '$NEW_NAME'
path = '$WORLD_ASP_ARCHIVE'

with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

changed = 0
updated = []
for line in lines:
    stripped = line.strip()
    if not stripped:
        updated.append(line)
        continue
    rec = json.loads(stripped)
    for goal in rec.get('goals', []):
        if goal.get('claimed_by') == old_name:
            goal['claimed_by'] = new_name
            changed += 1
        if goal.get('completed_by') == old_name:
            goal['completed_by'] = new_name
            changed += 1
    updated.append(json.dumps(rec, ensure_ascii=False) + '\n')

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(updated)

if changed > 0:
    print(f'  Updated {changed} reference(s) in world/aspirations-archive.jsonl')
"
fi

echo ""
echo "Agent renamed successfully: $OLD_NAME → $NEW_NAME"
echo "Remember to update AYOAI_AGENT environment variable in active sessions."
