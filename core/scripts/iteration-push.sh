#!/usr/bin/env bash
# iteration-push.sh — Fail-soft, rate-limited push of the shared Mind tree to origin.
#
# USER DIRECTIVE (user 2026-07-02, 4): loop-commits
# accumulate locally via iteration-commit.sh (which explicitly does NOT push —
# see its header "push is manual or via a future iteration-push.sh"). This is
# that durable mechanism: keep origin current without a per-commit push storm.
# Wired into iteration-close.sh do_productivity_check AFTER the state-update
# commit and BEFORE the ITERATION COMPLETE imperative.
#
# Contract:
#   - Push the current branch to origin. NEVER force-push, NEVER rewrite history.
#   - FETCH + INTEGRATE first (multi-machine): before the push decision, run a
#     throttled `git fetch origin <branch>` (default: at most once per
#     FETCH_INTERVAL_MIN, stateless via FETCH_HEAD mtime), and if origin has
#     commits local lacks, `git merge --no-edit` them in (merge, NEVER rebase —
#     shared history is never rewritten). This is what keeps EVERY machine both
#     fetching updates from GitHub and pushing updates to GitHub: without it, the
#     first push from a second machine wedges this machine's pushes permanently
#     (non-fast-forward retried forever — the 2026-07-03 6-vs-12 divergence).
#     Merge safety in the shared multi-agent tree: `git merge` ABORTS if the
#     index has staged entries (it cannot absorb a concurrent partner's staged
#     files the way a bare `git commit` can — guard-741/guard-836 hazard does
#     not apply), and refuses rather than overwrites dirty working-tree files.
#     Any merge failure (dirty tree, staged entries, true conflict) is aborted
#     cleanly (merge --abort if MERGE_HEAD exists), logged LOUDLY, and retried
#     next iteration after iteration-commit has swept the churn. A REPEATED
#     conflict log means a true cross-machine content conflict — surface it,
#     do not silence it.
#   - Rate-limit (batch): push only when local is ahead of origin by
#     >= MIN_COMMITS, OR the OLDEST unpushed commit is >= MAX_AGE_MIN minutes old
#     (a freshness floor so a lone commit does not sit unpushed for hours).
#     Both thresholds are derived purely from git state — no timestamp file, so
#     the decision is stateless, race-tolerant, and SELF-COORDINATING across the
#     shared multi-agent tree: all agents share ONE .git, so after any agent
#     pushes, the shared refs/remotes/origin/<branch> ref advances and every
#     other agent's next iteration-close sees 0-ahead and skips (no push storm,
#     no timestamp-file contention).
#   - Skip (do NOT remove — guard-853) when .git/index.lock is held; retry next
#     iteration.
#   - Fail-soft: ANY failure (auth, network, fetch, merge) logs to stderr and
#     exits 0 — a sync failure MUST NEVER block or abort the loop. Next iteration
#     retries. Never forced.
#   - Headless-safe: GIT_TERMINAL_PROMPT=0 turns a would-be credential PROMPT into
#     an immediate failure instead of a hang.
#   - Auth via the repo's configured credential helper (GCM `manager` over HTTPS);
#     no PAT is embedded, constructed, or printed (guard-724). The remote URL is
#     the plain https://github.com/... form (no tokenized URL).
#
# Exit: always 0 (fail-soft) UNLESS --strict is passed (tests only), in which
#       case a genuine push/merge failure exits 1 and a throttle/skip still exits 0.
#
# Origin: 4 (alpha, user-directed keep-github-current-autopush).
#         Fetch+integrate: user-directed 2026-07-03 (bolster the GitHub side of
#         the relationship — both directions, regardless of what computer).

set -uo pipefail   # deliberately NOT -e: fail-soft is the whole point; every
                   # failure path is handled explicitly and exits 0 by default.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

REPO="${ITERATION_PUSH_REPO:-}"
BRANCH_OVERRIDE=""
DRY_RUN=0
STRICT=0
MIN_COMMITS="${ITERATION_PUSH_MIN_COMMITS:-5}"
MAX_AGE_MIN="${ITERATION_PUSH_MAX_AGE_MIN:-20}"
FETCH_INTERVAL_MIN="${ITERATION_PUSH_FETCH_INTERVAL_MIN:-10}"
NO_FETCH=0

