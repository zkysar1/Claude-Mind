"""Tests for T1.7: board.read --mark-read and --unread-only daemon parity.

The conftest fixture seeds world/board/general.jsonl with msg-1 and msg-2.
These tests verify that the daemon endpoint correctly reads/writes the
per-channel reads sidecar (world/board/<channel>-reads.jsonl) and filters
unread messages — mirroring board.py cmd_read lines 245-296.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request


def _get(port: int, path: str, query: dict, *, agent: str) -> tuple[int, str]:
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    req = urllib.request.Request(url)
    req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_mark_read_appends_to_sidecar(running_daemon):
    """mark_read=1 should create a reads sidecar with entries for returned messages."""
    project_root, port = running_daemon
    sidecar = project_root / "world" / "board" / "general-reads.jsonl"

    # Sidecar should not exist yet.
    assert not sidecar.exists()

    # Read with mark_read=1.
    status, body = _get(
        port, "/v1/board/read",
        {"channel": "general", "json": "1", "mark_read": "1"},
        agent="alpha",
    )
    assert status == 200

    # Verify the messages came back.
    messages = [json.loads(line) for line in body.strip().splitlines() if line.strip()]
    msg_ids = {m["id"] for m in messages}
    assert "msg-1" in msg_ids
    assert "msg-2" in msg_ids

    # Sidecar should now exist with entries for both messages.
    assert sidecar.exists()
    rows = []
    for line in sidecar.read_text(encoding="utf-8").strip().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    seen_ids = {r["msg_id"] for r in rows}
    assert "msg-1" in seen_ids
    assert "msg-2" in seen_ids
    # Every row should record the reader agent.
    for r in rows:
        assert r["reader_agent"] == "alpha"


def test_unread_only_filters_seen_messages(running_daemon):
    """unread_only=1 should exclude messages already marked read."""
    project_root, port = running_daemon

    # First: mark all messages as read.
    status, _ = _get(
        port, "/v1/board/read",
        {"channel": "general", "json": "1", "mark_read": "1"},
        agent="alpha",
    )
    assert status == 200

    # Second: read with unread_only=1 — should return zero messages.
    status, body = _get(
        port, "/v1/board/read",
        {"channel": "general", "json": "1", "unread_only": "1"},
        agent="alpha",
    )
    assert status == 200
    # Body should be empty (no unread messages).
    lines = [l for l in body.strip().splitlines() if l.strip()]
    assert len(lines) == 0


def test_mark_read_idempotent(running_daemon):
    """Calling mark_read=1 twice should not create duplicate sidecar entries."""
    project_root, port = running_daemon
    sidecar = project_root / "world" / "board" / "general-reads.jsonl"

    # Mark read twice.
    for _ in range(2):
        _get(
            port, "/v1/board/read",
            {"channel": "general", "json": "1", "mark_read": "1"},
            agent="alpha",
        )

    rows = []
    for line in sidecar.read_text(encoding="utf-8").strip().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    # Should have exactly 2 entries (msg-1 + msg-2), not 4.
    alpha_rows = [r for r in rows if r["reader_agent"] == "alpha"]
    alpha_ids = [r["msg_id"] for r in alpha_rows]
    assert alpha_ids.count("msg-1") == 1
    assert alpha_ids.count("msg-2") == 1


def test_unread_only_agent_scoped(running_daemon):
    """unread_only filtering is per-agent: alpha's reads don't affect bravo."""
    project_root, port = running_daemon

    # Alpha marks all as read.
    _get(
        port, "/v1/board/read",
        {"channel": "general", "json": "1", "mark_read": "1"},
        agent="alpha",
    )

    # Bravo with unread_only should still see both messages.
    status, body = _get(
        port, "/v1/board/read",
        {"channel": "general", "json": "1", "unread_only": "1"},
        agent="bravo",
    )
    assert status == 200
    messages = [json.loads(line) for line in body.strip().splitlines() if line.strip()]
    assert len(messages) == 2
