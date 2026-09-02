#!/usr/bin/env bash
# /seed verify <destination> — post-plant smoke test (9 checks).
#
# Usage: seed-verify.sh <destination> [--manifest <path>] [--expect-commit]
#
# Outputs structured PASS/FAIL/WARN per check. Exits non-zero if any FAIL.
# --expect-commit: grade this as a POST-PLANT verify, where a dirty destination
# tree is a FAIL rather than an informational note ().
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

DEST=""
MANIFEST="$CONFIG_DIR/seed-manifest.yaml"
# --expect-commit is OPT-IN on purpose (). This script has TWO callers
# with different contracts: a standalone `/seed verify <dest>` inspects whatever
# state the destination happens to be in (a dirty tree there is informational),
# while a POST-PLANT verify knows a commit was supposed to land, so a dirty tree
# is proof the plant failed. Defaulting to strict would break the standalone use;
# defaulting to lenient for BOTH is what let an empty promotion report success.
EXPECT_COMMIT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="$2"; shift ;;
        --expect-commit) EXPECT_COMMIT=1 ;;
        -*) echo "Unknown flag: $1" >&2; exit 2 ;;
        *) DEST="$1" ;;
    esac
    shift
done

if [ -z "$DEST" ] || [ ! -d "$DEST" ]; then
    echo "Usage: seed-verify.sh <destination> [--manifest <path>] [--expect-commit]" >&2
    exit 2
fi
DEST="$(cd "$DEST" && pwd)"

FAILS=0
WARNS=0

echo "[seed-verify] dest=$DEST"

# Each engine subcommand emits a JSON object. We use _seed_verify_format.py
# to parse and print PASS/FAIL/WARN per check rather than interpolating
# paths into inline Python strings (shell-injection risk caught by the
# fresh-eyes review).

run_check() {
    local label="$1"; shift
    local exit_kind="$1"; shift  # "fail" | "warn"
    local check_id="$1"; shift
    echo "[$check_id] $label"
    local rc=0
    if ! py -3 "$SCRIPT_DIR/_seed_verify_format.py" "$check_id" "$@" 2>&1; then
        rc=$?
    fi
    if [ $rc -ne 0 ]; then
        if [ "$exit_kind" = "fail" ]; then
            FAILS=$((FAILS+1))
        else
            WARNS=$((WARNS+1))
        fi
    fi
}

# --- Check 1: Manifest completeness ---
run_check "Manifest completeness" "fail" "1" \
    --engine "$SCRIPT_DIR/_seed_engine.py" \
    --cmd verify-completeness \
    --manifest "$MANIFEST" \
    --source "$PROJECT_ROOT" \
    --dest "$DEST"

# --- Check 2: Domain leakage ---
run_check "Domain leakage (excluded paths not copied)" "fail" "2" \
    --engine "$SCRIPT_DIR/_seed_engine.py" \
    --cmd verify-leak-check \
    --manifest "$MANIFEST" \
    --dest "$DEST"

# --- Check 3: Cruft absence ---
run_check "Cruft absence" "warn" "3" \
    --engine "$SCRIPT_DIR/_seed_engine.py" \
    --cmd verify-cruft \
    --manifest "$MANIFEST" \
    --dest "$DEST"

