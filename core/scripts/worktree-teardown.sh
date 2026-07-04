#!/usr/bin/env bash
# worktree-teardown.sh — Safely tear down a git worktree by FIRST reaping the
# worktree's OWN mind_api daemon (by its published PIDs, zero cross-repo risk),
# waiting for Windows async handle release, then removing the worktree, and
# finishing with one cross-repo-safe orphan sweep pass.
#
# Usage:
#   bash core/scripts/worktree-teardown.sh <worktree-path> [--force] [--quiet]
#
# Why (): pytest subprocess tests run INSIDE a throwaway promotion
# worktree shell out through _runtime.sh rt_ensure_running(), which auto-spawns
# a daemon rooted at the WORKTREE's mind_api/state/. Deleting the worktree while
# that daemon is still alive (a) leaves a true orphan (the pid file vanishes, so
# the daemon is unkillable-by-pidfile) and (b) fails on Windows
# ('Device or resource busy' / 'Permission denied') because the live daemon
# holds the worktree dir open. This wrapper reaps the worktree's daemon FIRST
# (by the worktree's own daemon.pid + daemon.parent.pid — exact PIDs, so there
# is ZERO cross-repo risk), so the removal succeeds and no orphan is created.
# Composable into promotion-preflight.sh / seed-transplant.sh teardown steps.
#
# Exit codes:
#   0 — worktree removed (or already absent + metadata pruned)
#   1 — removal failed (git worktree remove failed, e.g. handle still busy)
#   2 — usage error
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_runtime.sh"   # rt_force_kill_tree — CommandLine-guarded reap

WORKTREE=""
FORCE=0
QUIET=0
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1 ;;
        --quiet) QUIET=1 ;;
        -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
        --) shift; [ $# -gt 0 ] && WORKTREE="$1"; break ;;
        -*) echo "[worktree-teardown] ERROR: unknown flag: $1" >&2; exit 2 ;;
        *)
            [ -z "$WORKTREE" ] || { echo "[worktree-teardown] ERROR: multiple paths given" >&2; exit 2; }
            WORKTREE="$1"
            ;;
    esac
    shift
done

[ -n "$WORKTREE" ] || { echo "[worktree-teardown] ERROR: worktree path required" >&2; sed -n '7,8p' "$0" >&2; exit 2; }

_say() { [ "$QUIET" = "1" ] && return 0; echo "$@"; }

# Resolve to an absolute path if it still exists; tolerate already-removed dirs
# (we still prune the git metadata in that case).
if [ -d "$WORKTREE" ]; then
    WORKTREE="$(cd "$WORKTREE" && pwd)"
else
    _say "[worktree-teardown] path not present on disk: $WORKTREE (will still prune git metadata)"
fi

# Safety guard: never operate on the main repo itself.
if [ "$WORKTREE" = "$PROJECT_ROOT" ]; then
    echo "[worktree-teardown] ERROR: refusing to tear down PROJECT_ROOT ($PROJECT_ROOT)" >&2
    exit 2
fi

# 1. Reap the worktree's OWN daemon by its published PIDs (both state layouts).
#    rt_force_kill_tree is CommandLine-guarded against 'mind_api' and kills only
#    the exact PIDs passed — it can never touch a sibling repo's daemon.
_reap_worktree_daemon() {
    local base="$1" sub pidf child parent
    for sub in "mind_api/state" ".mind-data/mind_api/state"; do
        pidf="$base/$sub/daemon.pid"
        [ -f "$pidf" ] || continue
        child="$(tr -d '[:space:]' < "$pidf" 2>/dev/null)"
        parent=""
        [ -f "$base/$sub/daemon.parent.pid" ] && parent="$(tr -d '[:space:]' < "$base/$sub/daemon.parent.pid" 2>/dev/null)"
        if [ -n "$child" ]; then
            _say "[worktree-teardown] reaping worktree daemon: child=$child parent=${parent:-<none>} ($sub)"
            rt_force_kill_tree "$child" "$parent"
        fi
    done
}
[ -d "$WORKTREE" ] && _reap_worktree_daemon "$WORKTREE"

# 2. Wait for Windows async handle release before removal (the killed daemon's
#    open handles on the worktree dir are released asynchronously).
sleep 2

# 3. Remove the worktree via git (run from the owning main repo, PROJECT_ROOT).
git_args=(worktree remove "$WORKTREE")
[ "$FORCE" = "1" ] && git_args+=(--force)
rm_ok=0
if git -C "$PROJECT_ROOT" "${git_args[@]}" 2>&1; then
    rm_ok=1
    _say "[worktree-teardown] git worktree remove succeeded"
else
    # Retry once after a longer settle — Windows handle release can lag.
    _say "[worktree-teardown] first removal failed; settling 3s and retrying..."
    sleep 3
    if git -C "$PROJECT_ROOT" "${git_args[@]}" 2>&1; then
        rm_ok=1
        _say "[worktree-teardown] git worktree remove succeeded on retry"
    fi
fi

# 4. Prune stale worktree metadata regardless (handles already-deleted dirs and
#    leaves the worktree registry clean even if removal needed --force).
git -C "$PROJECT_ROOT" worktree prune >/dev/null 2>&1 || true

# 5. One cross-repo-safe sweep pass to clean any residual orphan whose pid file
#    has now vanished with the worktree. daemon-orphan-sweep.sh is cross-repo
#    safe by construction (): it protects every live sibling deployment.
_say "[worktree-teardown] cross-repo-safe orphan sweep..."
bash "$SCRIPT_DIR/daemon-orphan-sweep.sh" --clean --quiet || true

if [ "$rm_ok" = "1" ]; then
    _say "[worktree-teardown] done."
    exit 0
fi
echo "[worktree-teardown] ERROR: worktree removal failed (handle still busy?): $WORKTREE" >&2
exit 1
