#!/usr/bin/env bash
# agent-aspirations-read — thin wrapper that forces --source agent.
# Delegates to aspirations-read.sh (daemon-aware). aspirations.py `read`
# was deleted in the 2026-05-14 cutover; see
# .claude/rules/no-python-cli-fallback.md
set -euo pipefail
exec bash "$(cd "$(dirname "$0")" && pwd)/aspirations-read.sh" --source agent "$@"
