#!/usr/bin/env bash
# transplant-pack.sh {own-cloud|offline|land|verify} [flags]
#
# Source-side packer for the /transplant skill — relocates a LIVING mind
# (agents + world + meta + identity) to another machine. NOT daemon-routed:
# this is a filesystem/packaging utility (like seed-transplant.sh), not a
# data-layer op, so the no-python-cli-fallback rule does not apply. It resolves
# the external world/meta paths + storage backend, then delegates to the Python
# engine via `py -3` (Windows-safe per python-invocation.md).
#
# Sub-commands:
#   own-cloud  — verify backend+git, print destination bring-up checklist (no archive)
#   offline    — pack a portable archive (git-archive + world/ + meta/, minus .history)
#   land       — destination-side resume helper (offline: unpack+wire paths)
#   verify     — post-land smoke test at a destination
#
# The actual resume at the destination is /start Phase A-0's job — this script
# NEVER writes agent-state/agent-mode (guard-340) and never calls /start.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
set -euo pipefail

SUB="${1:-}"
if [ -z "$SUB" ]; then
    echo "usage: transplant-pack.sh {own-cloud|offline|land|verify} [flags]" >&2
    exit 1
fi
shift

# Resolve the storage backend (own-cloud gate) from env, else .env.local, else 'local'.
BACKEND="${STORAGE_BACKEND:-}"
if [ -z "$BACKEND" ] && [ -f "$PROJECT_ROOT/.env.local" ]; then
    BACKEND="$(grep -E '^STORAGE_BACKEND=' "$PROJECT_ROOT/.env.local" 2>/dev/null \
        | head -1 | cut -d= -f2- | tr -d '\r' | awk '{print $1}' || true)"
fi
BACKEND="${BACKEND:-local}"

# Default owned-agents list (own-cloud checklist) from .env.local.
OWNED=""
if [ -f "$PROJECT_ROOT/.env.local" ]; then
    OWNED="$(grep -E '^MACHINE_OWNED_AGENTS=' "$PROJECT_ROOT/.env.local" 2>/dev/null \
        | head -1 | cut -d= -f2- | tr -d '\r' | awk '{print $1}' || true)"
fi

exec py -3 "$SCRIPT_DIR/_transplant_pack.py" "$SUB" \
    --project-root "$PROJECT_ROOT" \
    --world "${WORLD_PATH:-}" \
    --meta "${META_PATH:-}" \
    --backend "$BACKEND" \
    --owned-agents "$OWNED" \
    "$@"
