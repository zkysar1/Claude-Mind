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
# re-seed empty stubs over the real state. Pull meta/ from S3 FIRST.
# Non-own-cloud no-ops. (init-mind.sh runs init-world.sh first, which pulls
# world/; this independently covers a standalone init-meta.sh on a fresh box.)
# FAIL-LOUD on pull errors (, symmetric to init-world.sh): a masked
# partial pull lets the stub-seed below write .initialized and permanently
# close this gate with subtrees missing. --pull exits non-zero iff errors > 0;
# an empty-S3 first init exits 0 and seeds normally. Escape hatch:
# FORCE_INIT_WITHOUT_PULL=1.
if [ ! -f "$META/.initialized" ] && [ "${STORAGE_BACKEND:-local}" = "own-cloud" ]; then
    echo "  own-cloud fresh box: pulling meta/ from S3 before init gate..."
    if ! MIND_META="${MIND_META:-$META}" python3 "$CORE_ROOT/scripts/owncloud_sync.py" --pull --root meta; then
        if [ "${FORCE_INIT_WITHOUT_PULL:-0}" = "1" ]; then
            echo "  WARN: bootstrap meta-pull reported errors — proceeding anyway (FORCE_INIT_WITHOUT_PULL=1)" >&2
        else
            echo "ERROR: init-meta.sh — bootstrap meta-pull reported errors; refusing to proceed." >&2
            echo "  Seeding stubs over a partial mirror would write .initialized and permanently" >&2
            echo "  close this pull gate with subtrees missing (g-328-35). Fix S3 credentials/" >&2
            echo "  network and re-run /start — the gate stays open until one clean pull." >&2
            echo "  To force a degraded init: FORCE_INIT_WITHOUT_PULL=1 bash core/scripts/init-meta.sh" >&2
            exit 1
        fi
    fi
fi

# --- Idempotent gate (+  additive seed-backfill) ---
# A meta/ initialized BEFORE a seed line was added to this script never
# received it — the early-exit closed the gate forever (seed-drift class,
# guard-146 incident; e.g. the cognitive-horizons ~3wk fleet-wide FNF).
# Instead of exiting, an already-initialized meta/ runs the SAME seed body in
# BACKFILL mode: every seeding op below is per-file guarded (seed_needed /
# meta-init.py --missing-only), so the pass is additive-only — missing seeds
# land, existing files are never overwritten. INIT_BACKFILL=0 restores the
# old skip.
BACKFILL=0
if [ -f "$META/.initialized" ]; then
    if [ "${INIT_BACKFILL:-1}" = "0" ]; then
        echo "meta/ already initialized — skipping (INIT_BACKFILL=0)"
        exit 0
    fi
    BACKFILL=1
    echo "meta/ already initialized — additive seed-backfill pass (g-115-2524; INIT_BACKFILL=0 to skip)"
    PRE_FILE_COUNT=$(find "$META" -type f | wc -l)
else
    echo "Initializing meta/ (domain-agnostic self-improvement strategies)..."
fi

# --- Helper: extract initial_state from config YAML ---
extract_initial_state() {
    local config_file="$1"
    local target_file="$2"
    sed -n '/^initial_state:$/,$ { /^initial_state:$/d; s/^  //; p }' "$config_file" \
        | tr -d '\r' > "$target_file"
    echo "  Seeded $(basename "$target_file") from $(basename "$config_file")"
}

