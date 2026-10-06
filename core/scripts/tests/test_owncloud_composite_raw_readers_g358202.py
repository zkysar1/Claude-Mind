"""Tests for the RAW readers of the composite goal-queue layout ( U3).

THE SEAM: `_owncloud_composite.decode_whole(be, key, obj)`, which the readers that bypass the backend's
mirror call where they used to call the codec's `decode_response`. Two exist for the goal queue:
`aspirations_write._authoritative_goal_lookup` (the persistence read-back behind add-goal, claim and every
critical transition) and `worker_stall._read_queue_lines` (the queue read of the stall detector and of the
stranded-claim sweep: claim map, terminal ids, known ids, goal meta; `peer_liveness` reads it too).

WHAT GOES WRONG WITHOUT IT. Under the layout the object at the store's key is a small HEAD that holds no
goal. A reader that parses it as the file gets a CLEAN, WRONG answer rather than an error: the read-back
answers 'cannot verify' for a goal that is there (the write-loss check goes silent), and the stall readers
return an empty claim map and an EMPTY goal census at 'authoritative' provenance. The census is the
dangerous one: `body_row_reaper._goal_vanished` reads a goal's absence from `read_known_goal_ids` as 'this
goal exists nowhere'. The head-blind controls below produce each of those answers on purpose, so every
positive pin here is one that can fail.

Every scenario that touches S3 runs against BOTH an in-memory fake and moto (the `s3` fixture shared with
test_owncloud_composite_read_g358202); the pure `decode_whole` pins need neither.

Coverage:
  1. decode_whole: a plain or gzip whole body is returned and the backend is never touched; a head is
     handed to `be.join_composite` with the DECODED head and the response's ETag
  2. the persistence read-back finds a goal in the oldest, a middle and the newest segment, reports an
     absent goal as absent, and reads a claim and a transition off the joined goal (both layouts, one answer)
  3. a head that cannot be joined (a segment gone, a segment corrupt) fails OPEN as 'no-verify' and is never
     read as a loss; the fast path answers from the mirror under an unchanged head and declines after a
     peer commit, when only the joined newer segment holds the new value
  4. the stall readers (claims, terminal ids, known ids, goal meta) see every segment at 'authoritative'
     provenance; a head that cannot be joined degrades them to the local mirror, never to an empty answer
  5. controls: the head-blind readers answer no-verify / an empty authoritative census

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(PROJECT_ROOT / "core" / "scripts"), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _owncloud_codec as codec  # noqa: E402
import _owncloud_composite as comp  # noqa: E402
import worker_stall as ws  # noqa: E402
from mind_api.src.endpoints import aspirations_write as aw  # noqa: E402
from test_owncloud_composite_read_g358202 import (  # noqa: E402,F401  (the doubles are shared)
    BUCKET, _Stream, _aspiration, _isolate, _legacy, _publish, _setup, s3,
)

ASP = "asp-9"
OLD_GOAL, MID_GOAL, NEW_GOAL = "g-9-5", "g-9-300", "g-9-550"  # segment tokens 0, 1, 2 at span 250
CLAIMS = {"sid-5": OLD_GOAL, "sid-300": MID_GOAL, "sid-550": NEW_GOAL}
TERMINAL = {"g-9-6", "g-9-301", "g-9-551", "g-12-2"}
POPULATION = 40 + 600 + 40


def _goal(recs, gid):
    for rec in recs:
        for g in rec["goals"]:
            if g["id"] == gid:
                return g
    raise KeyError(gid)


def _queue():
    """Three aspirations; asp-9 spans three segments (goal ids 1..600 at span 250), so a claim or a
    finished goal can sit in the oldest, a middle and the newest of them."""
    recs = [_aspiration("3", 40), _aspiration("9", 600), _aspiration("12", 40)]
    for sid, gid in CLAIMS.items():
        _goal(recs, gid).update(status="in-progress", claimed_by="alpha", claimed_by_sid=sid)
    for gid, status in (("g-9-6", "completed"), ("g-9-301", "skipped"), ("g-9-551", "expired"),
                        ("g-12-2", "completed")):
        _goal(recs, gid)["status"] = status
    return recs


def _stored(s3, key):
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _store(s3, tmp_path, layout="composite"):
    """A backend over a goal queue stored in `layout`, with NOTHING read yet: its fence is empty, so the
    persistence read-back takes the raw read (the path under test). Returns (be, path, key, raw)."""
    be, p, key = _setup(tmp_path, s3)
    raw = _legacy(_queue())
    if layout == "composite":
        _publish(s3, key, raw)
        assert comp.is_head(_stored(s3, key)), "the fixture must leave a composite head at the key"
    else:
        s3.put_object(Bucket=BUCKET, Key=key, Body=raw)
        assert _stored(s3, key) == raw
    return be, p, key, raw


def _segment_key(s3, key, asp, token):
    """The one segment object of `asp` at `token`, found by listing the store (never by recomputing a name)."""
    prefix = comp.segment_s3_key(key, "")
    hits = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])
            if "/%s/%s." % (asp, token) in o["Key"]]
    assert len(hits) == 1, hits
    return hits[0]


def _wire_aw(monkeypatch, be):
    monkeypatch.setattr(aw, "get_backend", lambda: be)


def _wire_ws(monkeypatch, be):
    """`_read_queue_lines` imports `storage_backend` at call time, so the module is swapped (the pattern
    test_worker_stall uses). Build the store BEFORE calling this: the backend module imports it too."""
    monkeypatch.setitem(sys.modules, "storage_backend", types.SimpleNamespace(get_backend=lambda: be))


def _head_blind(be, key, obj):
    """What both readers did before U3: the codec's decode and nothing else."""
    return codec.decode_response(obj, key=key)


