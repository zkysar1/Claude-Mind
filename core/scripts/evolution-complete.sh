#!/usr/bin/env bash
# evolution-complete.sh — Phase b of D1 (per world/conventions/self-program-evolution.md).
#
# Thin wrapper: delegates to evolution-complete.py via py -3.
# Why bash wrapper exists: matches the project's invocation convention
# (productivity-stop-gate.sh, reflection-cadence-stamp.sh, wm.sh) and
# survives the Windows python3 stub (rb-370, guard-335).
#
# CLI passthrough — see core/scripts/evolution-complete.py --help.
#
# Authorized callers:
#   - Agent: after Edit/Write to self.md, program.md, SKILL.md, or rule .md
#     (guardrail action_hint will prompt the agent)
#   - User: manual override (rare)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec py -3 "$SCRIPT_DIR/evolution-complete.py" "$@"
