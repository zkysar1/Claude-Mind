"""Executable-bit resolution shared by the promotion preflight and the seed plant.

WHY THIS MODULE EXISTS (g-360-13)
---------------------------------
The exec bit fails in the SILENT direction: a git hook or wrapper that loses
u+x does not error, it simply never runs, so a downstream Mind adopting a tag
runs with its commit gates absent and nothing red anywhere.

Detection landed twice before preservation did -- g-360-07 (source-index
mode-strip block) and g-360-09 (preflight exec_bits / mode_differing fields)
both DETECT a strip; neither made the plant PRESERVE the bit. Measured on
v2.12.5: staging held 0 x 100755 and all 628 source-executable paths came out
100644, restored by hand as a757343.

These three helpers were authored in promotion-preflight.py by g-360-09 and are
hosted here so the plant consumes the SAME logic rather than a second copy
(communication-clarity rule 5 -- one source of truth, no parallel drift).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def exec_bits(p: Path) -> int | None:
    """The EXECUTE bits of a file (0o111 mask), or None if unreadable.

    Only the execute bits are compared. Read/write bits track each box's umask
    and would make mode drift a pure noise generator; the execute bit is the one
    that changes BEHAVIOUR, and it fails in the silent direction (g-360-09).
    """
    try:
        return p.stat().st_mode & 0o111
    except OSError:
        return None


def index_exec_map(repo_root: Path) -> dict[str, bool]:
    """rel-path -> is-executable, read from git's INDEX (`git ls-tree -r HEAD`).

    THE INDEX MODE IS THE ONE THAT PROPAGATES (g-360-07, guard-844). A promotion
    commits what the index says; `chmod +x` alone does not travel. The filesystem
    mode is also the dimension that DISAPPEARS on a checkout whose mount flattens
    permissions -- precisely the boxes where exec_bits() reports "cannot see" and
    the whole check goes blind. Reading the index restores the signal there.

    Returns {} on any failure (not a git repo, git absent, detached/empty HEAD).
    An empty map is a DECLINE, not a clean result: callers fall back to
    exec_bits() per file and must never read {} as "no drift".
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-tree", "-r", "HEAD"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return {}
    if out.returncode != 0:
        return {}
    m: dict[str, bool] = {}
    for line in out.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        fields = parts[0].split()
        if not fields:
            continue
        # 100755 = executable blob, 100644 = regular. Anything else (120000
        # symlink, 160000 gitlink) is not a mode this check reasons about.
        if fields[0] in ("100755", "100644"):
            m[parts[1].replace("\\", "/")] = fields[0] == "100755"
    return m


def resolve_exec(rel: str, abs_path: Path, idx: dict[str, bool]) -> tuple[bool | None, str]:
    """(is_executable, source) preferring the INDEX, falling back to the filesystem.

    source is "index" or "fs" so a reader can tell which dimension produced a
    verdict -- a file staged-but-not-in-HEAD legitimately has no index entry and
    must not be silently treated as non-executable (that would invent a
    regression out of an absence).
    """
    if rel in idx:
        return idx[rel], "index"
    bits = exec_bits(abs_path)
    return (None if bits is None else bool(bits)), "fs"


def carry_exec_bit(rel: str, src: Path, dst: Path, idx: dict[str, bool]) -> bool:
    """Apply the SOURCE's executable bit to a freshly-WRITTEN dest file.

    ADD-ONLY, and that asymmetry is the whole safety argument. resolve_exec
    returns None when neither dimension can see the bit; `not None` is falsey so
    an unknown leaves the destination exactly as written. A false negative
    therefore reproduces the pre-fix status quo (0644, recoverable by the same
    hand chmod that shipped before), while a blind two-way sync could STRIP a
    bit the destination legitimately holds -- the silent-failure direction this
    module exists to close.

    Returns True only when a bit was actually added.
    """
    is_exec, _source = resolve_exec(rel, src, idx)
    if not is_exec:
        return False
    try:
        mode = dst.stat().st_mode
        if mode & 0o111 == 0o111:
            return False
        dst.chmod(mode | 0o111)
        return True
    except OSError:
        return False