# --- Check 4: Git state report ---
echo "[4] Git state at destination"
# -e, not -d: a linked git WORKTREE has a .git FILE (gitdir pointer), and the
# promote --pr path plants into exactly such a worktree. With -d this whole
# check skipped there, which blinded --expect-commit and let a 731-file
# uncommitted plant sail to "PROMOTED" (2026-08-19, v2.9.3 run 2 — the same
# dead-gate collapse  fixed, resurrected by the path shape).
if [ -e "$DEST/.git" ]; then
    BRANCH="$(git -C "$DEST" branch --show-current 2>/dev/null || echo '(no branch)')"
    # FAIL CLOSED (). `2>/dev/null` discarded stderr and the exit
    # code, so a git failure produced an empty STATUS that the report below
    # renders as "clean" — a verification tool asserting cleanliness it never
    # established. stderr now lands in the capture and rc is checked, so a
    # failure is non-empty and reports as drift. --no-optional-locks keeps this
    # probe off .git/index.lock (verified on git 2.43.0, not assumed).
    STATUS_RC=0
    STATUS="$(git --no-optional-locks -C "$DEST" status --porcelain 2>&1)" || STATUS_RC=$?
    if [ $STATUS_RC -ne 0 ]; then
        STATUS="git status failed (rc=$STATUS_RC) — cannot verify clean: ${STATUS:-<no stderr>}"
    fi
    LAST="$(git -C "$DEST" log --oneline -1 2>/dev/null || echo '(no commits)')"
    REMOTE="$(git -C "$DEST" remote -v 2>/dev/null | head -1 || echo '(no remote)')"
    echo "   branch=$BRANCH"
    echo "   last_commit=$LAST"
    echo "   remote=$REMOTE"
    if [ -z "$STATUS" ]; then
        echo "   clean: PASS"
    elif [ "$EXPECT_COMMIT" -eq 1 ]; then
        # POST-PLANT a dirty tree is BY DEFINITION a failed plant: the planted
        # files were supposed to be committed. Calling it "expected after plant"
        # (which this branch did unconditionally until ) meant the
        # verifier PRINTED the evidence of a failed plant and passed anyway —
        # so promote-to-upstream.sh's `|| fail "post-promotion verify FAILED"`
        # could never fire and an empty promotion opened a PR and banner-ed
        # PROMOTED. Same class as the --plan verdict collapse fixed by .
        N_DIRTY="$(echo "$STATUS" | wc -l)"
        echo "   FAIL: $N_DIRTY uncommitted change(s) after a plant that was supposed to commit"
        echo "         The plant did not land. 'last_commit' above is the destination's"
        echo "         PRE-EXISTING head, not this plant — do not read it as evidence of success."
        echo "$STATUS" | head -10 | sed 's/^/         /'
        FAILS=$((FAILS+1))
    else
        N_DIRTY="$(echo "$STATUS" | wc -l)"
        echo "   $N_DIRTY changed files (informational — pass --expect-commit to treat this as a failed plant)"
    fi
else
    echo "   INFO: no .git/ at destination"
fi

# --- Check 5: Sample integrity (SHA-256) ---
run_check "Sample integrity (post-transform source matches destination)" "warn" "5" \
    --engine "$SCRIPT_DIR/_seed_engine.py" \
    --cmd verify-integrity \
    --manifest "$MANIFEST" \
    --source "$PROJECT_ROOT" \
    --dest "$DEST"

# --- Check 6: Framework bootability ---
echo "[6] Framework bootability (prerequisites at destination)"
if [ -f "$DEST/core/scripts/check-prerequisites.sh" ]; then
    if bash "$DEST/core/scripts/check-prerequisites.sh" >/dev/null 2>&1; then
        echo "   PASS"
    else
        echo "   WARN: check-prerequisites.sh reported issues — see destination output"
        WARNS=$((WARNS+1))
    fi
else
    echo "   FAIL: check-prerequisites.sh missing at destination"
    FAILS=$((FAILS+1))
fi

# --- Check 7: Path self-reference scan ---
echo "[7] Path self-reference scan"
set +e
bash "$SCRIPT_DIR/seed-path-self-reference-scan.sh" "$DEST"
SCAN_RC=$?
set -e
if [ $SCAN_RC -eq 0 ]; then
    echo "   PASS"
elif [ $SCAN_RC -eq 1 ]; then
    echo "   WARN: self-references found (see scan output above)"
    WARNS=$((WARNS+1))
else
    echo "   WARN: scan script error (rc=$SCAN_RC)"
    WARNS=$((WARNS+1))
fi

# --- Check 8: Executable bits by GIT INDEX MODE (, guard-5806) ---
# The plant's own counters report what the copy DID; this reads what git
# RECORDED -- source index vs destination index, for every path both carry. A
# Windows-driven hop (core.fileMode=false) chmods into the void and lands NEW
# scripts at 100644 while every other check here passes: v2.12.47, 15 files.
run_check "Executable bits (source index vs destination index)" "fail" "8" \
    --engine "$SCRIPT_DIR/_seed_engine.py" \
    --cmd verify-exec-bits \
    --manifest "$MANIFEST" \
    --source "$PROJECT_ROOT" \
    --dest "$DEST"

# --- Check 9: Summary ---
echo ""
echo "[seed-verify] SUMMARY"
echo "  FAILS: $FAILS"
echo "  WARNS: $WARNS"
if [ "$FAILS" -gt 0 ]; then
    echo "  Status: FAIL"
    exit 1
elif [ "$WARNS" -gt 0 ]; then
    echo "  Status: WARN"
    exit 0
else
    echo "  Status: PASS"
    exit 0
fi
