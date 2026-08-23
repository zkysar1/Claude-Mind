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

# Marker for a RANGED read (offset/limit/pages). Such a read is real evidence the
# file was opened, but only PARTIAL evidence of context — read-before-edit.md
# Rule 1 counts it "only if it covers the region being edited", which no gate can
# evaluate (Edit carries old_string, never a line range). So the two consumers
# deliberately diverge ():
#
#   read_tracker()        FULL only  -> cmd_gate, the BLOCKING dedup gate,
#                                       and cmd_check (a partly-read convention
#                                       is not a loaded convention)
#   _read_tracker_split() both sets  -> cmd_check_file, the non-blocking advisory,
#                                       which must tell the two states apart
#
# Keeping partials out of read_tracker is the load-bearing half. Were a ranged
# peek allowed to satisfy the dedup gate, the follow-up WHOLE-file read would be
# refused as "Already in context" — losing the very context the peek lacked, and
# colliding with verify-before-assuming.md's re-verify mandate. Recording ranged
# reads at all is the other half: without it the advisory claimed "has not been
# Read" for a file just read, on every large file (a large file is exactly the one
# read with offset/limit), and read-before-edit.md Rule 4 names that
# desensitization as the specific harm.
PARTIAL_PREFIX = "#partial:"

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

# Individual files tracked outside any tracked prefix.
#
# BOTH compact files must be listed, and the summary was missing for as long as
# it has existed (). `load-aspirations-compact.sh` emits the SUMMARY
# path (its line 108) and invalidates the SUMMARY path (its line 102), but only
# the full compact was named here — and the summary lives under
# agents/<agent>/session/, which matches no TRACKED_PREFIXES. So
# is_in_scope_advisory() returned False for it and cmd_check_file `continue`d,
# printing NOTHING on rc=0 **unconditionally, on every invocation, for every
# agent, on every box** — not the intended "already read, reuse it" dedup.
# Every caller follows `IF path returned: Read it`, so the IF never fired and
# the loop proceeded with no portfolio in context: precheck could not compute
# active_count, strategic-scan S1 reviewed zero recurring goals, S3/S4a computed
# over an empty list — each reporting a clean pass (the rb-245 vacuous-zero
# family, sitting on a loader most major loop phases depend on). Measured
# 2026-08-11 (zeta, hostname cc-02, uname -r 6.8.0-136-generic) with a positive
# control: check-file on aspirations-compact.json printed 60 bytes while
# check-file on aspirations-compact-summary.json printed 0, same session, same
# tracker state, only the filename differing.
#
# cmd_invalidate gates on this SAME list, which is what proves the omission was
# an oversight rather than a design choice: the wrapper already invalidates the
# summary on regeneration, and that call had been a silent no-op too.
#
# Adding an entry here widens is_in_scope (the BLOCKING re-read gate) as well as
# is_in_scope_advisory (guard-2601 — check the wider caller before extending a
# shared predicate). That is correct and not a new posture: the full compact
# beside it already carries exactly these semantics, as does load-tree-summary's
# _summary.json via TRACKED_PREFIXES. A generated cache is precisely what the
# re-read dedup is for, and regeneration clears it via the invalidate above.
TRACKED_FILES = [
    str(AGENT_DIR / "session" / "aspirations-compact.json"),
    str(AGENT_DIR / "session" / "aspirations-compact-summary.json"),
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
    # SEPARATOR NORMALIZATION MUST PRECEDE resolve() (). Claude Code
    # sends file_path in NATIVE form, so on Windows every path arrives
    # backslashed. Resolving FIRST and replacing after is too late: on a POSIX
    # Path implementation a backslashed string has no separators at all, so it
    # parses as ONE relative filename component and resolve() glues CWD onto the
    # front — yielding <cwd>/<whole-path> which matches no tracked prefix. The
    # read is then dropped silently at rc=0, and the read-before-edit advisory
    # it feeds inverts from never-firing to always-firing (a 100% false-positive
    # banner, which read-before-edit.md Rule 4 calls worse than a silent one).
    # The trailing replace is still required for WindowsPath's backslash output.
    p = Path(str(file_path).replace("\\", "/")).resolve()
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


def _read_tracker_split(session_id=None):
    """Read the tracker file, return (full_paths, partial_paths) as two sets.

    Side effect: if session_id doesn't match stored session, DELETES the tracker
    file and returns empty. This self-healing behavior is the ONLY mechanism that
    clears stale trackers across sessions — do not remove it.
    """
    stored_sid, path_lines = _read_raw_lines(session_id)

    if session_id and stored_sid and session_id != stored_sid:
        tp = tracker_path(session_id)
        if tp is not None and tp.exists():
            tp.unlink()
        return set(), set()

    full, partial = set(), set()
    for line in path_lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith(PARTIAL_PREFIX):
            partial.add(line[len(PARTIAL_PREFIX):])
        else:
            full.add(line)
    return full, partial


def read_tracker(session_id=None):
    """Set of FULLY-read paths. Ranged reads are deliberately EXCLUDED.

    Every pre-g-115-3747 caller keeps its exact original semantics, which is what
    stops cmd_gate from ever refusing a whole-file read on the strength of a
    ranged peek. Advisory consumers want _read_tracker_split() instead, which
    hands back both sets so they can tell "never opened" from "opened in part".
    """
    return _read_tracker_split(session_id)[0]


def append_tracker(normalized, session_id=None, partial=False):
    """Append a single path to the tracker file.

    partial=True writes the entry behind PARTIAL_PREFIX so it is visible to the
    advisory but invisible to the blocking dedup gate.
    """
    if SESSION_DIR is None:
        return  # No agent bound

    entry = (PARTIAL_PREFIX + normalized) if partial else normalized
    tp = tracker_path(session_id)
    # The per-Body tracker lives under sessions/<unitKey>/ (created by /start
    # FORK-BODY); the agent-wide tracker under session/. Guard the parent dir so
    # a torn-down dir is a no-op rather than a crash.
    if tp is None or not tp.parent.is_dir():
        return

    if not tp.exists() or tp.stat().st_size == 0:
        # New tracker — write session header + first path
        header = f"{SESSION_HEADER_PREFIX}{session_id}\n" if session_id else ""
        tp.write_text(header + entry + "\n", encoding="utf-8")
    else:
        with open(tp, "a", encoding="utf-8") as f:
            f.write(entry + "\n")


def remove_from_tracker(normalized, session_id=None):
    """Remove a path from the tracker file if present."""
    tp = tracker_path(session_id)
    if tp is None or not tp.exists():
        return
    lines = tp.read_text(encoding="utf-8").strip().splitlines()
    # Preserve session header, filter path lines
    header_lines = [l for l in lines if l.startswith(SESSION_HEADER_PREFIX)]
    path_lines = [l for l in lines if not l.startswith(SESSION_HEADER_PREFIX)]
    # BOTH forms must go. Clearing only the full entry would leave a partial one
    # behind, and the advisory would then stay silent for a file that has since
    # been modified — a false all-clear, which is the strictly worse direction
    # than the false alarm this whole change exists to remove.
    stale = (normalized, PARTIAL_PREFIX + normalized)
    remaining = [line for line in path_lines if line.strip() not in stale]
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
    """PostToolUse recorder: append path to tracker if in scope.

    --partial marks a ranged (offset/limit/pages) read. Before g-115-3747 the
    shell hook dropped those reads entirely rather than passing them here.
    """
    normalized = normalize_path(args.file_path)
    partial = bool(getattr(args, "partial", False))

    # split-read MUST run before is_in_scope — it clears stale cross-session trackers
    full, partial_set = _read_tracker_split(session_id=args.session_id)

    if not is_in_scope_advisory(normalized):
        return  # Not tracked (recorder uses the WIDER advisory scope — )

    if normalized in full:
        return  # Already at full fidelity; a later ranged peek adds nothing

    if partial:
        if normalized in partial_set:
            return  # Already recorded as partial — repeated peeks stay one entry
        append_tracker(normalized, session_id=args.session_id, partial=True)
        return

    # A FULL read supersedes any prior partial: drop the stale marker entry so the
    # dedup gate starts applying (a partial entry is invisible to it by design).
    if normalized in partial_set:
        remove_from_tracker(normalized, session_id=args.session_id)
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
    """Delete the tracker file THIS session uses. Session-aware since .

    `--session-id` routes through tracker_path(), so the clear lands on the
    body tracker for a forked worker Body and on the agent-wide tracker for a
    reducer — never both, and never another session's. That scoping is the
    point, not an optimisation: on a box where a reducer and a worker coexist,
    clearing the agent-wide file from a worker's hook is exactly the
    cross-session shared-state mutation guard-404 forbids.

    Bare `clear` (no --session-id) keeps the agent-wide behaviour, which is
    what an operator running the wrapper by hand means.

    WHY THIS BECAME SESSION-AWARE. The docstring here used to argue an explicit
    per-Body clear was "a Phase-2 concern (worker Bodies don't run PreCompact)".
    Both halves were wrong by 2026-08-22 and measurably so on cc-08: worker
    Bodies DO compact (their PreCompact wrote a body-keyed compact-checkpoint.yaml
    at 12:55), and the agent-wide path this cleared did not exist for ANY agent
    on the box — every live tracker was a sessions/<SID>/body-context-reads.txt.
    So the clear ran, found nothing, and left a 135-entry manifest asserting
    in-context for files the compaction had just evicted. The self-heal the
    docstring relied on is a CROSS-SESSION header mismatch; it cannot fire
    within one session, and a compaction never changes the SID.
    """
    tp = tracker_path(getattr(args, "session_id", None))
    if tp is not None and tp.exists():
        tp.unlink()
        print(f"[context-reads] cleared tracker: {tp}")
    else:
        print(f"[context-reads] no tracker to clear: {tp}")


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args):
    """Print tracker contents for debugging."""
    stored_sid, _path_lines = _read_raw_lines(None)
    full, partial_set = _read_tracker_split(None)
    if not full and not partial_set:
        print("Context reads tracker: empty (no files tracked)")
        return
    if stored_sid:
        print(f"Session: {stored_sid}")
    print(f"Context reads tracker: {len(full)} full, {len(partial_set)} partial")
    # Marker-stripped by the split above — printing raw lines here would hand
    # os.path.relpath a "#partial:/abs/path" string and render it as garbage.
    for path, suffix in ([(p, "") for p in sorted(full)]
                         + [(p, "  (partial)") for p in sorted(partial_set)]):
        try:
            rel = os.path.relpath(path, str(PROJECT_ROOT).replace("\\", "/"))
        except ValueError:
            rel = path
        print(f"  {rel}{suffix}")


