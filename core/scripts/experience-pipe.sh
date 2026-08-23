#!/usr/bin/env bash
# PostToolUse hook (experience-pipe) — Pearl Step 3.2.
# Fires on every Write and Edit. Fast-exits unless:
#   1. The written file is an experience archive entry, AND
#   2. MIND_COMMONS_POLICY is 'shared' or 'open'.
# When both conditions hold, launches the Lodestar pipe in the background.
# Always exits 0 — never blocks a Write or Edit.

# Read the PostToolUse hook event from stdin.
HOOK_JSON=$(cat)

# Extract tool_input.file_path from the event JSON.
FILE_PATH=""
FILE_PATH=$(printf '%s' "$HOOK_JSON" \
  | py -3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path",""))' \
  2>/dev/null) || true

# Fast-exit: only fire on experience archive entries.
case "$FILE_PATH" in
  *experience*) ;;
  *) exit 0 ;;
esac

# Gate on MIND_COMMONS_POLICY — skip unless sharing is enabled.
POLICY="${MIND_COMMONS_POLICY:-nothing}"
case "$POLICY" in
  shared|open) ;;
  *) exit 0 ;;
esac

# Locate the Lodestar project (sibling repo, or override via LODESTAR_DIR env).
LODESTAR_DIR="${LODESTAR_DIR:-$(dirname "$(pwd)")/Lodestar-Web-App}"

# Require node_modules (npm install must have run at least once).
if [ ! -d "$LODESTAR_DIR/node_modules" ]; then
  exit 0
fi

# Fire and forget — pipe runs in the background; hook exits 0 immediately.
( cd "$LODESTAR_DIR" && npm run pipe --silent > /dev/null 2>&1 & )

exit 0
