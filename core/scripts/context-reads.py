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
import datetime
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

# Marker for a PROVENANCE entry — a thing RETRIEVED this session that is not a
# local file read: a WebFetch/WebSearch URL, a tree-node key, a board message.
# Entry body is "<kind>|<iso-ts>|<value>" (split on the first two pipes only, so
# a value may itself contain them).
#
# These share the tracker file to inherit its session-scoping, its self-healing
# session-mismatch delete, and its per-Body routing — one file, one lifecycle.
# But they are NOT reads of a tracked path, so `_read_tracker_split` drops them
# on the floor before its full/partial fork. That exclusion is load-bearing for
# the same reason PARTIAL_PREFIX's is: `full` feeds read_tracker(), and
# read_tracker() feeds cmd_gate — the BLOCKING dedup gate. A marker line landing
# in `full` would put a URL into the set of "files already in context" and print
# it from cmd_check_file as though it were an unread path. The default branch
# there is `else: full.add(line)`, i.e. it admits ANYTHING unrecognised, so a new
# prefix is only excluded by being excluded EXPLICITLY.
PROVENANCE_PREFIX = "#prov:"

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
        if line.startswith(PROVENANCE_PREFIX):
            continue  # provenance markers are not path reads — see PROVENANCE_PREFIX
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

    _append_line(tp, entry, session_id)


def _append_line(tp, entry, session_id):
    """Append one already-composed line, creating the header on a new tracker.

    APPEND-CHEAP BY CONTRACT (guard-875): the steady-state path is a single
    open(..., "a") + write — never a read of the whole file. This runs from
    PostToolUse hooks on every Read and every fetch, and the tracker is
    append-only, so an unbounded read here would be O(filesize) per tool call.
    Callers that need to SEARCH the tracker (provenance-check) pay that read
    once, on an explicit query, off the hook path.
    """
    if not tp.exists() or tp.stat().st_size == 0:
        # New tracker — write session header + first entry
        header = f"{SESSION_HEADER_PREFIX}{session_id}\n" if session_id else ""
        tp.write_text(header + entry + "\n", encoding="utf-8")
    else:
        with open(tp, "a", encoding="utf-8") as f:
            f.write(entry + "\n")


def append_provenance(kind, value, session_id=None, when=None):
    """Record one non-file retrieval (url / search / node / board) this session."""
    if SESSION_DIR is None:
        return
    tp = tracker_path(session_id)
    if tp is None or not tp.parent.is_dir():
        return
    ts = when or datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    # Newlines would forge extra entries; pipes are the field separator for the
    # first two fields only, so they are safe inside the value itself.
    value = str(value).replace("\r", " ").replace("\n", " ").strip()
    kind = str(kind).replace("|", "-").strip()
    if not value:
        return
    _append_line(tp, f"{PROVENANCE_PREFIX}{kind}|{ts}|{value}", session_id)


