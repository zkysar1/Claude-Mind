"""`.history/` snapshots for the daemon's write path.

Mirrors `_fileops.save_history` exactly so daemon-written history snapshots
are interchangeable with fallback-path snapshots — same filename format,
same skip-on-missing semantics.

Snapshot location:
    <base_dir>/.history/<relative-path>/<timestamp>_<agent><ext>

Where:
    base_dir       = WORLD_DIR or META_DIR (resolved per-request, not global)
    relative-path  = path.relative_to(base_dir)
    timestamp      = local time, "%Y-%m-%dT%H-%M-%S" (hyphens — Windows
                     filenames can't contain colons)
    agent          = X-Mind-Agent header value
    ext            = path.suffix (.jsonl, .yaml, .md, ...)
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def snapshot(path: Path, base_dir: Path, agent_name: str,
             summary: str = "") -> Optional[Path]:
    """Save a copy of `path` to `<base_dir>/.history/...` before overwriting.

    Returns the snapshot path on success, None when `path` does not yet
    exist (new file — nothing to version).

    CRITICAL: identical byte-format and filename to `_fileops.save_history`.
    A reader looking at .history/ should not be able to tell which writer
    (daemon or fallback) produced any given snapshot.

    Pure path operations + shutil.copy2. No locks here — callers MUST
    already hold the file lock via `file_locks.locked(path)`.
    """
    path = Path(path).resolve()
    base_dir = Path(base_dir).resolve()
    if not path.exists():
        return None

    rel = path.relative_to(base_dir)
    history_dir = base_dir / ".history" / str(rel)
    history_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    ext = path.suffix
    snapshot_path = history_dir / f"{timestamp}_{agent_name}{ext}"
    shutil.copy2(str(path), str(snapshot_path))

    if summary:
        meta_file = snapshot_path.with_suffix(snapshot_path.suffix + ".meta")
        # Trailing newline matches _fileops.save_history byte-for-byte —
        # see CRITICAL note in module docstring. Do not strip.
        meta_file.write_text(summary + "\n", encoding="utf-8")

    return snapshot_path
