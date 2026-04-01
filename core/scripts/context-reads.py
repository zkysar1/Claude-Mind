#!/usr/bin/env python3
"""Context read deduplication engine for <agent>/session/context-reads.txt.

Tracks which files have been loaded into the LLM's context window since the
last autocompact. Prevents redundant Read tool calls from re-loading identical
file content into context.

Subcommands:
  gate <file_path>           Exit 0 (allow) or exit 2 + message (block re-read)
  record <file_path>         Append path to tracker if in scope
  invalidate <file_path>     Remove path from tracker (file was modified)
  check <name1> [name2] ...  Batch check: print convention paths NOT yet tracked
  check-file <path1> [...]   Check if arbitrary file paths are tracked (print untracked)
  clear                      Delete tracker file
  status                     Print tracker contents

Session scoping:
  The --session-id flag (passed by hook wrappers) scopes the tracker to the
  current Claude Code session. A new session auto-clears stale tracker data
  from a previous session. This prevents gating files that are NOT in the
  current context window.
"""

import argparse
import os
import sys
import threading
from pathlib import Path

# Self-destruct after 10s — prevents zombie processes when the parent hook wrapper
# is killed by timeout but Python child survives (Windows doesn't propagate SIGTERM
# from bash to Python subprocesses).
# MUST be daemon=True so normal exit isn't blocked waiting for the timer.
_timer = threading.Timer(10, lambda: os._exit(0))
_timer.daemon = True
_timer.start()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import PROJECT_ROOT, WORLD_DIR, AGENT_DIR, CONFIG_DIR

SESSION_DIR = AGENT_DIR / "session" if AGENT_DIR else None
TRACKER_PATH = SESSION_DIR / "context-reads.txt" if SESSION_DIR else None
CONVENTIONS_DIR = CONFIG_DIR / "conventions"

SESSION_HEADER_PREFIX = "#session:"

# Scope filter: only these path prefixes are tracked
TRACKED_PREFIXES = [
    str(CONFIG_DIR),
    str(PROJECT_ROOT / ".claude" / "skills"),
    str(WORLD_DIR / "knowledge" / "tree"),
    str(WORLD_DIR / "conventions"),
]

# Individual files tracked outside any tracked prefix
TRACKED_FILES = [
    str(AGENT_DIR / "session" / "aspirations-compact.json")
] if AGENT_DIR else []


def normalize_path(file_path):
    """Resolve a file path to an absolute, normalized string."""
    p = Path(file_path).resolve()
    # Forward slashes are the canonical form — all tracker lookups depend on this
    return str(p).replace("\\", "/")


def is_in_scope(normalized):
    """Check if a normalized path falls within tracked prefixes or is a tracked file."""
    for tf in TRACKED_FILES:
        if normalized == tf.replace("\\", "/"):
            return True
    for prefix in TRACKED_PREFIXES:
        norm_prefix = prefix.replace("\\", "/")
        if normalized.startswith(norm_prefix):
            return True
    return False


def _read_raw_lines():
    """Read tracker file, return (session_id_or_None, [path_lines])."""
    if TRACKER_PATH is None or not TRACKER_PATH.exists():
        return None, []
    lines = TRACKER_PATH.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        return None, []
    stored_sid = None
    path_lines = lines
    if lines[0].startswith(SESSION_HEADER_PREFIX):
        stored_sid = lines[0][len(SESSION_HEADER_PREFIX):]
        path_lines = lines[1:]
    return stored_sid, path_lines


def read_tracker(session_id=None):
    """Read the tracker file, return a set of normalized paths.

    Side effect: if session_id doesn't match stored session, DELETES the tracker
    file and returns empty. This self-healing behavior is the ONLY mechanism that
    clears stale trackers across sessions — do not remove it.
    """
    stored_sid, path_lines = _read_raw_lines()

    if session_id and stored_sid and session_id != stored_sid:
        if TRACKER_PATH.exists():
            TRACKER_PATH.unlink()
        return set()

    return set(line.strip() for line in path_lines if line.strip())


def append_tracker(normalized, session_id=None):
    """Append a single path to the tracker file."""
    if SESSION_DIR is None or not SESSION_DIR.is_dir():
        return  # No agent bound, or dir gone

    if not TRACKER_PATH.exists() or TRACKER_PATH.stat().st_size == 0:
        # New tracker — write session header + first path
        header = f"{SESSION_HEADER_PREFIX}{session_id}\n" if session_id else ""
        TRACKER_PATH.write_text(header + normalized + "\n", encoding="utf-8")
    else:
        with open(TRACKER_PATH, "a", encoding="utf-8") as f:
            f.write(normalized + "\n")


def remove_from_tracker(normalized):
    """Remove a path from the tracker file if present."""
    if TRACKER_PATH is None or not TRACKER_PATH.exists():
        return
    lines = TRACKER_PATH.read_text(encoding="utf-8").strip().splitlines()
    # Preserve session header, filter path lines
    header_lines = [l for l in lines if l.startswith(SESSION_HEADER_PREFIX)]
    path_lines = [l for l in lines if not l.startswith(SESSION_HEADER_PREFIX)]
    remaining = [line for line in path_lines if line.strip() != normalized]
    if len(remaining) == len(path_lines):
        return  # Not found, nothing to do
    all_lines = header_lines + remaining
    content = ("\n".join(all_lines) + "\n") if all_lines else ""
    TRACKER_PATH.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Subcommand: gate
# ---------------------------------------------------------------------------

