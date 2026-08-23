#!/usr/bin/env bash
# test_co_invest_alignment.sh —  / co-investigation primitive.
#
# Verifies the goal-selector.py co_invest_alignment criterion correctly
# applies a 1.0 raw bonus (×0.5 weight = 0.5 final) ONLY when:
#   - candidate has a non-null co_parent_id
#   - some partner's team-state agent_status.<other>.in_flight.co_parent_id
#     matches that value
#
# Three scenarios cover the truth table:
#   1. no co_parent_id              → raw = 0.0
#   2. co_parent_id, no partner match → raw = 0.0
#   3. co_parent_id matches partner  → raw = 1.0
#
# Plus one regression check: meta/goal-selection-strategy.yaml has
# weights.co_invest_alignment set to 0.5.
#
# Run: bash core/scripts/tests/test_co_invest_alignment.sh
# Exit 0 = all pass, 1 = any failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../_paths.sh"

failures=0
pass=0

# ─── Set up isolated test world ─────────────────────────────────────────
TMP=$(mktemp -d)
trap "rm -rf '$TMP'" EXIT

if [ "${MSYSTEM:-}" != "" ] && command -v cygpath &>/dev/null; then
    TMP="$(cygpath -m "$TMP")"
fi

export MIND_WORLD="$TMP/world"
export MIND_META="$TMP/meta"
export MIND_AGENT_DIR="$TMP/alpha"
mkdir -p "$MIND_WORLD" "$MIND_META" "$MIND_AGENT_DIR/session"

ASP_JSONL="$MIND_WORLD/aspirations.jsonl"
TEAM_STATE="$MIND_WORLD/team-state.yaml"
GS_STRATEGY="$MIND_META/goal-selection-strategy.yaml"
PIPE_JSONL="$MIND_WORLD/pipeline.jsonl"
PIPE_ARCHIVE="$MIND_WORLD/pipeline-archive.jsonl"

# Aspiration with two pending agent goals — one with co_parent_id, one without.
# Both unblocked and pending so they reach the scorer.
cat >"$ASP_JSONL" <<'EOF'
{"id":"asp-test","title":"Test","description":"t","status":"active","priority":"MEDIUM","scope":"sprint","sessions_active":1,"created":"2026-05-10","archived":false,"goals":[{"id":"g-test-01","title":"plain","description":"plain goal no co_parent_id","status":"pending","priority":"MEDIUM","participants":["agent"],"category":"framework-engineering","co_parent_id":null},{"id":"g-test-02","title":"co-invested","description":"co-invest sub-goal","status":"pending","priority":"MEDIUM","participants":["agent"],"category":"framework-engineering","co_parent_id":"g-test-99"}]}
EOF

# Empty pipeline files (selector reads them).
: > "$PIPE_JSONL"
: > "$PIPE_ARCHIVE"

# Strategy file with co_invest_alignment weight.
cat >"$GS_STRATEGY" <<'EOF'
version: 4
last_updated: "2026-05-10"
weights:
  priority: 1.0
  deadline_urgency: 1.0
  agent_executable: 0.8
  variety_bonus: 0.3
  streak_momentum: 0.5
  novelty_bonus: 0.6
  recurring_urgency: 0.8
  reward_history: 0.5
  evidence_backing: 0.7
  deferred_readiness: 0.6
  context_coherence: 1.0
  skill_affinity: 0.4
  recurring_saturation: 0.8
  completion_pressure: 1.4
  depth_bonus: 0.6
  directive_boost: 1.5
  tail_bonus: 0.8
  handoff_bonus: 1.0
  per_goal_saturation: 0.8
  user_signal_boost: 1.2
  class_balance_bonus: 0.8
  role_affinity: 1.0
  cross_aspiration_support: 0.5
  co_invest_alignment: 0.5
custom_criteria: []
agent_role_multipliers:
  alpha:
    product: 1.5
    framework: 1.3
    hygiene: 1.0
    research: 0.5
    unclassified: 0.0
EOF

