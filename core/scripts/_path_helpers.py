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
import os
import re
from pathlib import Path, PureWindowsPath

# MSYS/Git-Bash drive form: a SINGLE-letter first segment (`/c`, `/c/rest`).
# A multi-character first segment (`/home/...`, `/cygdrive/c/...`) is not this
# shape and is deliberately left alone.
_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(?:/(.*))?$")


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


def normalize_msys_path(value: str, *, is_windows=None, exists=None) -> str:
    """On a Windows host, translate an MSYS/Git-Bash `/c/...` path to `C:/...`.

    The exact mirror of the bug `absolutize` defends against. There, the host
    is POSIX and the VALUE is Windows-flavored. Here the host is Windows and
    the VALUE is MSYS-flavored — and it fails SILENTLY, because Windows Python
    reads a leading `/` as absolute-on-the-current-drive: `Path("/c/W/x.jsonl")`
    resolves to `C:\\c\\W\\x.jsonl`, so `.is_file()` returns False for a file
    that plainly exists and every existence-gated caller takes its not-found
    branch. `_platform.sh` already cygpath-converts the path ENV VARS, which is
    why this looks fixed — but a caller interpolates `$WORLD_DIR` into ARGV
    *before* the callee runs, and `MSYS_NO_PATHCONV=1` (exported by that same
    file) specifically stops MSYS from rewriting argv. So no amount of env
    conversion can reach an argv-delivered path: a callee that accepts a path
    ARGUMENT must normalize it itself. (g-115-4175, measured on ZDS-Mind prod.)

    The conversion is applied ONLY when the converted form actually exists, so
    a legitimate Windows `/c/...` path (which genuinely means `C:\\c\\...`) is
    never clobbered. On non-Windows hosts the value is returned untouched —
    `/c/foo` is an ordinary absolute path on POSIX and rewriting it there would
    turn this defense into a new bug.

    `is_windows` / `exists` are injection seams for TESTS ONLY, and they are
    load-bearing rather than decorative: the branch that matters executes only
    on Windows, while dev and staging are POSIX. Without them this function
    would be verifiable only on the one platform where nobody would notice it
    breaking — which is precisely the asymmetry that produced the bug
    (`.claude/rules/run-full-suite-after-deep-code.md`: treat platform as part
    of the production shape).
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        return value
    m = _MSYS_DRIVE_RE.match(value)
    if not m:
        return value
    converted = "{}:/{}".format(m.group(1).upper(), m.group(2) or "")
    if exists is None:
        def exists(p):
            return Path(p).exists()
    return converted if exists(converted) else value


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


class CruftPathRefused(Exception):
    """Raised by assert_not_cruft when a path looks like cruft mirror.

    The exception name is structured: callers and tests can catch
    CruftPathRefused specifically without swallowing unrelated OSError /
    FileNotFoundError. The raise-not-skip choice is deliberate \u2014 silent
    skip leaves the system in a partial-write state where the file IS
    on disk (cruft mirror) but the daemon thinks the write failed. Loud
    fail surfaces the bypass at the call site so it gets fixed instead
    of accumulating as a noisy log entry no one reads.
    """


def assert_not_cruft(path: Path, operation: str = "write") -> None:
    """Tripwire: refuse to proceed if `path` looks like cruft mirror.

    Use at write boundaries (mkdir, open(w), write_text) in daemon code
    where the path argument is a function parameter and its provenance
    cannot be audited locally. If the path traces back to ctx.paths.*
    (which goes through absolutize()), this is a no-op. If a caller
    constructed the path via raw join of a non-absolutized value, this
    fires.

    Canonical sites: every mkdir(parents=True) in mind_api/src/
    (g-315-77 audit, 2026-05-21). Adding to a new daemon write helper
    is the standard hardening pattern \u2014 see  the audit at
    core/scripts/tests/test_daemon_mkdir_cruft_tripwires.py for the
    canonical site list + test coverage.
    """
    if looks_like_cruft(path):
        raise CruftPathRefused(
            f"Refusing {operation}: path looks like cruft mirror \u2014 "
            f"{path!r}. Caller bypassed absolutize() somewhere upstream. "
            f"See .claude/rules/path-resolution.md \"L1 Cruft Prevention\"."
        )
