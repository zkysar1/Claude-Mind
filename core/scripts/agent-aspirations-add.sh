#!/usr/bin/env bash
# agent-aspirations-add — thin wrapper that forces --source agent.
# Delegates to aspirations-add.sh (daemon-aware). aspirations.py
# `add` was deleted in the 2026-05-14 cutover; see
# .claude/rules/no-python-cli-fallback.md
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/aspirations-add.sh" --source agent "$@"
