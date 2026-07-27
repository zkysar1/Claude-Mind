#!/usr/bin/env bash
# temp-drain-purge.sh — canonical GUARDED purge of pure-ephemera from the bound
# agent's temp/ dir. Exists so autonomous agents NEVER hand-roll an unguarded
# `rm` on a possibly-empty variable path — which triggers a Claude Code
# dangerous-rm permission dialog that HANGS the agent (even under
# --dangerously-skip-permissions, the fleet launch mode). Observed 2026-07-09:
# an agent hung 46+ min blocked on such a dialog ("Dangerous rm operation on
# possibly-empty variable path (TEMP_DIR/f), proceed?") during a temp-drain
# purge (, filed by the fleet operator). Eliminating the
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
# THREE guarded deletion lanes — ALL bounded by the assert_safe_temp_dir guard
# above; NONE ever uses a per-file `rm` on an interpolated path:
#   Lane 1 (ephemera+empties): `find "$TEMP_DIR" -maxdepth 1 -type f (ephemera
#                        globs + 0-byte empties, EXCLUDING dotfiles) -mmin +AGE
#                        -delete`. SSOT glob = _purge_find_predicate (see its
#                        header for the two sub-lanes + the .gitkeep dotfile
#                        exclusion, ). -maxdepth 1 leaves drained/
#                        (a subdir) untouched.
#   Lane 2 (drained GC): `find "$TEMP_DIR/drained" -maxdepth 1 -type f
#                        -mtime +DRAINED_AGE_DAYS -delete` — prunes stale archived
#                        files (temp-store.md: drained/ contents >30d carry zero
#                        retrieval value). drained/ itself is preserved ().
#   Lane 3 (stray dirs): `find "$TEMP_DIR" -mindepth 1 -maxdepth 1 -type d
#                        ! -name drained -mmin +AGE_MIN` → each match guarded-
#                        deleted via `find "$stray" -delete` (re-asserted strictly
#                        under TEMP_DIR/). Removes abandoned scratch subdirs the
#                        file lanes never touch (e.g. a leftover session subdir,
#                        ).
#
# Usage: temp-drain-purge.sh [--dry-run] [--age-min N] [--drained-age-days N]
#   --dry-run           list what WOULD purge/clean, delete nothing
#   --age-min           file + stray-dir age guard in minutes (default 120; skips
#                       actively-written logs and still-active scratch dirs)
#   --drained-age-days  drained/ GC age guard in days (default 30)
# Output (stdout, JSON): {"purged":N,"would_purge":N,"files":[...],
#   "drained_gc_purged":N,"drained_gc_would_purge":N,"stray_purged":N,
#   "stray_would_purge":N,"dry_run":bool,"age_min":N,"drained_age_days":N,
#   "temp_dir":"..."} — the no-temp-dir no-op path emits the SAME field set
#   (all-zero lane fields) so both exit paths share one schema (fresh-eyes
#   finding bravo-fec-noop-json-missing-lane-fields).
# Exit: 0 on success (incl. no-temp-dir no-op); 1 on a guard refusal; 2 on bad args.
#
# assert_safe_temp_dir() + the lane functions (gc_drained_archive,
# cleanup_stray_dirs) + _purge_find_predicate are sourceable + unit-tested
# (test_temp_drain_purge.sh): `source temp-drain-purge.sh` does NOT run main()
# (guarded at the bottom), so a test can call each with hostile/synthetic inputs.
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

