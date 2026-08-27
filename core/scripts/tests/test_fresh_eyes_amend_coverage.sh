#!/usr/bin/env bash
# test_fresh_eyes_amend_coverage.sh —  / amend-detection.
#
# Verifies the post-state-update-gate.sh cooldown coverage check correctly
# distinguishes path-only matches from content-amendment cases. Pre-573 the
# subset check was path-only and silently false-suppressed dispatch when a
# later commit covered the same paths but with different content. Fix:
# extend the WM/team-state record schema with a content_signatures dict
# (sha1[:12] per path) and require sig match for sig-bearing records;
# pre-573 records (no content_signatures) fall through to path-only by
# design (backward-compat).
#
# Test scenarios (4 fixtures via _fresh_eyes_coverage_check.py + 1 helper test):
#   A. _fresh_eyes_signatures.py output (direct unit test)
#   1. Single-commit-at-end: identical content → covered → verdict yes:self.
#   2. Mid-execution-commit: disjoint file set → not covered → verdict no.
#   3. Amend-after-Step-1.75: same paths, different content →
#        pre-fix: false yes:self (suppress, BUG)
#        post-fix: no (fire, CORRECT).
#   4. Backward-compat: pre-573 record without content_signatures → path-only
#        fallback → verdict yes:self.
#
# Plus regression checks:
#   - content_signatures key appears in post-state-update-gate.sh writer block.
#   - content_signatures key appears in fresh-eyes-code/SKILL.md Phase 5b.
#
# Run: bash core/scripts/tests/test_fresh_eyes_amend_coverage.sh
# Exit 0 = all pass, 1 = any failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../_paths.sh"

failures=0
pass=0
total=0

NL=$'\n'

assert_equal() {
    local label="$1"
    local actual="$2"
    local expected="$3"
    total=$((total + 1))
    if [ "$actual" = "$expected" ]; then
        echo "CASE $label PASS"
        pass=$((pass + 1))
        return 0
    fi
    printf 'CASE %s FAIL: expected %q, got %q\n' "$label" "$expected" "$actual"
    failures=$((failures + 1))
    return 1
}

assert_contains() {
    local label="$1"
    local haystack="$2"
    local needle="$3"
    total=$((total + 1))
    case "$haystack" in
        *"$needle"*)
            echo "CASE $label PASS"
            pass=$((pass + 1))
            return 0
            ;;
    esac
    printf 'CASE %s FAIL: expected substring %q in %q\n' "$label" "$needle" "$haystack"
    failures=$((failures + 1))
    return 1
}

# ─── Set up isolated fixture root ───────────────────────────────────────
TMP=$(mktemp -d)
trap "rm -rf '$TMP'" EXIT

if [ "${MSYSTEM:-}" != "" ] && command -v cygpath &>/dev/null; then
    TMP="$(cygpath -m "$TMP")"
fi

mkdir -p "$TMP/core/scripts" "$TMP/world"
TEAM_STATE="$TMP/world/team-state.yaml"

# Minimal team-state.yaml — peer records seeded per-fixture (helper tolerates
# missing file: peer_records returns empty).
: > "$TEAM_STATE"

# Helper: invoke _fresh_eyes_coverage_check.py with controlled env. Returns
# stdout (verdict line + covered JSON line). Uses TMP as PROJECT_ROOT so
# file_sig() reads our fixture files.
run_check() {
    local current="$1"          # newline-separated paths
    local cooldown_json="$2"    # JSON list of records (or "null")
    local self_agent="${3:-alpha}"
    CURRENT="$current" \
    COOLDOWN_JSON="$cooldown_json" \
    COOLDOWN_HOURS="4" \
    TEAM_STATE_PATH="$TEAM_STATE" \
    SELF_AGENT="$self_agent" \
    PROJECT_ROOT="$TMP" \
    SUPPRESSION_AUDIT_PATH="" \
    py -3 "$CORE_ROOT/scripts/_fresh_eyes_coverage_check.py"
}

# Helper: compute sha1[:12] of TMP/<rel> (mirrors the production hash).
sig_of() {
    local rel="$1"
    py -3 -c "
import hashlib, sys
with open(r'$TMP/$rel','rb') as f:
    print(hashlib.sha1(f.read()).hexdigest()[:12])
"
}

