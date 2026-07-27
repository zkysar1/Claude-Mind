"""POST /v1/board/post, POST /v1/board/mark-read, GET /v1/board/channels.

Daemonises core/scripts/board.py cmd_post / cmd_mark_read / cmd_channels.
The read path (GET /v1/board/read) already lives in endpoints/board.py; this
module adds the WRITE + listing surface.

BYTE-COMPATIBILITY with the CLI write path is a hard requirement so a channel
alternately written by the CLI and the daemon does not churn .history / git.
Compatibility holds because:

  1. Each appended line is `json.dumps(rec, ensure_ascii=True) + "\n"` —
     the EXACT serialization _fileops.locked_append_jsonl uses (board.py
     cmd_post / cmd_mark_read both write through that helper). The record
     dict is built with the SAME key insertion order as board.py's _build
     (board.py:128-142) and cmd_mark_read row (board.py:234-238), so the
     JSON line is byte-identical except for the wall-clock fields (id ts,
     timestamp, read_at) which differ by construction between any two writes.

  2. Append is single-line (open "a"), NEVER a full rewrite — a rewrite
     would re-serialize pre-existing lines and reflow the channel file.

  3. The msg-id count component is computed INSIDE file_locks.locked(path)
     from the channel's record snapshot, closing the
     msg-YYYYMMDD-HHMMSS-author-NNN allocator race (board.py:121-144).

  4. Agent attribution comes from the X-Mind-Agent header, NOT os.environ.
     The daemon serves all agents from one process; _fileops._agent_name's
     env read would mis-attribute every write to whatever the daemon was
     started with. Mirrors endpoints/store.py _agent_name / _require_agent_header.

  5. history.snapshot + changelog.append mirror _fileops.save_history /
     append_changelog (board channels live under WORLD_DIR, so resolve_base_dir
     returns WORLD_DIR — daemon uses base = ctx.paths.world to match).

SIDE-EFFECTS replicated from board.py cmd_post:
  - coordination channel -> _wake_signals.touch_peer_signals("board-activity")
  - findings channel with guard-NNN / rb-NNN tags -> bump the cited entry's
    utilization.times_inferred_helpful. board.py routes via _rt.store_increment
    (g-115-2351 — its old reasoning-bank.py subprocess was a silent no-op after
    H2 Wave 2); here we drive the canonical in-process store increment
    (store.py increment), no subprocess hop. NEVER switch the counter to
    times_cited — it carries zero weight in the active utilization_score v1
    formula (board.py cmd_post, guard-343 measurement-gap incident).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .. import file_locks, history, changelog
from ..jsonl_cache import cache as _jsonl_cache
from ..agent_paths import assert_not_cruft

from _fileops import _validate_no_surrogates  # noqa: E402
from storage_backend import get_backend  # noqa: E402  # s5c: own-cloud read freshness

DEFAULT_CHANNELS = ["general", "findings", "coordination", "decisions"]

# Citation tag form: guard-NNN / rb-NNN, 3+ digits (board.py cmd_post twin).
# : the old \d{3} (exactly three) silently excluded every 4-digit
# ID once rb/guard numbering crossed 999 (rb-3742, guard-1151 live) — modern
# findings cites produced ZERO attribution. Keep both regexes in sync.
_CITE_RE = re.compile(r"(?:guard|rb)-\d{3,}")


# ---------------------------------------------------------------------------
# Paths + small helpers
# ---------------------------------------------------------------------------

def _channel_path(ctx, channel: str) -> Path:
    return ctx.paths.world / "board" / f"{channel}.jsonl"


def _reads_sidecar_path(ctx, channel: str) -> Path:
    return ctx.paths.world / "board" / f"{channel}-reads.jsonl"


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _require_agent_header(ctx):
    """Reject board writes when X-Mind-Agent is missing (mirrors store.py)."""
    from ..server import Response
    agent = (ctx.headers.get("x-mind-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for board writes. Caller "
            "environment likely has MIND_AGENT empty/unset (g-115-957).")
    return None


def _read_jsonl(path: Path):
    # s5c (own-cloud): force-fresh via the backend before reading. In-lock RMW
    # callers get lost-update prevention + the If-Match fence etag; plain probe
    # reads get the latest remote state. No-op on LocalBackend.
    get_backend().refresh(path)
    items = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _generate_message_id(author: str, items) -> str:
    """msg-YYYYMMDD-HHMMSS-author-NNN. Count from the in-lock snapshot
    (board.py:58-79, items branch)."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"msg-{ts}-{author}-{len(items) + 1:03d}"


