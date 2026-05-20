"""Shared stdio reconfiguration helper ().

Module name `_stdio` (NOT `_io`) — the stdlib has a built-in C module
named `_io` that takes precedence over any local file with the same name,
so a `from _io import reconfigure_stdio` would fail with ImportError
(unknown location). Do NOT rename this module to `_io`.

Forces utf-8 on stdin / stdout / stderr regardless of caller environment.
Defends against the Windows cp1252 fallback that produces UTF-8 mojibake
when scripts ingest text from PowerShell pipelines or subprocess output
without explicit encoding hints.

The companion `core/scripts/_platform.sh` exports `PYTHONIOENCODING=utf-8`
when sourced by .sh wrappers — but bypass paths exist (heredoc Python,
direct `py -3 -c`, hooks that drop env, subprocess.run with env=...
forgetting to copy PYTHONIOENCODING). Calling reconfigure_stdio() at
script entry layers an explicit defense over the env shim.

See alpha/reports/mojibake-writer-audit-2026-05-07.md for the audit
that motivated this. See core/scripts/_fileops.py FILEOPS_SURROGATE_GATE
for the write-time safety net.

Usage at script entry:
    from _io import reconfigure_stdio
    reconfigure_stdio()
"""
import sys


def reconfigure_stdio():
    """Force utf-8 + errors='replace' on stdin / stdout / stderr.

    Idempotent. Safe to call when streams are already configured
    (e.g., when PYTHONIOENCODING=utf-8 was set by _platform.sh) — the
    reconfigure() method swaps the current TextIOWrapper for an
    equivalent one with the new encoding; subsequent calls do not
    accumulate state.

    Streams that don't expose .reconfigure() (test harnesses replacing
    sys.stdin with a StringIO, embedded interpreters, etc.) are
    silently skipped — they are typically already unicode-aware.

    errors='replace' is chosen over 'strict' because a single corrupted
    byte from upstream should not crash the script — let the FILEOPS
    surrogate gate at write-time fail loud with a structured message
    instead.
    """
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
