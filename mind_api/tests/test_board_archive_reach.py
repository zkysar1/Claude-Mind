"""Regression pins for GET /v1/board/read reaching <channel>-archive.jsonl ().

Rotation MOVES posts into ``<channel>-archive.jsonl`` and deletes nothing, but
``read()`` opened only the live file — so any ``since`` window older than the
live file's earliest retained post was cut SILENTLY, at HTTP 200, with no
warning. Measured 2026-09-16 (bravo, cc-05): ``fresh-eyes-program`` asks the
coordination channel for 60 days and was getting ~8.2 of them; the findings
channel's live window is 21.4 days against 30d and 60d readers. guard-6252 (a
msg-id search that returns zero is a window artifact) is the same defect seen
from the search side.

SCOPE CORRECTION, recorded here because the goal's own wording implies otherwise:
the goal asks for the fix "on BOTH the daemon and the CLI path". There is no CLI
read path to fix. ``core/scripts/board.py`` has exactly three subcommands —
``post``, ``mark-read``, ``channels`` — and no ``read``; ``board-read.sh`` is
daemon-only (``rt_call /v1/board/read``) under the 2026-05-14 cutover, and
``.claude/rules/no-python-cli-fallback.md`` rule 3 forbids adding one. So the
daemon endpoint IS the CLI path, there is one implementation, and this file is
the whole test surface for it.

FOUR PROPERTIES ARE PINNED, and the second is the one that keeps the common read
cheap: a window that lies INSIDE the live file must not stat, open, or read a
34 MB archive.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mind_api.src.endpoints import board


# --- Harness (mirrors test_board_since_filter.py) ---------------------------

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


def _msg(mid: str, ts: datetime, text: str, *, author="bravo", tags=None):
    return {
        "id": mid,
        "author": author,
        "session_id": "",
        "timestamp": ts.strftime(TS_FMT),
        "channel": CH,
        "type": "status",
        "text": text,
        "reply_to": None,
        "tags": tags or [],
    }


def _write(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _seed(tmp_path: Path, *, archive=True, dup=False, n_archive=3):
    """A live file holding only RECENT posts, beside an archive of OLD ones.

    Returns (world, now). The live file's earliest post is `now - 2d`, so a
    `--since 30d` read can only see the archived posts if the archive is read.
    """
    now = datetime.now().replace(microsecond=0)
    world = tmp_path / "world"
    board_dir = world / "board"

    live = [
        _msg("msg-live-001", now - timedelta(days=2), "live oldest"),
        _msg("msg-live-002", now - timedelta(hours=3), "live middle"),
        _msg("msg-live-003", now - timedelta(minutes=5), "live newest"),
    ]
    _write(board_dir / f"{CH}.jsonl", live)

    if archive:
        arch = [
            _msg(f"msg-arch-{i:03d}", now - timedelta(days=20 - i), f"archived {i}")
            for i in range(n_archive)
        ]
        if dup:
            # Rotation re-archives: the archive carries a STALE copy of a post
            # that is also live.  measured 4,728 such rows on
            # coordination. The live copy must win.
            stale = _msg("msg-live-001", now - timedelta(days=2),
                         "STALE ARCHIVED COPY — must not win")
            arch.append(stale)
            # ...and a duplicate of an archive-only post, which must collapse.
            arch.append(_msg("msg-arch-000", now - timedelta(days=20),
                             "archived 0 (re-archived duplicate)"))
        _write(board_dir / f"{CH}-archive.jsonl", arch)

    return world, now


def _read(world: Path, **query):
    q = {"channel": CH}
    q.update({k: str(v) for k, v in query.items()})
    return board.read(_FakeCtx(world, q))


def _json_records(resp):
    body = resp.body.decode("utf-8")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# --- Outcome 1: the archive is reached, deduped, live wins ------------------

def test_since_older_than_live_returns_archived_posts(tmp_path):
    """THE defect. A 30d window over a 2d live file must see the archive."""
    world, _ = _seed(tmp_path)

    without = _json_records(_read(world, since="1d", json="1"))
    assert {m["id"] for m in without} == {"msg-live-002", "msg-live-003"}, \
        "positive control: a window inside the live file is unchanged"

    got = _json_records(_read(world, since="30d", json="1"))
    ids = [m["id"] for m in got]
    assert ids == ["msg-arch-000", "msg-arch-001", "msg-arch-002",
                   "msg-live-001", "msg-live-002", "msg-live-003"], ids


def test_dedup_by_id_live_copy_wins(tmp_path):
    """A re-archived stale copy must never displace the live record."""
    world, _ = _seed(tmp_path, dup=True)
    got = _json_records(_read(world, since="30d", json="1"))

    ids = [m["id"] for m in got]
    assert len(ids) == len(set(ids)), f"duplicate ids survived: {ids}"
    assert ids.count("msg-arch-000") == 1, "archive-side duplicate not collapsed"

    live_001 = [m for m in got if m["id"] == "msg-live-001"]
    assert len(live_001) == 1
    assert live_001[0]["text"] == "live oldest", \
        "the STALE archived copy won — guard-3523 dedup is inverted"


def test_missing_archive_is_not_an_error(tmp_path):
    """A channel that has never rotated still answers a wide window."""
    world, _ = _seed(tmp_path, archive=False)
    got = _json_records(_read(world, since="30d", json="1"))
    assert [m["id"] for m in got] == ["msg-live-001", "msg-live-002", "msg-live-003"]


# --- Outcome 2: a window inside the live file never touches the archive -----

def test_window_inside_live_file_never_opens_the_archive(tmp_path, monkeypatch):
    """The cost guarantee. Spy on the path helper, not just the reader — the
    assertion is that the archive is not even RESOLVED, so a future `stat()`
    added next to the read cannot quietly reintroduce the cost."""
    world, _ = _seed(tmp_path)
    calls = []

    real_path = board._archive_path
    real_read = board._read_archive_tail
    monkeypatch.setattr(board, "_archive_path",
                        lambda ctx, ch: calls.append(("path", ch)) or real_path(ctx, ch))
    monkeypatch.setattr(board, "_read_archive_tail",
                        lambda p, *a: calls.append(("read", str(p))) or real_read(p, *a))

    got = _json_records(_read(world, since="1d", json="1"))
    assert {m["id"] for m in got} == {"msg-live-002", "msg-live-003"}
    assert calls == [], f"archive was touched for an in-live window: {calls}"

    # Positive control on the SPY itself: it does fire when it should, so the
    # empty list above is evidence about the code and not about the harness.
    _read(world, since="30d", json="1")
    assert [c[0] for c in calls] == ["path", "read"], calls


def test_no_since_never_opens_the_archive(tmp_path, monkeypatch):
    """`board-read.sh --channel X` with no window keeps its old cost."""
    world, _ = _seed(tmp_path)
    calls = []
    monkeypatch.setattr(board, "_archive_path",
                        lambda ctx, ch: calls.append(ch) or Path("/nonexistent"))
    got = _json_records(_read(world, json="1"))
    assert len(got) == 3
    assert calls == []


# --- Outcome 3: the archive read is byte-bounded, and the bound is visible --

def test_tail_budget_bounds_the_read_and_says_so(tmp_path, monkeypatch):
    """A CEILING (budget x _ARCHIVE_TAIL_MAX_STEPS) smaller than the archive
    returns only the TAIL, and the human footer states that the window is not
    fully covered. Step 1/16 -> ceiling 1/2 of the archive (g-358-118)."""
    world, _ = _seed(tmp_path, n_archive=12)
    arch = world / "board" / f"{CH}-archive.jsonl"
    full = arch.stat().st_size

    monkeypatch.setenv("BOARD_ARCHIVE_TAIL_BYTES", str(full // 16))
    got = _json_records(_read(world, since="30d", json="1"))
    arch_ids = [m["id"] for m in got if m["id"].startswith("msg-arch-")]

    assert arch_ids, "budget starved the read entirely"
    assert len(arch_ids) < 12, "budget did not bound the read"
    # The TAIL is the NEWEST archived posts — the ones adjacent to the live
    # file's earliest, which is the direction a widened window reaches first.
    assert arch_ids[-1] == "msg-arch-011", arch_ids
    assert arch_ids == sorted(arch_ids), "merged archive records are out of order"

    human = _read(world, since="30d").body.decode("utf-8")
    assert "TRUNCATED AT BUDGET" in human, human[-400:]
    assert "window NOT fully covered" in human


def test_budget_large_enough_reports_no_truncation(tmp_path, monkeypatch):
    """The negative control for the line above: same code path, no warning."""
    world, _ = _seed(tmp_path, n_archive=12)
    monkeypatch.setenv("BOARD_ARCHIVE_TAIL_BYTES", "10485760")
    human = _read(world, since="30d").body.decode("utf-8")
    assert "archive=12 record(s) merged" in human, human[-400:]
    assert "TRUNCATED" not in human


# --- : the tail extends with the request, under a hard ceiling ------

def test_read_extends_backward_until_the_window_is_covered(tmp_path, monkeypatch):
    """A step budget far smaller than the window no longer cuts it: the read
    steps back until the boundary record predates the cutoff."""
    world, _ = _seed(tmp_path, n_archive=12)
    full = (world / "board" / f"{CH}-archive.jsonl").stat().st_size
    monkeypatch.setenv("BOARD_ARCHIVE_TAIL_BYTES", str(full // 4))
    human = _read(world, since="30d").body.decode("utf-8")
    assert "archive=12 record(s) merged" in human, human[-400:]
    assert "TRUNCATED" not in human


def test_extension_stops_once_the_cutoff_is_reached(tmp_path):
    """The read costs what the WINDOW weighs, not what the archive weighs."""
    world, now = _seed(tmp_path, n_archive=12)
    arch = world / "board" / f"{CH}-archive.jsonl"
    full = arch.stat().st_size
    cutoff = now - timedelta(days=14)
    recs, truncated, nbytes = board._read_archive_tail(arch, full // 12, cutoff)
    assert truncated and nbytes < full, (truncated, nbytes, full)
    assert min(board._parse_ts(r["timestamp"]) for r in recs) <= cutoff
    assert {f"msg-arch-{i:03d}" for i in range(7, 12)} <= {r["id"] for r in recs}


def test_ceiling_is_a_hard_bound_whatever_the_window(tmp_path):
    """rb-3803: a window deeper than the archive must not read the archive."""
    world, now = _seed(tmp_path, n_archive=12)
    arch = world / "board" / f"{CH}-archive.jsonl"
    step = arch.stat().st_size // 32
    _, truncated, nbytes = board._read_archive_tail(arch, step, now - timedelta(days=365))
    assert truncated
    assert nbytes <= step * board._ARCHIVE_TAIL_MAX_STEPS


def test_without_a_cutoff_the_read_is_one_budget(tmp_path):
    world, _ = _seed(tmp_path, n_archive=12)
    arch = world / "board" / f"{CH}-archive.jsonl"
    step = arch.stat().st_size // 4
    _, truncated, nbytes = board._read_archive_tail(arch, step)
    assert truncated and nbytes <= step


def test_cli_read_path_is_this_endpoint():
    """One bound for the daemon and CLI read paths because there is ONE path:
    board-read.sh is a daemon-only wrapper over /v1/board/read, and
    core/scripts/board.py has no read subcommand to drift."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "core" / "scripts" / "board-read.sh").read_text(encoding="utf-8")
    assert "rt_call GET /v1/board/read" in text
    assert "board.py" not in text


