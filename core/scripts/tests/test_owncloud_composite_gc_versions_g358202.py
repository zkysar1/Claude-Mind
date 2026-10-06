"""The delete pass on a VERSIONED store removes the versions it listed, by id, not the key ( U8).

THE SEAMS: `_owncloud_composite.plan_version_delete` (pure: what may go, in what order, or why the orphan stays) and
`OwnCloudBackend.composite_gc_apply`, which on a store whose archive GET returns a VersionId lists each archived
object's chain (`_composite_object_versions`), writes it to the receipt before any delete, re-checks by VersionId and
deletes every listed version by id, the latest last. The planner is pinned in test_owncloud_composite_gc_g358202, the
re-check by last_modified and the rest of the pass in test_owncloud_composite_gc_apply_g358202 (which now also runs
over a versioned in-memory store and moto with bucket versioning on).

WHY: a key delete names the KEY, so on a versioned store it hides whatever is current, a writer's re-PUT of an old
segment (a version the listing never saw) included, and it leaves every version behind for the noncurrent window.
Measured live on the MinIO store (U7): DeleteObject IfMatch is ignored and a same-bytes re-PUT leaves the ETag
unchanged, so only the version id can tell the two apart. Every claim here of the form 'X survived' is paired with the
unversioned control in which the same trigger loses X (a pin that cannot fail proves nothing, guard-4166).

Coverage:
  1. the planner: order (noncurrent oldest first, latest last), a moved or unreadable chain, bytes the archive does
     not hold
  2. the chain listing: exact to the key, read to the end of its pagination, loud on a truncation with no marker and
     on a missing permission
  3. a pass over a versioned store: the receipt names every version before the first delete; every listed version and
     marker goes by id and nothing is left; a failure on the last delete leaves the object present; a refused delete
     raises; a delete the store does not honour is reported, not counted; a version already gone is not an error; an
     empty chain keeps the name and never becomes a key delete
  4. the gaps: a re-PUT before the re-check keeps the name with no delete; one after it survives; one between the
     two listings is caught by its time; the same late re-PUT is LOST on an unversioned store (the control)
  5. bytes the archive does not hold keep the whole name
  6. an unversioned store keeps the key delete and lists no versions

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(PROJECT_ROOT / "core" / "scripts"), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _owncloud_composite as comp  # noqa: E402
from test_owncloud_composite_gc_apply_g358202 import (  # noqa: E402,F401  (the fixtures and helpers are shared)
    DAY, ENV_ROOT, GRACE, _aged, _apply, _body, _exists, _gc_flag_off, _key_is, _seg, _world, gc_on,
)
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401
    BUCKET, REGION, _MemS3, _client_error, _isolate,
)


@pytest.fixture(params=["mem-versioned", "moto-versioned"])
def s3v(request, monkeypatch):
    yield from _store(request, monkeypatch, versioned=True)


@pytest.fixture(params=["mem", "moto"])
def s3u(request, monkeypatch):
    yield from _store(request, monkeypatch, versioned=False)


def _store(request, monkeypatch, versioned):
    if request.param.startswith("mem"):
        yield _Deletes(_MemS3(versioned=versioned))
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
        if versioned:
            client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield _Deletes(client)


class _Deletes:
    """Records the keyword arguments of every delete_object the backend issues: the call tap logs only the key, and
    what these tests pin is WHICH VERSION each delete names."""

    def __init__(self, real):
        self._real = real
        self.deletes = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name != "delete_object":
            return attr

        def call(**kw):
            self.deletes.append(dict(kw))
            return attr(**kw)
        return call


def _is_mem(s3):
    return isinstance(s3._real, _MemS3)


def _chain(s3, key):
    """The live chain of `key`, newest first by the store's own listing: (version id, is_latest, is_marker)."""
    out = s3.list_object_versions(Bucket=BUCKET, Prefix=key)
    rows = [(v["VersionId"], v["IsLatest"], False) for v in out.get("Versions", []) if v["Key"] == key]
    rows += [(m["VersionId"], m["IsLatest"], True) for m in out.get("DeleteMarkers", []) if m["Key"] == key]
    return rows


def _latest(s3, key):
    return next(v for v, latest, _m in _chain(s3, key) if latest)


