""": optional object-store endpoint override on the own-cloud client
factory (STORAGE_S3_ENDPOINT_URL), plus the stale-caller fix in liveness_check.

The contract, in the goal's own words: with the override UNSET, client
construction is byte-for-byte today's (no endpoint_url kwarg exists at all —
not endpoint_url=None); with it SET, the S3 client carries it and the DynamoDB
client never does (Phase 1a keeps the lock/session tables where they are);
reverting is unsetting the var. Both factory branches (default credential chain
and scoped-credential Session) must agree, and an injected client bypasses the
factory entirely.

liveness_check's own-cloud fresh signal must head through the backend's OWN
client: a fresh boto3.client ignores the override (and the scoped creds), so
after a store cutover it would keep reading the old store while reporting a
verdict — the post-cutover stale-caller class.
"""
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import liveness_check as lc  # noqa: E402
import owncloud_backend as ob  # noqa: E402

ENDPOINT = "http://objects.basement.example:3900"


class _Client:
    def __init__(self, svc, kw):
        self.svc, self.kw = svc, kw


def _record_clients(monkeypatch):
    """Replace ob.boto3 with a recorder covering BOTH factory branches.
    Returns the (service, kwargs) pairs the factory asked for, in order."""
    calls = []

    def client(svc, **kw):
        calls.append((svc, kw))
        return _Client(svc, kw)

    class _Session:
        def __init__(self, **kw):
            self.kw = kw

        def client(self, svc, **kw):
            calls.append((svc, kw))
            return _Client(svc, kw)

    monkeypatch.setattr(ob, "boto3", types.SimpleNamespace(client=client, Session=_Session))
    return calls


def _build(tmp_path, **kw):
    return ob.OwnCloudBackend(env_id="t", bucket="b", lock_table="l",
                              sessions_table="s", cache_root=str(tmp_path),
                              region="us-east-2", **kw)


def _kw(calls, svc):
    hits = [kw for s, kw in calls if s == svc]
    assert len(hits) == 1, (svc, calls)
    return hits[0]


# --- the factory -------------------------------------------------------------

