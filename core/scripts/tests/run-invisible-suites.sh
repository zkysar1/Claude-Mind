#!/usr/bin/env bash
# run-invisible-suites.sh — run every pytest-INVISIBLE test file in
# core/scripts/tests and report per-file pass/fail plus an aggregate verdict.
#
# TWO populations are invisible to `pytest core/scripts/tests`:
#   (1) main()-style .py files — zero top-level `def test_` functions, so
#       pytest collects nothing from them (detected dynamically below).
#   (2) shell tests — .sh files, which pytest cannot collect AT ALL. Every
#       shell test here is invisible by construction, so unlike the .py half
#       there is no shape predicate to apply: the glob IS the population.
#
# The shell half was added 2026-07-29 (). Until then 19 shell
# contract tests executed in NO automated runner, and the cost was measured,
# not theoretical: test_iteration_commit.sh sat 6-of-55 red, one of its
# assertions since 2026-05-13 (c72c5d5c6 generalized a skip message and left
# the test behind) — 77 days silent. Worse than the visible reds, its fixture
# seeded agent dirs at the repo ROOT instead of under agents/, which made the
# production namespace-filter discovery find zero agent dirs and SELF-DISABLE;
# two further tests were therefore passing VACUOUSLY, asserting a filter does
# not fire when it could never have fired at all.
#
# Why this exists (, 2026-07-16): 69 of the test files here are
# main()-style. The daemon-safe full suite (4400+ collected tests) says
# nothing about any of them, so their redness is silent until someone runs
# them by hand — observed twice in one week: the asp-257 suites sat red 3
# days masking a real NameError ( / rb-3678), and
# test_layer_d_telemetry.py was red the same way outside any aggregator
# (). This runner is the population-level counterpart of
# run-asp-257-suite.sh: enumeration is DYNAMIC (a new main()-style file is
# covered automatically; a file converted to pytest shape drops out
# automatically).
#
# Usage:
#   bash core/scripts/tests/run-invisible-suites.sh [--list | --resolve-only]
#     --list          print the enumerated invisible files (both halves) and exit
#     --resolve-only  print the bound-agent resolution verdict and exit
#
# Exit: 0 when every file passes (or the run SKIPs unbound), 1 when any
# fails, 0 on --list / --resolve-only.
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

# ---- Bound-agent resolution () -----------------------------------
# The production scripts these suites invoke resolve their agent via
# MIND_AGENT, which normally arrives through the PreToolUse bash-agent-inject
# hook. That hook does NOT reach backgrounded Bash calls, cron, CI, or nested
# subshells — exactly the contexts a suite runner lives in — so an unbound run
# used to fail 6 files env-shaped (RuntimeError: wm: MIND_AGENT not set) and
# print them under the new-reds header, camouflaging genuine reds. _paths.sh
# cannot help: its conf fallthrough resolves WORLD_PATH only and never exports
# MIND_AGENT. Chain (first hit wins), mirroring sibling resolvers
# (_resolve_agent_from_sid.py, recovery-gate.sh): (1) env, (2) the resident
# runner via a sole running-session-id, (3) the sole local-paths.conf on a
# single-agent box. Ambiguous/empty → the dispatch half SKIPS loudly below
# (exit 0) rather than running unbound and manufacturing reds.
# MIND_AGENTS_ROOT is a TEST-hermeticity override only (precedent:
# gates/goal_duplication.py) — production callers never set it.
AGENTS_ROOT_DIR="${MIND_AGENTS_ROOT:-$(agents_root)}"
AGENT_RESOLUTION=""
_rsids=()
_confs=()
if [ -n "${MIND_AGENT:-}" ]; then
  AGENT_RESOLUTION="env"