def _put_again(w, name):
    """A writer's freshen: the same bytes PUT again at an old segment's key (a new version, current)."""
    key = _seg(w, name)
    w.s3.put_object(Bucket=BUCKET, Key=key, Body=_body(w.s3, key))


def _grow_chain(w, name):
    """v1 (there already), a same-bytes re-PUT, a delete marker, then a re-PUT again: three versions and a marker."""
    key = _seg(w, name)
    raw = _body(w.s3, key)
    w.s3.put_object(Bucket=BUCKET, Key=key, Body=raw)
    w.s3.delete_object(Bucket=BUCKET, Key=key)
    w.s3.put_object(Bucket=BUCKET, Key=key, Body=raw)
    assert len(_chain(w.s3, key)) == 4
    w.s3.deletes.clear()


def _age_current(w, name, days=100):
    """The listing reports the object as `days` old (the in-memory double only: moto stamps at PUT time)."""
    w.s3._real.objects[_seg(w, name)]["LastModified"] = datetime.now(timezone.utc) - timedelta(days=days)


def _v(version_id, latest=False, marker=False, etag='"e"', lm=100.0):
    return {"version_id": version_id, "is_latest": latest, "marker": marker, "etag": None if marker else etag,
            "size": 0 if marker else 3, "last_modified": lm}


# --- 1. the planner ---------------------------------------------------------------------------------------------
def test_a_chain_goes_noncurrent_first_and_oldest_first_with_the_latest_last():
    chain = [_v("v4", latest=True), _v("v3", marker=True), _v("v2"), _v("v1")]
    out = comp.plan_version_delete(chain, 100.0, "v4", '"e"')
    assert out.keep is None and out.order == ["v1", "v2", "v3", "v4"]


def test_a_single_version_is_just_the_latest():
    out = comp.plan_version_delete([_v("only", latest=True)], 100.0, "only", '"e"')
    assert out.keep is None and out.order == ["only"]


@pytest.mark.parametrize("head", ["v1", None, "", "null"])
def test_a_head_that_is_not_the_listed_latest_keeps_the_orphan(head):
    chain = [_v("v2", latest=True), _v("v1")]
    assert comp.plan_version_delete(chain, 100.0, head, '"e"') == comp.VersionDelete([], "rewritten-since-listing")


def test_a_latest_version_later_than_the_plans_listing_keeps_the_orphan_and_the_slop_does_not():
    chain = [_v("v2", latest=True, lm=100.0 + comp.GC_MTIME_SLOP_S + 0.5), _v("v1")]
    assert comp.plan_version_delete(chain, 100.0, "v2", '"e"') == comp.VersionDelete([], "rewritten-since-listing")
    inside = [_v("v2", latest=True, lm=100.0 + comp.GC_MTIME_SLOP_S), _v("v1")]
    assert comp.plan_version_delete(inside, 100.0, "v2", '"e"').order == ["v1", "v2"]  # the control: it can pass


def test_a_listed_version_that_is_not_the_archived_bytes_keeps_the_whole_orphan():
    chain = [_v("v3", latest=True), _v("v2", etag='"other"'), _v("v1")]
    assert comp.plan_version_delete(chain, 100.0, "v3", '"e"') == comp.VersionDelete([], "version-bytes-not-archived")
    assert comp.plan_version_delete([_v("v1", latest=True)], 100.0, "v1", None).keep == "version-bytes-not-archived"
    assert comp.plan_version_delete([_v("v2", latest=True), _v("m", marker=True)], 100.0, "v2", '"e"').keep is None


@pytest.mark.parametrize("chain, head", [
    ([], "v2"),
    ([_v("v1")], "v1"),  # no latest
    ([_v("v2", latest=True), _v("v1", latest=True)], "v2"),  # two latests
    ([_v("m", latest=True, marker=True), _v("v1")], "m"),  # the latest is a delete marker: the key is not current
    ([_v("v2", latest=True), _v("")], "v2"),  # an entry with no id cannot be deleted by id
    ([_v("v2", latest=True, lm=None)], "v2"),  # an undated latest
])
def test_an_unreadable_chain_keeps_the_orphan(chain, head):
    """The head always names the chain's own latest, so the head check can never be what keeps the orphan: only the
    chain's unreadability can. (A case that also failed the head check would stay green with the chain check removed:
    the marker case did, until a mutation proof caught it.)"""
    assert comp.plan_version_delete(chain, 100.0, head, '"e"') == comp.VersionDelete([], "rewritten-since-listing")


