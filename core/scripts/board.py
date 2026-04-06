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
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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


def generate_message_id(channel, author):
    """Generate a unique message ID."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Simple counter: count existing messages in channel to avoid collisions
    ch_path = channel_path(channel)
    count = 0
    if ch_path.exists():
        with open(ch_path, "r", encoding="utf-8") as f:
            count = sum(1 for line in f if line.strip())
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

    author = args.author or os.environ.get("AYOAI_AGENT", "system")
    channel = args.channel

    # Structured message type (optional, defaults to "status" for backward compat)
    msg_type = getattr(args, "type", None) or "status"

    msg = {
        "id": generate_message_id(channel, author),
        "author": author,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "channel": channel,
        "type": msg_type,
        "text": text,
        "reply_to": args.reply_to,
        "tags": [t.strip() for t in args.tags.split(",")] if args.tags else [],
    }

    ch_path = channel_path(channel)
    from _fileops import locked_append_jsonl
    locked_append_jsonl(ch_path, msg)
    print(msg["id"])


def cmd_read(args):
    """Read messages from a channel."""
    require_board()

    ch_path = channel_path(args.channel)
    if not ch_path.exists():
        print(f"Channel '{args.channel}' is empty or does not exist.")
        return

    messages = []
    with open(ch_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                messages.append(json.loads(stripped))

    # Filter by --since
    if args.since:
        delta = parse_duration(args.since)
        if delta:
            cutoff = datetime.now() - delta
            messages = [m for m in messages
                        if datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S") >= cutoff]

    # Filter by --author
    if args.author:
        messages = [m for m in messages if m["author"] == args.author]

    # Filter by --type (structured message type)
    msg_type_filter = getattr(args, "type", None)
    if msg_type_filter:
        messages = [m for m in messages if m.get("type") == msg_type_filter]

    # Filter by --tag
    if args.tag:
        messages = [m for m in messages if args.tag in m.get("tags", [])]

    # Limit by --last
    if args.last:
        messages = messages[-args.last:]

    # Output
    if args.json_output:
        for msg in messages:
            print(json.dumps(msg, ensure_ascii=False))
    else:
        for msg in messages:
            tags = f" [{', '.join(msg.get('tags', []))}]" if msg.get("tags") else ""
            reply = f" (reply to {msg['reply_to']})" if msg.get("reply_to") else ""
            mtype = msg.get("type", "status")
            type_label = f" ({mtype})" if mtype and mtype != "status" else ""
            print(f"[{msg['timestamp']}] {msg['author']}{type_label}: {msg['text']}{tags}{reply}")
            print(f"  id: {msg['id']}")
            print()


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
    post_p.add_argument("--author", help="Author name (defaults to AYOAI_AGENT)")
    post_p.add_argument("--reply-to", help="Message ID to reply to")
    post_p.add_argument("--tags", help="Comma-separated tags")
    post_p.add_argument("--type", help="Message type (claim, complete, blocked, encoding, finding, status)",
                        default="status")

    # read
    read_p = sub.add_parser("read", help="Read messages from a channel")
    read_p.add_argument("--channel", required=True, help="Channel name")
    read_p.add_argument("--since", help="Duration filter (e.g., 1h, 30m, 2d)")
    read_p.add_argument("--author", help="Filter by author")
    read_p.add_argument("--tag", help="Filter by tag")
    read_p.add_argument("--type", help="Filter by message type (claim, complete, blocked, encoding, finding, status)")
    read_p.add_argument("--last", type=int, help="Show only last N messages")
    read_p.add_argument("--json", dest="json_output", action="store_true",
                        help="Output as JSONL")

    # channels
    sub.add_parser("channels", help="List channels with message counts")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "post": cmd_post,
        "read": cmd_read,
        "channels": cmd_channels,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