# --- 1. decode_whole, pure --------------------------------------------------------------------------
class _Untouchable:
    """A backend that a body which is not a head must never reach."""

    def __getattr__(self, name):
        raise AssertionError("decode_whole reached the backend (%s) for a body that is not a head" % name)


def _resp(body, **extra):
    return {"Body": _Stream(body), **extra}


def test_the_fixture_spreads_the_three_goals_over_three_segments():
    assert [comp.goal_token(g) for g in (OLD_GOAL, MID_GOAL, NEW_GOAL)] == ["0", "1", "2"]
    plan = comp.plan_write(None, _legacy(_queue()))
    assert sorted(n.split(".")[0] for n in plan.segments) == ["asp-12/0", "asp-3/0", "asp-9/0", "asp-9/1", "asp-9/2"]


def test_a_plain_body_is_returned_and_the_backend_is_never_touched():
    raw = _legacy(_queue())
    assert comp.decode_whole(_Untouchable(), "k", _resp(raw, ETag='"e1"')) == raw


def test_a_gzip_whole_body_is_decoded_and_the_backend_is_never_touched():
    raw = _legacy(_queue())
    kw = codec.put_kwargs(raw)
    assert kw["Body"][:2] == b"\x1f\x8b" and kw["Body"] != raw  # the control: it really is the encoded form
    resp = _resp(kw["Body"], ContentEncoding=kw["ContentEncoding"], Metadata=kw["Metadata"], ETag='"e2"')
    assert comp.decode_whole(_Untouchable(), "k", resp) == raw


def test_a_head_is_handed_to_the_backend_join_with_the_decoded_head_and_the_response_etag():
    head = comp.plan_write(None, _legacy(_queue())).head
    assert comp.is_head(head)
    calls = []

    class _Be:
        def join_composite(self, key, body, etag):
            calls.append((key, body, etag))
            return b"WHOLE"

    assert comp.decode_whole(_Be(), "env/world/q", _resp(head, ETag='"h1"')) == b"WHOLE"
    assert calls == [("env/world/q", head, '"h1"')]


# --- 2. the persistence read-back -------------------------------------------------------------------
@pytest.mark.parametrize("layout", ["composite", "whole"])
@pytest.mark.parametrize("gid", [OLD_GOAL, MID_GOAL, NEW_GOAL])
def test_the_readback_finds_a_goal_in_any_segment(s3, tmp_path, monkeypatch, layout, gid):
    be, p, key, _raw = _store(s3, tmp_path, layout)
    _wire_aw(monkeypatch, be)
    mark = len(s3.log)
    assert aw._authoritative_goal_lookup(p, ASP, gid) == ("found", _goal(_queue(), gid))
    gets = s3.since("get", mark)
    assert gets[0] == key  # the raw read ran (the fast path had no fence to answer from) ...
    assert (len(gets) > 1) == (layout == "composite")  # ... and joined segments exactly when the store is a head


@pytest.mark.parametrize("layout", ["composite", "whole"])
def test_the_readback_reports_an_absent_goal_as_absent_and_a_present_one_as_persisted(
        s3, tmp_path, monkeypatch, layout):
    be, p, _key, _raw = _store(s3, tmp_path, layout)
    _wire_aw(monkeypatch, be)
    assert aw._authoritative_goal_lookup(p, ASP, "g-9-601") == ("goal-absent", None)
    assert aw._verify_goal_persisted(p, ASP, "g-9-601") is False
    assert aw._verify_goal_persisted(p, ASP, NEW_GOAL) is True


def test_the_claim_and_transition_readbacks_read_the_field_off_the_joined_goal(s3, tmp_path, monkeypatch):
    be, p, _key, _raw = _store(s3, tmp_path)
    _wire_aw(monkeypatch, be)
    assert aw._verify_claim_persisted(p, ASP, MID_GOAL, "alpha") is True
    assert aw._verify_claim_persisted(p, ASP, MID_GOAL, "bravo") is False
    assert aw._verify_transition_persisted(p, ASP, MID_GOAL, {"status": "in-progress"}) == (True, [])
    persisted, mismatches = aw._verify_transition_persisted(p, ASP, MID_GOAL, {"status": "completed"})
    assert persisted is False
    assert mismatches == [{"field": "status", "expected": "completed", "observed": "in-progress"}]


