"""Shared file operations: locking, history snapshots, changelog, and locked writes.

Imported by all write scripts to provide:
  - File locking (acquire_lock / release_lock)
  - Copy-on-write history (.history/ snapshots before overwriting)
  - Changelog auto-append (base_dir/changelog.jsonl)
  - Locked write functions (locked_write_jsonl, locked_append_jsonl, etc.)
  - Atomic read-modify-write primitives (locked_modify_yaml,
    locked_modify_jsonl, locked_append_jsonl_with_allocator) that hold
    the lock across the full read-modify-write cycle. Use these for any
    write where the new state depends on the existing state (counter
    increments, id allocation, dup checks, field updates).
"""

import json
import os
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from _paths import WORLD_DIR, META_DIR, AGENT_DIR, PROJECT_ROOT
from _path_helpers import looks_like_cruft


# ---------------------------------------------------------------------------
# File Locking
# ---------------------------------------------------------------------------

def acquire_lock(lock_path, timeout=10, stale_seconds=30):
    """Acquire a file lock using atomic create. Breaks stale locks > stale_seconds (default 30s).

    stale_seconds tunes the staleness threshold to the caller's RMW cycle time.
    Sub-100ms cycles (working memory) should pass stale_seconds=10; subprocess-
    inside-lock paths (skill-quality scoring) may legitimately need 60s. The
    30s default preserves prior behavior for any caller that omits it.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    while True:
        try:
            # O_CREAT | O_EXCL is atomic: create-if-absent in one syscall.
            # Do NOT replace with exists() + write_text() — that has a TOCTOU race.
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            return  # Lock acquired
        except (FileExistsError, PermissionError):
            # On POSIX, an existing lock surfaces as FileExistsError. On Windows
            # the same situation surfaces as PermissionError when another
            # process has the file open with default sharing — ERROR_SHARING_
            # VIOLATION (winerror 32) maps to EACCES, not EEXIST. We must
            # treat both as "lock held, retry" or contention loses on Windows
            # the moment the holder is mid-write. Surfaced by
            # tests/concurrency_stress.py (PR 6).
            try:
                if time.time() - lock_path.stat().st_mtime > stale_seconds:
                    lock_path.unlink(missing_ok=True)
                    continue  # Retry immediately after breaking stale lock
            except FileNotFoundError:
                continue  # Lock was released between open() and stat()
            if time.time() - start > timeout:
                raise TimeoutError(f"Could not acquire lock: {lock_path}")
            time.sleep(0.1)


def release_lock(lock_path):
    """Release a file lock."""
    Path(lock_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Cruft tripwire ()
# ---------------------------------------------------------------------------

def _cruft_tripwire(base_dir, caller, operation):
    """Return True (and emit a stack to stderr) when base_dir looks like a
    cruft mirror — drive-letter content or U+F03A in a non-root position.

    Defense-in-depth: the primary defense is `_path_helpers.absolutize` at
    the resolver. This catches new bypass call sites that didn't route
    through it. Callers MUST `return` immediately on True to skip the
    operation rather than create cruft.
    """
    if not looks_like_cruft(base_dir):
        return False
    import traceback
    print(
        f"[_fileops.{caller}] CRUFT TRIPWIRE: base_dir looks like cruft "
        f"mirror ({str(base_dir)!r}). Skipping {operation}. Caller "
        f"bypassed _absolutize — fix the call site.\nStack:",
        file=sys.stderr,
    )
    traceback.print_stack(file=sys.stderr)
    return True


# ---------------------------------------------------------------------------
# History Snapshots
# ---------------------------------------------------------------------------

def save_history(path, base_dir, agent_name, summary=""):
    """Save a copy of the current file to .history/ before overwriting.

    Args:
        path: The file being overwritten.
        base_dir: The root directory (e.g., WORLD_DIR or META_DIR).
            History is stored at base_dir/.history/<relative-path>/.
        agent_name: Who is making the change (for the filename).
        summary: Optional one-line description (stored in sidecar).
    """
    # Both must be resolved — callers may pass unresolved paths while
    # resolve_base_dir returns resolved paths. Mismatch breaks relative_to.
    path = Path(path).resolve()
    base_dir = Path(base_dir).resolve()
    if not path.exists():
        return  # Nothing to version — new file

    if _cruft_tripwire(base_dir, "save_history", "snapshot"):
        return

    rel = path.relative_to(base_dir)
    history_dir = base_dir / ".history" / str(rel)
    history_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    ext = path.suffix
    snapshot = history_dir / f"{timestamp}_{agent_name}{ext}"
    shutil.copy2(str(path), str(snapshot))

    # Write optional summary sidecar (same name + .meta)
    if summary:
        meta_file = snapshot.with_suffix(snapshot.suffix + ".meta")
        meta_file.write_text(summary + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------

def append_changelog(base_dir, agent_name, file_path, action, summary="", lines_changed=0):
    """Append an entry to base_dir/changelog.jsonl.

    Args:
        base_dir: Directory containing changelog.jsonl (typically WORLD_DIR).
        agent_name: Who made the change.
        file_path: The file that was changed (absolute or relative).
        action: One of: "create", "edit", "delete", "restore".
        summary: One-line description.
        lines_changed: Approximate number of lines affected.
    """
    # Plan v1 step 0.1 (2026-05-19) — None guard: WORLD_DIR/META_DIR may
    # resolve to None when no external path is configured (the fallback to
    # PROJECT_ROOT/{world,meta} was removed to refuse cruft creation). A
    # caller passing None as base_dir would TypeError on Path(None) below;
    # this guard turns that into a single-source diagnostic naming the
    # missing env var instead.
    if base_dir is None:
        raise RuntimeError(
            "append_changelog: base_dir is None — WORLD_DIR/META_DIR "
            "unresolved (no MIND_WORLD/MIND_META env, no conf entry). "
            "Bind an agent via /start, or set the env var explicitly. "
            "The PROJECT_ROOT/world|meta fallback was removed in plan v1 "
            "step 0.1 (2026-05-19) to refuse silent cruft creation."
        )
    # Resolve so the cruft check sees the same shape save_history does,
    # and so the rel_path computation below doesn't redundantly resolve.
    base_dir = Path(base_dir).resolve()

    if _cruft_tripwire(base_dir, "append_changelog", "changelog entry"):
        return

    changelog = base_dir / "changelog.jsonl"

    # Make file_path relative to base_dir for readability
    try:
        rel_path = str(Path(file_path).resolve().relative_to(base_dir))
    except ValueError:
        rel_path = str(file_path)

    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": agent_name,
        "file": rel_path,
        "action": action,
        "summary": summary,
        "lines_changed": lines_changed,
    }

    # Multi-agent concurrency: caller-side locks protect the file being edited
    # but NOT the shared changelog.jsonl. Two agents writing different files
    # concurrently both call append_changelog → unsynchronized appends to the
    # same file. On Windows, partial-line interleaving across OS page boundaries
    # produces corrupt JSONL that crashes downstream consumers (history.py,
    # schema-drift-sweep.py). Pattern matches locked_append_jsonl below — same
    # acquire/append/release with shared `.lock` sibling. (, fresh-eyes
    # finding from  iter-14.)
    lock_path = changelog.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        with open(changelog, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Path Resolution
# ---------------------------------------------------------------------------

def resolve_base_dir(path):
    """Determine which base directory (WORLD_DIR, META_DIR, AGENT_DIR, or PROJECT_ROOT/.claude/) a path belongs to.

    Returns the base dir Path, or None if the path doesn't match any
    configured directory. Order: WORLD_DIR > META_DIR > AGENT_DIR > .claude/.
    AGENT_DIR is None when no agent is bound (MIND_AGENT unset) — that branch is skipped.

    Phase 1 G1 patch (world/conventions/self-program-evolution.md): AGENT_DIR added
    so save_history(), append_changelog(), and the locked_write_* family
    operate on <agent>/self.md, <agent>/aspirations.jsonl, and other
    per-agent files. Previously this function returned None for agent
    paths, which meant agent-file writes skipped history snapshots and
    changelog entries.

    Phase 2 G1b additive (world/conventions/self-program-evolution.md): PROJECT_ROOT/.claude/
    added so save_history() works for .claude/skills/**/SKILL.md and .claude/rules/*.md
    edits per §14 (US-03 — SKILL/rule autonomous evolution extension). Snapshots
    land in PROJECT_ROOT/.claude/.history/.
    """
    path = Path(path).resolve()
    if WORLD_DIR:
        try:
            if path.is_relative_to(WORLD_DIR.resolve()):
                return WORLD_DIR.resolve()
        except (ValueError, OSError):
            pass
    if META_DIR:
        try:
            if path.is_relative_to(META_DIR.resolve()):
                return META_DIR.resolve()
        except (ValueError, OSError):
            pass
    if AGENT_DIR:
        try:
            if path.is_relative_to(AGENT_DIR.resolve()):
                return AGENT_DIR.resolve()
        except (ValueError, OSError):
            pass
    claude_dir = (PROJECT_ROOT / ".claude").resolve()
    try:
        if path.is_relative_to(claude_dir):
            return claude_dir
    except (ValueError, OSError):
        pass
    return None


def _agent_name():
    """Get the current agent name, defaulting to 'system'."""
    return os.environ.get("MIND_AGENT", "system")


# ---------------------------------------------------------------------------
# UTF-8 Surrogate Gate ()
# ---------------------------------------------------------------------------
# Reject payloads containing unpaired UTF-16 surrogates (U+D800-U+DFFF). These
# code points cannot be encoded as valid UTF-8 and indicate upstream cp1252-
# decode corruption (the canonical source: stdin readers without explicit
# `sys.stdin.reconfigure(encoding="utf-8")` falling back to Windows cp1252,
# producing surrogateescape low-surrogates that round-trip into JSONL files).
# Loud failure here prevents the mojibake from landing on disk; the writer's
# caller sees a ValueError with the offending path and field.
#
# Set FILEOPS_SURROGATE_GATE=off to disable (emergency escape hatch only).

def _has_surrogates(s):
    """True if string contains any U+D800-U+DFFF code point."""
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def _validate_no_surrogates(item, path_for_error):
    """Walk item; raise ValueError if any string contains an unpaired surrogate.

    No-op when FILEOPS_SURROGATE_GATE=off. Walks dict keys+values, list
    elements, tuple elements. Non-string atoms ignored. Bounded recursion via
    Python's normal stack — deeply-nested payloads (>1000 levels) would hit
    RecursionError, but JSONL items in this codebase are flat-ish records.
    """
    if os.environ.get("FILEOPS_SURROGATE_GATE", "").lower() == "off":
        return
    stack = [item]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if _has_surrogates(cur):
                preview = cur[:80].encode("unicode_escape").decode("ascii")
                raise ValueError(
                    f"FILEOPS_SURROGATE_GATE: unpaired surrogate in payload for "
                    f"{path_for_error} — fragment={preview!r}. Likely cause: "
                    "stdin read without `sys.stdin.reconfigure(encoding=\"utf-8\")` "
                    "fell back to cp1252. Fix at ingest, then retry."
                )
        elif isinstance(cur, dict):
            stack.extend(cur.keys())
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)


# ---------------------------------------------------------------------------
# Atomic Write with OneDrive-Lock Fallback (, hoisted)
# ---------------------------------------------------------------------------
# OneDrive Files-On-Demand reparse points block os.replace with WinError 5
# (Access denied) even when the file is pinned. The reparse-point holds a
# handle that tolerates write-through (open-truncate-write) but refuses
# rename. This helper encapsulates the retry+fallback policy so every
# locked_write_* path uses the same numbers — single source of truth for
# the lock-contention strategy. Was previously duplicated in 5 places with
# inconsistent retry counts (5 vs 8) and inconsistent fallback (none vs
# in-place rewrite); see commit c234c7e for the original aspirations.py
# implementation that the others now match.

def _atomic_write_with_fallback(target_path, write_to_handle, *,
                                fallback_counter_key=None, max_retries=10):
    """Write target_path atomically; fall back to in-place rewrite under contention.

    Strategy:
      1. Write content to <target>.tmp once (via write_to_handle callable).
      2. Attempt os.replace with exponential backoff up to max_retries
         (cap 5s/wait, ~16.7s total budget at default max_retries=10:
          ~0.1, 0.15, 0.25, 0.45, 0.85, 1.65, 3.25, 5.0, 5.0s — 9 sleeps).
         Schedule tuned to the .fallback-stats.jsonl distribution (g-285-03,
         see world/conventions/file-system-resilience.md): the prior 8-retry
         / 3s-cap schedule (~6.45s budget) fell through to fallback on
         every 30-60s sync burst (11.7% of observed fallbacks). The new
         budget covers 64%+ of observed bursts within retry. The 5-30min
         sustained-burst cases (25.7%) still fall back — fallback is
         correct, this is a latency-not-correctness tune.
      3. If all retries fail, fall back to in-place truncate-rewrite of
         target_path. Sacrifices crash-atomicity for liveness.

    Crash safety: the fallback path leaves a partial file if the process
    is killed mid-write. Mitigations rely on the caller:
      - Caller MUST hold target_path.with_suffix('.lock') so the partial
        file cannot collide with another writer.
      - Caller SHOULD have called save_history() before this helper, so a
        recent snapshot exists in <base_dir>/.history/<rel-path>/.
      - Read paths SHOULD use read_jsonl_with_recovery() (or equivalent)
        for parse-or-restore semantics on JSONL targets.

    Args:
        target_path: Path-like to the live file.
        write_to_handle: callable(open_writable_handle) writing the FULL
            file content. Called once for the tmp write, and again for the
            fallback rewrite. Must be idempotent — must produce the same
            output both times.
        fallback_counter_key: Optional string identifier; when fallback
            fires, appends a record to <WORLD_DIR>/.fallback-stats.jsonl
            keyed by this string. Recommended values: "locked_write_jsonl",
            "locked_write_json", "locked_write_yaml", "locked_modify_yaml",
            "aspirations_live".
        max_retries: Number of os.replace attempts before falling back.
            Default 8.

    Raises:
        Whatever write_to_handle raises (e.g., serialization error). On
        write_to_handle success but os.replace exhaustion, falls back
        rather than raising — that's the whole point of this helper.
    """
    target_path = Path(target_path)
    # DETERMINISTIC tmp path — required, not stylistic. Two callers cannot
    # collide here because each must hold target_path.with_suffix('.lock'),
    # which is single-writer. Do NOT switch to tempfile.NamedTemporaryFile —
    # random names break (a) cleanup of orphaned tmps from prior crashes
    # (next "w" open truncates the deterministic name), and (b) the implicit
    # contract that the lock file name maps 1:1 to its tmp file name.
    tmp_path = Path(str(target_path) + ".tmp")

    # Write tmp once.
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            write_to_handle(f)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise

    last_err = None
    # : capture retry-loop start to surface contention as observable
    # telemetry (meta/file-contention-telemetry.jsonl). Zero-retry writes
    # produce no telemetry — only contention events are recorded.
    _retry_start_ms = time.monotonic() * 1000.0
    for attempt in range(max_retries):
        try:
            # os.replace is the ONLY atomic path. Do NOT replace with
            # shutil.move (falls back to copy+delete, non-atomic) or with a
            # remove+rename pair (TOCTOU race on the target). When OneDrive
            # blocks os.replace, the in-place fallback below is the only
            # safe alternative — see commit c234c7e.
            os.replace(str(tmp_path), str(target_path))
            # Success — atomic path taken. Emit telemetry if any retries occurred.
            if attempt > 0:
                _record_contention_telemetry(
                    target_path=str(target_path),
                    retry_count=attempt,
                    wall_clock_ms=int(time.monotonic() * 1000.0 - _retry_start_ms),
                    fallback_used=False,
                    error_class=type(last_err).__name__ if last_err else None,
                )
            return
        except (PermissionError, OSError) as e:
            last_err = e
            if attempt == max_retries - 1:
                break
            wait = min(0.05 * (2 ** attempt) + random.uniform(0, 0.1), 5.0)
            print(f"_atomic_write retry {attempt+1}/{max_retries}: {e} "
                  f"(waiting {wait:.2f}s) target={target_path.name}",
                  file=sys.stderr)
            time.sleep(wait)

    # Retries exhausted. Fall back to in-place rewrite.
    # WHY direct open: OneDrive's reparse point tolerates write-through but
    # refuses rename. Going around os.replace via in-place truncate-rewrite
    # is the WHOLE REASON this helper exists. Do NOT replace this with
    # another rename attempt or a copy-then-rename — both hit the same
    # reparse-point block. Crash-atomicity is sacrificed deliberately;
    # read_jsonl_with_recovery handles post-crash recovery from .history/.
    print(f"_atomic_write FALLBACK to in-place rewrite "
          f"after {max_retries} replace attempts: {last_err} "
          f"target={target_path.name}", file=sys.stderr)
    try:
        with open(target_path, "w", encoding="utf-8") as live:
            write_to_handle(live)
        if fallback_counter_key:
            _record_fallback_hit(fallback_counter_key, str(target_path),
                                 str(last_err))
        # : also emit contention telemetry on fallback path so
        # observers see fallback as a TYPE of contention event, not just a
        # separate stat. fallback_used distinguishes the two cases.
        _record_contention_telemetry(
            target_path=str(target_path),
            retry_count=max_retries,
            wall_clock_ms=int(time.monotonic() * 1000.0 - _retry_start_ms),
            fallback_used=True,
            error_class=type(last_err).__name__ if last_err else None,
        )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _record_contention_telemetry(target_path, retry_count, wall_clock_ms,
                                 fallback_used, error_class=None):
    """Append a lock-contention record to meta/file-contention-telemetry.jsonl.

    g-285-04: surfaces _atomic_write retry-loop contention as observable
    signal. One line per write that experienced retries; zero-retry writes
    produce no telemetry (zero overhead on the hot path).

    Best-effort: never raises (same pattern as _record_fallback_hit).
    Schema:
      timestamp     — local ISO 8601, agent system clock
      agent         — MIND_AGENT or "unknown"
      target        — absolute path of the contended file
      retry_count   — number of os.replace attempts that failed
      wall_clock_ms — total ms spent in retry loop (including final attempt)
      fallback_used — True if in-place rewrite was reached; False if a later
                      retry succeeded
      error_class   — last exception class name (PermissionError, OSError, ...)

    Read with: jq -c '.' <META_DIR>/file-contention-telemetry.jsonl
    Distribution: jq -s 'group_by(.fallback_used) | map({k:.[0].fallback_used,
                  n:length})' <META_DIR>/file-contention-telemetry.jsonl
    """
    if META_DIR is None:
        return
    try:
        telemetry_path = Path(META_DIR) / "file-contention-telemetry.jsonl"
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": _agent_name(),
            "target": target_path,
            "retry_count": retry_count,
            "wall_clock_ms": wall_clock_ms,
            "fallback_used": fallback_used,
            "error_class": error_class,
        }
        # Plain append, no lock — same best-effort policy as _record_fallback_hit.
        # Record is well under 4KB, so the write is single-line atomic on most
        # filesystems. A torn line is harmless observability noise.
        with open(telemetry_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        return


def _record_fallback_hit(counter_key, target_path, error_msg):
    """Append a fallback-hit record to world/.fallback-stats.jsonl.

    Best-effort observability: never raises. Auxiliary logging MUST NOT
    crash the writer — same pattern as log_script_decision below.

    Each line: {timestamp, agent, key, target, error}

    Read with: jq -c '.' <WORLD_DIR>/.fallback-stats.jsonl
    Count last hour: jq -c 'select(.timestamp > "...")' ... | wc -l
    """
    if WORLD_DIR is None:
        return
    try:
        stats_path = Path(WORLD_DIR) / ".fallback-stats.jsonl"
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": _agent_name(),
            "key": counter_key,
            "target": target_path,
            "error": error_msg,
        }
        # Plain append, no lock — best-effort, single-line atomic on most
        # filesystems for sub-PIPE_BUF writes (this record is well under
        # 4KB). Worse case: a torn line in the stats file is harmless,
        # this is an observability sidecar not durable state.
        with open(stats_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        return


# ---------------------------------------------------------------------------
# Read-Side Recovery from .history/ Snapshots ( follow-up B)
# ---------------------------------------------------------------------------
# The _atomic_write_with_fallback fallback can leave a partial file on
# crash. read_jsonl_with_recovery detects severe corruption (zero parsed
# records when lines exist, or every line failed to parse) and restores
# the most recent .history/ snapshot in place. The corrupt file is
# preserved as <path>.corrupt for forensics.

def _find_latest_history_snapshot(path):
    """Find the most recent .history/ snapshot for path, or None.

    Snapshots are named <timestamp>_<agent><ext>; sort lexicographically
    descending picks the newest. Skips .meta sidecars.
    """
    base_dir = resolve_base_dir(path)
    if base_dir is None:
        return None
    try:
        rel = Path(path).resolve().relative_to(Path(base_dir).resolve())
    except ValueError:
        return None
    history_dir = Path(base_dir) / ".history" / str(rel)
    if not history_dir.exists():
        return None
    snapshots = sorted(
        [p for p in history_dir.iterdir() if p.suffix != ".meta" and p.is_file()],
        reverse=True,
    )
    if not snapshots:
        return None
    return snapshots[0]


def read_jsonl_with_recovery(path):
    """Read a JSONL file with severe-corruption recovery from .history/.

    Routine partial-line corruption (one bad line out of many): skipped
    with stderr WARN, returns the parseable subset.

    Severe corruption (zero parseable lines despite non-empty file, or
    every line failed to parse with at least 3 lines present): restore
    the most recent .history/ snapshot in place. The corrupt file is
    saved as <path>.corrupt for forensics.

    Returns: list of parsed records.
    """
    path = Path(path)
    if not path.exists():
        return []

    items, parse_errors, total_lines = _parse_jsonl_skip_corrupt(path)

    severe_corruption = (
        (total_lines > 0 and len(items) == 0) or
        (total_lines >= 3 and parse_errors == total_lines)
    )
    if not severe_corruption:
        return items

    snapshot = _find_latest_history_snapshot(path)
    if snapshot is None:
        print(f"[read_jsonl_with_recovery] ERROR: severe corruption in "
              f"{path} but no history snapshot available — returning "
              f"{len(items)} parseable records", file=sys.stderr)
        return items

    backup = Path(str(path) + ".corrupt")
    # No try/except around shutil.copy — failures must propagate. The two
    # realistic failure modes (target locked by a parallel writer's fallback
    # rewrite, or genuine OS error) are both signal the caller MUST see, not
    # mask. Hiding them would silently truncate the aspirations queue.
    shutil.copy(str(path), str(backup))
    shutil.copy(str(snapshot), str(path))
    print(f"[read_jsonl_with_recovery] RECOVERED: restored {path.name} "
          f"from {snapshot.name}; corrupted version saved to "
          f"{backup.name}", file=sys.stderr)
    # Observability: log the recovery to fallback-stats so the same
    # dashboard surfaces both write-side fallback hits and read-side
    # recoveries — together they tell the OneDrive-contention story.
    _record_fallback_hit("read_jsonl_recovery", str(path),
                         f"severe corruption: parsed={len(items)}/"
                         f"{total_lines} lines")
    items, _, _ = _parse_jsonl_skip_corrupt(path)
    return items


def _parse_jsonl_skip_corrupt(path):
    """Parse JSONL skipping corrupt lines. Returns (items, parse_errors, total_lines)."""
    items = []
    parse_errors = 0
    total_lines = 0
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            total_lines += 1
            try:
                items.append(json.loads(stripped))
            except json.JSONDecodeError as e:
                parse_errors += 1
                print(f"[read_jsonl] WARN: skipped corrupt line {line_no} "
                      f"in {path}: {e}", file=sys.stderr)
    return items, parse_errors, total_lines


# ---------------------------------------------------------------------------
# Locked Write Operations
# ---------------------------------------------------------------------------
# These wrap lock + history + atomic write + changelog into single calls.
# Scripts delegate their write_jsonl/write_yaml/etc. to these.
# Atomic write + OneDrive-fallback policy lives in _atomic_write_with_fallback
# above — single source of truth for retry/fallback semantics.

def locked_write_jsonl(path, items):
    """Lock → history → atomic JSONL rewrite → changelog → unlock."""
    # Plan v1 step 0.1 (2026-05-19) — None guard. See append_changelog.
    if path is None:
        raise RuntimeError(
            "locked_write_jsonl: path is None — likely WORLD_DIR/META_DIR "
            "unresolved (no MIND_WORLD/MIND_META env, no conf entry). "
            "The PROJECT_ROOT/world|meta fallback was removed in plan v1 "
            "step 0.1 (2026-05-19) to refuse silent cruft creation."
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    # : validate BEFORE acquiring the lock so the failure mode is
    # cheap (no lock churn on rejected writes).
    for item in items:
        _validate_no_surrogates(item, path)
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)

        def _write(handle):
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=True) + "\n")
        _atomic_write_with_fallback(
            path, _write, fallback_counter_key="locked_write_jsonl")

        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=len(items))
    finally:
        release_lock(lock_path)


def locked_append_jsonl(path, item):
    """Lock → history → JSONL append → changelog → unlock.

    No retry loop here — retrying an append risks writing duplicate records.
    """
    # Plan v1 step 0.1 (2026-05-19) — None guard. See append_changelog.
    if path is None:
        raise RuntimeError(
            "locked_append_jsonl: path is None — likely WORLD_DIR/META_DIR "
            "unresolved (no MIND_WORLD/MIND_META env, no conf entry). "
            "The PROJECT_ROOT/world|meta fallback was removed in plan v1 "
            "step 0.1 (2026-05-19) to refuse silent cruft creation."
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    # : validate BEFORE acquiring the lock.
    _validate_no_surrogates(item, path)
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=True) + "\n")
        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=1)
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Atomic Read-Modify-Write for JSONL (g-280-* concurrency hardening)
# ---------------------------------------------------------------------------
# Without these, every script that did
#   items = read_jsonl(path)
#   items[i] = mutated; or items.append(new); or check_no_duplicate_id(items, id)
#   locked_append_jsonl(path, item)  # or locked_write_jsonl(path, items)
# had a race window between the unlocked read and the locked write. Two
# concurrent agents could both observe the same baseline and the second
# writer would silently clobber the first. The team-state.py audit traced
# this back to the read-then-locked-write split (see git log e2276b5 for
# the team-state fix that motivated extending the same primitive to JSONL).
#
# Both primitives below hold the lock across the full read-modify-write.
# Choose by call shape:
#   - locked_modify_jsonl: rewrite-all (update-path: increment counter,
#     mutate field, replace record). O(N) write.
#   - locked_append_jsonl_with_allocator: append-only with allocator-
#     computed id (add-path: rb_add, guard_add, sig_add, sq_add, journal,
#     board.cmd_post). O(1) write.
#
# NEITHER lock is re-entrant. Calling either primitive on the SAME path
# from inside modifier_fn / build_record_fn deadlocks until the 30s
# stale-lock break. Modifier/build callbacks must be pure in-memory work.

def locked_modify_jsonl(path, modifier_fn, *, initial=None):
    """Atomic read-modify-write of a JSONL file.

    Holds the lock across the entire cycle: read items list → invoke
    modifier_fn(items) → write the returned list atomically. Two concurrent
    callers cannot read the same baseline and clobber each other.

    Args:
      path: Path to the JSONL file.
      modifier_fn: Callable[[list[dict]], list[dict]] receiving the current
        records list and returning the new list to persist. May mutate the
        list in place and return it, or build a new list. Returning None is
        equivalent to returning the (possibly mutated) input list.
      initial: List used as starting state when the file does not exist. If
        None and the file is missing, modifier_fn receives [].

    Returns:
      The list of records that was written (modifier_fn's return value).

    Race semantics: same lineage as locked_modify_yaml (e2276b5). The read
    happens INSIDE the lock that guards the write — any concurrent writer
    either (a) finishes fully before our read, or (b) blocks until our
    write completes. No clobber.

    Recovery: severe-corruption recovery (zero parseable lines despite
    non-empty file, or every line failed to parse) restores the most
    recent .history/ snapshot in place via read_jsonl_with_recovery
    semantics. The corrupt file is preserved as <path>.corrupt for
    forensics. Routine partial-line corruption is skipped with a stderr
    WARN and the parseable subset is returned.

    Lock is NOT re-entrant. modifier_fn must NOT call locked_modify_jsonl,
    locked_append_jsonl, locked_append_jsonl_with_allocator, or
    locked_write_jsonl on the same path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        # Read inside the lock with the same severe-corruption recovery
        # semantics read_jsonl_with_recovery provides. Routine partial-line
        # corruption returns the parseable subset; severe corruption
        # restores from .history/ with the corrupt file preserved as
        # <path>.corrupt. Transient Windows PermissionError (anti-virus,
        # OneDrive sync) gets retry — symmetric to locked_modify_yaml.
        # The retry loop exits ONLY via break (items set) or raise (last
        # attempt) — there is no fall-through path, so no defensive
        # post-loop None-check is needed.
        if path.exists():
            read_retries = 5
            for attempt in range(read_retries):
                try:
                    items = read_jsonl_with_recovery(path)
                    break
                except (PermissionError, OSError) as e:
                    if attempt == read_retries - 1:
                        raise
                    wait = 0.05 * (2 ** attempt) + random.uniform(0, 0.1)
                    print(f"locked_modify_jsonl read-retry "
                          f"{attempt+1}/{read_retries}: {e} "
                          f"(waiting {wait:.2f}s)", file=sys.stderr)
                    time.sleep(wait)
        else:
            items = list(initial) if initial is not None else []

        # Modify
        new_items = modifier_fn(items)
        if new_items is None:
            new_items = items

        #  lineage: validate post-modify, pre-write. Cannot validate
        # before acquire_lock because new_items doesn't exist until
        # modifier_fn runs inside the lock. Surrogate-laced output fails
        # loud before yaml/json serialization corrupts the on-disk file.
        for item in new_items:
            _validate_no_surrogates(item, path)

        # Write inside the same lock
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)

        def _write(handle):
            for item in new_items:
                handle.write(json.dumps(item, ensure_ascii=True) + "\n")
        _atomic_write_with_fallback(
            path, _write, fallback_counter_key="locked_modify_jsonl")

        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=len(new_items))
        return new_items
    finally:
        release_lock(lock_path)


def locked_append_jsonl_with_allocator(path, build_record_fn, *, initial=None):
    """Atomic append with allocator-computed fields. O(1) write — no rewrite.

    Holds the lock across read → build_record_fn(items) → append. The
    builder sees the current records inside the lock, so allocator output
    (next id, next session number, next message-NNN counter) cannot collide
    with a concurrent caller's allocator output on the same file.

    Args:
      path: Path to the JSONL file.
      build_record_fn: Callable[[list[dict]], dict] receiving the current
        records list and returning the new record to append. The record
        MUST be complete (id, timestamps, etc.) — the primitive does not
        mutate it.
      initial: List used as starting state when the file does not exist
        (rarely matters for append paths; included for symmetry).

    Returns:
      The record that was appended (build_record_fn's return value).

    Use this for add-paths (rb_add, guard_add, board.cmd_post, journal,
    sig_add, sq_add). For update-paths (mutating an existing record's
    field, incrementing a counter), use locked_modify_jsonl instead.

    Lock is NOT re-entrant. build_record_fn must NOT call any locked_*
    primitive on the same path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        # Read inside the lock for allocator visibility. Same recovery
        # path as locked_modify_jsonl — severe corruption restores from
        # .history/. Retry loop exits via break (items set) or raise (last
        # attempt) — no fall-through, no defensive None-check needed.
        if path.exists():
            read_retries = 5
            for attempt in range(read_retries):
                try:
                    items = read_jsonl_with_recovery(path)
                    break
                except (PermissionError, OSError) as e:
                    if attempt == read_retries - 1:
                        raise
                    wait = 0.05 * (2 ** attempt) + random.uniform(0, 0.1)
                    print(f"locked_append_jsonl_with_allocator read-retry "
                          f"{attempt+1}/{read_retries}: {e} "
                          f"(waiting {wait:.2f}s)", file=sys.stderr)
                    time.sleep(wait)
        else:
            items = list(initial) if initial is not None else []

        # Build record with allocator visibility into existing items
        new_record = build_record_fn(items)
        if new_record is None:
            raise ValueError(
                "build_record_fn returned None — must return the record "
                "to append (or raise to abort)")

        # Validate post-build, pre-append
        _validate_no_surrogates(new_record, path)

        # Append (no rewrite — this is the perf advantage over
        # locked_modify_jsonl). save_history snapshots the file BEFORE
        # the append so a single-record-back undo is always possible.
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(new_record, ensure_ascii=True) + "\n")
        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=1)
        return new_record
    finally:
        release_lock(lock_path)


def next_id_for_prefix(items, prefix, *, id_field="id", pad_width=0,
                        separator="-"):
    """Compute the next sequential id for a prefix-NNN id scheme.

    Scans items for ids matching ^{prefix}{separator}(\\d+)$ (any digit
    count), returns f"{prefix}{separator}{N+1:0{pad_width}d}". If no items
    match, returns f"{prefix}{separator}{1:0{pad_width}d}".

    Non-conforming ids (legacy formats, different prefixes, ids with
    suffixes) are silently skipped — only valid prefix-NNN ids count
    toward max. This means an existing rb-9999z record will not block
    rb-{N+1} where N is the highest valid rb-NNN.

    Args:
      items: List of records (dicts).
      prefix: ID prefix without separator (e.g., "rb", "guard", "sig", "de"
        for separator="-"; "sq-c" for the spark-question candidate format
        sq-c01 with separator="").
      id_field: Field name where the id lives. Default "id".
      pad_width: Zero-pad the numeric portion to this many digits. Default
        0 (no padding — produces "rb-735" not "rb-00735"). Use 3 for
        spark-question questions ("sq-001" format), 2 for candidates
        ("sq-c01" format).
      separator: Character between prefix and number. Default "-". Pass
        "" for ids like "sq-c01" where the prefix already ends with the
        format ("sq-c") and the digits attach directly.

    Returns:
      The next id string.
    """
    import re
    pattern = re.compile(
        rf"^{re.escape(prefix)}{re.escape(separator)}(\d+)$")
    max_n = 0
    for item in items:
        match = pattern.match(item.get(id_field, "") or "")
        if match:
            n = int(match.group(1))
            if n > max_n:
                max_n = n
    next_n = max_n + 1
    if pad_width > 0:
        return f"{prefix}{separator}{next_n:0{pad_width}d}"
    return f"{prefix}{separator}{next_n}"


def locked_write_json(path, data):
    """Lock → history → atomic JSON rewrite → changelog → unlock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    # : validate BEFORE acquiring the lock.
    _validate_no_surrogates(data, path)
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)

        def _write(handle):
            json.dump(data, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
        _atomic_write_with_fallback(
            path, _write, fallback_counter_key="locked_write_json")

        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
    finally:
        release_lock(lock_path)


def log_script_decision(script_name, record):
    """Append a per-decision audit record to world/<script-name>-log.jsonl.

    Lightweight append-only: lock + append + unlock. Skips save_history and
    changelog — these logs are append-forever audit trails, not structural
    data worth versioning.

    DESIGN — do not "fix" without reading both points:
      1. Entry injection order: `{**record, "timestamp": ..., "agent": ...}`
         is intentional. Helper-owned metadata MUST win if a caller passes
         a field of the same name. Do NOT reorder.
      2. Broad `except Exception: return` is intentional. Audit logging is
         auxiliary; infrastructure failure (disk full, permission denied,
         lock contention) MUST NOT crash the caller's main decision flow.
         Do NOT narrow this except — `locked_append_jsonl` fails loudly
         on purpose because it writes durable state; this helper does not.
    """
    if WORLD_DIR is None:
        return
    try:
        path = Path(WORLD_DIR) / f"{script_name}-log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            **record,
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": _agent_name(),
        }
        lock_path = path.with_suffix(".lock")
        # Audit-log fast path: tiny single-line append, sub-millisecond hold.
        # 5s stale-break is 5000x cycle time — short enough to recover from a
        # crashed mid-write quickly without false-breaking a healthy holder.
        acquire_lock(lock_path, stale_seconds=5)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")
        finally:
            release_lock(lock_path)
    except Exception:
        return


def locked_write_yaml(path, data):
    """Lock → history → atomic YAML rewrite → changelog → unlock."""
    import yaml
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    # : validate BEFORE acquiring the lock so the failure mode is
    # cheap (no lock churn on rejected writes). Mirrors locked_write_json.
    _validate_no_surrogates(data, path)
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)

        def _write(handle):
            yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                      default_flow_style=False, allow_unicode=True,
                      sort_keys=False)
        _atomic_write_with_fallback(
            path, _write, fallback_counter_key="locked_write_yaml")

        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
    finally:
        release_lock(lock_path)


def locked_modify_yaml(path, modifier_fn, initial=None):
    """Atomic read-modify-write on a YAML file. Holds the lock across the
    ENTIRE cycle, closing the race where two agents read the same baseline
    and the second writer clobbers the first.

    Args:
      path: Path to the YAML file.
      modifier_fn: Callable accepting the current data (dict) and returning
        the new data (dict) to write. May mutate in place and return the
        same object, or construct a new dict.
      initial: Dict to use as the starting state when the file does not
        exist. If None and the file is missing, the modifier receives {}.

    Returns:
      The data written to the file (modifier_fn's return value).

    Race semantics (g-240-52): cmd_update-style sites previously did
      state = read_state(); state[x] = y; write_state(state)
    The read was unlocked, so two concurrent writers could both observe
    the baseline before either wrote, and the second write clobbered the
    first. locked_modify_yaml reads INSIDE the lock that guards the write,
    so any other writer either (a) writes fully before our read starts,
    or (b) blocks until our write completes. Either way, no silent
    clobber.
    """
    import yaml
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        # Read inside the lock — this is the critical move. Retry on
        # transient Windows PermissionError from concurrent file-handle
        # churn (anti-virus, indexing, OneDrive sync, etc.); symmetric to
        # the write-retry loop below. Without this, a racer's read can
        # fail even when the lock is held by no other agent.
        data = None
        if path.exists():
            read_retries = 5
            for attempt in range(read_retries):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        # CSafeLoader (libyaml-backed) is ~6× faster than
                        # yaml.safe_load on large YAMLs (verified: _tree.yaml
                        # 758KB load 7.3s→1.25s). Required for the tree-sync
                        # PostToolUse hook to complete within its 5s timeout.
                        # CSafeDumper produces byte-identical output. DO NOT
                        # downgrade to safe_load — single source of truth.
                        data = yaml.load(f, Loader=yaml.CSafeLoader)
                    break
                except (PermissionError, OSError) as e:
                    if attempt == read_retries - 1:
                        raise
                    wait = 0.05 * (2 ** attempt) + random.uniform(0, 0.1)
                    print(f"locked_modify_yaml read-retry {attempt+1}/{read_retries}: "
                          f"{e} (waiting {wait:.2f}s)", file=sys.stderr)
                    time.sleep(wait)
            # NOT dead code — diverges from locked_modify_jsonl pattern.
            # PyYAML parse returns None for empty/whitespace/comment-only
            # YAML files (regardless of loader). read_jsonl_with_recovery
            # always returns a list, so its sibling post-loop None-check
            # was dead and got removed.
            # Here it is REQUIRED — without it, an empty YAML file produces
            # data=None which crashes modifier_fn / _validate_no_surrogates.
            # Verified by  (2026-05-08).
            if data is None:
                data = dict(initial) if initial is not None else {}
        else:
            data = dict(initial) if initial is not None else {}

        # Modify
        new_data = modifier_fn(data)
        if new_data is None:
            # Allow the modifier to signal "no write" by returning None; fall
            # back to the mutated-in-place data. If the caller truly wants to
            # skip the write they can short-circuit before calling this.
            new_data = data

        # : validate post-modify, pre-write. Cannot validate before
        # acquire_lock here (unlike locked_write_yaml) because new_data does
        # not exist until modifier_fn runs inside the lock. The walk is cheap
        # and short-circuits on the kill-switch; it raises before yaml.dump
        # so a surrogate-laced modifier_fn fails loud without writing the
        # tmp file or saving a .history/ snapshot of the corrupted attempt.
        _validate_no_surrogates(new_data, path)

        # Write inside the same lock
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)

        def _write(handle):
            yaml.dump(new_data, handle, Dumper=yaml.CSafeDumper,
                      default_flow_style=False, allow_unicode=True,
                      sort_keys=False)
        _atomic_write_with_fallback(
            path, _write, fallback_counter_key="locked_modify_yaml")

        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
        return new_data
    finally:
        release_lock(lock_path)