# --- 2. the chain listing ---------------------------------------------------------------------------------------
def test_the_chain_is_exact_to_the_key_and_read_to_the_end_of_its_pagination(s3v, tmp_path):
    if not _is_mem(s3v):
        pytest.skip("moto pages at 1000 entries: the in-memory double pages at 2")
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    key = _seg(w, name)
    _grow_chain(w, name)
    w.s3.put_object(Bucket=BUCKET, Key=key + ".tmp", Body=b"x")  # a longer key under the same prefix
    s3v._real.page_cap = 2
    mark = len(w.tap.calls)
    chain = w.be._composite_object_versions(key)
    listings = [c for c in w.tap.calls[mark:] if c[0] == "list_object_versions"]
    assert w.s3.list_object_versions(Bucket=BUCKET, Prefix=key, MaxKeys=2)["IsTruncated"]  # the control: it paginates
    assert len(listings) >= 2 and all(k == key for _op, k in listings)  # more than one page, all over this key
    assert len(chain) == 4 and sorted(v["marker"] for v in chain) == [False, False, False, True]  # 4 > the page of 2
    assert [v["version_id"] for v in chain if v["is_latest"]] == [_latest(s3v, key)]
    assert all(set(v) == {"version_id", "is_latest", "marker", "etag", "size", "last_modified"} for v in chain)
    assert next(v for v in chain if v["marker"])["etag"] is None


def test_a_listing_truncated_without_a_key_marker_is_an_error_not_a_loop(s3v, tmp_path):
    if not _is_mem(s3v):
        pytest.skip("the truncation is injected into the in-memory double")
    w = _world(tmp_path, s3v)
    s3v._real.list_object_versions = lambda **kw: {"Versions": [], "DeleteMarkers": [], "IsTruncated": True}
    with pytest.raises(comp.CompositeError):
        w.be._composite_object_versions(_seg(w, w.orphans[0]))


def test_a_listing_the_principal_may_not_make_is_a_permission_error_before_any_delete(s3v, tmp_path, gc_on):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    w = _world(tmp_path, s3v)

    def deny(kw):
        raise _client_error("AccessDenied", "ListObjectVersions")

    w.tap.rule("list_object_versions", lambda kw: True, deny, when="before")
    with pytest.raises(OwnCloudPermissionError):
        _apply(w)
    assert "delete_object" not in w.tap.ops() and s3v.deletes == []
    assert _exists(s3v, _seg(w, w.orphans[0]))


# --- 3. a pass over a versioned store ---------------------------------------------------------------------------
def test_the_receipt_names_every_version_before_the_first_delete(s3v, tmp_path, gc_on):
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    _grow_chain(w, name)
    key = _seg(w, name)
    listed = {v: (latest, marker) for v, latest, marker in _chain(s3v, key)}
    now = time.time() + GRACE + 10
    seen = []
    w.tap.rule("delete_object", lambda kw: True, lambda kw: seen.append(
        json.loads(_body(w.s3, comp.gc_receipt_key(ENV_ROOT, comp.gc_run_id(now, w.orphans))))), when="before")
    out = _apply(w, now=now)
    assert out.deleted == [name]
    (before,) = seen
    assert before["status"] == "archived" and before["deleted"] == []
    rec = before["objects"][name]
    assert {v["version_id"]: (v["is_latest"], v["marker"]) for v in rec["versions"]} == listed
    assert rec["version_id"] == _latest_listed(listed) and rec["etag"] == '"%s"' % rec["md5"]
    final = json.loads(_body(s3v, comp.gc_receipt_key(ENV_ROOT, out.run_id)))
    assert final["status"] == "done" and final["deleted"] == [name]


def _latest_listed(listed):
    return next(v for v, (latest, _marker) in listed.items() if latest)


