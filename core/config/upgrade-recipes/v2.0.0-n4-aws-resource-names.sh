#!/usr/bin/env bash
# domain-leak-exempt: executable cross-world migration recipe. boto3/DynamoDB and the
# lodestar-*/zds-* AWS resource names are FUNCTIONAL here (the script imports boto3 and
# operates on these named resources) — not pedagogical examples. This is the marker's
# sanctioned use per domain-free-examples.md "Marker Restriction" (executable code with
# functional domain strings). The recipe must travel cross-world into the seed so a
# downstream world can run its own migration; genericizing operator-facing migration
# code would harm the human who executes the live data move. NOTE for the rails owner:
# N4 is the FIRST domain-touching recipe — see the gate-2 design note on whether
# domain-specific recipes should be marker-exempt-and-travel vs stripped on promote.
# upgrade-recipe: v2.0.0  — N4 rename of the own-cloud AWS resources lodestar-* -> zds-*
#
#   upgrade-recipe: v2.0.0
#   min_source:     1.0.0
#   breaking:       true
#   cross_world:    true       # the S3 bucket holds ALL world/+meta/ state; renaming it is a live data migration
#   rollback:       core/config/upgrade-recipes/v2.0.0-n4-aws-resource-names-rollback.sh
#
# WHAT N4 DOES (omni + Zachary sign-off 2026-06-05): renames the four own-cloud
# AWS resources THIS environment (env-id ayoai-mind) uses, from lodestar-* to the
# operator-prefixed zds-*:
#   s3://lodestar-data       -> s3://zds-own-cloud-data        (holds ALL world/+meta/+agents state under ayoai-mind/)
#   ddb  lodestar-locks      -> zds-locks
#   ddb  lodestar-sessions   -> zds-sessions
#   ddb  lodestar-aspirations -> NOT mirrored (decision 2 — provisioned-but-unused; decommissioned with the rest)
# The PUBLIC-COMMONS resources (lodestar-accounts / lodestar-analytics) are NOT
# touched — those correctly keep the lodestar- brand prefix. The three live
# web-app repos hold ZERO references to lodestar-data/locks/sessions (omni
# verified), so this rename is invisible to the public sites.
#
# WHY cross_world:true — the daemon reads resource NAMES from os.environ ONLY
# (owncloud_backend.py:213-215, hard-required, no default), so the daemon CODE
# needs no change; the weight is the LIVE S3 + DynamoDB DATA that must be copied
# and the per-machine .env.local VALUES that must be flipped. A git-tag rollback
# cannot restore that external state, so this recipe snapshots $WORLD_PATH +
# $META_PATH FIRST (H3b) — that snapshot is the only rollback path for them.
#
# DEPLOY SEQUENCE (decision 3): stop-all-agents -> snapshot -> create zds-* +
# DUAL-GRANT IAM -> copy S3 (VERIFY object counts BEFORE the flip) -> copy DDB ->
# flip .env.local -> restart daemon -> post-check. The DUAL-GRANT IAM window
# (refinement b) grants BOTH lodestar-* and zds-* for the whole migration, so
# neither the .env.local flip order nor the IAM update order can lock the daemon
# out. Old lodestar-* resources are RETAINED for a 7-day rollback soak, then
# decommissioned separately (decision 6) AFTER the offline tools point at zds-*.
#
# CREDS: the one-time copy + provisioning use the ADMIN AWS_* identity from
# .env.local (same as provision_aws.py) — an operator action, NOT the daemon's
# scoped MIND_AWS_* role. Secrets are read into the environment, never printed.
#
# CONTRACT (validated STRUCTURALLY by release.sh at cut; NEVER executed at
# release-cut — Zachary clears the live AWS data move as a SEPARATE checkpoint).
# This recipe is idempotent: a re-run after a successful (or partial) migration
# resumes safely.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/../../scripts/_paths.sh"

ENV_LOCAL="$PROJECT_ROOT/.env.local"
PY="python3"; command -v python3 >/dev/null 2>&1 || PY="py -3"

