#!/usr/bin/env bash
# Lock-safe access to world/board/events.jsonl -- the multi-agent
# task-decomposition record store ( Gap 10, child 2/3).
# Subcommand-dispatched to the events.py engine.
#   add | update-status | read | list-active | check-completion
# Engine: core/scripts/events.py. Schema: world/conventions/events.md.
# APPEND-ONLY / event-sourced per guard-832 (own-cloud-synced store): a status
# change appends a NEW record (same event_id); readers fold-by-latest.
# Pure-CLI wrapper (no daemon routing) -- low-frequency store, uses _fileops
# file-locking directly; no daemon endpoint. See events.py docstring.
# NOTE: avoid naming the daemon runtime-call token in this .sh file --
# check-no-python-cli-fallback.sh greps for it WITHOUT stripping comments and
# would misclassify this pure-CLI wrapper, blocking the commit (rb-2116).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/events.py" "$@"