def test_out_of_order_archive_is_sorted_into_the_merge(tmp_path):
    """The merge SORTS; it does not assume the archive is ordered on disk.

    Rotation appends in timestamp order, so a single-rotation archive is already
    sorted and this property is invisible in the common case — a mutation that
    deletes the sort survives every other test here. It stops being invisible the
    moment two rotations interleave or a post is back-dated, and `last=N` then
    returns the wrong N posts, silently.
    """
    world, now = _seed(tmp_path, archive=False)
    shuffled = [
        _msg("msg-arch-c", now - timedelta(days=5), "third"),
        _msg("msg-arch-a", now - timedelta(days=15), "first"),
        _msg("msg-arch-b", now - timedelta(days=10), "second"),
    ]
    _write(world / "board" / f"{CH}-archive.jsonl", shuffled)

    got = _json_records(_read(world, since="30d", json="1"))
    assert [m["id"] for m in got] == [
        "msg-arch-a", "msg-arch-b", "msg-arch-c",
        "msg-live-001", "msg-live-002", "msg-live-003",
    ], [m["id"] for m in got]

    last_two = _json_records(_read(world, since="30d", json="1", last="2"))
    assert [m["id"] for m in last_two] == ["msg-live-002", "msg-live-003"]


def test_partial_first_line_at_the_seek_boundary_is_dropped(tmp_path):
    """Seeking to a byte offset lands mid-record. The half-record must be
    dropped, not parsed into a malformed message and not raise."""
    world, now = _seed(tmp_path, n_archive=4)
    arch = world / "board" / f"{CH}-archive.jsonl"
    raw = arch.read_bytes()
    first_len = raw.index(b"\n") + 1
    # Budget that starts the read exactly in the MIDDLE of record 0.
    budget = len(raw) - (first_len // 2)
    recs, truncated, _ = board._read_archive_tail(arch, budget)
    assert truncated is True
    assert [r["id"] for r in recs] == ["msg-arch-001", "msg-arch-002", "msg-arch-003"]


def test_unparseable_archive_lines_are_skipped_not_fatal(tmp_path):
    world, _ = _seed(tmp_path, n_archive=3)
    arch = world / "board" / f"{CH}-archive.jsonl"
    with open(arch, "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")
    got = _json_records(_read(world, since="30d", json="1"))
    assert len([m for m in got if m["id"].startswith("msg-arch-")]) == 3


# --- Outcome 4: read-state parity between archive- and live-served posts ----

def test_unread_only_and_mark_read_treat_archive_posts_like_live_ones(tmp_path):
    """One sidecar, one semantics. An archive-served post must be markable and
    must then disappear from an --unread-only read exactly as a live one does."""
    world, _ = _seed(tmp_path)

    first = _json_records(_read(world, since="30d", json="1",
                                unread_only="1", mark_read="1"))
    assert len(first) == 6, [m["id"] for m in first]

    sidecar = world / "board" / f"{CH}-reads.jsonl"
    assert sidecar.exists(), "mark_read wrote no sidecar"
    marked = set()
    for line in sidecar.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            marked.update(rec.get("msg_ids") or ([rec["msg_id"]] if rec.get("msg_id") else []))
    assert {"msg-arch-000", "msg-live-003"} <= marked, marked

    second = _json_records(_read(world, since="30d", json="1", unread_only="1"))
    assert second == [], [m["id"] for m in second]


def test_filters_apply_uniformly_across_the_merge(tmp_path):
    """author/type/tag/last run AFTER the merge, so an archived post is as
    filterable as a live one — the parity that makes one code path enough."""
    world, now = _seed(tmp_path, archive=False)
    arch = [
        _msg("msg-arch-100", now - timedelta(days=15), "tagged archived",
             author="echo", tags=["insight_trigger"]),
        _msg("msg-arch-101", now - timedelta(days=14), "untagged archived",
             author="zeta"),
    ]
    _write(world / "board" / f"{CH}-archive.jsonl", arch)

    by_author = _json_records(_read(world, since="30d", json="1", author="echo"))
    assert [m["id"] for m in by_author] == ["msg-arch-100"]

    by_tag = _json_records(_read(world, since="30d", json="1", tag="insight_trigger"))
    assert [m["id"] for m in by_tag] == ["msg-arch-100"]

    last_two = _json_records(_read(world, since="30d", json="1", last="2"))
    assert [m["id"] for m in last_two] == ["msg-live-002", "msg-live-003"]


def test_since_still_refuses_an_unparseable_value(tmp_path):
    """The  pin must survive this change: an unparseable `since` is a
    400, never the whole channel plus the whole archive."""
    world, _ = _seed(tmp_path)
    resp = _read(world, since="30 days", json="1")
    assert resp.status == 400, resp.body[:200]
    assert b"invalid_param" in resp.body


# --- : the archive is refreshed from the store before the tail read ---
#
# *-archive.jsonl is excluded from the own-cloud eager pull (), so on a
# box that did not run the channel's last rotation the local archive is FROZEN.
# The reach read must go through the backend refresh first. A fake stands in for
# the store: calling it appends the records the store holds but the local mirror
# lacks, which is exactly what the range-tail pull does to a stale mirror.

def _fake_refresh(monkeypatch, *, appended=(), error=None):
    import storage_backend
    calls = []

    def fake(path, *, op="ensure_local"):
        # Only the ARCHIVE refresh is this fake's subject. Since  U3 the
        # read also refreshes today's and yesterday's date segments. Those calls
        # pass through untouched, so they neither count here nor receive
        # `appended` (test_board_segment_refresh.py pins them).
        if op != "board_archive_refresh":
            return None
        calls.append((Path(path).name, op))
        if error is not None:
            return error
        if appended:
            with open(path, "a", encoding="utf-8") as fh:
                for r in appended:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return None

    monkeypatch.setattr(storage_backend, "ensure_local_before_append", fake)
    return calls


def test_stale_local_archive_is_refreshed_before_the_tail_read(tmp_path, monkeypatch):
    """THE defect. Records the store archive holds but the local mirror lacks
    must come back, and the refresh must run BEFORE the tail is read."""
    world, now = _seed(tmp_path)
    store_only = [_msg("msg-arch-store-1", now - timedelta(days=5), "store only 1"),
                  _msg("msg-arch-store-2", now - timedelta(days=4), "store only 2")]

    # Positive control: with a refresh that changes nothing, the store-only
    # records are absent, so their presence below is caused by the refresh.
    _fake_refresh(monkeypatch)
    before = {m["id"] for m in _json_records(_read(world, since="30d", json="1"))}
    assert not before & {"msg-arch-store-1", "msg-arch-store-2"}, before

    order = []
    real_read = board._read_archive_tail
    monkeypatch.setattr(board, "_read_archive_tail",
                        lambda p, *a: order.append("read") or real_read(p, *a))
    calls = _fake_refresh(monkeypatch, appended=store_only)
    import storage_backend
    fake = storage_backend.ensure_local_before_append
    monkeypatch.setattr(storage_backend, "ensure_local_before_append",
                        lambda p, *, op="ensure_local":
                        (op == "board_archive_refresh" and order.append("refresh"))
                        or fake(p, op=op))

    got = _json_records(_read(world, since="30d", json="1"))
    ids = [m["id"] for m in got]
    assert "msg-arch-store-1" in ids and "msg-arch-store-2" in ids, ids
    assert order == ["refresh", "read"], order
    assert calls == [(f"{CH}-archive.jsonl", "board_archive_refresh")], calls


def test_refresh_also_materializes_an_archive_absent_locally(tmp_path, monkeypatch):
    """A box that never held the archive must still reach it: the existence
    check has to come AFTER the refresh, or a remote-only archive is skipped."""
    world, now = _seed(tmp_path, archive=False)
    arch = world / "board" / f"{CH}-archive.jsonl"
    import storage_backend

    def fake(path, *, op="ensure_local"):
        if op == "board_archive_refresh":
            _write(Path(path), [_msg("msg-arch-remote", now - timedelta(days=9), "remote only")])
        return None

    monkeypatch.setattr(storage_backend, "ensure_local_before_append", fake)
    assert not arch.exists()
    ids = [m["id"] for m in _json_records(_read(world, since="30d", json="1"))]
    assert ids[0] == "msg-arch-remote", ids


def test_failed_refresh_never_claims_the_window_is_covered(tmp_path, monkeypatch):
    """A refresh that fails still serves the local archive (fail-open), but the
    human footer must not say the window is covered."""
    world, _ = _seed(tmp_path)
    _fake_refresh(monkeypatch, error="ClientError: SlowDown")

    got = _json_records(_read(world, since="30d", json="1"))
    assert [m["id"] for m in got][:3] == ["msg-arch-000", "msg-arch-001", "msg-arch-002"], \
        "a failed refresh must still serve the local archive"

    human = _read(world, since="30d").body.decode("utf-8")
    footer = human.strip().splitlines()[-1]
    assert "NOT verified covered" in footer, footer
    assert "archive refresh FAILED (ClientError: SlowDown)" in footer, footer
    assert "window covered" not in footer, footer

    # Negative control: the same read with a working refresh keeps the old footer.
    _fake_refresh(monkeypatch)
    ok_footer = _read(world, since="30d").body.decode("utf-8").strip().splitlines()[-1]
    assert "window covered" in ok_footer and "NOT verified" not in ok_footer, ok_footer


def test_failed_refresh_with_no_local_archive_is_still_reported(tmp_path, monkeypatch):
    """No archive anywhere locally AND the refresh failed: the empty archive
    half is unknown, not absent, so the footer must still refuse coverage."""
    world, _ = _seed(tmp_path, archive=False)
    _fake_refresh(monkeypatch, error="EndpointConnectionError: unreachable")
    footer = _read(world, since="30d").body.decode("utf-8").strip().splitlines()[-1]
    assert "NOT verified covered" in footer, footer


def test_window_inside_live_file_never_refreshes_the_archive(tmp_path, monkeypatch):
    """The refresh sits inside the reach branch, so an in-live read pays no
    remote request. Positive control on the spy with a wide window."""
    world, _ = _seed(tmp_path)
    calls = _fake_refresh(monkeypatch)
    _read(world, since="1d", json="1")
    _read(world, json="1")
    assert calls == [], calls
    _read(world, since="30d", json="1")
    assert len(calls) == 1, calls