# --- Helper: should <target> be seeded? ( additive backfill) ---
# TRUE only when the target is genuinely missing. Backfill runs are additive-
# only: an existing local file is NEVER touched. Under own-cloud in backfill
# mode, local absence is not authoritative (read-through cache, guard-980
# class): a stub seeded over a store-present evolved file would be pushed by
# the next sweep. _init_seed_probe.py consults the store of record — verdict
# "seed" (absent there too) seeds; "materialized" (present, pulled into the
# cache) and "skip-error" (probe failed — safe direction) do not.
seed_needed() {
    local target="$1"
    [ -e "$target" ] && return 1
    if [ "$BACKFILL" = "1" ] && [ "${STORAGE_BACKEND:-local}" = "own-cloud" ]; then
        local verdict
        verdict="$(python3 "$CORE_ROOT/scripts/_init_seed_probe.py" "$target" 2>/dev/null || echo skip-error)"
        case "$verdict" in
            seed) return 0;;
            materialized) echo "  backfill: materialized $(basename "$target") from store (was cache-absent)"; return 1;;
            *) echo "  backfill: WARN store probe inconclusive for $(basename "$target") — not seeding (safe direction)" >&2; return 1;;
        esac
    fi
    return 0
}

# --- 1. Create directory structure ---
# meta/eval/ holds the Tier-2 eval-harness calibration corpus (cases.jsonl)
# consumed by core/scripts/eval_harness.py load_cases(). Created here as the
# sanctioned init step so it bypasses the L1 new-top-level-entry cruft gate
# (which blocks bare Write/Edit of a new dir under META_PATH but not shell
# mkdir in an init-*.sh). See .claude/rules/path-resolution.md "L1 Cruft
# Prevention" option 2 + .
mkdir -p \
    "$META/experiments" \
    "$META/transfer" \
    "$META/meta-knowledge" \
    "$META/eval"

echo "  Created directory structure"

# --- 2. Extract initial_state via Python seeder (existing meta-strategies) ---
if [ "$BACKFILL" = "1" ]; then
    python3 "$CORE_ROOT/scripts/meta-init.py" --missing-only
else
    python3 "$CORE_ROOT/scripts/meta-init.py"
fi

# --- 3. Create JSONL stores (existing) ---
# : touch targets route through seed_needed too — under own-cloud
# BACKFILL a bare touch on a cache-absent-but-store-present file creates a
# 0-byte local with no manifest baseline, which the real-time single-file
# push path (sync_file, multi_machine=False) or a single-machine sweep would
# push over the store content (guard-980 clobber class). The periodic
# multi-machine own-cloud sweep pulls instead (), but the guard
# closes the other lanes and skips the gratuitous mtime bump on existing files.
if seed_needed "$META/meta-log.jsonl"; then
    touch "$META/meta-log.jsonl"
    echo "  Created empty meta-log.jsonl"
fi
if seed_needed "$META/dead-ends.jsonl"; then
    touch "$META/dead-ends.jsonl"
    echo "  Created empty dead-ends.jsonl"
fi

# --- 4. Create transfer index ---
if seed_needed "$META/transfer/_index.yaml"; then
cat > "$META/transfer/_index.yaml" << 'EOF'
bundles: []
EOF
echo "  Created transfer/_index.yaml"
fi

# --- 5. Files moved from mind/ to meta/ (domain-agnostic) ---

# Spark Questions: metacognitive prompts (yield rates carry over across domains)
if seed_needed "$META/spark-questions.jsonl"; then
    extract_initial_state "$CONFIG/spark-questions.yaml" "$META/spark-questions.yaml.tmp"
    python3 "$CORE_ROOT/scripts/spark-questions.py" migrate-yaml "$META/spark-questions.yaml.tmp" "$META/spark-questions.jsonl"
    rm -f "$META/spark-questions.yaml.tmp"
fi

# Evolution log: meta-strategy change audit trail
# : seed_needed-guarded (see the section-3 comment above).
if seed_needed "$META/evolution-log.jsonl"; then
    touch "$META/evolution-log.jsonl"
    echo "  Created empty evolution-log.jsonl"
fi

# Skill quality: framework skill quality scores
if seed_needed "$META/skill-quality.yaml"; then
cat > "$META/skill-quality.yaml" << 'EOF'
last_updated: null
skills: {}
EOF
echo "  Created skill-quality.yaml"
fi

# Skill gaps: cognitive capability gaps
if seed_needed "$META/skill-gaps.yaml"; then
    extract_initial_state "$CONFIG/skill-gaps.yaml" "$META/skill-gaps.yaml"
