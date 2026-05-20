"""Shared path helpers used by both CLI and daemon code.

Single source of truth for `absolutize` — the function that defends
against the canonical g-115-733 cruft bug (drive-letter paths
misinterpreted as relative on POSIX-flavored Python).

This module has NO module-level side effects: no I/O, no env reads,
no config parsing. Safe to import from both CLI (`_paths.py`) and
daemon (`mind_api/src/agent_paths.py`).

DO NOT add module-level side effects here — that would defeat the
"shared by daemon and CLI" design. If new helpers need agent context
or PROJECT_ROOT discovery, add them at the call site, not here.
"""
from __future__ import annotations
from pathlib import Path, PureWindowsPath


def absolutize(value: str, project_root: Path) -> Path:
    """Coerce a path string to an absolute Path; treat Windows drive-letter
    prefix as absolute regardless of host OS interpretation.

    Two-stage defense (g-115-733):
      1. Drive-letter detection via `PureWindowsPath`. Catches the case
         where the host Python is POSIX/MSYS-flavored: `Path("C:/...")`
         returns a PosixPath that reports `is_absolute() == False`, and
         a naive helper would then anchor to project_root or cwd —
         producing the cruft mirror. On a Windows host the value is
         already absolute and returned unchanged (f1a646c); on a
         POSIX/MSYS host it is forced absolute (anchored at the
         filesystem root) so a bad-host write fails loudly instead of
         cwd-mirroring the cruft (v3 — closes f1a646c's residual gap).
      2. If neither drive-letter-absolute nor host-absolute, anchor to
         project_root — NEVER to cwd. cwd is mutable and not under
         the caller's control.

    Per `.claude/rules/path-resolution.md` and
    `world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md`.
    """
    # Stage 1: drive-letter detection independent of host OS Path flavor.
    if PureWindowsPath(value).is_absolute():
        p = Path(value)
        if p.is_absolute():
            return p  # Windows host: WindowsPath, already absolute (f1a646c)
        # POSIX/MSYS host — the residual gap f1a646c documented but did not
        # close: Path("C:/...") is a RELATIVE PosixPath here. Returning it
        # raw lets a downstream .mkdir(parents=True)/open() cwd-anchor it,
        # producing the C<U+F03A>/Users/.../<WORLD_DIR> cruft mirror. Force
        # it absolute so a bad-host write fails loudly at the filesystem
        # root instead of silently mirroring under cwd.
        return Path("/") / value
    # Stage 2: host-absolute check + project_root-anchored fallback.
    p = Path(value)
    if not p.is_absolute():
        p = (project_root / value).resolve()
    return p


def looks_like_cruft(p: Path) -> bool:
    """Return True when a Path matches the  cruft shape.

    The canonical cruft shape has drive-letter content (`:` or the
    Windows private-use substitute U+F03A) appearing in a non-root
    position. Two shapes this catches:
      - /home/user/repo/C:/Users/foo (POSIX-flavored resolve() anchoring
        a relative PosixPath under cwd)
      - C:/repo/C\\uF03A/Users/foo   (Windows after the OS rewrites
        invalid `:` to U+F03A when creating the dir)

    A legitimate Windows absolute path (`C:/Users/foo`) returns False
    because the drive-letter prefix is stripped before the scan.

    Use as a tripwire at write boundaries (e.g., save_history,
    locked_append_jsonl) to detect new bypass call sites that didn't
    route through `absolutize`.
    """
    s = str(p).replace("\\", "/")
    # Strip legitimate Windows drive-letter prefix so `C:/foo` doesn't
    # self-flag. The check is "any `:` REMAINING after the drive."
    if len(s) >= 3 and s[1] == ":" and s[2] == "/":
        s = s[2:]
    return ":" in s or "\uF03A" in s
