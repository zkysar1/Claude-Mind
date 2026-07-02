#!/usr/bin/env python3
"""Idempotent provisioner for Lodestar own-cloud AWS resources.

Stands up (or verifies) everything OwnCloudBackend needs on AWS:
  - the S3 bucket (versioning, SSE-S3, public-access-block, lifecycle)
  - the DynamoDB lock + sessions tables (+ optional aspirations table)
  - a scoped least-privilege IAM user + inline policy for ONE env-id

Resources are SHARED across environments (one bucket + one table set); each
environment is isolated by the `<env-id>/` key prefix. Only the IAM user/policy
is per-env (the policy's S3 ARNs + prefix condition are env-id-scoped). So a
second environment reuses the shared resources and only needs its own scoped
user: `provision_aws.py --env-id other-env --user OtherUser --apply`.

RESOURCE NAMES ARE REQUIRED — NO lodestar-* DEFAULT (N4, 2026-06-05). Pass the
bucket + table names via --bucket/--lock-table/--sessions-table OR set the
STORAGE_S3_BUCKET / STORAGE_DDB_LOCK_TABLE / STORAGE_DDB_SESSIONS_TABLE keys in
.env.local. This matches the daemon's hard-required `os.environ[STORAGE_*]`
contract (owncloud_backend.py): an operator who forgets the env vars FAILS FAST
here rather than silently provisioning/granting the OLD lodestar-* resources.
The aspirations table is OPTIONAL (omit --aspirations-table to skip it).

DUAL-GRANT (migration window — N4 refinement b): pass --legacy-bucket /
--legacy-lock-table / --legacy-sessions-table to ALSO grant the old resource set
in the IAM policy. During an AWS resource rename the daemon may be reading
EITHER the old or the new names depending on .env.local flip ordering; granting
BOTH for the whole migration window means no flip-order can lock the daemon out.
Re-run WITHOUT the --legacy-* args at decommission to narrow the policy to the
new resources only.

Spec source of truth: mind_api/docs/lodestar-own-cloud-architecture.md section 3.
Disaster recovery: re-run with --apply; every step is check-then-create.

Admin creds: read from .env.local (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) —
must be an admin/root identity (you are bootstrapping a scoped user). The scoped
keys this can mint (--mint-key) are written to .env.local under MIND_AWS_*; the
secret is NEVER printed (only the non-secret AccessKeyId is shown).

Usage:
  python mind_api/scripts/provision_aws.py                 # dry-run (uses STORAGE_* from .env.local)
  python mind_api/scripts/provision_aws.py --apply         # create/verify resources + user + policy
  python mind_api/scripts/provision_aws.py --apply --mint-key   # also mint scoped keys -> .env.local
  python mind_api/scripts/provision_aws.py --bucket zds-own-cloud-data --lock-table zds-locks \
      --sessions-table zds-sessions --legacy-bucket lodestar-data \
      --legacy-lock-table lodestar-locks --legacy-sessions-table lodestar-sessions \
      --legacy-aspirations-table lodestar-aspirations --apply   # N4 migration dual-grant
"""
import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_LOCAL = REPO_ROOT / ".env.local"


def err_code(e):
    return e.response["Error"]["Code"]


def load_admin_creds(env_local):
    out = {}
    p = Path(env_local)
    if not p.exists():
        sys.exit(f"ERROR: {env_local} not found (need admin AWS_* creds to bootstrap)")
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            out[k.strip()] = (v.strip().split()[0] if v.strip() else "")
    if not out.get("AWS_ACCESS_KEY_ID") or not out.get("AWS_SECRET_ACCESS_KEY"):
        sys.exit("ERROR: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY missing in .env.local")
    return out


def _require(label, arg_val, env, env_key, flag):
    """Resolve a resource name from --arg or .env.local; FAIL FAST if absent.
    No lodestar-* default — an operator who forgets the name must not silently
    provision or grant the OLD resources (N4 refinement a)."""
    v = arg_val or env.get(env_key)
    if not v:
        sys.exit(f"ERROR: {label} not provided. Pass {flag} or set {env_key} in "
                 f".env.local. (No default — matches the daemon's hard-required "
                 f"STORAGE_* contract; refusing to fall back to the old resources.)")
    return v


