#!/usr/bin/env bash
# test-check-sh-exec-bits-staged.sh — pins `check-sh-exec-bits.sh --staged`
# (), the pre-commit Gate 13 predicate.
#
# WHY THE --staged MODE EXISTS AT ALL: the sibling default mode reads the
# FILESYSTEM (`find ! -perm -u+x`), but what propagates fleet-wide is the mode
# GIT RECORDS. On a clone with core.filemode=false — git's default on Windows,
# and this repo was authored there — a `chmod +x` never reaches the index, so a
# filesystem check passes on the very box introducing the defect and the
# breakage surfaces on someone else's Linux checkout. These two modes answer
# different questions and neither substitutes for the other.
#
# CASE 4 IS THE REASON THIS FILE EXISTS. The first implementation parsed
# `git diff --cached --raw` with awk. Rename detection is ON BY DEFAULT, so a
# rename INSIDE core/scripts emits ":<old> <new> <sha> <sha> R100\t<old>\t<new>"
# — TWO tabs — and an awk stripping to the FIRST tab reported "<old>\t<new>" as
# the path, naming the OLD path, which no longer exists. The gate still refused
# (the mode field was read correctly), so exit codes looked right; only the
# human-facing fix line was unrunnable.
#
# It hid because the OBVIOUS rename test moves a file INTO core/scripts from
# outside the pathspec, and git renders that as a plain ADD with ONE tab — the
# convenient shape, not the production one (guard-920). The fix asks git for the
# paths (`--name-only`, which yields the NEW path for a rename) and then asks
# git for each path's mode (`ls-files -s`), rather than hand-parsing --raw at
# all (guard-1083: never write a parsing pipe against an output shape you have
# not looked at; guard-1989: use a real scanner).
#
# HERMETIC: every case runs in its own throwaway git repo with a COPY of the
# script. No real repo, index, or hook is touched.
#
# Run: bash core/scripts/tests/test-check-sh-exec-bits-staged.sh

set -uo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SCRIPTS_DIR/check-sh-exec-bits.sh"
PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# Build a throwaway repo with two committed, executable scripts. Echoes the path.
new_repo() {
    local d
    d="$(mktemp -d)"
    git -C "$d" init -q -b main .
    git -C "$d" config user.email t@t
    git -C "$d" config user.name t
    git -C "$d" config commit.gpgsign false
    mkdir -p "$d/core/scripts/gates"
    cp "$SCRIPT" "$d/core/scripts/"
    printf 'x\n' > "$d/core/scripts/orig.sh"
    printf 'k\n' > "$d/core/scripts/keep.sh"
    chmod +x "$d/core/scripts/orig.sh" "$d/core/scripts/keep.sh"
    git -C "$d" add -A
    git -C "$d" commit -qm seed
    echo "$d"
}

# Run the gate inside repo $1. Sets GATE_RC and GATE_OUT.
run_gate() {
    GATE_OUT="$(cd "$1" && bash core/scripts/check-sh-exec-bits.sh --staged 2>&1)"
    GATE_RC=$?
}

echo "=== check-sh-exec-bits.sh --staged ==="

# --- 1: nothing staged -> allow -------------------------------------------
R="$(new_repo)"
run_gate "$R"
[[ $GATE_RC -eq 0 ]] && pass "nothing staged -> allow" \
                     || fail "nothing staged: expected rc=0, got $GATE_RC ($GATE_OUT)"
rm -rf "$R"

# --- 2: new .sh at 100644 -> refuse ---------------------------------------
R="$(new_repo)"
printf 'n\n' > "$R/core/scripts/new.sh"
git -C "$R" add core/scripts/new.sh
git -C "$R" update-index --chmod=-x core/scripts/new.sh
run_gate "$R"
if [[ $GATE_RC -ne 0 ]] && grep -q 'core/scripts/new.sh' <<<"$GATE_OUT"; then
    pass "new .sh at 100644 -> refuse, names the file"
