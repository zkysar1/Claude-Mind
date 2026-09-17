""": a rotation of a board store must survive a colliding peer write.

The board stores are rotated by jsonl_hygiene (archive the oldest front slice,
then drop it) while registered to a LINE-UNION merge handler. Whenever the
rotation's write and a peer's write collide, the side still holding the
pre-rotation copy used to put the whole archived slice back into the live file.
merge_rotated_board_jsonl removes exactly that slice and falls back to the plain
union for every other shape.

Two layers are pinned here:
  * the pure handler -- the removal, and every shape that must NOT remove;
  * the CALLER -- OwnCloudBackend's real 412 -> _merge_reconcile_put path, driven
    through both race orders on a mocked object store, each with a one-variable
    control arm (the plain union swapped back in) that must resurrect. A test
    that cannot fail on the old handler would prove nothing.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination_merge as cm  # noqa: E402

CAP = cm._ROTATED_WINDOW_MIN_RECORDS      # a rotation that keeps exactly the floor
DROP = 200                                # the slice a rotation archives


def _msg(i, **kw):
    r = {"id": f"msg-{i:05d}", "timestamp": f"2026-09-{1 + i // 86400:02d}T"
         f"{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}",
         "text": f"post {i}"}
    r.update(kw)
    return r


def _blob(recs):
    return "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in recs).encode()


def _ids(blob):
    return [json.loads(ln)["id"] for ln in blob.decode().splitlines() if ln.strip()]


PRE = [_msg(i) for i in range(CAP + DROP)]          # the file before rotation
ARCHIVED = PRE[:DROP]                               # what the rotation archived
KEPT = PRE[DROP:]                                   # what the rotation left live
NEW_A = _msg(90000, text="appended by the stale side")
NEW_B = _msg(90001, text="appended by the rotated side")


# --- the removal -------------------------------------------------------------
def test_rotated_out_slice_is_removed_in_both_argument_orders():
    stale = _blob(PRE + [NEW_A])
    rotated = _blob(KEPT + [NEW_B])
    ab = cm.merge_rotated_board_jsonl(stale, rotated)
    ba = cm.merge_rotated_board_jsonl(rotated, stale)
    assert ab == ba                                        # guard-907
    ids = _ids(ab)
    assert len(ids) == CAP + 2
    assert not set(ids) & {r["id"] for r in ARCHIVED}      # nothing resurrected
    assert {NEW_A["id"], NEW_B["id"]} <= set(ids)          # both appends kept


def test_plain_union_control_resurrects_the_same_slice():
    """Reciprocal control: identical inputs, the one variable is the handler."""
    out = cm.merge_append_only_jsonl(_blob(PRE + [NEW_A]), _blob(KEPT + [NEW_B]))
    assert len(_ids(out)) == CAP + DROP + 2
    assert {r["id"] for r in ARCHIVED} <= set(_ids(out))


def test_retry_loop_reaches_a_fixpoint():
    stale, rotated = _blob(PRE + [NEW_A]), _blob(KEPT + [NEW_B])
    merged = cm.merge_rotated_board_jsonl(stale, rotated)
    for _ in range(3):
        again = cm.merge_rotated_board_jsonl(stale, merged)
        assert again == merged == cm.merge_rotated_board_jsonl(rotated, merged)
        merged = again


def test_identical_sides_are_unchanged():
    blob = _blob(PRE)
    assert cm.merge_rotated_board_jsonl(blob, blob) == cm.merge_append_only_jsonl(blob, blob)


# --- shapes that must keep the plain union ----------------------------------
def _assert_plain_union(a, b):
    got = cm.merge_rotated_board_jsonl(a, b)
    assert got == cm.merge_append_only_jsonl(a, b)
    assert got == cm.merge_rotated_board_jsonl(b, a)


def test_a_side_below_the_window_floor_never_vouches_for_a_cut():
    short = PRE[DROP + 1:]                              # CAP - 1 records
    assert len(short) == CAP - 1
    _assert_plain_union(_blob(PRE), _blob(short))


def test_a_tail_fragment_body_keeps_every_record():
    """guard-5168: a partial body is a SUFFIX, just as a rotated file is."""
    fragment = _blob(PRE[-5:] + [NEW_A])
    out = cm.merge_rotated_board_jsonl(fragment, _blob(PRE))
    assert len(_ids(out)) == CAP + DROP + 1
    _assert_plain_union(fragment, _blob(PRE))


def test_a_recreated_file_keeps_every_record():
    _assert_plain_union(_blob([NEW_A]), _blob(PRE))


def test_a_prefix_line_the_other_side_still_holds_is_not_a_rotation():
    rotated = KEPT + [ARCHIVED[7]]                      # out-of-order holder
    _assert_plain_union(_blob(PRE), _blob(rotated))


def test_a_torn_line_on_the_vouching_side_keeps_the_union(capsys):
    torn = _blob(KEPT) + b'{"id": "msg-torn", "timest\n'
    _assert_plain_union(_blob(PRE), torn)
    assert "torn line" in capsys.readouterr().err


# --- dispatch ----------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/w/world/board/coordination.jsonl", "/w/world/board/findings.jsonl",
    "/w/world/board/decisions.jsonl", "/w/world/board/general.jsonl",
    "/w/world/board/reasoning.jsonl", "/w/world/board/events.jsonl",
    "/w/world/board/coordination-reads.jsonl",
    "C:\\w\\world\\board\\feedback.jsonl",
])
def test_rotated_board_stores_resolve_to_the_rotation_aware_handler(path):
    assert cm.merge_handler_for(path) is cm.merge_rotated_board_jsonl


def test_the_same_basenames_elsewhere_keep_the_plain_union():
    for path in ("coordination.jsonl", "/w/agents/zeta/events.jsonl",
                 "/w/world/findings.jsonl"):
        assert cm.merge_handler_for(path) is cm.merge_append_only_jsonl, path


def test_the_unregistered_board_archive_stays_fence_only():
    assert cm.merge_handler_for("/w/world/board/coordination-archive.jsonl") is None
    assert cm.merge_handler_for("/w/world/board/__guardtest__.jsonl") is None


def test_a_history_snapshot_of_a_board_store_never_gets_rotation_eviction():
    """: branch 10 carries branch 9's `.history` exclusion. A snapshot
    is an immutable point-in-time copy, never a rotated live window."""
    path = "/w/world/.history/snapshots/board/coordination.jsonl"
    assert cm.merge_handler_for(path) is not cm.merge_rotated_board_jsonl


# --- : only a DATED front block is a rotation ------------------------
# The merged file is sorted by _log_ts, and an undated record (no field in
# _LOG_TS_FIELDS) sorts FIRST whatever its age. So a fresh undated line one side
# holds and the other has not received yet sits exactly where a rotated-out slice
# sits, and it was never archived. The -reads sidecars are this shape for EVERY
# record: they stamp `read_at`, which is not a _LOG_TS_FIELDS member.
def _read(i, **kw):
    r = {"msg_id": f"msg-{i:05d}", "reader_agent": "zeta",
         "reader_sid": "sid-1", "read_at": "2026-09-17T01:00:00"}
    r.update(kw)
    return r


@pytest.mark.parametrize("make, fresh", [
    (_msg, {"id": "dir-undated-1", "text": "a directive with no timestamp"}),
    (_read, _read(0, reader_agent="alpha", read_at="2026-09-17T02:00:00")),
])
def test_an_undated_front_line_the_other_side_lacks_is_kept(make, fresh):
    kept = [make(i) for i in range(1, CAP + 1)]
    holder = cm.merge_append_only_jsonl(_blob([fresh]), _blob(kept))  # sorted: fresh first
    assert json.loads(holder.decode().splitlines()[0]) == fresh
    merged = cm.merge_rotated_board_jsonl(holder, _blob(kept))
    assert fresh in [json.loads(ln) for ln in merged.decode().splitlines()]
    _assert_plain_union(holder, _blob(kept))


def test_two_rotators_cut_only_a_slice_one_of_them_archived():
    """The asymmetric case: A archived and dropped 200, B archived and dropped 300.
    The merge cuts records 200-299 from live -- and every one of them is in B's
    archive, because rotation archives FIRST. Nothing is in no store."""
    pre = [_msg(i) for i in range(CAP + 300)]
    a_archive, a_live = pre[:200], pre[200:]
    b_archive, b_live = pre[:300], pre[300:]
    merged = cm.merge_rotated_board_jsonl(_blob(a_live), _blob(b_live))
    assert merged == cm.merge_rotated_board_jsonl(_blob(b_live), _blob(a_live))
    live_ids = set(_ids(merged))
    cut = {r["id"] for r in a_live} - live_ids
    assert cut == {r["id"] for r in pre[200:300]}
    assert cut <= {r["id"] for r in b_archive}
    stored = live_ids | {r["id"] for r in a_archive + b_archive}
    assert stored == {r["id"] for r in pre}


# --- the caller: OwnCloudBackend's real 412 -> merge path --------------------
moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")
from moto import mock_aws  # noqa: E402

BUCKET, LOCKS, SESSIONS, REGION = "zds-data", "zds-locks", "zds-sessions", "us-east-2"


@pytest.fixture
def two_boxes(monkeypatch, tmp_path):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "_rt"))
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET,
                         CreateBucketConfiguration={"LocationConstraint": REGION})
        for table, key in ((LOCKS, "lock_key"), (SESSIONS, "session_key")):
            ddb.create_table(
                TableName=table, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}])
        from owncloud_backend import OwnCloudBackend

        def box(name):
            root = tmp_path / name
            return root, OwnCloudBackend(
                env_id="ayoai-mind", bucket=BUCKET, lock_table=LOCKS,
                sessions_table=SESSIONS, cache_root=root, machine_id=name,
                region=REGION, s3=s3, ddb=ddb)

        yield box("rotator"), box("peer"), s3


def _live(s3, backend, root):
    key = backend._s3_key(root / "world" / "board" / "coordination.jsonl")
    return _ids(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())


def _append_loses_to_rotation(two_boxes, monkeypatch):
    (a_root, a), (b_root, b), s3 = two_boxes
    a_path = a_root / "world" / "board" / "coordination.jsonl"
    b_path = b_root / "world" / "board" / "coordination.jsonl"
    a.write_jsonl(a_path, PRE)
    real_read = b._read_jsonl_fresh

    def read_then_rotation_lands(path):
        items = real_read(path)                            # peer holds PRE + fence
        a.modify_jsonl(a_path, lambda cur: cur[DROP:])     # rotation commits first
        return items

    monkeypatch.setattr(b, "_read_jsonl_fresh", read_then_rotation_lands)
    b.append_jsonl_record(b_path, NEW_A)                   # stale fence -> 412 -> merge
    return _live(s3, a, a_root)


def _rotation_loses_to_append(two_boxes, monkeypatch):
    (a_root, a), (b_root, b), s3 = two_boxes
    a_path = a_root / "world" / "board" / "coordination.jsonl"
    b_path = b_root / "world" / "board" / "coordination.jsonl"
    a.write_jsonl(a_path, PRE)
    real_read = a._read_jsonl_fresh

    def read_then_append_lands(path):
        items = real_read(path)                            # rotator holds PRE + fence
        b.append_jsonl_record(b_path, NEW_B)               # peer append commits first
        return items

    monkeypatch.setattr(a, "_read_jsonl_fresh", read_then_append_lands)
    a.modify_jsonl(a_path, lambda cur: cur[DROP:])         # stale fence -> 412 -> merge
    return _live(s3, a, a_root)


@pytest.mark.parametrize("race,appended", [
    (_append_loses_to_rotation, NEW_A),
    (_rotation_loses_to_append, NEW_B),
])
def test_rotation_survives_a_colliding_peer_write(two_boxes, monkeypatch, race, appended):
    ids = race(two_boxes, monkeypatch)
    assert len(ids) == CAP + 1                             # the cap plus the one append
    assert not set(ids) & {r["id"] for r in ARCHIVED}      # zero overlap with archive
    assert appended["id"] in ids


@pytest.mark.parametrize("race", [_append_loses_to_rotation, _rotation_loses_to_append])
def test_control_arm_plain_union_undoes_the_rotation(two_boxes, monkeypatch, race):
    monkeypatch.setattr(cm, "merge_rotated_board_jsonl", cm.merge_append_only_jsonl)
    ids = race(two_boxes, monkeypatch)
    assert len(ids) == CAP + DROP + 1
    assert {r["id"] for r in ARCHIVED} <= set(ids)
