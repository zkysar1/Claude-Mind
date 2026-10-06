"""Tests for the scheduled orphan-collection pass of the composite goal-queue store ( U14).

THE SEAMS: composite_gc_runner.run_pass(be, path, ...) over a real OwnCloudBackend on an in-memory S3, moto, and moto with
bucket versioning on, with the writer producing real heads and segments; plus the three backend methods and two pure helpers
it stands on (composite_gc_state_get / composite_gc_state_put, composite_gc_runs, gc_state_key, gc_run_time). The delete pass
itself is pinned in test_owncloud_composite_gc_apply_g358202; these tests pin what the RUNNER decides around it.

WHAT THESE PINS ARE FOR. Most claims here are ABSENCE-shaped ('nothing was deleted', 'no S3 call', 'the ledger was not
overwritten'), and each is also what a dead component produces (guard-4166), so each is paired with a control that can fail:
the same state with both licences DOES delete, the call tap demonstrably records a delete, and every refusal is checked
against the store it left unchanged. The default path runs with every parameter at its default (guard-3925): a gate tested
only with its arguments passed explicitly measures a branch production never takes.

Coverage:
  1. inert: no flag, no S3 and no lock call; either flag activates; a flag naming another environment does not; a backend
     that cannot host the layout is not touched
  2. observe by default: never deletes even when the delete flag is on, writes only its two state documents; apply without
     the backend's flag observes instead
  3. apply with both licences: archives, deletes, records the ledger and a delete post
  4. cadence and lease: the interval and its boundary, a future stamp, a held lease, an expired lease, the lock path,
     release after a pass and after a failed pass
  5. the ledger: unreadable is not empty (garbage, a transient error, a wrong shape), invalid entries restart their grace,
     an unreadable stamp writes nothing
  6. the late restore: puts back what a late head names and needs no flag, visits runs by age (the boundary), skips a run
     with no receipt, survives one failing run
  7. refusals and retries: a head commit during the listing is retried and bounded, a legacy whole file is routine, an
     unlisted segment is an anomaly
  8. the result: skip reasons, the pass's own restores, an exception, the post contract
  9. the CLI: one JSON line, exit codes
 10. the backend methods and the pure helpers

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import types
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(PROJECT_ROOT / "core" / "scripts"), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _owncloud_codec as codec  # noqa: E402
import _owncloud_composite as comp  # noqa: E402
import composite_gc_runner as runner  # noqa: E402
from test_owncloud_composite_gc_apply_g358202 import _Tap  # noqa: E402
from test_owncloud_composite_gc_enumerate_g358202 import _names, _state  # noqa: E402
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401  (the doubles are shared)
    BUCKET, ENV_ID, REGION, REL, _MemS3, _backend, _client_error, _isolate, _publish, _setup,
)

DAY = 86400.0
GRACE = comp.GC_GRACE_S
ENV_ROOT = ENV_ID + "/"
NOW_I = 1_800_000_000.0
PATH = Path("/x/world/aspirations.jsonl")
LOCK_KEY = ENV_ID + "/" + REL + ".gc.lock"
HORIZON = runner.TRUST_WINDOW_S + runner.INTERVAL_S


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    for name in (comp.FLAG_ENV, comp.GC_FLAG_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def writer_on(monkeypatch):
    monkeypatch.setenv(comp.FLAG_ENV, ENV_ID)


@pytest.fixture
def gc_on(monkeypatch):
    monkeypatch.setenv(comp.GC_FLAG_ENV, ENV_ID)


class _DelimS3(_MemS3):
    """_MemS3 plus the one thing it lacks: Delimiter. With a delimiter the listing returns the keys directly under the prefix
    as Contents and the directories below it as CommonPrefixes (what S3 does), paged by `page_cap` over the sorted entries."""

    def list_objects_v2(self, *, Bucket, Prefix="", ContinuationToken=None, MaxKeys=None, Delimiter=None, **kw):
        if not Delimiter:
            return super().list_objects_v2(Bucket=Bucket, Prefix=Prefix, ContinuationToken=ContinuationToken,
                                           MaxKeys=MaxKeys, **kw)
        order, seen = [], set()
        for k in sorted(self.objects):
            if not k.startswith(Prefix):
                continue
            rest = k[len(Prefix):]
            if Delimiter in rest:
                p = Prefix + rest.split(Delimiter)[0] + Delimiter
                if p not in seen:
                    seen.add(p)
                    order.append(("prefix", p))
            else:
                order.append(("key", k))
        start = int(ContinuationToken) if ContinuationToken else 0
        cap = min(self.page_cap or 1000, MaxKeys or 1000)
        page = order[start:start + cap]
        out = {"Contents": [{"Key": k, "Size": len(self.objects[k]["Body"]), "ETag": self.objects[k]["ETag"],
                             "LastModified": self.objects[k]["LastModified"]} for t, k in page if t == "key"],
               "CommonPrefixes": [{"Prefix": p} for t, p in page if t == "prefix"],
               "KeyCount": len(page), "IsTruncated": start + cap < len(order)}
        if out["IsTruncated"]:
            out["NextContinuationToken"] = str(start + cap)
        return out


@pytest.fixture(params=["mem", "mem-versioned", "moto", "moto-versioned"])
def s3(request, monkeypatch):
    if request.param in ("mem", "mem-versioned"):
        yield _DelimS3(versioned=request.param == "mem-versioned")
        return
    pytest.importorskip("moto")
    import boto3  # noqa: PLC0415
    from moto import mock_aws  # noqa: PLC0415
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        if request.param == "moto-versioned":
            client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield client


class _FakeDdb:
    """The lock table, with the backend's own condition: a put succeeds when the key is absent or its ttl has passed, and a
    delete only when the holder matches. `put_keys` is every key a put was attempted on."""

    def __init__(self):
        self.items = {}
        self.put_keys = []
        self.calls = []

    @staticmethod
    def _refused(op):
        return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, op)

    def put_item(self, *, TableName, Item, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        key = Item["lock_key"]["S"]
        self.calls.append(("put_item", key))
        self.put_keys.append(key)
        cur = self.items.get(key)
        if cur is not None and int(cur["ttl"]["N"]) >= int(ExpressionAttributeValues[":now"]["N"]):
            raise self._refused("PutItem")
        self.items[key] = Item

    def delete_item(self, *, TableName, Key, ConditionExpression, ExpressionAttributeValues):
        key = Key["lock_key"]["S"]
        self.calls.append(("delete_item", key))
        cur = self.items.get(key)
        if cur is None or cur["holder"]["S"] != ExpressionAttributeValues[":me"]["S"]:
            raise self._refused("DeleteItem")
        del self.items[key]


def _world(tmp_path, s3):
    """A backend (over a call tap and a fake lock table) on a store after TWO writer commits: the second changed one goal
    of asp-2, so head 2 no longer names asp-2's first segment object, which is still there: the one orphan."""
    tap = _Tap(s3)
    be, p, key = _setup(tmp_path, tap)
    be.ddb = _FakeDdb()
    raw1, raw2 = _state(), _state((2, 100))
    _e1, head1 = _publish(s3, key, raw1)
    _e2, head2 = _publish(s3, key, raw2, old_head=head1)
    gone = _names(raw1) - _names(raw2)
    assert len(gone) == 1
    return types.SimpleNamespace(be=be, p=p, key=key, tap=tap, s3=s3, ddb=be.ddb, raw1=raw1, raw2=raw2, head1=head1,
                                 head2=head2, orphan=next(iter(gone)), names2=_names(raw2))