def test_every_listed_version_and_marker_goes_by_id_with_the_latest_last_and_nothing_is_left(s3v, tmp_path, gc_on):
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    key = _seg(w, name)
    _grow_chain(w, name)
    ids, latest = {v for v, _l, _m in _chain(s3v, key)}, _latest(s3v, key)
    out = _apply(w)
    assert out.stopped is None and out.deleted == [name] and out.skipped == {}
    named = [d for d in s3v.deletes if d["Key"] == key]
    assert all(d.get("VersionId") for d in named)  # no key-only delete: that would only hide the object
    assert {d["VersionId"] for d in named} == ids and len(named) == len(ids)
    assert named[-1]["VersionId"] == latest  # the latest goes last
    assert _chain(s3v, key) == [] and not _exists(s3v, key)  # no marker, no noncurrent version
    assert name not in out.ledger
    archive = comp.gc_archive_key(ENV_ROOT, out.run_id, "world/aspirations.jsonl", name)
    assert _exists(s3v, archive)  # the archive is now the only copy


def test_a_failure_on_the_last_delete_leaves_the_object_present(s3v, tmp_path, gc_on):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    key = _seg(w, name)
    _grow_chain(w, name)
    latest = _latest(s3v, key)

    def deny(kw):
        raise _client_error("AccessDenied", "DeleteObject")

    w.tap.rule("delete_object", lambda kw: kw.get("VersionId") == latest, deny, when="before")
    with pytest.raises(OwnCloudPermissionError):
        _apply(w)
    assert _exists(s3v, key) and _chain(s3v, key) == [(latest, True, False)]  # only the noncurrent entries went
    receipts = [k for op, k in w.tap.calls if op == "put_object" and k and k.endswith("/RECEIPT.json")]
    final = json.loads(_body(s3v, receipts[-1]))
    assert final["status"] == "stopped: OwnCloudPermissionError" and final["deleted"] == []


def test_a_refused_first_delete_leaves_the_whole_chain(s3v, tmp_path, gc_on):
    from owncloud_backend import OwnCloudPermissionError  # noqa: PLC0415
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    key = _seg(w, name)
    _grow_chain(w, name)
    before = _chain(s3v, key)

    def deny(kw):
        raise _client_error("AccessDenied", "DeleteObject")

    w.tap.rule("delete_object", lambda kw: True, deny, when="before")
    with pytest.raises(OwnCloudPermissionError):
        _apply(w)
    assert _chain(s3v, key) == before


def test_an_empty_chain_on_a_versioned_store_keeps_the_name_and_never_becomes_a_key_delete(s3v, tmp_path, gc_on):
    """The versioned branch is chosen by the archive GET's VersionId, not by what the listing returned: a listing
    that comes back empty must keep the orphan, because the key delete it would fall back to hides whatever is
    current (and a version a writer put since)."""
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    key = _seg(w, name)
    before = _chain(s3v, key)
    w.tap.rule("list_object_versions", lambda kw: True,
               lambda kw: {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}, when="instead")
    out = _apply(w)
    assert out.deleted == [] and out.skipped == {name: "rewritten-since-listing"} and name in out.ledger
    assert s3v.deletes == [] and _chain(s3v, key) == before and _exists(s3v, key)


def test_a_version_delete_the_store_does_not_honour_is_reported_not_counted(s3v, tmp_path, gc_on):
    """A store that accepts the call and leaves the version in place has not deleted it: the read-back names the
    orphan 'delete-not-effective', it stays in the ledger, and the object is still there."""
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    key = _seg(w, name)
    latest = _latest(s3v, key)
    w.tap.rule("delete_object", lambda kw: kw.get("VersionId") == latest, lambda kw: {}, when="instead")
    out = _apply(w)
    assert out.deleted == [] and out.skipped == {name: "delete-not-effective"} and name in out.ledger
    assert _exists(s3v, key) and _chain(s3v, key) == [(latest, True, False)]


def test_a_version_the_store_reports_already_gone_is_not_an_error(s3v, tmp_path, gc_on):
    """The version was removed and the store says NoSuchVersion (a second remover got there first): the state asked
    for is reached, so the pass goes on and the read-back judges the outcome."""
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    key = _seg(w, name)
    _grow_chain(w, name)
    first = next(v for v, latest, marker in _chain(s3v, key) if not latest and not marker)

    def gone(kw):
        raise _client_error("NoSuchVersion", "DeleteObject")

    w.tap.rule("delete_object", lambda kw: kw.get("VersionId") == first, gone, when="after")
    out = _apply(w)
    assert out.stopped is None and out.deleted == [name] and out.skipped == {}
    assert _chain(s3v, key) == [] and not _exists(s3v, key)


