#!/usr/bin/env bash
# _python_launcher.sh — rt_python_launcher and rt_python_launcher_into, with
# no other side effects.
#
# Split out of _runtime.sh () so a script can resolve its python
# launcher without sourcing _runtime.sh, which also sources _paths.sh and sets
# RT_* state. Sourcing this file only defines the functions; call one:
#
#     source "$SCRIPT_DIR/_python_launcher.sh"
#     PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3
#     $PYLAUNCH -c '...'          # unquoted: it is two words on Windows
#
# On a hot path (a hook, or anything run on every tool call), set the variable
# directly. Same result, without the substitution's subshell ():
#
#     rt_python_launcher_into PYLAUNCH || PYLAUNCH=python3
#
# The `|| PYLAUNCH=python3` fallback is for inline calls (guard-1098). rt_spawn
# does not take it, because the daemon must never start under python3 on
# Windows (, see below).

# rt_python_launcher_into VAR — set VAR to the right python launcher for the
# current OS. Single source of truth for Windows-vs-POSIX launcher selection.
# On Windows Git Bash, `python3` may resolve to the Microsoft Store stub;
# `py -3` is the reliable entry point when available.
# VAR becomes "py -3" on a Windows shell with py and "python3" on any other
# shell (rc 0). On a Windows shell without py it becomes "" and rc is 1, the
# value `$(rt_python_launcher)` would have given. No locals: printf -v assigns
# through bash's dynamic scope, so a local here would shadow a caller's
# variable of the same name.
rt_python_launcher_into() {
    # $OSTYPE, not `uname -s`: bash sets it at startup, so there is no fork.
    # The uname form cost ~217 ms per call on Git Bash (measured 2026-09-29),
    # and this runs at the top of every script that calls python. Same
    # classes as the _paths.sh shim gate: msys = Git Bash/MSYS2, cygwin.
    case "${OSTYPE:-}" in
        msys*|cygwin*)
            if command -v py >/dev/null 2>&1; then
                printf -v "$1" '%s' "py -3"
                return
            fi
            #  v3: never fall back to bare `python3` on Windows.
            # Git-bash python3 is frequently POSIX/MSYS-flavored; under it
            # _path_helpers.absolutize() returns a RELATIVE WORLD_DIR
            # (Path("C:/...") is a relative PosixPath there) and the daemon
            # mkdir-mirrors C<U+F03A>/Users/.../<WORLD_DIR> at cwd then
            # crash-loops. Fail loud — the caller logs + aborts the spawn.
            printf -v "$1" '%s' ""
            return 1
            ;;
    esac
    printf -v "$1" '%s' "python3"
}

# rt_python_launcher — print the launcher rt_python_launcher_into selects, for
# callers that capture it with $(...). Used by rt_spawn and by wrappers that
# need a short inline python call (e.g. JSON parse on response handling).
rt_python_launcher() {
    local _rt_python_launcher
    rt_python_launcher_into _rt_python_launcher || return 1
    echo "$_rt_python_launcher"
}
