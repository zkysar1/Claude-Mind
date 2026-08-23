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
# commit-msg joined the chain 2026-08-18 (, hot-path size budget) —
# it must be listed here or a mode-100644 checkout silently skips it too.
for _hook in pre-commit pre-push post-commit post-merge commit-msg; do
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

# Section-level merge driver for the NARRATIVE daily journal
# (merge=ayoai-journal-md in .gitattributes). Same registration mechanism and
# rationale as the ledger driver above. The narrative daily .md had NO merge
# routing at all, so two boxes running one agent identity both created the same
# agents/<agent>/journal/<yyyy>/<mm>/<yyyy-mm-dd>.md with no common ancestor —
# a guaranteed add/add that wedged iteration-push EVERY calendar day and
# stranded unrelated ledger work with it (, from ). NOT
# merge=union: that path family has 1980 historical deleted content lines, so it
# fails .gitattributes' zero-deleted-lines evidence gate. Idempotent + fail-open.
_JOURNAL_MD_DRIVER='bash core/scripts/git-merge-journal-md.sh %O %A %B %P'
_CUR_JOURNAL_MD_DRIVER="$(git config --local --get merge.ayoai-journal-md.driver 2>/dev/null || echo "")"
if [ "$_CUR_JOURNAL_MD_DRIVER" != "$_JOURNAL_MD_DRIVER" ]; then
    git config --local merge.ayoai-journal-md.name \
        "the framework section-level narrative-journal merge (unions by ## heading; same-heading divergence still conflicts)" 2>/dev/null || true
    if git config --local merge.ayoai-journal-md.driver "$_JOURNAL_MD_DRIVER" 2>/dev/null; then
        echo "[install-git-hooks] merge.ayoai-journal-md driver registered (cross-box daily-journal add/add now self-heals)" >&2
    else
        echo "[install-git-hooks] WARN: could not register merge.ayoai-journal-md driver (non-fatal; journal conflicts fall back to manual union)" >&2
    fi
fi

# REGISTERING THE DRIVER IS NOT THE SAME AS THE DRIVER BEING USED ().
# Everything above writes .git/config. But .git/info/attributes -- per-clone and
# UNTRACKED, so invisible to git status, ls-files and every review -- OUTRANKS
# the tracked .gitattributes. One `agents/<agent>/*.jsonl merge=union` line there
# silently reverts the whole  migration for that agent while every
# check above still reports success. Union "resurrects pruned/archived/edited
# records", which on aspirations.jsonl means completed goals returning on a
# cross-box merge.
# check-merge-driver-drift.sh detects exactly this and is the reason the class
# was found twice -- but until now it had NO CALLER, so it was run only when
# someone already suspected a problem. That is why the identical override
# survived on the ZDS clone for 11 days AFTER the same defect was found and
# fixed on Ayoai (, 2026-07-26): the detector was correct, complete,
# and never invoked. A sweep with no call site is indistinguishable from a sweep
# that always returns clean.
# Wired HERE because this script already runs at every session start (via
# sessionstart-orchestrator.sh) and already owns driver registration -- so the
# check lands at the one moment the answer can still change before any merge
# runs. Advisory and fail-open, matching the posture above: it prints and never
# blocks, because a false positive must not be able to stop a session from
# starting. Backgrounding it would defeat the point (the output must reach the
# session that is about to merge), so it is bounded instead.
if [ -f core/scripts/check-merge-driver-drift.sh ]; then
    timeout 20 bash core/scripts/check-merge-driver-drift.sh >&2 2>&1 || true
fi

exit 0
