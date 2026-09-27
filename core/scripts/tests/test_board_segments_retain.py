"""Board date-segment retention MOVES, never deletes ().

FIXTURE-BASED ON PURPOSE. No board channel has a date segment yet: the flip
that creates them (g-358-183) waits on this goal. So every segment below is
synthetic, and `--today` is pinned so the verdicts do not drift with the
calendar.

THE DIRECTION THAT MATTERS. This script removes files from a store with no
.history layer (board/ is snapshot-blacklisted). The cheap half is proving it
moves what it should. The load-bearing half is proving it removes NOTHING whose
records are not in the STORE copy of the archive, and touches nothing else. It
must also refuse when it cannot tell.
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# guard-955: pin the backend BEFORE importing anything that resolves one.
os.environ["STORAGE_BACKEND"] = "local"

import importlib.util  # noqa: E402

import storage_backend  # noqa: E402

_SCRIPT = _SCRIPTS / "board-segments-retain.py"
_SPEC = importlib.util.spec_from_file_location("board_segments_retain", _SCRIPT)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

TODAY = "2026-09-27"      # retention 10d -> cutoff 2026-09-17
CH = "coordination"
EXPIRED = ("2026-09-05", "2026-09-06")
KEPT = ("2026-09-17", "2026-09-26")   # the boundary day, and one inside the window


def _post(mid, ts, text="x"):
    return {"id": mid, "author": "bravo", "session_id": "", "timestamp": ts,
            "channel": CH, "type": "status", "text": text, "reply_to": None, "tags": []}


def _write(path, recs):
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def _board(tmp):
    b = tmp / "board"
    b.mkdir()
    # The archive already carries a re-archived duplicate ( residue).
    # The move must leave it exactly as it is.
    _write(b / f"{CH}-archive.jsonl", [
        _post("msg-a1", "2026-08-01T10:00:00"), _post("msg-a2", "2026-08-02T10:00:00"),
        _post("msg-a1", "2026-08-01T10:00:00")])
    _write(b / f"{CH}.jsonl", [_post("msg-base1", "2026-08-20T10:00:00")])
    # Decoys that share the channel stem and must never move.
    _write(b / f"{CH}-reads.jsonl", [{"msg_id": "msg-a1", "agent": "alpha"}])
    _write(b / f"{CH}-archive-archive.jsonl", [])
    segs = {
        "2026-09-05": [_post("msg-s05a", "2026-09-05T01:00:00"),
                       _post("msg-s05b", "2026-09-05T23:59:59")],
        # A post whose lock wait crossed midnight lands in the previous day.
        "2026-09-06": [_post("msg-s06a", "2026-09-06T12:00:00"),
                       _post("msg-s06b", "2026-09-07T00:00:02")],
        "2026-09-17": [_post("msg-s17", "2026-09-17T09:00:00")],
        "2026-09-26": [_post("msg-s26", "2026-09-26T09:00:00")],
    }
    for day, recs in segs.items():
        _write(b / f"{CH}-{day}.jsonl", recs)
    return b


def _seg(b, day):
    return b / f"{CH}-{day}.jsonl"


def _run(b, *extra):
    return mod.main(["--board-dir", str(b), "--today", TODAY, "--batch-days", "0", *extra])


def _ids(path):
    return [json.loads(ln)["id"] for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _md5s(b):
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(b.iterdir())
            if p.is_file()}


MOVED_IDS = {"msg-s05a", "msg-s05b", "msg-s06a", "msg-s06b"}


# --- plan -------------------------------------------------------------------

def test_plan_moves_only_segments_older_than_the_cutoff(tmp_path):
    b = _board(tmp_path)
    segs = mod.channel_segments(b)
    assert list(segs) == [CH], "archive, reads and archive-archive files are not segments"
    import datetime as dt
    row = mod.plan(segs, dt.date(2026, 9, 17), 0)[CH]
    assert [p.name for _, p in row["expired"]] == [_seg(b, d).name for d in EXPIRED]
    assert row["present"] == 4 and row["due"]


def test_the_newest_segment_is_kept_even_when_every_segment_is_expired():
    import datetime as dt
    segs = {CH: [(dt.date(2026, 9, 1), pathlib.Path("a")),
                 (dt.date(2026, 9, 2), pathlib.Path("b"))]}
    row = mod.plan(segs, dt.date(2026, 9, 17), 0)[CH]
    assert [d for d, _ in row["expired"]] == [dt.date(2026, 9, 1)], \
        "a quiet channel keeps one segment, the anchor of board.py's archive reach"


def test_batch_is_due_only_once_the_oldest_expired_segment_is_batch_days_old():
    import datetime as dt
    cutoff = dt.date(2026, 9, 17)

    def due(oldest):
        segs = {CH: [(oldest, pathlib.Path("x")), (cutoff, pathlib.Path("y"))]}
        return mod.plan(segs, cutoff, 7)[CH]["due"]

    assert not due(dt.date(2026, 9, 11))   # 6 days past the cutoff: waits
    assert due(dt.date(2026, 9, 10))       # 7 days: the whole batch moves
    assert not mod.plan({CH: [(cutoff, pathlib.Path("y"))]}, cutoff, 0)[CH]["due"]


# --- refusals ---------------------------------------------------------------

def test_dry_run_is_the_default_and_touches_nothing(tmp_path, capsys):
    b = _board(tmp_path)
    before = _md5s(b)
    assert _run(b) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["action"] == "would-move" and not rep["applied"]
    assert rep["channels"][CH]["expired"] == [_seg(b, d).name for d in EXPIRED]
    assert _md5s(b) == before


def test_apply_without_archive_dir_refuses_and_touches_nothing(tmp_path, capsys):
    b = _board(tmp_path)
    before = _md5s(b)
    assert _run(b, "--apply") == 2
    assert json.loads(capsys.readouterr().out)["action"] == "refused-no-archive-dir"
    assert _md5s(b) == before


# --- outcome 1: moved by id set, nothing else touched ----------------------

def test_apply_moves_by_id_set_and_leaves_everything_else_byte_identical(tmp_path, capsys):
    b = _board(tmp_path)
    cold = tmp_path / "cold"
    before = _md5s(b)
    assert _run(b, "--apply", "--archive-dir", str(cold)) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["action"] == "moved"
    assert rep["channels"][CH]["appended"] == 4
    for d in EXPIRED:
        assert not _seg(b, d).exists()
    archive = _ids(b / f"{CH}-archive.jsonl")
    # The prior lines, residue duplicate included, are untouched and first.
    assert archive[:3] == ["msg-a1", "msg-a2", "msg-a1"]
    assert set(archive[3:]) == MOVED_IDS and len(archive) == 7
    after = _md5s(b)
    for name in (f"{CH}.jsonl", f"{CH}-reads.jsonl", f"{CH}-archive-archive.jsonl",
                 _seg(b, KEPT[0]).name, _seg(b, KEPT[1]).name):
        assert after[name] == before[name], name
    for d in EXPIRED:
        name = _seg(b, d).name
        assert hashlib.md5((cold / name).read_bytes()).hexdigest() == before[name]
    receipt = (cold / "RECEIPT.md").read_text(encoding="utf-8")
    assert all(_seg(b, d).name in receipt for d in EXPIRED)


def test_a_record_without_an_id_is_archived_once(tmp_path):
    b = _board(tmp_path)
    anon = {"author": "bravo", "timestamp": "2026-09-05T02:00:00", "text": "no id"}
    with _seg(b, EXPIRED[0]).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(anon) + "\n")
    saved = _seg(b, EXPIRED[0]).read_bytes()
    assert _run(b, "--apply", "--archive-dir", str(tmp_path / "c1")) == 0
    _seg(b, EXPIRED[0]).write_bytes(saved)
    assert _run(b, "--apply", "--archive-dir", str(tmp_path / "c2")) == 0
    lines = (b / f"{CH}-archive.jsonl").read_text(encoding="utf-8").splitlines()
    assert sum(1 for ln in lines if '"no id"' in ln) == 1


# --- outcome 2: a repeat run and a second box add zero duplicates ----------

def test_repeat_run_on_a_resurrected_segment_adds_no_duplicate_ids(tmp_path, capsys):
    b = _board(tmp_path)
    cold = tmp_path / "cold"
    saved = _seg(b, EXPIRED[0]).read_bytes()
    assert _run(b, "--apply", "--archive-dir", str(cold)) == 0
    capsys.readouterr()
    archive = b / f"{CH}-archive.jsonl"
    count, distinct = len(_ids(archive)), len(set(_ids(archive)))
    archive_md5 = hashlib.md5(archive.read_bytes()).hexdigest()
    # A second box's stale mirror, or a restore, puts the segment back.
    _seg(b, EXPIRED[0]).write_bytes(saved)
    assert _run(b, "--apply", "--archive-dir", str(cold)) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["channels"][CH]["appended"] == 0
    assert rep["channels"][CH]["already_archived"] == 2
    assert (len(_ids(archive)), len(set(_ids(archive)))) == (count, distinct)
    # Every key was already held, so the archive was not even rewritten.
    assert hashlib.md5(archive.read_bytes()).hexdigest() == archive_md5
    assert not _seg(b, EXPIRED[0]).exists()


def test_two_concurrent_runs_add_no_duplicate_ids(tmp_path):
    b = _board(tmp_path)
    before = _ids(b / f"{CH}-archive.jsonl")
    env = dict(os.environ, STORAGE_BACKEND="local")
    procs = [subprocess.Popen(
        [sys.executable, str(_SCRIPT), "--board-dir", str(b), "--today", TODAY,
         "--batch-days", "0", "--apply", "--archive-dir", str(tmp_path / f"cold{i}")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(2)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    after = _ids(b / f"{CH}-archive.jsonl")
    assert after[:len(before)] == before
    new = after[len(before):]
    assert sorted(new) == sorted(MOVED_IDS), "each moved id appended exactly once"
    for d in EXPIRED:
        assert not _seg(b, d).exists()


# --- the store is the witness ------------------------------------------------

class _Backend:
    """The LocalBackend, except the store lane is scripted per basename."""

    def __init__(self, store=None):
        self.real = storage_backend.LocalBackend()
        self.store = store or {}

    def read_authoritative_bytes(self, path):
        name = pathlib.Path(path).name
        if name in self.store:
            return self.store[name]
        return self.real.read_authoritative_bytes(path)

    def delete(self, path):
        return self.real.delete(path)


def test_lane_divergence_stops_the_channel_at_that_segment(tmp_path, monkeypatch, capsys):
    b = _board(tmp_path)
    second = _seg(b, EXPIRED[1])
    before = second.read_bytes()
    fake = _Backend({second.name: before + b'{"id":"msg-store-only"}\n'})
    monkeypatch.setattr(storage_backend, "get_backend", lambda: fake)
    assert _run(b, "--apply", "--archive-dir", str(tmp_path / "cold")) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["channels"][CH]["moved"] == [_seg(b, EXPIRED[0]).name]
    assert "lanes diverged" in " ".join(rep["channels"][CH]["errors"])
    assert second.read_bytes() == before
    assert not {"msg-s06a", "msg-s06b"} & set(_ids(b / f"{CH}-archive.jsonl"))


def test_a_segment_is_kept_unless_the_STORE_archive_holds_its_ids(tmp_path, monkeypatch, capsys):
    b = _board(tmp_path)
    archive = b / f"{CH}-archive.jsonl"
    frozen = archive.read_bytes()   # the store never receives the append
    fake = _Backend({archive.name: frozen})
    monkeypatch.setattr(storage_backend, "get_backend", lambda: fake)
    assert _run(b, "--apply", "--archive-dir", str(tmp_path / "cold")) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["channels"][CH]["moved"] == []
    assert "not in the store archive" in " ".join(rep["channels"][CH]["errors"])
    for d in EXPIRED:
        assert _seg(b, d).exists(), "the local archive holds the ids; the store does not"
