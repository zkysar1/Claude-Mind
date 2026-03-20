#!/usr/bin/env bash
# init-mind.sh — Deterministic mind/ directory initialization
#
# Creates the agent's mind from core/config/ initial_state: sections.
# Idempotent: exits early if mind/ already exists.
# Called by /boot Phase -2 instead of LLM-driven file creation.
#
# Usage:
#   bash core/scripts/init-mind.sh
#
# To reinitialize: rm -rf mind/ first (or run core/scripts/factory-reset.sh)

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
MIND="$REPO_ROOT/mind"
CONFIG="$CONFIG_DIR"

# --- Idempotent gate ---
if [ -d "$MIND" ]; then
    echo "mind/ already exists — skipping initialization"
    exit 0
fi

echo "Initializing mind/ from core/config/ framework definitions..."

# --- 1. Create directory structure ---
mkdir -p \
    "$MIND/journal" \
    "$MIND/session" \
    "$MIND/conventions" \
    "$MIND/knowledge/tree" \
    "$MIND/knowledge/patterns" \
    "$MIND/knowledge/meta" \
    "$MIND/knowledge/strategies"

echo "  Created directory structure"

# --- 1b. Create forged skills registry ---
cat > "$MIND/forged-skills.yaml" << 'EOF'
skills: {}
EOF
echo "  Created forged-skills.yaml"

# --- 1c. Domain verification checklist ---
# Foundational domain checks live in core/config/verification-checklist-domain-specific.md
# and are read directly by /verify-learning. The mind copy is for agent-discovered checks only.
cat > "$MIND/verification-checklist.md" << 'CHECKLIST_EOF'
# Agent-Discovered Verification Checks

Foundational domain checks: see `core/config/verification-checklist-domain-specific.md` (read directly by /verify-learning).
This file is for checks the agent discovers during autonomous operation.
CHECKLIST_EOF
echo "  Created empty verification-checklist.md (foundational checks read from core/config/)"

# --- 2. Extract initial_state from config files ---
# Each config file has initial_state: as the last top-level key.
# We extract everything after that line and strip 2 spaces of indentation.
extract_initial_state() {
    local config_file="$1"
    local target_file="$2"
    sed -n '/^initial_state:$/,$ { /^initial_state:$/d; s/^  //; p }' "$config_file" \
        | tr -d '\r' > "$target_file"
    echo "  Seeded $(basename "$target_file") from $(basename "$config_file")"
}

# --- Aspirations: JSONL format (not YAML) ---
if [ -f "$CONFIG/aspirations-initial.jsonl" ]; then
    cp "$CONFIG/aspirations-initial.jsonl" "$MIND/aspirations.jsonl"
    # Derive progress.total_goals from actual goal count — no manual maintenance needed in template
    python3 "$CORE_ROOT/scripts/aspirations.py" recompute-all-progress "$MIND/aspirations.jsonl"
    echo "  Seeded aspirations.jsonl from core/config/aspirations-initial.jsonl"
else
    touch "$MIND/aspirations.jsonl"
    echo "  Created empty aspirations.jsonl (no initial template found)"
fi
touch "$MIND/aspirations-archive.jsonl"
touch "$MIND/evolution-log.jsonl"
cat > "$MIND/aspirations-meta.json" << 'EOF'
{"last_updated": null, "last_evolution": null, "session_count": 0, "readiness_gates": {}}
EOF
extract_initial_state "$CONFIG/developmental-stage.yaml"  "$MIND/developmental-stage.yaml"
extract_initial_state "$CONFIG/evolution-triggers.yaml"    "$MIND/evolution-triggers.yaml"
extract_initial_state "$CONFIG/memory-pipeline.yaml"       "$MIND/memory-pipeline.yaml"
extract_initial_state "$CONFIG/profile.yaml"               "$MIND/profile.yaml"
extract_initial_state "$CONFIG/skill-gaps.yaml"            "$MIND/skill-gaps.yaml"
extract_initial_state "$CONFIG/reflection-templates.yaml"  "$MIND/reflection-templates.yaml"
extract_initial_state "$CONFIG/tree.yaml"                  "$MIND/knowledge/tree/_tree.yaml"

# --- Spark Questions: extract YAML initial_state, then convert to JSONL ---
extract_initial_state "$CONFIG/spark-questions.yaml"       "$MIND/spark-questions.yaml.tmp"
python3 "$CORE_ROOT/scripts/spark-questions.py" migrate-yaml "$MIND/spark-questions.yaml.tmp" "$MIND/spark-questions.jsonl"
rm -f "$MIND/spark-questions.yaml.tmp"

# --- Pattern Signatures: extract YAML initial_state, then convert to JSONL ---
extract_initial_state "$CONFIG/pattern-signatures.yaml"    "$MIND/pattern-signatures.yaml.tmp"
python3 "$CORE_ROOT/scripts/pattern-signatures.py" migrate-yaml "$MIND/pattern-signatures.yaml.tmp" "$MIND/pattern-signatures.jsonl"
rm -f "$MIND/pattern-signatures.yaml.tmp"

# --- 3. Create boilerplate state files ---

cat > "$MIND/sources.yaml" << 'EOF'
last_updated: null
total_sources: 0
sources: []
EOF

cat > "$MIND/prep-tasks.yaml" << 'EOF'
last_updated: null
phases: []
EOF

cat > "$MIND/strategy-archive.yaml" << 'EOF'
archive: []
EOF

cat > "$MIND/config-overrides.yaml" << 'EOF'
overrides: {}
EOF

