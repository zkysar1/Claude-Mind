#!/usr/bin/env python3
"""Message board engine for inter-agent communication.

Manages JSONL channel files in world/board/. Each channel is a separate file.
Messages are append-only — never edited or deleted.

Subcommands:
  post     — Post a message to a channel
  read     — Read messages from a channel
  channels — List available channels with message counts
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import WORLD_DIR

BOARD_DIR = WORLD_DIR / "board"
DEFAULT_CHANNELS = ["general", "findings", "coordination", "decisions"]

# Reference list — not enforced (any string accepted as --type).
# Source of truth: core/config/conventions/board.md → Message Types table.
VALID_MESSAGE_TYPES = [
    "claim",           # Agent claimed a goal for execution
    "release",         # Agent released a goal (failed/abandoned)
    "complete",        # Agent finished a goal
    "blocked",         # Agent is blocked on something
    "encoding",        # Agent is encoding to a tree node
    "finding",         # Agent discovered something
    "review-request",  # Code change needs peer review
    "escalation",      # Goal stuck after repeated failures
    "handoff",         # Goal done, follow-up needed by other agent
    "blocker-alert",   # Shared resource blocked
    "directive",           # Strategic direction or priority change
    "execution-feedback",  # Cross-agent goal quality feedback
    "status",              # General update (backward-compatible default)
]

def require_board():
    """Ensure board directory exists."""
    BOARD_DIR.mkdir(parents=True, exist_ok=True)

def channel_path(name):
    """Get the JSONL file path for a channel."""
    return BOARD_DIR / f"{name}.jsonl"

def generate_message_id(channel, author, *, items=None):
    """Generate a unique message ID.

    When `items` is supplied (the channel records, read inside a lock by
    cmd_post's allocator), the count is computed from that snapshot —
    no separate file read, no race window. When called outside a lock
    (legacy callers), the function still works but reads the file
    unlocked, which is the original race documented in the
    msg-20260428-045553-alpha-NNN finding.
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if items is None:
        # Legacy fallback: count existing lines without locking. Caller is
        # responsible for race-handling. cmd_post no longer takes this path.
        ch_path = channel_path(channel)
        count = 0
        if ch_path.exists():
            with open(ch_path, "r", encoding="utf-8") as f:
                count = sum(1 for line in f if line.strip())
    else:
        count = len(items)
    return f"msg-{ts}-{author}-{count + 1:03d}"

def parse_duration(duration_str):
    """Parse a duration string like '1h', '30m', '2d' into a timedelta."""
    if not duration_str:
        return None
    unit = duration_str[-1].lower()
    try:
        value = int(duration_str[:-1])
    except ValueError:
        return None
    if unit == "m":
        return timedelta(minutes=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    return None

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_post(args):
    """Post a message to a channel."""
    require_board()

    # Read message from stdin
    text = sys.stdin.read().strip()
    if not text:
        print("Error: No message text provided (pipe to stdin)", file=sys.stderr)
        sys.exit(1)

    author = args.author or os.environ.get("MIND_AGENT", "system")
    channel = args.channel

    # Structured message type (optional, defaults to "status" for backward compat)
    msg_type = getattr(args, "type", None) or "status"

    ch_path = channel_path(channel)
    from _fileops import locked_append_jsonl_with_allocator

    # Build msg INSIDE the lock so the count component of msg-id reflects
    # the channel's actual record count at write time. The previous
    # generate_message_id-then-locked_append split was the
    # msg-20260428-045553-alpha-NNN race — two posts in the same wall-clock
    # second from one agent's parallel processes both saw count=N and
    # both wrote msg-...-(N+1).
    def _build(items):
        return {
            "id": generate_message_id(channel, author, items=items),
            "author": author,
            # session_id is the PreToolUse-injected SID (core/scripts/bash-agent-inject.py).
            # Lets future readers distinguish observer posts from runner posts by comparing
            # against <agent>/session/running-session-id without needing a new schema concept.
            # Empty string when MIND_SID is absent (rare hook-timeout case) — purely additive.
            "session_id": os.environ.get("MIND_SID", ""),
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "channel": channel,
            "type": msg_type,
            "text": text,
            "reply_to": args.reply_to,
            "tags": [t.strip() for t in args.tags.split(",")] if args.tags else [],
        }

    msg = locked_append_jsonl_with_allocator(ch_path, _build)
    print(msg["id"])

    # : wake any sleeping peer agents when this is a coordination
    # post — peers polling interruptible-sleep.sh will exit 2 (light-precheck
    # branch) within 1s instead of running their full quiescent sleep.
    # Coordination is the cross-agent signal channel (claims, releases,
    # directives, abstentions). Other channels currently do not fan wake
    # signals — findings/general/decisions deliberately stay quiet so a busy
    # board doesn't thrash partner sleep cycles.
    # Best-effort: import lazily and swallow any failure. A board post must
    # not fail because a peer's filesystem is unavailable.
    if channel == "coordination":
        try:
            from _wake_signals import touch_peer_signals
            touch_peer_signals("board-activity")
        except Exception:
            pass

    # : source-tag attribution — guard-NNN / rb-NNN tags on a
    # findings post bump the named entry's times_inferred_helpful (half-
    # weight in utilization_score per reasoning-bank.py recompute). The
    # finding's existence IS positive signal: the gate fired, defect surfaced.
    #
    # CRITICAL — never change `times_inferred_helpful` to `times_cited`:
    # times_cited carries zero weight in the active v1 utilization_score
    # formula. The whole point is to FLOW value into the score; switching
    # to times_cited silently re-creates the measurement gap (guard-343
    # read 0.04 despite producing 57% of critical findings, n=246,
    # 2026-04-27..05-09).
    #
    # DAEMON-ROUTED (1): the previous subprocess spawn of
    # `reasoning-bank.py <family> increment ...` had been a silent no-op
    # since H2 Wave 2 (2026-05-15) removed the rb CLI subcommands — the
    # child imported the library and exited 0 without writing. Route
    # through _rt.store_increment (POST /v1/store/increment), the same
    # canonical Python->daemon client utilization-feedback.py uses.
    # Fail-soft with a VISIBLE stderr line per cite — silent swallow is
    # exactly how this path stayed dead for two months.
    # ID width \d{3,}: rb/guard IDs crossed into 4 digits (rb-3742,
    # guard-1151); the old \d{3} silently excluded every modern cite.
    if channel == "findings":
        cited = {t for t in msg["tags"] if re.fullmatch(r"(?:guard|rb)-\d{3,}", t)}
        if cited:
            try:
                import _rt
            except Exception as e:  # fail-soft: the post already landed
                print(f"[board] citation increments skipped — _rt import "
                      f"failed: {e}", file=sys.stderr)
                _rt = None
            for cite in sorted(cited) if _rt else []:
                store = "reasoning-bank" if cite.startswith("rb-") else "guardrails"
                try:
                    _rt.store_increment(store, cite,
                                        "utilization.times_inferred_helpful")
                except Exception as e:  # fail-soft per-cite, visibly
                    print(f"[board] citation increment failed for {cite} "
                          f"({store}): {e}", file=sys.stderr)

def reads_sidecar_path(channel):
    """Get the sidecar path for a channel's read events."""
    return BOARD_DIR / f"{channel}-reads.jsonl"

def cmd_mark_read(args):
    """Mark message IDs as read by the current agent.

    Reads msg_ids from --ids (comma-separated) OR from stdin (one per line).
    Appends one row per msg_id to <channel>-reads.jsonl.
    Idempotent: re-marking the same msg_id by the same agent is allowed but
    consumers can dedupe by (msg_id, reader_agent) when reporting.
    """
    require_board()

    if args.ids:
        msg_ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    else:
        msg_ids = [line.strip() for line in sys.stdin if line.strip()]

    if not msg_ids:
        print("Error: No message IDs provided (use --ids or pipe stdin)",
              file=sys.stderr)
        sys.exit(1)

    reader = args.reader or os.environ.get("MIND_AGENT", "unknown")
    reader_sid = os.environ.get("MIND_SID", "")
    read_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    sidecar = reads_sidecar_path(args.channel)
    sidecar.parent.mkdir(parents=True, exist_ok=True)

    # Dedup against existing sidecar rows so re-marking the same msg_id by the
    # same agent is a no-op (idempotent). Cross-agent re-marks still write.
    seen = _load_read_msg_ids(args.channel, reader)

    from _fileops import locked_append_jsonl
    written = 0
    for mid in msg_ids:
        if mid in seen:
            continue
        # locked_append_jsonl takes ONE item per call; loop here. For prime's
        # typical batch (a few coordination posts), this is sub-ms per row.
        # If batch sizes ever grow large, swap to locked_write_jsonl(read-
        # modify-write append) to acquire the lock once.
        locked_append_jsonl(sidecar, {
            "msg_id": mid,
            "reader_agent": reader,
            "reader_sid": reader_sid,
            "read_at": read_at,
        })
        seen.add(mid)
        written += 1
    print(f"Marked {written} new message(s) read in {args.channel} by {reader} "
          f"({len(msg_ids) - written} already read)", file=sys.stderr)

def _load_read_msg_ids(channel, reader_agent):
    """Return the set of msg_ids in <channel>-reads.jsonl already read by reader_agent.

    Fail-open: returns empty set on any error so --unread-only never blocks read.
    """
    sidecar = reads_sidecar_path(channel)
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
                if row.get("reader_agent") == reader_agent:
                    mid = row.get("msg_id")
                    if mid:
                        seen.add(mid)
    except OSError:
        return set()
    return seen

def cmd_channels(args):
    """List available channels with message counts."""
    require_board()

    if not BOARD_DIR.exists():
        print("No board directory yet.")
        return

    channels = sorted(BOARD_DIR.glob("*.jsonl"))
    if not channels:
        print("No channels yet.")
        return

    print("Channels:")
    for ch in channels:
        name = ch.stem
        count = 0
        if ch.exists():
            with open(ch, "r", encoding="utf-8") as f:
                count = sum(1 for line in f if line.strip())
        # Get last message timestamp
        last_ts = ""
        if count > 0:
            with open(ch, "r", encoding="utf-8") as f:
                last_line = ""
                for line in f:
                    if line.strip():
                        last_line = line
                if last_line:
                    try:
                        last_msg = json.loads(last_line)
                        last_ts = f" (last: {last_msg['timestamp']})"
                    except (json.JSONDecodeError, KeyError):
                        pass
        print(f"  {name}: {count} messages{last_ts}")

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Message board for inter-agent communication")
    sub = parser.add_subparsers(dest="command", required=True)

    # post
    post_p = sub.add_parser("post", help="Post a message (text from stdin)")
    post_p.add_argument("--channel", required=True, help="Channel name")
    post_p.add_argument("--author", help="Author name (defaults to MIND_AGENT)")
    post_p.add_argument("--reply-to", help="Message ID to reply to")
    post_p.add_argument("--tags", help="Comma-separated tags")
    post_p.add_argument("--type", help="Message type (claim, complete, blocked, encoding, finding, status)",
                        default="status")

    # mark-read (standalone — pre-existing msg IDs)
    mr_p = sub.add_parser("mark-read", help="Mark message IDs as read (g-304-03)")
    mr_p.add_argument("--channel", required=True, help="Channel name")
    mr_p.add_argument("--ids", help="Comma-separated msg IDs (or pipe one-per-line via stdin)")
    mr_p.add_argument("--reader", help="Reader agent (defaults to MIND_AGENT)")

    # channels
    sub.add_parser("channels", help="List channels with message counts")

    return parser

def main():
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "post": cmd_post,
        "mark-read": cmd_mark_read,
        "channels": cmd_channels,
    }
    dispatch[args.command](args)

if __name__ == "__main__":
    main()