# _purge_find_predicate <age_min> — populate the global PURGE_FIND_PRED array
# with the find predicate for the Lane-1 purge. SINGLE SOURCE OF TRUTH for the
# purge glob: main() uses it for BOTH the list pass and the -delete pass, and
# test_temp_drain_purge.sh sources it to assert lane behavior against a
# synthetic temp dir (so the test can never diverge from the real glob).
# Two sub-lanes, both age-guarded by -mmin +age_min:
#   (1) ephemera EXTENSIONS — .log/.txt/.py/.sh/.err (test-suite output, tool
#       dumps, one-shot scratch scripts) + .raw/.out/.bak (raw command-output
#       dumps / stdout redirects / backup copies) — carry no knowledge.
#   (2) 0-BYTE EMPTIES of ANY name (-empty) — no content to drain; catches an
#       empty .json/.md left by an interrupted redirect.
# EXCLUDES DOTFILES (! -name '.*'): temp/'s only git-TRACKED file is a 0-byte
# `.gitkeep` (preserves the dir on a fresh clone — temp-store.md); the -empty
# lane would otherwise delete it (and any 0-byte dotfile marker) once past the
# age guard, and iteration-commit would commit that deletion (
# fresh-eyes catch). Real ephemera are never dotfiles, so the exclusion loses
# nothing.
# -maxdepth 1 -type f leaves drained/ (a subdir) untouched. -empty works on bfs
# (this box's find) and GNU findutils alike.
# SYNC: any change to this glob MUST update the ephemera table in
# core/config/conventions/temp-store.md (that file mandates the joint update).
_purge_find_predicate() {
  local age_min="$1"
  PURGE_FIND_PRED=( -maxdepth 1 -type f ! -name '.*' \( \( -name '*.log' -o -name '*.txt' -o -name '*.py' -o -name '*.sh' -o -name '*.err' -o -name '*.raw' -o -name '*.out' -o -name '*.bak' \) -o -empty \) -mmin "+$age_min" )
}

# gc_drained_archive <drained_dir> <age_days> <dry_run> — Lane 2. Prune files
# DIRECTLY under drained/ older than <age_days>. Echoes the match count (the
# would-purge count when dry_run=1, else the purged count). find -maxdepth 1
# -type f keeps -delete bounded to files (never the drained/ dir itself) — never
# a hand-rolled rm. Caller MUST have asserted drained_dir's parent temp_dir safe.
# Sourceable + unit-tested (test_temp_drain_purge.sh) with a synthetic drained/.
gc_drained_archive() {
  local drained_dir="${1:-}" age_days="${2:-30}" dry_run="${3:-0}" list count=0
  [ -d "$drained_dir" ] || { echo 0; return 0; }
  list="$(find "$drained_dir" -maxdepth 1 -type f -mtime "+$age_days" 2>/dev/null || true)"
  [ -n "$list" ] && count="$(printf '%s\n' "$list" | grep -c . || true)"
  if [ "$count" -gt 0 ] && [ "$dry_run" -eq 0 ]; then
    find "$drained_dir" -maxdepth 1 -type f -mtime "+$age_days" -delete 2>/dev/null || true
  fi
  echo "$count"
}

