#!/usr/bin/env bash
# domain-leak-exempt: git merge-driver wrapper for the AyoAI narrative daily
# journal; the python-driver name is a real repo path, not a domain example.
#
# Thin PY-detection wrapper for the merge=ayoai-journal-md git driver so it runs
# on both Linux (python3) and git-for-windows (py -3) — mirrors the OS switch in
# core/scripts/git-merge-ayoai-ledger.sh. Git invokes this with its %O %A %B %P
# placeholders (see git-merge-journal-md.py header); this wrapper passes them
# straight through. Registered per-clone by install-git-hooks.sh (git config is
# NOT version-controlled). (g-115-3425)
set -euo pipefail

case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*) PY="py -3" ;;
    *)                    PY="python3" ;;
esac

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Hand python a native path (g-115-10806). git spawns this driver in its
# caller's env, and iteration-push.sh inherits MSYS_NO_PATHCONV=1 from
# _platform.sh, so MSYS no longer rewrites the /c/... pwd form and Windows
# python opens C:\c\... (rc=2, which git reads as a conflict). No cygpath off
# Windows: the path passes through unchanged.
if command -v cygpath >/dev/null 2>&1; then
    _HERE_NATIVE="$(cygpath -w "$_HERE")"
else
    _HERE_NATIVE="$_HERE"
fi
exec $PY "$_HERE_NATIVE/git-merge-journal-md.py" "$@"
