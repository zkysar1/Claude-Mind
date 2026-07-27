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

# Ensure the tracked hooks are executable in this working tree. core.hooksPath
# points DIRECTLY at the tracked dir (not a copy in .git/hooks), so git uses the
# working-tree exec bit to decide whether to run each hook — a non-exec bit makes
# git SILENTLY SKIP the hook. Root cause of the 2026-05-15..2026-07-03 pre-commit
# dormancy (): pre-commit was created at mode 100644 (3f93a4ef) and this
# installer never ensured executability, so all 11 Layer-B gates were bypassed
# fleet-wide for ~7 weeks. Idempotent (only chmods a non-exec hook) + fail-open
# (a chmod hiccup must not block session start — guarded, never trips set -e).
for _hook in pre-commit pre-push post-commit; do
    _hp="$REPO_ROOT/core/githooks/$_hook"
    if [ -f "$_hp" ] && [ ! -x "$_hp" ]; then
        if chmod +x "$_hp" 2>/dev/null; then
            echo "[install-git-hooks] restored +x on core/githooks/$_hook (was non-exec — git skips non-exec hooks)" >&2
        fi
    fi
done

# Record-level merge driver for the RMW agent ledgers (merge=ayoai-ledger in
# .gitattributes). git driver config lives in .git/config, which — like
# .git/hooks and unlike core.hooksPath's target — is NOT version-controlled, so
# this tracked installer is the cross-clone registration mechanism. The driver
# resolves cross-box iteration-push.sh conflicts on experience / changelog /
# experience-meta / journal / aspirations ledgers by record-level commutative
# union instead of aborting (). Idempotent (only writes on drift) +
# fail-open (a config hiccup must not block session start).
_LEDGER_DRIVER='bash core/scripts/git-merge-ayoai-ledger.sh %O %A %B %P'
_CUR_LEDGER_DRIVER="$(git config --local --get merge.ayoai-ledger.driver 2>/dev/null || echo "")"
if [ "$_CUR_LEDGER_DRIVER" != "$_LEDGER_DRIVER" ]; then
    git config --local merge.ayoai-ledger.name \
        "the framework record-level agent-ledger merge (commutative; reuses coordination_merge)" 2>/dev/null || true
    if git config --local merge.ayoai-ledger.driver "$_LEDGER_DRIVER" 2>/dev/null; then
        echo "[install-git-hooks] merge.ayoai-ledger driver registered (cross-box ledger conflicts now self-heal)" >&2
    else
        echo "[install-git-hooks] WARN: could not register merge.ayoai-ledger driver (non-fatal; ledger conflicts fall back to manual union)" >&2
    fi
fi

exit 0
