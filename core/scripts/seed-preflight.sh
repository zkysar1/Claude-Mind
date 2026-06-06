#!/usr/bin/env bash
# seed-preflight.sh — Source publishability gate aggregator.
#
# Runs seven publishability checks against the source repo before allowing
# a seed transplant to proceed. Each check is a single-responsibility
# standalone script; the aggregator collects results and exits non-zero
# if ANY check FAILed.
#
# Single source of truth shared by:
#   - seed-transplant.sh Step 4.5 (block bad promotions before staging)
#   - /verify-learning (one section delegates to the aggregator)
#   - manual invocation: `bash core/scripts/seed-preflight.sh`
#
# Each check exits 0 PASS / 1 FAIL / 2 script error. The aggregator
# treats script error (2) as FAIL (conservative — don't promote on
# probe-broken state).
#
# Usage:
#   bash core/scripts/seed-preflight.sh             # full run, exit 0/1
#   bash core/scripts/seed-preflight.sh --json      # JSON summary on stdout
#   bash core/scripts/seed-preflight.sh --quiet     # suppress per-check output
#
# Exit: 0 = all PASS, 1 = at least one FAIL or script-error, 2 = aggregator error
#
# Origin: rb-1108 (orphan-mirror), rb-1109 (commit hygiene), rb-1110
# (seed-backup gitignore) + system/publication-pipeline tree node.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || {
    echo "ERROR: failed to source _paths.sh" >&2
    exit 2
}

JSON_OUTPUT=0
QUIET=0
for arg in "$@"; do
    case "$arg" in
        --json) JSON_OUTPUT=1 ;;
        --quiet) QUIET=1 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# Each check entry: ID|LABEL|COMMAND
# Order matters — cheapest / most-fundamental first so early FAILs surface
# the highest-leverage problem.
CHECKS=(
    "domain-leak|Domain token blocklist sweep|bash $SCRIPT_DIR/domain-leak-check.sh"
    "skill-frontmatter|SKILL.md front matter parses|bash $SCRIPT_DIR/skill-frontmatter-audit.sh"
    "pseudocode-scripts|SKILL.md pseudocode-script references exist|py -3 $SCRIPT_DIR/verify-pseudocode-scripts.py --scripts-only"
    "forged-skill-tagging|Forged-skill registry bidirectional consistency|bash $SCRIPT_DIR/audit-forged-skill-tagging.sh"
    "aspirations-templates|Agent-aspirations starter templates domain-free|bash $SCRIPT_DIR/audit-aspirations-templates-clean.sh"
    "toplevel-allowlist|core/ + .claude/ top-level allowlist|bash $SCRIPT_DIR/audit-toplevel-allowlist.sh"
    "releases-current|RELEASES.json newest entry matches __version__|bash $SCRIPT_DIR/check-releases-current.sh"
)

PASSES=()
FAILS=()
ERRORS=()
LAST_OUTPUTS=()

run_one() {
    local id="$1" label="$2" cmd="$3"
    local output=""
    local rc=0
    set +e
    output="$(eval "$cmd" 2>&1)"
    rc=$?
    set -e
    LAST_OUTPUTS+=("=== [$id] $label ===")
    LAST_OUTPUTS+=("$output")
    if [ $rc -eq 0 ]; then
        PASSES+=("$id")
        if [ $QUIET -eq 0 ] && [ $JSON_OUTPUT -eq 0 ]; then
            echo "[PASS] $id — $label"
        fi
    elif [ $rc -eq 1 ]; then
        FAILS+=("$id")
        if [ $JSON_OUTPUT -eq 0 ]; then
            echo "[FAIL] $id — $label"
            echo "$output" | sed 's/^/   /'
        fi
    else
        ERRORS+=("$id (rc=$rc)")
        if [ $JSON_OUTPUT -eq 0 ]; then
            echo "[ERROR] $id (rc=$rc) — $label"
            echo "$output" | sed 's/^/   /'
        fi
    fi
}

if [ $JSON_OUTPUT -eq 0 ] && [ $QUIET -eq 0 ]; then
    echo "[seed-preflight] Source publishability gate (${#CHECKS[@]} checks)"
fi

for entry in "${CHECKS[@]}"; do
    IFS='|' read -r id label cmd <<< "$entry"
    run_one "$id" "$label" "$cmd"
done

# Summary
TOTAL=${#CHECKS[@]}
N_PASS=${#PASSES[@]}
N_FAIL=${#FAILS[@]}
N_ERR=${#ERRORS[@]}

if [ $JSON_OUTPUT -eq 1 ]; then
    # Build JSON summary manually (avoid Python invocation overhead in tight wrappers)
    printf '{\n'
    printf '  "total": %d,\n' "$TOTAL"
    printf '  "passed": %d,\n' "$N_PASS"
    printf '  "failed": %d,\n' "$N_FAIL"
    printf '  "errors": %d,\n' "$N_ERR"
    printf '  "passes": ['
    for i in "${!PASSES[@]}"; do
        [ $i -gt 0 ] && printf ', '
        printf '"%s"' "${PASSES[$i]}"
    done
    printf '],\n  "fails": ['
    for i in "${!FAILS[@]}"; do
        [ $i -gt 0 ] && printf ', '
        printf '"%s"' "${FAILS[$i]}"
    done
    printf '],\n  "errors_detail": ['
    for i in "${!ERRORS[@]}"; do
        [ $i -gt 0 ] && printf ', '
        printf '"%s"' "${ERRORS[$i]}"
    done
    printf ']\n}\n'
else
    echo ""
    echo "[seed-preflight] SUMMARY: $N_PASS passed, $N_FAIL failed, $N_ERR errors (of $TOTAL)"
    if [ $N_FAIL -gt 0 ] || [ $N_ERR -gt 0 ]; then
        echo "[seed-preflight] STATUS: REFUSE PROMOTION"
        echo "[seed-preflight] Fix the failing checks above, OR pass --skip-preflight to seed-transplant"
        echo "[seed-preflight] (audit-trail to stderr). Override is a last resort, never routine."
    else
        echo "[seed-preflight] STATUS: PUBLISHABLE"
    fi
fi

if [ $N_FAIL -gt 0 ] || [ $N_ERR -gt 0 ]; then
    exit 1
fi
exit 0
