"""Uncommitted-work gate logic (pure-ish — explicit side effect on override).

Refuses goal closure when framework code is dirty in the repo. See the CLI
wrapper at core/scripts/uncommitted-work-gate.py for the original docstring
and the orphan-code-audit rationale.

Public API:
    evaluate(goal_id, override, repo_path, world_dir, agent_name) -> dict

Output dict shape (matches the legacy CLI's JSON payload byte-for-byte):
    {
      "would_block": bool,
      "dirty_framework_files": [str, ...],   # sorted, deduped, repo-relative
      "repo_path": str,
      "goal_id": str,
      "override_applied": str | None,
    }

Side effect:
    When `override` is a non-empty string AND dirty_framework_files is
    non-empty, this function appends one record to
    `<world_dir>/uncommitted-work-overrides.jsonl` via the shared
    `_fileops.locked_append_jsonl` primitive. The audit-log write is
    fail-open (errors print to stderr but never propagate).

    If `world_dir` is None (caller couldn't resolve WORLD_DIR), the
    override is still accepted at the gate level but no ledger entry is
    written — same fail-open behavior as the legacy CLI.

Daemon safety:
    - Reads no environment variables. MIND_AGENT is passed in via
      `agent_name`. WORLD_DIR is passed in via `world_dir`. The function
      is therefore safe to call from any daemon thread without racing
      on per-request state.
    - Imports `locked_append_jsonl` at module load. That import chain
      pulls in `_paths` which reads env vars at module-load time — but
      this gate doesn't consume any of those globals, so the daemon's
      single `_paths` import doesn't pollute per-request behavior here.
    - `subprocess.run(["git", ...])` runs as a child process and does
      not interact with daemon state.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# _fileops lives at core/scripts/_fileops.py — our parent dir. The CLI
# wrapper has sys.path set up to find it; daemon callers do too via
# `mind_api/src/file_locks.py`'s sys.path insertion.
from _fileops import locked_append_jsonl  # type: ignore


# Framework-code patterns — paths matching ANY of these regexes block
# the close. Anything else is treated as agent state churn and ignored.
# This list is the SINGLE SOURCE OF TRUTH; the CLI wrapper imports
# evaluate() from this module and does not duplicate the patterns.
# DO NOT copy this list elsewhere — extend HERE and callers see it.
FRAMEWORK_INCLUDE_PATTERNS = [
    re.compile(r"^core/scripts/.+\.(py|sh)$"),
    re.compile(r"^core/config/.+\.(yaml|md)$"),
    re.compile(r"^\.claude/skills/[^/]+/.+\.(py|sh|md)$"),
    re.compile(r"^\.claude/rules/.+\.md$"),
    re.compile(r"^CLAUDE\.md$"),
]


def _is_framework_code(path: str) -> bool:
    norm = path.replace("\\", "/")
    return any(p.match(norm) for p in FRAMEWORK_INCLUDE_PATTERNS)


def _parse_porcelain_line(line: str) -> Optional[str]:
    """Extract the path from a `git status --porcelain` line.

    Format: 2 status chars + space + path. For renames (R), the format
    is 'R  old -> new' — we want the new path. Returns None on malformed.
    """
    if not line or len(line) < 4:
        return None
    rest = line[3:]
    if " -> " in rest:
        rest = rest.split(" -> ", 1)[1]
    return rest.strip().strip('"')


def get_dirty_framework_files(repo_path: Path) -> List[str]:
    """Return sorted/deduped list of dirty framework paths.

    Fail-open: subprocess errors → empty list (gate doesn't block when
    it can't probe).
    """
    try:
        # CRITICAL — --untracked-files=all preserves per-file rows for new
        # framework directories. See the legacy file's comment for why this
        # flag is load-bearing.
        result = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain",
             "--untracked-files=all"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"[uncommitted-gate] git status failed: {exc} — fail-open",
              file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"[uncommitted-gate] git status rc={result.returncode}: "
              f"{result.stderr.strip()} — fail-open", file=sys.stderr)
        return []
    dirty = []
    for line in result.stdout.splitlines():
        path = _parse_porcelain_line(line)
        if path and _is_framework_code(path):
            dirty.append(path)
    return sorted(set(dirty))


def _log_override(world_dir: Path, agent_name: str, goal_id: str,
                  justification: str, dirty_files: List[str]) -> None:
    """Append to <world_dir>/uncommitted-work-overrides.jsonl.

    Fail-open: errors print to stderr; the gate never blocks on
    audit-log infrastructure problems.
    """
    ledger = world_dir / "uncommitted-work-overrides.jsonl"
    record = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "agent": agent_name or "unknown",
        "goal_id": goal_id,
        "justification": justification,
        "dirty_files": dirty_files,
    }
    try:
        locked_append_jsonl(str(ledger), record)
    except Exception as exc:
        print(f"[uncommitted-gate] override-log write failed: {exc}",
              file=sys.stderr)


def evaluate(*, goal_id: str, override: Optional[str], repo_path: Path,
             world_dir: Optional[Path], agent_name: str = "") -> dict:
    """Run the gate. Pure function for the decision; explicit side effect on override.

    Args:
        goal_id: ID of the goal being closed (audit-log correlation).
        override: justification string, or None for no override. Empty /
            whitespace-only strings are coerced to None with a stderr warn
            — matches the legacy CLI's behavior (an empty audit-log entry
            would defeat the audit's purpose).
        repo_path: Path to scan with `git status --porcelain`. Usually
            the framework repo.
        world_dir: WORLD_DIR for the override audit log. None disables
            the audit-log write (with a stderr warning).
        agent_name: MIND_AGENT value for the audit-log "agent" field.
            Empty string is recorded as "unknown" to match legacy.

    Returns:
        Dict with keys would_block, dirty_framework_files, repo_path,
        goal_id, override_applied. Byte-for-byte JSON-equivalent to the
        legacy CLI's payload.
    """
    # Empty/whitespace-only override → treat as no override + warn. This
    # mirrors the legacy CLI's `main()` block at L226-235; the rationale
    # there is that an empty-justification audit entry would defeat the
    # audit's purpose, and exiting non-zero would silently fail-open in
    # the subprocess caller (which fail-opens on non-JSON exits).
    effective_override = override
    if effective_override is not None and effective_override.strip() == "":
        print("[uncommitted-gate] WARN: --override is empty/whitespace-only "
              "— treating as no override. Provide a real justification or "
              "commit the dirty files.", file=sys.stderr)
        effective_override = None

    dirty = get_dirty_framework_files(repo_path)
    would_block = bool(dirty) and effective_override is None

    if effective_override is not None and dirty:
        if world_dir is None:
            print("[uncommitted-gate] WARN: no WORLD_DIR — skipping override log",
                  file=sys.stderr)
        else:
            _log_override(world_dir, agent_name, goal_id, effective_override, dirty)

    return {
        "would_block": would_block,
        "dirty_framework_files": dirty,
        "repo_path": str(repo_path),
        "goal_id": goal_id,
        "override_applied": effective_override,
    }
