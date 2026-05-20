#!/usr/bin/env bash
# domain-leak-check.sh — Scan core framework files for domain-specific terms.
# Usage: bash core/scripts/domain-leak-check.sh [--verbose] [--core-only]
#
# Reads terms from core/config/domain-term-blocklist.txt (case-sensitive).
# Excludes forged skills (from world/forged-skills.yaml). Scans all if unavailable.
# Exit code: 0 = clean, 1 = leaks found.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

BLOCKLIST="$PROJECT_ROOT/core/config/domain-term-blocklist.txt"
VERBOSE=false
CORE_ONLY=false

for arg in "$@"; do
  case "$arg" in
    --verbose) VERBOSE=true ;;
    --core-only) CORE_ONLY=true ;;
  esac
done

if [[ ! -f "$BLOCKLIST" ]]; then
  echo "ERROR: Blocklist not found: $BLOCKLIST" >&2
  exit 2
fi

# --- Build forged-skill exclusion list from world/forged-skills.yaml ---
# Single source of truth. If unavailable, scans everything (fail open).
EXCLUDE_ARGS=()

if [[ -f "$SCRIPT_DIR/_paths.sh" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true
  FORGED="${WORLD_DIR:-}/forged-skills.yaml"
  if [[ -f "$FORGED" ]]; then
    # Skills are YAML keys at 2-space indent under 'skills:' (e.g., "  access-efs-data:")
    while IFS= read -r skill_name; do
      [[ -n "$skill_name" ]] && EXCLUDE_ARGS+=("--exclude-dir=$skill_name")
    done < <(sed -n '/^skills:/,/^[^ ]/{ /^  [a-z]/{ s/: *$//; s/^ *//; p; } }' "$FORGED")
  fi
fi

# --- Scan directories ---
SCAN_DIRS=(
  "$PROJECT_ROOT/core/config"
  "$PROJECT_ROOT/core/scripts"
  "$PROJECT_ROOT/.claude/rules"
)

if [[ "$CORE_ONLY" == false ]]; then
  SCAN_DIRS+=("$PROJECT_ROOT/.claude/skills")
  # mind_api/ is part of the publishable framework (Phase 3.2 of
  # publication-cleanup-plan.md). Scan src/ and tests/ — skip docs/
  # (development-history is excluded from seed anyway) and state/ (runtime).
  [[ -d "$PROJECT_ROOT/mind_api/src" ]] && SCAN_DIRS+=("$PROJECT_ROOT/mind_api/src")
  [[ -d "$PROJECT_ROOT/mind_api/tests" ]] && SCAN_DIRS+=("$PROJECT_ROOT/mind_api/tests")
fi

FOUND=0

while IFS= read -r term; do
  # Skip comments and blank lines
  [[ -z "$term" || "$term" == \#* ]] && continue

  for dir in "${SCAN_DIRS[@]}"; do
    [[ -d "$dir" ]] || continue

    # Case-sensitive, word-boundary search (blocklist specifies exact case)
    hits=$(grep -rnw "${EXCLUDE_ARGS[@]}" --include="*.md" --include="*.yaml" --include="*.yml" --include="*.sh" --include="*.py" --include="*.txt" "$term" "$dir" 2>/dev/null || true)

    if [[ -n "$hits" ]]; then
      # Filter out self-referential hits (blocklist, this script, the rule file)
      hits=$(echo "$hits" | grep -v "domain-term-blocklist.txt" | grep -v "domain-leak-check.sh" | grep -v "domain-free-examples.md" || true)
      # Test fixtures deliberately use domain tokens to exercise pattern-matching
      # (test-capability-gate.sh uses "cannot access EFS" to lock in rb-389).
      # Skip any file whose basename starts with `test-` or `test_` — covers both
      # the test-*.sh shell convention and the Python test_*.py convention.
      hits=$(echo "$hits" | grep -vE '/test[-_][A-Za-z0-9_-]+\.(sh|py)(:|$)' || true)
      # verify-learning/SKILL.md is the meta-documentation of framework invariants
      # — it describes the blocklist/gate rules by example (same category as
      # domain-free-examples.md above).
      hits=$(echo "$hits" | grep -v "verify-learning/SKILL.md" || true)
      # Per-file opt-in: any file containing the marker `domain-leak-exempt:`
      # on any line is skipped entirely. Used by rule files that teach by
      # example (probe-before-defer.md citing EFS/Roblox) and algorithm-
      # comment docs where literal test strings are part of the reasoning
      # (capability-gate.py explaining rb-389 means-vs-ends). Adding the
      # marker is a review gesture — future editors see the rationale inline.
      if [[ -n "$hits" ]]; then
        hits=$(echo "$hits" | while IFS= read -r line; do
          f="${line%%:*}"
          [[ -f "$f" ]] || { echo "$line"; continue; }
          grep -q "domain-leak-exempt:" "$f" || echo "$line"
        done)
      fi
    fi

    if [[ -n "$hits" ]]; then
      FOUND=1
      # Hoisted out of the if/else so $count is set under `set -u` regardless
      # of mode — telemetry block below references it unconditionally.
      count=$(echo "$hits" | wc -l)
      if $VERBOSE; then
        echo "=== Term: '$term' ==="
        echo "$hits"
        echo
      else
        # Use repo-relative path so multi-segment scan dirs (mind_api/src,
        # mind_api/tests) are unambiguous in output. basename "mind_api/src"
        # would be "src/" — collides with any other src/ in the tree.
        rel_dir="${dir#"$PROJECT_ROOT/"}"
        echo "LEAK: '$term' — $count hit(s) in $rel_dir/"
      fi
      # Telemetry: per-violation block firing (g-248-80). One log per
      # (term, dir) pair so retirement-evaluator sees exactly which
      # blocklist entries are actually catching leaks vs which are dead
      # weight.
      bash "$SCRIPT_DIR/gate-log.sh" domain-leak-check block \
        --caller "domain-leak-check.sh:scan-loop" \
        --trigger "$term" \
        --extra-json "{\"decision_path\":\"leak-found\",\"dir\":\"${dir#"$PROJECT_ROOT/"}\",\"count\":$count}" \
        2>/dev/null || true
    fi
  done
done < "$BLOCKLIST"

# --- Phase 5.6: Marker-misplacement scan ---
# The `domain-leak-exempt:` marker is reserved for executable code (scripts,
# tests) where domain strings are FUNCTIONAL (regex patterns, sentinels,
# fixtures) and for rule files that teach by example. It is NOT a license
# to keep domain examples in `.claude/skills/*/SKILL.md` or
# `core/config/conventions/*.md` — those should genericize examples
# (per `.claude/rules/domain-free-examples.md` § Marker Restriction).
#
# This scan is the COMPLEMENT of the marker-honor logic above:
#   - marker-honor (lines 91-97): "if a scanned file carries the marker,
#     SKIP it" — used to suppress noise from teaching-by-example files.
#   - marker-misplacement (this block): "if an over-applied location
#     carries the marker, REPORT it" — surfaces pre-existing or
#     override-introduced markers that violate the placement doctrine.
#
# Layered with the `marker-placement-gate.py` PreToolUse gate (Phase 5.7),
# which prevents NEW introductions. This scanner is the audit half.
#
# Allowlist of known-legitimate carriers in scope (mirrors ALLOWLIST in
# core/scripts/marker-placement-gate.py — keep in sync).
# Entries belong here ONLY if the file's primary purpose is to DOCUMENT the
# marker itself (literal token must appear in prose / assertions) OR the
# file contains scanner sentinel patterns that would misfire if transformed
# (seed/SKILL.md).
MISPLACEMENT_FOUND=0
ALLOWLIST=(
  ".claude/skills/seed/SKILL.md"
  ".claude/skills/verify-learning/SKILL.md"
  "core/config/conventions/learning-routing.md"
)

scan_misplaced_markers() {
  local scan_root="$1" pattern_glob="$2"
  local hits f rel allowlisted entry
  # set -euo pipefail is in effect at the script level. Inside this command
  # substitution, grep returns 1 on no-match — `&&` short-circuits without
  # tripping set -e, but the inner while's last-iteration status leaks out
  # as the pipeline's exit status, and the captured assignment then fails
  # under set -e. Temporarily disable set -e for the capture so a no-match
  # scan returns empty rather than killing the script.
  set +e
  hits=$(find "$scan_root" -type f -name "$pattern_glob" 2>/dev/null | while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    if grep -q "domain-leak-exempt:" "$f" 2>/dev/null; then
      echo "$f"
    fi
  done)
  set -e
  [[ -z "$hits" ]] && return 0
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    rel="${f#$PROJECT_ROOT/}"
    rel="${rel//\\//}"
    allowlisted=0
    for entry in "${ALLOWLIST[@]}"; do
      [[ "$rel" == "$entry" ]] && { allowlisted=1; break; }
    done
    [[ $allowlisted -eq 1 ]] && continue
    MISPLACEMENT_FOUND=1
    echo "MARKER MISPLACED: $rel"
    bash "$SCRIPT_DIR/gate-log.sh" domain-leak-check block \
      --caller "domain-leak-check.sh:marker-misplacement-scan" \
      --trigger "marker-misplaced" \
      --extra-json "{\"decision_path\":\"marker-misplaced\",\"path\":\"$rel\"}" \
      2>/dev/null || true
  done <<< "$hits"
}

if [[ "$CORE_ONLY" == false && -d "$PROJECT_ROOT/.claude/skills" ]]; then
  scan_misplaced_markers "$PROJECT_ROOT/.claude/skills" "SKILL.md"
fi
if [[ -d "$PROJECT_ROOT/core/config/conventions" ]]; then
  scan_misplaced_markers "$PROJECT_ROOT/core/config/conventions" "*.md"
fi

if [[ $MISPLACEMENT_FOUND -eq 1 ]]; then
  echo
  echo "Misplaced markers in SKILL.md / conventions/ should be removed."
  echo "Genericize examples instead (use 'agent-a', 'service-x', 'the framework')."
  echo "See .claude/rules/domain-free-examples.md § 'Marker Restriction'."
  echo "Allowlist additions live in core/scripts/marker-placement-gate.py ALLOWLIST."
  FOUND=1
fi

if [[ $FOUND -eq 0 ]]; then
  echo "CLEAN: No domain terms found in framework files."
  # Telemetry: clean-pass noop firing (g-248-80). Counts toward invocation
  # totals so retirement-evaluator can compute fire-rate against total
  # runs.
  bash "$SCRIPT_DIR/gate-log.sh" domain-leak-check noop \
    --caller "domain-leak-check.sh:exit-clean" \
    --trigger "no-leaks" \
    --extra-json '{"decision_path":"all-clean"}' \
    2>/dev/null || true
  exit 0
else
  echo
  echo "Run with --verbose for full details."
  exit 1
fi
