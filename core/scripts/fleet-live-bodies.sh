#!/usr/bin/env bash
# Derive the LIVE BODY stop list at run time. Thin wrapper around fleet-live-bodies.py.
#
# Usage:
#   bash core/scripts/fleet-live-bodies.sh [--json] [--agent <a>] [--sid <s>] [--host <h>]
#
# Use it wherever a ceremony must reach EVERY Body (quiesce, drain, restart,
# credential rotation, upgrade). Its rows COUNT BODIES — one reducer Body plus N
# worker Bodies per agent, each its own session on its own box, each needing its
# own `/stop`. A hand-maintained AGENT table cannot follow that and drifts
# silently (guard-6027, ).
#
# Stdout: a stop table (or JSON with --json). Exit: 0 always — it is a read, and
# an advisory that refuses to run is worse than one that reports what it saw.
# Rows marked `!` could not be confirmed from this box: verify at the terminal,
# never assume idle. Fresh carriers are a FLOOR, not a census.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
py -3 "$SCRIPT_DIR/fleet-live-bodies.py" "$@"