def read_provenance(session_id=None):
    """All provenance entries this session as [(kind, timestamp, value)].

    Shares _read_raw_lines with the path lanes, but deliberately does NOT go
    through _read_tracker_split — that function's job is to drop these.
    """
    stored_sid, path_lines = _read_raw_lines(session_id)
    if session_id and stored_sid and session_id != stored_sid:
        return []          # stale tracker; the split-reader owns deleting it
    out = []
    for line in path_lines:
        line = line.strip()
        if not line.startswith(PROVENANCE_PREFIX):
            continue
        body = line[len(PROVENANCE_PREFIX):]
        parts = body.split("|", 2)
        if len(parts) == 3:
            out.append((parts[0], parts[1], parts[2]))
    return out


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
    """PostToolUse invalidator: remove path from tracker if present.

    session_id routes through tracker_path() exactly as `record` and `gate` do
    (g-115-3764). Without it invalidation always targeted the AGENT-WIDE
    session/context-reads.txt while reads were recorded per-Body, so in a forked
    Body editing a tracked tree node did NOT clear it: the stale entry survived
    the edit, `gate` kept BLOCKING re-reads of a file that HAD CHANGED, and
    `check-file` stayed silent for it. That is the false-all-clear direction —
    invalidation exists precisely so a mid-session edit re-arms the advisory.

    Latent until a 2nd Body forks (g-306-64: with one Body the routing collapses
    to the agent-wide file), which is why it sat undetected — and why the
    single-Body path is pinned alongside the Body path in the tests.

    getattr rather than args.session_id: callers that build a Namespace directly
    (tests, and any in-process caller) predate this flag, and an AttributeError
    inside a PostToolUse hook is swallowed — it would silently disable
    invalidation entirely, which is a worse failure than the one being fixed.
    """
    normalized = normalize_path(args.file_path)
    session_id = getattr(args, "session_id", None)

    # Allow invalidation of individually tracked files (e.g., aspirations-compact.json)
    for tf in TRACKED_FILES:
        if normalized == tf.replace("\\", "/"):
            remove_from_tracker(normalized, session_id)
            return

    # Only invalidate tree nodes — they change during goal execution.
    # world/conventions/** are tracked but procedurally stable (no mid-session edits).
    tree_prefix = str(WORLD_DIR / "knowledge" / "tree").replace("\\", "/")
    if not normalized.startswith(tree_prefix):
        return

    remove_from_tracker(normalized, session_id)


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
# Subcommand: record-prov / provenance-check  (session provenance manifest)
# ---------------------------------------------------------------------------

# `retrieval` vs `retrieval-auto` is the load-bearing distinction in this tuple,
# not a taxonomy nicety (). Both are written by retrieve.sh, the
# framework's unified retrieval entry point. But retrieve.sh has TWO callers with
# opposite meanings:
#   retrieval       — an agent DELIBERATELY consulted the stores before acting.
#   retrieval-auto  — user-prompt-retrieval-inject.sh ran its automatic pre-pass
#                     on a user message. The model did not ask for this and may
#                     never read the injected result.
# The retrieval-floor query below counts only the DELIBERATE kinds. Collapsing
# the two would make the floor unreachable rather than merely noisy: that hook
# fires on essentially every substantive non-loop user message, so a floor that
# accepted `retrieval-auto` as evidence would report "the agent consulted its
# knowledge" for every session in which a human typed a sentence — passing
# forever while measuring nothing, which is the failure mode a floor exists to
# prevent (guard-1760: a checker must not report what it declined to look at as
# a pass).
PROVENANCE_KINDS = ("url", "search", "node", "board", "retrieval", "retrieval-auto")

# Kinds that count as the agent having actually consulted something. Everything
# in PROVENANCE_KINDS except the automatic pre-pass.
DELIBERATE_RETRIEVAL_KINDS = ("url", "search", "node", "board", "retrieval")


def count_deliberate_retrievals(session_id=None):
    """How many DELIBERATE store/source consultations happened this session.

    Excludes `retrieval-auto` by construction — see DELIBERATE_RETRIEVAL_KINDS.
    """
    return sum(1 for kind, _ts, _v in read_provenance(session_id=session_id)
               if kind in DELIBERATE_RETRIEVAL_KINDS)


def cmd_retrieval_floor(args):
    """exit 0 = this session HAS consulted something; exit 1 = zero consultations.

    THE NEGATIVE IS NARROW AND MUST BE READ AS SUCH (guard-4407). The manifest is
    fed by hooks bound to the Read/WebFetch/WebSearch TOOLS plus retrieve.sh and
    tree-read.sh. A page pulled with `curl`, a store read with `cat`, or a
    consultation made before the last manifest reset leaves no entry. So exit 1
    means "no recorded consultation", never "the agent invented this" — which is
    why every consumer of this query is ADVISORY and none may refuse a write.
    """
    n = count_deliberate_retrievals(session_id=args.session_id)
    if not args.quiet:
        print(n)
    # sys.exit, NOT `return` — main() calls fn(args) and DISCARDS the result, so
    # a returned code is silently dropped and this query would answer "consulted"
    # for every session including the empty ones. The exit code IS the answer
    # here, exactly as in cmd_provenance_check below.
    sys.exit(0 if n > 0 else 1)


