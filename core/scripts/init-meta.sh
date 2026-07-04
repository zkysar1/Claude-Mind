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

# --- Fail-loud guard: unresolvable META_DIR (g-328 fresh-box safety) ---
# Symmetric to init-world.sh. A fresh box with no local-paths.conf resolves
# META_DIR empty; proceeding would seed a blank meta/ at the filesystem root and
# (on own-cloud) clobber the real S3 meta state on the sweep. The cache path is
# per-machine, so init cannot invent it — fail loud with guidance instead.
if [ -z "${META:-}" ]; then
    echo "ERROR: init-meta.sh — META_DIR is empty/unresolvable; refusing to seed a blank meta/." >&2
    echo "  Fix: provision agents/<name>/local-paths.conf with WORLD_PATH+META_PATH," >&2
    echo "  or export MIND_META, before running init. On own-cloud a blank seed" >&2
    echo "  would clobber the real S3 meta state on the next sweep." >&2
    exit 1
fi

# --- Fresh-box bootstrap pull (durable closer) ---
# Symmetric to init-world.sh: on own-cloud the LOCAL meta/.initialized marker
# can be absent while meta/ is fully initialized in S3, so the gate below would
# re-seed empty stubs over the real state. Pull meta/ from S3 FIRST. Fail-soft;
# non-own-cloud no-ops. (init-mind.sh runs init-world.sh first, which pulls
# world/; this independently covers a standalone init-meta.sh on a fresh box.)
if [ ! -f "$META/.initialized" ] && [ "${STORAGE_BACKEND:-local}" = "own-cloud" ]; then
    echo "  own-cloud fresh box: pulling meta/ from S3 before init gate..."
    MIND_META="${MIND_META:-$META}" python3 "$CORE_ROOT/scripts/owncloud_sync.py" --pull --root meta \
        || echo "  WARN: bootstrap meta-pull returned non-zero — proceeding (will seed if S3 truly empty)" >&2
fi

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
# meta/eval/ holds the Tier-2 eval-harness calibration corpus (cases.jsonl)
# consumed by core/scripts/eval_harness.py load_cases(). Created here as the
# sanctioned init step so it bypasses the L1 new-top-level-entry cruft gate
# (which blocks bare Write/Edit of a new dir under META_PATH but not shell
# mkdir in an init-*.sh). See .claude/rules/path-resolution.md "L1 Cruft
# Prevention" option 2 + 6.
mkdir -p \
    "$META/experiments" \
    "$META/transfer" \
    "$META/meta-knowledge" \
    "$META/eval"

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
