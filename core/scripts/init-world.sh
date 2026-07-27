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

# --- Fail-loud guard: unresolvable WORLD_DIR (g-328 fresh-box safety) ---
# On a fresh box with no agents/<name>/local-paths.conf (and no MIND_WORLD /
# .mind-data default), WORLD_DIR resolves EMPTY. Proceeding would (a) run the
# bootstrap pull with an empty root (silent no-op), then (b) seed a blank world
# via `mkdir -p "$WORLD/conventions"` == `mkdir -p "/conventions"` — cruft at the
# filesystem root — and on own-cloud the sweep would then push that blank tree
# OVER the real S3 state (the catastrophic clobber). The own-cloud cache path is
# per-machine, so init cannot invent it; fail loud with guidance instead
# (mirrors the daemon-side _ensure_owncloud_roots fail-loud). Container/operator
# provisioning must supply local-paths.conf (WORLD_PATH+META_PATH) before init.
if [ -z "${WORLD:-}" ]; then
    echo "ERROR: init-world.sh — WORLD_DIR is empty/unresolvable; refusing to seed a blank world." >&2
    echo "  Fix: provision agents/<name>/local-paths.conf with WORLD_PATH+META_PATH," >&2
    echo "  or export MIND_WORLD, before running init. On own-cloud a blank seed" >&2
    echo "  would clobber the real S3 world tree on the next sweep." >&2
    exit 1
fi

# --- Fresh-box bootstrap pull (durable closer) ---
# On own-cloud the local cache can be empty while world/ is fully initialized
# in S3. The LOCAL .initialized marker is then absent, so the gate below would
# RE-SEED empty stubs OVER the real S3 state (fresh-own-cloud-box blank-tree
# failure,  / BLOCKER 9). Pull world/ from S3 FIRST so the gate sees the
# true initialized state. Runs standalone (no daemon: owncloud_sync talks to S3
# directly via the AWS SDK), so it works BEFORE the daemon is up.
# FAIL-LOUD on pull errors (): the previous `|| echo WARN` masked a
# failed/partial pull, so init fell through to the stub-seed below, wrote
# .initialized, and permanently closed this gate on a box missing entire
# subtrees — nothing ever re-pulls once .initialized exists (bred the
# 5-of-2545 knowledge/tree gap on the 2026-07-14 bring-up). --pull exits
# non-zero iff errors > 0 (every S3 stat/list/refresh failure counts), while
# a genuinely-first init (nothing in S3) pulls nothing WITHOUT errors and
# seeds normally — so aborting on non-zero never blocks a true first init.
# On failure: fix credentials/network and re-run (the gate stays open, the
# pull retries). Escape hatch for a deliberate degraded init:
# FORCE_INIT_WITHOUT_PULL=1.
if [ ! -f "$WORLD/.initialized" ] && [ "${STORAGE_BACKEND:-local}" = "own-cloud" ]; then
    echo "  own-cloud fresh box: pulling world/ from S3 before init gate..."
    if ! MIND_WORLD="${MIND_WORLD:-$WORLD}" python3 "$CORE_ROOT/scripts/owncloud_sync.py" --pull --root world; then
        if [ "${FORCE_INIT_WITHOUT_PULL:-0}" = "1" ]; then
            echo "  WARN: bootstrap world-pull reported errors — proceeding anyway (FORCE_INIT_WITHOUT_PULL=1)" >&2
        else
            echo "ERROR: init-world.sh — bootstrap world-pull reported errors; refusing to proceed." >&2
            echo "  Seeding stubs over a partial mirror would write .initialized and permanently" >&2
            echo "  close this pull gate with entire subtrees missing (the knowledge/tree" >&2
            echo "  bring-up gap, g-328-35). Fix S3 credentials/network and re-run /start —" >&2
            echo "  the gate stays open until one clean pull. To force a degraded init:" >&2
            echo "  FORCE_INIT_WITHOUT_PULL=1 bash core/scripts/init-world.sh" >&2
            exit 1
        fi
    fi
fi