# ---------------------------------------------------------------------------
# Subcommand: retrieval-pulse  ( layer 2 — the zero-retrieval pulse)
# ---------------------------------------------------------------------------
#
# Layer 1 (retrieval-floor-gate.sh) asks "did this session consult ANYTHING
# before writing to a knowledge store?" — one question, at one moment, on one
# store class. That leaves the mid-task interior uncovered: a session can run
# for dozens of substantive tool calls, drift a long way from what it last
# checked, and never touch a knowledge store at all — so the floor never fires
# and nothing else is watching.
#
# The pulse is the interior counter. It counts consecutive substantive tool
# calls with NO new deliberate consultation and, at the threshold, emits one
# advisory naming the count and the nearest retrieve-before-deciding decision
# points, then resets.
#
# IT MUST REUSE count_deliberate_retrievals AND NEVER RE-COUNT THE MANIFEST.
# The layer-1 author recorded this as the design constraint the spec did not
# anticipate: `retrieval-auto` (the UserPromptSubmit auto pre-pass) fires on
# essentially every substantive user message, so a pulse that reset on injected
# retrievals would reset constantly and measure nothing — the same
# unreachable-gate failure the floor's DELIBERATE_RETRIEVAL_KINDS split exists
# to prevent (guard-1760).

DEFAULT_PULSE_THRESHOLD = 15


def pulse_state_path(session_id=None):
    """Counter state for the zero-retrieval pulse, beside this session's tracker.

    Routed through tracker_path() rather than resolving a second time of its
    own: two concurrent Bodies must not share a streak counter, for exactly the
    reason they must not share a dedup tracker (Phase 1D per-Body routing).
    """
    t = tracker_path(session_id=session_id)
    if t is None:
        return None
    return t.with_name(t.stem + "-pulse.txt")


def _read_pulse_state(path):
    """(last_n, streak) from the one-line state file; (0, 0) on any problem.

    Plain text, not JSON, matching the sibling tracker's format — and so that a
    truncated or hand-mangled file degrades to a reset rather than an exception
    on a hook path that must never raise.
    """
    try:
        parts = path.read_text(encoding="utf-8").split()
        return int(parts[0]), int(parts[1])
    except Exception:
        return 0, 0


