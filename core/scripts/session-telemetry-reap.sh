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
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

TSDIR="$SCRIPT_DIR" python3 -c 'import os, sys, json; sys.path.insert(0, os.environ["TSDIR"]); from _session_telemetry import reap_stale_active; print(json.dumps(reap_stale_active()))'