def _doc(w, name):
    k = comp.gc_state_key(ENV_ROOT, REL, name)
    try:
        body = w.s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return json.loads(body)


def _put_raw(w, name, body):
    w.s3.put_object(Bucket=BUCKET, Key=comp.gc_state_key(ENV_ROOT, REL, name), Body=body)


def _raw(w, name):
    return w.s3.get_object(Bucket=BUCKET, Key=comp.gc_state_key(ENV_ROOT, REL, name))["Body"].read()


def _present(w, name):
    return w.be._gc_present(comp.segment_s3_key(w.key, name))


def _mutations(w, mark=0):
    return [c for c in w.tap.calls[mark:] if c[0] in ("put_object", "delete_object", "copy_object")]


def _hold(w, ttl_offset=1000, holder="other:1:1"):
    w.ddb.items[LOCK_KEY] = {"lock_key": {"S": LOCK_KEY}, "holder": {"S": holder},
                             "acquired_at": {"N": str(int(time.time()))},
                             "ttl": {"N": str(int(time.time()) + ttl_offset)}}


# ---- a backend double for the routing branches -----------------------------------------------------


class _FakeBe:
    """Every method the runner needs, canned, so a branch of the result logic can be reached without building a store.
    `apply_result` and `enum_results` are what the passes return, or raise when an Exception; `restore` maps a run id to
    what composite_gc_restore returns, or raises when it gives an Exception."""
    env_id = ENV_ID

    def __init__(self, *, apply_result=None, enum_results=None, runs=(), restore=None, state=None, ledger=None):
        self.apply_result = apply_result
        self.enum_results = list(enum_results or [])
        self.runs = runs
        self.restore = restore or (lambda run_id: [])
        self.docs = {"state": state, "ledger": ledger}
        self.puts = []
        self.calls = []
        self.apply_calls = 0
        self.enum_calls = 0

    def acquire_lock(self, lock_path, timeout=10, stale_seconds=30):
        self.calls.append(("acquire", Path(lock_path).name, timeout, stale_seconds))

    def release_lock(self, lock_path):
        self.calls.append(("release", Path(lock_path).name))

    def composite_gc_state_get(self, path, doc):
        v = self.docs[doc]
        if isinstance(v, Exception):
            raise v
        return v

    def composite_gc_state_put(self, path, doc, obj):
        self.puts.append((doc, obj))
        self.docs[doc] = obj

    def composite_gc_runs(self):
        if isinstance(self.runs, Exception):
            raise self.runs
        return list(self.runs)

    def composite_gc_restore(self, path, run_id):
        self.calls.append(("restore", run_id))
        out = self.restore(run_id)
        if isinstance(out, Exception):
            raise out
        return out

    def composite_gc_apply(self, path, ledger, now, **kw):
        self.apply_calls += 1
        if isinstance(self.apply_result, Exception):
            raise self.apply_result
        return self.apply_result

    def composite_gc_enumerate(self, path, ledger, now, **kw):
        self.enum_calls += 1
        r = self.enum_results.pop(0) if len(self.enum_results) > 1 else self.enum_results[0]
        if isinstance(r, Exception):
            raise r
        return r


def _plan(delete=(), ledger=None, refused=(), unknown=(), counts=None):
    return comp.GcPlan(list(delete), dict(ledger or {}), list(refused), list(unknown), dict(counts or {"listed": 3}))


def _enum(plan=None):
    return comp.GcEnumeration(plan or _plan(), '"etag"', {})


def _applied(stopped=None, plan=None, ledger=None, run_id=None, deleted=(), restored=(), skipped=None):
    return comp.GcApplied(stopped, plan, dict(ledger or {}), run_id, list(deleted), list(restored), dict(skipped or {}))


# ---- 1. inert ---------------------------------------------------------------------------------------


def test_with_no_flag_the_runner_makes_no_s3_and_no_lock_call(s3, tmp_path):
    w = _world(tmp_path, s3)
    mark = len(w.tap.calls)
    res = runner.run_pass(w.be, w.p, now=NOW_I)
    assert res["verdict"] == "inactive" and res["post"] is None and res["anomalies"] == []
    assert w.tap.calls[mark:] == [] and w.ddb.calls == [] and w.ddb.items == {}


@pytest.mark.parametrize("flag", [comp.FLAG_ENV, comp.GC_FLAG_ENV])
def test_either_flag_naming_the_environment_activates_the_runner(s3, tmp_path, monkeypatch, flag):
    w = _world(tmp_path, s3)
    monkeypatch.setenv(flag, "some-other-env")
    mark = len(w.tap.calls)
    assert runner.run_pass(w.be, w.p, now=NOW_I)["verdict"] == "inactive"  # the control: a flag naming another environment
    assert w.tap.calls[mark:] == [] and w.ddb.calls == []
    monkeypatch.setenv(flag, ENV_ID)
    res = runner.run_pass(w.be, w.p, now=NOW_I)
    assert res["verdict"] == "observed" and res["would_delete"] == 0 and res["counts"]["listed"] == len(w.names2) + 1