else
  shopt -s nullglob
  _rsids=("$AGENTS_ROOT_DIR"/*/session/running-session-id)
  _confs=("$AGENTS_ROOT_DIR"/*/local-paths.conf)
  shopt -u nullglob
  if [ ${#_rsids[@]} -eq 1 ]; then
    _d="${_rsids[0]%/session/running-session-id}"
    export MIND_AGENT="${_d##*/}"
    AGENT_RESOLUTION="running-session-id"
  elif [ ${#_confs[@]} -eq 1 ]; then
    _d="${_confs[0]%/local-paths.conf}"
    export MIND_AGENT="${_d##*/}"
    AGENT_RESOLUTION="single-conf"
  fi
fi

# Test/debug surface: print the resolution verdict and exit before any
# enumeration or dispatch. This is what the hermetic regression test drives.
if [ "${1:-}" = "--resolve-only" ]; then
  echo "agent=${MIND_AGENT:-} resolution=${AGENT_RESOLUTION:-none}"
  exit 0
fi

# QUARANTINE — known-red files, each with a triage verdict and an open goal.
# Quarantined files are SKIPPED (listed loudly, never run) so the aggregate
# verdict stays meaningful for the healthy population. Remove a file from
# this list in the SAME commit that fixes it (its goal ID is the tracker).
# Baseline:  sweep, 2026-07-16 — 9 reds of 69.
declare -A QUARANTINE=(
  # Class A () RESOLVED 2026-07-17: all four repaired — and the sweep
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
  # Class B () RESOLVED 2026-07-16: all three were harness drift.
  #   test_cross_lane_claim.py — rewritten on DaemonFixture+HTTP (cmd_claim was
  #     removed in the daemon-only migration; the endpoint owns the guard). The
  #     rewrite surfaced and fixed a REAL fixture gap: _daemon_fixture.py now
  #     pins MIND_WORLD/MIND_META (g-2297 bootstrap poisoned main()-style runs).
  #   test_distill_candidate_filters.py — sys.path insert before tree.py load.
  #   test_paths_mind_data_resolution.py — PROJECT_ROOT .parent arithmetic fix.
  # Class C () RESOLVED 2026-07-16: both were test drift, gates intact.
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
# Shell half: BOTH separators. This directory uses test_*.sh (14) and
# test-*.sh (5) interchangeably, and globbing only the underscore form would
# leave those 5 invisible — reproducing the exact defect this runner closes,
# in the runner meant to close it. The originating goal itself said "14 shell
# tests"; the directory holds 19. Whenever this glob is edited, re-count with
#   ls core/scripts/tests/test_*.sh core/scripts/tests/test-*.sh | wc -l
# and compare against the --list total, rather than trusting either number.
# No shape predicate here (contrast the .py half): pytest cannot collect a
# .sh file at all, so every match is invisible by construction.
mapfile -t INVISIBLE_SH < <(
  for f in "$TESTS_DIR"/test_*.sh "$TESTS_DIR"/test-*.sh; do
    echo "$f"
  done | sort -u
)
shopt -u nullglob

if [ ${#INVISIBLE[@]} -eq 0 ] && [ ${#INVISIBLE_SH[@]} -eq 0 ]; then
  echo "invisible-suites: 0 pytest-invisible files — population fully pytest-collectable"
  exit 0
fi

if [ "${1:-}" = "--list" ]; then
  [ ${#INVISIBLE[@]} -gt 0 ] && printf '%s\n' "${INVISIBLE[@]##*/}"
  [ ${#INVISIBLE_SH[@]} -gt 0 ] && printf '%s\n' "${INVISIBLE_SH[@]##*/}"
  echo "── ${#INVISIBLE[@]} main()-style .py + ${#INVISIBLE_SH[@]} shell = $(( ${#INVISIBLE[@]} + ${#INVISIBLE_SH[@]} )) pytest-invisible file(s)"
  exit 0
fi

# Unresolvable binding: dispatching would re-manufacture the env-shaped reds
# this resolver exists to kill, so SKIP the whole half loudly instead. Exit 0
# is deliberate — a skip is not a failure, and the banner states the reason so
# a reader can never mistake it for coverage (rb-5650 looks-like-coverage).
# (--list above still works unbound: enumeration invokes nothing.)
if [ -z "${MIND_AGENT:-}" ]; then
  echo "════════════════════════════════════════"
  echo "invisible-suites: SKIPPED — no resolvable agent binding (g-115-4141)"
  echo "  MIND_AGENT unset; running-session-id files=${#_rsids[@]}, local-paths.conf files=${#_confs[@]} under $AGENTS_ROOT_DIR"
  echo "  Running ${#INVISIBLE[@]}+${#INVISIBLE_SH[@]} suites unbound would fail env-shaped, camouflaging genuine reds."
  echo "  Set MIND_AGENT=<name> to run this half."
  exit 0
fi

# Always record HOW this run was bound — an env-injected run and a self-resolved
# run are different launch contexts, and the log line is the only place that
# distinction survives.
echo "invisible-suites: agent=$MIND_AGENT resolution=$AGENT_RESOLUTION"

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

# ---- Shell half () -----------------------------------------------
# Ported from the reference implementation, $WORLD_PATH/scripts/run-domain-tests.sh
# ("Shell half"), which has run this shape against the domain suite since
# . Shares this runner's QUARANTINE map, tally, and FAILED_FILES so a
# shell red is reported exactly like a python red. `bash "$f"` matches the
# reference; guard-580's bare-"bash"-argv[0] prohibition is scoped to
# Windows-native Python subprocess.run(["bash",...]), not a shell script
# invoking bash, and this line is inside a shell script.
for f in "${INVISIBLE_SH[@]}"; do
  base="${f##*/}"
  if [ -n "${QUARANTINE[$base]:-}" ]; then
    SKIPPED=$((SKIPPED + 1))
    echo "QUARANTINED $base — ${QUARANTINE[$base]}"
    continue
  fi
  out=$(timeout "$PER_FILE_TIMEOUT" bash "$f" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then
    PASSES=$((PASSES + 1))
    echo "PASS $base (shell)"
  else
    FAILS=$((FAILS + 1))
    FAILED_FILES+=("$base")
    echo "FAIL(rc=$rc) $base (shell)"
    printf '%s\n' "$out" | tail -8 | sed 's/^/    | /'
  fi
done

echo "════════════════════════════════════════"
echo "invisible-suites: $PASSES/$((PASSES + FAILS)) files passed, $SKIPPED quarantined (open goals above)"
echo "  population: ${#INVISIBLE[@]} main()-style .py + ${#INVISIBLE_SH[@]} shell"
if [ $FAILS -gt 0 ]; then
  echo "Failed files (NOT quarantined — new reds):"
  for ff in "${FAILED_FILES[@]}"; do echo "  - $ff"; done
  exit 1
fi
exit 0
