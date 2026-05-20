"""Changelog appends for the daemon's write path.

Mirrors `_fileops.append_changelog` schema AND locking exactly so daemon-
written entries are indistinguishable from fallback-path entries.

Schema (one JSONL line per write to base_dir/changelog.jsonl):
    {
      "timestamp": "<local ISO, second precision>",
      "agent": "<X-Mind-Agent value or 'system'>",
      "file": "<path relative to base_dir, or absolute if not relative>",
      "action": "create" | "edit" | "delete" | "restore",
      "summary": "<one-line description>",
      "lines_changed": <int>,
    }
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import file_locks


def append(base_dir: Path, agent_name: str, file_path: Path,
           action: str, summary: str = "", lines_changed: int = 0) -> None:
    """Append a changelog entry to `<base_dir>/changelog.jsonl`.

    Best-effort: write failures (full disk, permission) are swallowed so
    they don't propagate up and mask the underlying user-visible write.
    Mirrors _fileops.append_changelog's behaviour.

    CRITICAL: holds `changelog.lock` across the append (file_locks.locked
    uses path.with_suffix('.lock'), matching _fileops). Two writers — two
    daemon threads OR a daemon thread alongside a fallback-path direct-
    python writer — serialise on the same lock file. Without it, partial-
    line interleaving across OS page boundaries can produce corrupt JSONL
    on Windows (g-240-78, original incident: schema-drift-sweep crashed
    parsing torn lines). Do not "simplify" by dropping the lock.
    """
    base_dir = Path(base_dir)
    changelog = base_dir / "changelog.jsonl"

    try:
        rel_path = str(Path(file_path).resolve().relative_to(base_dir.resolve()))
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

    try:
        with file_locks.locked(changelog):
            with changelog.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except OSError:
        pass
