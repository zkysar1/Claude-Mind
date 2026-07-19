"""`.history/` snapshots for the daemon's write path.

DELEGATES to `_fileops.save_history` — the single snapshot writer — so daemon
writes get the same defenses as every other write path (g-115-2410):

  - CAS-delta store (Stage 2 authoritative): gzip'd content-addressed blobs +
    delta chains under `<base_dir>/.history/{blobs,patches,snapshots}/`,
    deduped on identical content.
  - `_SNAPSHOT_BLACKLIST`: high-churn no-restore-value files skip snapshots.
  - Per-file snapshot caps (`_PER_FILE_SNAPSHOT_CAP`) on the legacy tree.
  - JSONL parse-validation (guard-600): refuses to snapshot a non-empty
    source with zero parseable records (raises CorruptSourceError so the
    endpoint write fails loud instead of destroying the last-good snapshot).

HISTORY: this module previously re-implemented the Stage-0 snapshot format
(uncompressed full copy via shutil.copy2, no cap, no blacklist) and claimed
to "mirror _fileops.save_history exactly" — a claim that went three
generations stale as _fileops gained gzip (2026-05-22), then caps+blacklist,
then the CAS-delta store. Since the 2026-05-14 daemon-only cutover, that
meant EVERY daemon store write full-copied multi-MB JSONLs uncompressed:
measured 13.9GB of `.history/` growth in 4 days on one box (cc-04 disk
emergency, 2026-07-16). Delegation makes divergence structurally impossible.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Daemon-safe: save_history takes explicit paths; importing _fileops here is
# the same pattern file_locks.py already uses (see its import note).
from _fileops import save_history as _save_history  # noqa: E402


def snapshot(path: Path, base_dir: Path, agent_name: str,
             summary: str = "") -> Optional[Path]:
    """Save a pre-write snapshot of `path` via `_fileops.save_history`.

    Same call signature as before the g-115-2410 unification; none of the
    46 daemon call sites use the return value, so this now always returns
    None (save_history writes the CAS store and returns nothing).

    Skip-on-missing semantics preserved: a new file (path does not exist)
    is a no-op inside save_history.

    Raises CorruptSourceError (from _fileops) when a non-empty JSONL source
    parses to zero records — intended guard-600 fail-loud: the endpoint's
    write is refused rather than snapshotting corrupt state.

    Callers MUST already hold the file lock via `file_locks.locked(path)` —
    unchanged; save_history assumes the same.
    """
    _save_history(path, base_dir, agent_name, summary=summary)
    return None
