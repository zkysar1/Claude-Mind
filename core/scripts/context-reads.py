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

from _paths import (
    PROJECT_ROOT, WORLD_DIR, AGENT_DIR, CONFIG_DIR, AGENT_NAME, agent_session_dir,
)

SESSION_DIR = AGENT_DIR / "session" if AGENT_DIR else None
CONVENTIONS_DIR = CONFIG_DIR / "conventions"


def tracker_path(session_id=None):
    """Effective context-reads tracker path — reducer-aware per-Body routing (Phase 1D, ).

    Routes to the per-Body tracker (sessions/<unitKey>/body-context-reads.txt)
    when session_id (the unitKey) names a Body whose forked body-WM-FILE exists
    (sessions/<unitKey>/working-memory.yaml) -- the SAME activation signal as
    wm.py's BODY_WM routing and AgentPaths.wm_path (the 1B reducer-aware re-key,
    rb-2297). A reducer/observer (no forked body-WM-file) stays on the agent-wide
    singleton (session/context-reads.txt), so with one Body this collapses to
    today's behavior -- inert until a 2nd Body forks, and concurrent Bodies then
    no longer clobber each other's session-scoped dedup state.

    Resolved per-call (NOT a frozen module constant — the g-306-68 PEP-562
    lesson) because session_id is only known at command dispatch, AND because
    context-reads-record.sh / pre-edit-context-gate.sh are Read/Edit hooks where
    bash-agent-inject.py does NOT inject BODY_WM_PATH (it only prepends env to
    Bash-TOOL commands). The Body is therefore detected from the forked-WM-file's
    existence directly, not the env var wm.py reads.
    """
    if SESSION_DIR is None:
        return None
    if session_id and AGENT_NAME:
        body_dir = agent_session_dir(AGENT_NAME, session_id)
        if (body_dir / "working-memory.yaml").exists():
            return body_dir / "body-context-reads.txt"
    return SESSION_DIR / "context-reads.txt"

SESSION_HEADER_PREFIX = "#session:"

# Scope filter: only these path prefixes are tracked
TRACKED_PREFIXES = [
    str(CONFIG_DIR),
    str(PROJECT_ROOT / ".claude" / "skills"),
]
# WORLD_DIR is None on UNINITIALIZED first-run (no local-paths.conf yet).
# Guard so /start can run load-conventions / context-reads without crashing
# before Phase B writes the conf. Mirrors the SESSION_DIR / AGENT_DIR pattern
# above and the AGENT_DIR pattern in TRACKED_FILES below.
if WORLD_DIR:
    TRACKED_PREFIXES.extend([
        str(WORLD_DIR / "knowledge" / "tree"),
        str(WORLD_DIR / "conventions"),
    ])

# Individual files tracked outside any tracked prefix
TRACKED_FILES = [
    str(AGENT_DIR / "session" / "aspirations-compact.json")
] if AGENT_DIR else []

# Advisory-ONLY extension prefixes (). The read-before-edit ADVISORY
# (pre-edit-context-gate.sh -> check-file) plus the RECORDER should cover
# framework-CODE edits, which concentrate in core/scripts/ — the surface where
# the loop self-evolves the framework and where stale-context edits after an
# autocompact are most costly. But the BLOCKING re-read dedup gate (cmd_gate,
# exit 2) MUST NOT extend here: blocking a whole-file re-read of a script the
# agent just edited would collide with verify-before-assuming.md's mandated
# "re-verify after linter/user notification" (a required whole-file re-read
# would be refused as "already in context"). So these prefixes widen ONLY the
# recorder + advisory scope; cmd_gate keeps the narrow TRACKED_PREFIXES. Net:
# core/scripts reads ARE recorded (giving the advisory signal) but are NEVER
# dedup-blocked, and — because invalidate leaves them recorded across the agent's
# own edits — a consecutive follow-up edit does NOT mis-fire a false advisory.
# (.claude/rules/** is a deliberate future candidate — also framework-text and
# currently silent per read-before-edit.md Rule 4 — added here only if the same
# stale-context miss pattern is observed there; scoped out of .)
ADVISORY_EXTRA_PREFIXES = [
    str(PROJECT_ROOT / "core" / "scripts"),
]


def normalize_path(file_path):
    """Resolve a file path to an absolute, normalized string."""
    p = Path(file_path).resolve()
    # Forward slashes are the canonical form — all tracker lookups depend on this
    return str(p).replace("\\", "/")


