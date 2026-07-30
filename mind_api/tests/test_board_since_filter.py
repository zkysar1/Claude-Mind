"""Regression pins for GET /v1/board/read `since` handling ().

Before this fix, `_parse_duration` accepted only ``<int><m|h|d>``; an ISO
timestamp made ``int(s[:-1])`` raise, the function returned None, and the
caller's ``if delta:`` gate skipped the ENTIRE filter block — returning every
message in the channel at HTTP 200 with no error and no warning.

That mattered because ``world/conventions/post-execution.md`` Step 1.75a mints
an absolute timestamp for the sole purpose of feeding Step 1.75d's
``board-read.sh --since "<TIMESTAMP>"``, whose result feeds the fail-closed
Step 1.75e commit gate. An agent complying literally read its whole fresh-eyes
history — so the gate either blocked forever or got eyeballed past, with no
correct-firing regime.

Two behaviors are pinned here, and BOTH must hold:
  - the timestamp shape now parses  (allow-case)
  - an unparseable value is a 400   (refuse-case) — never the whole channel
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mind_api.src.endpoints import board


# --- Harness ----------------------------------------------------------------

class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        # read() reads agent_name for the --unread-only / --mark-read parity
        # block, which runs after the filters even when neither flag is set.
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, world: Path, query: dict):
        self.paths = _FakePaths(world)
        self.query = query
        self.headers = {}


TS_FMT = "%Y-%m-%dT%H:%M:%S"


def _seed(tmp_path: Path) -> tuple[Path, datetime]:
    """One channel with an OLD message and a RECENT one, straddling `cutoff`.

    Returns (world, cutoff). Any correct filter keyed on `cutoff` returns
    exactly the recent message; a filter that silently no-ops returns both.
    """
    now = datetime.now()
    old = now - timedelta(days=60)
    recent = now - timedelta(minutes=5)
    cutoff = now - timedelta(hours=1)

    world = tmp_path / "world"
    (world / "board").mkdir(parents=True)
    rows = [
        {"id": "msg-old", "timestamp": old.strftime(TS_FMT), "author": "alpha",
         "type": "finding", "text": "stale finding", "tags": ["fresh-eyes-code"]},
        {"id": "msg-recent", "timestamp": recent.strftime(TS_FMT), "author": "alpha",
         "type": "finding", "text": "this-invocation finding", "tags": ["fresh-eyes-code"]},
    ]
    with (world / "board" / "findings.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return world, cutoff


def _read(world: Path, query: dict):
    resp = board.read(_FakeCtx(world, query))
    return resp.status, resp.body.decode("utf-8")


def _ids(body: str) -> list[str]:
    return [json.loads(ln)["id"] for ln in body.splitlines() if ln.strip()]


# --- Unit: the parser itself ------------------------------------------------

def test_parse_since_accepts_both_shapes_and_rejects_neither():
    now = datetime(2026, 7, 29, 12, 0, 0)

    # duration shape — unchanged behavior
    assert board._parse_since("1h", now) == now - timedelta(hours=1)
    assert board._parse_since("30m", now) == now - timedelta(minutes=30)
    assert board._parse_since("2d", now) == now - timedelta(days=2)

    # timestamp shape — the shape Step 1.75a mints, newly accepted
    assert board._parse_since("2026-07-29T00:45:45", now) == datetime(2026, 7, 29, 0, 45, 45)

    # a legitimately-parsed ZERO must survive. timedelta(0) is falsy, so the
    # pre-fix `if delta:` gate swallowed `0h` too and widened the window to
    # unbounded — a second silent drop hiding in the same line, and the reason
    # infra-streak-notify.sh:393 carries a ceil() ().
    assert board._parse_since("0h", now) == now
    assert board._parse_since("0m", now) == now

    # neither shape — None, which the caller MUST turn into a 400
    for bad in ("5x", "yesterday", "2026-07-29", "", "h", "abc1h"):
        assert board._parse_since(bad, now) is None, f"{bad!r} should not parse"


# --- Behavioral: allow-cases ------------------------------------------------

def test_duration_since_still_filters(tmp_path):
    """The pre-existing shape must keep working — this fix is additive."""
    world, _ = _seed(tmp_path)
    status, body = _read(world, {"channel": "findings", "since": "1h", "json": "1"})
    assert status == 200
    ids = _ids(body)
    assert ids, "duration filter returned nothing — fixture or filter is broken"
    assert ids == ["msg-recent"]


def test_timestamp_since_filters(tmp_path):
    """The NEW shape. Pre-fix this returned BOTH messages at HTTP 200."""
    world, cutoff = _seed(tmp_path)
    status, body = _read(world, {"channel": "findings",
                                 "since": cutoff.strftime(TS_FMT), "json": "1"})
    assert status == 200
    ids = _ids(body)
    assert ids, "timestamp filter returned nothing — fixture or filter is broken"
    assert ids == ["msg-recent"], (
        f"timestamp `since` did not scope the read (got {ids}) — the silent "
        "no-op returned the whole channel before g-115-3775")


def test_step_1_75d_call_shape_round_trips(tmp_path):
    """Goal check 2: the literal convention call shape must scope correctly.

    post-execution.md Step 1.75d:
      board-read.sh --channel findings --since "<TIMESTAMP-FROM-1.75a>"
                    --tag fresh-eyes-code --author $MIND_AGENT --json
    """
    world, cutoff = _seed(tmp_path)
    status, body = _read(world, {
        "channel": "findings",
        "since": cutoff.strftime(TS_FMT),
        "tag": "fresh-eyes-code",
        "author": "alpha",
        "json": "1",
    })
    assert status == 200
    ids = _ids(body)
    assert ids, "Step 1.75d shape returned nothing — 1.75e would pass vacuously"
    assert ids == ["msg-recent"]


# --- Behavioral: refuse-case ------------------------------------------------

@pytest.mark.parametrize("bad", ["5x", "yesterday", "2026-07-29"])
def test_unparseable_since_is_400_not_the_whole_channel(tmp_path, bad):
    """The root hazard: a filter that silently declines to filter.

    Asserts BOTH halves — the status is 400 AND the stale message is absent.
    Status alone would still pass if a future refactor returned 400 while
    leaking the body; the membership assert is what pins the actual hazard.
    """
    world, _ = _seed(tmp_path)
    status, body = _read(world, {"channel": "findings", "since": bad, "json": "1"})
    assert status == 400, (
        f"since={bad!r} returned {status}, not 400 — an unparseable filter must "
        "never be answered with an unscoped 200 (verify-before-assuming rule 4)")
    assert "msg-old" not in body, "400 response leaked the unfiltered channel"
    assert "invalid_param" in body


def test_zero_duration_since_is_honored_not_swallowed(tmp_path):
    """`0h` is a VALID window meaning "nothing older than now", not "no filter".

    Pre-fix, timedelta(0) was falsy so `if delta:` skipped the filter and
    returned the whole channel — the same silent-drop as an unparseable value,
    reached by a completely different route.
    """
    world, _ = _seed(tmp_path)
    status, body = _read(world, {"channel": "findings", "since": "0h", "json": "1"})
    assert status == 200
    assert _ids(body) == [], (
        "since=0h returned messages — a falsy-but-valid parse was swallowed "
        "and the window silently widened to unbounded")


def test_absent_since_returns_everything(tmp_path):
    """Specificity control: the 400 fires on a BAD value, not on no value.

    Without this, a mutation making `since` mandatory would pass every test
    above (guard-1660 — a mutation-proof PASS measures sensitivity only).
    """
    world, _ = _seed(tmp_path)
    status, body = _read(world, {"channel": "findings", "json": "1"})
    assert status == 200
    assert sorted(_ids(body)) == ["msg-old", "msg-recent"]
