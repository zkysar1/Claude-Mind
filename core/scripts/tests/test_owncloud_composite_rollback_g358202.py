"""Tests for a mixed fleet over the composite store of core/scripts/owncloud_backend.py ( U21): processes of one
deployment over ONE object store, some started with OWNCLOUD_COMPOSITE_STORES naming the environment and some without it.

The flag is read from the process environment at call time (`_composite.should_composite`), and a real process's
environment is fixed at its start, so each box below sets or clears it around every one of its own calls: that is the same
thing as a process that was started with, or without, the flag. A box is one backend: its own mirror, its own fences and its
own cached head.

Why a file of its own: U2d's tests and U11's live replay run ONE writer with the flag on. The rollout (design record, flip
item 5: the flag reaches each process at its next start, so it takes days) and a removed flag both put a process WITHOUT the
flag over a head and a process WITH it over a whole object, and `_store_put`'s docstring says only that the revert "every
reader tolerates".

Coverage:
  1. a process without the flag over a head: one whole-object PUT under the head's fence, no segment PUT, no edit lost
  2. a process with the flag over what the other left: its cached head is not trusted (the fence moved), every segment is
     attempted, each that already exists gets one freshen HEAD, and the head commits under the whole object's fence
  3. alternating writers lose no edit, commit once each, and every reader reads the same joined bytes
  4. a stale-fence writer of either kind takes the merge path across the layout change and keeps both edits
  5. a collecting pass that took every segment between the two writes leaves no dangling head, on either path
  6. the flag removed with a restart, then set again with another: the head still reads, the first write reverts the layout,
     the next one re-seeds
  7. the sync sweep's merge_put from a process without the flag over a head

Not covered, and not claimed: the daemon's lock, read-modify-write and refresh path with the flag on (U11, "Not entered"),
and a live store (the rehearsal under audit-reports/g-358-202/ is that).

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))

import _owncloud_composite as comp  # noqa: E402
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401  (the doubles are shared)
    BUCKET, ENV_ID, REGION, REL, _isolate, _legacy, _records, s3,
)
from test_owncloud_composite_write_g358202 import gz  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _floor(monkeypatch, _isolate):
    """The size floor is lowered to the fixture's size (the real floor has its own test). The writer flag is set per box."""
    monkeypatch.setattr(comp, "MIN_RAW_BYTES", 1)


class _Box:
    """One process of the fleet. `on` is whether it was started with the writer flag."""

    def __init__(self, name, s3, tmp_path, on):
        from owncloud_backend import OwnCloudBackend  # noqa: PLC0415
        self.on, self.rt = on, tmp_path / (name + "-rt")
        with self.process():
            self.be = OwnCloudBackend(env_id=ENV_ID, bucket=BUCKET, lock_table="test-locks", sessions_table="test-sessions",
                                      cache_root=tmp_path / name, machine_id="m-" + name, region=REGION, s3=s3, ddb=object())
        self.p = tmp_path / name / "world" / "aspirations.jsonl"
        self.key = self.be._s3_key(self.p)
        assert self.key == "%s/%s" % (ENV_ID, REL)

    @contextmanager
    def process(self):
        with pytest.MonkeyPatch.context() as m:
            m.setenv("RUNTIME_DIR", str(self.rt))
            if self.on:
                m.setenv("OWNCLOUD_COMPOSITE_STORES", ENV_ID)
            else:
                m.delenv("OWNCLOUD_COMPOSITE_STORES", raising=False)
            yield

    def read(self):
        """The refresh a locked write begins with: the joined store, and the fence it carries."""
        with self.process():
            return self.be.read_bytes(self.p, force_fresh=True)

    def modify(self, k):
        """Refresh, apply edit k to what was read, write it back under the fence the refresh took."""
        with self.process():
            new = _edit(self.be.read_bytes(self.p, force_fresh=True), k)
            self.be.write_bytes(self.p, new)
            return new

    def write_stale(self, k):
        """Apply edit k to this box's own last view and write it without refreshing: its fence is whatever it last saw."""
        with self.process():
            self.be.write_bytes(self.p, _edit(self.p.read_bytes(), k))


def _edit(raw, k):
    """Complete one goal, a different one for each k (a status only moves forward, so the merge keeps every edit)."""
    recs = [json.loads(line) for line in raw.decode("ascii").splitlines()]
    recs[k % len(recs)]["goals"][(k * 7) % 240]["status"] = "completed"
    return _legacy(recs)


def _completed(raw):
    return {(r["id"], g["id"]) for r in map(json.loads, raw.decode("ascii").splitlines()) for g in r["goals"]
            if g["status"] == "completed"}