# --- Idempotent gate (+  additive seed-backfill) ---
# A world initialized BEFORE a seed line was added to this script never
# received it — the early-exit closed the gate forever (seed-drift class,
# guard-146 incident). Instead of exiting, an already-initialized world runs
# the SAME seed body in BACKFILL mode: every seeding op below is per-file
# guarded (seed_needed), so the pass is additive-only — missing seeds land,
# existing files are never overwritten. INIT_BACKFILL=0 restores the old skip.
BACKFILL=0
if [ -f "$WORLD/.initialized" ]; then
    if [ "${INIT_BACKFILL:-1}" = "0" ]; then
        echo "world/ already initialized — skipping (INIT_BACKFILL=0)"
        exit 0
    fi
    BACKFILL=1
    echo "world/ already initialized — additive seed-backfill pass (g-115-2524; INIT_BACKFILL=0 to skip)"
    PRE_FILE_COUNT=$(find "$WORLD" -type f | wc -l)
else
    echo "Initializing world/ (collective domain state)..."
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
# telemetry/session-records holds durable per-session telemetry records
# (world/telemetry/session-records/<agent>/<sid>.json) written by
# core/scripts/_session_telemetry.py. Establishing it here makes it a
# first-class world dir carried by the own-cloud sweep; the writer module
# also creates it lazily, so this is belt-and-suspenders (and documents intent).
# NOT "telemetry/sessions" — owncloud_sync.py _EXCLUDE_DIRS walk-prunes the
# "sessions" basename, which would block the S3 sweep (rb 2026-06-03).
mkdir -p \
    "$WORLD/conventions" \
    "$WORLD/config" \
    "$WORLD/knowledge/tree" \
    "$WORLD/knowledge/patterns" \
    "$WORLD/knowledge/strategies" \
    "$WORLD/telemetry/session-records"

echo "  Created directory structure"

# --- 1.1 Seed empty domain-overlay stub files (Phase 2.5 packaging) ---
# Core scripts read these via core/scripts/_world_config.py with safe-empty
# defaults. A fresh deployment can leave them empty; the host deployment
# populates them with domain-specific routing/classification tables. See
# core/scripts/_world_config.py docstring + the read sites:
#   - gates/capability_route.py:   capability-routing.yaml
#   - gates/scaffolded_exploration.py:  scaffolded-exploration.yaml
#   - audit-applies-to.py:         applies-to-rules.yaml
#
# IDEMPOTENCY: each cat-into-config-file is guarded (seed_needed, which
# subsumes the original [[ -f ]] form and adds the backfill-mode
# store-of-record probe) so a rerun does NOT clobber populated overlays.
# Fresh-eyes review HIGH H3 (2026-05-18): the top-level .initialized
# marker is necessary but not sufficient — if a user deletes .initialized
# to "reseed" while keeping their populated overlays, the prior version
# of this script would replace the 8.7KB capability-routing.yaml with an
# empty stub. Per-file guards prevent that data loss.
if seed_needed "$WORLD/config/capability-routing.yaml"; then cat > "$WORLD/config/capability-routing.yaml" << 'EOF'
# Domain-specific capability routing overlay — populated by the host
# deployment. Read by core/scripts/gates/capability_route.py.
# Empty = classifier returns "either" for every goal (safe default).
# Schema:
#   title_prefix_routes:
#     - prefix: "investigate:"
#       agent: "<agent-name>"
#       confidence: 0.88
#       rationale: "..."
#   category_routes:
#     - category: "<category-key>"
#       agent: "<agent-name>"
#       confidence: 0.55
#       rationale: "..."
#   description_heuristics:
#     - phrase: "<substring>"
#       agent: "<agent-name>"
#       delta: 0.10
#       rationale: "..."
title_prefix_routes: []
category_routes: []
description_heuristics: []
EOF
fi

if seed_needed "$WORLD/config/scaffolded-exploration.yaml"; then cat > "$WORLD/config/scaffolded-exploration.yaml" << 'EOF'
# Domain-specific scaffolded-exploration overlay — populated by the host
# deployment. Read by core/scripts/gates/scaffolded_exploration.py.
# Empty = gate never fires on category (no Apply: blocking on missing
# discovered_by). Add your product's category prefixes here to enable
# the Investigate-precursor enforcement on Apply: goals.
product_category_prefixes: []
EOF
fi

if seed_needed "$WORLD/config/applies-to-rules.yaml"; then cat > "$WORLD/config/applies-to-rules.yaml" << 'EOF'
# Domain-specific applies-to classification overlay — populated by the host
# deployment. Read by core/scripts/audit-applies-to.py.
# Empty = every reasoning-bank entry falls into "uncertain" (no auto-
# classification as "domain"). Add your domain's category prefixes here
# (e.g. "math", "lessons", "chemistry") to enable the auto-classifier.
# METHODOLOGY_TERMS remain in core (framework-universal).
domain_prefixes: []
EOF
fi