def cmd_gate(args):
    """PreToolUse gate: exit 0 to allow, exit 2 to block with message."""
    normalized = normalize_path(args.file_path)

    # read_tracker MUST run before is_in_scope — it clears stale cross-session trackers
    tracked = read_tracker(session_id=args.session_id)

    if not is_in_scope(normalized):
        sys.exit(0)  # Not tracked, always allow

    if normalized not in tracked:
        sys.exit(0)  # First read, allow

    # Already tracked — block with advisory message
    try:
        rel = os.path.relpath(normalized, str(PROJECT_ROOT).replace("\\", "/"))
    except ValueError:
        rel = normalized
    print(f"Already in context: {rel} — skip re-reading.")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Subcommand: record
# ---------------------------------------------------------------------------

def cmd_record(args):
    """PostToolUse recorder: append path to tracker if in scope."""
    normalized = normalize_path(args.file_path)

    # read_tracker MUST run before is_in_scope — it clears stale cross-session trackers
    tracked = read_tracker(session_id=args.session_id)

    if not is_in_scope(normalized):
        return  # Not tracked

    if normalized in tracked:
        return  # Already recorded

    append_tracker(normalized, session_id=args.session_id)


# ---------------------------------------------------------------------------
# Subcommand: invalidate
# ---------------------------------------------------------------------------

def cmd_invalidate(args):
    """PostToolUse invalidator: remove path from tracker if present."""
    normalized = normalize_path(args.file_path)

    # Allow invalidation of individually tracked files (e.g., aspirations-compact.json)
    for tf in TRACKED_FILES:
        if normalized == tf.replace("\\", "/"):
            remove_from_tracker(normalized)
            return

    # Only invalidate tree nodes — they change during goal execution.
    # world/conventions/** are tracked but procedurally stable (no mid-session edits).
    tree_prefix = str(WORLD_DIR / "knowledge" / "tree").replace("\\", "/")
    if not normalized.startswith(tree_prefix):
        return

    remove_from_tracker(normalized)


# ---------------------------------------------------------------------------
# Subcommand: check (batch convention check)
# ---------------------------------------------------------------------------

def cmd_check(args):
    """Batch check: print convention file paths NOT yet in tracker."""
    tracked = read_tracker(session_id=args.session_id)

    for name in args.names:
        # Framework conventions take priority — continue skips domain fallback.
        # Without the continue, both framework AND domain paths would print for the same name.
        conv_path = normalize_path(CONVENTIONS_DIR / f"{name}.md")
        if conv_path not in tracked and os.path.exists(str(CONVENTIONS_DIR / f"{name}.md")):
            print(conv_path)
            continue
        # Fallback: domain conventions in world/conventions/ (only if no framework match)
        domain_dir = WORLD_DIR / "conventions"
        domain_path = normalize_path(domain_dir / f"{name}.md")
        if domain_path not in tracked and os.path.exists(str(domain_dir / f"{name}.md")):
            print(domain_path)


# ---------------------------------------------------------------------------
# Subcommand: clear
# ---------------------------------------------------------------------------

def cmd_clear(args):
    """Delete the tracker file."""
    if TRACKER_PATH.exists():
        TRACKER_PATH.unlink()


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args):
    """Print tracker contents for debugging."""
    stored_sid, path_lines = _read_raw_lines()
    tracked = set(line.strip() for line in path_lines if line.strip())
    if not tracked:
        print("Context reads tracker: empty (no files tracked)")
        return
    if stored_sid:
        print(f"Session: {stored_sid}")
    print(f"Context reads tracker: {len(tracked)} file(s)")
    for path in sorted(tracked):
        try:
            rel = os.path.relpath(path, str(PROJECT_ROOT).replace("\\", "/"))
        except ValueError:
            rel = path
        print(f"  {rel}")


# ---------------------------------------------------------------------------
# Subcommand: check-file (arbitrary file path check)
# ---------------------------------------------------------------------------

def cmd_check_file(args):
    """Check if file paths are tracked. Print untracked ones (in scope only)."""
    tracked = read_tracker(session_id=args.session_id)

    for fp in args.file_paths:
        normalized = normalize_path(fp)
        if is_in_scope(normalized) and normalized not in tracked:
            print(normalized)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Context read deduplication engine")
    sub = parser.add_subparsers(dest="command", required=True)

    gate_p = sub.add_parser("gate", help="PreToolUse gate: allow or block re-reads")
    gate_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    gate_p.add_argument("file_path", help="Absolute path to the file being read")

    record_p = sub.add_parser("record", help="Record a file read into the tracker")
    record_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    record_p.add_argument("file_path", help="Absolute path to the file that was read")

    inv_p = sub.add_parser("invalidate", help="Remove a file from the tracker (modified)")
    inv_p.add_argument("file_path", help="Absolute path to the file that was modified")

    check_p = sub.add_parser("check", help="Batch check: print untracked convention paths")
    check_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    check_p.add_argument("names", nargs="+", help="Convention names (e.g., aspirations pipeline)")

    cf_p = sub.add_parser("check-file", help="Check if file paths are tracked (print untracked)")
    cf_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    cf_p.add_argument("file_paths", nargs="+", help="Absolute file paths to check")

    sub.add_parser("clear", help="Delete the tracker file")
    sub.add_parser("status", help="Print tracker contents")

    return parser


DISPATCH = {
    "gate": cmd_gate,
    "record": cmd_record,
    "invalidate": cmd_invalidate,
    "check": cmd_check,
    "check-file": cmd_check_file,
    "clear": cmd_clear,
    "status": cmd_status,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    fn = DISPATCH.get(args.command)
    if fn is None:
        parser.error(f"Unknown command: {args.command}")
    fn(args)


if __name__ == "__main__":
    # Fail-open: if anything goes wrong, allow the read (exit 0)
    try:
        main()
    except SystemExit:
        raise  # sys.exit(2) in gate MUST propagate — removing this breaks all dedup
    except Exception:
        sys.exit(0)