def build_policy(env_id, account, region, resource_sets):
    """Least-privilege policy for one env-id. `resource_sets` is a list of dicts
    {bucket, lock, sessions, aspirations?}; one statement group is emitted per
    set. Pass TWO sets for a dual-grant migration window (new + legacy). Sids are
    suffixed per set to stay unique. DeleteObject and version-listing are
    intentionally absent (daemon role, not operator)."""
    stmts = []
    for i, rs in enumerate(resource_sets):
        suf = "" if i == 0 else str(i + 1)
        b = rs["bucket"]
        stmts += [
            {"Sid": f"S3ObjectReadWriteEnvScoped{suf}", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:PutObject"],
             "Resource": f"arn:aws:s3:::{b}/{env_id}/*"},
            {"Sid": f"S3BucketListEnvScoped{suf}", "Effect": "Allow",
             "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{b}",
             "Condition": {"StringLike": {"s3:prefix": f"{env_id}/*"}}},
            {"Sid": f"S3BucketVersioningRead{suf}", "Effect": "Allow",
             "Action": "s3:GetBucketVersioning", "Resource": f"arn:aws:s3:::{b}"},
            {"Sid": f"DDBLockTable{suf}", "Effect": "Allow",
             "Action": ["dynamodb:PutItem", "dynamodb:DeleteItem"],
             "Resource": f"arn:aws:dynamodb:{region}:{account}:table/{rs['lock']}"},
            {"Sid": f"DDBSessionsTable{suf}", "Effect": "Allow",
             # Scan is required by list_runner_claims() — the env-scoped fleet
             # ownership enumeration behind OWNERSHIP_MODE=dynamic and the
             # GET /v1/admin/runner-claims fleet-health endpoint (FR-6/FR-7). It
             # was intentionally absent under static single-machine ownership;
             # the dynamic fleet resolver needs it.
             "Action": ["dynamodb:GetItem", "dynamodb:PutItem",
                        "dynamodb:UpdateItem", "dynamodb:Scan"],
             "Resource": f"arn:aws:dynamodb:{region}:{account}:table/{rs['sessions']}"},
        ]
        if rs.get("aspirations"):
            stmts.append(
                {"Sid": f"DDBAspirationsTable{suf}", "Effect": "Allow",
                 "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
                 "Resource": f"arn:aws:dynamodb:{region}:{account}:table/{rs['aspirations']}"})
    return {"Version": "2012-10-17", "Statement": stmts}


def provision_bucket(s3, bucket, apply, log):
    try:
        s3.head_bucket(Bucket=bucket)
        log(f"S3 {bucket}: exists")
    except ClientError as e:
        if err_code(e) in ("404", "NoSuchBucket", "NotFound"):
            log(f"S3 {bucket}: {'CREATE' if apply else 'WOULD CREATE'}")
            if apply:
                kw = {"Bucket": bucket}
                if s3.meta.region_name != "us-east-1":
                    kw["CreateBucketConfiguration"] = {"LocationConstraint": s3.meta.region_name}
                s3.create_bucket(**kw)
        else:
            raise
    if apply:
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
            {"ID": "abort-incomplete-multipart", "Status": "Enabled", "Filter": {"Prefix": ""},
             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
            {"ID": "expire-noncurrent-versions", "Status": "Enabled", "Filter": {"Prefix": ""},
             "NoncurrentVersionExpiration": {"NoncurrentDays": 90}}]})
        log("  applied: public-access-block, versioning, SSE-S3, lifecycle")


