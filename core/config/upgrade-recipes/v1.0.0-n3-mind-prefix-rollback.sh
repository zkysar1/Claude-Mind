#!/usr/bin/env bash
# rollback-recipe: v1.0.0  — undo the N3 MIND_ de-overload
#
#   rollback-for:  v1.0.0
#   cross_world:   false   # framework-only: restore .env.local KEYS + git-revert code
#
# Reverses the v1.0.0 N3 cutover on ONE machine: restores the pre-N3 .env.local
# (old MIND_ key names) and points the operator at the code revert. Because N3 is
# framework-only (no external world/+meta/ DATA migration), the code half rolls
# back with a plain git revert/checkout — there is NO world/meta snapshot to
# restore (that is the cross_world case, which N3 is not).
#
# IDEMPOTENT: re-running on already-rolled-back state is a no-op — restoring from
# the .bak is value-preserving, and the new->old key reversal has no `^NEW=` line
# left to match once already reverted. Safe to run repeatedly.
#
# Run with all agents IDLE, BEFORE restarting the daemon.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/../../scripts/_paths.sh"

ENV_LOCAL="$PROJECT_ROOT/.env.local"
BAK="$ENV_LOCAL.pre-v1.0.0-n3.bak"

# --- Pre-check (idempotent — safe to re-run) ---
for sf in "$(agents_root)"/*/session/agent-state; do
  [[ -f "$sf" && "$(cat "$sf" 2>/dev/null)" == "RUNNING" ]] && {
    echo "ERROR: an agent is RUNNING — stop all agents before rolling back N3" >&2; exit 1; }
done

# --- Steps ---
# 1) Restore .env.local from the pre-N3 backup (the clean, value-preserving path).
if [[ -f "$BAK" ]]; then
  cp "$BAK" "$ENV_LOCAL" || { echo "ERROR: could not restore .env.local from $BAK" >&2; exit 1; }
  echo "[v1.0.0-n3-rollback] restored .env.local from $BAK"
elif [[ -f "$ENV_LOCAL" ]]; then
  # No backup (migration may have been hand-applied) — reverse the rename new->old.
  for pair in \
    "ENVIRONMENT_ID=MIND_ENV_ID" "STORAGE_BACKEND=MIND_STORAGE_BACKEND" \
    "STORAGE_S3_BUCKET=MIND_S3_BUCKET" "STORAGE_DDB_LOCK_TABLE=MIND_DDB_LOCK_TABLE" \
    "STORAGE_DDB_SESSIONS_TABLE=MIND_DDB_SESSIONS_TABLE" "MACHINE_ID=MIND_MACHINE_ID" \
    "MACHINE_OWNED_AGENTS=MIND_OWNED_AGENTS" "MACHINE_MULTI=MIND_MULTI_MACHINE" \
    "OWNCLOUD_CACHE_TTL=MIND_CLOUD_CACHE_TTL" "OWNCLOUD_SYNC_DISABLE=MIND_OWNCLOUD_SYNC_DISABLE" \
    "OWNCLOUD_SYNC_INTERVAL=MIND_OWNCLOUD_SYNC_INTERVAL" "RUNTIME_DIR=MIND_RUNTIME_DIR"; do
    new="${pair%%=*}"; old="${pair#*=}"
    sed -i "s/^${new}=/${old}=/" "$ENV_LOCAL"
  done
  echo "[v1.0.0-n3-rollback] no backup found — reversed key renames in $ENV_LOCAL"
else
  echo "[v1.0.0-n3-rollback] no .env.local present — nothing to restore"
fi

# 2) Code revert (operator action — the agent never force-reverts a tagged release):
echo
echo "[v1.0.0-n3-rollback] NEXT — revert the code half (choose one), then restart the daemon:"
echo "    git revert --no-edit <v1.0.0 release commit>      # preferred: keeps history"
echo "    # or, to hard-reset working tree to the prior tag for the renamed files:"
echo "    git checkout v0.2.0 -- mind_api core/scripts .claude .env.example CLAUDE.md"
echo "    bash core/scripts/mind-api-start.sh --restart"

# --- Post-check ---
if [[ -f "$ENV_LOCAL" ]] && grep -qE '^MIND_STORAGE_BACKEND=' "$ENV_LOCAL"; then
  echo "[v1.0.0-n3-rollback] post-check OK: old MIND_STORAGE_BACKEND key restored in .env.local."
fi
echo "Rollback of v1.0.0 (N3) .env.local complete (code revert is the operator step above)."