# Helper: ISO timestamp now.
now_iso() {
    py -3 -c "from datetime import datetime; print(datetime.now().isoformat(timespec='seconds'))"
}

# ─── Case A: _fresh_eyes_signatures.py direct unit test ─────────────────
mkdir -p "$TMP/core/scripts"
echo "alpha-content" > "$TMP/core/scripts/fixA1.sh"
echo "bravo-content" > "$TMP/core/scripts/fixA2.sh"

sigA1=$(sig_of "core/scripts/fixA1.sh")
sigA2=$(sig_of "core/scripts/fixA2.sh")

# Test the helper with two known files + one missing.
hd_out=$(PROJECT_ROOT="$TMP" py -3 "$CORE_ROOT/scripts/_fresh_eyes_signatures.py" \
    --files-json '["core/scripts/fixA1.sh","core/scripts/fixA2.sh","core/scripts/missing.sh"]')

# Parse output and verify expected sigs present, missing key absent.
verifyA=$(echo "$hd_out" | py -3 -c "
import json, sys
d = json.loads(sys.stdin.read())
ok = (
    d.get('core/scripts/fixA1.sh') == '$sigA1'
    and d.get('core/scripts/fixA2.sh') == '$sigA2'
    and 'core/scripts/missing.sh' not in d
)
print('ok' if ok else f'fail: {d}')
")
assert_equal "A (signatures helper)" "$verifyA" "ok" || true

# ─── Case 1: single-commit-at-end (identical content → covered) ─────────
mkdir -p "$TMP/core/scripts"
echo "v1" > "$TMP/core/scripts/fix1a.sh"
echo "v1" > "$TMP/core/scripts/fix1b.sh"
echo "v1" > "$TMP/core/scripts/fix1c.sh"

s1a=$(sig_of "core/scripts/fix1a.sh")
s1b=$(sig_of "core/scripts/fix1b.sh")
s1c=$(sig_of "core/scripts/fix1c.sh")
ts=$(now_iso)

cooldown1=$(py -3 -c "
import json
print(json.dumps([{
    'time': '$ts',
    'files': ['core/scripts/fix1a.sh','core/scripts/fix1b.sh','core/scripts/fix1c.sh'],
    'content_signatures': {
        'core/scripts/fix1a.sh': '$s1a',
        'core/scripts/fix1b.sh': '$s1b',
        'core/scripts/fix1c.sh': '$s1c',
    }
}]))
")

current1=$'core/scripts/fix1a.sh\ncore/scripts/fix1b.sh\ncore/scripts/fix1c.sh'
out1=$(run_check "$current1" "$cooldown1")
verdict1=$(echo "$out1" | sed -n '1p')
assert_equal "1 (single-commit-at-end → suppress)" "$verdict1" "yes:self" || true

# ─── Case 2: mid-execution-commit (disjoint files → fire) ───────────────
echo "v1" > "$TMP/core/scripts/fix2a.sh"
echo "v1" > "$TMP/core/scripts/fix2b.sh"
echo "v1" > "$TMP/core/scripts/fix2c.sh"

# Cooldown record has DIFFERENT files than current change set.
cooldown2=$(py -3 -c "
import json
print(json.dumps([{
    'time': '$ts',
    'files': ['core/scripts/fix1a.sh','core/scripts/fix1b.sh','core/scripts/fix1c.sh'],
    'content_signatures': {
        'core/scripts/fix1a.sh': '$s1a',
        'core/scripts/fix1b.sh': '$s1b',
        'core/scripts/fix1c.sh': '$s1c',
    }
}]))
")

current2=$'core/scripts/fix2a.sh\ncore/scripts/fix2b.sh\ncore/scripts/fix2c.sh'
out2=$(run_check "$current2" "$cooldown2")
verdict2=$(echo "$out2" | sed -n '1p')
assert_equal "2 (mid-execution-commit → fire)" "$verdict2" "no" || true

# ─── Case 3: amend-after-Step-1.75 (same paths, different content → fire) ──
mkdir -p "$TMP/core/scripts"
echo "v1" > "$TMP/core/scripts/fix3a.sh"
echo "v1" > "$TMP/core/scripts/fix3b.sh"
echo "v1" > "$TMP/core/scripts/fix3c.sh"

# Capture v1 sigs (these go into the cooldown record).
s3a_v1=$(sig_of "core/scripts/fix3a.sh")
s3b_v1=$(sig_of "core/scripts/fix3b.sh")
s3c_v1=$(sig_of "core/scripts/fix3c.sh")

cooldown3=$(py -3 -c "
import json
print(json.dumps([{
    'time': '$ts',
    'files': ['core/scripts/fix3a.sh','core/scripts/fix3b.sh','core/scripts/fix3c.sh'],
    'content_signatures': {
        'core/scripts/fix3a.sh': '$s3a_v1',
        'core/scripts/fix3b.sh': '$s3b_v1',
        'core/scripts/fix3c.sh': '$s3c_v1',
    }
}]))
")

# AMEND each file (different content, same paths). This is the
# Step-1.75 case — content changes after fresh-eyes already reviewed v1.
echo "v2-amended" > "$TMP/core/scripts/fix3a.sh"
echo "v2-amended" > "$TMP/core/scripts/fix3b.sh"
echo "v2-amended" > "$TMP/core/scripts/fix3c.sh"

current3=$'core/scripts/fix3a.sh\ncore/scripts/fix3b.sh\ncore/scripts/fix3c.sh'
out3=$(run_check "$current3" "$cooldown3")
verdict3=$(echo "$out3" | sed -n '1p')
covered3=$(echo "$out3" | sed -n '2p')

# Post-fix expectation: verdict=no (sig mismatch on all 3 paths means
# none are covered). covered list is [] (empty array, NOT containing
# any of the amended paths).
assert_equal "3 (amend-after-Step-1.75 → fire)" "$verdict3" "no" || true
assert_equal "3b (covered set excludes amended paths)" "$covered3" "[]" || true

# ─── Case 4: backward-compat pre-upgrade record (no content_signatures) ──
echo "v1" > "$TMP/core/scripts/fix4a.sh"
echo "v1" > "$TMP/core/scripts/fix4b.sh"
echo "v1" > "$TMP/core/scripts/fix4c.sh"

# Cooldown record WITHOUT content_signatures (pre-573 schema).
cooldown4=$(py -3 -c "
import json
print(json.dumps([{
    'time': '$ts',
    'files': ['core/scripts/fix4a.sh','core/scripts/fix4b.sh','core/scripts/fix4c.sh']
}]))
")

current4=$'core/scripts/fix4a.sh\ncore/scripts/fix4b.sh\ncore/scripts/fix4c.sh'
out4=$(run_check "$current4" "$cooldown4")
verdict4=$(echo "$out4" | sed -n '1p')
assert_equal "4 (backward-compat path-only → suppress)" "$verdict4" "yes:self" || true

# ─── Case 5: regression — content_signatures in gate writer block ───────
gate_writer_check=$(grep -c "content_signatures" "$CORE_ROOT/scripts/post-state-update-gate.sh" || echo 0)
total=$((total + 1))
if [ "$gate_writer_check" -ge 1 ]; then
    echo "CASE 5 PASS: content_signatures appears in post-state-update-gate.sh"
    pass=$((pass + 1))
else
    echo "CASE 5 FAIL: content_signatures missing from post-state-update-gate.sh"
    failures=$((failures + 1))
fi

# ─── Case 6: regression — content_signatures in SKILL.md Phase 5b ───────
skill_check=$(grep -c "content_signatures" "$PROJECT_ROOT/.claude/skills/fresh-eyes-code/SKILL.md" || echo 0)
total=$((total + 1))
if [ "$skill_check" -ge 1 ]; then
    echo "CASE 6 PASS: content_signatures appears in fresh-eyes-code/SKILL.md"
    pass=$((pass + 1))
else
    echo "CASE 6 FAIL: content_signatures missing from fresh-eyes-code/SKILL.md"
    failures=$((failures + 1))
fi

# ─── Case 7: peer-coverage record with content_signatures ───────────────
# Sanity check: a peer record with sigs matching current content suppresses
# correctly, and a peer record with sig mismatch fires (cross-agent path).
echo "peer-content-v1" > "$TMP/core/scripts/fix7.sh"
s7=$(sig_of "core/scripts/fix7.sh")

cat > "$TEAM_STATE" <<EOF
last_updated: "$ts"
agent_status:
  bravo:
    last_active: "$ts"
    last_fresh_eyes_run:
      time: "$ts"
      files:
        - core/scripts/fix7.sh
      count: 1
      content_signatures:
        core/scripts/fix7.sh: "$s7"
EOF

# Run as alpha — bravo is peer.
out7=$(run_check $'core/scripts/fix7.sh' "null" "alpha")
verdict7=$(echo "$out7" | sed -n '1p')
assert_equal "7 (peer sig match → suppress)" "$verdict7" "yes:peer" || true

# Now amend the file → peer sig mismatch.
echo "peer-content-v2-amended" > "$TMP/core/scripts/fix7.sh"
out7b=$(run_check $'core/scripts/fix7.sh' "null" "alpha")
verdict7b=$(echo "$out7b" | sed -n '1p')
assert_equal "7b (peer sig mismatch → fire)" "$verdict7b" "no" || true

# Reset team-state for any subsequent fixtures.
: > "$TEAM_STATE"

# ─── Case 8:  — self team-state record covers own dispatch ──────
# Verifies parse_self_ts_record() reads SELF agent's
# team-state.<self>.last_fresh_eyes_run and treats it as own-side coverage.
# Without this, /fresh-eyes-code Phase 5b writes are invisible to the gate
# (peer-scan skips self; WM fresh_eyes_last_fire is written only when the
# gate ITSELF fires, not when /fresh-eyes-code runs voluntarily).
#
# Scenario: agent 'bravo' invokes /fresh-eyes-code, which writes
# team-state.bravo.last_fresh_eyes_run for files [X,Y,Z]. Subsequent gate
# dispatch from a deep state-update for the same agent on same dirty state
# must suppress (verdict=yes:self), not re-dispatch.
echo "self-ts-content" > "$TMP/core/scripts/fix8a.sh"
echo "self-ts-content" > "$TMP/core/scripts/fix8b.sh"
echo "self-ts-content" > "$TMP/core/scripts/fix8c.sh"
s8a=$(sig_of "core/scripts/fix8a.sh")
s8b=$(sig_of "core/scripts/fix8b.sh")
s8c=$(sig_of "core/scripts/fix8c.sh")

cat > "$TEAM_STATE" <<EOF
last_updated: "$ts"
agent_status:
  bravo:
    last_active: "$ts"
    last_fresh_eyes_run:
      time: "$ts"
      files:
        - core/scripts/fix8a.sh
        - core/scripts/fix8b.sh
        - core/scripts/fix8c.sh
      count: 3
      content_signatures:
        core/scripts/fix8a.sh: "$s8a"
        core/scripts/fix8b.sh: "$s8b"
        core/scripts/fix8c.sh: "$s8c"
EOF

# Run as bravo (self), COOLDOWN_JSON=null (no WM record — simulating /fresh-eyes-code
# write WITHOUT corresponding gate firing). Expect yes:self via parse_self_ts_record.
current8=$'core/scripts/fix8a.sh\ncore/scripts/fix8b.sh\ncore/scripts/fix8c.sh'
out8=$(run_check "$current8" "null" "bravo")
verdict8=$(echo "$out8" | sed -n '1p')
assert_equal "8 (self team-state record → suppress as own)" "$verdict8" "yes:self" || true

# Case 8b: amend a file → sig mismatch → fire (regression: self_ts must
# obey same sig-aware coverage rules as own_records / peer_records).
echo "self-ts-content-AMENDED" > "$TMP/core/scripts/fix8a.sh"
out8b=$(run_check "$current8" "null" "bravo")
verdict8b=$(echo "$out8b" | sed -n '1p')
assert_equal "8b (self team-state sig mismatch → fire)" "$verdict8b" "no" || true

# Reset team-state.
: > "$TEAM_STATE"

# ─── Summary ────────────────────────────────────────────────────────────
echo
echo "──────────────────────────────────────────"
if [ "$failures" -eq 0 ]; then
    echo "TEST PASS: $pass/$total"
    exit 0
else
    echo "TEST FAIL: $pass passed, $failures failed (total $total)"
    exit 1
fi
