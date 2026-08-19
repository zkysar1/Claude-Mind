"""GET /v1/board/read — parity with `board.py read`.

Required:
    channel=<name>

Optional filters (all combinable):
    since=<duration|timestamp>
                         duration  e.g. 1h, 30m, 2d
                         timestamp e.g. 2026-07-29T00:45:45
                         an unparseable value is a 400, never a silent no-op
                         (g-115-3775)
    author=<name>
    tag=<tag>
    type=<message-type>
    last=<N>             show only last N messages after filters
    json=1               output as JSONL (one message per line)
                          default: human-readable [ts] author (type): text ...
    unread_only=1        filter out messages already seen by the requesting agent
    mark_read=1          after returning messages, append their IDs to the
                          per-channel reads sidecar so subsequent reads skip them

Each channel is its own JSONL file under <world>/board/<channel>.jsonl.
Empty/missing channel produces a plain-text "Channel '<name>' is empty
or does not exist." line, matching the CLI.

Equivalence target: stdout of `python3 core/scripts/board.py read --channel ... [...]`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from ..jsonl_cache import cache
from ..agent_paths import assert_not_cruft


def _channel_path(ctx, channel: str):
    return ctx.paths.world / "board" / f"{channel}.jsonl"


def _reads_sidecar_path(ctx, channel: str):
    """Per-channel read-tracking sidecar: world/board/<channel>-reads.jsonl.

    Mirrors board.py:reads_sidecar_path (line 318-320).
    """
    return ctx.paths.world / "board" / f"{channel}-reads.jsonl"


def _load_seen_set(ctx, channel: str, agent: str) -> set:
    """Load msg_ids already read by `agent` from the sidecar.

    Mirrors board.py:_load_read_msg_ids (lines 375-400). Fail-open: returns
    empty set on any error so unread_only never blocks the read path.
    """
    sidecar = _reads_sidecar_path(ctx, channel)
    if not sidecar.exists():
        return set()
    seen = set()
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("reader_agent") == agent:
                    mid = row.get("msg_id")
                    if mid:
                        seen.add(mid)
    except OSError:
        return set()
    return seen


def _mark_read_append(ctx, channel: str, agent: str, messages: list, seen: set):
    """Append unseen message IDs to the reads sidecar.

    Mirrors board.py cmd_read mark_read block (lines 276-296). Fail-open:
    errors writing the sidecar are logged to stderr but do NOT block the read.
    """
    sidecar = _reads_sidecar_path(ctx, channel)
    try:
        assert_not_cruft(sidecar.parent, "mkdir (board reads sidecar)")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        # <channel>-reads.jsonl is an S3-backed governed store (registered in
        # coordination_merge._HANDLERS), and an append never reads the file — so
        # without this nothing on the mark-read path ever pulls it and the append
        # extends a stale mirror (). Fail-open by return value, never
        # by raise; this whole block is already best-effort by design.
        from storage_backend import ensure_local_before_append
        ensure_local_before_append(sidecar)
        read_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with open(sidecar, "a", encoding="utf-8") as f:
            for m in messages:
                mid = m.get("id")
                if not mid or mid in seen:
                    continue
                row = json.dumps({
                    "msg_id": mid,
                    "reader_agent": agent,
                    "reader_sid": "",
                    "read_at": read_at,
                }, ensure_ascii=False)
                f.write(row + "\n")
                seen.add(mid)
    except Exception as e:
        import sys
        print(f"[board.read] WARN: mark_read append failed: {e}", file=sys.stderr)


def _parse_duration(s: str):
    """e.g. '1h' -> timedelta(hours=1). Returns None on parse failure."""
    if not s:
        return None
    unit = s[-1].lower()
    try:
        value = int(s[:-1])
    except ValueError:
        return None
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    return None


def _parse_since(s: str, now=None):
    """Resolve a `since` query value to an absolute cutoff datetime.

    Two accepted shapes:
      - relative duration  ``<int><m|h|d>``            e.g. ``24h``, ``30m``, ``7d``
      - absolute timestamp ``YYYY-MM-DDTHH:MM:SS``     the naive form every
        framework caller mints via ``date +%Y-%m-%dT%H:%M:%S``

    Returns the cutoff datetime, or None when the value matches NEITHER shape.

    Callers MUST treat None as a client error. Do NOT restore the old
    ``if delta:`` gate, which skipped the whole filter block on an unparseable
    value and returned the entire channel at HTTP 200 — a zero-signal read
    presented as a scoped one (verify-before-assuming.md rule 4). Measured on
    the live daemon 2026-07-29 (alpha, cc-04), same channel seconds apart:
    ``--since 2026-01-01T00:00:00`` returned 5069 lines, ``--since 1h``
    returned 13.

    The timestamp shape is added here rather than removed from the caller
    because ``world/conventions/post-execution.md`` Step 1.75a mints an absolute
    instant on purpose — "findings posted since I started this goal" is not
    expressible as a whole-unit duration without rounding and a race.

    Note the ``is not None`` below is load-bearing and NOT a style choice: the
    old truthiness test ALSO swallowed a legitimately-parsed ``0h``
    (``timedelta(0)`` is falsy), silently widening the window to unbounded.
    That second silent drop is not hypothetical — ``infra-streak-notify.sh:393``
    carries a ceil() specifically because an accidental ``0h`` board query
    "re-permitted the cross-claimant double email" (g-249-33). Truthiness on a
    parse result conflates "no value" with "a valid zero". (g-115-3775)
    """
    if not s:
        return None
    now = now or datetime.now()
    delta = _parse_duration(s)
    if delta is not None:
        return now - delta
    return _parse_ts(s)


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    channel = (q.get("channel") or "").strip()
    if not channel:
        return Response.error(400, "missing_param",
                              "query parameter 'channel' is required")

    ch_path = _channel_path(ctx, channel)
    if not ch_path.exists():
        # Match the CLI: prints a single human-readable line. JSON mode still
        # gets the same text — the CLI doesn't branch on json_output here.
        return Response.text(
            f"Channel '{channel}' is empty or does not exist.",
            content_type="text/plain",
        )

    messages = list(cache().get(ch_path))

    since = q.get("since")
    if since:
        cutoff = _parse_since(since)
        if cutoff is None:
            return Response.error(
                400, "invalid_param",
                "query parameter 'since' must be a duration (<int><m|h|d>, "
                "e.g. '24h') or a timestamp (YYYY-MM-DDTHH:MM:SS); "
                f"got {since!r}")
        messages = [
            m for m in messages
            if _parse_ts(m.get("timestamp")) and _parse_ts(m.get("timestamp")) >= cutoff
        ]

    author = q.get("author")
    if author:
        messages = [m for m in messages if m.get("author") == author]

    msg_type = q.get("type")
    if msg_type:
        messages = [m for m in messages if m.get("type") == msg_type]

    tag = q.get("tag")
    if tag:
        messages = [m for m in messages if tag in (m.get("tags") or [])]

    last_raw = q.get("last")
    if last_raw:
        try:
            last_n = int(last_raw)
        except ValueError:
            return Response.error(400, "invalid_param", "last must be integer")
        messages = messages[-last_n:]

    # T1.7: --unread-only / --mark-read parity (board.py lines 245-296).
    from ._jsonl_common import flag as _flag
    unread_only = _flag(q, "unread_only")
    mark_read = _flag(q, "mark_read")
    current_agent = ctx.paths.agent_name or "unknown"
    seen = (_load_seen_set(ctx, channel, current_agent)
            if (unread_only or mark_read) else set())
    if unread_only:
        messages = [m for m in messages if m.get("id") not in seen]

    as_json = (q.get("json") or "").lower() not in ("", "0", "false", "no")

    if as_json:
        # CLI prints one JSON object per line.
        if mark_read and messages:
            _mark_read_append(ctx, channel, current_agent, messages, seen)
        lines = [json.dumps(m, ensure_ascii=False) for m in messages]
        return Response.text("\n".join(lines), content_type="application/json")

    # Human-readable. Mirror the CLI exactly: two lines per message + blank line.
    out = []
    for msg in messages:
        tags = (msg.get("tags") or [])
        tags_str = f" [{', '.join(tags)}]" if tags else ""
        reply = f" (reply to {msg['reply_to']})" if msg.get("reply_to") else ""
        mtype = msg.get("type", "status")
        type_label = f" ({mtype})" if mtype and mtype != "status" else ""
        out.append(
            f"[{msg.get('timestamp', '?')}] {msg.get('author', '?')}{type_label}: "
            f"{msg.get('text', '')}{tags_str}{reply}"
        )
        out.append(f"  id: {msg.get('id', '')}")
        out.append("")

    # T1.7: mark_read AFTER building output (mirrors CLI: display then mark).
    if mark_read and messages:
        _mark_read_append(ctx, channel, current_agent, messages, seen)

    return Response.text("\n".join(out), content_type="text/plain")


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def register(routes) -> None:
    routes[("GET", "/v1/board/read")] = read