# --- 4. the gaps ------------------------------------------------------------------------------------------------
def test_a_version_put_after_the_recheck_survives_the_delete(s3v, tmp_path, gc_on):
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    key = _seg(w, name)
    w.tap.rule("delete_object", lambda kw: True, lambda kw: _put_again(w, name), when="before")
    out = _apply(w)
    assert out.deleted == [] and out.skipped == {name: "rewritten-since-listing"} and name in out.ledger
    assert _exists(s3v, key) and len(_chain(s3v, key)) == 1  # the listed version went, the re-PUT is current


def test_the_same_late_put_is_lost_on_an_unversioned_store(s3u, tmp_path, gc_on):
    """The control for the pin above: with no version id to tell the re-PUT apart, the key delete removes it."""
    w = _world(tmp_path, s3u)
    name = w.orphans[0]
    w.tap.rule("delete_object", lambda kw: True, lambda kw: _put_again(w, name), when="before")
    out = _apply(w)
    assert out.deleted == [name] and not _exists(s3u, _seg(w, name))


def test_a_version_put_before_the_recheck_keeps_the_name_and_deletes_nothing(s3v, tmp_path, gc_on):
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    key = _seg(w, name)
    w.tap.rule("head_object", _key_is(key), lambda kw: _put_again(w, name), when="before")
    out = _apply(w)
    assert out.deleted == [] and out.skipped == {name: "rewritten-since-listing"} and name in out.ledger
    assert s3v.deletes == [] and "delete_object" not in w.tap.ops()
    assert len(_chain(s3v, key)) == 2


def test_a_re_put_between_the_two_listings_is_caught_by_its_time(s3v, tmp_path, gc_on):
    """The HEAD agrees with the version listing here (both see the re-PUT), so only the listed time can tell."""
    if not _is_mem(s3v):
        pytest.skip("the original is aged in the in-memory double: moto stamps last_modified at PUT time")
    w = _world(tmp_path, s3v)
    name = w.orphans[0]
    _age_current(w, name)
    w.tap.rule("list_object_versions", lambda kw: True, lambda kw: _put_again(w, name), when="before")
    out = _apply(w)
    assert out.deleted == [] and out.skipped == {name: "rewritten-since-listing"}
    assert s3v.deletes == [] and len(_chain(s3v, _seg(w, name))) == 2


# --- 5. bytes the archive does not hold -------------------------------------------------------------------------
def test_a_noncurrent_version_with_other_bytes_keeps_the_whole_name(s3v, tmp_path, gc_on):
    w = _world(tmp_path, s3v)
    good = b'{"id": "g-9-1"}\n'
    stray = "asp-9/0.%s.jsonl" % hashlib.md5(good).hexdigest()
    key = _seg(w, stray)
    w.s3.put_object(Bucket=BUCKET, Key=key, Body=b"not what the name says\n")  # v1: bytes the archive will not hold
    w.s3.put_object(Bucket=BUCKET, Key=key, Body=good)  # v2: current, and what the name says
    before = _chain(s3v, key)
    w.s3.deletes.clear()
    out = _apply(w, ledger=_aged(w, w.orphans + [stray]))
    assert out.skipped == {stray: "version-bytes-not-archived"} and out.deleted == w.orphans
    assert _chain(s3v, key) == before and not [d for d in s3v.deletes if d["Key"] == key]
    assert stray in out.ledger  # kept, and reported on every pass until a person looks


# --- 6. an unversioned store ------------------------------------------------------------------------------------
def test_an_unversioned_store_keeps_the_key_delete_and_lists_no_versions(s3u, tmp_path, gc_on):
    w = _world(tmp_path, s3u)
    name = w.orphans[0]
    out = _apply(w)
    assert out.deleted == [name]
    assert "list_object_versions" not in w.tap.ops()
    assert [d for d in s3u.deletes if d["Key"] == _seg(w, name)] == [{"Bucket": BUCKET, "Key": _seg(w, name)}]
    rec = json.loads(_body(s3u, comp.gc_receipt_key(ENV_ROOT, out.run_id)))["objects"][name]
    assert rec["versions"] == [] and rec["version_id"] is None
