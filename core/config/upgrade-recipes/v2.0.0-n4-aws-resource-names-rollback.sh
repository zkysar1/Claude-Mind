#!/usr/bin/env bash
# rollback-recipe for v2.0.0 (N4) — reverses v2.0.0-n4-aws-resource-names.sh in
# REVERSE order. Returns the daemon to the lodestar-* resources (RETAINED through
# the 7-day soak) and restores $WORLD_PATH/$META_PATH from the upgrade snapshot.
#
# CONTRACT (validated STRUCTURALLY by release.sh; NEVER executed at cut). A
# rollback MUST:
#   1. Be idempotent — re-running on already-rolled-back state is a no-op success.
#   2. Have a Pre-check — verify the current state before acting.
#   3. Reverse the upgrade's steps in reverse order.
#   4. (cross_world — H3b) RESTORE $WORLD_PATH + $META_PATH from the upgrade
#      snapshot in EXECUTABLE code (a comment promising a restore is not one).
#   5. Have a Post-check — verify the OLD state is restored.
#
# WHAT ROLLBACK DOES: flips .env.local VALUES zds-* -> lodestar-*, restores the
# local world/+meta/ cache from the upgrade snapshot (SNAP_DIR), and restarts the
# daemon onto the retained lodestar-* resources. Any work written to zds-* during
# the soak is intentionally abandoned — rollback returns to the pre-migration
# state. The zds-* resources are LEFT in place (delete them separately if
# abandoning N4). The dual-grant IAM is harmless and left as-is.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/../../scripts/_paths.sh"

ENV_LOCAL="$PROJECT_ROOT/.env.local"
OLD_BUCKET="lodestar-data";       NEW_BUCKET="zds-own-cloud-data"
OLD_LOCKS="lodestar-locks";       NEW_LOCKS="zds-locks"
OLD_SESSIONS="lodestar-sessions"; NEW_SESSIONS="zds-sessions"

# --- Pre-check ---
if [[ ! -f "$ENV_LOCAL" ]]; then
  echo "[v2.0.0-n4 rollback] no .env.local — nothing to roll back (idempotent no-op)."; exit 0
fi
# All agents MUST be IDLE before a daemon-affecting rollback.
for sf in "$(agents_root)"/*/session/agent-state; do
  [[ -f "$sf" && "$(cat "$sf" 2>/dev/null)" == "RUNNING" ]] && {
    echo "ERROR: an agent is RUNNING — stop ALL agents before rolling back" >&2; exit 1; }
done

# --- Steps (reverse order of the upgrade) ---
# Reverse step 4: flip .env.local VALUES zds-* -> lodestar-*, UNCONDITIONALLY and for
# ALL THREE storage vars BEFORE any short-circuit. The seds are idempotent (a line
# already on lodestar- has no zds- value to match; \b tolerates a trailing CR), so
# running them unconditionally self-heals a PARTIAL rollback — e.g. a crash that
# reverted the bucket but not the DDB tables. (adversarial finding: the prior
# bucket-only early-exit short-circuited BEFORE the DDB seds and left a lodestar-data
# S3 + zds-locks/sessions DDB split-brain on a rollback re-run.)
sed -i "s|^\(STORAGE_S3_BUCKET=\)${NEW_BUCKET}\b|\1${OLD_BUCKET}|" "$ENV_LOCAL"
sed -i "s|^\(STORAGE_DDB_LOCK_TABLE=\)${NEW_LOCKS}\b|\1${OLD_LOCKS}|" "$ENV_LOCAL"
sed -i "s|^\(STORAGE_DDB_SESSIONS_TABLE=\)${NEW_SESSIONS}\b|\1${OLD_SESSIONS}|" "$ENV_LOCAL"
echo "[v2.0.0-n4 rollback] flipped .env.local storage values back to lodestar-* (all 3 vars)"

# Pure-no-op fast path: if (AFTER the idempotent seds) all three vars are already on
# lodestar-* AND no snapshot restore was requested, there is nothing left to do —
# skip the world/meta restore and the daemon restart. This preserves the original
# no-op-on-re-run behavior WITHOUT the pre-sed short-circuit that caused the split-brain.
if [[ -z "${SNAP_DIR:-}" ]] \
   && grep -qE "^STORAGE_S3_BUCKET=${OLD_BUCKET}\b" "$ENV_LOCAL" \
   && grep -qE "^STORAGE_DDB_LOCK_TABLE=${OLD_LOCKS}\b" "$ENV_LOCAL" \
   && grep -qE "^STORAGE_DDB_SESSIONS_TABLE=${OLD_SESSIONS}\b" "$ENV_LOCAL"; then
  echo "[v2.0.0-n4 rollback] already fully on lodestar-* and no SNAP_DIR — nothing to restore/restart (idempotent no-op)."
  exit 0
fi

# --- Restore world/+meta/ from the upgrade snapshot (cross_world — H3b) ---
# The upgrade printed its SNAP_DIR; pass it back via env (SNAP_DIR=...). A git-tag
# rollback CANNOT restore these external paths, so this snapshot copy is the ONLY
# path. Idempotent: if SNAP_DIR is unset or the snapshot is absent, the restore is
# skipped (the .env.local reversal above + the retained lodestar-* S3 store still
# return the daemon to the pre-migration data).
if [[ -n "${SNAP_DIR:-}" && -d "$SNAP_DIR/world" && -d "$SNAP_DIR/meta" ]]; then
  cp -r "$SNAP_DIR/world/." "$WORLD_PATH/" || { echo "ERROR: world restore from snapshot failed" >&2; exit 1; }
  cp -r "$SNAP_DIR/meta/."  "$META_PATH/"  || { echo "ERROR: meta restore from snapshot failed" >&2; exit 1; }
  echo "[v2.0.0-n4 rollback] restored world/+meta/ from snapshot $SNAP_DIR"
else
  echo "NOTE: SNAP_DIR unset or snapshot absent — world/+meta/ NOT restored from snapshot (the retained lodestar-* S3 store still holds the pre-migration data; the local cache re-hydrates on daemon read)." >&2
fi

# Restart the daemon onto the lodestar-* resources.
bash "$PROJECT_ROOT/core/scripts/mind-api-start.sh" --restart || {
  echo "WARNING: daemon restart returned non-zero — verify manually" >&2; }

# --- Post-check (symmetric with the forward recipe: verify ALL THREE storage vars) ---
grep -qE "^STORAGE_S3_BUCKET=${OLD_BUCKET}\b" "$ENV_LOCAL" || { echo "ERROR: post-check: STORAGE_S3_BUCKET not restored to ${OLD_BUCKET}" >&2; exit 1; }
grep -qE "^STORAGE_DDB_LOCK_TABLE=${OLD_LOCKS}\b" "$ENV_LOCAL" || { echo "ERROR: post-check: lock table not restored to ${OLD_LOCKS}" >&2; exit 1; }
grep -qE "^STORAGE_DDB_SESSIONS_TABLE=${OLD_SESSIONS}\b" "$ENV_LOCAL" || { echo "ERROR: post-check: sessions table not restored to ${OLD_SESSIONS}" >&2; exit 1; }
if grep -qE "^STORAGE_S3_BUCKET=${NEW_BUCKET}\b" "$ENV_LOCAL"; then echo "ERROR: post-check: zds- bucket value still present after rollback" >&2; exit 1; fi
echo "[v2.0.0-n4 rollback] post-check OK: .env.local back on lodestar-* (all 3 vars); daemon restarted."
echo "Rollback to v1.0.0 (pre-N4 resource names) complete."