# ---------------------------------------------------------------------------
# Subcommand: check-file (arbitrary file path check)
# ---------------------------------------------------------------------------

def cmd_check_file(args):
    """Check if file paths are tracked. Print untracked ones (in scope only).

    Default output is unchanged from pre-g-115-3747: a bare path per line, where a
    partially-read file still counts as "not in context". That contract is
    load-bearing for the FIVE digest-loader callers (load-loop-digest.sh,
    load-aspirations-compact.sh, load-tree-summary.sh, load-execute-protocol.sh,
    load-consolidation-housekeeping.sh) — they do a plain emptiness test on stdout
    and never parse a prefix, and for them a partly-read digest genuinely does need
    re-emitting. Only pre-edit-context-gate.sh passes --partial-aware, because only
    it needs to tell "never opened" apart from "opened, but not all of it".
    """
    full, partial_set = _read_tracker_split(session_id=args.session_id)
    partial_aware = bool(getattr(args, "partial_aware", False))

    for fp in args.file_paths:
        normalized = normalize_path(fp)
        # WIDER advisory scope (): the read-before-edit advisory covers
        # core/scripts framework-code edits, which the narrow is_in_scope omits.
        if not is_in_scope_advisory(normalized) or normalized in full:
            continue
        if partial_aware and normalized in partial_set:
            print(f"PARTIAL\t{normalized}")
        else:
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
    record_p.add_argument("--partial", action="store_true",
                          help="Ranged read (offset/limit/pages) — advisory-visible, never dedup-blocking")
    record_p.add_argument("file_path", help="Absolute path to the file that was read")

    inv_p = sub.add_parser("invalidate", help="Remove a file from the tracker (modified)")
    inv_p.add_argument("file_path", help="Absolute path to the file that was modified")

    check_p = sub.add_parser("check", help="Batch check: print untracked convention paths")
    check_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    check_p.add_argument("names", nargs="+", help="Convention names (e.g., aspirations pipeline)")

    cf_p = sub.add_parser("check-file", help="Check if file paths are tracked (print untracked)")
    cf_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    cf_p.add_argument("--partial-aware", action="store_true",
                      help="Prefix ranged-read-only paths with 'PARTIAL\\t' (edit advisory only)")
    cf_p.add_argument("file_paths", nargs="+", help="Absolute file paths to check")

    clear_p = sub.add_parser("clear", help="Delete the tracker file")
    clear_p.add_argument("--session-id", default=None,
                         help="Session ID (from hook JSON) — clears THAT session's tracker "
                              "(body tracker for a forked Body, agent-wide for a reducer). "
                              "Omit for the agent-wide tracker.")
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