def is_in_scope(normalized):
    """Check if a normalized path falls within tracked prefixes or is a tracked file.

    This is the NARROW scope — used by the BLOCKING re-read dedup gate
    (cmd_gate). Widening it would start blocking whole-file re-reads of the
    added prefix; see ADVISORY_EXTRA_PREFIXES for why core/scripts must NOT be
    dedup-blocked. Recorder + advisory use is_in_scope_advisory (wider) instead.
    """
    for tf in TRACKED_FILES:
        if normalized == tf.replace("\\", "/"):
            return True
    for prefix in TRACKED_PREFIXES:
        norm_prefix = prefix.replace("\\", "/")
        if normalized.startswith(norm_prefix):
            return True
    return False


def is_in_scope_advisory(normalized):
    """WIDER scope for the RECORDER (cmd_record) + read-before-edit ADVISORY
    (cmd_check_file) ONLY. Superset of is_in_scope: adds ADVISORY_EXTRA_PREFIXES
    (core/scripts) so the advisory fires on framework-CODE edits. The BLOCKING
    dedup gate (cmd_gate) deliberately does NOT call this — see the
    ADVISORY_EXTRA_PREFIXES comment (g-115-2210) for the re-read-block rationale.
    """
    if is_in_scope(normalized):
        return True
    for prefix in ADVISORY_EXTRA_PREFIXES:
        if normalized.startswith(prefix.replace("\\", "/")):
            return True
    return False


def _read_raw_lines(session_id=None):
    """Read tracker file, return (session_id_or_None, [path_lines])."""
    tp = tracker_path(session_id)
    if tp is None or not tp.exists():
        return None, []
    lines = tp.read_text(encoding="utf-8").strip().splitlines()
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
    stored_sid, path_lines = _read_raw_lines(session_id)

    if session_id and stored_sid and session_id != stored_sid:
        tp = tracker_path(session_id)
        if tp is not None and tp.exists():
            tp.unlink()
        return set()

    return set(line.strip() for line in path_lines if line.strip())


def append_tracker(normalized, session_id=None):
    """Append a single path to the tracker file."""
    if SESSION_DIR is None:
        return  # No agent bound

    tp = tracker_path(session_id)
    # The per-Body tracker lives under sessions/<unitKey>/ (created by /start
    # FORK-BODY); the agent-wide tracker under session/. Guard the parent dir so
    # a torn-down dir is a no-op rather than a crash.
    if tp is None or not tp.parent.is_dir():
        return

    if not tp.exists() or tp.stat().st_size == 0:
        # New tracker — write session header + first path
        header = f"{SESSION_HEADER_PREFIX}{session_id}\n" if session_id else ""
        tp.write_text(header + normalized + "\n", encoding="utf-8")
    else:
        with open(tp, "a", encoding="utf-8") as f:
            f.write(normalized + "\n")


def remove_from_tracker(normalized, session_id=None):
    """Remove a path from the tracker file if present."""
    tp = tracker_path(session_id)
    if tp is None or not tp.exists():
        return
    lines = tp.read_text(encoding="utf-8").strip().splitlines()
    # Preserve session header, filter path lines
    header_lines = [l for l in lines if l.startswith(SESSION_HEADER_PREFIX)]
    path_lines = [l for l in lines if not l.startswith(SESSION_HEADER_PREFIX)]
    remaining = [line for line in path_lines if line.strip() != normalized]
    if len(remaining) == len(path_lines):
        return  # Not found, nothing to do
    all_lines = header_lines + remaining
    content = ("\n".join(all_lines) + "\n") if all_lines else ""
    tp.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Subcommand: gate
# ---------------------------------------------------------------------------

def cmd_gate(args):
    """PreToolUse gate: exit 0 to allow, exit 2 to block with message."""
    normalized = normalize_path(args.file_path)

    # read_tracker MUST run before is_in_scope — it clears stale cross-session trackers
    tracked = read_tracker(session_id=args.session_id)

    # NARROW is_in_scope is load-bearing here — do NOT switch this to
    # is_in_scope_advisory. The block gate must never refuse a core/scripts
    # whole-file re-read (verify-before-assuming.md re-verify mandate); that
    # prefix is advisory-only ().
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

    if not is_in_scope_advisory(normalized):
        return  # Not tracked (recorder uses the WIDER advisory scope — )

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
    # clear carries no --session-id (PreCompact / reducer-side utility) -> the
    # agent-wide tracker. A forked Body's tracker self-heals across sessions via
    # the session-header mismatch in read_tracker; an explicit per-Body clear is
    # a Phase-2 concern (worker Bodies don't run PreCompact).
    tp = tracker_path(None)
    if tp is not None and tp.exists():
        tp.unlink()


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args):
    """Print tracker contents for debugging."""
    stored_sid, path_lines = _read_raw_lines(None)
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
        # WIDER advisory scope (): the read-before-edit advisory covers
        # core/scripts framework-code edits, which the narrow is_in_scope omits.
        if is_in_scope_advisory(normalized) and normalized not in tracked:
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
