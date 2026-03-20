#!/usr/bin/env bash
# Factory Reset — wipe all accumulated state, return to blank-slate agent
#
# Usage:
#   bash core/scripts/factory-reset.sh          # interactive (prompts for confirmation)
#   bash core/scripts/factory-reset.sh --force  # no prompt (for CI/scripting)
#
# What it does:
#   1. Reads mind/forged-skills.yaml to identify forged skill directories
#   2. Deletes forged skill directories from .claude/skills/
#   3. Deletes the entire mind/ directory
#   Next /start recreates mind/ from core/config/ initial_state: sections.

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

# Color output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}=== Factory Reset ===${NC}"
echo "This will delete ALL accumulated knowledge, hypotheses, journal entries,"
echo "session state, learned patterns, and forged skills."
echo "Framework files (.claude/skills/, .claude/rules/, core/) are unchanged."
echo "Next /start will recreate a blank slate from framework config."
echo ""

# Confirmation unless --force
if [[ "${1:-}" != "--force" ]]; then
    read -p "Are you sure? Type 'reset' to confirm: " confirm
    if [[ "$confirm" != "reset" ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# --- 1. Remove forged skills from .claude/skills/ ---
echo ""
echo -e "${YELLOW}Removing forged skills...${NC}"

if [ -f "$REPO_ROOT/mind/forged-skills.yaml" ]; then
    # Read forged skill names from registry BEFORE wiping mind/
    forged=$(python3 -c "
import yaml
with open('$REPO_ROOT/mind/forged-skills.yaml') as f:
    data = yaml.safe_load(f) or {}
for name in data.get('skills', {}):
    print(name)
" 2>/dev/null || true)

    for skill in $forged; do
        if [ -d "$REPO_ROOT/.claude/skills/$skill" ]; then
            rm -rf "$REPO_ROOT/.claude/skills/$skill"
            echo "  removed forged skill: $skill/"
        fi
    done
else
    echo "  (no forged-skills.yaml found — nothing to clean)"
fi

# --- 2. Wipe all agent state ---
echo ""
echo -e "${YELLOW}Removing mind/ directory...${NC}"

if [ -d "$REPO_ROOT/mind" ]; then
    rm -rf "$REPO_ROOT/mind"
    if [ -d "$REPO_ROOT/mind" ]; then
        echo "  WARNING: mind/ not fully removed (files may be locked)"
        echo "  Close other programs accessing mind/ and retry."
        exit 1
    fi
    echo "  deleted: mind/"
else
    echo "  (mind/ directory not found — already clean)"
fi

echo ""
echo -e "${GREEN}=== Factory reset complete ===${NC}"
echo ""
echo "The agent is now a blank slate. To start learning:"
echo "  1. Open Claude Code"
echo "  2. Type /start"
echo "  3. Choose a domain to explore"
echo ""