if seed_needed "$WORLD/config/work-class-mapping.yaml"; then cat > "$WORLD/config/work-class-mapping.yaml" << 'EOF'
# Domain-specific work-class mapping overlay — populated by the host
# deployment. Merged with core/config/work-class-mapping.yaml (per-key
# override). Read by core/scripts/_work_class.py.
# Empty = core framework-universal mapping alone (framework / hygiene /
# research / unclassified). Add your domain's "product" categories here
# (e.g. lesson-planning: product) to participate in portfolio balance.
# Format: see core/config/work-class-mapping.yaml header.
mapping: {}
EOF
fi

if seed_needed "$WORLD/config/stale-scanner.yaml"; then cat > "$WORLD/config/stale-scanner.yaml" << 'EOF'
# Domain-specific stale-process-scanner thresholds (WORLD overlay).
# Read by world/scripts/stale-jobs-scan.py (if your deployment uses one).
# Each threshold is a lifetime limit in hours per process type. Processes
# older than their type's threshold are kill candidates (unless protected).
# Empty = scanner falls back to its built-in DEFAULT_THRESHOLDS.
thresholds: {}
EOF
fi

if seed_needed "$WORLD/config/infra-health-categories.yaml"; then cat > "$WORLD/config/infra-health-categories.yaml" << 'EOF'
# Domain-specific infra-health component categories (WORLD overlay).
# Read by core/scripts/infra-health.py for known_blocker affected_categories
# sync. Each entry maps a probe-component name to the goal categories that
# should be suppressed when the component crosses failing_streak_threshold.
# Empty = streak alerts still fire but never reach the goal-selector.
component_categories: {}
EOF
fi

# Cross-world versioning DOMAIN OVERLAY (Wave 1). Names the concrete repos
# filling each promotion-chain role + the release-feed URLs. Does NOT travel
# with the seed — each deployment writes its own. The framework half is
# core/config/compatibility.yaml. Read by release.sh step 6 + (Wave 2)
# check-upstream.sh / promote-to-upstream.sh. Per-file guard: a populated
# overlay is never clobbered on reseed (omni Q6).
if seed_needed "$WORLD/config/compatibility.yaml"; then cat > "$WORLD/config/compatibility.yaml" << 'EOF'
# Cross-world DOMAIN OVERLAY — fill in for THIS deployment. Empty/placeholder
# means release.sh's frontier-invariant pre-check (H1) cannot fetch the seed
# feed and will require --force-release "<reason>" until the URLs are real.
self_role: ""         # which role THIS repo plays: frontier | seed | downstream
roles:
  frontier: ""        # repo that develops core + cuts releases first
  seed: ""            # domain-free seed source
  downstream: []      # repos that consume the seed
sources:
  frontier:
    releases_url: ""  # https://raw.githubusercontent.com/<org>/<frontier>/main/RELEASES.json
  seed:
    releases_url: ""  # https://raw.githubusercontent.com/<org>/<seed>/main/RELEASES.json
EOF
fi
echo "  Seeded world/config/ stub overlay files (empty defaults)"

# --- 2. Domain verification checklist ---
if seed_needed "$WORLD/verification-checklist.md"; then
cat > "$WORLD/verification-checklist.md" << 'CHECKLIST_EOF'
# Agent-Discovered Verification Checks

Foundational domain checks: see `core/config/verification-checklist-domain-specific.md` (read directly by /verify-learning).
This file is for checks agents discover during autonomous operation.
CHECKLIST_EOF
echo "  Created verification-checklist.md"
fi

# --- 3. Seed collective data from config ---

# --- Aspirations: World-level task queue (JSONL) ---
# NOTE: This seeds the WORLD bootstrap aspiration ( "Explore and Learn").
# The agent-level  ("Maintain Agent Health") is a DIFFERENT aspiration
# sharing the canonical bootstrap ID by convention — seeded in init-agent.sh.
# See core/config/conventions/aspirations.md → Dual-Scope Bootstrap IDs.
if seed_needed "$WORLD/aspirations.jsonl"; then
    if [ -f "$CONFIG/world-aspirations-initial.jsonl" ]; then
        cp "$CONFIG/world-aspirations-initial.jsonl" "$WORLD/aspirations.jsonl"
        python3 "$CORE_ROOT/scripts/aspirations.py" recompute-all-progress "$WORLD/aspirations.jsonl"
        echo "  Seeded world aspirations.jsonl"
    else
        touch "$WORLD/aspirations.jsonl"
        echo "  Created empty world aspirations.jsonl"
    fi
