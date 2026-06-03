#!/usr/bin/env python3
"""Shared bash resolver for Python scripts that shell out to .sh wrappers.

On Windows, ``subprocess.run(['bash', '<script.sh>'])`` resolves to the WSL
bash stub in ``C:\\Windows\\System32`` — CreateProcess's search order hits
SYSTEM32 before PATH, so the WSL stub wins even though ``shutil.which('bash')``
reports Git Bash. WSL bash reads repo paths under ``/mnt/c/`` and fails with
rc=127 on a Windows-side ``C:/...`` script path. The failure is silent at most
callsites (``except Exception: pass`` or no rc check), manufacturing false
"success" reports.

This module pins a usable bash once at import (preferring ``MIND_SHELL``, then
Git Bash) and exposes it as the ``BASH`` constant. ``bash_cmd()`` builds the
argv for ``subprocess.run`` and ALSO enforces the ``.as_posix()`` script-path
convention so callers never pass a ``str(WindowsPath)`` whose backslashes bash
silently strips.

Patterns:
    from _runtime_bash import BASH, bash_cmd
    subprocess.run(bash_cmd("core/scripts/world-cat.sh", "file.json"),
                   capture_output=True, text=True)
    # or, when constructing argv by hand:
    subprocess.run([BASH, Path(script).as_posix(), *args], ...)

Lineage: extracted from verify-pseudocode-scripts.py:_resolve_bash (g-115-900);
audit matrix agents/zeta/reports/g-115-863-bash-subprocess-audit.md; root cause
g-115-789; rb-577 (two-layer defense); guard-580 (resolve to Git Bash, not WSL);
guard-581 (.as_posix() script paths). Upstream half of the fix is the
MIND_SHELL auto-detect in core/scripts/_paths.sh:133-150 — this is the
downstream half for Python-native subprocess callsites that bypass _paths.sh.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resolve_bash() -> str:
    """Return a usable bash, avoiding the System32 WSL stub on Windows.

    Resolution order:
      1. ``MIND_SHELL`` env var (set by ``_paths.sh`` when sourced) if it
         points at an existing file.
      2. Known Git Bash install locations on Windows.
      3. ``shutil.which('bash')`` (honors PATH), then bare ``'bash'``.

    The bare-``'bash'`` fallback is the last resort only; it re-triggers the
    SYSTEM32 search and should never be hit on a configured Windows host.
    """
    env_shell = os.environ.get("MIND_SHELL")
    if env_shell and Path(env_shell).exists():
        return env_shell
    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ):
            if Path(candidate).exists():
                return candidate
    return shutil.which("bash") or "bash"


# Computed once at import — identical to the MIND_SHELL convention _paths.sh
# exports. Import this constant rather than calling resolve_bash() per call.
BASH = resolve_bash()


def bash_cmd(script, *args) -> "list[str]":
    """Build a ``subprocess.run`` argv that invokes a ``.sh`` wrapper safely.

    Prepends the resolved ``BASH`` (guard-580 — never the System32 WSL stub)
    and passes the script path via ``Path(script).as_posix()`` (guard-581 —
    ``str(WindowsPath)`` yields backslash separators that bash treats as escape
    introducers and strips, silently producing a nonexistent path). Extra args
    are stringified and appended verbatim.

    The caller keeps its own ``subprocess.run`` kwargs (``capture_output``,
    ``text``, ``input``, ``cwd``, ``env``, ``timeout``, …).
    """
    return [BASH, Path(script).as_posix(), *(str(a) for a in args)]
