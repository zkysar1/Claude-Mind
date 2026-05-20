#!/usr/bin/env bash
# test_dotted_path_rejection.sh —  / Option A symmetric reject.
#
# Verifies that all 6 field=value scripts reject dotted field paths and fail
# loud (exit 1, BLOCKED message) per zeta/reports/-dotted-path-
# corruption-decision.md. Pre-2026-05-10 these scripts either silently corrupted
# (aspirations.py, spark-questions.py) or silently honored dotted writes via
# set_nested_field (pipeline.py, reasoning-bank.py x2, experience.py,
# pattern-signatures.py). Decision recorded Option A — symmetric REJECT —
# as the project-wide policy.
#
# Test scenarios:
#   Full fixture (aspirations.py update-goal):
#     1. Positive flat-write: update priority HIGH → exit 0
#     2. Negative dotted-write: verification.outcomes <json> → exit 1, BLOCKED
#     3. Positive full-nested-JSON: verification '<full-json>' → exit 0
#
#   Spot check (one surviving CLI update-field):
#     7. experience.py update-field exp-X utility_ratio.foo 0.1 → exit 1, BLOCKED
#
#   Cases 4-6 (pipeline / reasoning-bank rb / reasoning-bank guard) and
#   cases 8-9 (spark-questions / pattern-signatures) — REMOVED. The CLI
#   update-field subcommands were deleted in the daemon-only migration
#   (H2 Wave 2: 2026-05-14 — rb + pipeline; H2 Wave 3: 2026-05-15 — sq + sig).
#   Dotted-path rejection for those five stores is now enforced server-side
#   by mind_api/src/endpoints/store.py set-field handler (line 388:
#   "." in field_name → HTTP 400) and tested via
#   mind_api/tests/test_runtime_store_patsig_spark.py (plus equivalent
#   suites for pipeline/rb/guard endpoints). Re-adding CLI cases here would
#   test argparse "invalid choice" rejections, NOT the dotted-path guard.
#
# Run: bash core/scripts/tests/test_dotted_path_rejection.sh
# Exit 0 = all pass, 1 = any failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../_paths.sh"

# Set up isolated test world/meta dirs to avoid touching live state.
TMP=$(mktemp -d)
trap "rm -rf '$TMP'" EXIT

# MSYS path conversion: Python (Windows-native) needs C:/ paths, not /tmp/.
if [ "${MSYSTEM:-}" != "" ] && command -v cygpath &>/dev/null; then
    TMP="$(cygpath -m "$TMP")"
fi

export MIND_WORLD="$TMP/world"
export MIND_META="$TMP/meta"
export MIND_AGENT_DIR="$TMP/alpha"
mkdir -p "$MIND_WORLD/board" "$MIND_META" "$MIND_AGENT_DIR/session"

# Empty token for cleaner output below.
NL=$'\n'

failures=0
pass=0

assert_pass() {
    local label="$1"
    local rc="$2"
    local expected="$3"
    local stderr="$4"
    local expect_phrase="${5:-}"

    if [ "$rc" != "$expected" ]; then
        printf 'CASE %s FAIL: expected rc=%s got rc=%s%sstderr: %s\n' \
            "$label" "$expected" "$rc" "$NL" "$stderr"
        failures=$((failures + 1))
        return 1
    fi
    if [ -n "$expect_phrase" ] && ! grep -q "$expect_phrase" <<<"$stderr"; then
        printf 'CASE %s FAIL: stderr missing %q%sstderr: %s\n' \
            "$label" "$expect_phrase" "$NL" "$stderr"
        failures=$((failures + 1))
        return 1
    fi
    printf 'CASE %s PASS\n' "$label"
    pass=$((pass + 1))
    return 0
}

# ─── Fixture seeding ────────────────────────────────────────────────────
# Build minimal JSONL records by hand (scripts' add commands have many
# required fields; direct JSONL keeps the fixture small and stable).
ASP_JSONL="$MIND_WORLD/aspirations.jsonl"
PIPE_JSONL="$MIND_WORLD/pipeline.jsonl"
RB_JSONL="$MIND_WORLD/reasoning-bank.jsonl"
GUARD_JSONL="$MIND_WORLD/guardrails.jsonl"
EXP_JSONL="$MIND_AGENT_DIR/experience.jsonl"
SQ_JSONL="$MIND_META/spark-questions.jsonl"
SIG_JSONL="$MIND_WORLD/pattern-signatures.jsonl"

# Aspiration with a goal that has a verification stub.
cat >"$ASP_JSONL" <<'EOF'
{"id":"asp-test","title":"Test","description":"t","status":"active","priority":"MEDIUM","scope":"sprint","sessions_active":0,"created":"2026-05-10","goals":[{"id":"g-test-01","title":"t","description":"t","status":"pending","priority":"MEDIUM","participants":["agent"],"category":"framework-engineering","verification":{"outcomes":[],"checks":[],"preconditions":[]}}]}
EOF

cat >"$PIPE_JSONL" <<'EOF'
{"id":"2026-05-10_dotted-path-test","kind":"micro","horizon":"micro","statement":"t","status":"discovered","prediction":{"type":"binary","unit":"y","threshold":1},"created":"2026-05-10"}
EOF

cat >"$RB_JSONL" <<'EOF'
{"id":"rb-test","title":"t","type":"success","category":"framework-architecture","applies_to":"framework","content":"t","when_to_use":{"conditions":[],"category":""},"utilization":{"retrieval_count":0,"last_retrieved":null,"times_helpful":0,"times_noise":0,"times_active":0,"times_skipped":0,"times_inferred_helpful":0,"times_inferred_unknown":0,"times_cited":0,"utilization_score":0.0},"created":"2026-05-10","status":"active"}
EOF

