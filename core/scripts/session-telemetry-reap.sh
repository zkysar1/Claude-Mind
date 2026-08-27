#!/usr/bin/env bash
# session-telemetry-reap.sh — periodic stale-active telemetry reaper (WP1 Phase 1.5).
#
# Flips ORPHANED session-telemetry records to status=unknown /
# ended_reason=unknown. A record is reaped only when ALL hold: status=active,
# started >24h ago, this machine, AND the owning agent's autonomous runner is
# NOT alive (runner-heartbeat stale >6h). These orphans are (a) force-closed
# reader/assistant/observer sessions (killed terminal — no /stop, no
# crash-recovery path) and (b) autocompact-rotated old-SID autonomous records —
# design doc §8.6 #1 + #3 (mind_api/docs/session-telemetry-design.md).
#
# Invoked by the  recurring maintenance goal (~24h interval), NOT from
# any hook hot path: a full telemetry-tree scan is too heavy for
# cleanup-stale-bindings.sh (IRREDUCIBLY LOCAL, runs every turn-end +
# SessionStart). Running it from the recurring goal also guarantees the live
# runner's own record is heartbeat-fresh at firing time (the loop ticks the
# heartbeat in Phase -0.5 of the same iteration), so the reaper can never
# clobber the agent that is running it. reap_stale_active is total, idempotent,
# and guards live runners via heartbeat freshness — safe to run anytime.
#
# python3 is sanctioned here: this .sh sources _paths.sh (CLAUDE.md
# python-invocation rule). The import-a-pure-library form (no
# `scripts/X.py <subcommand>`) is clean past check-no-python-cli-fallback —
# the same pattern recovery-gate.sh uses for write_crash. guard-165: the
# script dir passes via ENV (TSDIR); the python source is single-quoted, no
# bash interpolation. The library self-resolves world_dir/project_root via its
# own _paths import (MIND_AGENT-scoped), so no path args are threaded in.
#
# Prints the reaper summary JSON to stdout for the recurring goal's
# verification. stderr is NOT suppressed (rb-400) — a broken import must
# surface, not hide.
#
# EXIT CODES (). 0 = no backlog. 3 = BACKLOG PRESENT — stale-active
# records exist that this box cannot reap. **3 IS NOT AN ERROR** and must not be
# reported as a failed run; it is a read-the-output signal (guard-4910), and the
# JSON on stdout is still complete and still valid. A genuine fault (broken
# import, unreadable tree) surfaces as a python traceback on stderr with a
# non-3 code.
#
# WHY THE VERDICT MOVES THE EXIT CODE RATHER THAN ONLY ADDING A FIELD:
# guard-963's closing clause, measured — "a human-readable warning beside an
# unchanged rc=0 is a false clean wearing a caveat". The summary ALREADY carried
# `skipped_other_machine` and it did not stop anyone reading `reaped: 0` as a
# clean fleet; the recurring goal's own run log has an agent writing "CLEAN"
# directly beside a 54-record backlog. A field that the reader has to already be
# suspicious of is not a signal. Expect rc=3 to persist until the backlog is
# genuinely dealt with — that visible permanence is the mechanism, not a defect
# to tune away.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

# Heredoc is quoted ('PY') so bash performs NO interpolation; the script dir
# still travels by ENV, not by substitution (guard-165).
TSDIR="$SCRIPT_DIR" python3 - <<'PY'
import os, sys, json
sys.path.insert(0, os.environ["TSDIR"])
from _session_telemetry import reap_stale_active

summary = reap_stale_active()
print(json.dumps(summary))

n = summary.get("backlog_total", 0)
if n:
    by = summary.get("backlog_by_machine") or {}
    top = ", ".join(f"{m}={c}" for m, c in
                    sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))[:3])
    sys.stderr.write(
        f"BACKLOG: {n} stale-active record(s) that NO box is reaping "
        f"({summary.get('stale_other_machine', 0)} owned by other machines, "
        f"{summary.get('stale_here_held_live', 0)} on this box held by an "
        f"agent-granular liveness verdict). Concentrated: {top}. "
        f"The 'reaped: 0' above is CORRECT for this box and does NOT mean the "
        f"fleet is clean — see g-115-5114. Exit 3 = backlog present, NOT a "
        f"failed run.\n")
    sys.exit(3)
PY
