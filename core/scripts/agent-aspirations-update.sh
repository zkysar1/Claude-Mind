#!/usr/bin/env bash
# agent-aspirations-update — thin wrapper that forces --source agent.
# Delegates to aspirations-update.sh (daemon-aware). aspirations.py
# `update` was deleted in the 2026-05-14 cutover; see
# .claude/rules/no-python-cli-fallback.md
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/aspirations-update.sh" --source agent "$@"
