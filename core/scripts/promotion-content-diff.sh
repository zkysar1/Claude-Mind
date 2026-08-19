#!/usr/bin/env bash
# promotion-content-diff.sh — Exhaustive deterministic framework content diff.
#
# Enumerates ALL framework files in two repos, sha256-hashes each, and classifies
# every file as IDENTICAL / SOURCE_ONLY / TARGET_ONLY / DIFFERING.  For DIFFERING
# files, determines direction (target_ahead / source_ahead / ambiguous) via
# git-log commit timestamps.
#
# Replaces LLM-sampled promotion audits (which missed 459 of ~700 differing files)
# with a deterministic full scan.  Pure git + sha256 — no LLM, no daemon.
#
# Exit codes:
#   0  CLEAN   — no target-ahead core files (safe to promote)
#   2  DRIFT   — target-ahead core files exist (back-port before promoting)
#   1  ERROR   — bad invocation or missing arguments
#
# Usage:
#   bash core/scripts/promotion-content-diff.sh --source <repo> --target <repo>
#   bash core/scripts/promotion-content-diff.sh --source <repo> --target <repo> --json
#   bash core/scripts/promotion-content-diff.sh --source <repo> --target <repo> --strict
#
# Options:
#   --source <path>   Source (upstream) repo root
#   --target <path>   Target (downstream) repo root
#   --json            Emit machine-readable JSON to stdout (human report to stderr)
#   --strict          Exit non-zero on ANY differing file, not just target-ahead core
#
set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────

SOURCE=""
TARGET=""
JSON_OUTPUT=false
STRICT=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)  SOURCE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --target)  TARGET="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --json)    JSON_OUTPUT=true; shift ;;
    --strict)  STRICT=true; shift ;;
    -h|--help)
      sed -n '2,/^$/{ s/^# //; s/^#$//; p; }' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$SOURCE" || -z "$TARGET" ]]; then
  echo "ERROR: --source and --target are required" >&2
  exit 1
fi

# Resolve to absolute paths
SOURCE="$(cd "$SOURCE" && pwd)"
TARGET="$(cd "$TARGET" && pwd)"

if [[ "$SOURCE" == "$TARGET" ]]; then
  echo "ERROR: --source and --target are the same directory" >&2
  exit 1
fi

if [[ ! -d "$SOURCE" ]]; then
  echo "ERROR: --source does not exist: $SOURCE" >&2
  exit 1
fi

if [[ ! -d "$TARGET" ]]; then
  echo "ERROR: --target does not exist: $TARGET" >&2
  exit 1
fi

# ── Temp directory for intermediate files ─────────────────────────────────────

TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_WORK"' EXIT

# ── Constants ─────────────────────────────────────────────────────────────────

# Framework path roots to scan (relative to repo root).
FRAMEWORK_ROOTS=(
  "core/config"
  "core/scripts"
  ".claude/skills"
  ".claude/rules"
  "mind_api/src"
  "CLAUDE.md"
)

# Directories to exclude entirely.
EXCLUDE_DIRS_RE="(^|/)(__|\.git|\.python-shim|node_modules|\.pytest_cache|\.history|\.mypy_cache|\.ruff_cache)(/|$)"

# Substring segments to exclude.
EXCLUDE_SUBSTR="_tmp_"

# Deployment-local files (reported but not counted as blocking drift).
is_deployment_local() {
  case "$1" in
    CLAUDE.md|.claude/settings.json|.claude/settings.local.json|.claude/rules/promotion-cycle.md)
      return 0 ;;
  esac
  return 1
}

