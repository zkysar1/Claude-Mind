#!/usr/bin/env bash
# agent-aspirations-meta-update — thin wrapper that forces --source agent.
# Delegates to aspirations-meta-update.sh (daemon-aware since PR 52).
set -euo pipefail
exec bash "$(cd "$(dirname "$0")" && pwd)/aspirations-meta-update.sh" --source agent "$@"
