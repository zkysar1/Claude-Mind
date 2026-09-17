#!/usr/bin/env bash
# Thin PY-detection wrapper for the merge=ayoai-append-ledger git driver so it
# runs on both Linux (python3) and git-for-windows (py -3) — mirrors
# core/scripts/git-merge-journal-md.sh. Git invokes this with its %O %A %B %P
# placeholders (see git-merge-append-ledger.py header); this wrapper passes them
# straight through. Registered per-clone by install-git-hooks.sh (git config is
# NOT version-controlled). ()
set -euo pipefail

case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*) PY="py -3" ;;
    *)                    PY="python3" ;;
esac

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec $PY "$_HERE/git-merge-append-ledger.py" "$@"