@pytest.mark.parametrize("missing", runner._BACKEND_METHODS)
def test_a_backend_that_cannot_host_the_layout_is_not_touched(monkeypatch, writer_on, missing):
    partial = type("Partial", (_FakeBe,), {missing: None})()
    res = runner.run_pass(partial, PATH, now=NOW_I)
    assert res["verdict"] == "not-own-cloud" and partial.calls == [] and partial.puts == []
    full = _FakeBe(enum_results=[_enum()])  # the control: the same flags, a backend that has every method
    assert runner.run_pass(full, PATH, now=NOW_I)["verdict"] == "observed"
    assert runner.run_pass(types.SimpleNamespace(env_id=ENV_ID), PATH, now=NOW_I)["verdict"] == "not-own-cloud"


# ---- 2. observe by default --------------------------------------------------------------------------


def test_the_default_pass_observes_and_never_deletes_even_when_the_delete_flag_is_on(s3, tmp_path, writer_on, gc_on):
    w = _world(tmp_path, s3)
    t0 = time.time()
    first = runner.run_pass(w.be, w.p, now=t0)  # every parameter at its default but the clock
    assert first["mode"] == "observe" and first["verdict"] == "observed" and first["would_delete"] == 0
    later = t0 + GRACE + 10  # past the grace and past the interval: the orphan IS deletable now
    res = runner.run_pass(w.be, w.p, now=later)
    assert res["mode"] == "observe" and res["verdict"] == "observed" and res["deleted"] == [] and res["run_id"] is None
    assert res["would_delete"] == 1
    assert [c for c in w.tap.calls if c[0] == "delete_object"] == []
    assert _present(w, w.orphan)


