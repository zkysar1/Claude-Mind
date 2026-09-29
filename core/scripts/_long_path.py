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


# A writer needs headroom BELOW the 260-char file limit, not just at it: a
# same-directory mkstemp sibling adds ".XXXXXXXX.tmp" (13 chars) to the name,
# and CreateDirectoryW stops at 248 (MAX_PATH - 12). Prefixing from 240 keeps
# the parent dir and the tmp sibling inside both limits whenever the path
# itself is below the threshold, and every derived path is prefixed above it.
# Measured 2026-09-28 on DESKTOP-O91DLK2 (LongPathsEnabled=0, ): a
# 250-char target reads fine but its mkstemp sibling fails errno 2, which is
# the failure that kept 11 of 14 deep tree nodes (248-259 chars) off the box.
_PREFIX_FROM = 240


def long_path(path):
    """`path` as a Path every Win32 file API accepts at any length.

    For read AND write, including the mkdir/mkstemp/os.replace dance of an
    atomic writer. Returns an absolute, backslashed, `\\\\?\\`-prefixed Path on
    Windows once the path reaches _PREFIX_FROM chars (UNC maps to
    `\\\\?\\UNC\\`), and `Path(path)` unchanged below it and everywhere else,
    so the common case is byte-identical to not calling it. Idempotent.

    Use the result ONLY for file I/O. A prefixed path is not `relative_to()`
    its unprefixed root, so never hand it to code that maps a path to a key.
    """
    if os.name != "nt":
        return Path(path)
    s = os.fspath(path)
    if s.startswith("\\\\?\\"):
        return Path(s)
    full = os.path.abspath(s)
    if len(full) < _PREFIX_FROM:
        return Path(path)
    if full.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + full[2:])
    return Path("\\\\?\\" + full)
