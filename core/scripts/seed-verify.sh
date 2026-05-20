#!/usr/bin/env bash
# /seed verify <destination> — post-transplant smoke test (8 checks).
#
# Usage: seed-verify.sh <destination> [--manifest <path>]
#
# Outputs structured PASS/FAIL/WARN per check. Exits non-zero if any FAIL.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

DEST=""
MANIFEST="$CONFIG_DIR/seed-manifest.yaml"

while [ $# -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="$2"; shift ;;
        -*) echo "Unknown flag: $1" >&2; exit 2 ;;
        *) DEST="$1" ;;
    esac
    shift
done

if [ -z "$DEST" ] || [ ! -d "$DEST" ]; then
    echo "Usage: seed-verify.sh <destination> [--manifest <path>]" >&2
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
run_check "Domain leakage (excluded paths not transplanted)" "fail" "2" \
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
if [ -d "$DEST/.git" ]; then
    BRANCH="$(git -C "$DEST" branch --show-current 2>/dev/null || echo '(no branch)')"
    STATUS="$(git -C "$DEST" status --porcelain 2>/dev/null)"
    LAST="$(git -C "$DEST" log --oneline -1 2>/dev/null || echo '(no commits)')"
    REMOTE="$(git -C "$DEST" remote -v 2>/dev/null | head -1 || echo '(no remote)')"
    echo "   branch=$BRANCH"
    echo "   last_commit=$LAST"
    echo "   remote=$REMOTE"
    if [ -z "$STATUS" ]; then
        echo "   clean: PASS"
    else
        N_DIRTY="$(echo "$STATUS" | wc -l)"
        echo "   $N_DIRTY changed files (expected after transplant)"
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

# --- Check 8: Summary ---
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
