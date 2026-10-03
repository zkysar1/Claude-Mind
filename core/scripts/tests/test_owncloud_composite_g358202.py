"""Unit tests for core/scripts/_owncloud_composite.py — the composite head+segment
layout for a whole-file-rewritten JSONL store (g-358-202).

Pure-Python: no backend, no daemon, no network.

Coverage:
  1. split -> join is byte-identical on a fixture whose goal ids straddle a range edge
     and whose STRING order differs from NUMERIC order (so the check can fail)
  2. controls that can fail: a file in numeric order is refused; swapped goals are refused
  3. one goal mutation -> the head plus exactly one segment, at least 10x fewer bytes
  4. integrity: a missing segment, a stale segment, a tampered head and a non-head all raise
  5. refusals (duplicate goal id, null goals, no trailing newline, blank line, ...) raise
     NotSplittable, so the caller PUTs the whole object
  6. the merge handler registered for the legacy path runs on JOINED bytes and its output
     satisfies the split invariant, including for a goal on a range edge
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _owncloud_composite as c  # noqa: E402


def _dump(rec) -> str:
    return json.dumps(rec, ensure_ascii=True)


def _legacy(records) -> bytes:
    return "".join(_dump(r) + "\n" for r in records).encode("ascii")


def _goal(asp, n, **extra):
    g = {"id": "g-%s-%d" % (asp, n), "title": "goal %d of %s" % (n, asp), "status": "pending",
         "priority": "MEDIUM", "description": "x" * 240}
    g.update(extra)
    return g


def _aspiration(asp, numbers, **extra):
    goals = sorted((_goal(asp, n) for n in numbers), key=lambda g: g["id"])  # plain string order
    rec = {"id": "asp-%s" % asp, "title": "aspiration %s" % asp, "status": "active", "goals": goals}
    rec.update(extra)
    return rec


def _fixture():
    numbers = list(range(1, 261)) + [1000]  # 9 < 10 < 100 in numbers, "g-1-10" < "g-1-9" as strings
    odd = _aspiration("2", [1, 2, 3])
    odd["goals"][0]["title"] = "naïve — 漢字 \U0001F600"
    return [_aspiration("1", numbers), odd, {"id": "asp-3", "title": "no goals key"},
            {"id": "asp-4", "title": "empty goals", "goals": []}]


def _ids(rec):
    return [g["id"] for g in rec["goals"]]


def test_fixture_orders_differ_so_the_order_checks_can_fail():
    ids = _ids(_fixture()[0])
    assert ids == sorted(ids)
    assert ids != sorted(ids, key=lambda i: int(i.rsplit("-", 1)[1]))


def test_round_trip_is_byte_identical_and_keys_straddle_the_range_edge():
    raw = _legacy(_fixture())
    s = c.split(raw)
    assert c.join(s.head, s.segments) == raw
    assert c.is_head(s.head) and not c.is_head(raw)
    assert sorted(s.segments) == ["asp-1/0", "asp-1/1", "asp-1/4", "asp-2/0"]
    assert s.manifest["asp-1/0"]["goals"] == 249 and s.manifest["asp-1/1"]["goals"] == 11


def test_empty_input_round_trips():
    s = c.split(b"")
    assert s.segments == {} and c.join(s.head, s.segments) == b""


def test_a_file_in_numeric_order_is_refused():
    recs = _fixture()
    recs[0]["goals"].sort(key=lambda g: int(g["id"].rsplit("-", 1)[1]))
    with pytest.raises(c.NotSplittable):
        c.split(_legacy(recs))


def test_swapped_goals_are_refused():
    recs = _fixture()
    g = recs[0]["goals"]
    g[3], g[4] = g[4], g[3]
    with pytest.raises(c.NotSplittable):
        c.split(_legacy(recs))


def _big_fixture():
    return [_aspiration(str(a), range(1, 241)) for a in range(20)]


def _remote(raw):
    plan = c.plan_write(None, raw)
    return plan.head, plan.segments


def test_one_mutation_puts_the_head_and_one_new_segment_object_only():
    first = c.plan_write(None, _legacy(_big_fixture()))
    recs = _big_fixture()
    recs[7]["goals"][100]["status"] = "completed"
    raw = _legacy(recs)
    plan = c.plan_write(first.head, raw)
    assert len(plan.segments) == 1
    (name, body), = plan.segments.items()
    assert name.startswith("asp-7/0.") and name.endswith(".jsonl") and name not in first.segments
    assert c.plan_write(plan.head, raw).segments == {}  # nothing changed, nothing to PUT
    put_bytes = len(plan.head) + len(body)
    assert len(raw) / put_bytes >= 10, (len(raw), put_bytes)


def test_the_first_write_puts_every_segment_under_a_distinct_content_name():
    raw = _legacy(_fixture())
    plan = c.plan_write(None, raw)
    s = c.split(raw)
    assert sorted(plan.segments) == sorted(c.segment_object_name(k, m["md5"]) for k, m in s.manifest.items())


def test_an_object_name_always_carries_the_same_bytes():
    a = c.plan_write(None, _legacy(_fixture()))
    recs = _fixture()
    recs[1]["goals"][0]["status"] = "completed"
    b = c.plan_write(a.head, _legacy(recs))
    assert set(a.segments) & set(b.segments) == set()
    for name, body in {**a.segments, **b.segments}.items():
        assert name.endswith(".%s.jsonl" % hashlib.md5(body).hexdigest())


def test_a_reader_with_nothing_fetches_everything_and_rebuilds_the_file():
    raw = _legacy(_fixture())
    head, objects = _remote(raw)
    r = c.plan_refresh(None, head)
    assert r.reuse == {} and sorted(r.fetch.values()) == sorted(objects)
    assert c.join(head, {k: objects[n] for k, n in r.fetch.items()}) == raw


def test_a_reader_one_mutation_behind_fetches_one_segment():
    old = _legacy(_big_fixture())
    recs = _big_fixture()
    recs[7]["goals"][100]["status"] = "completed"
    new = _legacy(recs)
    head, objects = _remote(new)
    r = c.plan_refresh(old, head)
    assert list(r.fetch) == ["asp-7/0"] and len(r.reuse) == 19
    assert c.join(head, {**r.reuse, **{k: objects[n] for k, n in r.fetch.items()}}) == new


def test_a_reader_already_current_fetches_nothing():
    raw = _legacy(_fixture())
    head, _ = _remote(raw)
    r = c.plan_refresh(raw, head)
    assert r.fetch == {} and len(r.reuse) == len(c.split(raw).segments)


def test_a_local_segment_with_other_content_is_refetched():
    head, _ = _remote(_legacy(_fixture()))
    recs = _fixture()
    recs[1]["goals"][0]["title"] = "locally edited"
    r = c.plan_refresh(_legacy(recs), head)
    assert list(r.fetch) == ["asp-2/0"] and len(r.reuse) == 3


@pytest.mark.parametrize("local", [b"not json at all\n", b"[1]\n", _legacy([{"id": "asp-1", "goals": [{"id": "g-1-1"}, {"id": "g-1-1"}]}])])
def test_an_unreproducible_local_file_means_fetch_everything(local):
    head, objects = _remote(_legacy(_fixture()))
    r = c.plan_refresh(local, head)
    assert r.reuse == {} and len(r.fetch) == len(objects)


def test_integrity_failures_are_loud():
    raw = _legacy(_fixture())
    s = c.split(raw)
    missing = {k: v for k, v in s.segments.items() if k != "asp-2/0"}
    with pytest.raises(c.IntegrityError):
        c.join(s.head, missing)
    stale = dict(s.segments)
    stale["asp-2/0"] = c.split(_legacy([_aspiration("2", [1, 2])])).segments["asp-2/0"]
    with pytest.raises(c.IntegrityError):
        c.join(s.head, stale)
    doc = json.loads(s.head)
    doc["joined_md5"] = "0" * 32
    tampered = (_dump(doc) + "\n").encode("ascii")
    with pytest.raises(c.IntegrityError):
        c.join(tampered, s.segments)
    with pytest.raises(c.IntegrityError):
        c.join(raw, s.segments)


def _dup_goal():
    recs = _fixture()
    recs[1]["goals"].append(dict(recs[1]["goals"][0]))
    return _legacy(recs)


def _null_goals():
    recs = _fixture()
    recs[3]["goals"] = None
    return _legacy(recs)


def _goal_without_id():
    recs = _fixture()
    del recs[1]["goals"][0]["id"]
    return _legacy(recs)


@pytest.mark.parametrize("raw", [
    _legacy(_fixture())[:-1],                                   # no trailing newline
    _legacy(_fixture()) + b"\n",                                # blank line
    b"not json\n",
    b"[1, 2]\n",
    _dup_goal(),
    _null_goals(),
    _goal_without_id(),
    _legacy([{"id": "asp/1", "goals": []}]),                    # id unusable as a segment key
    _legacy([{"id": "asp-1", "goals": []}, {"id": "asp-1", "goals": []}]),
], ids=["no-newline", "blank-line", "not-json", "not-object", "dup-goal", "null-goals",
        "goal-without-id", "unsafe-aspiration-id", "dup-aspiration"])
def test_unreproducible_input_is_refused(raw):
    with pytest.raises(c.NotSplittable):
        c.split(raw)


def test_a_head_is_not_splittable_again():
    with pytest.raises(c.NotSplittable):
        c.split(c.split(_legacy(_fixture())).head)


def test_the_merge_handler_for_the_legacy_path_runs_on_joined_bytes():
    import coordination_merge as cm
    assert cm.merge_handler_for("world/aspirations.jsonl") is cm.merge_aspirations
    local = _legacy([_aspiration("1", [1, 249, 251, 9, 10])])
    remote = _legacy([_aspiration("1", [250, 251, 100, 99])])
    merged = cm.merge_aspirations(local, remote)
    assert merged != local and merged != remote
    ids = _ids(json.loads(merged.split(b"\n")[0]))
    assert ids == sorted(ids) and {"g-1-249", "g-1-250", "g-1-251"} <= set(ids)
    s = c.split(merged)  # the handler's own output satisfies the invariant
    assert c.join(s.head, s.segments) == merged
    assert {"asp-1/0", "asp-1/1"} <= set(s.segments)


def test_should_composite_is_off_by_default_and_env_scoped():
    rel = "world/aspirations.jsonl"
    assert not c.should_composite(rel, "env-a", env={})
    assert c.should_composite(rel, "env-a", env={c.FLAG_ENV: "env-a"})
    assert c.should_composite(rel, "ENV-A", env={c.FLAG_ENV: "env-a, env-b"})
    assert not c.should_composite(rel, "env-c", env={c.FLAG_ENV: "env-a,env-b"})
    assert c.should_composite(rel, "env-c", env={c.FLAG_ENV: "*"})
    assert not c.should_composite(rel, None, env={c.FLAG_ENV: "env-a"})


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_a_legacy_boolean_flag_names_no_environment(value):
    assert not c.should_composite("world/aspirations.jsonl", "env-a", env={c.FLAG_ENV: value})


def test_should_composite_requires_an_allowlisted_store():
    env = {c.FLAG_ENV: "*"}
    assert c.should_composite("world/aspirations.jsonl", "env-a", env=env)
    assert not c.should_composite("world/guardrails.jsonl", "env-a", env=env)
    assert not c.should_composite("world/aspirations.jsonl.bak", "env-a", env=env)


def test_the_composite_and_gzip_flags_are_independent():
    import _owncloud_codec as codec
    rel = "world/aspirations.jsonl"
    assert not c.should_composite(rel, "env-a", env={codec.FLAG_ENV: "env-a"})
    assert not codec.should_encode(rel, "env-a", env={c.FLAG_ENV: "env-a"})
    assert codec.should_encode(rel, "env-a", env={codec.FLAG_ENV: "env-a"})  # the gzip default path is unchanged


# --- read_whole (U2c): the one reader the backend and the raw-S3 readers share ---------------------------


class _Objects:
    """A segment store for read_whole: object name -> bytes, plus a log of what was fetched."""

    def __init__(self, objects, head=None, etag="E1"):
        self.objects = dict(objects)
        self.fetched = []
        self.rereads = 0
        self._head, self._etag = head, etag

    def fetch(self, name):
        self.fetched.append(name)
        if name not in self.objects:
            raise c.SegmentMissing(name)
        return self.objects[name]

    def reread(self):
        self.rereads += 1
        return self._head, self._etag


def _one_mutation():
    old = _legacy(_big_fixture())
    recs = _big_fixture()
    recs[7]["goals"][100]["status"] = "completed"
    return old, _legacy(recs)


def test_read_whole_cold_read_rebuilds_the_file_and_fetches_every_segment_once():
    raw = _legacy(_fixture())
    head, objects = _remote(raw)
    store = _Objects(objects)
    got, etag = c.read_whole(head, "E1", store.fetch, store.reread)
    assert (got, etag) == (raw, "E1")
    assert sorted(store.fetched) == sorted(objects) and store.rereads == 0


def test_read_whole_warm_read_fetches_only_the_changed_segment():
    old, new = _one_mutation()
    head, objects = _remote(new)
    store = _Objects(objects)
    got, _ = c.read_whole(head, "E2", store.fetch, store.reread, local_raw=old)
    assert got == new
    assert len(store.fetched) == 1 and store.fetched[0].startswith("asp-7/0.")


def test_read_whole_for_a_current_local_file_fetches_nothing():
    raw = _legacy(_fixture())
    head, _ = _remote(raw)
    store = _Objects({})  # no object is available, so any fetch would raise
    got, _ = c.read_whole(head, "E1", store.fetch, store.reread, local_raw=raw)
    assert got == raw and store.fetched == []


def test_read_whole_returns_a_whole_object_unchanged_and_does_no_io():
    raw = _legacy(_fixture())

    def boom(*_a):
        raise AssertionError("a body that is not a head needs no I/O")

    assert c.read_whole(raw, "E7", boom, boom) == (raw, "E7")


def test_read_whole_never_trusts_a_local_segment_that_differs_from_the_manifest():
    _old, new = _one_mutation()
    head, objects = _remote(new)
    poisoned = _big_fixture()
    poisoned[3]["goals"][5]["title"] = "locally edited"  # asp-3/0 no longer matches the head's md5
    store = _Objects(objects)
    got, _ = c.read_whole(head, "E2", store.fetch, store.reread, local_raw=_legacy(poisoned))
    assert got == new and b"locally edited" not in got
    assert sorted(n.split(".")[0] for n in store.fetched) == ["asp-3/0", "asp-7/0"]


def test_read_whole_rereads_the_head_when_a_segment_was_collected_and_keeps_the_new_etag():
    a, b = _one_mutation()
    head_a, objects_a = _remote(a)
    head_b, objects_b = _remote(b)
    gone = sorted(set(objects_a) - set(objects_b))
    assert len(gone) == 1  # the control: exactly A's own asp-7/0 segment is absent from the store
    store = _Objects(objects_b, head=head_b, etag="E2")
    got, etag = c.read_whole(head_a, "E1", store.fetch, store.reread)
    assert got == b and gone[0] in store.fetched and store.rereads == 1
    assert etag == "E2"  # the fence token follows the bytes: newer bytes, newer token, never E1


def test_read_whole_gives_up_after_the_bounded_number_of_joins():
    raw = _legacy(_fixture())
    head, objects = _remote(raw)
    victim = sorted(objects)[0]
    store = _Objects({n: body for n, body in objects.items() if n != victim}, head=head)
    with pytest.raises(c.IntegrityError, match="%d joins" % c.READ_ATTEMPTS):
        c.read_whole(head, "E1", store.fetch, store.reread)
    assert store.rereads == c.READ_ATTEMPTS - 1 and store.fetched.count(victim) == c.READ_ATTEMPTS


def test_read_whole_retries_a_segment_whose_bytes_do_not_match_the_manifest():
    raw = _legacy(_fixture())
    head, objects = _remote(raw)
    victim = sorted(objects)[0]
    store = _Objects(objects, head=head)
    tampered = []

    def fetch(name):
        body = store.fetch(name)
        if name == victim and not tampered:
            tampered.append(name)
            return b"tampered\n"
        return body

    got, etag = c.read_whole(head, "E1", fetch, store.reread)
    assert got == raw and etag == "E1"
    assert tampered == [victim] and store.rereads == 1  # the first join failed, the retry succeeded


def test_read_whole_returns_the_whole_file_when_the_reread_finds_the_old_layout():
    raw = _legacy(_fixture())
    head, _objects = _remote(raw)
    store = _Objects({}, head=raw, etag="E9")  # the writer was switched off: the key holds the file again
    assert c.read_whole(head, "E1", store.fetch, store.reread) == (raw, "E9")
    assert store.rereads == 1


def test_segment_s3_key_is_a_dot_directory_beside_the_head():
    name = "asp-1/0.%s.jsonl" % ("a" * 32)
    assert c.segment_s3_key("ayoai-mind/world/aspirations.jsonl", name) == \
        "ayoai-mind/world/.composite/aspirations.jsonl/" + name
    assert c.segment_s3_key("acme/ayoai-mind/world/aspirations.jsonl", name) == \
        "acme/ayoai-mind/world/.composite/aspirations.jsonl/" + name
    assert c.segment_s3_key("aspirations.jsonl", name) == ".composite/aspirations.jsonl/" + name
    assert c.SEGMENT_DIR == ".composite"


def test_reads_composite_is_the_allowlist_and_does_not_read_the_writer_flag():
    assert c.reads_composite("world/aspirations.jsonl")
    assert not c.reads_composite("world/aspirations-archive.jsonl")
    assert not c.reads_composite("world/board/general.jsonl")
    assert not c.reads_composite("")
    assert not c.should_composite("world/aspirations.jsonl", "ayoai-mind", env={})  # the writer gate is off
    assert c.reads_composite("world/aspirations.jsonl")  # and the reader gate is not


# --- the stored head: padded, never shortened ( U2d, outcome 6 condition C2) -----------------
def test_pad_head_fills_a_short_head_to_exactly_the_floor_and_it_still_joins():
    raw = _legacy(_fixture())
    split = c.split(raw)
    assert len(split.head) < 4096  # the control: there is something to pad
    padded = c.pad_head(split.head, floor=4096)
    assert len(padded) == 4096 and c.is_head(padded) and padded.endswith(b"\n")
    assert padded.rstrip() == split.head.rstrip()  # only blanks were added
    assert c.join(padded, split.segments) == raw


def test_pad_head_never_shortens_a_head_and_is_idempotent():
    head = c.split(_legacy(_fixture())).head
    assert c.pad_head(head, floor=len(head)) == head
    assert c.pad_head(head, floor=1) == head
    once = c.pad_head(head, floor=8192)
    assert c.pad_head(once, floor=8192) == once


def test_a_padded_old_head_still_tells_plan_write_what_the_object_store_holds():
    old, new = _one_mutation()
    padded = c.pad_head(c.plan_write(None, old).head, floor=c.HEAD_MIN_BYTES)
    assert len(padded) >= c.HEAD_MIN_BYTES
    assert c.plan_write(padded, old).segments == {}
    assert len(c.plan_write(padded, new).segments) == 1


def test_the_stored_head_floor_is_above_the_inline_threshold_and_is_the_default():
    assert c.HEAD_MIN_BYTES > 128 * 1024
    head = c.split(_legacy(_fixture())).head
    assert len(c.pad_head(head)) == c.HEAD_MIN_BYTES
