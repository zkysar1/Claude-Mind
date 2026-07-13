#!/usr/bin/env bash
# temp-drain-purge.sh — canonical GUARDED purge of pure-ephemera from the bound
# agent's temp/ dir. Exists so autonomous agents NEVER hand-roll an unguarded
# `rm` on a possibly-empty variable path — which triggers a Claude Code
# dangerous-rm permission dialog that HANGS the agent (even under
# --dangerously-skip-permissions, the fleet launch mode). Observed 2026-07-09:
# an agent hung 46+ min blocked on such a dialog ("Dangerous rm operation on
# possibly-empty variable path (TEMP_DIR/f), proceed?") during a temp-drain
# purge (6, filed by the fleet operator). Eliminating the
# hand-rolled-rm class means giving every agent ONE guarded purge path.
#
# GUARDS (assert_safe_temp_dir) — ALL must pass before ANY deletion; any failure
# returns non-zero and deletes NOTHING (fail-loud is always safer than a
# dangerous rm):
#   1. agent_dir set + non-empty (the bound agent, via _paths.sh)
#   2. project_root set + non-empty (via _paths.sh)
#   3. temp_dir set + non-empty
#   4. temp_dir is an ABSOLUTE path
#   5. temp_dir is strictly UNDER "$project_root/" (never /, /temp, or a sibling)
#   6. basename(temp_dir) == "temp"
# Deletion uses `find "$TEMP_DIR" -maxdepth 1 -type f (ephemera globs) -mmin +AGE
# -delete` — never a per-file `rm` on an interpolated path. -maxdepth 1 leaves
# drained/ (a subdir) untouched.
#
# Usage: temp-drain-purge.sh [--dry-run] [--age-min N]
#   --dry-run  list what WOULD purge, delete nothing
#   --age-min  age guard in minutes (default 120; skips actively-written logs)
# Output (stdout, JSON): {"purged":N,"would_purge":N,"files":[...],"dry_run":bool,
#                         "age_min":N,"temp_dir":"..."}
# Exit: 0 on success (incl. no-temp-dir no-op); 1 on a guard refusal; 2 on bad args.
#
# assert_safe_temp_dir() is sourceable + unit-tested (test_temp_drain_purge.sh):
# `source temp-drain-purge.sh` does NOT run main() (guarded at the bottom), so a
# test can call the guard with hostile inputs (empty, /temp, outside-root, ...).
set -uo pipefail

# assert_safe_temp_dir <candidate_temp_dir> <project_root> <agent_dir>
# Pure validation — echoes a REFUSED reason to stderr + returns 1 on any guard
# failure, returns 0 when the candidate is safe to purge. NEVER deletes.
assert_safe_temp_dir() {
  local temp_dir="${1:-}" project_root="${2:-}" agent_dir="${3:-}"
  if [ -z "$agent_dir" ]; then
    echo "temp-drain-purge.sh: REFUSED — AGENT_DIR empty/unset (agent binding failed). Purged nothing." >&2; return 1
  fi
  if [ -z "$project_root" ]; then
    echo "temp-drain-purge.sh: REFUSED — PROJECT_ROOT empty/unset. Purged nothing." >&2; return 1
  fi
  if [ -z "$temp_dir" ]; then
    echo "temp-drain-purge.sh: REFUSED — TEMP_DIR empty. Purged nothing." >&2; return 1
  fi
  case "$temp_dir" in
    /*) : ;;
    *) echo "temp-drain-purge.sh: REFUSED — TEMP_DIR '$temp_dir' is not absolute. Purged nothing." >&2; return 1 ;;
  esac
  case "$temp_dir" in
    "$project_root"/*) : ;;
    *) echo "temp-drain-purge.sh: REFUSED — TEMP_DIR '$temp_dir' is not under PROJECT_ROOT '$project_root'. Purged nothing." >&2; return 1 ;;
  esac
  if [ "$(basename "$temp_dir")" != "temp" ]; then
    echo "temp-drain-purge.sh: REFUSED — TEMP_DIR basename is not 'temp' ('$temp_dir'). Purged nothing." >&2; return 1
  fi
  return 0
}

main() {
  local DRY_RUN=0 AGE_MIN=120
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --age-min) AGE_MIN="${2:?temp-drain-purge.sh: --age-min needs a value}"; shift 2 ;;
      -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; return 0 ;;
      *) echo "temp-drain-purge.sh: unknown arg '$1'" >&2; return 2 ;;
    esac
  done
  case "$AGE_MIN" in
    ''|*[!0-9]*) echo "temp-drain-purge.sh: --age-min must be a non-negative integer, got '$AGE_MIN'" >&2; return 2 ;;
  esac

  # Resolve paths via the canonical helper (never a caller-supplied var).
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # shellcheck disable=SC1091
  source "$script_dir/_paths.sh"

  local temp_dir="${AGENT_DIR:-}/temp"
  # An empty AGENT_DIR yields temp_dir="/temp"; the under-PROJECT_ROOT + empty
  # agent_dir guards both catch that before any deletion.
  [ -z "${AGENT_DIR:-}" ] && temp_dir=""

  if ! assert_safe_temp_dir "$temp_dir" "${PROJECT_ROOT:-}" "${AGENT_DIR:-}"; then
    return 1
  fi

  # Soft guard: no temp dir = nothing to purge — clean no-op (fresh agent).
  if [ ! -d "$temp_dir" ]; then
    printf '{"purged":0,"would_purge":0,"files":[],"dry_run":%s,"age_min":%d,"temp_dir":"%s","note":"temp dir does not exist"}\n' \
      "$([ "$DRY_RUN" -eq 1 ] && echo true || echo false)" "$AGE_MIN" "$temp_dir"
    return 0
  fi

  # All guards passed. List ephemera (for the caller's report), then delete
  # (unless --dry-run). find -maxdepth 1 -type f leaves drained/ untouched.
  local find_expr=( -maxdepth 1 -type f \( -name '*.log' -o -name '*.txt' -o -name '*.py' -o -name '*.sh' -o -name '*.err' \) -mmin "+$AGE_MIN" )
  local ephemera_list count
  ephemera_list="$(find "$temp_dir" "${find_expr[@]}" 2>/dev/null || true)"
  if [ -z "$ephemera_list" ]; then count=0; else count="$(printf '%s\n' "$ephemera_list" | grep -c . || true)"; fi

  if [ "$count" -gt 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    find "$temp_dir" "${find_expr[@]}" -delete 2>/dev/null || true
  fi

  # Build the files JSON array (basenames) in pure bash — temp ephemera names
  # are kebab-case with no JSON-hostile characters.
  local files_json='[' _first=1 _f _b
  if [ "$count" -gt 0 ]; then
    while IFS= read -r _f; do
      [ -z "$_f" ] && continue
      _b="$(basename "$_f")"
      [ "$_first" -eq 0 ] && files_json="$files_json,"
      files_json="$files_json\"$_b\""
      _first=0
    done <<EOF
$ephemera_list
EOF
  fi
  files_json="$files_json]"

  local purged
  if [ "$DRY_RUN" -eq 1 ]; then purged=0; else purged="$count"; fi
  printf '{"purged":%d,"would_purge":%d,"files":%s,"dry_run":%s,"age_min":%d,"temp_dir":"%s"}\n' \
    "$purged" "$count" "$files_json" "$([ "$DRY_RUN" -eq 1 ] && echo true || echo false)" "$AGE_MIN" "$temp_dir"
  return 0
}

# Run main() ONLY when executed directly, not when sourced (so the guard is
# unit-testable via `source`).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
  exit $?
fi