def _write_pulse_state(path, last_n, streak):
    """Best-effort atomic write. Never raises — this runs inside a PostToolUse hook."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("%d %d\n" % (last_n, streak), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


PULSE_ADVISORY = (
    "[retrieval-pulse] ADVISORY: {n} substantive tool calls this session with no new "
    "recorded store consultation. If you are about to edit an existing file (point 10), "
    "file a goal that prescribes a fix (11), run a probe whose empty result authorizes an "
    "action (12), report a count or census (13), or declare something absent/impossible (8) "
    "— retrieve first: bash core/scripts/retrieve.sh --category \"<topic>\" --depth shallow. "
    "Consultations made with cat/curl/grep are invisible to the manifest (guard-4407), so "
    "this is a prompt to check, never a claim that you have retrieved nothing."
)


def cmd_retrieval_pulse(args):
    """exit 0 = pulse FIRED (advisory printed); exit 1 = silent.

    The exit code IS the answer, as in cmd_retrieval_floor — main() calls fn(args)
    and discards the return value, so a returned code would be dropped and every
    tick would read as "fired".
    """
    threshold = args.threshold
    if threshold is None:
        try:
            threshold = int(os.environ.get("RETRIEVAL_PULSE_THRESHOLD", "")
                            or DEFAULT_PULSE_THRESHOLD)
        except ValueError:
            threshold = DEFAULT_PULSE_THRESHOLD
    if threshold <= 0:          # 0 / negative disables the pulse entirely
        sys.exit(1)

    path = pulse_state_path(session_id=args.session_id)
    if path is None:
        sys.exit(1)

    n = count_deliberate_retrievals(session_id=args.session_id)
    last_n, streak = _read_pulse_state(path)

    # ANY movement in the deliberate count resets the streak, in EITHER
    # direction. An increase means the agent consulted something. A DECREASE
    # means the manifest was cleared (new context window after autocompact), so
    # the streak that preceded it describes a session that no longer exists —
    # continuing to count it would fire an advisory about someone else's work.
    if n != last_n:
        _write_pulse_state(path, n, 0)
        sys.exit(1)

    streak += 1
    if streak >= threshold:
        _write_pulse_state(path, n, 0)
        if not args.quiet:
            print(PULSE_ADVISORY.format(n=streak))
        sys.exit(0)

    _write_pulse_state(path, n, streak)
    sys.exit(1)


def cmd_record_prov(args):
    """PostToolUse: record one non-file retrieval into the session manifest."""
    append_provenance(args.kind, args.value, session_id=args.session_id)


def cmd_provenance_check(args):
    """Answer 'was this retrieved this session?' — exit 0 yes, 1 no.

    Accepts a URL, a file path, or a tree-node key. File paths are answered from
    the SAME full/partial sets the read tracker already maintains, so a Read and
    a WebFetch are queryable through one interface; anything else is answered
    from the provenance lane.
    """
    query = args.query.strip()
    hits = []

    for kind, ts, value in read_provenance(session_id=args.session_id):
        if value == query:
            hits.append((kind, ts, value))

    # File-path lane: a tracked path is provenance too, recorded by the Read hook.
    full, partial = _read_tracker_split(session_id=args.session_id)
    normalized = normalize_path(query)
    if normalized in full:
        hits.append(("read", "", normalized))
    elif normalized in partial:
        hits.append(("read-partial", "", normalized))

    if not hits:
        if not args.quiet:
            print(f"NOT RETRIEVED this session: {query}")
        sys.exit(1)

    if not args.quiet:
        for kind, ts, value in hits:
            when = ts or "(this session)"
            print(f"RETRIEVED\t{kind}\t{when}\t{value}")
    sys.exit(0)


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
    inv_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")

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

    rp_p = sub.add_parser("record-prov", help="Record a non-file retrieval (URL, node, board msg)")
    rp_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    rp_p.add_argument("--kind", default="url", choices=list(PROVENANCE_KINDS),
                      help="What kind of thing was retrieved (default: url)")
    rp_p.add_argument("value", help="The URL, node key, or message id that was retrieved")

    rf_p = sub.add_parser("retrieval-floor",
                          help="Count DELIBERATE consultations this session; exit 1 if zero")
    rf_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    rf_p.add_argument("--quiet", action="store_true", help="Exit code only, no count")

    pu_p = sub.add_parser("retrieval-pulse",
                          help="Tick the zero-retrieval counter; exit 0 (+advisory) when it fires")
    pu_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    pu_p.add_argument("--threshold", type=int, default=None,
                      help="Consecutive no-consultation tool calls before firing "
                           "(default: $RETRIEVAL_PULSE_THRESHOLD or %d; <=0 disables)"
                           % DEFAULT_PULSE_THRESHOLD)
    pu_p.add_argument("--quiet", action="store_true", help="Exit code only, no advisory text")

    pc_p = sub.add_parser("provenance-check",
                          help="Was this URL/path/node retrieved this session? exit 0 yes, 1 no")
    pc_p.add_argument("--session-id", default=None, help="Current session ID (from hook JSON)")
    pc_p.add_argument("--quiet", action="store_true", help="Exit code only, no output")
    pc_p.add_argument("query", help="URL, file path, or tree-node key")

    return parser


DISPATCH = {
    "gate": cmd_gate,
    "record": cmd_record,
    "invalidate": cmd_invalidate,
    "check": cmd_check,
    "check-file": cmd_check_file,
    "clear": cmd_clear,
    "status": cmd_status,
    "record-prov": cmd_record_prov,
    "retrieval-floor": cmd_retrieval_floor,
    "retrieval-pulse": cmd_retrieval_pulse,
    "provenance-check": cmd_provenance_check,
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
