"""Shared test helpers for bash subprocess invocation under Windows.

Consolidates the `_resolve_bash` helper previously duplicated across six
test files (g-115-725, 2026-05-16). The bare `bash` name on Windows PATH
can resolve to **WSL bash** (`C:\\Windows\\System32\\bash.exe`) when
invoked from `subprocess.run([...])`. WSL bash:

  - Sees the repo under `/mnt/c/...`, breaking absolute Windows paths.
  - Strips Windows environment variables that `_paths.sh` sources.
  - Cannot exec `python3` against Windows-side scripts in finite time
    (observed: `subprocess.run` hangs past the timeout).

The canonical resolution priority mirrors `_paths.sh`'s own
`bash = os.environ.get("MIND_SHELL")` lookup:

  1. `MIND_SHELL` env var (set by `_paths.sh` on Windows when sourced
     from Git Bash) — honored unconditionally if the file exists.
  2. Platform-probe (`sys.platform == "win32"`): Git Bash candidate paths
     in order — `Program Files\\Git\\usr\\bin`, `Program Files\\Git\\bin`,
     `Program Files (x86)\\Git\\usr\\bin`.
  3. `shutil.which("bash")` or the literal `"bash"` — system default.

Usage (module level — `BASH` is computed once at import):

    from _bash_helpers import BASH
    subprocess.run([BASH, "core/scripts/foo.sh"], capture_output=True, text=True)

Or call the function directly for case-specific decisions:

    from _bash_helpers import resolve_bash
    git_bash = resolve_bash()

The import requires that `core/scripts/tests/` is on `sys.path`. The
conftest.py in this directory establishes that for all collected tests.
For ad-hoc invocations (running a test file directly via `py -3 file.py`),
the test file itself must add `SCRIPT_DIR` to `sys.path` BEFORE the
import — the 6 refactored tests already do this for `core/scripts/`,
so adding `SCRIPT_DIR` is a one-liner.

See also:
  - `_paths.sh` `bash = os.environ.get("MIND_SHELL")` — the source of
    the env-var convention this helper honors.
  - `core/scripts/verify-pseudocode-scripts.py:32` — a near-duplicate
    `_resolve_bash` in non-test CLI code, intentionally NOT consolidated
    here (test helpers shouldn't be imported by CLI tools); a separate
    follow-up may extract it to a shared non-test location.
  - g-115-725 (this consolidation) and rb-919 (Windows bash test
    harness lesson — referenced by the goal description; the rb-919
    title is actually about prefix-validated approval ids, so the
    cite appears to be a misattribution in the goal description).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path


_WINPATH_CALL = re.compile(r'\$\(_winpath "([^"]*)"\)')


def resolve_winpath(src: str) -> str:
    """Rewrite `$(_winpath "X")` back to `X` in shell source before asserting.

    Structural tests over `core/scripts/*.sh` pin CALL SHAPES by searching the
    source for a literal (`python3 "$SCRIPT_DIR/foo.py" --goal "$GOAL_ID"`).
    g-115-8706 routed every `python3 <file-arg>` through the `_winpath` cygpath
    helper, so those literals stopped appearing verbatim -- and three tests went
    red while the property each pinned (foo.py is invoked, once, with those
    args) stayed perfectly true (g-115-8995).

    THIS IS A NORMALIZER, NOT A LOOSENED PATTERN, and the distinction is the
    reason it exists (rb-9927): widening each assertion until it matches
    `$(_winpath ...)` would re-pin the expression SHAPE at lower resolution and
    go red again on the next path-routing refactor -- which is how a guard test
    earns a reputation for false alarms and gets deleted. Resolving the helper
    first is what the shell itself does at runtime, so every other byte of the
    call stays pinned at full resolution.

    Deliberately NARROW: only the double-quoted single-argument form this file's
    callers use. An unrecognised form is left ALONE, so the assertion still
    fails rather than silently matching something else.
    """
    return _WINPATH_CALL.sub(r"\1", src)


def resolve_bash() -> str:
    """Return the path to a bash executable safe for Windows subprocess use.

    See module docstring for resolution priority and rationale.
    """
    explicit = os.environ.get("MIND_SHELL")
    if explicit and Path(explicit).exists():
        return explicit
    if sys.platform == "win32":
        # Prefer Git\bin\bash.exe (the login-launcher) over Git\usr\bin\bash.exe
        # (the raw MSYS binary). EMPIRICAL ROOT CAUSE (2026-06-06, rb-1472):
        # when bash.exe is spawned by a Windows-process parent whose PATH lacks
        # Git's usr/bin (e.g. pytest launched from cmd.exe / PowerShell rather
        # than Git Bash), the RAW usr/bin/bash.exe does NOT self-configure its
        # PATH, so coreutils (dirname, sed, tr, head) read as "command not
        # found". Any wrapper computing SCRIPT_DIR via
        # `$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)` then mis-resolves to
        # cwd, and the nested `bash "$SCRIPT_DIR/sub.sh"` fails rc=127
        # ("No such file") — the seed-preflight / test_promote failure class.
        # bin/bash.exe sources the MSYS profile and rebuilds PATH, so coreutils
        # are always found and the chain works regardless of the parent env.
        # Verified end-to-end: real seed-preflight.sh under a clean
        # System32-first PATH -> usr/bin rc=2 (dirname not found), bin rc=0
        # (PUBLISHABLE). This supersedes rb-1468's incorrect
        # "non-login MSYS can't open absolute paths" theory (which is why the
        # three path-string-normalization fix attempts all failed — the lever
        # is the bash BINARY, not the path form). Linux skips this whole block
        # (sys.platform != "win32") and falls through to shutil.which, so the
        # reorder is a no-op off Windows.
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ):
            if Path(candidate).exists():
                return candidate
    return shutil.which("bash") or "bash"


# Convenience: module-level cache. Tests that just need a single BASH
# string at import time can `from _bash_helpers import BASH` without
# calling `resolve_bash()` themselves. Tests that need to re-resolve
# (e.g., after mutating `MIND_SHELL` mid-session) should call
# `resolve_bash()` directly each time.
BASH = resolve_bash()