fi

# Reflection templates: HOW to reflect (process templates)
if seed_needed "$META/reflection-templates.yaml"; then
    extract_initial_state "$CONFIG/reflection-templates.yaml" "$META/reflection-templates.yaml"
fi

# Cognitive horizons: SSOT for hypothesis-horizon constants ( / BRD Gap 19).
# Static config consumed FAIL-LOUD by precheck-eval.py cmd_hypothesis_health (no
# hardcoded fallback, guard-424). MUST be seeded here so fresh-box init does not
# FileNotFoundError -- the ~3wk fleet-wide FNF since 2026-06-13 was this exact
# missing seed (init-seed parity; echo-2744 audit / ).
# Per-file guard (): the bootstrap pull above can populate meta/
# WITHOUT bringing .initialized (marker absent in S3), so the script-level
# gate falls through — a bare cp here would clobber the S3-pulled evolved
# SSOT with the pristine template, and the next sweep would push the clobber.
# (seed_needed subsumes the [[ -f ]] guard and adds the backfill-mode
# store-of-record probe — .)
if seed_needed "$META/cognitive-horizons.yaml"; then
    cp "$CONFIG/cognitive-horizons.yaml" "$META/cognitive-horizons.yaml"
    echo "  Seeded cognitive-horizons.yaml from core/config"
fi

# Skill-discovery strategy: SSOT for forged-skill discoverability thresholds +
# triage templates. Consumed FAIL-LOUD by skill-discovery.py + the mind_api
# skill_discovery endpoint ("DO NOT add fallback defaults"; CLI exit 3 / raise
# when absent). MUST be seeded so fresh-box init does not FileNotFoundError the
# moment aspirations-evolve Step 9.5.5 runs (init-seed parity; echo-2744
# systematic audit /  — the second gap found after cognitive-horizons).
if seed_needed "$META/skill-discovery-strategy.yaml"; then
    cp "$CONFIG/skill-discovery-strategy.yaml" "$META/skill-discovery-strategy.yaml"
    echo "  Seeded skill-discovery-strategy.yaml from core/config"
fi

# Strategy archive: failed/replaced strategies
if seed_needed "$META/strategy-archive.yaml"; then
cat > "$META/strategy-archive.yaml" << 'EOF'
archive: []
EOF
echo "  Created strategy-archive.yaml"
fi

# Config overrides: agent config preferences
if seed_needed "$META/config-overrides.yaml"; then
cat > "$META/config-overrides.yaml" << 'EOF'
overrides: {}
EOF
echo "  Created config-overrides.yaml"
fi

# Config changes: config change log
if seed_needed "$META/config-changes.yaml"; then
cat > "$META/config-changes.yaml" << 'EOF'
changes: []
EOF
echo "  Created config-changes.yaml"
fi

# Step attribution: reflection step performance tracking
if seed_needed "$META/step-attribution.yaml"; then
cat > "$META/step-attribution.yaml" << 'EOF'
last_updated: null
total_reflections: 0
steps: {}
EOF
echo "  Created step-attribution.yaml"
fi

# Meta-knowledge index
if seed_needed "$META/meta-knowledge/_index.yaml"; then
cat > "$META/meta-knowledge/_index.yaml" << 'EOF'
count: 0
entries: []
EOF
echo "  Created meta-knowledge/_index.yaml"
fi

# --- Done ---
FILE_COUNT=$(find "$META" -type f | wc -l)
DIR_COUNT=$(find "$META" -type d | wc -l)
touch "$META/.initialized"
echo ""
if [ "$BACKFILL" = "1" ]; then
    echo "Meta seed-backfill complete — $((FILE_COUNT - PRE_FILE_COUNT)) missing seed(s) added ($FILE_COUNT files total)"
else
    echo "Meta initialization complete — $FILE_COUNT files created, $DIR_COUNT directories"
fi
