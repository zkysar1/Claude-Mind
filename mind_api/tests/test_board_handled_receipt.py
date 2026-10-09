"""SHOWN vs HANDLED board read receipts ().

2026-08-11 failure: a directive that one iteration SAW but did not ack was consumed by
`--unread-only --mark-read` and went permanently invisible to the ack path, because the
receipt written on first display was also the key the ack dedup filtered on. The fix splits
the receipt: --mark-read still writes SHOWN, only a disposition (a reply with reply_to, or
an explicit kind=handled mark) writes HANDLED, and unhandled_only filters on HANDLED.

These tests reproduce the failure, then show the g-115-2990 ack-once property still holds.
The conftest fixture seeds world/board/general.jsonl with msg-1 and msg-2.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest


def _call(port, method, path, query, body=b"", *, agent):
    url = f"http://127.0.0.1:{port}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, data=body if method == "POST" else None, method=method)
    req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


SEEDED = ("msg-1", "msg-2")


def _read_ids(port, agent, *, seeded_only=True, **flags):
    """IDs a /v1/board/read call returns, sorted. By default only the two seeded posts:
    the replies these tests post are themselves unhandled posts in the same channel,
    where the loop's real ACK read is narrowed by --type directive."""
    q = {"channel": "general", "json": "1"}
    q.update({k: str(v) for k, v in flags.items()})
    status, body = _call(port, "GET", "/v1/board/read", q, agent=agent)
    assert status == 200, body
    ids = sorted(json.loads(ln)["id"] for ln in body.splitlines() if ln.strip())
    return [i for i in ids if i in SEEDED] if seeded_only else ids


def _reply(port, agent, parent):
    status, body = _call(port, "POST", "/v1/board/post",
                         {"channel": "general", "reply_to": parent}, b"Acknowledged", agent=agent)
    assert status == 200, body


def _mark(port, agent, ids, **extra):
    q = {"channel": "general", "ids": ids}
    q.update(extra)
    status, body = _call(port, "POST", "/v1/board/mark-read", q, agent=agent)
    assert status == 200, body
    return json.loads(body)


def _rows(project_root):
    p = project_root / "world" / "board" / "general-reads.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _handled(project_root, agent, msg_id):
    return [r for r in _rows(project_root)
            if r["reader_agent"] == agent and r["msg_id"] == msg_id and r.get("kind") == "handled"]


# The ACK read the loop runs: the wrapper sends unread_only beside unhandled_only.
ACK = {"unhandled_only": 1, "unread_only": 1, "mark_read": 1}


def test_shown_but_unacted_post_reappears_then_stops_once_handled(running_daemon):
    """Reproduces 2026-08-11, then shows the receipt that ends it."""
    project_root, port = running_daemon

    # Iteration 1 SEES both posts and records them SHOWN.
    assert _read_ids(port, "alpha", **ACK) == ["msg-1", "msg-2"]
    # Iteration 2: nothing was acted on, so both must come back. Before the split the
    # second ACK read returned nothing and the directive was invisible for good.
    assert _read_ids(port, "alpha", **ACK) == ["msg-1", "msg-2"]
    # The SHOWN filter still hides them: --unread-only keeps its meaning for every other caller.
    assert _read_ids(port, "alpha", unread_only=1) == []

    # An ack posted with reply_to is the disposition for msg-1 only.
    _reply(port, "alpha", "msg-1")
    assert _read_ids(port, "alpha", **ACK) == ["msg-2"]
    # An explicit disposition (a moot or FYI item) handles msg-2.
    assert _mark(port, "alpha", "msg-2", kind="handled")["marked"] == 1
    assert _read_ids(port, "alpha", **ACK) == []


def test_ack_is_written_once_per_agent(running_daemon):
    """ still holds: re-acking a directive does not stack receipts."""
    project_root, port = running_daemon
    _reply(port, "alpha", "msg-1")
    _reply(port, "alpha", "msg-1")
    assert len(_handled(project_root, "alpha", "msg-1")) == 1


def test_handled_receipt_is_per_agent(running_daemon):
    project_root, port = running_daemon
    _reply(port, "alpha", "msg-1")
    assert _read_ids(port, "bravo", **ACK) == ["msg-1", "msg-2"]
    assert _handled(project_root, "bravo", "msg-1") == []


def test_handled_row_shape_and_dedupe_are_independent_of_shown(running_daemon):
    project_root, port = running_daemon
    assert _mark(port, "alpha", "msg-1")["marked"] == 1            # SHOWN
    # A handled mark is not skipped because a SHOWN row already exists.
    out = _mark(port, "alpha", "msg-1", kind="handled")
    assert out["marked"] == 1 and out["kind"] == "handled"
    # Re-marking either kind is a no-op, so receipts never stack.
    assert _mark(port, "alpha", "msg-1", kind="handled")["already_read"] == 1
    assert _mark(port, "alpha", "msg-1")["already_read"] == 1

    shown, handled = _rows(project_root)
    # A SHOWN row stays byte-compatible with every row written before the split.
    assert list(shown.keys()) == ["msg_id", "reader_agent", "reader_sid", "read_at"]
    assert list(handled.keys()) == ["msg_id", "reader_agent", "reader_sid", "read_at", "kind"]
    assert handled["kind"] == "handled"


def test_reply_without_reply_to_and_plain_posts_write_no_handled_receipt(running_daemon):
    """guard-6617: an ack that is not threaded does not dispose of anything."""
    project_root, port = running_daemon
    status, _ = _call(port, "POST", "/v1/board/post", {"channel": "general"},
                      b"Acknowledged (unthreaded)", agent="alpha")
    assert status == 200
    assert _rows(project_root) == []
    # Nothing was disposed of, so all three posts (the two seeded and the unthreaded ack) return.
    assert len(_read_ids(port, "alpha", seeded_only=False, **ACK)) == 3


def test_unknown_kind_is_refused(running_daemon):
    _, port = running_daemon
    with pytest.raises(urllib.error.HTTPError) as exc:
        _mark(port, "alpha", "msg-1", kind="bogus")
    assert exc.value.code == 400