OLD_BUCKET="lodestar-data";        NEW_BUCKET="zds-own-cloud-data"
OLD_LOCKS="lodestar-locks";        NEW_LOCKS="zds-locks"
OLD_SESSIONS="lodestar-sessions";  NEW_SESSIONS="zds-sessions"
OLD_ASPIRATIONS="lodestar-aspirations"   # legacy IAM grant only; NOT mirrored to zds-*
ENV_ID="$(grep -E '^ENVIRONMENT_ID=' "$ENV_LOCAL" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
ENV_ID="${ENV_ID:-ayoai-mind}"

# --- Pre-check ---
[[ -f "$ENV_LOCAL" ]] || { echo "ERROR: no .env.local at $ENV_LOCAL — own-cloud not configured; nothing to migrate" >&2; exit 1; }
# Idempotency short-circuit: a no-op ONLY if ALL THREE storage vars are already on
# zds-*. A PARTIAL prior run (e.g. a crash between the sequential seds — bucket
# flipped but a DDB table not) must NOT short-circuit; it falls through and re-runs
# the idempotent steps below to self-heal. (adversarial finding: a single-var gate
# left a bucket=zds + tables=lodestar split-brain undetected.)
if grep -qE "^STORAGE_S3_BUCKET=${NEW_BUCKET}\b" "$ENV_LOCAL" \
   && grep -qE "^STORAGE_DDB_LOCK_TABLE=${NEW_LOCKS}\b" "$ENV_LOCAL" \
   && grep -qE "^STORAGE_DDB_SESSIONS_TABLE=${NEW_SESSIONS}\b" "$ENV_LOCAL"; then
  echo "[v2.0.0-n4] .env.local already on zds-* (all 3 storage vars) — migration already applied (idempotent no-op)."
  exit 0
