"""AC6 (BRD -Backed Shared-State & Coordination API): the in-repo
idempotent AWS provisioner stands up the S3 bucket + DDB lock/sessions tables
from an empty account, and its scoped IAM policy is env-id-scoped and grants the
dynamodb:Scan that list_runner_claims / the FR-7 fleet-health endpoint need.

All AWS calls run under moto (mock_aws) — no real AWS is touched. The provisioner
lives at mind_api/scripts/provision_aws.py (a script, not a package module), so it
is loaded by path via importlib.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Optional deps: on boxes without the AWS test stack, skip this module instead
# of interrupting collection for the entire mind_api suite (a bare
# ModuleNotFoundError at import time aborts pytest collection wholesale).
boto3 = pytest.importorskip("boto3", reason="boto3 not installed")
moto = pytest.importorskip("moto", reason="moto (AWS mock) not installed")
from botocore.exceptions import ClientError
from moto import mock_aws

REGION = "us-east-2"

# Load the non-package provisioner script by path.
_PROV_PATH = Path(__file__).resolve().parents[1] / "scripts" / "provision_aws.py"
_spec = importlib.util.spec_from_file_location("provision_aws", _PROV_PATH)
provision_aws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision_aws)


def _sink():
    """A log() callable that swallows output (the provisioner logs progress)."""
    return lambda _m: None


def test_provision_creates_bucket_and_tables_and_is_idempotent():
    """From an empty account: --apply creates the bucket + both tables with the
    correct partition keys; a second --apply is a no-op that does not error."""
    log = _sink()
    tables = [
        ("fleet-locks", [("lock_key", "HASH")], "ttl"),
        ("fleet-sessions", [("session_key", "HASH")], None),
    ]
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)

        provision_aws.provision_bucket(s3, "fleet-own-cloud-data", True, log)
        provision_aws.provision_tables(ddb, tables, True, log)

        # bucket exists
        s3.head_bucket(Bucket="fleet-own-cloud-data")
        # tables exist with the expected PK
        for name, pk in (("fleet-locks", "lock_key"),
                         ("fleet-sessions", "session_key")):
            desc = ddb.describe_table(TableName=name)["Table"]
            assert desc["KeySchema"][0]["AttributeName"] == pk
        # lock table has TTL enabled on `ttl`
        ttl = ddb.describe_time_to_live(TableName="fleet-locks")["TimeToLiveDescription"]
        assert ttl["TimeToLiveStatus"] in ("ENABLED", "ENABLING")
        assert ttl.get("AttributeName") == "ttl"

        # idempotent: a second apply re-verifies without raising
        provision_aws.provision_bucket(s3, "fleet-own-cloud-data", True, log)
        provision_aws.provision_tables(ddb, tables, True, log)


def test_provision_dry_run_creates_nothing():
    """Dry-run (apply=False) plans but creates no resources."""
    log = _sink()
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        provision_aws.provision_bucket(s3, "fleet-dry", False, log)
        provision_aws.provision_tables(
            ddb, [("fleet-dry-locks", [("lock_key", "HASH")], "ttl")], False, log)
        with pytest.raises(ClientError):
            s3.head_bucket(Bucket="fleet-dry")
        with pytest.raises(ClientError):
            ddb.describe_table(TableName="fleet-dry-locks")


def test_policy_is_env_scoped_and_grants_sessions_scan():
    """FR-6/FR-7: the scoped IAM policy is env-id-scoped (S3 prefix) AND grants
    dynamodb:Scan on the sessions table (needed by list_runner_claims and the
    GET /v1/admin/runner-claims fleet-health endpoint)."""
    pol = provision_aws.build_policy(
        "fleet-env", "123456789012", REGION,
        [{"bucket": "b", "lock": "l", "sessions": "s"}])
    sess = next(st for st in pol["Statement"]
                if st["Sid"].startswith("DDBSessionsTable"))
    assert "dynamodb:Scan" in sess["Action"]
    s3obj = next(st for st in pol["Statement"]
                 if st["Sid"].startswith("S3ObjectReadWrite"))
    assert s3obj["Resource"].endswith("/fleet-env/*")


def test_dual_grant_emits_two_resource_sets():
    """A migration-window dual-grant produces per-set statements (suffixed Sids)
    for BOTH the new and legacy resources."""
    pol = provision_aws.build_policy(
        "fleet-env", "123456789012", REGION, [
            {"bucket": "new-b", "lock": "new-l", "sessions": "new-s"},
            {"bucket": "old-b", "lock": "old-l", "sessions": "old-s"},
        ])
    sids = {st["Sid"] for st in pol["Statement"]}
    assert "DDBSessionsTable" in sids       # primary set (no suffix)
    assert "DDBSessionsTable2" in sids       # legacy set (suffix 2)


def test_require_fails_fast_without_name():
    """No lodestar-* default: a missing resource name exits (fail-fast) rather
    than silently provisioning/granting the OLD resources (N4)."""
    with pytest.raises(SystemExit):
        provision_aws._require(
            "S3 bucket", None, {}, "STORAGE_S3_BUCKET", "--bucket")