# cleanup_stray_dirs <temp_dir> <age_min> <dry_run> — Lane 3. Remove dirs
# DIRECTLY under temp_dir that are NOT drained/ and untouched past <age_min>
# minutes (abandoned scratch subdirs the file lanes never reach). Echoes the
# match count. Each removal is bounded under temp_dir/ by a per-dir re-assert
# (defense-in-depth) then a guarded `find "$stray" -delete` — never a hand-rolled
# rm. Caller MUST have asserted temp_dir safe. Sourceable + unit-tested.
cleanup_stray_dirs() {
  local temp_dir="${1:-}" age_min="${2:-120}" dry_run="${3:-0}" list count=0 d
  [ -d "$temp_dir" ] || { echo 0; return 0; }
  list="$(find "$temp_dir" -mindepth 1 -maxdepth 1 -type d ! -name drained -mmin "+$age_min" 2>/dev/null || true)"
  [ -z "$list" ] && { echo 0; return 0; }
  # Iterate candidates: preserve archive-before-delete archives ();
  # `count` reflects ONLY dirs actually purged (or that WOULD purge under
  # --dry-run), never the preserved archives.
  while IFS= read -r d; do
    [ -z "$d" ] && continue
    # archive-before-delete guard (): NEVER purge a stray dir that is an
    # archive-before-delete archive. A top-level RECEIPT.md or .archive-marker
    # sentinel marks a retention-immune recovery layer; destroying it as a drain
    # side-effect is the exact anti-pattern archive-before-delete.md forbids
    # (nearly lost -zeta-orphan-archive-20260713, a completed-S3-deletion
    # recovery layer). Preserve + report on stderr; do NOT count as purged.
    if [ -e "$d/RECEIPT.md" ] || [ -e "$d/.archive-marker" ]; then
      echo "temp-drain-purge: PRESERVING archive dir (RECEIPT.md/.archive-marker present): $d" >&2
      continue
    fi
    count=$((count + 1))
    if [ "$dry_run" -eq 0 ]; then
      case "$d" in "$temp_dir"/*) find "$d" -delete 2>/dev/null || true ;; esac
    fi
  done <<EOF
$list
EOF
  echo "$count"
}

main() {
  local DRY_RUN=0 AGE_MIN=120 DRAINED_AGE_DAYS=30
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --age-min) AGE_MIN="${2:?temp-drain-purge.sh: --age-min needs a value}"; shift 2 ;;
      --drained-age-days) DRAINED_AGE_DAYS="${2:?temp-drain-purge.sh: --drained-age-days needs a value}"; shift 2 ;;
      -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; return 0 ;;
      *) echo "temp-drain-purge.sh: unknown arg '$1'" >&2; return 2 ;;
    esac
  done
  case "$AGE_MIN" in
    ''|*[!0-9]*) echo "temp-drain-purge.sh: --age-min must be a non-negative integer, got '$AGE_MIN'" >&2; return 2 ;;
  esac
  case "$DRAINED_AGE_DAYS" in
    ''|*[!0-9]*) echo "temp-drain-purge.sh: --drained-age-days must be a non-negative integer, got '$DRAINED_AGE_DAYS'" >&2; return 2 ;;
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
  # Emits the FULL field set (all-zero lane fields) so this exit path shares one
  # schema with the main path below (fresh-eyes finding: a consumer of the lane
  # fields must not KeyError on the no-temp-dir branch).
  if [ ! -d "$temp_dir" ]; then
    printf '{"purged":0,"would_purge":0,"files":[],"drained_gc_purged":0,"drained_gc_would_purge":0,"stray_purged":0,"stray_would_purge":0,"dry_run":%s,"age_min":%d,"drained_age_days":%d,"temp_dir":"%s","note":"temp dir does not exist"}\n' \
      "$([ "$DRY_RUN" -eq 1 ] && echo true || echo false)" "$AGE_MIN" "$DRAINED_AGE_DAYS" "$temp_dir"
    return 0
  fi

  # ── Lane 1 (ephemera + empties). List purgeable files (for the caller's
  # report), then delete (unless --dry-run). The purge glob is the SSOT function
  # _purge_find_predicate (see its header for the two sub-lanes + the
  # temp-store.md sync obligation) — used here for BOTH passes so list and delete
  # can never diverge. -maxdepth 1 -type f leaves drained/ untouched.
  _purge_find_predicate "$AGE_MIN"
  local ephemera_list count
  ephemera_list="$(find "$temp_dir" "${PURGE_FIND_PRED[@]}" 2>/dev/null || true)"
  if [ -z "$ephemera_list" ]; then count=0; else count="$(printf '%s\n' "$ephemera_list" | grep -c . || true)"; fi

  if [ "$count" -gt 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    find "$temp_dir" "${PURGE_FIND_PRED[@]}" -delete 2>/dev/null || true
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

  # ── Lanes 2 & 3 (extracted → gc_drained_archive / cleanup_stray_dirs, both
  # sourceable + unit-tested). Bounded by the assert_safe_temp_dir guard already
  # passed above for temp_dir; each echoes its match count (would-purge when
  # --dry-run, else purged).
  local gc_count stray_count
  gc_count="$(gc_drained_archive "$temp_dir/drained" "$DRAINED_AGE_DAYS" "$DRY_RUN")"
  stray_count="$(cleanup_stray_dirs "$temp_dir" "$AGE_MIN" "$DRY_RUN")"

  local purged gc_purged stray_purged
  if [ "$DRY_RUN" -eq 1 ]; then
    purged=0; gc_purged=0; stray_purged=0
  else
    purged="$count"; gc_purged="$gc_count"; stray_purged="$stray_count"
  fi
  printf '{"purged":%d,"would_purge":%d,"files":%s,"drained_gc_purged":%d,"drained_gc_would_purge":%d,"stray_purged":%d,"stray_would_purge":%d,"dry_run":%s,"age_min":%d,"drained_age_days":%d,"temp_dir":"%s"}\n' \
    "$purged" "$count" "$files_json" "$gc_purged" "$gc_count" "$stray_purged" "$stray_count" \
    "$([ "$DRY_RUN" -eq 1 ] && echo true || echo false)" "$AGE_MIN" "$DRAINED_AGE_DAYS" "$temp_dir"
  return 0
}

# Run main() ONLY when executed directly, not when sourced (so the guard is
# unit-testable via `source`).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
  exit $?
fi
