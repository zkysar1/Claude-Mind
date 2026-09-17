"""Regression pins for segment freshness in GET /v1/board/read ( U3).

The read endpoint refreshes the channel's BASE file on every read (jsonl_cache.get
-> ensure_local), but `_board_paths.channel_paths()` enumerates date segments off
LOCAL disk. On own-cloud, a segment a PEER minted reached this box only when
pull_sweep's LIST materialised it (every ~10 min by default), and a local segment
was refreshed no faster than that. So a reader could miss a peer's recent posts
while the base file beside them was current.

The fix refreshes today's and yesterday's segment names BEFORE enumerating. These
tests stand a fake in for the store, the same way test_board_archive_reach.py does
for the archive refresh: calling the fake writes what the store holds and the
local mirror lacks. That is the one effect a real refresh has on disk.

FOUR PROPERTIES ARE PINNED:
  1. a store-only segment (today's, and yesterday's at the day boundary) is
     materialised before enumeration, and a stale local one is refreshed;
  2. the refresh is bounded to those two names, whatever the retention;
  3. a store-ABSENT name is re-probed at most once per cache TTL, so pre-flip
     reads do not pay two empty HEADs every time, while a PRESENT segment is
     refreshed on every read (the backend's own TTL governs it);
  4. a failed refresh still serves the local copies, but no footer claims the
     window is covered.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from mind_api.src.endpoints import board


class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, world: Path, query: dict):
        self.paths = _FakePaths(world)
        self.query = query
        self.headers = {}


TS_FMT = "%Y-%m-%dT%H:%M:%S"
CH = "coordination"
SEG_OP = "board_segment_refresh"


def _msg(mid: str, ts: datetime, text: str):
    return {
        "id": mid, "author": "bravo", "session_id": "",
        "timestamp": ts.strftime(TS_FMT), "channel": CH, "type": "status",
        "text": text, "reply_to": None, "tags": [],
    }


def _write(path: Path, records, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _seed(tmp_path: Path):
    """A base file holding one recent post. Returns (world, board_dir, now)."""
    now = datetime.now().replace(microsecond=0)
    world = tmp_path / "world"
    board_dir = world / "board"
    _write(board_dir / f"{CH}.jsonl",
           [_msg("msg-live-001", now - timedelta(hours=2), "live")])
    return world, board_dir, now


def _seg(board_dir: Path, day) -> Path:
    return board_dir / f"{CH}-{day.isoformat()}.jsonl"


def _store(monkeypatch, *, holds=None, error=None):
    """Stand in for the store behind ensure_local_before_append.

    `holds` maps a segment basename to the records the STORE has for it. A
    refresh of that name appends them to the local file, creating it when it is
    absent, which is what materialising or tail-pulling does to the mirror.
    Returns the list of (basename, op) calls the segment refresh made.
    """
    import storage_backend
    calls = []
    holds = dict(holds or {})

    def fake(path, *, op="ensure_local"):
        if op != SEG_OP:
            return None
        name = Path(path).name
        calls.append((name, op))
        if error is not None:
            return error
        if name in holds:
            _write(Path(path), holds.pop(name), mode="a")
        return None

    monkeypatch.setattr(storage_backend, "ensure_local_before_append", fake)
    monkeypatch.setattr(board, "_SEGMENT_ABSENT_PROBED_AT", {})
    return calls


def _read(world: Path, **query):
    q = {"channel": CH}
    q.update({k: str(v) for k, v in query.items()})
    return board.read(_FakeCtx(world, q))


def _ids(resp):
    body = resp.body.decode("utf-8")
    return [json.loads(line)["id"] for line in body.splitlines() if line.strip()]


def _footer(resp):
    return resp.body.decode("utf-8").strip().splitlines()[-1]


# --- 1. store-only and stale segments come back ------------------------------

def test_store_only_segment_for_today_is_materialized_before_enumeration(tmp_path, monkeypatch):
    """THE defect. A peer's segment that exists only in the store must be on disk
    before channel_paths() lists local files, or the read never opens it."""
    world, board_dir, now = _seed(tmp_path)
    today = now.date()
    peer_post = [_msg("msg-seg-peer", now - timedelta(minutes=3), "peer, store only")]

    # Positive control: a refresh that materialises nothing leaves the peer's
    # post absent, so its presence below is caused by the refresh.
    _store(monkeypatch)
    assert "msg-seg-peer" not in _ids(_read(world, since="1d", json="1"))

    _store(monkeypatch, holds={_seg(board_dir, today).name: peer_post})
    ids = _ids(_read(world, since="1d", json="1"))
    assert "msg-seg-peer" in ids, ids
    assert "msg-live-001" in ids, ids


def test_store_only_segment_for_yesterday_is_discovered(tmp_path, monkeypatch):
    """The day boundary. Just after midnight UTC, a post a peer wrote at 23:5x
    sits in YESTERDAY's segment, which this box may never have pulled."""
    world, board_dir, now = _seed(tmp_path)
    yesterday = now.date() - timedelta(days=1)
    late_post = [_msg("msg-seg-late", now - timedelta(hours=1), "written before midnight")]

    _store(monkeypatch, holds={_seg(board_dir, yesterday).name: late_post})
    ids = _ids(_read(world, since="2d", json="1"))
    assert "msg-seg-late" in ids, ids


