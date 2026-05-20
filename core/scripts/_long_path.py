"""Windows MAX_PATH (260-char) workaround helper.

Win32 stdlib open() fails on paths exceeding 260 chars. The Win32
extended-prefix API (\\\\?\\) bypasses the limit but requires:
  - absolute paths only
  - all-backslash separators (no forward slashes mixed in)
  - no relative components (. or ..)

Pure no-op on non-Windows platforms — the prefix is meaningless and
POSIX has no MAX_PATH limit on stdlib open().

Origin: rb-450 / g-115-165 — tree walkers (session_artifacts_count.py,
schema-drift-sweep.py) silently skipped legitimately-readable .md files
because their paths exceeded 260 chars after deeply-nested category
expansion (e.g., proactive-architecture-triggers/proactive-pipeline-design.md
under intelligence/npc-intelligence/npc-cognition/...).

Earlier diagnosis (g-115-160, closed) misattributed the failure to OneDrive
cloud-only placeholders — bash could read those fine, which ruled out the
placeholder hypothesis. rb-450 corrected the root cause: it's the Python /
Win32 MAX_PATH limit, not OneDrive.
"""

import os
from pathlib import Path


def open_long_path(path, mode="r", encoding="utf-8"):
    """open() with Windows long-path retry via extended-prefix API.

    Tries stdlib open() first. On OSError on Windows, retries with the
    \\\\?\\ prefix. Re-raises the original error if both attempts fail.
    On POSIX, identical to open() (no retry path; the prefix is
    meaningless there).

    Args mirror stdlib open(). Returns the same file handle stdlib open
    would return — caller is responsible for closing it (use `with` if
    possible, or explicit close()).

    Path conversion preserves invariants required by the Win32 API:
      - Path.resolve() produces an absolute, normalized path
      - replace("/", "\\\\") converts any forward slashes (introduced by
        callers passing POSIX-style strings) to backslashes — required
        because the extended-prefix API rejects forward slashes
    """
    try:
        return open(path, mode, encoding=encoding)
    except OSError:
        if os.name != "nt":
            raise
        try:
            abs_path = str(Path(path).resolve()).replace("/", "\\")
            ext_path = "\\\\?\\" + abs_path
            return open(ext_path, mode, encoding=encoding)
        except OSError:
            raise
