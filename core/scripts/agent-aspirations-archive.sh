#!/usr/bin/env bash
# agent-aspirations-archive — thin wrapper that forces --source agent.
# Delegates to aspirations-archive.sh (daemon-aware). aspirations.py
# `archive-sweep` was deleted in the 2026-05-14 cutover; see
# .claude/rules/no-python-cli-fallback.md
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/aspirations-archive.sh" --source agent "$@"
