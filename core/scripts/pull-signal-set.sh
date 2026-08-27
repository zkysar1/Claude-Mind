#!/usr/bin/env bash
# pull-signal-set.sh — THE producer for the `pull_signal` dependency-pull flag.
#
#  item (1), producer half. The CONSUMER (goal-selector.py
# `apply_pull_boost`) shipped 2026-08-17 and had NO producer for six days, so the
# feature was inert by construction: the boost requires a `pull_signal` dict and
# nothing ever wrote one. This script is that writer.
#
# ONE PRODUCER, TWO CALLERS — never two implementations (guard-2676, the
# no-transcription contract). The two lanes differ ONLY in how the triggering
# event is detected; the decide-and-write half is identical and lives here:
#   (a) WORKER lane  — worker-loop Phase 3.8, after `iteration-push.sh
#       --push-worker-ref`. Invoked as `--if-carrier-content`: this script owns
#       the git plumbing (detection is bash-owned, guard-399) so the SKILL.md
#       pseudocode stays a single call and cannot drift.
#   (b) REDUCER lane — `worker-ref-consume.sh --check`, which already computes
#       the exact non-merge framework-file count this needs. It passes an
#       explicit `--reason`; re-deriving the git plumbing there would be a second
#       copy of an instrument that already exists.
#
# WHY `--if-carrier-content` HAS A DISTINCT "UNREADABLE" VERDICT. A failed
# rev-list and a genuinely-empty carrier both yield zero files, and reporting
# them identically is the F-002 defect worker-ref-consume.sh already fixed on
# itself (guard-2298 class): an error that renders as a healthy zero is a
# permanently-silent producer. So a git failure prints SKIP-unreadable, never
# SKIP-no-content.
#
# IDEMPOTENCE — skip iff a LIVE signal already exists (rb-662, claim-once).
# The worker lane runs every work unit and the reducer lane every iteration
# close, so an unguarded producer would rewrite a shared world-store field
# several times an hour to no effect. A live signal already carries the boost;
# rewriting it changes no ranking and only adds contention. When the signal has
# aged out (or the consumer cleared it) and the dependency is STILL outstanding,
# the next call re-stamps — which is correct: outstanding content should keep
# pulling.
#
# FAIL-SOFT BY CONTRACT. Both call sites are advisory paths that must never fail
# on a visibility instrument, so this exits 0 unless --strict. The verdict is on
# stdout, not in the rc; tests assert the verdict, callers ignore the rc.
#
# Usage:
#   bash core/scripts/pull-signal-set.sh --if-carrier-content
#   bash core/scripts/pull-signal-set.sh --goal <id> --reason "<one line>"
#   bash core/scripts/pull-signal-set.sh --goal <id> --clear
#
# Verdicts (first token of stdout):
#   SET             wrote a fresh pull_signal
#   CLEARED         wrote pull_signal null (never a key removal — see goal-schemas.md)
#   SKIP-live       a live signal already exists; nothing to do
#   SKIP-no-content --if-carrier-content found no non-merge framework files
#   SKIP-unreadable git or the store could not answer (NOT the same as no content)
#   SKIP-no-goal    the consumer goal id resolved to no record
#   SKIP-write-failed  the update-goal write was refused
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
REPO="$PROJECT_ROOT"

GOAL=""; SOURCE="world"; REASON=""; BY=""; IF_CARRIER=0; DO_CLEAR=0; STRICT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --goal)               GOAL="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --source)             SOURCE="${2:-world}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --reason)             REASON="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --by)                 BY="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --if-carrier-content) IF_CARRIER=1; shift;;
    --clear)              DO_CLEAR=1; shift;;
    --strict)             STRICT=1; shift;;
    -h|--help)            sed -n '1,52p' "${BASH_SOURCE[0]}"; exit 0;;
    *) echo "pull-signal-set: unknown flag '$1'" >&2; exit 2;;
  esac
