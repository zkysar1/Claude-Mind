#!/usr/bin/env bash
# agent-aspirations-complete — thin wrapper that forces --source agent.
# Delegates to aspirations-complete.sh (daemon-aware). aspirations.py
# `complete` was deleted in the 2026-05-14 cutover; see
# .claude/rules/no-python-cli-fallback.md
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/aspirations-complete.sh" --source agent "$@"