def provision_tables(ddb, tables, apply, log):
    for name, keyspec, ttl_attr in tables:
        try:
            ddb.describe_table(TableName=name)
            log(f"DDB {name}: exists")
        except ClientError as e:
            if err_code(e) == "ResourceNotFoundException":
                log(f"DDB {name}: {'CREATE' if apply else 'WOULD CREATE'}")
                if apply:
                    ddb.create_table(
                        TableName=name, BillingMode="PAY_PER_REQUEST",
                        KeySchema=[{"AttributeName": a, "KeyType": k} for a, k in keyspec],
                        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a, _ in keyspec],
                        Tags=[{"Key": "project", "Value": "lodestar"}])
                    ddb.get_waiter("table_exists").wait(TableName=name)
            else:
                raise
        if ttl_attr and apply:
            cur = ddb.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
            if cur.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING"):
                ddb.update_time_to_live(TableName=name, TimeToLiveSpecification={
                    "Enabled": True, "AttributeName": ttl_attr})
                log(f"  {name}: TTL enabled on '{ttl_attr}' (GC only)")


def provision_user(iam, user, env_id, account, region, resource_sets, apply, log):
    try:
        iam.get_user(UserName=user)
        log(f"IAM user {user}: exists")
    except ClientError as e:
        if err_code(e) == "NoSuchEntity":
            log(f"IAM user {user}: {'CREATE' if apply else 'WOULD CREATE'}")
            if apply:
                iam.create_user(UserName=user, Tags=[
                    {"Key": "project", "Value": "lodestar"},
                    {"Key": "env-id", "Value": env_id}])
        else:
            raise
    pol = build_policy(env_id, account, region, resource_sets)
    log(f"IAM inline policy 'lodestar-own-cloud' (env-id={env_id}, "
        f"{len(resource_sets)} resource set(s)): "
        f"{'ATTACH' if apply else 'WOULD ATTACH'} ({len(pol['Statement'])} statements)")
    if apply:
        iam.put_user_policy(UserName=user, PolicyName="lodestar-own-cloud",
                            PolicyDocument=json.dumps(pol))


