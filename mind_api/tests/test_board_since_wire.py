"""WIRE-LEVEL pins for GET /v1/board/read `since` ().

`test_board_since_filter.py` pins the same behavior with 9 cases that build a
fake context and call ``board.read()`` directly. Per **guard-1462**, a test
seam's PLACEMENT is a silent scope declaration: everything UPSTREAM of the
injection point is structurally outside what any of those pins can falsify,
and nothing in their green run announces where that line was drawn. Production
is ``wrapper -> runtime -> HTTP GET -> handler``; those pins cover the last hop.

Two upstream hops carry real risk here, and BOTH are specific to what
g-115-3775 changed:

1. **QUERY-STRING ENCODING.** The timestamp shape that fix introduced contains
   COLONS (``2026-08-31T18:30:00``), which ``urlencode`` percent-escapes to
   ``%3A``. A direct handler call hands ``_parse_since`` an already-decoded
   string, so it can never exercise the escape/unescape round trip. If the
   runtime ever stopped decoding (or double-decoded), the filter would fail
   ONLY over the wire.

2. **NON-200 PROPAGATION.** g-115-3775 deliberately converted a silent 200 into
   a 400. ``urllib`` RAISES ``HTTPError`` on 4xx rather than returning it, so
   the status only reaches a caller that catches it — and a caller that
   discards non-200 turns that new loud failure back into a quiet empty
   result, which is precisely the silent-drop the 400 was introduced to
   prevent.

These use the in-process ``running_daemon`` fixture (conftest): a threaded
``ThreadingHTTPServer`` on an EPHEMERAL port against a tmp project root. It
does NOT spawn a subprocess daemon and does NOT touch the repo's real
``mind_api/state/daemon.port``, so it is not the daemon-lifecycle class
**guard-672** restricts to quiescent windows.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest


TS_FMT = "%Y-%m-%dT%H:%M:%S"


def _get(port: int, query: dict, *, agent: str = "alpha") -> tuple[int, str]:
    """GET /v1/board/read over a real socket.

    Returns (status, body) for BOTH success and error responses — urllib
    raises HTTPError on 4xx, and catching it here is the whole point of hop 2.
    """
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}/v1/board/read?{qs}"
    req = urllib.request.Request(url)
    req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _ids(body: str) -> list[str]:
    return [json.loads(ln)["id"] for ln in body.splitlines() if ln.strip()]


@pytest.fixture
def seeded_board(running_daemon) -> tuple[int, datetime]:
    """One channel straddling `cutoff`: a 60-day-old row and a 5-minute-old row.

    Any correct filter keyed on cutoff returns exactly the recent row; a filter
    that silently no-ops returns both. Mirrors the handler-level fixture so a
    wire failure is attributable to the transport, not to different data.
    """
    project_root, port = running_daemon
    now = datetime.now()
    cutoff = now - timedelta(hours=1)

    board_dir = project_root / "world" / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": "msg-old", "timestamp": (now - timedelta(days=60)).strftime(TS_FMT),
         "author": "alpha", "type": "finding", "text": "stale finding",
         "tags": ["fresh-eyes-code"]},
        {"id": "msg-recent", "timestamp": (now - timedelta(minutes=5)).strftime(TS_FMT),
         "author": "alpha", "type": "finding", "text": "this-invocation finding",
         "tags": ["fresh-eyes-code"]},
    ]
    with (board_dir / "findings.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return port, cutoff


# --- Hop 1: query-string encoding -------------------------------------------

def test_colon_bearing_timestamp_is_percent_encoded_on_the_wire():
    """POSITIVE CONTROL for the hop under test (guard-2988).

    A test that merely passes a timestamp proves nothing about encoding if the
    value never actually gets escaped — the probe would be present but
    constant. Pin the escape itself, so the round-trip assertions below are
    known to exercise a real transformation rather than an identity.
    """
    ts = "2026-08-31T18:30:00"
    qs = urllib.parse.urlencode({"channel": "findings", "since": ts})
    assert "%3A" in qs, (
        f"urlencode did not escape the colons in {ts!r} (got {qs!r}) — the "
        "encoding hop these pins exist to cover is not being exercised")
    assert ts not in qs, "raw colon-bearing timestamp survived urlencode unescaped"
    # And it must decode back to exactly what the parser expects.
    assert urllib.parse.parse_qs(qs)["since"][0] == ts


def test_timestamp_since_filters_over_the_wire(seeded_board):
    """Goal check 1: a colon-bearing timestamp survives encoding AND filters."""
    port, cutoff = seeded_board
    status, body = _get(port, {"channel": "findings",
                               "since": cutoff.strftime(TS_FMT), "json": "1"})
    assert status == 200, f"timestamp since returned {status}: {body[:200]}"
    ids = _ids(body)
    assert ids, "timestamp filter returned nothing over the wire — fixture or route broken"
    assert ids == ["msg-recent"], (
        f"timestamp `since` did not scope the read over HTTP (got {ids}) — the "
        "percent-encoded colons did not survive the round trip")


def test_duration_since_filters_over_the_wire(seeded_board):
    """The pre-existing shape must also hold over the wire — additive check."""
    port, _ = seeded_board
    status, body = _get(port, {"channel": "findings", "since": "1h", "json": "1"})
    assert status == 200, f"duration since returned {status}: {body[:200]}"
    assert _ids(body) == ["msg-recent"]


# --- Hop 2: non-200 propagation ---------------------------------------------

@pytest.mark.parametrize("bad", ["5x", "yesterday", "2026-07-29"])
def test_unparseable_since_is_400_over_the_wire(seeded_board, bad):
    """Goal outcome 2: assert the HTTP STATUS *and* that no content leaks.

    Status alone would still pass if a refactor returned 400 while streaming
    the unfiltered channel; the membership assert is what pins the hazard.
    Reaching this assertion at all depends on _get catching HTTPError — a
    caller that let it propagate, or swallowed it into an empty result, is the
    regression this case exists to catch.
    """
    port, _ = seeded_board
    status, body = _get(port, {"channel": "findings", "since": bad, "json": "1"})
    assert status == 400, (
        f"since={bad!r} returned {status} over the wire, not 400 — an "
        "unparseable filter must never be answered with an unscoped 200")
    assert "msg-old" not in body, "400 response leaked the unfiltered channel over the wire"
    assert "msg-recent" not in body, "400 response leaked channel content over the wire"
    assert "invalid_param" in body


def test_absent_since_returns_everything_over_the_wire(seeded_board):
    """Specificity control: the 400 fires on a BAD value, not on no value.

    Without this, a mutation making `since` mandatory would pass every case
    above (guard-1660 — a mutation-proof PASS measures sensitivity only).
    """
    port, _ = seeded_board
    status, body = _get(port, {"channel": "findings", "json": "1"})
    assert status == 200
    assert sorted(_ids(body)) == ["msg-old", "msg-recent"]
