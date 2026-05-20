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
import shutil
import sys
from pathlib import Path


def resolve_bash() -> str:
    """Return the path to a bash executable safe for Windows subprocess use.

    See module docstring for resolution priority and rationale.
    """
    explicit = os.environ.get("MIND_SHELL")
    if explicit and Path(explicit).exists():
        return explicit
    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
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