def _want(*ks):
    recs = _records()
    return {(recs[k % len(recs)]["id"], recs[k % len(recs)]["goals"][(k * 7) % 240]["id"]) for k in ks}


def _is_segment_key(k):
    return "/%s/aspirations.jsonl/" % comp.SEGMENT_DIR in k


def _kind(s3, key):
    return "head" if comp.is_head(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()) else "whole"


def _etag(s3, key):
    return s3.head_object(Bucket=BUCKET, Key=key)["ETag"]


def _seed(s3, key):
    """The store as it is before the flag: one whole object."""
    s3.put_object(Bucket=BUCKET, Key=key, Body=_legacy(_records()))


def _segment_keys(s3):
    out, kw = set(), dict(Bucket=BUCKET, Prefix=ENV_ID + "/")
    while True:
        page = s3.list_objects_v2(**kw)
        out |= {o["Key"] for o in page.get("Contents", []) if _is_segment_key(o["Key"])}
        if not page.get("IsTruncated"):
            return out
        kw["ContinuationToken"] = page["NextContinuationToken"]


def _trio(s3, tmp_path, a_on=True, b_on=False):
    """Two writers and a flagless reader that has never written."""
    return _Box("a", s3, tmp_path, a_on), _Box("b", s3, tmp_path, b_on), _Box("c", s3, tmp_path, False)


# --- 1. a process without the flag over a head ---------------------------------------------------------
def test_a_process_without_the_flag_over_a_head_replaces_it_with_one_whole_object_under_the_heads_fence(s3, tmp_path, gz):
    a, b, c = _trio(s3, tmp_path)
    _seed(s3, a.key)
    a.modify(1)  # the migration write: the store is a head
    assert _kind(s3, a.key) == "head"
    head_etag = _etag(s3, a.key)
    mark = len(s3.puts)
    new = b.modify(2)
    puts = s3.puts[mark:]
    assert [(x["Key"], x["IfMatch"], x["IfNoneMatch"]) for x in puts] == [(a.key, head_etag, None)]
    assert _kind(s3, a.key) == "whole"
    assert c.read() == new and _completed(new) == _want(1, 2)


# --- 2. a process with the flag over what the other left -------------------------------------------------
def test_a_flagged_process_over_what_the_other_left_does_not_trust_its_cached_head(s3, tmp_path, gz):
    a, b, c = _trio(s3, tmp_path)
    _seed(s3, a.key)
    a.modify(1)
    b.modify(2)  # a's cached head now describes an object that is gone
    whole_etag = _etag(s3, a.key)
    existing = _segment_keys(s3)
    mark_puts, mark_log = len(s3.puts), len(s3.log)
    new = a.modify(3)
    puts, heads = s3.puts[mark_puts:], [k for k in s3.since("head", mark_log) if _is_segment_key(k)]
    segs, head = puts[:-1], puts[-1]
    attempted = {x["Key"] for x in segs}
    assert attempted == {comp.segment_s3_key(a.key, n) for n in comp.plan_write(None, new).segments}  # all of them
    assert all(x["IfNoneMatch"] == "*" and x["IfMatch"] is None for x in segs)
    assert head["Key"] == a.key and head["IfMatch"] == whole_etag
    assert sorted(heads) == sorted(attempted & existing)  # one freshen HEAD for each segment that was already there
    assert _kind(s3, a.key) == "head" and c.read() == new and _completed(new) == _want(1, 2, 3)


# --- 3. alternating writers ----------------------------------------------------------------------------------
def test_alternating_writers_commit_once_each_lose_no_edit_and_every_reader_reads_the_same_bytes(s3, tmp_path, gz):
    a, b, c = _trio(s3, tmp_path)
    _seed(s3, a.key)
    for k in range(1, 9):
        w = a if k % 2 else b
        mark = len(s3.puts)
        w.modify(k)
        puts = s3.puts[mark:]
        assert len([x for x in puts if x["Key"] == a.key]) == 1
        assert bool([x for x in puts if _is_segment_key(x["Key"])]) == w.on  # only a flagged writer sends segments
        assert _kind(s3, a.key) == ("head" if w.on else "whole")
    reads = [x.read() for x in (a, b, c)]
    assert len(set(reads)) == 1 and _completed(reads[0]) == _want(*range(1, 9))
    a.modify(9)  # and the layout comes back
    assert _kind(s3, a.key) == "head" and _completed(c.read()) == _want(*range(1, 10))