fi
# All agents MUST be IDLE — a daemon restart mid-copy would split-brain old/new.
for sf in "$(agents_root)"/*/session/agent-state; do
  [[ -f "$sf" && "$(cat "$sf" 2>/dev/null)" == "RUNNING" ]] && {
    echo "ERROR: an agent is RUNNING — stop ALL agents before the N4 AWS migration" >&2; exit 1; }
done
# FROM-state guard: the bucket must be EITHER lodestar-data (clean start) OR zds-own-cloud-data
# (partial resume after a crash between the seds). Anything else is an unknown state.
# (Without the zds-own-cloud-data case a partial-resume — which the widened idempotency gate
# above deliberately lets fall through — would be wrongly rejected here.)
CUR_BUCKET="$(grep -E '^STORAGE_S3_BUCKET=' "$ENV_LOCAL" | head -1 | cut -d= -f2 | tr -d '[:space:]')"
case "$CUR_BUCKET" in
  "$OLD_BUCKET"|"$NEW_BUCKET") : ;;
  *) echo "ERROR: STORAGE_S3_BUCKET='$CUR_BUCKET' is neither ${OLD_BUCKET} (clean) nor ${NEW_BUCKET} (partial resume); refusing to migrate from an unknown state" >&2; exit 1 ;;
esac
# Admin creds for the one-time copy/provision (operator identity, not the daemon role).
AWS_ACCESS_KEY_ID="$(grep -E '^AWS_ACCESS_KEY_ID=' "$ENV_LOCAL" | head -1 | cut -d= -f2 | tr -d '[:space:]')"
AWS_SECRET_ACCESS_KEY="$(grep -E '^AWS_SECRET_ACCESS_KEY=' "$ENV_LOCAL" | head -1 | cut -d= -f2 | tr -d '[:space:]')"
AWS_DEFAULT_REGION="$(grep -E '^AWS_DEFAULT_REGION=' "$ENV_LOCAL" | head -1 | cut -d= -f2 | tr -d '[:space:]')"
AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-2}"
[[ -n "$AWS_ACCESS_KEY_ID" && -n "$AWS_SECRET_ACCESS_KEY" ]] || {
  echo "ERROR: admin AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY missing in .env.local (needed for the one-time copy)" >&2; exit 1; }
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION

# --- World/meta snapshot (cross_world — H3b) ---
# A git-tag rollback CANNOT restore $WORLD_PATH/$META_PATH; this copy-snapshot is
# the ONLY rollback path for them. A FAILED snapshot MUST abort BEFORE any AWS
# mutation (a half snapshot + a corrupting migration = irrecoverable). Pass
# SNAP_DIR back to the rollback recipe to restore.
SNAP_DIR="${SNAP_DIR_OVERRIDE:-$WORLD_PATH/../upgrade-snapshots/v2.0.0-n4-$(date +%Y%m%dT%H%M%S)}"
mkdir -p "$SNAP_DIR" || { echo "ERROR: cannot create snapshot dir $SNAP_DIR — aborting before migration" >&2; exit 1; }
cp -r "$WORLD_PATH" "$SNAP_DIR/world" || { echo "ERROR: world snapshot failed — aborting before migration" >&2; exit 1; }
cp -r "$META_PATH"  "$SNAP_DIR/meta"  || { echo "ERROR: meta snapshot failed — aborting before migration" >&2; exit 1; }
echo "[v2.0.0-n4] snapshot: $SNAP_DIR  (pass SNAP_DIR=$SNAP_DIR to the rollback recipe to restore world/+meta/)"

# --- Steps ---
# 1) Create zds-* resources + DUAL-GRANT IAM (grants BOTH old+new for the window).
echo "[v2.0.0-n4] creating zds-* resources + dual-grant IAM ..."
$PY "$PROJECT_ROOT/mind_api/scripts/provision_aws.py" \
  --bucket "$NEW_BUCKET" --lock-table "$NEW_LOCKS" --sessions-table "$NEW_SESSIONS" \
  --legacy-bucket "$OLD_BUCKET" --legacy-lock-table "$OLD_LOCKS" \
  --legacy-sessions-table "$OLD_SESSIONS" --legacy-aspirations-table "$OLD_ASPIRATIONS" \
  --env-id "$ENV_ID" --region "$AWS_DEFAULT_REGION" --apply

# 2) Copy all S3 objects old -> new, then VERIFY object counts match BEFORE the
#    flip (decision 3). Abort the migration if counts diverge — never flip onto
#    an incomplete copy. The sync itself is idempotent (skips identical objects).
echo "[v2.0.0-n4] copying s3://$OLD_BUCKET/$ENV_ID/ -> s3://$NEW_BUCKET/$ENV_ID/ ..."
aws s3 sync "s3://$OLD_BUCKET/$ENV_ID/" "s3://$NEW_BUCKET/$ENV_ID/" --only-show-errors
OLD_N="$(aws s3 ls "s3://$OLD_BUCKET/$ENV_ID/" --recursive | grep -c . || true)"
NEW_N="$(aws s3 ls "s3://$NEW_BUCKET/$ENV_ID/" --recursive | grep -c . || true)"
echo "[v2.0.0-n4] S3 object counts: old=$OLD_N new=$NEW_N"
[[ "$OLD_N" == "$NEW_N" && "$NEW_N" -gt 0 ]] || {
  echo "ERROR: S3 object-count mismatch (old=$OLD_N new=$NEW_N) — copy incomplete; NOT flipping .env.local. Re-run to retry the idempotent sync." >&2; exit 1; }

# 3) Copy DynamoDB items old -> new (sessions carry live state; locks are empty
#    with agents stopped but copied for safety). Idempotent (PutItem overwrites).
#    Values passed via env — the python source is a quoted heredoc, never
#    interpolated, per guard-165.
echo "[v2.0.0-n4] copying DynamoDB items ..."
for pair in "$OLD_SESSIONS=$NEW_SESSIONS" "$OLD_LOCKS=$NEW_LOCKS"; do
  DDB_SRC="${pair%%=*}" DDB_DST="${pair#*=}" $PY - <<'PY'
import os, sys, boto3
src = os.environ["DDB_SRC"]; dst = os.environ["DDB_DST"]
ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
t_src = ddb.Table(src); t_dst = ddb.Table(dst)
n = 0; kw = {}
while True:
    r = t_src.scan(**kw)
    with t_dst.batch_writer() as bw:
        for it in r.get("Items", []):
            bw.put_item(Item=it); n += 1
    lek = r.get("LastEvaluatedKey")
    if not lek:
        break
    kw = {"ExclusiveStartKey": lek}
# Verify dst >= src item count (count-preserving copy; mirrors the S3 count gate so
# the DDB copy gets the same incompleteness guard). Strongly-consistent COUNT scans
# avoid an eventual-consistency false negative right after the batch writes.
def _count(t):
    c = 0; ck = {"Select": "COUNT", "ConsistentRead": True}
    while True:
        rr = t.scan(**ck); c += rr.get("Count", 0)
        lek2 = rr.get("LastEvaluatedKey")
        if not lek2:
            return c
        ck = {"Select": "COUNT", "ConsistentRead": True, "ExclusiveStartKey": lek2}
src_c = _count(t_src); dst_c = _count(t_dst)
print(f"  copied {n} items {src} -> {dst}  (verify: src={src_c} dst={dst_c})")
if dst_c < src_c:
    sys.exit(f"ERROR: DDB copy incomplete {src} -> {dst}: dst={dst_c} < src={src_c}")
PY
done

# 4) Flip .env.local VALUES old -> new (anchored at the key; \b prevents matching
#    a longer value and tolerates a trailing CR/comment). Idempotent — a line
#    already on zds- has no lodestar- value to match. The DUAL-GRANT IAM means
#    the daemon retains access regardless of this flip's ordering.
BAK="$ENV_LOCAL.pre-v2.0.0-n4.bak"
[[ -f "$BAK" ]] || cp "$ENV_LOCAL" "$BAK"
sed -i "s|^\(STORAGE_S3_BUCKET=\)${OLD_BUCKET}\b|\1${NEW_BUCKET}|" "$ENV_LOCAL"
sed -i "s|^\(STORAGE_DDB_LOCK_TABLE=\)${OLD_LOCKS}\b|\1${NEW_LOCKS}|" "$ENV_LOCAL"
sed -i "s|^\(STORAGE_DDB_SESSIONS_TABLE=\)${OLD_SESSIONS}\b|\1${NEW_SESSIONS}|" "$ENV_LOCAL"
echo "[v2.0.0-n4] flipped .env.local storage values -> zds-* (backup: $BAK)"

# 5) Restart the daemon onto the new resource names.
bash "$PROJECT_ROOT/core/scripts/mind-api-start.sh" --restart || {
  echo "WARNING: daemon restart returned non-zero — verify manually" >&2; }

# --- Post-check ---
grep -qE "^STORAGE_S3_BUCKET=${NEW_BUCKET}\b" "$ENV_LOCAL" || { echo "ERROR: post-check: STORAGE_S3_BUCKET not on ${NEW_BUCKET}" >&2; exit 1; }
grep -qE "^STORAGE_DDB_LOCK_TABLE=${NEW_LOCKS}\b" "$ENV_LOCAL" || { echo "ERROR: post-check: lock table not flipped to ${NEW_LOCKS}" >&2; exit 1; }
grep -qE "^STORAGE_DDB_SESSIONS_TABLE=${NEW_SESSIONS}\b" "$ENV_LOCAL" || { echo "ERROR: post-check: sessions table not flipped to ${NEW_SESSIONS}" >&2; exit 1; }
if grep -qE "^STORAGE_S3_BUCKET=${OLD_BUCKET}\b" "$ENV_LOCAL"; then echo "ERROR: post-check: OLD bucket value still present after flip" >&2; exit 1; fi
echo "[v2.0.0-n4] post-check OK: .env.local on zds-*; daemon restarted onto the new resources."
echo "[v2.0.0-n4] Old lodestar-* resources RETAINED for the 7-day rollback soak."
echo "            Decommission separately (decision 6) AFTER the offline tools default to zds-*:"
echo "              - re-run provision_aws.py WITHOUT --legacy-* to narrow the IAM policy to zds-* only"
echo "              - then delete the lodestar-data bucket + lodestar-locks/sessions/aspirations tables"
echo "            Rollback: SNAP_DIR=$SNAP_DIR bash $(dirname "$0")/v2.0.0-n4-aws-resource-names-rollback.sh"
echo "Upgrade to v2.0.0 (N4) complete."