class _SubCtx:
    """Minimal ctx shim to drive store.py handlers in-process (findings
    citation increment). Forwards the parent request's paths + headers
    (so the increment is attributed to the posting agent), supplies fresh
    query/body."""

    __slots__ = ("paths", "headers", "query", "body")

    def __init__(self, ctx, query):
        self.paths = ctx.paths
        self.headers = ctx.headers
        self.query = query
        self.body = b""


def _increment_citation(ctx, store: str, cite: str) -> None:
    """Drive the canonical store increment for a findings source-tag.
    Fail-open per-cite (board.py:180-188)."""
    from . import store as _store
    _store.increment(_SubCtx(ctx, {
        "store": store,
        "id": cite,
        "field": "utilization.times_inferred_helpful",
    }))


# ---------------------------------------------------------------------------
# POST /v1/board/post
# ---------------------------------------------------------------------------

def post(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/board/post?channel=&author=&type=&reply_to=&tags=  body: text.

    Message text is the raw request body (the CLI reads it from stdin).
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    channel = (ctx.query.get("channel") or "").strip()
    if not channel:
        return Response.error(400, "missing_param",
                              "query parameter 'channel' is required")

    text = (ctx.body or b"").decode("utf-8").strip()
    if not text:
        return Response.error(400, "empty_text",
                              "message text required in request body")

    author = (ctx.query.get("author") or "").strip() or _agent_name(ctx)
    msg_type = (ctx.query.get("type") or "").strip() or "status"
    reply_to = ctx.query.get("reply_to") or None
    tags_raw = ctx.query.get("tags")
    tags = [t.strip() for t in tags_raw.split(",")] if tags_raw else []
    session_id = (ctx.headers.get("x-mind-sid") or "").strip()

    ch_path = _channel_path(ctx, channel)
    base = ctx.paths.world
    agent = _agent_name(ctx)
    msg: dict = {}
    reply_warnings: list = []

    try:
        assert_not_cruft(ch_path.parent, "mkdir (board post)")
        ch_path.parent.mkdir(parents=True, exist_ok=True)
        with file_locks.locked(ch_path):
            items = _read_jsonl(ch_path)
            # : WARN (do NOT block) on a dangling reply_to — one that
            # references no existing message in this channel silently dangles
            # thread linkage (observed 2026-07-21: a reply posted with a GUESSED
            # msg id succeeded with no signal). Cross-box lag means the parent
            # may legitimately not be local yet, so this is advisory only.
            # Fail-open: any error skips the check. Twin: board.py cmd_post.
            if reply_to:
                try:
                    if reply_to not in {it.get("id") for it in items
                                        if isinstance(it, dict)}:
                        reply_warnings.append(
                            f"reply_to '{reply_to}' not found in channel "
                            f"'{channel}' ({len(items)} messages) — thread "
                            f"linkage may dangle (cross-box lag can delay the "
                            f"parent; not blocking)")
                except Exception:
                    pass
            # Key order MUST match board.py:128-142 for byte-compat.
            msg = {
                "id": _generate_message_id(author, items),
                "author": author,
                "session_id": session_id,
                "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "channel": channel,
                "type": msg_type,
                "text": text,
                "reply_to": reply_to,
                "tags": tags,
            }
            _validate_no_surrogates(msg, ch_path)
            history.snapshot(ch_path, base, agent,
                             summary=f"board-post {channel} {msg['id']}")
            # s5c (own-cloud): route the append through the backend so the post
            # reaches S3 (board posts are primary domain state, not a deferrable
            # audit log). LocalBackend does the IDENTICAL raw open('a') append —
            # byte-for-byte unchanged on the default path.
            get_backend().append_jsonl_record(ch_path, msg)
            changelog.append(base, agent, ch_path, "edit",
                             summary=f"board-post {channel} {msg['id']}",
                             lines_changed=1)
            _jsonl_cache().invalidate(ch_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # Side-effect 1: wake sleeping peers on coordination posts (board.py:156-161).
    if channel == "coordination":
        try:
            from _wake_signals import touch_peer_signals
            touch_peer_signals("board-activity")
        except Exception:
            pass

    # Side-effect 2: source-tag attribution on findings posts (board.py:174-188).
    if channel == "findings":
        cited = {t for t in msg["tags"] if _CITE_RE.fullmatch(t)}
        for cite in sorted(cited):
            store = "reasoning-bank" if cite.startswith("rb-") else "guardrails"
            try:
                _increment_citation(ctx, store, cite)
            except Exception:
                pass  # fail-open per-cite

    resp = {"ok": True, "id": msg["id"], "record": msg}
    if reply_warnings:
        resp["warnings"] = reply_warnings
    return Response.json(resp)


# ---------------------------------------------------------------------------
# POST /v1/board/mark-read
# ---------------------------------------------------------------------------

def mark_read(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/board/mark-read?channel=&reader=&ids=a,b,c  (or ids via body,
    one per line).

    Appends one row per NOT-already-read msg_id to <channel>-reads.jsonl.
    Idempotent for (msg_id, reader_agent): re-marking the same pair is a
    no-op. Cross-agent re-marks still write (board.py:194-243).
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err

    channel = (ctx.query.get("channel") or "").strip()
    if not channel:
        return Response.error(400, "missing_param",
                              "query parameter 'channel' is required")

    ids_raw = ctx.query.get("ids")
    if ids_raw:
        msg_ids = [i.strip() for i in ids_raw.split(",") if i.strip()]
    else:
        body = (ctx.body or b"").decode("utf-8")
        msg_ids = [line.strip() for line in body.splitlines() if line.strip()]
    if not msg_ids:
        return Response.error(400, "missing_ids",
                              "no message IDs (use ?ids=a,b,c or request body)")

    reader = (ctx.query.get("reader") or "").strip() or _agent_name(ctx)
    reader_sid = (ctx.headers.get("x-mind-sid") or "").strip()
    read_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    sidecar = _reads_sidecar_path(ctx, channel)
    base = ctx.paths.world
    agent = _agent_name(ctx)
    written = 0

    try:
        assert_not_cruft(sidecar.parent, "mkdir (board mark-read)")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with file_locks.locked(sidecar):
            existing = _read_jsonl(sidecar)
            seen = {r.get("msg_id") for r in existing
                    if r.get("reader_agent") == reader}
            new_rows = []
            for mid in msg_ids:
                if mid in seen:
                    continue
                # Key order MUST match board.py:234-238 for byte-compat.
                row = {
                    "msg_id": mid,
                    "reader_agent": reader,
                    "reader_sid": reader_sid,
                    "read_at": read_at,
                }
                _validate_no_surrogates(row, sidecar)
                new_rows.append(row)
                seen.add(mid)
                written += 1
            if new_rows:
                history.snapshot(sidecar, base, agent,
                                 summary=f"board-mark-read {channel}")
                # s5c (own-cloud): append each read-row through the backend so
                # the sidecar reaches S3. LocalBackend = identical raw open('a')
                # appends (one per row, same bytes/order). N is the unread count
                # (small); on own-cloud each is a fenced read+append+PUT.
                for row in new_rows:
                    get_backend().append_jsonl_record(sidecar, row)
                changelog.append(base, agent, sidecar, "edit",
                                 summary=f"board-mark-read {channel}",
                                 lines_changed=len(new_rows))
                _jsonl_cache().invalidate(sidecar)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "channel": channel, "marked": written,
                          "already_read": len(msg_ids) - written})


# ---------------------------------------------------------------------------
# GET /v1/board/channels
# ---------------------------------------------------------------------------

def channels(ctx) -> "Response":  # type: ignore[name-defined]
    """GET /v1/board/channels — list channels with message counts + last ts.

    Mirrors board.py cmd_channels enumeration (every *.jsonl under board/,
    including -reads sidecars) but returns structured JSON. The wrapper
    formats for human display.
    """
    from ..server import Response

    board_dir = ctx.paths.world / "board"
    result = []
    if board_dir.exists():
        for ch in sorted(board_dir.glob("*.jsonl")):
            name = ch.stem
            count = 0
            last_ts = None
            try:
                with ch.open("r", encoding="utf-8") as f:
                    last_line = ""
                    for line in f:
                        if line.strip():
                            count += 1
                            last_line = line
                if last_line:
                    try:
                        last_ts = json.loads(last_line).get("timestamp")
                    except (json.JSONDecodeError, KeyError):
                        last_ts = None
            except OSError:
                pass
            result.append({"name": name, "count": count, "last_timestamp": last_ts})

    return Response.json({"ok": True, "channels": result})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/board/post")] = post
    routes[("POST", "/v1/board/mark-read")] = mark_read
    routes[("GET", "/v1/board/channels")] = channels
