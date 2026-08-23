#!/usr/bin/env bash
# test-sentinel-clear-guarded.sh — pins core/scripts/sentinel-clear-guarded.sh
# (, routed from ZDS/omni).
#
# HERMETIC BY CONSTRUCTION, and deliberately so. guard-1006 forbids probing a
# write-path gate by running the PRODUCTION write command with a throwaway
# payload, so no real store writer is invoked here: the producing commands are
# synthetic (`true`, `false`, `echo`) and the slot clear is redirected to a STUB.
#
# The stub trick is what makes "was the clear attempted?" directly observable
# rather than inferred. sentinel-clear-guarded.sh resolves its clear helper via
# `dirname "${BASH_SOURCE[0]}"`, so copying it into a tmp dir beside a fake
# helper routes every clear to the fake, which records the call. A test that
# only asserted exit codes could not distinguish "refused to clear" from
# "cleared and then reported failure" — which is the exact confusion the
# primitive exists to prevent.
#
# THE STUB IS NAMED FOR THE HELPER THE SCRIPT ACTUALLY CALLS, and that name
# changed in : the clear now routes through verified-wm-set.sh rather
# than a bare wm-set.sh. Stubbing the old name would not fail loudly — the
# script would look for a verified-wm-set.sh that is not there, every happy-path
# case would report clear=no, and the suite would read as a behaviour regression
# in the script rather than a stale stub. If a future change re-points the
# clear, this stub's FILENAME moves with it.
#
# Run: bash core/scripts/tests/test-sentinel-clear-guarded.sh

set -uo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$SCRIPTS_DIR/sentinel-clear-guarded.sh"

if [[ ! -f "$SRC" ]]; then
    echo "SETUP FAIL: $SRC not found"
    exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cp "$SRC" "$TMP/sentinel-clear-guarded.sh"

# Copy the registry SSOT beside the script. LOAD-BEARING, not convenience: the
# script resolves _sentinel_registry.py via `dirname "${BASH_SOURCE[0]}"`, so
# without this the slot validation hits its fail-OPEN validator-error branch on
# EVERY case and the tests below exercise none of it. Found the hard way — the
# suite reported 12/12 green against a validation path it never once entered,
# which is the guard-920 production-shape class turned inside out: the only
# environment where the check runs was the only environment the tests avoided.
cp "$SCRIPTS_DIR/_sentinel_registry.py" "$TMP/_sentinel_registry.py" 2>/dev/null || {
    echo "SETUP FAIL: could not copy _sentinel_registry.py — slot validation would fail open and the validation cases below would be vacuous"
    exit 2
}

CLEAR_LOG="$TMP/clear-calls.log"

# Stub verified-wm-set.sh — records every invocation so the test can assert
# whether a clear was even ATTEMPTED, not merely whether the script exited
# non-zero. Honours CLEAR_STUB_RC so the clear-FAILURE branch can be forced;
# defaults to 0 (success) for every case that does not set it.
cat > "$TMP/verified-wm-set.sh" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null   # drain the piped value
echo "CLEAR:$1" >> "$CLEAR_LOG_PATH"
exit "${CLEAR_STUB_RC:-0}"
STUB
chmod +x "$TMP/verified-wm-set.sh" "$TMP/sentinel-clear-guarded.sh"
export CLEAR_LOG_PATH="$CLEAR_LOG"

PASS=0
FAIL=0

# run_case <name> <expected_rc> <expect_clear:yes|no> <expected_stderr_substr> -- <args...>
run_case() {
    local name="$1" exp_rc="$2" exp_clear="$3" exp_msg="$4"
    shift 4
    [[ "$1" == "--" ]] && shift

    : > "$CLEAR_LOG"
    local out rc
    out="$(bash "$TMP/sentinel-clear-guarded.sh" "$@" 2>&1)"
    rc=$?

    local got_clear="no"
    [[ -s "$CLEAR_LOG" ]] && got_clear="yes"

    local ok=1
    [[ "$rc" == "$exp_rc" ]] || ok=0
    [[ "$got_clear" == "$exp_clear" ]] || ok=0
    if [[ -n "$exp_msg" && "$out" != *"$exp_msg"* ]]; then ok=0; fi

    if [[ $ok -eq 1 ]]; then
        printf '  PASS  %-52s rc=%s clear=%s\n' "$name" "$rc" "$got_clear"
        PASS=$((PASS+1))
    else
        printf '  FAIL  %-52s rc=%s (want %s) clear=%s (want %s)\n' \
            "$name" "$rc" "$exp_rc" "$got_clear" "$exp_clear"
        [[ -n "$exp_msg" && "$out" != *"$exp_msg"* ]] && printf '        missing stderr substring: %s\n' "$exp_msg"
        printf '        output: %s\n' "${out:0:220}"
        FAIL=$((FAIL+1))
    fi
}

echo "sentinel-clear-guarded.sh — behaviour pins"
echo

# ── Outcome 1: producing command FAILED -> slot must stay SET ───────────────
run_case "producing cmd rc!=0 -> refuse, slot stays SET" 1 no \
    "producing command exited rc=7" \
    -- --slot force_experience_archival --verify 'echo found-a-record' -- bash -c 'exit 7'

# ── Outcome 2: the load-bearing one. Producing cmd exits 0 but wrote NOTHING.
# This is the rc=0-on-refusal case that `&&` cannot catch, which is the entire
# reason the primitive owns the read-back.
run_case "rc=0 but read-back EMPTY -> refuse, slot stays SET" 2 no \
    "read-back returned EMPTY" \
    -- --slot force_experience_archival --verify 'echo -n ""' -- true

