#!/usr/bin/env bash
# init-meta.sh — Deterministic meta/ directory initialization
#
# Creates the meta-strategy directory from core/config/meta.yaml initial_state,
# plus domain-agnostic files that were moved here from mind/:
#   spark-questions, skill-quality, skill-gaps, evolution-log,
#   reflection-templates, strategy-archive, config-overrides, config-changes,
#   step-attribution, meta-knowledge index
#
# Idempotent: exits early if meta/.initialized marker exists.
# Called by init-mind.sh or directly by /boot.
#
# Usage:
#   bash core/scripts/init-meta.sh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
META="$META_DIR"
CONFIG="$CONFIG_DIR"

# --- Idempotent gate ---
if [ -f "$META/.initialized" ]; then
    echo "meta/ already initialized — skipping"
    exit 0
fi

echo "Initializing meta/ (domain-agnostic self-improvement strategies)..."

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
    "$META/experiments" \
    "$META/transfer" \
    "$META/meta-knowledge"

echo "  Created directory structure"

# --- 2. Extract initial_state via Python seeder (existing meta-strategies) ---
python3 "$CORE_ROOT/scripts/meta-init.py"

# --- 3. Create JSONL stores (existing) ---
touch "$META/meta-log.jsonl"
echo "  Created empty meta-log.jsonl"
touch "$META/dead-ends.jsonl"
echo "  Created empty dead-ends.jsonl"

# --- 4. Create transfer index ---
cat > "$META/transfer/_index.yaml" << 'EOF'
bundles: []
EOF
echo "  Created transfer/_index.yaml"

# --- 5. Files moved from mind/ to meta/ (domain-agnostic) ---

# Spark Questions: metacognitive prompts (yield rates carry over across domains)
extract_initial_state "$CONFIG/spark-questions.yaml" "$META/spark-questions.yaml.tmp"
python3 "$CORE_ROOT/scripts/spark-questions.py" migrate-yaml "$META/spark-questions.yaml.tmp" "$META/spark-questions.jsonl"
rm -f "$META/spark-questions.yaml.tmp"

# Evolution log: meta-strategy change audit trail
touch "$META/evolution-log.jsonl"
echo "  Created empty evolution-log.jsonl"

# Skill quality: framework skill quality scores
cat > "$META/skill-quality.yaml" << 'EOF'
last_updated: null
skills: {}
EOF
echo "  Created skill-quality.yaml"

# Skill gaps: cognitive capability gaps
extract_initial_state "$CONFIG/skill-gaps.yaml" "$META/skill-gaps.yaml"

# Reflection templates: HOW to reflect (process templates)
extract_initial_state "$CONFIG/reflection-templates.yaml" "$META/reflection-templates.yaml"

# Strategy archive: failed/replaced strategies
cat > "$META/strategy-archive.yaml" << 'EOF'
archive: []
EOF
echo "  Created strategy-archive.yaml"

# Config overrides: agent config preferences
cat > "$META/config-overrides.yaml" << 'EOF'
overrides: {}
EOF
echo "  Created config-overrides.yaml"

# Config changes: config change log
cat > "$META/config-changes.yaml" << 'EOF'
changes: []
EOF
echo "  Created config-changes.yaml"

# Step attribution: reflection step performance tracking
cat > "$META/step-attribution.yaml" << 'EOF'
last_updated: null
total_reflections: 0
steps: {}
EOF
echo "  Created step-attribution.yaml"

# Meta-knowledge index
cat > "$META/meta-knowledge/_index.yaml" << 'EOF'
count: 0
entries: []
EOF
echo "  Created meta-knowledge/_index.yaml"

# --- Done ---
FILE_COUNT=$(find "$META" -type f | wc -l)
DIR_COUNT=$(find "$META" -type d | wc -l)
touch "$META/.initialized"
echo ""
echo "Meta initialization complete — $FILE_COUNT files created, $DIR_COUNT directories"
