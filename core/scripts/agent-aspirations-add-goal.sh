#!/usr/bin/env bash
# agent-aspirations-add-goal — thin wrapper that forces --source agent.
# Delegates to aspirations-add-goal.sh (daemon-aware). aspirations.py
# `add-goal` was deleted in the 2026-05-14 cutover; see
# .claude/rules/no-python-cli-fallback.md
set -euo pipefail
exec bash "$(cd "$(dirname "$0")" && pwd)/aspirations-add-goal.sh" --source agent "$@"