# --- 3. a head that cannot be joined; the fast path ---------------------------------------------------
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_a_head_that_cannot_be_joined_fails_open_and_is_never_read_as_a_loss(
        s3, tmp_path, monkeypatch, capsys, damage):
    be, p, key, _raw = _store(s3, tmp_path)
    _wire_aw(monkeypatch, be)
    victim = _segment_key(s3, key, ASP, 1)
    if damage == "missing":
        s3.delete_object(Bucket=BUCKET, Key=victim)
    else:
        s3.put_object(Bucket=BUCKET, Key=victim, Body=b"not the segment\n")
    for gid in (OLD_GOAL, MID_GOAL):  # the join is whole-file: a goal in an intact segment cannot be verified either
        assert aw._authoritative_goal_lookup(p, ASP, gid) == ("no-verify", None)
    assert "persistence read-back unavailable (IntegrityError)" in capsys.readouterr().err
    # fail-open is the contract: a write that may well have landed is never answered with a false loss
    assert aw._verify_goal_persisted(p, ASP, "g-9-601") is True


def test_the_fast_path_answers_from_the_mirror_under_an_unchanged_head_and_declines_after_a_peer_commit(
        s3, tmp_path, monkeypatch):
    be, p, key = _setup(tmp_path, s3)
    raw = _legacy(_queue())
    _etag, head = _publish(s3, key, raw)
    _wire_aw(monkeypatch, be)
    assert be.read_bytes(p, force_fresh=True) == raw  # the mirror is the joined file, the fence the head's ETag
    mark = len(s3.log)
    status, goal = aw._authoritative_goal_lookup(p, ASP, MID_GOAL)
    assert (status, goal["status"]) == ("found", "in-progress")
    assert s3.since("head", mark) == [key] and s3.since("get", mark) == []  # one HEAD, no body read
    # a peer commits a change to that goal: the head moves and this backend's fence does not
    peer = _queue()
    _goal(peer, MID_GOAL)["status"] = "completed"
    _publish(s3, key, _legacy(peer), head)
    mark = len(s3.log)
    status, goal = aw._authoritative_goal_lookup(p, ASP, MID_GOAL)
    assert (status, goal["status"]) == ("found", "completed")  # only the newer joined segment holds this value
    gets = s3.since("get", mark)
    assert gets[0] == key and len(gets) > 1


# --- 4. the stall readers ----------------------------------------------------------------------------
@pytest.mark.parametrize("layout", ["composite", "whole"])
def test_the_stall_readers_see_every_segment_at_authoritative_provenance(s3, tmp_path, monkeypatch, layout):
    be, p, _key, _raw = _store(s3, tmp_path, layout)
    _wire_ws(monkeypatch, be)
    assert ws.read_claims(p) == (CLAIMS, "authoritative")
    assert ws.read_terminal_goal_ids(p) == (TERMINAL, "authoritative")
    known, via = ws.read_known_goal_ids(p)
    assert via == "authoritative" and len(known) == POPULATION
    assert {OLD_GOAL, MID_GOAL, NEW_GOAL, "g-3-1", "g-12-40"} <= known
    meta, via = ws.read_goal_meta(p)
    assert via == "authoritative" and len(meta) == POPULATION
    assert meta[MID_GOAL]["claimed_by_sid"] == "sid-300"


def test_a_head_that_cannot_be_joined_degrades_the_stall_read_to_the_local_mirror(s3, tmp_path, monkeypatch):
    be, p, key, raw = _store(s3, tmp_path)
    _wire_ws(monkeypatch, be)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)  # the mirror a refresh would have left
    s3.delete_object(Bucket=BUCKET, Key=_segment_key(s3, key, ASP, 2))
    assert ws.read_claims(p) == (CLAIMS, "local-mirror")  # never an empty answer labelled authoritative
    known, via = ws.read_known_goal_ids(p)
    assert via == "local-mirror" and len(known) == POPULATION


# --- 5. controls: the readers as they were before U3 -----------------------------------------------------
def test_a_head_blind_readback_cannot_verify_a_goal_that_is_there(s3, tmp_path, monkeypatch):
    be, p, _key, _raw = _store(s3, tmp_path)
    _wire_aw(monkeypatch, be)
    monkeypatch.setattr(aw, "_decode_whole", _head_blind)
    assert aw._authoritative_goal_lookup(p, ASP, MID_GOAL) == ("no-verify", None)


def test_head_blind_stall_readers_return_a_clean_empty_authoritative_answer(s3, tmp_path, monkeypatch):
    be, p, _key, _raw = _store(s3, tmp_path)
    _wire_ws(monkeypatch, be)
    monkeypatch.setattr(comp, "decode_whole", _head_blind)
    assert ws.read_claims(p) == ({}, "authoritative")
    # the census a reaper reads absence from: every goal of the queue 'exists nowhere'
    assert ws.read_known_goal_ids(p) == (set(), "authoritative")
