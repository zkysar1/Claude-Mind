#!/usr/bin/env bash
# mind-api-code-changed.sh — SINGLE-SOURCE predicate: did the daemon's code
# surface change between <BASE> and HEAD?
#
# The runtime daemon (`python -m mind_api.src`) loads these files into its
# long-lived process:
#   1. everything under mind_api/src/**     (the daemon package itself)
#   2. core/scripts/_*.py                 (underscore-prefixed shared modules
#                                          it imports)
#   3. specific NON-underscore core/scripts modules imported via the sys.path
#                                          insert (file_locks.py et al. prepend
#                                          core/scripts onto sys.path):
#                                            core/scripts/gates/**  (gate evaluators)
#                                            core/scripts/storage_backend.py
#                                            core/scripts/experience.py
#                                            core/scripts/tree.py
#                                            core/scripts/tree_match.py
#                                          The `_` prefix is NOT the boundary —
#                                          these non-underscore modules are
#                                          loaded too. Audited 2026-05-31 against
#                                          the E402/sys.path-resolved import set
#                                          in mind_api/src. If the daemon's
#                                          import surface changes, update BOTH
#                                          this list AND the pathspec below.
#
# A commit that touches only docs, world/, meta/, an agent dir, a *.sh
# wrapper, or a standalone (NON-imported) CLI cannot change daemon behaviour —
# the running process is still current. Recycling it then is pure churn.
#
# Two callers share THIS one predicate (do NOT inline the pathspec into
# either — the boundary lives here and only here):
#   - core/githooks/post-commit             BASE = HEAD~1
#       restart-on-commit trigger (recycle only when the commit touched
#       daemon code).
#   - core/scripts/_runtime.sh rt_check_staleness   BASE = daemon's running
#       /v1/admin/health git_head_sha
#       narrows the  stale-code auto-restart so a docs/world-only
#       commit no longer warns-and-recycles a daemon whose code is current.
#
# Contract:
#   $1 = BASE ref (any git-resolvable commit-ish — a full SHA or HEAD~1)
#   exit 0  → daemon code surface changed in BASE..HEAD (restart warranted)
#   exit 1  → no daemon-surface change (the running daemon is current)
#
# Fail-toward-restart: ANY git error (missing/un-resolvable BASE, not a
# repo, first-commit HEAD~1, GC'd SHA) exits 0. The correctness guarantee
# is "never serve stale daemon code"; a spurious restart is cheap, a missed
# restart serves stale routes/resolvers (rb-711, rb-936, guard-559). When
# we cannot PROVE "unchanged", we restart.

set -uo pipefail

BASE="${1:-}"
if [ -z "$BASE" ]; then
    # No base to diff against → cannot prove "unchanged" → fail toward restart.
    exit 0
fi

# Derive PROJECT_ROOT inline — this predicate needs ONLY the repo root,
# never WORLD/META/AGENT_DIR. Sourcing _paths.sh would also run its
# Windows-shell-auto-detect block (cygpath + uname + command -v subprocess
# chain) and Python-shim setup — ~2 seconds on Windows, paid SYNCHRONOUSLY
# by the post-commit hook before its detached spawn fires. Inline derivation
# is one subshell (~50 ms). The structural assumption (this script lives at
# <repo>/core/scripts/) is the SAME assumption _paths.sh makes; both encode
# it identically with `..` traversals — neither is "more authoritative."
# CRITICAL — TWO `..`s. dirname($0)=core/scripts, /..=core, /../..=<repo>.
# One `..` would give CORE_ROOT, not PROJECT_ROOT, and `git -C <core>` would
# still work (it walks up to find .git) but the pathspec `mind_api/src` would
# resolve relative to the WRONG cwd in some git versions. Don't shorten it.
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GIT=(git -C "$PROJECT_ROOT")

# Resolve BASE to a commit. A bad/unknown/absent ref must fail TOWARD
# restart, never be silently treated as "unchanged".
"${GIT[@]}" rev-parse --verify --quiet "${BASE}^{commit}" >/dev/null 2>&1 || exit 0

# CRITICAL — the pathspec below IS the daemon-code boundary and the single
# source of truth for it. The single-quotes are load-bearing: they stop the
# SHELL from glob-expanding `_*.py` so git receives the literal pathspec and
# does its own match. Do NOT inline this list into the post-commit hook or
# into _runtime.sh — both must call this script so the boundary stays in one
# place. Changing the daemon's import surface? Change this pathspec AND the
# import-surface list in the header comment above (keep the two in sync).
# B1: retry transient git-lock contention before failing toward restart.
# A concurrent agent commit (or the operator's) briefly holds .git/index.lock
# or a ref lock; the bare `|| exit 0` below used to treat that transient error
# as "cannot prove unchanged -> restart", producing the soak's spurious
# "pure fail-open" daemon recycles. We retry ONLY transient lock errors; any
# other error, or a transient one that persists past the retries, still exits
# 0 (fail toward restart — never serve stale code). Hot path: first try
# succeeds, zero added latency. stderr is folded into the capture to classify
# the error: on rc=0 the capture is the file list (stderr empty); on rc!=0 it
# is the error text (git diff fails before listing any file).
changed=""
_diff_rc=1
for _attempt in 1 2 3; do
    _out="$("${GIT[@]}" diff --name-only "$BASE" HEAD -- \
        mind_api/src \
        'core/scripts/_*.py' \
        core/scripts/gates \
        core/scripts/storage_backend.py \
        core/scripts/experience.py \
        core/scripts/tree.py \
        core/scripts/tree_match.py \
        2>&1)"
    _diff_rc=$?
    if [ "$_diff_rc" -eq 0 ]; then
        changed="$_out"
        break
    fi
    case "$_out" in
        *index.lock*|*"cannot lock"*|*"Unable to create"*|*"nother git process"*)
            sleep 0.2 ;;          # transient lock — retry
        *)
            break ;;              # non-transient error — fail toward restart now
    esac
done
[ "$_diff_rc" -eq 0 ] || exit 0   # error (incl. exhausted transient retries) -> fail toward restart

if [ -n "$changed" ]; then
    exit 0   # daemon code surface changed → restart warranted
fi
exit 1       # no daemon-surface change → running daemon is current
