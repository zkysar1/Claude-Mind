#!/usr/bin/env bash
# skill-edit-gate.sh — Tier-1 skill-quality gate (eval-harness-forge-accept), the
# portable entry point for core/scripts/skill_edit_gate.py.
# Usage: skill-edit-gate.sh gate --new-judgments '<json>' --skill-name <name> --caller <who>
#        [--old-judgments '<json>' --policy no_regression --epsilon 0.02]
# Exit codes are the gate's own: 0 = PASS, 1 = BLOCK (logged), 2 = malformed call (not a verdict).
# Why a wrapper: forge-skill Step 3.5 used to say `py -3 core/scripts/skill_edit_gate.py`, the
# launcher spelling that works only where a `py` shim is installed (it is, on every fleet box
# measured 2026-09-05 — cc-04 and zc-03 both carry /usr/local/bin/py). A wrapper that sources
# _paths.sh resolves python3 the way every other core script does, so the one gate a forge
# cannot skip no longer depends on a box-level shim (python-invocation.md: prefer the wrapper).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/skill_edit_gate.py" "$@"
