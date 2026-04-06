#!/usr/bin/env bash
# init-agent.sh — Deterministic per-agent directory initialization
#
# Creates a named agent's private state directory. Each agent has its own
# identity, curriculum, experience archive, journal, session state, and
# local aspiration queue.
#
# Idempotent: exits early if <agent>/.initialized marker exists.
# Called by /start after init-world.sh.
#
# Usage:
#   bash core/scripts/init-agent.sh <agent-name>
#
# The agent name becomes the directory name at the project root.
# Reserved names: core, meta, world, node_modules, .git, .claude
#
# To reinitialize: rm -rf <agent-name>/ first

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
CONFIG="$CONFIG_DIR"

# --- Validate agent name ---
AGENT_NAME_ARG="${1:-}"
if [ -z "$AGENT_NAME_ARG" ]; then
    echo "ERROR: Agent name required." >&2
    echo "Usage: bash core/scripts/init-agent.sh <agent-name>" >&2
    exit 1
fi

# Reserved names that conflict with project structure
RESERVED_NAMES="core meta world node_modules .git .claude .github"
for reserved in $RESERVED_NAMES; do
    if [ "$AGENT_NAME_ARG" = "$reserved" ]; then
        echo "ERROR: '$AGENT_NAME_ARG' is a reserved name. Choose a different agent name." >&2
        exit 1
    fi
done

# Validate kebab-case: lowercase letters, digits, hyphens only
if ! echo "$AGENT_NAME_ARG" | grep -qE '^[a-z][a-z0-9-]*$'; then
    echo "ERROR: Agent name must be lowercase kebab-case (e.g., alpha, deep-scout)." >&2
    exit 1
fi

AGENT="$PROJECT_ROOT/$AGENT_NAME_ARG"

# Export so session scripts can resolve paths via _paths.sh
export AYOAI_AGENT="$AGENT_NAME_ARG"

# --- Idempotent gate ---
if [ -f "$AGENT/.initialized" ]; then
    echo "$AGENT_NAME_ARG/ already initialized — skipping"
    exit 0
fi

echo "Initializing agent '$AGENT_NAME_ARG'..."

# --- Helper: extract initial_state from config YAML ---
extract_initial_state() {
    local config_file="$1"
    local target_file="$2"
    sed -n '/^initial_state:$/,$ { /^initial_state:$/d; s/^  //; p }' "$config_file" \
        | tr -d '\r' > "$target_file"
    echo "  Seeded $(basename "$target_file") from $(basename "$config_file")"
}

# --- 1. Create directory structure ---
mkdir -p \
    "$AGENT/journal" \
    "$AGENT/session" \
    "$AGENT/experience"

echo "  Created directory structure"

# --- 2. Identity & Progression ---

# Self placeholder (populated by /start conversation)
touch "$AGENT/self.md"
echo "  Created self.md placeholder"

extract_initial_state "$CONFIG/profile.yaml"             "$AGENT/profile.yaml"
extract_initial_state "$CONFIG/developmental-stage.yaml"  "$AGENT/developmental-stage.yaml"
extract_initial_state "$CONFIG/curriculum.yaml"           "$AGENT/curriculum.yaml"
touch "$AGENT/curriculum-promotions.jsonl"

# --- 3. Local aspiration queue ---
# Detect first vs subsequent agent to determine bootstrap aspirations
if [ -f "$WORLD_DIR/.initialized" ] && [ -s "$WORLD_DIR/program.md" ]; then
    AGENT_MODE="subsequent"
else
    AGENT_MODE="first"
fi

if [ -f "$CONFIG/agent-aspirations-initial.jsonl" ]; then
    cp "$CONFIG/agent-aspirations-initial.jsonl" "$AGENT/aspirations.jsonl"
    echo "  Seeded agent aspirations (maintenance goals)"
else
    touch "$AGENT/aspirations.jsonl"
    echo "  Created empty agent aspirations.jsonl (no template found)"
fi

# Subsequent agents get an onboarding aspiration too
if [ "$AGENT_MODE" = "subsequent" ] && [ -f "$CONFIG/agent-aspirations-onboard.jsonl" ]; then
    cat "$CONFIG/agent-aspirations-onboard.jsonl" >> "$AGENT/aspirations.jsonl"
    echo "  Added onboarding aspiration (Orient and Specialize)"
fi

# Single recompute after all aspirations are assembled
python3 "$CORE_ROOT/scripts/aspirations.py" recompute-all-progress "$AGENT/aspirations.jsonl"

touch "$AGENT/aspirations-archive.jsonl"
cat > "$AGENT/aspirations-meta.json" << 'EOF'
{"last_updated": null, "last_evolution": null, "session_count": 0, "readiness_gates": {}}
EOF

# --- 4. Raw experience archive (per-agent, MemCollab principle) ---
touch "$AGENT/experience.jsonl"
touch "$AGENT/experience-archive.jsonl"
cat > "$AGENT/experience-meta.json" << 'EOF'
{"last_updated":null,"total_live":0,"total_archived":0,"by_type":{},"by_category":{}}
EOF
cat > "$AGENT/experiential-index.yaml" << 'EOF'
last_updated: null
summary: {total_resolved: 0, total_correct: 0, total_incorrect: 0, accuracy_pct: 0.0}
by_category: {}
by_violation_cause: {}
entries: []
EOF
echo "  Created experience archive"

# --- 5. Activity log ---
touch "$AGENT/journal.jsonl"
echo "  Created journal"

# --- 6. Domain-specific tools ---
# Note: forged-skills.yaml and skill-relations.yaml are world-level (shared across agents).
# They are created by init-world.sh, not here.

cat > "$AGENT/infra-health.yaml" << 'EOF'
components: {}
skill_mapping: {}
category_mapping: {}
error_check: null
EOF

cat > "$AGENT/prep-tasks.yaml" << 'EOF'
last_updated: null
phases: []
EOF

echo "  Created domain-specific tool files"

# --- 7. Session state ---
cat > "$AGENT/session/pending-questions.yaml" << 'EOF'
questions: []
EOF

# Set persona active via session script (uses AYOAI_AGENT env var)
bash "$CORE_ROOT/scripts/session-persona-set.sh" true

echo "  Created session files"

# --- Done ---

FILE_COUNT=$(find "$AGENT" -type f | wc -l)
DIR_COUNT=$(find "$AGENT" -type d | wc -l)
touch "$AGENT/.initialized"
echo ""
echo "Agent '$AGENT_NAME_ARG' initialization complete — $FILE_COUNT files, $DIR_COUNT directories"
echo "  Mode: $AGENT_MODE agent"
if [ "$AGENT_MODE" = "subsequent" ]; then
    echo "  Onboarding aspiration seeded — agent will orient and specialize"
fi
