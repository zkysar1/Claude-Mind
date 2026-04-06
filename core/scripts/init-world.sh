#!/usr/bin/env bash
# init-world.sh — Deterministic world/ directory initialization
#
# Creates the collective domain state from core/config/ initial_state: sections.
# world/ holds shared knowledge, hypotheses, aspirations, and other collective
# data that all agents contribute to and read from.
#
# Idempotent: exits early if world/.initialized marker exists.
# Called by /boot or init-mind.sh (legacy wrapper).
#
# Usage:
#   bash core/scripts/init-world.sh
#
# To reinitialize: delete the world directory and re-run init-world.sh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
WORLD="$WORLD_DIR"
CONFIG="$CONFIG_DIR"

# --- Idempotent gate ---
if [ -f "$WORLD/.initialized" ]; then
    echo "world/ already initialized — skipping"
    exit 0
fi

echo "Initializing world/ (collective domain state)..."

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
    "$WORLD/conventions" \
    "$WORLD/knowledge/tree" \
    "$WORLD/knowledge/patterns" \
    "$WORLD/knowledge/strategies"

echo "  Created directory structure"

# --- 2. Domain verification checklist ---
cat > "$WORLD/verification-checklist.md" << 'CHECKLIST_EOF'
# Agent-Discovered Verification Checks

Foundational domain checks: see `core/config/verification-checklist-domain-specific.md` (read directly by /verify-learning).
This file is for checks agents discover during autonomous operation.
CHECKLIST_EOF
echo "  Created verification-checklist.md"

# --- 3. Seed collective data from config ---

# --- Aspirations: World-level task queue (JSONL) ---
if [ -f "$CONFIG/world-aspirations-initial.jsonl" ]; then
    cp "$CONFIG/world-aspirations-initial.jsonl" "$WORLD/aspirations.jsonl"
    python3 "$CORE_ROOT/scripts/aspirations.py" recompute-all-progress "$WORLD/aspirations.jsonl"
    echo "  Seeded world aspirations.jsonl"
else
    touch "$WORLD/aspirations.jsonl"
    echo "  Created empty world aspirations.jsonl"
fi
touch "$WORLD/aspirations-archive.jsonl"
cat > "$WORLD/aspirations-meta.json" << 'EOF'
{"last_updated": null, "last_evolution": null, "session_count": 0, "readiness_gates": {}}
EOF

# --- Knowledge tree ---
extract_initial_state "$CONFIG/tree.yaml" "$WORLD/knowledge/tree/_tree.yaml"

# --- Evolution triggers + memory pipeline (collective tuning) ---
extract_initial_state "$CONFIG/evolution-triggers.yaml" "$WORLD/evolution-triggers.yaml"
extract_initial_state "$CONFIG/memory-pipeline.yaml"    "$WORLD/memory-pipeline.yaml"

# --- Pattern Signatures: YAML initial_state → JSONL ---
extract_initial_state "$CONFIG/pattern-signatures.yaml" "$WORLD/pattern-signatures.yaml.tmp"
python3 "$CORE_ROOT/scripts/pattern-signatures.py" migrate-yaml "$WORLD/pattern-signatures.yaml.tmp" "$WORLD/pattern-signatures.jsonl"
rm -f "$WORLD/pattern-signatures.yaml.tmp"

# --- 4. Create collective JSONL stores ---
touch "$WORLD/pipeline.jsonl"
touch "$WORLD/pipeline-archive.jsonl"
cat > "$WORLD/pipeline-meta.json" << 'EOF'
{"last_updated":null,"stage_counts":{"discovered":0,"evaluating":0,"active":0,"resolved":0,"archived":0},"accuracy":{"total_resolved":0,"confirmed":0,"corrected":0,"accuracy_pct":0.0}}
EOF
echo "  Seeded pipeline JSONL files"

touch "$WORLD/reasoning-bank.jsonl"
touch "$WORLD/guardrails.jsonl"
echo "  Seeded reasoning-bank and guardrails JSONL files"

# --- 5. Create collective boilerplate ---

cat > "$WORLD/sources.yaml" << 'EOF'
last_updated: null
total_sources: 0
sources: []
EOF

# --- 6. Create knowledge support files ---

