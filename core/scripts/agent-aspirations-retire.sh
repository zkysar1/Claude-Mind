#!/usr/bin/env bash
# agent-aspirations-retire — thin wrapper that forces --source agent.
# Delegates to aspirations-retire.sh (daemon-aware). aspirations.py
# `retire` was deleted in the 2026-05-14 cutover; see
# .claude/rules/no-python-cli-fallback.md
set -euo pipefail
exec bash "$(cd "$(dirname "$0")" && pwd)/aspirations-retire.sh" --source agent "$@"