done

die_soft() { echo "$*"; [ "$STRICT" = 1 ] && exit 1; exit 0; }

# Config: the consumer goal id and the age bound both live in aspirations.yaml
# so this script and the scorer read ONE source of truth (communication-clarity
# rule 5). A hardcoded goal id here would be a second place to update.
CFG="$(SD="$SCRIPT_DIR" python3 - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["SD"])
try:
    import _paths, yaml
    with open(_paths.CONFIG_DIR / "aspirations.yaml", encoding="utf-8") as f:
        cfg = (yaml.safe_load(f) or {}).get("pull_boost") or {}
    print(cfg.get("carrier_consumer_goal") or "-", float(cfg.get("max_age_hours", 24.0)))
except Exception:
    print("-", 24.0)
PYEOF
)"
CFG_GOAL="$(printf '%s' "$CFG" | awk '{print $1}')"
CFG_MAX_AGE="$(printf '%s' "$CFG" | awk '{print $2}')"
[ -z "$CFG_GOAL" ] && CFG_GOAL="-"
[ -z "$CFG_MAX_AGE" ] && CFG_MAX_AGE="24.0"

[ -z "$GOAL" ] && [ "$CFG_GOAL" != "-" ] && GOAL="$CFG_GOAL"
[ -z "$GOAL" ] && die_soft "SKIP-no-goal (no --goal and no pull_boost.carrier_consumer_goal in aspirations.yaml)"
[ -z "$BY" ] && BY="${MIND_AGENT:-unknown}/$(hostname 2>/dev/null || echo unknown-host)"

# --- WORKER-LANE DETECTION (bash-owned, guard-399) -------------------------
# Non-merge framework content this body carries that origin/main lacks. Merge
# commits are excluded for the reason worker-ref-consume.sh measured: a body's
# own "Merge origin/main" syncs carry nothing main does not already have, and
# counting them makes every push look like a delivery.
if [ "$IF_CARRIER" = 1 ]; then
  if ! git -C "$REPO" rev-parse --verify -q origin/main >/dev/null 2>&1; then
    die_soft "SKIP-unreadable (origin/main not resolvable — git could not answer; this is NOT 'no content')"
  fi
  FW="$(git -C "$REPO" log --no-merges --name-only --format= origin/main..HEAD \
          -- core .claude CLAUDE.md 2>/dev/null | sed '/^$/d' | sort -u)"
  GIT_RC=$?
  if [ "$GIT_RC" -ne 0 ]; then
    die_soft "SKIP-unreadable (git log failed rc=$GIT_RC — NOT 'no content')"
  fi
  FW_COUNT="$(printf '%s' "$FW" | grep -c . || true)"
  if [ "$FW_COUNT" -eq 0 ]; then
    die_soft "SKIP-no-content (0 non-merge framework files in origin/main..HEAD)"
  fi
  REF_SHA="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  REASON="carrier ref $REF_SHA, $FW_COUNT framework file(s)"
fi

if [ "$DO_CLEAR" = 0 ] && [ -z "$REASON" ]; then
  die_soft "SKIP-write-failed (--reason is required unless --if-carrier-content or --clear)"
fi

# --- DECIDE + WRITE --------------------------------------------------------
# The decision lives in pull_signal_producer.decide() — a PURE function, so every
# branch is testable without a daemon, a world or a git tree (same shape and same
# reason as reducer_self_fence.py::decide). This wrapper owns only arg parsing
# and the git detection above; it must not re-implement any of the decision.
GOAL="$GOAL" SOURCE="$SOURCE" REASON="$REASON" BY="$BY" \
MAX_AGE="$CFG_MAX_AGE" DO_CLEAR="$DO_CLEAR" \
python3 "$SCRIPT_DIR/pull_signal_producer.py"
PY_RC=$?
[ "$STRICT" = 1 ] && exit "$PY_RC"
exit 0
