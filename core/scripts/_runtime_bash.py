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
        # Prefer Git\bin\bash.exe (login-launcher) over Git\usr\bin\bash.exe
        # (raw MSYS binary). rb-1472 (2026-06-06): when spawned by a
        # Windows-process parent whose PATH lacks Git's usr/bin (daemon started
        # without a shell env; a script run from cmd.exe/PowerShell), the raw
        # usr/bin/bash.exe does NOT self-configure its PATH, so coreutils
        # (dirname, sed, tr) are "command not found" and any wrapper computing
        # SCRIPT_DIR via $(cd "$(dirname …)" && pwd) mis-resolves -> nested
        # `bash $SCRIPT_DIR/sub.sh` rc=127. bin/bash.exe sources the MSYS
        # profile and rebuilds PATH, so coreutils always resolve. Matches the
        # _bash_helpers.py and infra-health.py (gold-standard) ordering.
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ):
            if Path(candidate).exists():
                return candidate
    return shutil.which("bash") or "bash"


# Computed once at import — identical to the MIND_SHELL convention _paths.sh
# exports. Import this constant rather than calling resolve_bash() per call.
BASH = resolve_bash()


# --- win32 argv-corruption guard (, guard-5633) -------------------
# On Windows the MSYS runtime re-processes the raw command line (quote handling
# plus glob/brace expansion) before handing argv to the program. Python's
# subprocess.list2cmdline only wraps an argument in quotes when it contains
# WHITESPACE, so a whitespace-free argument carrying quotes or braces reaches
# that re-processing unprotected and is silently altered.
#
# Two distinct failure modes, both measured 2026-08-31 with an argv-echo script:
#   quotes  -> argv TRUNCATION. '{"title":"probe"}' collapsed argc to 1-2 and
#              EVERY FOLLOWING ARGUMENT WAS LOST.
#   braces  -> value MANGLING, argc usually preserved: '{a:b}' arrives as
#              'a:b', 'a{b}c' as 'abc'; and '{a,b}' brace-EXPANDS into TWO
#              arguments, so argc GROWS.
#
# What makes this worth refusing rather than logging: the program then reports
# a DIFFERENT argument as missing than the one that was corrupted (observed:
# "ERROR: Missing --reason" when --inject-goal was the mangled flag), and the
# same command run by hand succeeds because an interactive shell quotes for
# itself. Hand-works/harness-fails plus a wrong-flag error is the fingerprint,
# and it cost a full investigation before the mechanism was found.
#
# Predicate measured over 25 shapes: 0 false positives, 0 false negatives.
# Whitespace is protective for BOTH classes (Python quotes those), so it is
# checked first. Backslash, backtick, $VAR, ; | & ( ) > and a non-matching *
# all survive unquoted and are deliberately NOT flagged -- flagging them would
# be the over-broad predicate guard-2860 warns against.
_WIN_ARG_UNSAFE = "\"'{}"


def _win_arg_corrupts(value: str) -> bool:
    """True when Windows will silently alter this argument in transit."""
    if any(ch.isspace() for ch in value):
        return False  # list2cmdline quotes it; measured safe for both classes
    return any(ch in value for ch in _WIN_ARG_UNSAFE)


def bash_cmd(script, *args) -> "list[str]":
    """Build a ``subprocess.run`` argv that invokes a ``.sh`` wrapper safely.

    Prepends the resolved ``BASH`` (guard-580 — never the System32 WSL stub)
    and passes the script path via ``Path(script).as_posix()`` (guard-581 —
    ``str(WindowsPath)`` yields backslash separators that bash treats as escape
    introducers and strips, silently producing a nonexistent path). Extra args
    are stringified and appended verbatim.

    On win32 ONLY, raises ``ValueError`` for an argument Windows would corrupt
    in transit (see the guard above). This is a no-op on Linux/macOS, where the
    argv list is delivered to execve untouched. The script path itself is not
    checked — ``as_posix()`` output is the established contract (guard-581).

    The caller keeps its own ``subprocess.run`` kwargs (``capture_output``,
    ``text``, ``input``, ``cwd``, ``env``, ``timeout``, …).
    """
    argv = [str(a) for a in args]
    if sys.platform == "win32" and not os.environ.get("MIND_BASH_ALLOW_UNSAFE_ARGS"):
        for i, value in enumerate(argv):
            if not _win_arg_corrupts(value):
                continue
            bad = "".join(sorted({c for c in value if c in _WIN_ARG_UNSAFE}))
            shown = value if len(value) <= 120 else value[:117] + "..."
            mode = ("TRUNCATE argv — this and EVERY FOLLOWING argument are lost"
                    if ('"' in value or "'" in value)
                    else "MANGLE this value (braces are stripped; {a,b} expands into extra args)")
            raise ValueError(
                "bash_cmd: argument %d would be silently corrupted by Windows and "
                "was REFUSED rather than passed.\n"
                "  argument : %r\n"
                "  offending: %s   (quote/brace characters, with no whitespace in the value)\n"
                "  effect   : Windows would %s\n"
                "\n"
                "  WHY YOU ARE SEEING THIS RATHER THAN A CONFUSING ERROR LATER:\n"
                "    Unrefused, the program receives different arguments than you passed and\n"
                "    blames the WRONG ONE — e.g. reporting a missing --reason when a JSON\n"
                "    --flag value was the corrupted argument. The same command run by hand\n"
                "    works, because an interactive shell quotes for itself.\n"
                "\n"
                "  FIX (in preference order):\n"
                "    1. Pass the payload on STDIN — subprocess.run(..., input=payload).\n"
                "       This is already the house convention for JSON (guard-2037).\n"
                "    2. Write it to a temp file and pass the PATH.\n"
                "    3. If the value is genuinely meant to be shell-expanded, set\n"
                "       MIND_BASH_ALLOW_UNSAFE_ARGS=1 to accept the corruption knowingly.\n"
                "\n"
                "  Do NOT 'fix' this by adding a space to the value — that changes the data.\n"
                "  Detail: guard-5633, g-115-8409."
                % (i + 1, shown, bad, mode)
            )
    return [BASH, Path(script).as_posix(), *argv]
