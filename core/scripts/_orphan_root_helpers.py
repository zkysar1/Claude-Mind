"""Helpers for orphan-root-sweep — Mode D detection ().

Mode D (added 2026-05-14, see external-path-resolution-cruft.md): cruft
directories at PROJECT_ROOT whose names encode a stale-daemon path-
resolution failure. Two shapes:

  1. Name contains U+F03A — the NTFS-remapped colon character. The OS
     rewrites an attempt to create `C:` as `C` because `:` is
     reserved on Windows. Shows up in `git status` as `C\357\200\272/`
     (the octal-escaped UTF-8 byte sequence).

  2. Name matches drive-letter-segment shape (single letter, optionally
     followed by `:` or U+F03A). Catches the literal `C:`/`C` shapes
     that survive when a POSIX-flavored Python interprets a Windows
     drive-letter path as relative-to-cwd.

This module has no I/O and no module-level side effects — it is pure
predicate logic, safe to import from anywhere. The bash sweeper at
`core/scripts/orphan-root-sweep.sh` Scan 4 implements the same logic
inline; this module is the authoritative reference impl + test target.

Diagnostic triage after detection: rb-939 (daemon-staleness three-probe
diagnosis), guard-554 (mandatory daemon restart after path-resolver fix).
Do not auto-delete findings — salvage may be needed first.
"""

from __future__ import annotations

_NTFS_REMAPPED_COLON = ""


def is_mode_d_cruft(name: str) -> bool:
    """Return True when a directory entry name matches Mode D cruft shape.

    Shape 1: name contains U+F03A anywhere (NTFS-remapped colon).
    Shape 2: drive-letter-segment shape — exactly 1 letter, or 1 letter
             followed by exactly ':' or U+F03A.

    >>> is_mode_d_cruft("C\\uf03a")
    True
    >>> is_mode_d_cruft("C")
    True
    >>> is_mode_d_cruft("C:")
    True
    >>> is_mode_d_cruft("alpha")
    False
    >>> is_mode_d_cruft("")
    False
    """
    if not name:
        return False
    # Shape 1: U+F03A anywhere in the name.
    if _NTFS_REMAPPED_COLON in name:
        return True
    # Shape 2: drive-letter-segment shape.
    if len(name) == 1 and name.isalpha():
        return True
    if len(name) == 2 and name[0].isalpha() and name[1] == ":":
        return True
    return False
