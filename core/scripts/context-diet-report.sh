#!/usr/bin/env bash
# context-diet-report.sh — before/after instrument for the context-diet goal family.
#
# Prints STATIC (deterministic from the git tree) + DYNAMIC (this session's
# transcript) + READINESS (125k local-inference budget) sections, appends a
# ledger row to meta/context-diet-ledger.jsonl, and ratchets
# meta/audit-baselines.yaml.
#
# See core/scripts/context-diet-report.py for the design rationale — in
# particular WHY the dynamic ratchet is keyed per role (worker and reducer are
# structurally different populations; pooling them produces a flapping baseline
# that describes neither).
#
# Usage:
#   bash core/scripts/context-diet-report.sh [--json] [--no-ledger]
#                                            [--no-ratchet] [--hard-gate]
#                                            [--agent NAME] [--transcript PATH]
#
# Exit: 0 (report) — or 1 when --hard-gate and a ratchet verdict is `regressed`.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\context-diet-report.py.
# Convert to Windows-native form before exec. Linux/macOS lack cygpath and
# fall through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

# Single python3 invocation per python-invocation.md rule 3: inside a .sh
# wrapper that has sourced _paths.sh, the shim is on PATH and python3 is
# canonical. exec replaces the bash process — output and exit pass through.
exec python3 "$SCRIPT_DIR_NATIVE/context-diet-report.py" "$@"