usage() {
  cat <<'EOF'
Usage: iteration-push.sh [--repo <path>] [--branch <name>] [--min-commits <n>]
                         [--max-age-min <m>] [--fetch-interval-min <m>]
                         [--no-fetch] [--dry-run] [--strict] [-h|--help]

Fail-soft, rate-limited bidirectional sync of the current branch with origin.
Fetches origin (throttled) and merges origin-ahead commits in (never rebase,
never force), then pushes when local is >= --min-commits ahead OR the oldest
unpushed commit is >= --max-age-min minutes old. Never blocks the loop.

Options:
  --repo <path>       Repo to push (default: $ITERATION_PUSH_REPO, else PROJECT_ROOT
                      from _paths.sh, else two levels above this script).
  --branch <name>     Branch to push (default: current branch).
  --min-commits <n>   Ahead-count threshold (default 5, env ITERATION_PUSH_MIN_COMMITS).
  --max-age-min <m>   Oldest-unpushed-commit age floor in minutes (default 20,
                      env ITERATION_PUSH_MAX_AGE_MIN).
  --fetch-interval-min <m>  Throttle: skip fetch if FETCH_HEAD is younger than
                      this many minutes (default 10, env
                      ITERATION_PUSH_FETCH_INTERVAL_MIN). 0 = always fetch.
  --no-fetch          Skip the fetch+integrate step entirely (offline/tests).
  --dry-run           Compute + log every decision; do NOT fetch-merge or push.
  --strict            Exit 1 on a genuine push/merge failure (tests). Default: always 0.
  -h, --help          Show this help.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)        REPO="${2:-}"; shift 2;;
    --branch)      BRANCH_OVERRIDE="${2:-}"; shift 2;;
    --min-commits) MIN_COMMITS="${2:-5}"; shift 2;;
    --max-age-min) MAX_AGE_MIN="${2:-20}"; shift 2;;
    --fetch-interval-min) FETCH_INTERVAL_MIN="${2:-10}"; shift 2;;
    --no-fetch)    NO_FETCH=1; shift;;
    --dry-run)     DRY_RUN=1; shift;;
    --strict)      STRICT=1; shift;;
    -h|--help)     usage; exit 0;;
    *)             echo "[iteration-push] unknown arg: $1" >&2; shift;;
  esac
done

log() { echo "[iteration-push] $*" >&2; }
# soft_exit <code>: honor <code> only under --strict; otherwise always 0.
soft_exit() { if [ "$STRICT" = 1 ]; then exit "${1:-0}"; fi; exit 0; }