def test_observe_writes_only_the_two_state_documents(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    mark = len(w.tap.calls)
    runner.run_pass(w.be, w.p, now=time.time())
    keys = {k for _op, k in _mutations(w, mark)}
    assert keys == {comp.gc_state_key(ENV_ROOT, REL, "ledger"), comp.gc_state_key(ENV_ROOT, REL, "state")}
    assert {op for op, _k in _mutations(w, mark)} == {"put_object"}


def test_apply_without_the_backends_flag_observes_instead(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    t0 = time.time()
    runner.run_pass(w.be, w.p, now=t0)
    res = runner.run_pass(w.be, w.p, apply=True, now=t0 + GRACE + 10)
    assert res["mode"] == "apply" and res["apply_refused"] == "gc-not-enabled" and res["verdict"] == "observed"
    assert res["deleted"] == [] and res["would_delete"] == 1 and res["anomalies"] == []
    assert [c for c in w.tap.calls if c[0] == "delete_object"] == [] and _present(w, w.orphan)
    assert _doc(w, "ledger")["ledger"][w.orphan] == pytest.approx(t0)  # the sighting was still recorded


# ---- 3. apply with both licences --------------------------------------------------------------------


def test_apply_with_both_licences_archives_deletes_and_records(s3, tmp_path, writer_on, gc_on):
    w = _world(tmp_path, s3)
    t0 = time.time()
    runner.run_pass(w.be, w.p, now=t0)
    later = t0 + GRACE + 10
    res = runner.run_pass(w.be, w.p, apply=True, now=later)
    assert res["verdict"] == "applied" and res["deleted"] == [w.orphan] and res["run_id"] and res["anomalies"] == []
    assert not _present(w, w.orphan)  # the control for the observe tests: this one DID delete
    assert [c for c in w.tap.calls if c[0] == "delete_object"]
    run_id = res["run_id"]
    receipt = json.loads(w.s3.get_object(Bucket=BUCKET, Key=comp.gc_receipt_key(ENV_ROOT, run_id))["Body"].read())
    assert receipt["status"] == "done" and receipt["deleted"] == [w.orphan]
    assert w.orphan not in _doc(w, "ledger")["ledger"]
    state = _doc(w, "state")
    assert state["last_pass_at"] == pytest.approx(later) and state["last_verdict"] == "applied"
    assert res["post"]["severity"] == "deleted" and run_id in res["post"]["body"]


# ---- 4. cadence and lease ---------------------------------------------------------------------------


def test_a_second_pass_inside_the_interval_is_not_due_and_reads_only_the_stamp(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    t0 = time.time()
    runner.run_pass(w.be, w.p, now=t0)
    mark = len(w.tap.calls)
    res = runner.run_pass(w.be, w.p, now=t0 + 60)
    assert res["verdict"] == "not-due" and 0 < res["next_due_in_s"] <= runner.INTERVAL_S and res["post"] is None
    assert w.tap.calls[mark:] == [("get_object", comp.gc_state_key(ENV_ROOT, REL, "state"))]
    assert w.ddb.items == {}  # the lease it took to read the stamp was released


def test_the_interval_boundary_is_due_exactly_at_the_interval(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    t0 = 1_700_000_000.0
    runner.run_pass(w.be, w.p, now=t0)
    assert runner.run_pass(w.be, w.p, now=t0 + runner.INTERVAL_S - 1)["verdict"] == "not-due"
    assert runner.run_pass(w.be, w.p, now=t0 + runner.INTERVAL_S)["verdict"] == "observed"


def test_force_skips_the_interval_but_not_the_lease(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    t0 = time.time()
    runner.run_pass(w.be, w.p, now=t0)
    assert runner.run_pass(w.be, w.p, now=t0 + 60, force=True)["verdict"] == "observed"
    _hold(w)
    assert runner.run_pass(w.be, w.p, now=t0 + 120, force=True)["verdict"] == "lease-held"


def test_a_cadence_stamp_in_the_future_is_due_and_overwritten(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    t0 = time.time()
    w.be.composite_gc_state_put(w.p, "state", {"format": 1, "last_pass_at": t0 + 10 * DAY})
    res = runner.run_pass(w.be, w.p, now=t0)
    assert res["verdict"] == "observed" and any("future" in a for a in res["anomalies"]) and res["post"]
    assert _doc(w, "state")["last_pass_at"] == pytest.approx(t0)


def test_a_held_lease_returns_lease_held_with_no_s3_call(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    _hold(w)
    mark = len(w.tap.calls)
    res = runner.run_pass(w.be, w.p, now=time.time())
    assert res["verdict"] == "lease-held" and res["post"] is None and res["anomalies"] == []
    assert w.tap.calls[mark:] == []
    assert w.ddb.items[LOCK_KEY]["holder"]["S"] == "other:1:1"  # the other holder's row is untouched


def test_an_expired_lease_is_taken_over_and_released(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    _hold(w, ttl_offset=-5)
    assert runner.run_pass(w.be, w.p, now=time.time())["verdict"] == "observed"
    assert w.ddb.items == {}


def test_the_lock_path_is_the_stores_own_plus_gc_lock_never_its_write_lock(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    runner.run_pass(w.be, w.p, now=time.time())
    assert w.ddb.put_keys == [LOCK_KEY] and LOCK_KEY != ENV_ID + "/world/aspirations.lock"
    assert w.ddb.items == {}


def test_a_pass_that_raises_still_releases_the_lease_and_advances_the_stamp(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)

    def boom(kw):
        raise _client_error("InternalError", "ListObjectsV2")

    w.tap.rule("list_objects_v2", lambda kw: kw.get("Prefix") == comp.segment_s3_key(w.key, ""), boom, when="before")
    t0 = time.time()
    res = runner.run_pass(w.be, w.p, now=t0)
    assert res["verdict"] == "error: ClientError" and w.ddb.items == {}
    assert res["post"]["severity"] == "anomaly" and "InternalError" in res["post"]["body"]
    assert _doc(w, "state")["last_pass_at"] == pytest.approx(t0)  # an error consumes the interval, so a failure cannot loop
    assert runner.run_pass(w.be, w.p, now=t0 + 60)["verdict"] == "not-due"


def test_an_unavailable_lease_table_is_an_anomaly_and_no_pass(monkeypatch, writer_on):
    be = _FakeBe(enum_results=[_enum()])

    def broken(lock_path, timeout=10, stale_seconds=30):
        raise _client_error("InternalError", "PutItem")

    be.acquire_lock = broken
    res = runner.run_pass(be, PATH, now=NOW_I)
    assert res["verdict"] == "lease-unavailable" and res["post"] and be.enum_calls == 0 and be.puts == []


def test_the_lease_is_tried_once_with_the_documented_ttl(writer_on):
    be = _FakeBe(enum_results=[_enum()])
    runner.run_pass(be, PATH, now=NOW_I)
    assert be.calls[0] == ("acquire", "aspirations.jsonl.gc.lock", 0, runner.LEASE_TTL_S)
    assert be.calls[-1] == ("release", "aspirations.jsonl.gc.lock")


def test_a_lease_that_cannot_be_released_is_an_anomaly_the_post_carries(writer_on):
    be = _FakeBe(enum_results=[_enum()])

    def broken(lock_path):
        raise _client_error("InternalError", "DeleteItem")

    be.release_lock = broken
    res = runner.run_pass(be, PATH, now=NOW_I)
    assert res["verdict"] == "observed" and any("lease-release-failed" in a for a in res["anomalies"])
    assert res["post"]["severity"] == "anomaly" and "lease-release-failed" in res["post"]["body"]


# ---- 5. the ledger ----------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["garbage", "wrong-shape", "transient"])
def test_an_unreadable_ledger_stops_the_pass_and_is_never_overwritten(s3, tmp_path, writer_on, gc_on, how):
    w = _world(tmp_path, s3)
    t0 = time.time()
    if how == "garbage":
        _put_raw(w, "ledger", b"not json at all")
    elif how == "wrong-shape":
        _put_raw(w, "ledger", json.dumps({"ledger": [1, 2]}).encode())
    else:
        key = comp.gc_state_key(ENV_ROOT, REL, "ledger")
        _put_raw(w, "ledger", json.dumps({"ledger": {}}).encode())

        def flaky(kw):
            raise _client_error("InternalError", "GetObject")

        w.tap.rule("get_object", lambda kw: kw.get("Key") == key, flaky, when="before")
    before = _raw(w, "ledger")
    res = runner.run_pass(w.be, w.p, apply=True, now=t0 + GRACE + 10)
    assert res["verdict"] == "ledger-unreadable" and res["post"]["severity"] == "anomaly" and res["deleted"] == []
    assert _raw(w, "ledger") == before  # an unreadable ledger is not an empty one: nothing overwrote it
    assert [c for c in w.tap.calls if c[0] == "delete_object"] == [] and _present(w, w.orphan)
    assert _doc(w, "state")["last_pass_at"] == pytest.approx(t0 + GRACE + 10)


def test_invalid_ledger_entries_are_dropped_and_their_grace_starts_over(s3, tmp_path, writer_on, gc_on):
    w = _world(tmp_path, s3)
    real = time.time()
    later = real + GRACE + 10
    ledger_doc = {"format": 1, "ledger": {w.orphan: 0}}  # a bogus zero would date the orphan as old on its first day
    w.be.composite_gc_state_put(w.p, "ledger", ledger_doc)
    res = runner.run_pass(w.be, w.p, apply=True, now=later)
    assert res["ledger_repaired"] == 1 and any("dropped" in a for a in res["anomalies"])
    assert res["deleted"] == [] and _present(w, w.orphan)  # its clock restarted at `later`
    assert _doc(w, "ledger")["ledger"][w.orphan] == pytest.approx(later)
    # the control: the same orphan with a valid old first sighting IS deleted
    w.be.composite_gc_state_put(w.p, "ledger", {"format": 1, "ledger": {w.orphan: real - 100 * DAY}})
    res2 = runner.run_pass(w.be, w.p, apply=True, now=later + runner.INTERVAL_S)
    assert res2["ledger_repaired"] == 0 and res2["deleted"] == [w.orphan]


def test_clean_ledger_keeps_only_finite_positive_past_entries():
    good, dropped = runner.clean_ledger({"ledger": {"ok": 50.0, "zero": 0, "neg": -1, "future": 200.0, "str": "x",
                                                    "bool": True, "nan": float("nan"), "inf": float("inf")}}, 100.0)
    assert good == {"ok": 50.0} and dropped == 7
    assert runner.clean_ledger(None, 100.0) == ({}, 0)
    with pytest.raises(runner.LedgerUnreadable):
        runner.clean_ledger([], 1.0)
    with pytest.raises(runner.LedgerUnreadable):
        runner.clean_ledger({"ledger": "x"}, 1.0)


@pytest.mark.parametrize("body", [b"{{{", b"[1, 2]"], ids=["garbage", "wrong-shape"])
def test_an_unreadable_stamp_stops_the_pass_and_writes_nothing(s3, tmp_path, writer_on, gc_on, body):
    w = _world(tmp_path, s3)
    _put_raw(w, "state", body)
    mark = len(w.tap.calls)
    res = runner.run_pass(w.be, w.p, apply=True, now=time.time() + GRACE + 10)
    assert res["verdict"] == "state-unreadable" and res["post"]["severity"] == "anomaly"
    assert [c for c in w.tap.calls[mark:] if c[0] in ("put_object", "delete_object")] == []
    assert w.ddb.items == {}


def test_the_ledger_is_rewritten_only_when_it_changed(writer_on):
    ledger = {"a/0." + "0" * 32 + ".jsonl": 50.0}
    be = _FakeBe(enum_results=[_enum(_plan(ledger=ledger))], ledger={"ledger": dict(ledger)})
    runner.run_pass(be, PATH, now=NOW_I)
    assert [d for d, _o in be.puts] == ["state"]  # stored and unchanged: no ledger write
    fresh = _FakeBe(enum_results=[_enum(_plan(ledger={}))], ledger=None)
    runner.run_pass(fresh, PATH, now=NOW_I)
    assert [d for d, _o in fresh.puts] == ["ledger", "state"]  # never written: written even when empty
    grown = _FakeBe(enum_results=[_enum(_plan(ledger={**ledger, "b/0." + "1" * 32 + ".jsonl": NOW_I}))],
                    ledger={"ledger": dict(ledger)})
    runner.run_pass(grown, PATH, now=NOW_I)
    assert [d for d, _o in grown.puts] == ["ledger", "state"]


def test_a_repaired_ledger_is_stored_back_so_the_invalid_entry_is_flagged_once(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    t0 = time.time()
    stale = "x/0." + "9" * 32 + ".jsonl"
    # the stored ledger already holds the orphan's real sighting, so the plan changes nothing: only the repair can force a write
    w.be.composite_gc_state_put(w.p, "ledger", {"format": 1, "ledger": {w.orphan: t0 - 100, stale: -5}})
    first = runner.run_pass(w.be, w.p, now=t0)
    assert first["ledger_repaired"] == 1 and any("dropped" in a for a in first["anomalies"])
    assert _doc(w, "ledger")["ledger"] == {w.orphan: t0 - 100}  # the invalid entry is gone from the store, the real one kept
    second = runner.run_pass(w.be, w.p, now=t0 + runner.INTERVAL_S + 1)
    assert second["ledger_repaired"] == 0 and second["anomalies"] == []  # flagged once, not on every pass


def test_an_unreadable_ledger_does_not_stop_the_late_restore(writer_on):
    run_id = comp.gc_run_id(NOW_I - 100, [])
    put_back = ["x/0." + "3" * 32 + ".jsonl"]
    be = _FakeBe(runs=[run_id], restore=lambda r: put_back, ledger=RuntimeError("transient"))
    res = runner.run_pass(be, PATH, now=NOW_I)
    assert res["verdict"] == "ledger-unreadable" and be.enum_calls == 0 and be.apply_calls == 0
    assert res["late_restored"] == {run_id: put_back}  # recovery never waits on the ledger
    assert [d for d, _o in be.puts] == ["state"] and be.puts[0][1]["last_verdict"] == "ledger-unreadable"


# ---- 6. the late restore ----------------------------------------------------------------------------


def test_a_late_restore_puts_back_an_object_a_late_head_names_and_needs_no_flag(s3, tmp_path, monkeypatch, writer_on,
                                                                                gc_on):
    w = _world(tmp_path, s3)
    t0 = time.time()
    runner.run_pass(w.be, w.p, now=t0)
    t1 = t0 + GRACE + 10
    first = runner.run_pass(w.be, w.p, apply=True, now=t1)
    assert first["deleted"] == [w.orphan]
    run_id = first["run_id"]
    # a head that commits LATE and names the deleted object: the window the archive exists to survive
    late = comp.plan_write(w.head2, w.raw1)
    w.s3.put_object(Bucket=BUCKET, Key=w.key, Body=late.head,
                    Metadata={codec.META_PLAIN_MD5: hashlib.md5(w.raw1).hexdigest()})
    assert not _present(w, w.orphan)
    monkeypatch.delenv(comp.GC_FLAG_ENV)  # recovery must not depend on the switch that enables deletion
    t2 = t1 + runner.INTERVAL_S + 1
    res = runner.run_pass(w.be, w.p, now=t2)
    assert res["late_restored"] == {run_id: [w.orphan]} and _present(w, w.orphan)
    assert res["verdict"].startswith("refused: head-names-unlisted-segments")  # the same pass saw the broken head first
    assert any("late restore put back 1 object" in a for a in res["anomalies"]) and res["post"]["severity"] == "anomaly"
    clean = runner.run_pass(w.be, w.p, now=t2 + runner.INTERVAL_S + 1)  # the control: after the repair the store is clean
    assert clean["verdict"] == "observed" and clean["anomalies"] == [] and clean["late_restored"] == {}


def test_the_late_restore_visits_runs_by_age_and_only_real_run_ids(writer_on):
    ids = {"inside": comp.gc_run_id(NOW_I - HORIZON + 1, []), "boundary": comp.gc_run_id(NOW_I - HORIZON, []),
           "fresh": comp.gc_run_id(NOW_I - 10, []), "future": comp.gc_run_id(NOW_I + 100, []),
           "old": comp.gc_run_id(NOW_I - 10 * HORIZON, [])}
    be = _FakeBe(enum_results=[_enum()], runs=list(ids.values()) + ["_state", "junk", "RECEIPT.json"])
    res = runner.run_pass(be, PATH, now=NOW_I)
    visited = [c[1] for c in be.calls if c[0] == "restore"]
    assert sorted(visited) == sorted([ids["inside"], ids["fresh"], ids["future"]])
    assert res["anomalies"] == [] and res["late_restored"] == {}


def test_a_run_with_no_receipt_is_skipped_and_one_failing_run_does_not_hide_the_rest(writer_on):
    a, b, c = (comp.gc_run_id(NOW_I - 100 * i, []) for i in (1, 2, 3))

    def restore(run_id):
        if run_id == a:
            return _client_error("NoSuchKey", "GetObject")
        if run_id == b:
            return comp.CompositeError("archive object no longer matches its receipt")
        return ["x/0." + "2" * 32 + ".jsonl"]

    be = _FakeBe(enum_results=[_enum()], runs=[c, b, a], restore=restore)
    res = runner.run_pass(be, PATH, now=NOW_I)
    assert sorted(x[1] for x in be.calls if x[0] == "restore") == sorted([a, b, c])  # every run was visited
    assert res["late_restored"] == {c: ["x/0." + "2" * 32 + ".jsonl"]}
    assert [x for x in res["anomalies"] if a in x] == []  # no receipt: nothing was deleted, nothing to say
    assert [x for x in res["anomalies"] if "late-restore-failed" in x and b in x]
    assert res["post"]["severity"] == "anomaly"


def test_an_archive_that_cannot_be_listed_is_an_anomaly_not_a_stop(writer_on):
    be = _FakeBe(enum_results=[_enum()], runs=_client_error("AccessDenied", "ListObjectsV2"))
    res = runner.run_pass(be, PATH, now=NOW_I)
    assert res["verdict"] == "observed" and any("archive-listing-failed" in a for a in res["anomalies"])
    assert [d for d, _o in be.puts] == ["ledger", "state"]


# ---- 7. refusals and retries ------------------------------------------------------------------------


def test_a_head_commit_during_the_listing_is_retried_and_succeeds(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    raw3 = _state((2, 100), (3, 50))
    w.tap.rule("list_objects_v2", lambda kw: kw.get("Prefix") == comp.segment_s3_key(w.key, ""),
               lambda kw: _publish(w.s3, w.key, raw3, old_head=w.head2), when="after")
    sleeps = []
    res = runner.run_pass(w.be, w.p, now=time.time(), sleep=sleeps.append)
    assert res["attempts"] == 2 and res["verdict"] == "observed" and res["anomalies"] == []
    assert sleeps == [runner.HEAD_MOVED_BACKOFF_S]


def test_a_head_that_keeps_moving_reads_busy_after_the_bound_and_is_routine(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    pred = lambda kw: kw.get("Prefix") == comp.segment_s3_key(w.key, "")  # noqa: E731
    heads = [w.head2]
    for i, extra in enumerate([((2, 100), (3, 50)), ((2, 100), (3, 50), (1, 7)), ((2, 100), (3, 50), (1, 7), (3, 90))]):
        def commit(kw, extra=extra):
            heads.append(_publish(w.s3, w.key, _state(*extra), old_head=heads[-1])[1])
        w.tap.rule("list_objects_v2", pred, commit, when="after")
    sleeps = []
    res = runner.run_pass(w.be, w.p, now=time.time(), sleep=sleeps.append)
    assert res["attempts"] == runner.HEAD_MOVED_ATTEMPTS and res["verdict"] == "busy"
    assert res["anomalies"] == [] and res["post"] is None
    assert len(sleeps) == runner.HEAD_MOVED_ATTEMPTS - 1


def test_the_retry_bound_and_backoff_with_a_canned_backend(writer_on):
    moved = _enum(_plan(refused=["head-moved-during-enumeration"]))
    be = _FakeBe(enum_results=[moved])
    sleeps = []
    res = runner.run_pass(be, PATH, now=NOW_I, sleep=sleeps.append)
    assert be.enum_calls == res["attempts"] == runner.HEAD_MOVED_ATTEMPTS and res["verdict"] == "busy"
    assert sleeps == [runner.HEAD_MOVED_BACKOFF_S] * (runner.HEAD_MOVED_ATTEMPTS - 1)
    other = _FakeBe(enum_results=[_enum(_plan(refused=["head-missing"]))])
    res2 = runner.run_pass(other, PATH, now=NOW_I, sleep=sleeps.append)
    assert other.enum_calls == 1 and res2["verdict"] == "refused: head-missing"  # only the head-moved refusal is retried


def test_a_legacy_whole_file_at_the_key_is_not_yet_composite_and_routine(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    w.s3.put_object(Bucket=BUCKET, Key=w.key, Body=_state())  # the whole file, as the writer left it before the flip
    res = runner.run_pass(w.be, w.p, now=time.time())
    assert res["verdict"] == "not-yet-composite" and res["anomalies"] == [] and res["post"] is None


def test_a_head_naming_an_unlisted_segment_is_an_anomaly(s3, tmp_path, writer_on):
    w = _world(tmp_path, s3)
    broken = comp.plan_write(None, _state((1, 5)))  # a head whose segments were never PUT
    w.s3.put_object(Bucket=BUCKET, Key=w.key, Body=broken.head)
    res = runner.run_pass(w.be, w.p, now=time.time())
    assert res["verdict"].startswith("refused: head-names-unlisted-segments") and res["post"]["severity"] == "anomaly"
    assert res["deleted"] == [] and res["would_delete"] == 0


# ---- 8. the result ----------------------------------------------------------------------------------


def test_skip_reasons_are_routine_or_an_anomaly(writer_on):
    skipped = {"n1": "re-referenced", "n2": "gone-since-listing", "n3": "gone-since-archive", "n4": "rewritten-since-listing",
               "n5": "undecodable", "n6": "delete-not-effective", "n7": "delete-not-effective"}
    be = _FakeBe(apply_result=_applied(plan=_plan(), run_id="R", deleted=["n0"], skipped=skipped))
    res = runner.run_pass(be, PATH, apply=True, now=NOW_I)
    kept = [a for a in res["anomalies"] if a.startswith("kept ")]
    assert sorted(kept) == ["kept 1 object(s): undecodable", "kept 2 object(s): delete-not-effective"]
    assert res["verdict"] == "applied" and res["post"]["severity"] == "anomaly"
    assert "deleted 1 orphan segment object(s) in run R" in res["post"]["body"]


def test_objects_the_pass_itself_had_to_put_back_are_an_anomaly(writer_on):
    be = _FakeBe(apply_result=_applied(plan=_plan(), run_id="R", deleted=["a", "b"], restored=["a"]))
    res = runner.run_pass(be, PATH, apply=True, now=NOW_I)
    assert any("412 window was met" in a for a in res["anomalies"])


def test_a_pass_abandoned_after_deleting_is_a_refusal_with_its_deletes_reported(writer_on):
    be = _FakeBe(apply_result=_applied("head-changed-layout", plan=_plan(), run_id="R", deleted=["a"]))
    res = runner.run_pass(be, PATH, apply=True, now=NOW_I)
    assert res["verdict"] == "refused: head-changed-layout" and res["deleted"] == ["a"]
    assert "deleted 1 orphan segment object(s)" in res["post"]["body"] and "head-changed-layout" in res["post"]["body"]


def test_apply_refused_by_the_backend_falls_back_to_one_observe(writer_on):
    be = _FakeBe(apply_result=_applied("gc-not-enabled"), enum_results=[_enum(_plan(delete=["a"]))])
    res = runner.run_pass(be, PATH, apply=True, now=NOW_I)
    assert (be.apply_calls, be.enum_calls) == (1, 1) and res["apply_refused"] == "gc-not-enabled"
    assert res["verdict"] == "observed" and res["would_delete"] == 1 and res["anomalies"] == []


def test_an_exception_in_the_pass_is_an_anomaly_that_still_stamps_and_releases(writer_on):
    be = _FakeBe(apply_result=RuntimeError("boom"))
    res = runner.run_pass(be, PATH, apply=True, now=NOW_I)
    assert res["verdict"] == "error: RuntimeError" and res["post"]["severity"] == "anomaly"
    assert [d for d, _o in be.puts] == ["state"] and be.calls[-1][0] == "release"
    assert be.puts[0][1]["last_verdict"] == "error: RuntimeError" and be.puts[0][1]["last_pass_at"] == NOW_I


def test_the_post_contract():
    quiet = runner._new_result(False, NOW_I)
    quiet["verdict"] = "observed"
    assert runner.build_post(quiet) is None
    deleted = dict(quiet, deleted=["a", "b"], run_id="R", verdict="applied")
    post = runner.build_post(deleted)
    assert post["severity"] == "deleted" and "2 orphan segment object(s) in run R" in post["body"]
    odd = dict(quiet, anomalies=["something is off"], verdict="refused: x")
    assert runner.build_post(odd)["severity"] == "anomaly"
    both = dict(deleted, anomalies=["something is off"])
    body = runner.build_post(both)["body"]
    assert runner.build_post(both)["severity"] == "anomaly" and "something is off" in body and "run R" in body


def test_the_summary_is_compact_and_json_safe():
    res = runner._new_result(True, NOW_I)
    res.update(deleted=["a"] * 500, restored=["b"], skipped={"x": "re-referenced", "y": "re-referenced", "z": "undecodable"},
               late_restored={"R": ["c", "d"]}, verdict="applied")
    out = runner.summary(res)
    assert out["deleted"] == 500 and out["restored"] == 1 and out["late_restored"] == {"R": 2}
    assert out["skipped"] == {"re-referenced": 2, "undecodable": 1} and len(json.dumps(out)) < 2000


def test_the_result_carries_the_plans_counts_and_unknowns_and_the_stamp_the_counts(writer_on):
    plan = _plan(unknown=["a/0." + "4" * 32 + ".jsonl", "b/0." + "5" * 32 + ".jsonl"], counts={"listed": 7, "referenced": 5})
    be = _FakeBe(enum_results=[_enum(plan)])
    res = runner.run_pass(be, PATH, now=NOW_I)
    assert res["unknown"] == 2 and res["counts"] == {"listed": 7, "referenced": 5}
    assert dict(be.puts)["state"]["last_counts"] == {"listed": 7, "referenced": 5}


# ---- 9. the CLI -------------------------------------------------------------------------------------


def test_main_prints_one_json_line_and_exits_zero_on_a_clean_observe(s3, tmp_path, monkeypatch, capsys, writer_on):
    import _paths  # noqa: PLC0415
    import storage_backend  # noqa: PLC0415
    w = _world(tmp_path, s3)
    monkeypatch.setattr(storage_backend, "get_backend", lambda: w.be)
    monkeypatch.setattr(_paths, "WORLD_DIR", tmp_path / "world")
    rc = runner.main([])
    lines = capsys.readouterr().out.strip().splitlines()
    assert rc == 0 and len(lines) == 1
    out = json.loads(lines[0])
    assert out["mode"] == "observe" and out["verdict"] == "observed" and out["deleted"] == 0
    assert runner.main([]) == 0 and json.loads(capsys.readouterr().out)["verdict"] == "not-due"
    assert runner.main(["--force", "--interval-s", "5"]) == 0


def test_main_exits_one_on_an_anomaly_and_two_on_a_setup_failure(s3, tmp_path, monkeypatch, capsys, writer_on):
    import _paths  # noqa: PLC0415
    import storage_backend  # noqa: PLC0415
    w = _world(tmp_path, s3)
    w.s3.put_object(Bucket=BUCKET, Key=w.key, Body=comp.plan_write(None, _state((1, 5))).head)  # head with no segments
    monkeypatch.setattr(storage_backend, "get_backend", lambda: w.be)
    monkeypatch.setattr(_paths, "WORLD_DIR", tmp_path / "world")
    assert runner.main(["--force"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"].startswith("refused")

    def broken():
        raise RuntimeError("no backend")

    monkeypatch.setattr(storage_backend, "get_backend", broken)
    assert runner.main([]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "setup-failed"


def test_main_apply_flag_reaches_run_pass(monkeypatch, capsys, writer_on):
    import _paths  # noqa: PLC0415
    import storage_backend  # noqa: PLC0415
    seen = {}

    def fake_run_pass(be, path, **kw):
        seen.update(kw)
        return dict(runner._new_result(kw["apply"], NOW_I), verdict="observed")

    monkeypatch.setattr(storage_backend, "get_backend", lambda: object())
    monkeypatch.setattr(_paths, "WORLD_DIR", Path("/x/world"))
    monkeypatch.setattr(runner, "run_pass", fake_run_pass)
    runner.main([])
    assert seen["apply"] is False and seen["force"] is False and seen["interval_s"] == runner.INTERVAL_S
    runner.main(["--apply", "--force", "--interval-s", "60"])
    assert seen["apply"] is True and seen["force"] is True and seen["interval_s"] == 60.0
    capsys.readouterr()


# ---- 10. the backend methods and the pure helpers ---------------------------------------------------


def test_gc_state_key_layout_and_an_unknown_document():
    assert comp.gc_state_key("c/e/", "world/aspirations.jsonl", "ledger") == \
        "c/e/_composite-gc-archive/_state/world/aspirations.jsonl/ledger.json"
    assert comp.gc_state_key(ENV_ROOT, REL, "state").endswith("/_state/%s/state.json" % REL)
    with pytest.raises(ValueError):
        comp.gc_state_key(ENV_ROOT, REL, "receipt")
    assert comp.gc_run_time(comp.GC_STATE_DIR) is None  # the state directory can never be mistaken for a run


def test_gc_run_time_is_the_inverse_of_gc_run_id():
    for now in (0.0, 1_791_052_800.0, NOW_I, 2_000_000_000.0):
        assert comp.gc_run_time(comp.gc_run_id(now, ["a/0." + "0" * 32 + ".jsonl"])) == now
    for bad in ("_state", "RECEIPT.json", "20261003T120000Z", "20261003T120000Z-ZZZZZZZZ", "", "objects",
                "20261003T120000Z-a8d578aa/", "x20261003T120000Z-a8d578aa"):
        assert comp.gc_run_time(bad) is None


def test_state_documents_round_trip_and_live_outside_the_governed_roots(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    assert be.composite_gc_state_get(p, "ledger") is None  # never written: None, and only that
    be.composite_gc_state_put(p, "ledger", {"x": 1, "ledger": {}})
    assert be.composite_gc_state_get(p, "ledger") == {"x": 1, "ledger": {}}
    assert be.composite_gc_state_get(p, "state") is None  # the two documents are separate objects
    k = comp.gc_state_key(ENV_ROOT, REL, "ledger")
    s3.get_object(Bucket=BUCKET, Key=k)
    assert k.startswith(ENV_ID + "/_composite-gc-archive/") and not k.startswith(ENV_ID + "/world/")
    with pytest.raises(ValueError):
        be.composite_gc_state_put(p, "receipt", {})


def test_state_get_is_none_only_for_a_missing_object(s3, tmp_path):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    tap = _Tap(s3)
    be, p, key = _setup(tmp_path, tap)
    k = comp.gc_state_key(ENV_ROOT, REL, "state")
    s3.put_object(Bucket=BUCKET, Key=k, Body=b"not json")
    with pytest.raises(ValueError):  # a body that is not JSON is unreadable, never empty
        be.composite_gc_state_get(p, "state")
    s3.put_object(Bucket=BUCKET, Key=k, Body=b'{"ok": true}')

    def denied(kw):
        raise _client_error("AccessDenied", "GetObject")

    tap.rule("get_object", lambda kw: kw.get("Key") == k, denied, when="before")
    with pytest.raises(OwnCloudPermissionError):
        be.composite_gc_state_get(p, "state")
    assert be.composite_gc_state_get(p, "state") == {"ok": True}  # the control: the object was there all along


def test_state_methods_refuse_a_store_off_the_allowlist(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    other = tmp_path / "world" / "other.jsonl"
    with pytest.raises(comp.CompositeError):
        be.composite_gc_state_get(other, "state")
    with pytest.raises(comp.CompositeError):
        be.composite_gc_state_put(other, "state", {})
    assert [k for k in getattr(s3, "objects", {}) if "_state" in k] == []


def test_composite_gc_runs_lists_only_run_directories_oldest_first(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    older, newer = comp.gc_run_id(NOW_I - DAY, []), comp.gc_run_id(NOW_I, [])
    root = ENV_ROOT + comp.GC_ARCHIVE_DIR + "/"
    for k in (root + newer + "/RECEIPT.json", root + older + "/RECEIPT.json", root + older + "/objects/%s/a/0.x" % REL,
              root + "_state/%s/state.json" % REL, root + "junk/x", root + "stray-file.json"):
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"{}")
    s3.put_object(Bucket=BUCKET, Key=ENV_ROOT + "world/aspirations.jsonl", Body=b"{}")
    assert be.composite_gc_runs() == [older, newer]


def test_composite_gc_runs_on_an_environment_with_no_archive_is_empty(s3, tmp_path):
    be, p, key = _setup(tmp_path, s3)
    assert be.composite_gc_runs() == []


def test_composite_gc_runs_reads_every_page(s3, tmp_path):
    if not isinstance(s3, _MemS3):
        pytest.skip("page size is a property of the in-memory double")
    s3.page_cap = 2
    be, p, key = _setup(tmp_path, s3)
    ids = [comp.gc_run_id(NOW_I - i * 3600, []) for i in range(7)]
    for rid in ids:
        s3.put_object(Bucket=BUCKET, Key=ENV_ROOT + comp.GC_ARCHIVE_DIR + "/" + rid + "/RECEIPT.json", Body=b"{}")
    assert be.composite_gc_runs() == sorted(ids)


def test_composite_gc_runs_treats_a_truncated_listing_without_a_token_as_an_error(s3, tmp_path):
    tap = _Tap(s3)
    be, p, key = _setup(tmp_path, tap)
    tap.rule("list_objects_v2", lambda kw: True, lambda kw: {"IsTruncated": True, "CommonPrefixes": []}, when="instead")
    with pytest.raises(comp.CompositeError):
        be.composite_gc_runs()
