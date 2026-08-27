#!/usr/bin/env bash
# tree-shape-fork.sh — crit3 shape-fork measurement for a knowledge-tree node.
#
# Thin wrapper over tree_shape_fork.py (gap-111). Mechanizes the MEASUREMENT
# half of the mandatory shape fork at .claude/skills/tree/SKILL.md Step 1.6
# (guard-2109 / rb-6055) — heading map, per-section BYTE spans, distribution —
# and DELIBERATELY WITHHOLDS the (a)/(b)/(c)/(d) routing call, which needs
# judgment about whether early sections are referenced by later ones.
#
# Framework placement, not world/scripts: it takes a markdown path and touches
# no domain resource, no named service and no credential.
#
# Exit codes are the contract:
#   0  profile emitted
#   2  no such file
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# : under Git Bash on Windows, $(cd ... && pwd) returns POSIX form
# /c/... which Windows python3 misreads as drive C: plus a literal subdir c/,
# yielding FileNotFoundError on C:\c\...\tree_shape_fork.py. Convert to
# Windows-native form before exec. Linux/macOS lack cygpath and fall through
# with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/tree_shape_fork.py" "$@"