def test_unset_builds_todays_clients_byte_for_byte(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_S3_ENDPOINT_URL", raising=False)
    calls = _record_clients(monkeypatch)
    _build(tmp_path)
    for svc in ("s3", "dynamodb"):
        kw = _kw(calls, svc)
        assert "endpoint_url" not in kw, svc
        assert set(kw) == {"region_name", "config"}, svc
        assert kw["region_name"] == "us-east-2"


def test_blank_value_means_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_S3_ENDPOINT_URL", "   ")
    calls = _record_clients(monkeypatch)
    _build(tmp_path)
    assert "endpoint_url" not in _kw(calls, "s3")


def test_set_puts_endpoint_on_s3_only(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_S3_ENDPOINT_URL", ENDPOINT)
    calls = _record_clients(monkeypatch)
    be = _build(tmp_path)
    assert _kw(calls, "s3")["endpoint_url"] == ENDPOINT
    assert "endpoint_url" not in _kw(calls, "dynamodb")
    # The client the backend actually HOLDS is the overridden one, and the
    # transport bounds () still ride along with it.
    assert be.s3.kw["endpoint_url"] == ENDPOINT
    assert be.s3.kw["config"] is _kw(calls, "dynamodb")["config"]


def test_scoped_credential_session_branch_agrees(tmp_path, monkeypatch):
    """The Session branch (MIND_AWS_* scoped keys, the production shape) must
    apply the identical rule — a branch nobody exercises carries no evidence."""
    monkeypatch.setenv("STORAGE_S3_ENDPOINT_URL", ENDPOINT)
    calls = _record_clients(monkeypatch)
    be = _build(tmp_path, aws_access_key_id="AKIAEXAMPLE", aws_secret_access_key="x")
    assert _kw(calls, "s3")["endpoint_url"] == ENDPOINT
    assert _kw(calls, "s3")["region_name"] == "us-east-2"
    assert "endpoint_url" not in _kw(calls, "dynamodb")
    assert be.s3.kw["endpoint_url"] == ENDPOINT


def test_injected_clients_bypass_the_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_S3_ENDPOINT_URL", ENDPOINT)
    calls = _record_clients(monkeypatch)
    s3, ddb = object(), object()
    be = _build(tmp_path, s3=s3, ddb=ddb)
    assert calls == []
    assert be.s3 is s3 and be.ddb is ddb


def test_reverting_is_unsetting_the_var(tmp_path, monkeypatch):
    """Rollback is a configuration change, not a code rollback."""
    monkeypatch.setenv("STORAGE_S3_ENDPOINT_URL", ENDPOINT)
    calls = _record_clients(monkeypatch)
    assert _build(tmp_path).s3.kw["endpoint_url"] == ENDPOINT
    monkeypatch.delenv("STORAGE_S3_ENDPOINT_URL")
    calls.clear()
    assert "endpoint_url" not in _build(tmp_path).s3.kw


# --- the stale caller ---------------------------------------------------------

def test_liveness_fresh_signal_heads_through_the_backend_client(tmp_path, monkeypatch):
    heads = []

    class _S3:
        def head_object(self, **kw):
            heads.append(kw)
            return {"LastModified": datetime(2026, 9, 9, 1, 0, 0, tzinfo=timezone.utc)}

    class _Backend:
        s3 = _S3()

        @staticmethod
        def from_env():
            return _Backend()

        def _s3_key(self, path):
            return "env/world/team-state/agents/alpha.yaml"

    mod = types.ModuleType("owncloud_backend")
    mod.OwnCloudBackend = _Backend
    monkeypatch.setitem(sys.modules, "owncloud_backend", mod)
    monkeypatch.setenv("STORAGE_S3_BUCKET", "bkt")

    # A fresh .client anywhere on this path IS the defect. The function is
    # fail-quiet (any exception -> None), so an unpatched stale caller shows up
    # as a None result and an empty `heads`, not as a raised error.
    import boto3

    def _stale(*a, **k):
        raise AssertionError("stale caller: fresh boto3.client on the fresh-signal path")
    monkeypatch.setattr(boto3, "client", _stale)

    out = lc.fetch_owncloud_shard_lastmodified("alpha", str(tmp_path))
    assert out is not None
    assert heads == [{"Bucket": "bkt", "Key": "env/world/team-state/agents/alpha.yaml"}]


# --- explicit per-instance override () --------------------------------
#
# The env var is the fleet-wide switch; `s3_endpoint_url=` lets ONE instance
# say otherwise. None keeps today's env read byte-for-byte; "" forces the
# regional endpoint even when the env override is set; a URL is that store.

def test_explicit_blank_endpoint_overrides_a_set_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_S3_ENDPOINT_URL", ENDPOINT)
    calls = _record_clients(monkeypatch)
    be = _build(tmp_path, s3_endpoint_url="")
    assert "endpoint_url" not in _kw(calls, "s3")
    assert be.s3_endpoint_url is None


def test_explicit_endpoint_overrides_a_set_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_S3_ENDPOINT_URL", ENDPOINT)
    calls = _record_clients(monkeypatch)
    be = _build(tmp_path, s3_endpoint_url="http://dr.example:9000")
    assert _kw(calls, "s3")["endpoint_url"] == "http://dr.example:9000"
    assert "endpoint_url" not in _kw(calls, "dynamodb")
    assert be.s3_endpoint_url == "http://dr.example:9000"


def test_none_keeps_the_env_read(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_S3_ENDPOINT_URL", ENDPOINT)
    calls = _record_clients(monkeypatch)
    be = _build(tmp_path, s3_endpoint_url=None)
    assert _kw(calls, "s3")["endpoint_url"] == ENDPOINT
    assert be.s3_endpoint_url == ENDPOINT


def _from_env_base(monkeypatch, tmp_path):
    (tmp_path / "world").mkdir()
    monkeypatch.setenv("STORAGE_S3_BUCKET", "live-bkt")
    monkeypatch.setenv("STORAGE_DDB_LOCK_TABLE", "l")
    monkeypatch.setenv("STORAGE_DDB_SESSIONS_TABLE", "s")
    monkeypatch.setenv("MIND_AWS_ACCESS_KEY_ID", "AKLIVE")
    monkeypatch.setenv("MIND_AWS_SECRET_ACCESS_KEY", "live-secret")
    monkeypatch.setenv("MACHINE_ID", "test-box")
    monkeypatch.setenv("MIND_WORLD", str(tmp_path / "world"))
    monkeypatch.delenv("MIND_META", raising=False)
    monkeypatch.delenv("META_PATH", raising=False)
    monkeypatch.setenv("STORAGE_S3_ENDPOINT_URL", ENDPOINT)


def test_from_env_default_reads_the_process_env(tmp_path, monkeypatch):
    _from_env_base(monkeypatch, tmp_path)
    calls = _record_clients(monkeypatch)
    be = ob.OwnCloudBackend.from_env()
    assert be.bucket == "live-bkt"
    assert _kw(calls, "s3")["endpoint_url"] == ENDPOINT


def test_from_env_reads_the_mapping_it_is_given_not_the_process_env(tmp_path, monkeypatch):
    """The cold-snapshot path: an overlaid COPY selects a different bucket,
    different keys and the regional endpoint while os.environ still carries
    the live store's override — and os.environ is untouched afterwards."""
    _from_env_base(monkeypatch, tmp_path)
    calls = _record_clients(monkeypatch)
    overlay = dict(os.environ)
    overlay.update({"STORAGE_S3_BUCKET": "dr-bkt", "STORAGE_S3_ENDPOINT_URL": "",
                    "MIND_AWS_ACCESS_KEY_ID": "AKDR",
                    "MIND_AWS_SECRET_ACCESS_KEY": "dr-secret"})
    be = ob.OwnCloudBackend.from_env(env=overlay)
    assert be.bucket == "dr-bkt"
    assert "endpoint_url" not in _kw(calls, "s3")
    assert be.s3_endpoint_url is None
    assert os.environ["STORAGE_S3_ENDPOINT_URL"] == ENDPOINT
    assert os.environ["STORAGE_S3_BUCKET"] == "live-bkt"