cat >"$GUARD_JSONL" <<'EOF'
{"id":"guard-test","rule":"t","category":"framework-architecture","applies_to":"framework","trigger_condition":"t","when_to_use":{"conditions":[],"category":""},"utilization":{"retrieval_count":0,"last_retrieved":null,"times_helpful":0,"times_noise":0,"times_active":0,"times_skipped":0,"times_inferred_helpful":0,"times_inferred_unknown":0,"times_cited":0,"utilization_score":0.0},"created":"2026-05-10","status":"active"}
EOF

cat >"$EXP_JSONL" <<'EOF'
{"id":"exp-test","goal_id":"g-test-01","outcome":"deep","date":"2026-05-10","content":"t","reasoning_chain":"t","decisions":"t","tool_outputs":"t","verbatim_anchors":"t","surprises":"t"}
EOF

cat >"$SQ_JSONL" <<'EOF'
{"id":"sq-test","type":"question","text":"t","category":"surprise","priority":"MEDIUM","status":"active","times_asked":0,"sparks_generated":0,"yield_rate":0.0,"created":"2026-05-10"}
EOF

cat >"$SIG_JSONL" <<'EOF'
{"id":"sig-test","strategy":"t","trigger_pattern":"t","outcome_stats":{"total":0,"correct":0,"failed":0,"accuracy":0.0,"last_outcomes":[]},"created":"2026-05-10","status":"active"}
EOF

# ─── Case 1: aspirations.py positive flat-write ─────────────────────────
out=$(py -3 "$CORE_ROOT/scripts/aspirations.py" --source world update-goal g-test-01 priority HIGH 2>&1)
rc=$?
assert_pass "1 (asp flat priority)" "$rc" 0 "$out" || true

# Verify the write actually landed.
new_priority=$(py -3 -c "
import json
for line in open(r'$ASP_JSONL'):
    asp = json.loads(line)
    for g in asp.get('goals', []):
        if g['id'] == 'g-test-01':
            print(g.get('priority'))
")
if [ "$new_priority" = "HIGH" ]; then
    echo "CASE 1b PASS: priority=HIGH on disk"
    pass=$((pass + 1))
else
    echo "CASE 1b FAIL: expected HIGH on disk, got '$new_priority'"
    failures=$((failures + 1))
fi

# ─── Case 2: aspirations.py negative dotted-write ───────────────────────
out=$(py -3 "$CORE_ROOT/scripts/aspirations.py" --source world update-goal g-test-01 verification.outcomes '["x"]' 2>&1)
rc=$?
assert_pass "2 (asp dotted)" "$rc" 1 "$out" "BLOCKED" || true

# Verify outcomes did NOT get the literal dotted key.
has_dotted=$(py -3 -c "
import json
for line in open(r'$ASP_JSONL'):
    asp = json.loads(line)
    for g in asp.get('goals', []):
        if g['id'] == 'g-test-01':
            print('YES' if 'verification.outcomes' in g else 'NO')
")
if [ "$has_dotted" = "NO" ]; then
    echo "CASE 2b PASS: no literal 'verification.outcomes' key"
    pass=$((pass + 1))
else
    echo "CASE 2b FAIL: literal 'verification.outcomes' key was created"
    failures=$((failures + 1))
fi

# ─── Case 3: aspirations.py positive full-nested-JSON ───────────────────
out=$(py -3 "$CORE_ROOT/scripts/aspirations.py" --source world update-goal g-test-01 verification '{"outcomes":["a"],"checks":[],"preconditions":[]}' 2>&1)
rc=$?
assert_pass "3 (asp nested-JSON)" "$rc" 0 "$out" || true

# Verify nested write landed correctly.
nested_landed=$(py -3 -c "
import json
for line in open(r'$ASP_JSONL'):
    asp = json.loads(line)
    for g in asp.get('goals', []):
        if g['id'] == 'g-test-01':
            v = g.get('verification', {})
            print('YES' if v.get('outcomes') == ['a'] else 'NO')
")
if [ "$nested_landed" = "YES" ]; then
    echo "CASE 3b PASS: verification.outcomes=['a'] correctly nested"
    pass=$((pass + 1))
else
    echo "CASE 3b FAIL: nested write did not land correctly"
    failures=$((failures + 1))
fi

# ─── Case 7: spot-check experience.py (only surviving CLI update-field) ──
out=$(py -3 "$CORE_ROOT/scripts/experience.py" update-field exp-test content.subkey x 2>&1)
rc=$?
assert_pass "7 (exp dotted)" "$rc" 1 "$out" "BLOCKED" || true

# Cases 4-6 (pipeline.py, reasoning-bank.py rb, reasoning-bank.py guard) and
# Cases 8-9 (spark-questions.py, pattern-signatures.py) were REMOVED from the
# CLI in the H2 Wave 2/3 daemon-only migration (2026-05-14 / 2026-05-15):
#   - reasoning-bank.py: CLI subcommands deleted (Wave 2)
#   - pipeline.py: update-field removed; only `recompute-meta` remains (Wave 2)
#   - spark-questions.py: update-field gutted (Wave 3)
#   - pattern-signatures.py: update-field gutted (Wave 3)
# Dotted-path rejection for these five stores is now enforced server-side by
# mind_api/src/endpoints/store.py set-field handler (line 388:
# "." in field_name → HTTP 400), and tested via
# mind_api/tests/test_runtime_store_patsig_spark.py (and equivalent suites
# for pipeline/rb/guard endpoints). Re-adding CLI cases here would test
# argparse "invalid choice" rejections, NOT the dotted-path guard contract.

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
