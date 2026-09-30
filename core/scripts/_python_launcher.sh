#!/usr/bin/env bash
# _python_launcher.sh — rt_python_launcher, with no other side effects.
#
# Split out of _runtime.sh () so a script can resolve its python
# launcher without sourcing _runtime.sh, which also sources _paths.sh and sets
# RT_* state. Sourcing this file only defines the function; call it:
#
#     source "$SCRIPT_DIR/_python_launcher.sh"
#     PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3
#     $PYLAUNCH -c '...'          # unquoted: it is two words on Windows
#
# The `|| PYLAUNCH=python3` fallback is for inline calls (guard-1098). rt_spawn
# does not take it, because the daemon must never start under python3 on
# Windows (, see below).

# rt_python_launcher — print the right python launcher for the current OS.
# Single source of truth for Windows-vs-POSIX launcher selection. On Windows
# Git Bash, `python3` may resolve to the Microsoft Store stub; `py -3` is
# the reliable entry point when available. Used by rt_spawn and by wrappers
# that need a short inline python call (e.g. JSON parse on response handling).
rt_python_launcher() {
    # $OSTYPE, not `uname -s`: bash sets it at startup, so there is no fork.
    # The uname form cost ~217 ms per call on Git Bash (measured 2026-09-29),
    # and this runs at the top of every script that calls python. Same
    # classes as the _paths.sh shim gate: msys = Git Bash/MSYS2, cygwin.
    case "${OSTYPE:-}" in
        msys*|cygwin*)
            if command -v py >/dev/null 2>&1; then
                echo "py -3"
                return 0
            fi
            #  v3: never fall back to bare `python3` on Windows.
            # Git-bash python3 is frequently POSIX/MSYS-flavored; under it
            # _path_helpers.absolutize() returns a RELATIVE WORLD_DIR
            # (Path("C:/...") is a relative PosixPath there) and the daemon
            # mkdir-mirrors C<U+F03A>/Users/.../<WORLD_DIR> at cwd then
            # crash-loops. Fail loud — the caller logs + aborts the spawn.
            return 1
            ;;
    esac
    echo "python3"
}