else
    fail "new 100644: expected refusal naming new.sh, rc=$GATE_RC out=$GATE_OUT"
fi
rm -rf "$R"

# --- 3: promoted to 100755 -> allow ---------------------------------------
R="$(new_repo)"
printf 'n\n' > "$R/core/scripts/new.sh"
git -C "$R" add core/scripts/new.sh
git -C "$R" update-index --chmod=+x core/scripts/new.sh
run_gate "$R"
[[ $GATE_RC -eq 0 ]] && pass "new .sh at 100755 -> allow" \
                     || fail "new 100755: expected rc=0, got $GATE_RC ($GATE_OUT)"
rm -rf "$R"

# --- 4: RENAME inside core/scripts, demoted -> refuse naming the NEW path --
# THE REGRESSION CASE. A --raw-parsing implementation prints "<old>\t<new>"
# here and names the OLD path. Assert the NEW path is named AND the old one is
# NOT, so a reader is never handed a chmod target that no longer exists.
R="$(new_repo)"
git -C "$R" mv core/scripts/orig.sh core/scripts/renamed.sh
git -C "$R" update-index --chmod=-x core/scripts/renamed.sh
run_gate "$R"
if [[ $GATE_RC -ne 0 ]] \
   && grep -q 'core/scripts/renamed.sh' <<<"$GATE_OUT" \
   && ! grep -q 'core/scripts/orig.sh' <<<"$GATE_OUT"; then
    pass "rename inside core/scripts -> refuse, names ONLY the new path"
else
    fail "rename: want refusal naming renamed.sh and NOT orig.sh; rc=$GATE_RC out=$GATE_OUT"
fi
rm -rf "$R"

# --- 5: staged deletion -> allow (no mode to gate) ------------------------
R="$(new_repo)"
git -C "$R" rm -q core/scripts/keep.sh
run_gate "$R"
[[ $GATE_RC -eq 0 ]] && pass "staged deletion -> allow" \
                     || fail "deletion: expected rc=0, got $GATE_RC ($GATE_OUT)"
rm -rf "$R"

# --- 6: subdirectory .sh -> refuse ----------------------------------------
# git pathspec wildcards cross '/', so core/scripts/*.sh reaches gates/.
# That matches the default mode's `find` recursion; the two stay consistent.
R="$(new_repo)"
printf 's\n' > "$R/core/scripts/gates/sub.sh"
git -C "$R" add -A
git -C "$R" update-index --chmod=-x core/scripts/gates/sub.sh
run_gate "$R"
if [[ $GATE_RC -ne 0 ]] && grep -q 'core/scripts/gates/sub.sh' <<<"$GATE_OUT"; then
    pass "subdirectory .sh at 100644 -> refuse"
else
    fail "subdir: expected refusal naming gates/sub.sh, rc=$GATE_RC out=$GATE_OUT"
fi
rm -rf "$R"

# --- 7: path containing a space -> refuse, path intact --------------------
R="$(new_repo)"
printf 'z\n' > "$R/core/scripts/has space.sh"
git -C "$R" add -A
git -C "$R" update-index --chmod=-x "core/scripts/has space.sh"
run_gate "$R"
if [[ $GATE_RC -ne 0 ]] && grep -q 'core/scripts/has space.sh' <<<"$GATE_OUT"; then
    pass "path with a space -> refuse, path preserved intact"
else
    fail "spaced path: expected refusal naming it whole, rc=$GATE_RC out=$GATE_OUT"
fi
rm -rf "$R"

# --- 8: default (filesystem) mode still works -----------------------------
# The --staged branch returns early; this pins that it did not swallow the
# original behaviour, which /verify-learning still consumes.
R="$(new_repo)"
OUT="$(cd "$R" && bash core/scripts/check-sh-exec-bits.sh 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q 'OK: all' <<<"$OUT"; then
    pass "default filesystem mode still reports OK"
else
    fail "default mode: expected rc=0 and 'OK: all', rc=$RC out=$OUT"
fi
rm -rf "$R"

echo
echo "TOTAL: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