fi
# : touch targets route through seed_needed too — under own-cloud
# BACKFILL a bare touch on a cache-absent-but-store-present file creates a
# 0-byte local with no manifest baseline, which the real-time single-file
# push path (sync_file, multi_machine=False) or a single-machine sweep would
# push over the store content (guard-980 clobber class). The periodic
# multi-machine own-cloud sweep pulls instead (), but the guard
# closes the other lanes and skips the gratuitous mtime bump on existing files.
if seed_needed "$WORLD/aspirations-archive.jsonl"; then
    touch "$WORLD/aspirations-archive.jsonl"
fi
if seed_needed "$WORLD/aspirations-meta.json"; then
cat > "$WORLD/aspirations-meta.json" << 'EOF'
{"last_updated": null, "last_evolution": null, "session_count": 0, "readiness_gates": {}}
EOF
fi

# --- Knowledge tree ---
if seed_needed "$WORLD/knowledge/tree/_tree.yaml"; then
    extract_initial_state "$CONFIG/tree.yaml" "$WORLD/knowledge/tree/_tree.yaml"
fi

# --- Evolution triggers + memory pipeline (collective tuning) ---
if seed_needed "$WORLD/evolution-triggers.yaml"; then
    extract_initial_state "$CONFIG/evolution-triggers.yaml" "$WORLD/evolution-triggers.yaml"
fi
if seed_needed "$WORLD/memory-pipeline.yaml"; then
    extract_initial_state "$CONFIG/memory-pipeline.yaml"    "$WORLD/memory-pipeline.yaml"
fi

# --- Pattern Signatures: YAML initial_state → JSONL ---
if seed_needed "$WORLD/pattern-signatures.jsonl"; then
    extract_initial_state "$CONFIG/pattern-signatures.yaml" "$WORLD/pattern-signatures.yaml.tmp"
    python3 "$CORE_ROOT/scripts/pattern-signatures.py" migrate-yaml "$WORLD/pattern-signatures.yaml.tmp" "$WORLD/pattern-signatures.jsonl"
    rm -f "$WORLD/pattern-signatures.yaml.tmp"
fi

# --- 4. Create collective JSONL stores ---
# : seed_needed-guarded (see the aspirations-archive comment above).
if seed_needed "$WORLD/pipeline.jsonl"; then
    touch "$WORLD/pipeline.jsonl"
fi
if seed_needed "$WORLD/pipeline-archive.jsonl"; then
    touch "$WORLD/pipeline-archive.jsonl"
fi
if seed_needed "$WORLD/pipeline-meta.json"; then
cat > "$WORLD/pipeline-meta.json" << 'EOF'
{"last_updated":null,"stage_counts":{"discovered":0,"active":0,"measurement-pending":0,"resolved":0,"archived":0},"accuracy":{"total_resolved":0,"confirmed":0,"corrected":0,"accuracy_pct":0.0}}
EOF
fi
echo "  Seeded pipeline JSONL files"

if seed_needed "$WORLD/reasoning-bank.jsonl"; then
    touch "$WORLD/reasoning-bank.jsonl"
fi
if seed_needed "$WORLD/guardrails.jsonl"; then
    touch "$WORLD/guardrails.jsonl"
fi
echo "  Seeded reasoning-bank and guardrails JSONL files"

# --- 5. Create collective boilerplate ---

if seed_needed "$WORLD/sources.yaml"; then
cat > "$WORLD/sources.yaml" << 'EOF'
last_updated: null
total_sources: 0
sources: []
EOF
fi

# --- 6. Create knowledge support files ---

if seed_needed "$WORLD/knowledge/beliefs.yaml"; then
cat > "$WORLD/knowledge/beliefs.yaml" << 'EOF'
last_updated: null
beliefs: []
EOF
fi

if seed_needed "$WORLD/knowledge/transitions.yaml"; then
cat > "$WORLD/knowledge/transitions.yaml" << 'EOF'
transitions: []
EOF
fi

if seed_needed "$WORLD/knowledge/patterns/_index.yaml"; then
cat > "$WORLD/knowledge/patterns/_index.yaml" << 'EOF'
count: 0
entries: []
EOF
fi

