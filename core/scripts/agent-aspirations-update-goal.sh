#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# agent-aspirations-update-goal — thin wrapper that forces --source agent.
#
# Normalizes --goal/--goal-id flag forms to a positional goal id, then
# delegates to aspirations-update-goal.sh (daemon-aware) with --source agent.
# This mirrors the thin-delegator pattern of the other 8 agent-aspirations-*
# wrappers. The previous direct local-CLI invocation (the aspirations engine's
# update-goal subcommand) is retired (2026-05-29 cutover): the daemon endpoint
# /v1/aspirations/update-goal handles every field write end-to-end, including
# Layer-D auto-Unblock filing on defer-time capability blocks (the sibling
# wrapper translates the daemon response back to the legacy CLI stdout shape).
set -euo pipefail

_SELF="$(cd "$(dirname "$0")" && pwd)"

# Normalize --goal/--goal-id → positional goal id (rewrites $@). MUST stay —
# verify-learning enforces that this wrapper sources the normalizer (the
# 12-wrapper normalizer-coverage grep). The sibling parses bare positionals.
GOAL_NORMALIZE_TARGET=positional source "$_SELF/_goal-arg-normalize.sh"

exec "$_SELF/aspirations-update-goal.sh" --source agent "$@"
