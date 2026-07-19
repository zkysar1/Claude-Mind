#!/usr/bin/env bash
# run-invisible-suites.sh — run every pytest-INVISIBLE test file in
# core/scripts/tests (main()-style files with zero `def test_` functions,
# which `pytest core/scripts/tests` collects NOTHING from) and report
# per-file pass/fail plus an aggregate verdict.
#
# Why this exists (9, 2026-07-16): 69 of the test files here are
# main()-style. The daemon-safe full suite (4400+ collected tests) says
# nothing about any of them, so their redness is silent until someone runs
# them by hand — observed twice in one week: the asp-257 suites sat red 3
# days masking a real NameError (3 / rb-3678), and
# test_layer_d_telemetry.py was red the same way outside any aggregator
# (8). This runner is the population-level counterpart of
# run-asp-257-suite.sh: enumeration is DYNAMIC (a new main()-style file is
# covered automatically; a file converted to pytest shape drops out
# automatically).
#
# Usage:
#   bash core/scripts/tests/run-invisible-suites.sh [--list]
#     --list  print the enumerated invisible files and exit (no runs)
#
# Exit: 0 when every file passes, 1 when any fails, 0 on --list.
#
# NOTE: files run SEQUENTIALLY — several main()-style suites are
# standalone-required (rb-2078) or spawn tmp-world subprocesses that would
# interfere under parallelism. Per-file timeout bounds a hung suite.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../_paths.sh"
cd "$PROJECT_ROOT"
# _paths.sh REDEFINES SCRIPT_DIR to its own dir (core/scripts) when sourced —
# re-derive the tests dir from PROJECT_ROOT after the source. (This is why
# sibling run-asp-257-suite.sh addresses files via $CORE_ROOT, never
# $SCRIPT_DIR, after its source line.)
TESTS_DIR="$PROJECT_ROOT/core/scripts/tests"

# INCIDENT rb-2983/guard-955: pin the storage backend to local so no suite
# here can PUT to the production own-cloud S3 store. main()-style files are
# exactly the population the pytest conftest autouse pin can NEVER protect —
# this export is their only fence. Do not remove.
export STORAGE_BACKEND=local

PER_FILE_TIMEOUT="${PER_FILE_TIMEOUT:-300}"

command -v python3 >/dev/null 2>&1 || { echo "ERROR: no python3 on PATH — _paths.sh shim not active" >&2; exit 2; }

# QUARANTINE — known-red files, each with a triage verdict and an open goal.
# Quarantined files are SKIPPED (listed loudly, never run) so the aggregate
# verdict stays meaningful for the healthy population. Remove a file from
# this list in the SAME commit that fixes it (its goal ID is the tracker).
# Baseline: 9 sweep, 2026-07-16 — 9 reds of 69.
declare -A QUARANTINE=(
  # Class A (1) RESOLVED 2026-07-17: all four repaired — and the sweep
  # surfaced two REAL production bugs the reds had been pointing at:
  #   (1) board.py's findings attribution subprocess (`reasoning-bank.py
  #       <family> increment`) had been a silent no-op since H2 Wave 2 removed
  #       the rb CLI (2026-05-15) — now routes via _rt.store_increment;
  #   (2) the citation regexes (board.py, board_write.py _CITE_RE,
  #       journal-append.sh) matched EXACTLY 3 digits, silently excluding
  #       every modern 4-digit ID (rb-3742, guard-1151) — now \d{3,}.
  #   test_jsonl_id_race.py — rewritten on DaemonFixture+HTTP against the
  #     daemon append allocator; the LIVE-STORE SWAP hazard is gone.
  #   test_board_source_tag_attribution.py — rewritten as an in-process unit
  #     test with stubbed _rt; daemon e2e (incl. 4-digit regression) lives in
  #     mind_api/tests/test_runtime_board_write.py.
  #   test_journal_tree_cite_scan.py — rewritten on DaemonFixture (on a
  #     .mind-data box the LIVE daemon resolves every agent to the live world
  #     before conf, so seeded confs can never sandbox it).
  #   test_all_unknown_backstop.py — agents/-relocated conf seed + env pins.
  # Class B (2) RESOLVED 2026-07-16: all three were harness drift.
  #   test_cross_lane_claim.py — rewritten on DaemonFixture+HTTP (cmd_claim was
  #     removed in the daemon-only migration; the endpoint owns the guard). The
  #     rewrite surfaced and fixed a REAL fixture gap: _daemon_fixture.py now
  #     pins MIND_WORLD/MIND_META (g-2297 bootstrap poisoned main()-style runs).
  #   test_distill_candidate_filters.py — sys.path insert before tree.py load.
  #   test_paths_mind_data_resolution.py — PROJECT_ROOT .parent arithmetic fix.
  # Class C (3) RESOLVED 2026-07-16: both were test drift, gates intact.
  #   test_remove_child_orphan_gate.py — scenario 3 converted to in-process
  #     cmd_batch (daemon-routed tree-update.sh ignored the tmp MIND_WORLD).
  #   test_fileops_corruption_guards.py — 3 allow-branch assertions repointed
  #     from the legacy .history/<file>/ layout to the Stage-2 CAS-delta store.
)

# Dynamic enumeration: main()-style = zero top-level `def test_` functions.
# nullglob: with no test_*.py matches the loop body never runs (default bash
# would pass the LITERAL pattern through — the pre-fix SCRIPT_DIR clobber
# produced exactly that "FAIL test_*.py" phantom; guard-1136).
shopt -s nullglob
mapfile -t INVISIBLE < <(
  for f in "$TESTS_DIR"/test_*.py; do
    grep -qE '^def test_' "$f" || echo "$f"
  done
)
shopt -u nullglob

if [ ${#INVISIBLE[@]} -eq 0 ]; then
  echo "invisible-suites: 0 pytest-invisible files — population fully pytest-collectable"
  exit 0
fi

if [ "${1:-}" = "--list" ]; then
  printf '%s\n' "${INVISIBLE[@]##*/}"
  echo "── ${#INVISIBLE[@]} pytest-invisible file(s)"
  exit 0
fi

PASSES=0
FAILS=0
SKIPPED=0
declare -a FAILED_FILES=()

for f in "${INVISIBLE[@]}"; do
  base="${f##*/}"
  if [ -n "${QUARANTINE[$base]:-}" ]; then
    SKIPPED=$((SKIPPED + 1))
    echo "QUARANTINED $base — ${QUARANTINE[$base]}"
    continue
  fi
  out=$(timeout "$PER_FILE_TIMEOUT" python3 "$f" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then
    PASSES=$((PASSES + 1))
    echo "PASS $base"
  else
    FAILS=$((FAILS + 1))
    FAILED_FILES+=("$base")
    echo "FAIL(rc=$rc) $base"
    printf '%s\n' "$out" | tail -8 | sed 's/^/    | /'
  fi
done

echo "════════════════════════════════════════"
echo "invisible-suites: $PASSES/$((PASSES + FAILS)) files passed, $SKIPPED quarantined (open goals above)"
if [ $FAILS -gt 0 ]; then
  echo "Failed files (NOT quarantined — new reds):"
  for ff in "${FAILED_FILES[@]}"; do echo "  - $ff"; done
  exit 1
fi
exit 0