if seed_needed "$WORLD/knowledge/strategies/_index.yaml"; then
cat > "$WORLD/knowledge/strategies/_index.yaml" << 'EOF'
count: 0
entries: []
EOF
fi

echo "  Created knowledge support files"

# --- 7. Program placeholder (populated by /start) ---
# : guarded — an evolved store-present program.md must never be
# shadowed by a 0-byte placeholder under BACKFILL.
if seed_needed "$WORLD/program.md"; then
    touch "$WORLD/program.md"
    echo "  Created program.md placeholder (The Program)"
fi

# --- 8. Create L1 tree stub markdown files ---

if seed_needed "$WORLD/knowledge/tree/execution.md"; then
cat > "$WORLD/knowledge/tree/execution.md" << 'EOF'
---
domain: execution
level: L1
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
fi

if seed_needed "$WORLD/knowledge/tree/intelligence.md"; then
cat > "$WORLD/knowledge/tree/intelligence.md" << 'EOF'
---
domain: intelligence
level: L1
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
fi

if seed_needed "$WORLD/knowledge/tree/performance.md"; then
cat > "$WORLD/knowledge/tree/performance.md" << 'EOF'
---
domain: performance
level: L1
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
fi

if seed_needed "$WORLD/knowledge/tree/system.md"; then
cat > "$WORLD/knowledge/tree/system.md" << 'EOF'
---
domain: system
level: L1
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
fi

echo "  Created L1 tree stub files"

# --- 9. Message board ---
mkdir -p "$WORLD/board"
for channel in general findings coordination decisions; do
    if seed_needed "$WORLD/board/$channel.jsonl"; then
        touch "$WORLD/board/$channel.jsonl"
    fi
done
echo "  Created message board channels"

# --- 9.5. Forged skills registry ---
if seed_needed "$WORLD/forged-skills.yaml"; then
cat > "$WORLD/forged-skills.yaml" << 'EOF'
# World Forged Skills Registry — shared across all agents
# triggers: phrases from core pseudocode that resolve to forged skills
skills: {}
EOF
echo "  Created forged skills registry"
fi

# --- 9.6. Skill relations ---
if seed_needed "$WORLD/skill-relations.yaml"; then
cat > "$WORLD/skill-relations.yaml" << 'EOF'
last_updated: null
forged_relations: []
co_invocation_log: []
EOF
fi
mkdir -p "$WORLD/scripts"
echo "  Created skill relations + scripts directory"

# Seed Layer-B/C defense scripts from framework templates so fresh worlds get
# the canonical loop-death prevention out-of-the-box (rb-629/guard-454). Domain
# may edit these in world/scripts/ later. Idempotent: only seed when missing.
if seed_needed "$WORLD/scripts/output-style-mode-guard.sh"; then
    cp "$CONFIG/templates/output-style-mode-guard.sh" "$WORLD/scripts/output-style-mode-guard.sh"
    chmod +x "$WORLD/scripts/output-style-mode-guard.sh" 2>/dev/null || true
    echo "  Seeded output-style-mode-guard.sh (Layer-B)"
fi
if seed_needed "$WORLD/scripts/trailing-text-detector.py"; then
    cp "$CONFIG/templates/trailing-text-detector.py" "$WORLD/scripts/trailing-text-detector.py"
    echo "  Seeded trailing-text-detector.py (Layer-C)"
fi

# --- 9.7. Team state ---
bash "$CORE_ROOT/scripts/team-state-init.sh"
echo "  Initialized team-state.yaml"

# --- 10. Changelog ---
# : guarded for consistency (changelog.jsonl is sweep-excluded /
# machine-local, so the own-cloud clobber lane does not apply — the guard
# still skips the gratuitous mtime bump on existing files).
if seed_needed "$WORLD/changelog.jsonl"; then
    touch "$WORLD/changelog.jsonl"
    echo "  Created changelog.jsonl"
fi

# --- Done ---

FILE_COUNT=$(find "$WORLD" -type f | wc -l)
DIR_COUNT=$(find "$WORLD" -type d | wc -l)
touch "$WORLD/.initialized"
echo ""
if [ "$BACKFILL" = "1" ]; then
    echo "World seed-backfill complete — $((FILE_COUNT - PRE_FILE_COUNT)) missing seed(s) added ($FILE_COUNT files total)"
else
    echo "World initialization complete — $FILE_COUNT files created, $DIR_COUNT directories"
    echo "Collective domain state ready for agent contributions"
fi