# Resolve repo root: explicit --repo/env > PROJECT_ROOT (from _paths.sh) > fallback.
if [ -z "$REPO" ]; then
  if [ -f "$SCRIPT_DIR/_paths.sh" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true
    REPO="${PROJECT_ROOT:-}"
  fi
fi
[ -z "$REPO" ] && REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Must be a git repo.
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  log "not a git repo: $REPO — skip"; soft_exit 0
fi

# Skip if a git operation is mid-flight (index.lock). guard-853: SKIP, never rm.
GITDIR="$(git -C "$REPO" rev-parse --git-dir 2>/dev/null || echo "")"
case "$GITDIR" in
  /*|[A-Za-z]:*) ;;              # already absolute (POSIX or Windows drive)
  *) GITDIR="$REPO/$GITDIR";;    # relative -> anchor to repo
esac
if [ -n "$GITDIR" ] && [ -f "$GITDIR/index.lock" ]; then
  log "index.lock held ($GITDIR/index.lock) — a git op is in progress; skip, retry next iteration"
  soft_exit 0
fi

# Current branch (skip detached HEAD).
BRANCH="${BRANCH_OVERRIDE:-$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")}"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  log "detached HEAD or no branch — skip"; soft_exit 0
fi

# Need the origin tracking ref to compute lag. First push must be manual/-u.
UPSTREAM="origin/$BRANCH"
if ! git -C "$REPO" rev-parse --verify "$UPSTREAM" >/dev/null 2>&1; then
  log "no $UPSTREAM ref — do the first push manually (git push -u origin $BRANCH); skip"
  soft_exit 0
fi

# --- Fetch (throttled, fail-soft) -------------------------------------------
# Keep this machine CURRENT with commits pushed from other machines. Stateless
# throttle: FETCH_HEAD mtime is the last-fetch timestamp (no extra state file).
if [ "$NO_FETCH" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  FETCH_DUE=1
  FETCH_HEAD="$GITDIR/FETCH_HEAD"
  if [ "$FETCH_INTERVAL_MIN" -gt 0 ] 2>/dev/null && [ -f "$FETCH_HEAD" ]; then
    FH_CT="$(date -r "$FETCH_HEAD" +%s 2>/dev/null || echo "")"
    case "$FH_CT" in
      ''|*[!0-9]*) ;;   # unreadable mtime -> fetch anyway
      *) NOW_CT="$(date +%s)"
         FH_AGE_MIN=$(( (NOW_CT - FH_CT) / 60 ))
         if [ "$FH_AGE_MIN" -ge 0 ] && [ "$FH_AGE_MIN" -lt "$FETCH_INTERVAL_MIN" ]; then
           FETCH_DUE=0
           log "fetch throttled: last fetch ${FH_AGE_MIN}m ago (< ${FETCH_INTERVAL_MIN}m) — using last-known origin ref"
         fi;;
    esac
  fi
  if [ "$FETCH_DUE" -eq 1 ]; then
    FETCH_OUT="$(GIT_TERMINAL_PROMPT=0 git -C "$REPO" fetch origin "$BRANCH" 2>&1)"
    FETCH_RC=$?
    if [ "$FETCH_RC" -ne 0 ]; then
      log "fetch FAILED (rc=${FETCH_RC}) — fail-soft, using last-known origin ref: $(printf '%s' "$FETCH_OUT" | tail -n 1)"
    fi
  fi
fi

# --- Integrate (merge origin-ahead commits; never rebase, never force) -------
# Without this, the first push from a SECOND machine leaves this machine
# non-fast-forward forever (the 2026-07-03 divergence wedge).
BEHIND="$(git -C "$REPO" rev-list --count "$BRANCH..$UPSTREAM" 2>/dev/null || echo 0)"
case "$BEHIND" in ''|*[!0-9]*) BEHIND=0;; esac
if [ "$BEHIND" -gt 0 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: origin/$BRANCH is ${BEHIND} ahead of local — WOULD merge before pushing"
  else
    log "origin/$BRANCH has ${BEHIND} commit(s) local lacks — integrating (merge --no-edit, never rebase)"
    MERGE_OUT="$(GIT_TERMINAL_PROMPT=0 git -C "$REPO" merge --no-edit "$UPSTREAM" 2>&1)"
    MERGE_RC=$?
    if [ "$MERGE_RC" -ne 0 ]; then
      # Conflict state left behind? Abort it — the tree must NEVER be left
      # mid-merge for the loop to trip over.
      if [ -f "$GITDIR/MERGE_HEAD" ]; then
        git -C "$REPO" merge --abort >/dev/null 2>&1 || true
        log "MERGE CONFLICT with $UPSTREAM — aborted cleanly, will retry next iteration."
        log "If this repeats every iteration it is a TRUE cross-machine content conflict: resolve manually (git merge $UPSTREAM) or investigate which store conflicted."
      else
        # Dirty tree / staged entries — merge refused before starting (safe).
        log "merge DEFERRED (rc=${MERGE_RC}): $(printf '%s' "$MERGE_OUT" | tail -n 1)"
        log "retry next iteration after iteration-commit sweeps the churn"
      fi
      soft_exit 1
    fi
    log "integrated ${BEHIND} origin commit(s) into $BRANCH"
  fi
fi

# Commits ahead of origin (shared local ref; agents are the only pushers).
AHEAD="$(git -C "$REPO" rev-list --count "$UPSTREAM..$BRANCH" 2>/dev/null || echo 0)"
case "$AHEAD" in ''|*[!0-9]*) AHEAD=0;; esac   # force numeric
if [ "$AHEAD" -eq 0 ]; then
  log "origin/$BRANCH up to date (0 ahead) — nothing to push"; soft_exit 0
fi

# Age (minutes) of the OLDEST unpushed commit — freshness floor.
OLDEST_CT="$(git -C "$REPO" log "$UPSTREAM..$BRANCH" --format=%ct 2>/dev/null | tail -n 1 || echo "")"
AGE_MIN=0
case "$OLDEST_CT" in
  ''|*[!0-9]*) AGE_MIN=0;;
  *) NOW_CT="$(date +%s)"; AGE_MIN=$(( (NOW_CT - OLDEST_CT) / 60 )); [ "$AGE_MIN" -lt 0 ] && AGE_MIN=0;;
esac

# Rate-limit: push only if enough commits accrued OR the oldest is stale enough.
if [ "$AHEAD" -lt "$MIN_COMMITS" ] && [ "$AGE_MIN" -lt "$MAX_AGE_MIN" ]; then
  log "throttled: ${AHEAD} ahead (< ${MIN_COMMITS}) AND oldest unpushed ${AGE_MIN}m old (< ${MAX_AGE_MIN}m) — defer"
  soft_exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
  log "dry-run: WOULD push ${BRANCH} -> origin (${AHEAD} ahead, oldest ${AGE_MIN}m old)"
  soft_exit 0
fi

log "pushing ${BRANCH} -> origin (${AHEAD} ahead, oldest ${AGE_MIN}m old)"
# GIT_TERMINAL_PROMPT=0: never block on an interactive credential prompt (headless).
# No --force, ever. Capture combined output for the log summary (GCM never prints
# the token; the remote is a plain https URL, so no secret leaks).
PUSH_OUT="$(GIT_TERMINAL_PROMPT=0 git -C "$REPO" push origin "$BRANCH" 2>&1)"
PUSH_RC=$?
if [ "$PUSH_RC" -eq 0 ]; then
  log "push OK: origin/${BRANCH} now at $(git -C "$REPO" rev-parse --short "$UPSTREAM" 2>/dev/null || echo '?')"
  soft_exit 0
else
  # Auth / network / non-fast-forward. Fail-soft; retry next iteration. NEVER force.
  log "push FAILED (rc=${PUSH_RC}) — fail-soft, will retry next iteration: $(printf '%s' "$PUSH_OUT" | tail -n 1)"
  soft_exit 1
fi