def test_stale_local_segment_is_refreshed_before_the_read(tmp_path, monkeypatch):
    """A segment already on disk must not be served at pull_sweep's cadence."""
    world, board_dir, now = _seed(tmp_path)
    seg = _seg(board_dir, now.date())
    _write(seg, [_msg("msg-seg-local", now - timedelta(minutes=30), "mirror has it")])
    newer = [_msg("msg-seg-newer", now - timedelta(minutes=1), "only the store has it")]

    _store(monkeypatch)
    assert "msg-seg-newer" not in _ids(_read(world, since="1d", json="1"))

    _store(monkeypatch, holds={seg.name: newer})
    ids = _ids(_read(world, since="1d", json="1"))
    assert "msg-seg-local" in ids and "msg-seg-newer" in ids, ids


# --- 2. the bound ------------------------------------------------------------

def test_only_todays_and_yesterdays_segments_are_refreshed(tmp_path, monkeypatch):
    """Older segments are closed by their date, and pull_sweep covers them. The
    refresh must not grow with retention. They are still READ, from the mirror."""
    world, board_dir, now = _seed(tmp_path)
    today = now.date()
    for back in (3, 5):
        _write(_seg(board_dir, today - timedelta(days=back)),
               [_msg(f"msg-old-{back}", now - timedelta(days=back), f"{back} days old")])

    calls = _store(monkeypatch)
    ids = _ids(_read(world, since="7d", json="1"))

    assert [name for name, _ in calls] == [
        _seg(board_dir, today - timedelta(days=1)).name,
        _seg(board_dir, today).name,
    ], calls
    assert "msg-old-3" in ids and "msg-old-5" in ids, ids


# --- 3. the throttle ---------------------------------------------------------

def test_store_absent_name_is_not_reprobed_within_the_ttl(tmp_path, monkeypatch):
    """Before the first segment is minted, both names are absent everywhere. The
    backend's TTL shortcut needs a local file, so without this throttle every
    board read would pay two HEADs that find nothing."""
    world, _, _ = _seed(tmp_path)
    calls = _store(monkeypatch)
    monkeypatch.setattr(board, "_segment_probe_ttl", lambda: 120.0)

    _read(world, json="1")
    assert len(calls) == 2, calls
    _read(world, json="1")
    assert len(calls) == 2, "an absent name was re-probed inside the TTL"

    # TTL expiry: age every mark past the TTL, and both names are probed again.
    for key in list(board._SEGMENT_ABSENT_PROBED_AT):
        board._SEGMENT_ABSENT_PROBED_AT[key] -= 121.0
    _read(world, json="1")
    assert len(calls) == 4, calls


def test_without_a_ttl_every_read_probes(tmp_path, monkeypatch):
    """Control for the throttle test: the local backend has no cache TTL, a
    refresh there costs nothing, and nothing is skipped."""
    world, _, _ = _seed(tmp_path)
    calls = _store(monkeypatch)
    monkeypatch.setattr(board, "_segment_probe_ttl", lambda: 0.0)
    _read(world, json="1")
    _read(world, json="1")
    assert len(calls) == 4, calls


def test_a_present_segment_is_refreshed_on_every_read(tmp_path, monkeypatch):
    """The throttle covers ABSENT names only. Once a segment is on disk (a peer's
    materialised by pull_sweep, or this box's own post), every read refreshes it
    and the backend's TTL governs the cost, even inside a prior absent mark."""
    world, board_dir, now = _seed(tmp_path)
    today_seg = _seg(board_dir, now.date())
    yesterday_seg = _seg(board_dir, now.date() - timedelta(days=1))
    calls = _store(monkeypatch)
    monkeypatch.setattr(board, "_segment_probe_ttl", lambda: 120.0)

    _read(world, json="1")                       # both absent: probed, marked
    _write(today_seg, [_msg("msg-seg-arrived", now, "landed after the probe")])
    _read(world, json="1")
    _read(world, json="1")

    names = [name for name, _ in calls]
    assert names.count(today_seg.name) == 3, names
    assert names.count(yesterday_seg.name) == 1, names


# --- 4. a failed refresh never claims coverage --------------------------------

def test_failed_segment_refresh_never_claims_the_window_is_covered(tmp_path, monkeypatch):
    """Fail-open like the archive refresh: the local copies are still served, and
    the human footer must say the window is NOT verified covered."""
    world, board_dir, now = _seed(tmp_path)
    _write(_seg(board_dir, now.date()),
           [_msg("msg-seg-local", now - timedelta(minutes=10), "mirror copy")])
    _store(monkeypatch, error="ClientError: SlowDown")

    ids = _ids(_read(world, since="1d", json="1"))
    assert "msg-seg-local" in ids and "msg-live-001" in ids, \
        "a failed refresh must still serve the local copies"

    footer = _footer(_read(world, since="1d"))
    assert "NOT verified covered (segment refresh failed)" in footer, footer
    assert "segment refresh FAILED (" in footer and "ClientError: SlowDown" in footer, footer
    assert "window covered" not in footer, footer

    # Negative control: the same read with a working refresh keeps the old footer.
    _store(monkeypatch)
    ok = _footer(_read(world, since="1d"))
    assert "window covered" in ok and "NOT verified" not in ok, ok


def test_failed_segment_refresh_reaches_an_empty_result_footer(tmp_path, monkeypatch):
    """A zero-match read has its own footer line. An empty answer on a stale
    mirror is exactly the wrong zero the note exists to flag."""
    world, _, _ = _seed(tmp_path)
    _store(monkeypatch, error="EndpointConnectionError: unreachable")
    text = _read(world, since="1d", author="nobody").body.decode("utf-8")
    assert "0 messages matched" in text, text[-400:]
    assert "window NOT verified covered" in text, text[-400:]
