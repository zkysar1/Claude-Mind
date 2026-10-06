"""Tests for the archive PRUNE EXECUTOR of the composite goal-queue layout ( U30).

THE SEAMS. Pure: `plan_run_removal`, `gc_run_prefix`, `gc_pruned_key`, `gc_prune_control_key`, `should_prune_archive`.
Backend: OwnCloudBackend.composite_gc_prune_enumerate (read-only), composite_gc_prune_control (the pre-batch control) and
composite_gc_prune_apply (the removal, behind its own default-OFF flag). The WHICH-RUNS planner is pinned in
test_owncloud_composite_gc_prune_g358202; this file pins what the executor does with its answer.

WHAT THESE PINS ARE FOR. The executor DELETES the only copy of what a versioned delete removed, so each pin is a way it could
remove something it must not, paired with a control that does remove, so a test can fail (guard-4166): every refusal is
paired with the unchanged archive, every absence claim with a tap that demonstrably records the call it says is missing, and
the ORDER claims (objects, then tombstone, then receipt) run against the tap's call log. The archive is listed through
`Delimiter`, which the in-memory double does not model, so the prune scenarios run on moto with bucket versioning on (what
production is: a delete leaves a delete marker); the control, which lists no archive, also runs on both doubles and on an
unversioned bucket, where it must refuse.

Coverage:
  1. the run planner: scoped by the receipt, never the directory (foreign key, wrong size, key outside the run, malformed)
  2. keys and flag: only a run id or a token makes a key; the flag is its own
  3. enumerate: read-only, needs no flag, reads only old receipts, honors `needed`, reports what is not a run
  4. apply: inert by default; removes in the protocol's order; leaves the bytes recoverable; stops or skips on each anomaly;
     a pass that dies anywhere is finished by the next
  5. the control: a versioned delete is proven recoverable and cleaned up; every miss stops the pass

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
import types
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core" / "scripts"), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _owncloud_composite as comp  # noqa: E402
from test_owncloud_composite_gc_apply_g358202 import (  # noqa: E402
    DAY, ENV_ID, ENV_ROOT, REL, _Tap, _aged, _body, _commit_head_of, _exists, _seg, _world,
)
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401  (_isolate is the shared autouse fixture)
    BUCKET, REGION, _MemS3, _Stream, _backend, _client_error, _isolate,
)

RUN = "20260901T000000Z-0123abcd"
OTHER_RUN = "20260801T000000Z-feedbeef"


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    for name in (comp.GC_FLAG_ENV, comp.GC_PRUNE_FLAG_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def prune_on(monkeypatch):
    monkeypatch.setenv(comp.GC_PRUNE_FLAG_ENV, ENV_ID)


def _moto_client(monkeypatch, versioned):
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
        yield client


def _store(param, monkeypatch):
    if param.startswith("mem"):
        yield _MemS3(versioned=param.endswith("versioned"))
        return
    yield from _moto_client(monkeypatch, param.endswith("versioned"))


@pytest.fixture
def s3(monkeypatch):
    yield from _moto_client(monkeypatch, True)


@pytest.fixture
def s3_unversioned(monkeypatch):
    yield from _moto_client(monkeypatch, False)


@pytest.fixture(params=["mem", "mem-versioned", "moto", "moto-versioned"])
def any_s3(request, monkeypatch):
    yield from _store(request.param, monkeypatch)


@pytest.fixture(params=["mem-versioned", "moto-versioned"])
def ver_s3(request, monkeypatch):
    yield from _store(request.param, monkeypatch)


# ---- the pure fixtures -----------------------------------------------------------------------------------------------
def _receipt(run_id=RUN, n=3):
    objs = {}
    for i in range(n):
        name = "1/%d.%032x.jsonl" % (i, i)
        objs[name] = {"archive_key": comp.gc_archive_key(ENV_ROOT, run_id, REL, name), "size": 100 + i, "md5": "0" * 32}
    return {"kind": "composite-gc-archive", "format": 1, "run_id": run_id, "store": REL, "status": "done", "objects": objs}


def _listing_of(receipt, run_id=RUN):
    out = {comp.gc_receipt_key(ENV_ROOT, run_id): 900}
    out.update({r["archive_key"]: r["size"] for r in receipt["objects"].values()})
    return out


def _removal(receipt, listing, run_id=RUN):
    return comp.plan_run_removal(run_id, receipt, listing, ENV_ROOT)


def _first(receipt):
    return next(iter(receipt["objects"].values()))


# ---- 1. the run planner ----------------------------------------------------------------------------------------------
def test_a_run_whose_listing_matches_its_receipt_goes_object_by_object_and_its_receipt_is_not_among_them():
    r = _receipt()
    out = _removal(r, _listing_of(r))
    assert out.keep is None and out.gone == []
    assert out.delete == sorted(x["archive_key"] for x in r["objects"].values()) and len(out.delete) == 3
    assert comp.gc_receipt_key(ENV_ROOT, RUN) not in out.delete


def test_a_key_the_receipt_does_not_name_keeps_the_whole_run():
    r = _receipt()
    listing = _listing_of(r)
    assert _removal(r, listing).keep is None  # control: the same run without the stray key is removable
    listing[comp.gc_run_prefix(ENV_ROOT, RUN) + "objects/stray.txt"] = 5
    out = _removal(r, listing)
    assert out.keep == "foreign-key-in-run" and out.delete == []


@pytest.mark.parametrize("delta", [1, -1])
def test_an_object_that_is_not_the_size_its_receipt_says_keeps_the_whole_run(delta):  # larger or smaller: either way it is not what was archived
    r = _receipt()
    listing = _listing_of(r)
    listing[_first(r)["archive_key"]] += delta
    out = _removal(r, listing)
    assert out.keep == "archive-size-differs" and out.delete == []


def test_a_key_the_receipt_names_and_the_listing_lacks_is_gone_and_the_rest_still_go():
    r = _receipt()
    listing = _listing_of(r)
    missing = _first(r)["archive_key"]
    del listing[missing]
    out = _removal(r, listing)
    assert out.keep is None and out.gone == [missing] and missing not in out.delete and len(out.delete) == 2


def test_a_run_whose_receipt_is_not_in_its_listing_stays():
    r = _receipt()
    listing = _listing_of(r)
    del listing[comp.gc_receipt_key(ENV_ROOT, RUN)]
    assert _removal(r, listing).keep == "receipt-not-listed"


@pytest.mark.parametrize("target", [
    comp.gc_archive_key(ENV_ROOT, OTHER_RUN, REL, "1/0.%032x.jsonl" % 0),  # another run's object
    ENV_ID + "/" + REL,                                                    # the live head itself
    ENV_ROOT + "world/knowledge/tree/_tree.yaml",                          # anything else in the bucket
    comp.gc_run_prefix(ENV_ROOT, RUN) + "RECEIPT.json",                    # its own receipt, which goes last and by name
])
def test_a_receipt_that_names_a_key_outside_the_runs_objects_directory_is_never_followed(target):
    r = _receipt()
    _first(r)["archive_key"] = target
    listing = _listing_of(r)
    listing[target] = _first(r)["size"]
    out = _removal(r, listing)
    assert out.keep == "receipt-names-foreign-key" and out.delete == []


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("objects"),
    lambda r: r.update(objects=[]),
    lambda r: r["objects"].update(bad="not a record"),
    lambda r: _first(r).pop("size"),
    lambda r: _first(r).update(size="100"),
    lambda r: _first(r).update(size=True),
    lambda r: _first(r).pop("archive_key"),
    lambda r: _first(r).update(archive_key=7),
])
def test_a_receipt_whose_records_are_not_all_a_key_and_an_integer_size_stays(mutate):
    r = _receipt()
    mutate(r)
    assert _removal(r, _listing_of(_receipt())).keep == "receipt-malformed"


def test_a_run_whose_receipt_names_no_objects_has_nothing_to_delete_and_may_go():
    r = _receipt(n=0)
    out = _removal(r, _listing_of(r))
    assert out.keep is None and out.delete == [] and out.gone == []


def test_the_run_planner_does_not_mutate_its_inputs():
    r = _receipt()
    listing = _listing_of(r)
    before = (json.dumps(r, sort_keys=True), dict(listing))
    _removal(r, listing)
    assert (json.dumps(r, sort_keys=True), listing) == before


# ---- 2. keys and flag ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "_state", "_pruned", "../x", RUN + "/..", RUN.upper(), "20260901T000000Z-0123abc", None, 7])
def test_a_name_that_is_not_a_run_id_never_makes_a_prefix_or_a_tombstone_key(bad):
    with pytest.raises(ValueError):
        comp.gc_run_prefix(ENV_ROOT, bad)
    with pytest.raises(ValueError):
        comp.gc_pruned_key(ENV_ROOT, bad)


def test_the_run_prefix_and_the_tombstone_key_sit_where_the_design_puts_them():
    assert comp.gc_run_prefix(ENV_ROOT, RUN) == "%s_composite-gc-archive/%s/" % (ENV_ROOT, RUN)
    assert comp.gc_pruned_key(ENV_ROOT, RUN) == "%s_composite-gc-archive/_pruned/%s/RECEIPT.json" % (ENV_ROOT, RUN)
    assert comp.gc_pruned_key(ENV_ROOT, RUN) != comp.gc_receipt_key(ENV_ROOT, RUN)


@pytest.mark.parametrize("bad", ["", "abc", "A" * 32, "g" * 32, "a" * 31, "a" * 33, "../" + "a" * 29, "a" * 32 + "\n", None, 7])
def test_only_a_32_hex_token_makes_a_control_key(bad):
    with pytest.raises(ValueError):
        comp.gc_prune_control_key(ENV_ROOT, bad)
    key = comp.gc_prune_control_key(ENV_ROOT, "ab" * 16)
    assert key == "%s_composite-gc-archive/_state/_prune-control/%s" % (ENV_ROOT, "ab" * 16)


def test_the_prune_flag_names_environments_and_is_independent_of_the_other_two():
    rel = comp.ALLOWLIST[0]
    assert not comp.should_prune_archive(rel, ENV_ID, {})
    assert comp.should_prune_archive(rel, ENV_ID, {comp.GC_PRUNE_FLAG_ENV: ENV_ID})
    assert comp.should_prune_archive(rel, ENV_ID, {comp.GC_PRUNE_FLAG_ENV: "other, %s" % ENV_ID.upper()})
    assert not comp.should_prune_archive(rel, ENV_ID, {comp.GC_PRUNE_FLAG_ENV: "other-env"})
    assert not comp.should_prune_archive(rel, ENV_ID, {comp.GC_PRUNE_FLAG_ENV: "1"})  # a legacy truthy value names no environment
    assert not comp.should_prune_archive("world/not-on-the-allowlist.jsonl", ENV_ID, {comp.GC_PRUNE_FLAG_ENV: ENV_ID})
    assert not comp.should_prune_archive(rel, ENV_ID, {comp.GC_FLAG_ENV: ENV_ID})  # licensing orphan deletion licenses no prune
    assert not comp.should_prune_archive(rel, ENV_ID, {comp.FLAG_ENV: ENV_ID})
    assert not comp.should_gc(rel, ENV_ID, {comp.GC_PRUNE_FLAG_ENV: ENV_ID})  # and the other way round
    assert len({comp.GC_PRUNE_FLAG_ENV, comp.GC_FLAG_ENV, comp.FLAG_ENV}) == 3


# ---- the backend world: a store, and archive runs aged 30, 25 and 20 days ---------------------------------------------
def _pworld(tmp_path, s3, monkeypatch, runs=1):
    """The writer's commits, then one real delete pass per run, 5 days apart, each at a clock past the grace (as the apply tests
    do: a pass whose clock is earlier than the objects' own timestamps finds them too young). `w.now` is 20 days after the newest
    run, so the runs are 20, 25 and 30 days old at it: the receipts are the ones `composite_gc_apply` writes, not look-alikes. The
    orphan-collection flag is SET here, which licenses no prune."""
    monkeypatch.setenv(comp.GC_FLAG_ENV, ENV_ID)
    w = _world(tmp_path, s3, changes=((2, 100), (1, 5), (3, 7))[:runs])
    base = time.time() + comp.GC_GRACE_S + 10
    w.now = base + (5 * (runs - 1) + 20) * DAY
    w.runs = []
    for i in range(runs):
        out = w.be.composite_gc_apply(w.p, _aged(w), base + 5 * i * DAY, max_delete=1)
        assert out.stopped is None and len(out.deleted) == 1 and out.run_id
        keys = [comp.gc_archive_key(ENV_ROOT, out.run_id, REL, n) for n in out.deleted]
        w.runs.append(types.SimpleNamespace(id=out.run_id, keys=keys, bodies={k: _body(s3, k) for k in keys},
                                            receipt=comp.gc_receipt_key(ENV_ROOT, out.run_id),
                                            tombstone=comp.gc_pruned_key(ENV_ROOT, out.run_id)))
    return w


def _enum(w, now=None, **kw):
    return w.be.composite_gc_prune_enumerate(w.p, w.now if now is None else now, **kw)


def _prune(w, now=None, **kw):
    return w.be.composite_gc_prune_apply(w.p, w.now if now is None else now, **kw)


def _writes(ops):
    return [o for o in ops if o in ("put_object", "delete_object", "delete_objects", "copy_object")]


def _tomb(w, run):
    return json.loads(_body(w.s3, run.tombstone))


def _boom(code="InternalError", op="Injected"):
    def fn(kw):
        raise _client_error(code, op)
    return fn


def _is(key):
    return lambda kw: kw.get("Key") == key


def _is_control(kw):
    return "_prune-control" in kw.get("Key", "")


# ---- 3. enumerate ----------------------------------------------------------------------------------------------------
def test_enumerate_names_the_run_and_what_it_may_remove_and_writes_nothing(tmp_path, s3, monkeypatch):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    mark = len(w.tap.calls)
    en = _enum(w)
    assert en.plan.refused == [] and en.plan.prune == [run.id] and en.needed_why is None
    assert en.runs[run.id].keep is None and en.runs[run.id].delete == sorted(run.keys) and en.runs[run.id].gone == []
    ops = w.tap.ops(mark)
    assert "list_objects_v2" in ops and "get_object" in ops and _writes(ops) == []
    assert _exists(s3, run.receipt) and all(_exists(s3, k) for k in run.keys)
    w.tap.calls.clear()  # control: the same tap DOES record a write when the executor writes
    monkeypatch.setenv(comp.GC_PRUNE_FLAG_ENV, ENV_ID)
    _prune(w)
    assert "put_object" in _writes(w.tap.ops()) and "delete_object" in _writes(w.tap.ops())


def test_enumerate_reads_the_receipt_of_a_run_only_once_it_is_old_enough_and_the_boundary_is_inclusive(tmp_path, s3, monkeypatch):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    when = comp.gc_run_time(run.id)
    mark = len(w.tap.calls)
    young = _enum(w, now=when + comp.GC_PRUNE_AFTER_S - 1)
    assert young.plan.prune == [] and young.plan.kept == {run.id: "inside-retention"}
    assert ("get_object", run.receipt) not in w.tap.calls[mark:]
    mark = len(w.tap.calls)
    exact = _enum(w, now=when + comp.GC_PRUNE_AFTER_S)
    assert exact.plan.prune == [run.id] and ("get_object", run.receipt) in w.tap.calls[mark:]


def test_a_head_that_names_an_archived_object_the_store_lacks_keeps_its_run(tmp_path, s3, monkeypatch):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    assert _enum(w).plan.prune == [run.id]  # control: with the head as the writer left it the run is a candidate
    _commit_head_of(w, w.raw0)  # a head that names the collected orphan again, which only the archive can still supply
    en = _enum(w)
    assert en.plan.prune == [] and en.plan.kept == {run.id: "head-needs-archived-object"}


@pytest.mark.parametrize("how", ["missing", "not-a-head"])
def test_a_head_that_cannot_be_read_as_a_composite_head_refuses_the_pass_and_says_why(tmp_path, s3, monkeypatch, how):
    w = _pworld(tmp_path, s3, monkeypatch)
    if how == "missing":
        s3.delete_object(Bucket=BUCKET, Key=w.key)
    else:
        s3.put_object(Bucket=BUCKET, Key=w.key, Body=w.raw)
    en = _enum(w)
    assert en.plan.refused == ["needed-unknown"] and en.plan.prune == []
    assert en.needed_why == ("head-missing" if how == "missing" else "head-not-composite")


def test_enumerate_of_a_store_off_the_allowlist_makes_no_s3_call(tmp_path, s3, monkeypatch):
    w = _pworld(tmp_path, s3, monkeypatch)
    mark = len(w.tap.calls)
    en = w.be.composite_gc_prune_enumerate(tmp_path / "world" / "not-on-the-allowlist.jsonl", w.now)
    assert en.plan.refused == ["store-not-allowlisted"] and en.runs == {} and w.tap.calls[mark:] == []


def test_what_is_not_a_run_is_reported_or_skipped_and_never_planned(tmp_path, s3, monkeypatch):
    w = _pworld(tmp_path, s3, monkeypatch)
    base = ENV_ROOT + comp.GC_ARCHIVE_DIR + "/"
    s3.put_object(Bucket=BUCKET, Key=base + "_state/world/aspirations.jsonl/state.json", Body=b"{}")
    s3.put_object(Bucket=BUCKET, Key=base + "_pruned/%s/RECEIPT.json" % OTHER_RUN, Body=b"{}")
    s3.put_object(Bucket=BUCKET, Key=base + "stray-dir/file.txt", Body=b"x")
    en = _enum(w)
    assert en.plan.prune == [w.runs[0].id] and en.plan.unknown == ["stray-dir"] and en.plan.counts["reserved"] == 2


@pytest.mark.parametrize("how", ["absent", "not-json", "not-an-object"])
def test_a_run_whose_receipt_is_missing_or_is_not_a_receipt_stays_and_a_pass_writes_nothing(tmp_path, s3, monkeypatch, prune_on, how):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    assert _enum(w).plan.prune == [run.id]  # control: with its receipt as the writer left it the run is a candidate
    if how == "absent":
        s3.delete_object(Bucket=BUCKET, Key=run.receipt)
    else:
        s3.put_object(Bucket=BUCKET, Key=run.receipt, Body=b"this is not json" if how == "not-json" else b'["a", "list"]')
    en = _enum(w)
    assert en.plan.prune == [] and en.plan.kept == {run.id: "no-receipt"} and en.runs == {}
    mark = len(w.tap.calls)
    out = _prune(w)
    assert out.stopped is None and out.pruned == [] and out.control is None
    assert _writes(w.tap.ops(mark)) == [] and all(_exists(s3, k) for k in run.keys)


@pytest.mark.parametrize("code", ["InternalError", "AccessDenied"])
def test_a_receipt_that_could_not_be_read_is_not_a_receipt_that_is_not_there(tmp_path, s3, monkeypatch, code):
    w = _pworld(tmp_path, s3, monkeypatch)
    w.tap.rule("get_object", _is(w.runs[0].receipt), _boom(code), when="before")
    expected = sys.modules[type(w.be).__module__].OwnCloudPermissionError if code == "AccessDenied" else Exception
    with pytest.raises(expected) as seen:
        _enum(w)
    assert code == "AccessDenied" or "InternalError" in str(seen.value)  # an error from S3 propagates as itself, never as 'no-receipt'


def test_a_run_listing_is_read_to_its_end_across_pages_and_carries_the_token(tmp_path, s3, monkeypatch):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    prefix = comp.gc_run_prefix(ENV_ROOT, run.id)
    real = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)["Contents"]
    assert len(real) == 2  # control: the receipt and the one archived object, one per page below
    seen = []
    is_run = lambda kw: kw.get("Prefix") == prefix  # noqa: E731

    def page_one(kw):
        seen.append(dict(kw))
        return {"Contents": real[:1], "IsTruncated": True, "NextContinuationToken": "t1"}

    def page_two(kw):
        seen.append(dict(kw))
        assert kw.get("ContinuationToken") == "t1", "the second request must carry the first page's token"
        return {"Contents": real[1:], "IsTruncated": False}
    w.tap.rule("list_objects_v2", is_run, page_one, when="instead")
    w.tap.rule("list_objects_v2", is_run, page_two, when="instead")
    en = _enum(w)
    assert en.runs[run.id].keep is None and en.runs[run.id].delete == sorted(run.keys) and en.runs[run.id].gone == []
    assert len(seen) == 2 and "ContinuationToken" not in seen[0]


def test_a_run_listing_that_says_it_is_truncated_without_a_token_is_an_error_not_a_short_answer(tmp_path, s3, monkeypatch):
    w = _pworld(tmp_path, s3, monkeypatch)
    prefix = comp.gc_run_prefix(ENV_ROOT, w.runs[0].id)
    w.tap.rule("list_objects_v2", lambda kw: kw.get("Prefix") == prefix, lambda kw: {"Contents": [], "IsTruncated": True}, when="instead")
    with pytest.raises(comp.CompositeError):
        _enum(w)


# ---- 4. apply --------------------------------------------------------------------------------------------------------
def test_the_pass_is_inert_without_its_own_flag_and_makes_no_s3_call(tmp_path, s3, monkeypatch):
    w = _pworld(tmp_path, s3, monkeypatch)  # the orphan-collection flag is set: it licenses no prune
    mark = len(w.tap.calls)
    out = _prune(w)
    assert out.stopped == "prune-not-enabled" and out.plan is None and out.control is None and out.pruned == []
    assert w.tap.calls[mark:] == []
    assert _exists(s3, w.runs[0].receipt) and all(_exists(s3, k) for k in w.runs[0].keys)
    monkeypatch.setenv(comp.GC_PRUNE_FLAG_ENV, ENV_ID)
    assert _prune(w).pruned == [w.runs[0].id]  # control: the same call with its flag set does remove


def test_a_pass_removes_a_run_past_its_window_and_leaves_a_tombstone_in_its_place(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    assert _exists(s3, run.receipt) and all(_exists(s3, k) for k in run.keys)  # control: all there before
    out = _prune(w)
    assert out.stopped is None and out.pruned == [run.id] and out.skipped == {} and out.control.ok
    assert not _exists(s3, run.receipt) and not any(_exists(s3, k) for k in run.keys)
    tomb = _tomb(w, run)
    assert (tomb["kind"], tomb["format"], tomb["status"], tomb["run_id"], tomb["store"]) == \
        ("composite-gc-archive-pruned", 1, "pruned", run.id, REL)
    assert sorted(r["archive_key"] for r in tomb["objects"].values()) == sorted(run.keys)
    assert sorted(tomb["removed"]) == sorted(run.keys) and all(tomb["removed"].values()) and tomb["gone"] == []
    assert tomb["control"] == out.control.evidence and tomb["window_s"] == comp.GC_PRUNE_AFTER_S
    assert _exists(s3, w.key) and all(_exists(s3, _seg(w, n)) for n in w.final)  # the live store is untouched


def test_the_tombstone_keeps_each_removed_objects_record_whole_so_a_restore_can_be_checked(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    before = json.loads(_body(s3, run.receipt))
    assert before["objects"]
    assert _prune(w).pruned == [run.id]
    tomb = _tomb(w, run)
    fields = ("archive_key", "source_key", "size", "md5", "plain_md5", "etag")
    assert set(tomb["objects"]) == set(before["objects"])
    for name, rec in before["objects"].items():
        assert {k: tomb["objects"][name][k] for k in fields} == {k: rec.get(k) for k in fields}
        assert tomb["objects"][name]["md5"]  # control: the receipt does carry a checksum for the tombstone to keep
    assert tomb["pruned_at"] == time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(w.now))


def test_a_key_an_earlier_pass_already_removed_is_listed_as_gone_in_the_tombstone_and_is_not_deleted_again(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    s3.delete_object(Bucket=BUCKET, Key=run.keys[0])  # what a pass that died after deleting the object and before the tombstone left
    mark = len(w.tap.calls)
    out = _prune(w)
    assert out.stopped is None and out.pruned == [run.id]
    tomb = _tomb(w, run)
    assert tomb["gone"] == run.keys and tomb["removed"] == {} and not _exists(s3, run.receipt)
    assert [k for o, k in w.tap.calls[mark:] if o == "delete_object" and k in run.keys] == []


def test_what_a_pass_removes_stays_recoverable_as_a_noncurrent_version(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    _prune(w)
    tomb = _tomb(w, run)
    for key in run.keys:
        versions = s3.list_object_versions(Bucket=BUCKET, Prefix=key)
        assert [m for m in versions.get("DeleteMarkers", []) if m["Key"] == key and m["IsLatest"]], key  # a plain delete
        assert s3.get_object(Bucket=BUCKET, Key=key, VersionId=tomb["removed"][key])["Body"].read() == run.bodies[key]


def test_the_order_is_control_then_objects_then_tombstone_then_receipt(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    mark = len(w.tap.calls)
    _prune(w)
    calls = w.tap.calls[mark:]

    def at(op, pred):
        return [i for i, (o, k) in enumerate(calls) if o == op and pred(k)]
    control_put, objects = at("put_object", lambda k: "_prune-control" in k), at("delete_object", lambda k: k in run.keys)
    tomb_put, receipt_del = at("put_object", lambda k: k == run.tombstone), at("delete_object", lambda k: k == run.receipt)
    assert len(control_put) >= 1 and len(objects) == len(run.keys) and len(tomb_put) == 1 and len(receipt_del) == 1
    assert max(control_put) < min(objects) <= max(objects) < tomb_put[0] < receipt_del[0]
    assert "delete_objects" not in w.tap.ops(mark)  # single calls, never a batch


def test_a_second_pass_has_nothing_to_remove_and_writes_nothing(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    assert _prune(w).pruned == [w.runs[0].id]
    mark = len(w.tap.calls)
    out = _prune(w)
    assert out.stopped is None and out.pruned == [] and out.control is None and out.plan.prune == []
    assert _writes(w.tap.ops(mark)) == [] and _exists(s3, w.runs[0].tombstone)


def test_a_window_under_the_floor_is_refused_and_nothing_is_removed(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    mark = len(w.tap.calls)
    out = _prune(w, prune_after_s=comp.GC_PRUNE_AFTER_S - 1)
    assert out.stopped == "retention-below-floor" and out.pruned == []
    assert _writes(w.tap.ops(mark)) == [] and _exists(s3, w.runs[0].receipt)


def test_the_cap_bounds_a_pass_and_the_oldest_runs_go_first(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch, runs=3)
    ids = [r.id for r in w.runs]
    assert ids == sorted(ids)  # control: the fixture's runs are in age order, oldest first
    out = _prune(w, max_runs=2)
    assert out.stopped is None and out.pruned == ids[:2] and out.plan.kept == {ids[2]: "over-cap"}
    assert all(_exists(s3, k) for k in w.runs[2].keys) and _exists(s3, w.runs[2].receipt)
    assert _prune(w).pruned == ids[2:]  # the next pass takes the rest


def test_a_foreign_key_in_a_run_keeps_that_run_and_the_pass_goes_on_to_the_next(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch, runs=2)
    stray = comp.gc_run_prefix(ENV_ROOT, w.runs[0].id) + "objects/stray.txt"
    s3.put_object(Bucket=BUCKET, Key=stray, Body=b"someone else's")
    out = _prune(w)
    assert out.stopped is None and out.pruned == [w.runs[1].id] and out.skipped == {w.runs[0].id: "foreign-key-in-run"}
    assert all(_exists(s3, k) for k in w.runs[0].keys) and _exists(s3, w.runs[0].receipt) and _exists(s3, stray)
    s3.delete_object(Bucket=BUCKET, Key=stray)  # control: the stray key alone was what kept it
    assert _prune(w).pruned == [w.runs[0].id]


def test_a_pass_whose_every_planned_run_is_kept_at_the_removal_never_reaches_the_control(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    s3.put_object(Bucket=BUCKET, Key=comp.gc_run_prefix(ENV_ROOT, run.id) + "objects/stray.txt", Body=b"someone else's")
    mark = len(w.tap.calls)
    out = _prune(w)
    assert out.stopped is None and out.pruned == [] and out.skipped == {run.id: "foreign-key-in-run"}
    assert out.control is None and _writes(w.tap.ops(mark)) == []  # not even its sentinel: the control needs ListBucketVersions


@pytest.mark.parametrize("how, why", [("missing", "head-missing"), ("not-a-head", "head-not-composite")])
def test_a_pass_whose_enumeration_cannot_say_what_the_head_needs_stops_and_names_why(tmp_path, s3, monkeypatch, prune_on, how, why):
    w = _pworld(tmp_path, s3, monkeypatch)
    if how == "missing":
        s3.delete_object(Bucket=BUCKET, Key=w.key)
    else:
        s3.put_object(Bucket=BUCKET, Key=w.key, Body=w.raw)
    mark = len(w.tap.calls)
    out = _prune(w)
    assert out.stopped == "needed-unknown: %s" % why and out.pruned == [] and out.control is None
    assert _writes(w.tap.ops(mark)) == [] and all(_exists(s3, k) for k in w.runs[0].keys) and _exists(s3, w.runs[0].receipt)


def test_an_archived_object_that_is_not_the_size_its_receipt_says_keeps_its_run(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    s3.put_object(Bucket=BUCKET, Key=run.keys[0], Body=run.bodies[run.keys[0]] + b"!")
    out = _prune(w)
    assert out.pruned == [] and out.skipped == {run.id: "archive-size-differs"} and _exists(s3, run.receipt)
    assert not _exists(s3, run.tombstone)


def test_a_head_that_begins_naming_an_archived_object_after_the_enumeration_keeps_its_run(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("put_object", _is_control, lambda kw: _commit_head_of(w, w.raw0), when="before")  # between the plan and the removal
    out = _prune(w)
    assert out.stopped is None and out.pruned == [] and out.skipped == {run.id: "head-needs-archived-object"}
    assert all(_exists(s3, k) for k in run.keys) and _exists(s3, run.receipt) and not _exists(s3, run.tombstone)


def test_a_head_that_vanishes_after_the_enumeration_stops_the_pass_before_any_removal(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("put_object", _is_control, lambda kw: s3.delete_object(Bucket=BUCKET, Key=w.key), when="before")
    out = _prune(w)
    assert out.stopped == "needed-unknown: head-missing" and out.pruned == []
    assert all(_exists(s3, k) for k in run.keys) and _exists(s3, run.receipt)


def test_an_object_that_changes_between_the_listing_and_its_delete_stops_the_pass_with_the_receipt_in_place(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("head_object", _is(run.keys[0]), lambda kw: s3.put_object(Bucket=BUCKET, Key=run.keys[0], Body=b"changed"), when="before")
    out = _prune(w)
    assert out.stopped == "run-stopped: changed-since-listing" and out.pruned == []
    assert _exists(s3, run.receipt) and not _exists(s3, run.tombstone) and _exists(s3, run.keys[0])


def test_a_delete_that_does_not_take_effect_stops_the_pass_and_is_not_counted(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("delete_object", _is(run.keys[0]), lambda kw: {}, when="instead")  # S3 says 204 and nothing happens
    out = _prune(w)
    assert out.stopped == "run-stopped: delete-not-effective" and out.pruned == []
    assert _exists(s3, run.keys[0]) and _exists(s3, run.receipt) and not _exists(s3, run.tombstone)


def test_a_pass_that_cannot_write_the_tombstone_keeps_the_receipt_and_the_next_pass_finishes_the_run(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("put_object", _is(run.tombstone), _boom(), when="before")
    with pytest.raises(Exception):
        _prune(w)
    assert not any(_exists(s3, k) for k in run.keys)  # the objects are gone ...
    assert _exists(s3, run.receipt) and not _exists(s3, run.tombstone)  # ... and the record of them is not
    out = _prune(w)  # the receipt still reads done and names keys that are already gone, which is fine
    assert out.stopped is None and out.pruned == [run.id] and _exists(s3, run.tombstone) and not _exists(s3, run.receipt)
    assert _tomb(w, run)["removed"] == {} and sorted(_tomb(w, run)["gone"]) == sorted(run.keys)


def test_a_pass_that_cannot_release_the_receipt_leaves_the_tombstone_and_the_next_pass_finishes_the_run(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("delete_object", _is(run.receipt), _boom(), when="before")
    with pytest.raises(Exception):
        _prune(w)
    assert _exists(s3, run.tombstone) and _exists(s3, run.receipt)
    out = _prune(w)
    assert out.pruned == [run.id] and not _exists(s3, run.receipt) and _tomb(w, run)["status"] == "pruned"


def test_a_tombstone_rewritten_after_a_death_before_the_receipt_went_keeps_the_version_ids_of_the_first(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("delete_object", _is(run.receipt), _boom(), when="before")
    with pytest.raises(Exception):
        _prune(w)
    first = _tomb(w, run)
    assert sorted(first["removed"]) == sorted(run.keys) and all(first["removed"].values()) and first["gone"] == []  # control: the first holds them
    assert _prune(w).pruned == [run.id]  # the objects are gone, so this pass deletes nothing and rewrites the tombstone
    again = _tomb(w, run)
    assert again["removed"] == first["removed"] and again["gone"] == [] and not _exists(s3, run.receipt)
    for key in run.keys:  # and the way back is intact: each version id still reads the archived bytes
        assert s3.get_object(Bucket=BUCKET, Key=key, VersionId=again["removed"][key])["Body"].read() == run.bodies[key]


@pytest.mark.parametrize("earlier", [b"this is not json", b'["a", "list"]', b'{"removed": "not a map"}'])
def test_an_earlier_tombstone_that_is_not_a_removed_map_is_replaced_without_ceremony(tmp_path, s3, monkeypatch, prune_on, earlier):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    s3.put_object(Bucket=BUCKET, Key=run.tombstone, Body=earlier)
    out = _prune(w)
    assert out.stopped is None and out.pruned == [run.id]
    tomb = _tomb(w, run)
    assert tomb["status"] == "pruned" and sorted(tomb["removed"]) == sorted(run.keys) and tomb["gone"] == []


@pytest.mark.parametrize("code", ["InternalError", "AccessDenied"])
def test_an_earlier_tombstone_that_cannot_be_read_stops_the_run_before_anything_is_deleted(tmp_path, s3, monkeypatch, prune_on, code):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("get_object", _is(run.tombstone), _boom(code), when="before")
    expected = sys.modules[type(w.be).__module__].OwnCloudPermissionError if code == "AccessDenied" else Exception
    with pytest.raises(expected):
        _prune(w)
    assert all(_exists(s3, k) for k in run.keys) and _exists(s3, run.receipt) and not _exists(s3, run.tombstone)
    assert _prune(w).pruned == [run.id]  # control: the rule fired once, and the same run goes at the next pass


def test_the_pass_does_not_touch_its_own_reserved_names_or_a_stray_directory(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    base = ENV_ROOT + comp.GC_ARCHIVE_DIR + "/"
    keep = {base + "_state/world/aspirations.jsonl/state.json": b'{"a":1}', base + "stray-dir/file.txt": b"x"}
    for k, v in keep.items():
        s3.put_object(Bucket=BUCKET, Key=k, Body=v)
    out = _prune(w)
    assert out.pruned == [w.runs[0].id] and out.plan.unknown == ["stray-dir"]
    assert {k: _body(s3, k) for k in keep} == keep
    assert _exists(s3, w.runs[0].tombstone)


def test_a_store_that_cannot_prove_its_delete_recoverable_loses_nothing(tmp_path, s3_unversioned, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3_unversioned, monkeypatch)
    run = w.runs[0]
    out = _prune(w)
    assert out.stopped == "control-failed: control-store-not-versioned" and out.pruned == [] and not out.control.ok
    assert all(_exists(s3_unversioned, k) for k in run.keys) and _exists(s3_unversioned, run.receipt)
    assert not _exists(s3_unversioned, out.control.evidence["key"])  # and the sentinel it tried is gone


def test_a_foreign_key_that_appears_after_the_enumeration_keeps_the_run_at_the_removal(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    stray = comp.gc_run_prefix(ENV_ROOT, run.id) + "objects/arrived-late.txt"
    w.tap.rule("put_object", _is_control, lambda kw: s3.put_object(Bucket=BUCKET, Key=stray, Body=b"late"), when="before")
    out = _prune(w)
    assert out.stopped is None and out.pruned == [] and out.skipped == {run.id: "foreign-key-in-run"}
    assert all(_exists(s3, k) for k in run.keys) and _exists(s3, run.receipt) and _exists(s3, stray)


def test_an_object_that_vanishes_before_its_delete_is_the_state_asked_for_and_the_run_still_finishes(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("head_object", _is(run.keys[0]), lambda kw: s3.delete_object(Bucket=BUCKET, Key=run.keys[0]), when="before")
    out = _prune(w)
    assert out.stopped is None and out.pruned == [run.id] and not _exists(s3, run.receipt)
    assert run.keys[0] not in _tomb(w, run)["removed"] and run.keys[0] in {r["archive_key"] for r in _tomb(w, run)["objects"].values()}


def test_a_tombstone_that_does_not_read_back_equal_keeps_the_receipt(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    reads = {"n": 0}

    def the_readback(kw):  # the pass's first read of the tombstone key looks for an earlier tombstone; the second is the read-back
        if kw.get("Key") != run.tombstone:
            return False
        reads["n"] += 1
        return reads["n"] == 2
    w.tap.rule("get_object", the_readback, lambda kw: {"Body": _Stream(b'{"status": "something else"}')}, when="instead")
    out = _prune(w)
    assert out.stopped == "run-stopped: tombstone-readback-mismatch" and out.pruned == []
    assert _exists(s3, run.receipt)  # the receipt is released only after the tombstone reads back (guard-4747)


def test_a_receipt_delete_that_does_not_take_effect_is_not_counted(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    run = w.runs[0]
    w.tap.rule("delete_object", _is(run.receipt), lambda kw: {}, when="instead")
    out = _prune(w)
    assert out.stopped == "run-stopped: receipt-delete-not-effective" and out.pruned == []
    assert _exists(s3, run.receipt) and _exists(s3, run.tombstone)


def test_a_head_that_moves_while_needed_is_being_read_refuses_the_pass(tmp_path, s3, monkeypatch, prune_on):
    w = _pworld(tmp_path, s3, monkeypatch)
    w.tap.rule("list_objects_v2", lambda kw: kw.get("Prefix") == comp.segment_s3_key(w.key, ""), lambda kw: _commit_head_of(w, w.raw0), when="before")
    en = _enum(w)
    assert en.plan.refused == ["needed-unknown"] and en.needed_why == "head-moved-during-read" and en.plan.prune == []


@pytest.mark.parametrize("bad", [None, "14d", float("nan")])
def test_a_window_or_clock_that_is_not_a_number_is_refused_not_crashed_on(tmp_path, s3, monkeypatch, prune_on, bad):
    w = _pworld(tmp_path, s3, monkeypatch)
    mark = len(w.tap.calls)
    refused = _prune(w, prune_after_s=bad)
    assert refused.stopped == "retention-below-floor" and refused.pruned == []
    clocked = w.be.composite_gc_prune_apply(w.p, bad)  # not through _prune: None there means the fixture's clock
    assert clocked.stopped == "now-invalid" and clocked.pruned == []
    assert _writes(w.tap.ops(mark)) == [] and _exists(s3, w.runs[0].receipt)


# ---- 5. the control --------------------------------------------------------------------------------------------------
def _control(tmp_path, s3, **kw):
    tap = _Tap(s3)
    return _backend(tmp_path, tap), tap


def test_the_control_proves_a_versioned_delete_recoverable_and_leaves_nothing_behind(tmp_path, ver_s3):
    be, tap = _control(tmp_path, ver_s3)
    out = be.composite_gc_prune_control(time.time())
    ev = out.evidence
    assert out.ok and out.failed is None and ev["cleanup"] == "clean"
    assert ev["version_id"] and ev["marker_version_id"] and ev["version_id"] != ev["marker_version_id"]
    assert ev["key"].startswith("%s_composite-gc-archive/_state/_prune-control/" % ENV_ROOT)
    assert ev["size"] > 0 and len(ev["md5"]) == 32 and set(ev["md5"]) <= set("0123456789abcdef")
    ops = tap.ops()
    assert ops.count("put_object") == 2 and "list_object_versions" in ops  # the PUT and the restore
    assert ver_s3.list_object_versions(Bucket=BUCKET, Prefix=ev["key"]).get("Versions", []) == []
    assert ver_s3.list_object_versions(Bucket=BUCKET, Prefix=ev["key"]).get("DeleteMarkers", []) == []
    assert not _exists(ver_s3, ev["key"])


def test_the_control_refuses_a_store_that_does_not_version_and_still_cleans_up(tmp_path, any_s3):
    if hasattr(any_s3, "versioned"):
        versioned = any_s3.versioned
    else:
        versioned = any_s3.get_bucket_versioning(Bucket=BUCKET).get("Status") == "Enabled"
    be, tap = _control(tmp_path, any_s3)
    out = be.composite_gc_prune_control(time.time())
    if versioned:
        assert out.ok  # control: a versioned store of the same double passes
    else:
        assert not out.ok and out.failed == "control-store-not-versioned" and out.evidence["cleanup"] == "clean"
        assert not _exists(any_s3, out.evidence["key"])


def test_the_control_fails_when_the_delete_leaves_no_recoverable_version(tmp_path, ver_s3):
    be, tap = _control(tmp_path, ver_s3)

    def delete_by_version_id(kw):
        cur = ver_s3.head_object(Bucket=BUCKET, Key=kw["Key"])
        return ver_s3.delete_object(Bucket=BUCKET, Key=kw["Key"], VersionId=cur["VersionId"])
    tap.rule("delete_object", lambda kw: _is_control(kw) and "VersionId" not in kw, delete_by_version_id, when="instead")
    out = be.composite_gc_prune_control(time.time())
    assert not out.ok and out.failed == "control-delete-left-no-recoverable-version" and out.evidence["cleanup"] == "clean"


def test_the_control_fails_when_the_noncurrent_copy_does_not_read_back_as_the_original(tmp_path, ver_s3):
    be, tap = _control(tmp_path, ver_s3)
    tap.rule("get_object", lambda kw: _is_control(kw) and "VersionId" in kw, lambda kw: {"Body": _Stream(b"not the original")},
             when="instead")
    out = be.composite_gc_prune_control(time.time())
    assert not out.ok and out.failed == "control-restore-mismatch" and out.evidence["cleanup"] == "clean"


def test_the_control_fails_when_the_delete_is_not_effective(tmp_path, ver_s3):
    be, tap = _control(tmp_path, ver_s3)
    tap.rule("delete_object", lambda kw: _is_control(kw) and "VersionId" not in kw, lambda kw: {}, when="instead")
    out = be.composite_gc_prune_control(time.time())
    assert not out.ok and out.failed == "control-delete-not-effective" and out.evidence["cleanup"] == "clean"


def test_the_control_never_touches_a_key_that_already_has_a_version(tmp_path, ver_s3):
    be, tap = _control(tmp_path, ver_s3)
    token = "cd" * 16
    key = comp.gc_prune_control_key(ENV_ROOT, token)
    ver_s3.put_object(Bucket=BUCKET, Key=key, Body=b"somebody's")
    mark = len(tap.calls)
    out = be.composite_gc_prune_control(time.time(), token=token)
    assert not out.ok and out.failed == "control-key-not-fresh"
    assert _writes(tap.ops(mark)) == [] and _body(ver_s3, key) == b"somebody's"
    assert len(ver_s3.list_object_versions(Bucket=BUCKET, Prefix=key)["Versions"]) == 1


def test_the_control_reports_a_sentinel_it_could_not_remove(tmp_path, ver_s3):
    be, tap = _control(tmp_path, ver_s3)
    tap.rule("delete_object", lambda kw: _is_control(kw) and "VersionId" in kw, lambda kw: {}, when="instead")
    out = be.composite_gc_prune_control(time.time())
    assert not out.ok and out.failed == "control-cleanup-failed" and out.evidence["cleanup"] == "left"


def test_the_control_fails_when_the_restored_copy_does_not_read_back(tmp_path, ver_s3):
    be, tap = _control(tmp_path, ver_s3)
    seen = []

    def last_current_get(kw):
        if _is_control(kw) and "VersionId" not in kw:
            seen.append(1)
            return len(seen) == 2  # the first is the read-back after the PUT, the second is the one after the restore
        return False
    tap.rule("get_object", last_current_get, lambda kw: {"Body": _Stream(b"not the restored bytes")}, when="instead")
    out = be.composite_gc_prune_control(time.time())
    assert not out.ok and out.failed == "control-restored-copy-mismatch" and out.evidence["cleanup"] == "clean"


def test_the_control_refuses_a_put_answered_with_the_null_version_id(tmp_path, ver_s3):
    # A store whose versioning is suspended (or was never on) answers a PUT with VersionId "null", not with no VersionId: the literal
    # "null" must refuse by name. The real PUT still happens, so a refusal that is only a LATER miss ('delete left no recoverable
    # version') would be a different, accidental reason and fails this test.
    be, tap = _control(tmp_path, ver_s3)
    tap.rule("put_object", _is_control, lambda kw: dict(ver_s3.put_object(**kw), VersionId="null"), when="instead")
    out = be.composite_gc_prune_control(time.time())
    assert not out.ok and out.failed == "control-store-not-versioned" and out.evidence["cleanup"] == "clean"
    assert not _exists(ver_s3, out.evidence["key"])
    assert "version_id" not in out.evidence  # it stopped at the PUT, before it deleted anything


def test_the_control_refuses_a_store_whose_versioning_is_suspended(tmp_path, monkeypatch):
    for gen in (_moto_client(monkeypatch, True),):
        client = next(gen)
        client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Suspended"})
        be, _tap = _control(tmp_path, client)
        out = be.composite_gc_prune_control(time.time())
        assert not out.ok and out.failed == "control-store-not-versioned" and out.evidence["cleanup"] == "clean"
        assert not _exists(client, out.evidence["key"])


def _alter_the_chain_read_after_the_delete(tap, real, alter):
    """Rewrite what the control reads back as the sentinel's version chain right after its delete (its second read of that key's
    versions: the first is the freshness check, the rest are the cleanup), leaving the store itself as it is."""
    seen = {"reads": 0}

    def second_read(kw):
        if "_prune-control" not in kw.get("Prefix", ""):
            return False
        seen["reads"] += 1
        return seen["reads"] == 2

    def altered(kw):
        resp = copy.deepcopy(real.list_object_versions(**kw))
        alter(resp)
        return resp
    tap.rule("list_object_versions", second_read, altered, when="instead")


@pytest.mark.parametrize("what", ["noncurrent-version-has-another-size", "delete-marker-is-not-the-newest-entry"])
def test_the_control_refuses_a_chain_whose_noncurrent_version_or_marker_is_not_what_its_delete_made(tmp_path, ver_s3, what):
    def other_size(resp):
        for v in resp.get("Versions", []):
            v["Size"] = int(v["Size"]) + 1

    def marker_not_newest(resp):
        for m in resp.get("DeleteMarkers", []):
            m["IsLatest"] = False
    be, tap = _control(tmp_path, ver_s3)
    _alter_the_chain_read_after_the_delete(tap, ver_s3, other_size if what.startswith("noncurrent") else marker_not_newest)
    out = be.composite_gc_prune_control(time.time())
    assert not out.ok and out.failed == "control-delete-left-no-recoverable-version" and out.evidence["cleanup"] == "clean"
    assert not _exists(ver_s3, out.evidence["key"])
    again, _tap2 = _control(tmp_path, ver_s3)
    assert again.composite_gc_prune_control(time.time()).ok  # control: with the chain read as the store holds it, the same call passes