is_skill() {
  [[ "$1" == .claude/skills/* ]]
}

# ── Helper functions ──────────────────────────────────────────────────────────

is_excluded_path() {
  local rel="$1"
  # Check excluded directory segments
  if echo "$rel" | grep -qE "$EXCLUDE_DIRS_RE"; then
    return 0
  fi
  # Check excluded substring
  if [[ "$rel" == *"$EXCLUDE_SUBSTR"* ]]; then
    return 0
  fi
  # Check excluded file patterns
  local base
  base="$(basename "$rel")"
  case "$base" in
    *.pyc|*.pyo|*.log|.DS_Store|*.swp) return 0 ;;
  esac
  return 1
}

# Walk a single framework root and emit relative paths (one per line, sorted).
walk_framework_files() {
  local repo_root="$1"
  for sub in "${FRAMEWORK_ROOTS[@]}"; do
    local full="$repo_root/$sub"
    if [[ -f "$full" ]]; then
      if ! is_excluded_path "$sub"; then
        echo "$sub"
      fi
    elif [[ -d "$full" ]]; then
      find "$full" -type f 2>/dev/null | sed 's|\\|/|g' | while IFS= read -r abs_path; do
        local rel
        rel="${abs_path#$repo_root/}"
        rel="${rel//\\//}"
        if ! is_excluded_path "$rel"; then
          echo "$rel"
        fi
      done
    fi
  done | sort -u
}

# SHA256 hash of a file.  Returns "NULL" if file does not exist.
file_hash() {
  if [[ -f "$1" ]]; then
    sha256sum "$1" 2>/dev/null | cut -d' ' -f1
  else
    echo "NULL"
  fi
}

# Git last-commit unix timestamp for a file in a repo.  Returns empty on failure.
git_commit_ts() {
  local repo="$1" rel="$2"
  git -C "$repo" log -1 --format=%ct -- "$rel" 2>/dev/null || true
}

# Count lines in a file (0 if empty or missing).
count_lines() {
  if [[ -s "$1" ]]; then
    wc -l < "$1" | tr -d ' '
  else
    echo 0
  fi
}

# ── Main logic ────────────────────────────────────────────────────────────────

# 1. Enumerate all framework files in both repos (to temp files)
SRC_LIST="$TMPDIR_WORK/src_files"
TGT_LIST="$TMPDIR_WORK/tgt_files"
walk_framework_files "$SOURCE" > "$SRC_LIST"
walk_framework_files "$TARGET" > "$TGT_LIST"

# Classification output files
F_IDENTICAL="$TMPDIR_WORK/identical"
F_SOURCE_ONLY="$TMPDIR_WORK/source_only"
F_TARGET_ONLY="$TMPDIR_WORK/target_only"
F_SOURCE_AHEAD="$TMPDIR_WORK/source_ahead"
F_TARGET_AHEAD="$TMPDIR_WORK/target_ahead"
F_AMBIGUOUS="$TMPDIR_WORK/ambiguous"
: > "$F_IDENTICAL"
: > "$F_SOURCE_ONLY"
: > "$F_TARGET_ONLY"
: > "$F_SOURCE_AHEAD"
: > "$F_TARGET_AHEAD"
: > "$F_AMBIGUOUS"

# Source-only files: in SRC_LIST but not in TGT_LIST
comm -23 "$SRC_LIST" "$TGT_LIST" > "$F_SOURCE_ONLY"

# Target-only files: in TGT_LIST but not in SRC_LIST
comm -13 "$SRC_LIST" "$TGT_LIST" > "$F_TARGET_ONLY"

# Common files: in both
COMMON="$TMPDIR_WORK/common"
comm -12 "$SRC_LIST" "$TGT_LIST" > "$COMMON"

# 2. For common files, hash and classify
while IFS= read -r rel; do
  h_src="$(file_hash "$SOURCE/$rel")"
  h_tgt="$(file_hash "$TARGET/$rel")"

  if [[ "$h_src" == "$h_tgt" ]]; then
    echo "$rel" >> "$F_IDENTICAL"
    continue
  fi

  # Differing — determine direction via git timestamps
  ts_src="$(git_commit_ts "$SOURCE" "$rel")"
  ts_tgt="$(git_commit_ts "$TARGET" "$rel")"
  # Trim whitespace
  ts_src="${ts_src## }" ; ts_src="${ts_src%% }"
  ts_tgt="${ts_tgt## }" ; ts_tgt="${ts_tgt%% }"

  if [[ -n "$ts_src" && -n "$ts_tgt" ]]; then
    if (( ts_tgt > ts_src )); then
      echo "$rel" >> "$F_TARGET_AHEAD"
    elif (( ts_src > ts_tgt )); then
      echo "$rel" >> "$F_SOURCE_AHEAD"
    else
      echo "$rel" >> "$F_AMBIGUOUS"
    fi
  elif [[ -n "$ts_tgt" && -z "$ts_src" ]]; then
    echo "$rel" >> "$F_TARGET_AHEAD"
  elif [[ -n "$ts_src" && -z "$ts_tgt" ]]; then
    echo "$rel" >> "$F_SOURCE_AHEAD"
  else
    echo "$rel" >> "$F_AMBIGUOUS"
  fi
done < "$COMMON"

# 3. Get repo HEADs
source_head="$(git -C "$SOURCE" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
target_head="$(git -C "$TARGET" rev-parse --short HEAD 2>/dev/null || echo "unknown")"

# 4. Count everything
n_identical="$(count_lines "$F_IDENTICAL")"
n_source_only="$(count_lines "$F_SOURCE_ONLY")"
n_target_only="$(count_lines "$F_TARGET_ONLY")"
n_source_ahead="$(count_lines "$F_SOURCE_AHEAD")"
n_target_ahead="$(count_lines "$F_TARGET_AHEAD")"
n_ambiguous="$(count_lines "$F_AMBIGUOUS")"
n_total=$(( n_identical + n_source_only + n_target_only + n_source_ahead + n_target_ahead + n_ambiguous ))

# 5. Compute blocking target-ahead core files (non-deployment-local, non-skill)
n_blocking_ta=0
n_blocking_to=0
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  if ! is_deployment_local "$f" && ! is_skill "$f"; then
    n_blocking_ta=$(( n_blocking_ta + 1 ))
  fi
done < "$F_TARGET_AHEAD"

while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  if ! is_deployment_local "$f" && ! is_skill "$f"; then
    n_blocking_to=$(( n_blocking_to + 1 ))
  fi
done < "$F_TARGET_ONLY"

# 6. Determine exit code
exit_code=0
if [[ $n_blocking_ta -gt 0 || $n_blocking_to -gt 0 ]]; then
  exit_code=2
fi
if [[ "$STRICT" == "true" ]]; then
  if [[ $n_ambiguous -gt 0 || $n_source_ahead -gt 0 ]]; then
    exit_code=2
  fi
fi

# ── Output ────────────────────────────────────────────────────────────────────

# Helper: convert a newline-delimited file to a JSON array string
file_to_json_array() {
  local file="$1"
  if [[ ! -s "$file" ]]; then
    echo "[]"
    return
  fi
  local out="["
  local first=true
  while IFS= read -r item; do
    [[ -z "$item" ]] && continue
    if [[ "$first" == "true" ]]; then
      first=false
    else
      out+=","
    fi
    item="${item//\\/\\\\}"
    item="${item//\"/\\\"}"
    out+="\"$item\""
  done < "$file"
  out+="]"
  echo "$out"
}

# Human-readable report (to stderr if --json, to stdout otherwise)
report_fd=1
if [[ "$JSON_OUTPUT" == "true" ]]; then
  report_fd=2
fi

{
  echo "================================================================"
  echo "  PROMOTION CONTENT DIFF"
  echo "================================================================"
  echo "Source: $SOURCE ($source_head)"
  echo "Target: $TARGET ($target_head)"
  echo ""
  echo "-- Counts ------------------------------------------------------"
  echo "  Identical:     $n_identical"
  echo "  Source-only:   $n_source_only"
  echo "  Target-only:   $n_target_only"
  echo "  Source-ahead:  $n_source_ahead"
  echo "  Target-ahead:  $n_target_ahead"
  echo "  Ambiguous:     $n_ambiguous"
  echo "  Total scanned: $n_total"
  echo ""

  if [[ $n_target_ahead -gt 0 ]]; then
    echo "-- TARGET-AHEAD (back-port before promoting) -----------------"
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      note=""
      if is_deployment_local "$f"; then note=" [deployment-local]"; fi
      if is_skill "$f"; then note=" [skill]"; fi
      echo "  $f$note"
    done < "$F_TARGET_AHEAD"
    echo ""
  fi

  if [[ $n_target_only -gt 0 ]]; then
    echo "-- TARGET-ONLY (orphan risk or prod-ahead) -------------------"
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      note=""
      if is_deployment_local "$f"; then note=" [deployment-local]"; fi
      if is_skill "$f"; then note=" [skill]"; fi
      echo "  $f$note"
    done < "$F_TARGET_ONLY"
    echo ""
  fi

  if [[ $n_ambiguous -gt 0 ]]; then
    echo "-- AMBIGUOUS (manual review) ---------------------------------"
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      echo "  $f"
    done < "$F_AMBIGUOUS"
    echo ""
  fi

  if [[ $n_source_ahead -gt 0 ]]; then
    echo "-- SOURCE-AHEAD (safe to overwrite) --------------------------"
    echo "  ($n_source_ahead files -- omitted for brevity)"
    echo ""
  fi

  if [[ $n_source_only -gt 0 ]]; then
    echo "-- SOURCE-ONLY (normal promotion payload) --------------------"
    echo "  ($n_source_only files -- omitted for brevity)"
    echo ""
  fi

  if [[ $exit_code -eq 0 ]]; then
    echo "RESULT: CLEAN -- no blocking target-ahead drift. Safe to promote."
  else
    echo "RESULT: DRIFT -- ${n_blocking_ta} target-ahead core + ${n_blocking_to} target-only core files. Back-port before promoting."
  fi
  echo "================================================================"
} >&$report_fd

# JSON output
if [[ "$JSON_OUTPUT" == "true" ]]; then
  cat <<ENDJSON
{
  "source_root": "$SOURCE",
  "target_root": "$TARGET",
  "source_head": "$source_head",
  "target_head": "$target_head",
  "counts": {
    "identical": $n_identical,
    "source_only": $n_source_only,
    "target_only": $n_target_only,
    "source_ahead": $n_source_ahead,
    "target_ahead": $n_target_ahead,
    "ambiguous": $n_ambiguous,
    "total": $n_total
  },
  "target_ahead": $(file_to_json_array "$F_TARGET_AHEAD"),
  "source_ahead": $(file_to_json_array "$F_SOURCE_AHEAD"),
  "target_only": $(file_to_json_array "$F_TARGET_ONLY"),
  "source_only": $(file_to_json_array "$F_SOURCE_ONLY"),
  "ambiguous": $(file_to_json_array "$F_AMBIGUOUS"),
  "exit_code": $exit_code
}
ENDJSON
fi

exit $exit_code
