#!/usr/bin/env bash
# Generalize-down body-WM merge (Phase 1C). Thin wrapper around body-merge.py.
#
# Usage:
#   bash core/scripts/body-merge.sh generalize-down --agent <mindKey> [--output json|text]
#
# Stdout: JSON summary {agent, scanned, merged[], noop[], skipped[], passes}.
# Stderr: human-readable error on failure.
# Exit: 0 success (incl. nothing-to-merge), 2 validation error, 3 io error.
#
# Backward-compatible / dormant: in single-runner no closed-pending-merge Body
# manifest exists, so this is a no-op (empty summary). It activates only once a
# 2nd worker Body forks (Phase 2). See body-merge.py module docstring + the SSOT
# tree node mind-engine-identity-bridge.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
py -3 "$SCRIPT_DIR/body-merge.py" "$@"