# Helper: get the raw co_invest_alignment score for goal_id g-test-02.
get_co_invest_raw() {
    local goal_id="$1"
    MIND_AGENT=alpha py -3 "$CORE_ROOT/scripts/goal-selector.py" select 2>/dev/null \
      | py -3 -c "
import sys, json
arr = json.load(sys.stdin)
for g in arr:
    if g['goal_id'] == '$goal_id':
        print(g['raw'].get('co_invest_alignment', 'MISSING'))
        sys.exit(0)
print('GOAL_MISSING')
"
}

# ─── Scenario 1: no partner team-state → all goals raw = 0.0 ────────────
# (no team-state.yaml file exists yet)
raw_no_partner=$(get_co_invest_raw "g-test-02")
if [ "$raw_no_partner" = "0.0" ] || [ "$raw_no_partner" = "0" ]; then
    echo "CASE 1 PASS: no team-state → raw=0.0 (got $raw_no_partner)"
    pass=$((pass + 1))
else
    echo "CASE 1 FAIL: expected 0.0, got '$raw_no_partner'"
    failures=$((failures + 1))
fi

# ─── Scenario 2: partner exists but no in_flight → raw = 0.0 ────────────
cat >"$TEAM_STATE" <<'EOF'
last_updated: "2026-05-10T12:00:00"
agent_status:
  bravo:
    last_active: "2026-05-10T12:00:00"
    in_flight: null
EOF
raw_no_in_flight=$(get_co_invest_raw "g-test-02")
if [ "$raw_no_in_flight" = "0.0" ] || [ "$raw_no_in_flight" = "0" ]; then
    echo "CASE 2 PASS: partner without in_flight → raw=0.0 (got $raw_no_in_flight)"
    pass=$((pass + 1))
else
    echo "CASE 2 FAIL: expected 0.0, got '$raw_no_in_flight'"
    failures=$((failures + 1))
fi

# ─── Scenario 3: partner in_flight.co_parent_id matches → raw = 1.0 ─────
cat >"$TEAM_STATE" <<'EOF'
last_updated: "2026-05-10T12:00:00"
agent_status:
  bravo:
    last_active: "2026-05-10T12:00:00"
    in_flight:
      goal_id: "g-test-50"
      title: "bravo's sub-goal"
      phase: 4
      claimed_at: "2026-05-10T12:00:00"
      co_parent_id: "g-test-99"
EOF
raw_match=$(get_co_invest_raw "g-test-02")
if [ "$raw_match" = "1.0" ] || [ "$raw_match" = "1" ]; then
    echo "CASE 3 PASS: matching partner co_parent_id → raw=1.0 (got $raw_match)"
    pass=$((pass + 1))
else
    echo "CASE 3 FAIL: expected 1.0, got '$raw_match'"
    failures=$((failures + 1))
fi

# ─── Scenario 4: candidate without co_parent_id stays 0.0 ───────────────
# (verifies the gate doesn't apply to plain goals)
raw_plain=$(get_co_invest_raw "g-test-01")
if [ "$raw_plain" = "0.0" ] || [ "$raw_plain" = "0" ]; then
    echo "CASE 4 PASS: candidate without co_parent_id → raw=0.0 (got $raw_plain)"
    pass=$((pass + 1))
else
    echo "CASE 4 FAIL: expected 0.0, got '$raw_plain'"
    failures=$((failures + 1))
fi

# ─── Regression check: weight in strategy file ──────────────────────────
weight=$(py -3 -c "
import yaml
with open(r'$GS_STRATEGY') as f:
    d = yaml.safe_load(f)
print(d.get('weights', {}).get('co_invest_alignment', 'MISSING'))
")
if [ "$weight" = "0.5" ]; then
    echo "CASE 5 PASS: strategy has weights.co_invest_alignment=0.5"
    pass=$((pass + 1))
else
    echo "CASE 5 FAIL: expected weight=0.5, got '$weight'"
    failures=$((failures + 1))
fi

# ─── Summary ────────────────────────────────────────────────────────────
total=$((pass + failures))
echo
echo "──────────────────────────────────────────"
if [ "$failures" -eq 0 ]; then
    echo "TEST PASS: $pass/$total"
    exit 0
else
    echo "TEST FAIL: $pass passed, $failures failed (total $total)"
    exit 1
fi