cat > "$WORLD/knowledge/beliefs.yaml" << 'EOF'
last_updated: null
beliefs: []
EOF

cat > "$WORLD/knowledge/transitions.yaml" << 'EOF'
transitions: []
EOF

cat > "$WORLD/knowledge/patterns/_index.yaml" << 'EOF'
count: 0
entries: []
EOF

cat > "$WORLD/knowledge/strategies/_index.yaml" << 'EOF'
count: 0
entries: []
EOF

echo "  Created knowledge support files"

# --- 7. Program placeholder (populated by /start) ---
touch "$WORLD/program.md"
echo "  Created program.md placeholder (The Program)"

# --- 8. Create L1 tree stub markdown files ---

cat > "$WORLD/knowledge/tree/execution.md" << 'EOF'
---
domain: execution
level: L1
domain_confidence: null
last_updated: null
children: []
topics: []
---

# Execution — What to DO

Strategies and methods for taking action. This domain grows as agents learn domain-specific strategies.

## Capability Map

(No topics yet — L2 nodes will be created as agents discover execution strategies.)

## Topic Summaries

(Empty — L2 nodes will be created as agents learn.)
EOF

cat > "$WORLD/knowledge/tree/intelligence.md" << 'EOF'
---
domain: intelligence
level: L1
domain_confidence: null
last_updated: null
children: []
topics: []
---

# Intelligence — What we KNOW

Domain knowledge and understanding. This domain grows as agents research and learn about the focus area.

## Capability Map

(No topics yet — L2 nodes will be created as agents research the domain.)

## Topic Summaries

(Empty — L2 nodes will be created as agents learn.)
EOF

cat > "$WORLD/knowledge/tree/performance.md" << 'EOF'
---
domain: performance
level: L1
domain_confidence: null
last_updated: null
children: []
topics: []
---

# Performance — How we're DOING

Outcome tracking and accuracy analysis. This domain grows as agents form hypotheses and track results.

## Capability Map

(No topics yet — L2 nodes will be created as agents resolve hypotheses.)

## Topic Summaries

(Empty — L2 nodes will be created as agents learn.)
EOF

cat > "$WORLD/knowledge/tree/system.md" << 'EOF'
---
domain: system
level: L1
domain_confidence: null
last_updated: null
children: []
topics: []
---

# System — HOW we work

Meta-knowledge about agent operation, preferences, and environment.

## System Constraints

- No terminal state — completion of one thing seeds the next
- Append-only journal — never modify past entries
- Pipeline records never deleted — move between stages via scripts
- Aspirations complete → evolve new ones

## Capability Map

(No topics yet — L2 nodes will be created as agents gain self-knowledge.)

## Topic Summaries

(Empty — L2 nodes will be created as agents learn.)
EOF

echo "  Created L1 tree stub files"

# --- 9. Message board ---
mkdir -p "$WORLD/board"
for channel in general findings coordination decisions; do
    touch "$WORLD/board/$channel.jsonl"
done
echo "  Created message board channels"

# --- 9.5. Forged skills registry ---
cat > "$WORLD/forged-skills.yaml" << 'EOF'
# World Forged Skills Registry — shared across all agents
# triggers: phrases from core pseudocode that resolve to forged skills
skills: {}
EOF
echo "  Created forged skills registry"

# --- 9.6. Skill relations ---
cat > "$WORLD/skill-relations.yaml" << 'EOF'
last_updated: null
forged_relations: []
co_invocation_log: []
EOF
mkdir -p "$WORLD/scripts"
echo "  Created skill relations + scripts directory"

# --- 9.7. Team state ---
bash "$CORE_ROOT/scripts/team-state-init.sh"
echo "  Initialized team-state.yaml"

# --- 10. Changelog ---
touch "$WORLD/changelog.jsonl"
echo "  Created changelog.jsonl"

# --- Done ---

FILE_COUNT=$(find "$WORLD" -type f | wc -l)
DIR_COUNT=$(find "$WORLD" -type d | wc -l)
touch "$WORLD/.initialized"
echo ""
echo "World initialization complete — $FILE_COUNT files created, $DIR_COUNT directories"
echo "Collective domain state ready for agent contributions"