def mint_key(iam, user, env_local, log):
    """Mint a fresh scoped access key, write MIND_AWS_* to .env.local. Deletes any
    prior keys first (their secrets are unrecoverable). Secret is written to file
    only; never printed."""
    for k in iam.list_access_keys(UserName=user)["AccessKeyMetadata"]:
        iam.delete_access_key(UserName=user, AccessKeyId=k["AccessKeyId"])
        log(f"  deleted prior key {k['AccessKeyId']}")
    nk = iam.create_access_key(UserName=user)["AccessKey"]
    akid, secret = nk["AccessKeyId"], nk["SecretAccessKey"]
    updates = {"MIND_AWS_ACCESS_KEY_ID": akid, "MIND_AWS_SECRET_ACCESS_KEY": secret}
    p = Path(env_local)
    lines = p.read_text(encoding="utf-8").splitlines()
    seen = set()
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if ("=" in ln and not ln.strip().startswith("#")) else None
        if key in updates:
            out.append(f"{key}={updates[key]}"); seen.add(key)
        else:
            out.append(ln)
    for k in (m for m in updates if m not in seen):
        out.append(f"{k}={updates[k]}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    log(f"  minted AccessKeyId={akid}; secret written to {env_local} (MIND_AWS_*), not shown")


def main():
    ap = argparse.ArgumentParser(description="Provision Lodestar own-cloud AWS resources (idempotent).")
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run plan)")
    ap.add_argument("--env-id", default="ayoai-mind", help="environment id (key prefix); default ayoai-mind")
    ap.add_argument("--user", default="Zak_first_test", help="scoped IAM user name")
    ap.add_argument("--region", default="us-east-2")
    ap.add_argument("--bucket", default=None, help="S3 bucket (or STORAGE_S3_BUCKET in .env.local). REQUIRED — no default.")
    ap.add_argument("--lock-table", default=None, help="DDB lock table (or STORAGE_DDB_LOCK_TABLE). REQUIRED.")
    ap.add_argument("--sessions-table", default=None, help="DDB sessions table (or STORAGE_DDB_SESSIONS_TABLE). REQUIRED.")
    ap.add_argument("--aspirations-table", default=None, help="DDB aspirations table (OPTIONAL — omit to skip provisioning it)")
    ap.add_argument("--legacy-bucket", default=None, help="dual-grant: also grant this OLD bucket in the IAM policy (migration window)")
    ap.add_argument("--legacy-lock-table", default=None, help="dual-grant: also grant this OLD lock table")
    ap.add_argument("--legacy-sessions-table", default=None, help="dual-grant: also grant this OLD sessions table")
    ap.add_argument("--legacy-aspirations-table", default=None, help="dual-grant: also grant this OLD aspirations table")
    ap.add_argument("--mint-key", action="store_true", help="mint scoped access key -> .env.local MIND_AWS_* (requires --apply)")
    ap.add_argument("--env-local", default=str(DEFAULT_ENV_LOCAL))
    args = ap.parse_args()
    if args.mint_key and not args.apply:
        sys.exit("--mint-key requires --apply")

    creds = load_admin_creds(args.env_local)

    # Resolve resource names: --arg, else .env.local STORAGE_*, else FAIL FAST.
    bucket = _require("S3 bucket", args.bucket, creds, "STORAGE_S3_BUCKET", "--bucket")
    lock_table = _require("DDB lock table", args.lock_table, creds, "STORAGE_DDB_LOCK_TABLE", "--lock-table")
    sessions_table = _require("DDB sessions table", args.sessions_table, creds, "STORAGE_DDB_SESSIONS_TABLE", "--sessions-table")
    aspirations_table = args.aspirations_table  # optional; no env fallback, no default

    tables = [
        (lock_table, [("lock_key", "HASH")], "ttl"),       # TTL attr (GC only)
        (sessions_table, [("session_key", "HASH")], None),
    ]
    if aspirations_table:
        tables.append((aspirations_table, [("pk", "HASH"), ("sk", "RANGE")], None))

    # IAM resource sets: the primary (new) set, plus an optional legacy set for a
    # dual-grant migration window.
    primary = {"bucket": bucket, "lock": lock_table, "sessions": sessions_table}
    if aspirations_table:
        primary["aspirations"] = aspirations_table
    resource_sets = [primary]
    if args.legacy_bucket or args.legacy_lock_table or args.legacy_sessions_table or args.legacy_aspirations_table:
        legacy = {
            "bucket": args.legacy_bucket or bucket,
            "lock": args.legacy_lock_table or lock_table,
            "sessions": args.legacy_sessions_table or sessions_table,
        }
        if args.legacy_aspirations_table:
            legacy["aspirations"] = args.legacy_aspirations_table
        resource_sets.append(legacy)

    sess = boto3.Session(aws_access_key_id=creds["AWS_ACCESS_KEY_ID"],
                         aws_secret_access_key=creds["AWS_SECRET_ACCESS_KEY"],
                         region_name=args.region)
    account = sess.client("sts").get_caller_identity()["Account"]
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Lodestar provision [{mode}]  account={account}  region={args.region}  "
          f"env-id={args.env_id}  user={args.user} ===")
    print(f"  bucket={bucket}  lock={lock_table}  sessions={sessions_table}  "
          f"aspirations={aspirations_table or '(skipped)'}")
    if len(resource_sets) > 1:
        print(f"  DUAL-GRANT migration window: IAM policy also grants legacy set "
              f"{resource_sets[1]} (re-run without --legacy-* at decommission to narrow)")

    def log(m):
        print(m)

    provision_bucket(sess.client("s3", region_name=args.region), bucket, args.apply, log)
    provision_tables(sess.client("dynamodb", region_name=args.region), tables, args.apply, log)
    provision_user(sess.client("iam"), args.user, args.env_id, account, args.region, resource_sets, args.apply, log)
    if args.mint_key:
        mint_key(sess.client("iam"), args.user, args.env_local, log)

    print(f"\n{'PROVISION APPLIED' if args.apply else 'DRY-RUN complete (re-run with --apply to execute)'}")
    if not args.apply:
        print("STORAGE_BACKEND stays 'local' regardless — the cutover to own-cloud "
              "(backfill + flip) is a separate, deliberate step (s6/s7).")


if __name__ == "__main__":
    main()