run_case "rc=0 but read-back whitespace-only -> refuse" 2 no \
    "read-back returned EMPTY" \
    -- --slot force_experience_archival --verify 'printf "   \n  "' -- true

# ── read-back itself errored ────────────────────────────────────────────────
run_case "read-back rc!=0 -> refuse, slot stays SET" 2 no \
    "read-back exited rc=3" \
    -- --slot force_tree_maintain --verify 'exit 3' -- true

# ── --expect gives an identity check, not just non-emptiness ────────────────
run_case "--expect substring absent -> refuse" 2 no \
    "did not contain --expect" \
    -- --slot force_tree_maintain --verify 'echo some-other-id' --expect 'exp-wanted-id' -- true

run_case "--expect substring present -> clear" 0 yes \
    "cleared" \
    -- --slot force_tree_maintain --verify 'echo exp-wanted-id here' --expect 'exp-wanted-id' -- true

# ── Happy path ──────────────────────────────────────────────────────────────
run_case "both checks pass -> slot cleared" 0 yes \
    "read-back confirmed the write landed" \
    -- --slot force_experience_archival --verify 'echo exp-2026-07-31-x' -- true

# ── Fail-closed on caller error. Omitting --verify must be FATAL, never a
# silent degrade to exit-status-only (that would reintroduce the whole defect).
run_case "missing --verify -> usage error, never clears" 3 no \
    "--verify is required" \
    -- --slot force_tree_maintain -- true

run_case "missing --slot -> usage error" 3 no \
    "--slot is required" \
    -- --verify 'echo x' -- true

run_case "no producing command after -- -> usage error" 3 no \
    "no producing command" \
    -- --slot force_tree_maintain --verify 'echo x' --

# ── --dry-run reports the verdict without clearing ──────────────────────────
run_case "--dry-run passes checks but does NOT clear" 0 no \
    "DRY-RUN" \
    -- --slot force_tree_maintain --verify 'echo x' --dry-run -- true

# ── Diagnostic must NAME the slot it left set (outcome 3) ───────────────────
run_case "diagnostic names the slot left SET" 1 no \
    "LEFT SET" \
    -- --slot force_metric_encoding_pending --verify 'echo x' -- false

# ── The clear itself FAILED () ────────────────────────────────────
# Had ZERO coverage before this: the stub always exited 0, so every case above
# is vacuous with respect to this branch — it can only be observed by forcing
# the antecedent (guard-2982). It is also the branch that changed, since the
# clear now routes through verified-wm-set.sh, whose whole contribution is
# exiting non-zero when the write did not persist. The clear must still be
# ATTEMPTED (clear=yes) and the script must NOT report success.
export CLEAR_STUB_RC=1
run_case "clear helper fails -> rc=3, reports slot still SET" 3 yes \
    "clearing slot 'force_tree_maintain' FAILED" \
    -- --slot force_tree_maintain --verify 'echo x' -- true
unset CLEAR_STUB_RC

# ── Slot validation against the registry SSOT ───────────────────────────────
# Found by fresh-eyes review of the script itself: a typo'd slot ran the whole
# pipeline, cleared the nonexistent slot, and reported success while the REAL
# sentinel stayed SET. The read-back cannot catch that — it proves the WRITE
# landed, not that the SLOT being cleared is the one that gates it.
run_case "unregistered slot -> usage error, never clears" 3 no \
    "is not a registered sentinel slot" \
    -- --slot force_experience_archivl --verify 'echo x' -- true

# The refusal must come BEFORE the producing command runs — a typo'd slot must
# not execute a real write. Asserted directly via a marker file, not inferred
# from the exit code (rc=3 alone cannot distinguish "refused early" from
# "ran the producer, then refused").
MARKER="$TMP/producer-ran.marker"
rm -f "$MARKER"
: > "$CLEAR_LOG"
bash "$TMP/sentinel-clear-guarded.sh" --slot totally_not_a_slot --verify 'echo x' \
     -- touch "$MARKER" >/dev/null 2>&1
if [[ -f "$MARKER" ]]; then
    printf '  FAIL  %-52s producing command RAN despite invalid slot\n' "invalid slot refuses before running producer"
    FAIL=$((FAIL+1))
else
    printf '  PASS  %-52s producer never ran\n' "invalid slot refuses before running producer"
    PASS=$((PASS+1))
fi

# Validator error must fail OPEN, not closed: if the registry is unreadable the
# script must WARN and proceed, because wedging the gate on a broken helper is
# worse than the hole, and the read-back is still enforced. Exercised by running
# from a dir with no registry beside the script — which is exactly the shape this
# whole suite silently had before the registry copy above was added.
NOREG="$(mktemp -d)"
cp "$TMP/sentinel-clear-guarded.sh" "$TMP/verified-wm-set.sh" "$NOREG/"
: > "$CLEAR_LOG"
noreg_out="$(bash "$NOREG/sentinel-clear-guarded.sh" --slot force_experience_archival \
              --verify 'echo x' -- true 2>&1)"
noreg_rc=$?
if [[ "$noreg_rc" == "0" && "$noreg_out" == *"could not validate"* && -s "$CLEAR_LOG" ]]; then
    printf '  PASS  %-52s rc=0 warned+proceeded\n' "registry unreadable -> fail OPEN"
    PASS=$((PASS+1))
else
    printf '  FAIL  %-52s rc=%s (want 0 + warn + clear)\n' "registry unreadable -> fail OPEN" "$noreg_rc"
    printf '        output: %s\n' "${noreg_out:0:200}"
    FAIL=$((FAIL+1))
fi
rm -rf "$NOREG"

echo
echo "TOTAL: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
