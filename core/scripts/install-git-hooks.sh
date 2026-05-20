#!/usr/bin/env bash
# Idempotent installer for the repo's git hooks.
#
# Points git at the tracked hooks dir (core/githooks) via core.hooksPath, so
# the Layer B pre-commit gate (daemon-only drift defense) propagates to every
# clone without a per-clone manual step. .git/hooks/ is NOT version-controlled,
# which is why a tracked dir + this idempotent installer is the cross-clone
# mechanism. Invoked from sessionstart-orchestrator.sh Step 0.5 (so alpha /
# bravo / zeta auto-install on next session) and safe to run by hand or in CI.
#
# Idempotent: no-op when already set correctly. Fail-open: never blocks the
# caller — a hook-install hiccup must not prevent a session from starting.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$REPO_ROOT"

WANT="core/githooks"
CUR="$(git config --local --get core.hooksPath 2>/dev/null || echo "")"

if [ "$CUR" != "$WANT" ]; then
    if git config --local core.hooksPath "$WANT" 2>/dev/null; then
        echo "[install-git-hooks] core.hooksPath -> $WANT (Layer B pre-commit gate now active)" >&2
    else
        echo "[install-git-hooks] WARN: could not set core.hooksPath (non-fatal; gate inactive this clone)" >&2
    fi
fi

exit 0
