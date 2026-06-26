#!/usr/bin/env bash
# jsonl-hygiene.sh -- wrapper for jsonl_hygiene.py ( KEYSTONE).
#
# Sources _paths.sh (exports PROJECT_ROOT / WORLD_DIR / META_DIR / AGENT_DIR so
# the helper's `from _paths import ...` resolves) and .env.local (STORAGE_BACKEND
# + scoped backend creds for own-cloud) before running the helper.
#
# This is a standalone maintenance CLI (like aspirations-compact-completed.py),
# NOT one of the 35 daemon-migrated wrappers -- it has no daemon endpoint to
# fall back from. It bounds append-only JSONL stores via _fileops.locked_modify_jsonl,
# coordinating with the daemon through the per-file lock.
#
# Usage:
#   bash core/scripts/jsonl-hygiene.sh sweep [--apply]
#   bash core/scripts/jsonl-hygiene.sh rotate --path world/changelog.jsonl --mode rotate --by lines --max-lines 20000 [--apply]
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
if [ -f "$PROJECT_ROOT/.env.local" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$PROJECT_ROOT/.env.local"
  set +a
fi
# OwnCloudBackend.from_env (owncloud_backend.py _resolve_root_map) maps governed
# world/meta paths to roots via MIND_WORLD/MIND_META (or *_PATH). _paths.sh
# resolves these as WORLD_DIR/META_DIR but does NOT export the MIND_* names the
# backend reads, so the apply path errored ("cannot map a governed path to a
# root") on governed stores like meta/gate-firings.jsonl under
# STORAGE_BACKEND=owncloud. Re-expose them (BOTH together -- guard-652); an
# already-set MIND_* (e.g. from .env.local) wins. .
export MIND_WORLD="${MIND_WORLD:-$WORLD_DIR}"
export MIND_META="${MIND_META:-$META_DIR}"
exec py -3 "$SCRIPT_DIR/jsonl_hygiene.py" "$@"
