#!/usr/bin/env bash
# domain-leak-exempt: git merge-driver wrapper for AyoAI agent ledgers; the
# python-driver name is a real repo path, not a domain example.
#
# Thin PY-detection wrapper for the merge=ayoai-ledger git driver so it runs on
# both Linux (python3) and git-for-windows (py -3) — mirrors the OS switch in
# core/githooks/pre-push / pre-commit. Git invokes this with its %O %A %B %P
# placeholders (see git-merge-ayoai-ledger.py header); this wrapper passes them
# straight through. Registered per-clone by install-git-hooks.sh (git config is
# NOT version-controlled). (g-115-2767)
set -euo pipefail

case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*) PY="py -3" ;;
    *)                    PY="python3" ;;
esac

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec $PY "$_HERE/git-merge-ayoai-ledger.py" "$@"