cat > "$MIND/config-changes.yaml" << 'EOF'
changes: []
EOF

echo "  Created boilerplate state files"

# --- 3b. Create infrastructure health tracker ---
cat > "$MIND/infra-health.yaml" << 'EOF'
components: {}
skill_mapping: {}
category_mapping: {}
error_check: null
EOF
echo "  Created infra-health.yaml"

# --- 4. Create index files ---

# --- Pipeline: JSONL format (not YAML directories) ---
touch "$MIND/pipeline.jsonl"
touch "$MIND/pipeline-archive.jsonl"
cat > "$MIND/pipeline-meta.json" << 'EOF'
{"last_updated":null,"stage_counts":{"discovered":0,"evaluating":0,"active":0,"resolved":0,"archived":0},"accuracy":{"total_resolved":0,"confirmed":0,"corrected":0,"accuracy_pct":0.0}}
EOF
echo "  Seeded pipeline JSONL files"

# --- Experience Archive: JSONL format ---
mkdir -p "$MIND/experience"
touch "$MIND/experience.jsonl"
touch "$MIND/experience-archive.jsonl"
cat > "$MIND/experience-meta.json" << 'EOF'
{"last_updated":null,"total_live":0,"total_archived":0,"by_type":{},"by_category":{}}
EOF
echo "  Seeded experience archive JSONL files"

# --- Reasoning Bank + Guardrails + Journal: JSONL format ---
touch "$MIND/reasoning-bank.jsonl"
touch "$MIND/guardrails.jsonl"
touch "$MIND/journal.jsonl"
echo "  Seeded reasoning-bank, guardrails, journal JSONL files"

cat > "$MIND/knowledge/patterns/_index.yaml" << 'EOF'
count: 0
entries: []
EOF

cat > "$MIND/knowledge/meta/_index.yaml" << 'EOF'
count: 0
entries: []
EOF

cat > "$MIND/knowledge/strategies/_index.yaml" << 'EOF'
count: 0
entries: []
EOF

echo "  Created index files"

# --- 5. Create knowledge support files ---

cat > "$MIND/knowledge/beliefs.yaml" << 'EOF'
last_updated: null
beliefs: []
EOF

cat > "$MIND/knowledge/transitions.yaml" << 'EOF'
transitions: []
EOF

cat > "$MIND/experiential-index.yaml" << 'EOF'
last_updated: null
summary: {total_resolved: 0, total_correct: 0, total_incorrect: 0, accuracy_pct: 0.0}
by_category: {}
by_violation_cause: {}
entries: []
EOF

cat > "$MIND/knowledge/meta/step-attribution.yaml" << 'EOF'
last_updated: null
total_reflections: 0
steps: {}
EOF

echo "  Created knowledge support files"

# --- 6. Create session files ---

cat > "$MIND/session/pending-questions.yaml" << 'EOF'
questions: []
EOF

bash "$CORE_ROOT/scripts/session-persona-set.sh" true

echo "  Created session files"

# --- 6b. Create Self placeholder (populated by /start) ---
touch "$MIND/self.md"
echo "  Created self.md placeholder"

# --- 7. Create L1 tree stub markdown files ---

cat > "$MIND/knowledge/tree/execution.md" << 'EOF'
---
domain: execution
level: L1
domain_confidence: null
last_updated: null
children: []
topics: []
---

# Execution — What to DO

Strategies and methods for taking action. This domain grows as the agent learns domain-specific strategies.

## Capability Map

(No topics yet — L2 nodes will be created as the agent discovers execution strategies.)

## Topic Summaries

(Empty — L2 nodes will be created as the agent learns.)
EOF

cat > "$MIND/knowledge/tree/intelligence.md" << 'EOF'
---
domain: intelligence
level: L1
domain_confidence: null
last_updated: null
children: []
topics: []
---

# Intelligence — What we KNOW

Domain knowledge and understanding. This domain grows as the agent researches and learns about its focus area.

## Capability Map

(No topics yet — L2 nodes will be created as the agent researches its domain.)

## Topic Summaries

(Empty — L2 nodes will be created as the agent learns.)
EOF

cat > "$MIND/knowledge/tree/performance.md" << 'EOF'
---
domain: performance
level: L1
domain_confidence: null
last_updated: null
children: []
topics: []
---

# Performance — How we're DOING

Outcome tracking and accuracy analysis. This domain grows as the agent forms hypotheses and tracks results.

## Capability Map

(No topics yet — L2 nodes will be created as the agent resolves hypotheses.)

## Topic Summaries

(Empty — L2 nodes will be created as the agent learns.)
EOF

cat > "$MIND/knowledge/tree/system.md" << 'EOF'
---
domain: system
level: L1
domain_confidence: null
last_updated: null
children: []
topics: []
---

# System — HOW we work

Meta-knowledge about the agent's own operation, preferences, and environment.

## System Constraints

- No terminal state — completion of one thing seeds the next
- Append-only journal — never modify past entries
- Pipeline records never deleted — move between stages via scripts
- Aspirations complete → evolve new ones

## Capability Map

(No topics yet — L2 nodes will be created as the agent gains self-knowledge.)

## Topic Summaries

(Empty — L2 nodes will be created as the agent learns.)
EOF

echo "  Created L1 tree stub files"

# --- Done ---

FILE_COUNT=$(find "$MIND" -type f | wc -l)
DIR_COUNT=$(find "$MIND" -type d | wc -l)
echo ""
echo "Mind initialization complete — $FILE_COUNT files created, $DIR_COUNT directories"
echo "First boot detected — agent is a blank slate"