# --- 4. a stale fence across the layout change -------------------------------------------------------------------
def test_a_stale_writer_without_the_flag_over_a_head_merges_and_keeps_both_edits(s3, tmp_path, gz):
    a, b, c = _trio(s3, tmp_path)
    _seed(s3, a.key)
    b.read()  # b has seen the whole object: its fence is that object's ETag
    seed_etag = _etag(s3, a.key)
    a.modify(1)
    head_etag = _etag(s3, a.key)
    mark = len(s3.puts)
    b.write_stale(2)
    puts = s3.puts[mark:]
    assert [x["IfMatch"] for x in puts if x["Key"] == a.key] == [seed_etag, head_etag]  # the stale fence lost, the merge refetched
    assert not [x for x in puts if _is_segment_key(x["Key"])]
    assert _kind(s3, a.key) == "whole" and _completed(c.read()) == _want(1, 2)


def test_a_stale_flagged_writer_over_a_whole_object_merges_and_re_seeds(s3, tmp_path, gz):
    a, b, c = _trio(s3, tmp_path)
    _seed(s3, a.key)
    a.modify(1)
    head_etag = _etag(s3, a.key)
    b.modify(2)
    whole_etag = _etag(s3, a.key)
    mark = len(s3.puts)
    a.write_stale(3)
    puts = s3.puts[mark:]
    assert [x["IfMatch"] for x in puts if x["Key"] == a.key] == [head_etag, whole_etag]
    assert all(x["IfNoneMatch"] == "*" for x in puts if _is_segment_key(x["Key"]))
    assert _kind(s3, a.key) == "head" and _completed(c.read()) == _want(1, 2, 3)


# --- 5. a collecting pass between the two writes ------------------------------------------------------------------
def test_a_pass_that_took_every_segment_between_the_two_writes_leaves_no_dangling_head(s3, tmp_path, gz):
    a, b, c = _trio(s3, tmp_path)
    _seed(s3, a.key)
    a.modify(1)
    b.modify(2)  # every segment the old head named is an orphan now
    gone = _segment_keys(s3)
    assert gone
    for k in gone:
        s3.delete_object(Bucket=BUCKET, Key=k)  # the most a collecting pass can do
    mark = len(s3.log)
    new = a.modify(3)
    assert not [k for k in s3.since("head", mark) if _is_segment_key(k)]  # none was found present, so none was freshened
    assert _kind(s3, a.key) == "head"
    assert c.read() == new  # the join needs every segment the head names
    assert _completed(new) == _want(1, 2, 3)
    b.modify(4)  # the same again, through the merge path
    for k in _segment_keys(s3):
        s3.delete_object(Bucket=BUCKET, Key=k)
    a.write_stale(5)
    assert _kind(s3, a.key) == "head" and _completed(c.read()) == _want(1, 2, 3, 4, 5)


# --- 6. the flag removed, then set again --------------------------------------------------------------------------
def test_a_removed_flag_still_reads_the_head_its_first_write_reverts_the_layout_and_the_next_re_seeds(s3, tmp_path, gz):
    a1, c = _Box("a", s3, tmp_path, True), _Box("c", s3, tmp_path, False)
    _seed(s3, a1.key)
    a1.modify(1)
    last = a1.modify(2)
    assert _kind(s3, a1.key) == "head"
    a2 = _Box("a", s3, tmp_path, False)  # the same box, restarted without the flag: same mirror, no fence, no cached head
    assert a2.read() == last  # a head is joined whatever the flag says
    mark = len(s3.puts)
    new = a2.modify(3)
    puts = s3.puts[mark:]
    assert [x["Key"] for x in puts] == [a1.key]
    assert _kind(s3, a1.key) == "whole" and c.read() == new
    a3 = _Box("a", s3, tmp_path, True)  # restarted again, flagged
    newer = a3.modify(4)
    assert _kind(s3, a1.key) == "head" and c.read() == newer and _completed(newer) == _want(1, 2, 3, 4)


# --- 7. the sync sweep ------------------------------------------------------------------------------------------------
def test_the_sync_sweeps_merge_put_from_a_process_without_the_flag_reverts_the_head_and_keeps_both_edits(s3, tmp_path, gz):
    a, b, c = _trio(s3, tmp_path)
    _seed(s3, a.key)
    a.modify(1)
    head_etag = _etag(s3, a.key)
    local = _edit(_legacy(_records()), 2)  # a locally-newer file the sweep must not lose
    mark = len(s3.puts)
    with b.process():
        res = b.be.merge_put(b.p, local)
    puts = s3.puts[mark:]
    assert res is not None
    assert [(x["Key"], x["IfMatch"]) for x in puts] == [(a.key, head_etag)]
    assert _kind(s3, a.key) == "whole" and _completed(c.read()) == _want(1, 2)
