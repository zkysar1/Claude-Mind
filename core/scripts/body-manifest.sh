#!/usr/bin/env bash
# Read/write the per-Body body-manifest.yaml. Thin wrapper around body-manifest.py.
#
# Usage:
#   bash core/scripts/body-manifest.sh write --sid <unitKey> --agent <mindKey> \
#       [--env-id local] [--role reducer|worker|observer] [--reducer-sid remote]
#     --reducer-sid remote  (-a) marks a CROSS-BOX worker: the reducer
#       holds the DDB claim from another machine. Forces the WM fork, which the
#       default path cannot do on a worker box because it decides by reading the
#       local running-session-id, and a worker box never writes one. "remote" is
#       the only accepted value — a cross-box reducer SID is unobtainable here.
#   bash core/scripts/body-manifest.sh read      --sid <unitKey> --agent <mindKey>
#   bash core/scripts/body-manifest.sh set-state  --sid <unitKey> --agent <mindKey> <state>
#   bash core/scripts/body-manifest.sh is-reducer --sid <unitKey> --agent <mindKey>
#
# Stdout: manifest path (write/set-state), JSON (read), true|false (is-reducer).
# Stderr: human-readable error on failure.
# Exit: 0 success, 2 validation error, 3 io error.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
py -3 "$SCRIPT_DIR/body-manifest.py" "$@"
