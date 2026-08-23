#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Wrapper for domain-class-gate.py — domain-class portfolio steering gate.
# Origin: rb-616, , . See domain-class-gate.py docstring.
#
# Usage:
#   domain-class-gate.sh check --candidate-class <class>
#   domain-class-gate.sh log --payload '{"date":"...","event":"domain_class_override","class":"...","current_ratio":...,"max":...,"reason":"..."}'
#
# Exit 0 on success (including fail-open paths). Non-zero only on argparse
# / payload validation errors. JSON output on stdout.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/domain-class-gate.py" "$@"
