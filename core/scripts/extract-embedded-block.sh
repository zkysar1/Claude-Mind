#!/usr/bin/env bash
# Thin wrapper for extract-embedded-block.py — companion script of the
# `extract-and-run-embedded-block` forged skill (gap-042).
#
# NOT a daemon wrapper: this is a standalone local tool, so
# .claude/rules/no-python-cli-fallback.md does not apply (that rule governs the
# 35 wrappers migrated to daemon endpoints). The wrapper exists only so bash
# callers get the `py -3` invocation right — a direct `python3 -c` from a Bash
# tool call hits the Windows Store stub (python-invocation.md).
#
# Usage:
#   extract-embedded-block.sh --list
#   extract-embedded-block.sh --name <check-name> [--run] [--json]
#   extract-embedded-block.sh --name <check> --from staged --run
#   extract-embedded-block.sh --grammar shell --file <path> \
#       --open-marker "python3 -c '" --assert-no-apostrophe --run
set -uo pipefail

_SELF="$(cd "$(dirname "$0")" && pwd)"
# py -3 on Windows, python3 elsewhere: never a bare `py -3` (, guard-1098).
source "$_SELF/_python_launcher.sh"
PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$_SELF/../.." && pwd)}"
cd "$PROJECT_ROOT" || exit 2

# Exit code is the extractor's verdict (0 PASS / 1 FAIL / 2 ERROR /
# 3 INDETERMINATE) — do NOT append an echo or a pipe after this call, either
# would replace it with the appended command's status (guard-1150, guard-888).
exec $PYLAUNCH "$PROJECT_ROOT/core/scripts/extract-embedded-block.py" "$@"
