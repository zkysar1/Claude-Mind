#!/usr/bin/env bash
# Write a session binding.yaml. Thin wrapper around session-binding-write.py.
#
# Usage:
#   bash core/scripts/session-binding-write.sh \
#       --sid <SID> --agent <NAME> --mode <reader|assistant|autonomous> \
#       [--started-by claude-code] [--retire-legacy]
#
# Stdout: absolute path of the binding.yaml on success.
# Stderr: human-readable error on failure.
# Exit: 0 success, 2 validation error, 3 write error.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
py -3 "$SCRIPT_DIR/session-binding-write.py" "$@"
