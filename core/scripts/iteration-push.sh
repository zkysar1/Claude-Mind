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
#     cleanly (merge --abort if MERGE_HEAD exists) and logged LOUDLY. Dirty-tree
#     refusals self-heal in-run (agents/<self>/* churn is COMMITTED pathspec-
#     limited, 9; agents/<other>/* churn is CLEARED, 3) and
#     the merge retries once; remaining shapes (staged partner work, dirty
#     core/world files) defer to next iteration. A REPEATED conflict log means
#     a true cross-machine content conflict — surface it, do not silence it.
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
#   - Push-race recovery (9): a race-shaped push rejection
#     (non-fast-forward / fetch-first / cannot-lock-ref) triggers ONE bounded
#     in-invocation recovery — unthrottled fetch + merge + one retry push —
#     before deferring. Without it, the fetch throttle makes the next
#     iteration's retry re-fail against the same stale tracking ref for up to
#     FETCH_INTERVAL_MIN while a deep-close commit sits stranded local-only
#     (the rb-3970 completed-but-not-on-origin phantom window). Auth/network
#     failures do not match the race signature and defer directly.
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

# --- Cross-agent-churn self-heal helper (3, per 3) ---------
# On a dirty-tree merge REFUSAL (git refuses before starting; MERGE_HEAD absent),
# the deadlock is almost always unstaged cross-agent churn: agents/<other>/*
# files that owncloud re-materialised and iteration-commit's namespace filter
# refuses to commit, overlapping the incoming origin merge. The retry-next-
# iteration path never self-heals because owncloud re-creates the same churn
# every cycle (observed 2026-07-06: alpha framework commits stranded 2+ iters).
#
# This helper narrowly self-heals TWO shapes (all-or-nothing scan first):
#   - agents/<other>/* churn (unstaged tracked-dirty + untracked,
#     --exclude-standard so ignored scratch is out): CLEAR those paths
#     (owncloud re-syncs the sibling's authoritative state next cycle; origin
#     is authoritative).
#   - agents/<self>/* churn: COMMIT it via an index-clean + explicit-pathspec
#     + pathspec-limited commit (9). Own agent-dir ledgers
#     (changelog.jsonl re-appends on EVERY write) re-dirty between
#     iteration-commit and this merge — and iteration-commit only runs on
#     deep closes at all — so the pre-2249 defer wedged a behind box
#     INDEFINITELY (cc-05: 15-ahead/53-behind, stable, never resolving).
#     Never cleared (that would discard own work); committed work is
#     preserved in history and pushed.
# Then RETRY the merge once. Returns 0 iff healed+re-merged clean. Returns 1
# (DEFER — caller keeps the current behaviour) for EVERY other shape:
#   - MIND_AGENT unresolved (cannot identify self → cannot scope safely)
#   - guard-741: ANY staged index entry (a CONCURRENT agent's staged cross-agent
#     work — NEVER discard it; defer, exactly as the bare merge already does)
#   - ANY blocking file outside agents/* (core/, world/ — never clear or
#     commit shared work from the push path)
#   - empty blocking set, self-commit failure, or the retry still failing
# FAIL-SOFT throughout (|| true; iteration-push is soft_exit — a bug here can
# only DEFER, never wedge the loop or discard staged work).
_selfheal_cross_agent_churn_remerge() {
  local self="${MIND_AGENT:-}"
  if [ -z "$self" ]; then
    log "self-heal: MIND_AGENT unresolved — cannot scope safely, defer"
    return 1
  fi

  # guard-741: the shared multi-agent index can hold a CONCURRENT agent's STAGED
  # cross-agent files. NEVER discard staged work — if the index has any staged
  # entry, defer (the bare merge already refuses on staged overlap anyway).
  if [ -n "$(git -C "$REPO" diff --cached --name-only 2>/dev/null)" ]; then
    log "self-heal: staged index entries present — guard-741, defer (never discard partner's staged work)"
    return 1
  fi

  # Two blocking categories, cleared by different git verbs:
  #   tracked-dirty (unstaged modify/delete) -> git checkout -- (restore to HEAD)
  #   untracked (--exclude-standard)          -> git clean -fdq -- (remove)
  # Keep them SEPARATE so a mixed pathspec can't make `git checkout` abort on an
  # untracked path and leave the tracked ones un-restored. NUL-delimited reads
  # for space-safe paths; index is already known clean (staged check above).
  local dirty=() untracked=() p
  while IFS= read -r -d '' p; do dirty+=("$p"); done \
    < <(git -C "$REPO" diff --name-only -z 2>/dev/null)
  while IFS= read -r -d '' p; do untracked+=("$p"); done \
    < <(git -C "$REPO" ls-files --others --exclude-standard -z 2>/dev/null)

  local total=$(( ${#dirty[@]} + ${#untracked[@]} ))
  if [ "$total" -eq 0 ]; then
    log "self-heal: no unstaged/untracked files — not a dirty-tree churn shape, defer"
    return 1
  fi

  # Classify EVERY blocking path FIRST (all-or-nothing): agents/<self>/* is
  # committed, agents/<other>/* is cleared, ANY path outside agents/* defers
  # the whole tree untouched (never clear or commit core/world/shared work
  # from the push path).
  local rel name
  local self_paths=() cross_dirty=() cross_untracked=()
  for rel in "${dirty[@]}"; do
    case "$rel" in
      agents/*)
        name="${rel#agents/}"; name="${name%%/*}"
        if [ "$name" = "$self" ]; then self_paths+=("$rel"); else cross_dirty+=("$rel"); fi
        ;;
      *)
        log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
        return 1
        ;;
    esac
  done
  for rel in "${untracked[@]}"; do
    case "$rel" in
      agents/*)
        name="${rel#agents/}"; name="${name%%/*}"
        if [ "$name" = "$self" ]; then self_paths+=("$rel"); else cross_untracked+=("$rel"); fi
        ;;
      *)
        log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
        return 1
        ;;
    esac
  done

  # SELF-namespace churn: COMMIT it, never clear, never defer on it
  # (9). guard-741/836-safe here because (a) the staged-index check
  # above proved the shared index holds ZERO staged entries, (b) staging is by
  # EXPLICIT self-namespace paths only, and (c) the commit is pathspec-limited
  # to agents/<self>/ — a partner's stage racing in between (a) and the commit
  # is excluded from it. Same namespace scope iteration-commit.sh's filter
  # enforces — NOT a bare `git commit` (which would absorb anything staged).
  if [ "${#self_paths[@]}" -gt 0 ]; then
    log "self-heal: committing ${#self_paths[@]} SELF-namespace file(s) pre-merge (g-115-2249)"
    if ! git -C "$REPO" add -- "${self_paths[@]}" 2>/dev/null; then
      log "self-heal: git add of self-namespace churn failed — defer"
      return 1
    fi
    if ! git -C "$REPO" commit -q \
         -m "chore($self): pre-merge self-namespace churn (iteration-push self-heal, g-115-2249)" \
         -- "agents/$self/" 2>/dev/null; then
      # Unstage what we staged so a failed heal leaves the index as found.
      git -C "$REPO" reset -q -- "agents/$self/" 2>/dev/null || true
      log "self-heal: pathspec-limited commit of self-namespace churn failed — defer"
      return 1
    fi
  fi

  if [ $(( ${#cross_dirty[@]} + ${#cross_untracked[@]} )) -gt 0 ]; then
    log "self-heal: clearing ${#cross_dirty[@]} tracked + ${#cross_untracked[@]} untracked cross-agent file(s), retrying merge once (g-115-1843)"
    [ "${#cross_dirty[@]}" -gt 0 ]     && { git -C "$REPO" checkout -- "${cross_dirty[@]}" 2>/dev/null || true; }
    [ "${#cross_untracked[@]}" -gt 0 ] && { git -C "$REPO" clean -fdq -- "${cross_untracked[@]}" 2>/dev/null || true; }
  fi

  # Retry the merge ONCE. Reassigns the OUTER MERGE_OUT/MERGE_RC (intentional —
  # the caller's post-helper defer log then reflects the retry outcome).
  MERGE_OUT="$(GIT_TERMINAL_PROMPT=0 git -C "$REPO" merge --no-edit "$UPSTREAM" 2>&1)"
  MERGE_RC=$?
  if [ "$MERGE_RC" -eq 0 ]; then
    return 0
  fi
  # Retry still failed — clean up any half-merge state and defer.
  if [ -f "$GITDIR/MERGE_HEAD" ]; then
    git -C "$REPO" merge --abort >/dev/null 2>&1 || true
  fi
  log "self-heal: merge retry still failed after clearing churn (rc=${MERGE_RC}) — defer"
  return 1
}

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
        log "If this repeats every iteration it is a TRUE cross-machine content conflict (MERGE_HEAD was created — NOT the dirty-tree defer shape): resolve manually (git merge $UPSTREAM) or investigate which store conflicted."
        soft_exit 1
      fi
      # Dirty tree — merge refused before starting (MERGE_HEAD absent). Try the
      # narrow cross-agent-churn self-heal (3): if the entire blocking
      # set is unstaged agents/<other>/* churn, clear it and retry the merge
      # once. Any other shape (staged entries, self/core/world dirty) DEFERS —
      # exactly the pre-1843 behaviour (log + soft_exit 1).
      if _selfheal_cross_agent_churn_remerge; then
        log "integrated ${BEHIND} origin commit(s) into $BRANCH (after churn self-heal, g-115-1843/g-115-2249)"
      else
        log "merge DEFERRED (rc=${MERGE_RC}): $(printf '%s' "$MERGE_OUT" | tail -n 1)"
        log "remaining defer shapes: a partner's staged index entries (guard-741) or dirty core/world/shared files — NOT self churn (auto-committed) or cross-agent churn (auto-cleared); retry next iteration"
        soft_exit 1
      fi
    else
      log "integrated ${BEHIND} origin commit(s) into $BRANCH"
    fi
  fi
fi

# Commits ahead of origin (shared local ref; agents are the only pushers).
AHEAD="$(git -C "$REPO" rev-list --count "$UPSTREAM..$BRANCH" 2>/dev/null || echo 0)"
case "$AHEAD" in ''|*[!0-9]*) AHEAD=0;; esac   # force numeric
if [ "$AHEAD" -eq 0 ]; then
  log "origin/$BRANCH up to date (0 ahead) — nothing to push"; soft_exit 0
fi

# Stranded-depth alarm (8 user correction). A push-blocked window
# (read-only deploy key rb-3236/guard-1021, disabled push, repeated fail-soft)
# lets AHEAD grow silently — 121 (8) then 281 by 2026-07-16, and the
# eventual bulk unwedge push carried a stale store base that transiently
# regressed world/aspirations.jsonl by ~184 goals (2). Once depth
# crosses the cap, bang the drum EVERY iteration until it drains: the banner
# lands in iteration-close stdout where the loop LLM must act on it (fix the
# push pipe or notify the user — never let depth keep growing). Alarm only;
# never blocks or defers the push itself.
BULK_ALARM="${ITERATION_PUSH_BULK_ALARM:-25}"
case "$BULK_ALARM" in ''|*[!0-9]*) BULK_ALARM=25;; esac
if [ "$AHEAD" -ge "$BULK_ALARM" ]; then
  log "⚠ STRANDED-DEPTH ALARM: ${AHEAD} unpushed commit(s) >= ${BULK_ALARM} on ${BRANCH} — bulk-push side-effect risk (stale store bases, g-115-2362 class). ACT NOW: if pushes are failing, fix the credential/remote (rb-3236/guard-1021); if push is deliberately disabled, notify the user of the growing backlog. Do NOT let depth grow to another 281-commit unwedge (g-115-2398)."
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
fi

# --- Push-race recovery (9, rb-3970 phantom window) -----------------
# A race-shaped rejection here means origin advanced AFTER the fetch/merge
# above — or the THROTTLED fetch used a stale tracking ref, so the integrate
# step saw BEHIND=0 and skipped the merge entirely. "Retry next iteration"
# COMPOUNDS under the throttle: the next run finds FETCH_HEAD fresh, throttles
# again, computes BEHIND against the SAME stale ref, skips the merge, and
# fails the identical push — up to FETCH_INTERVAL_MIN of repeated failures
# while a deep-close commit sits stranded local-only (the goal-status-vs-origin
# phantom window; both 2026-07-18 phantoms rode this shape, and 3
# landed first-try via exactly this unthrottled fetch+merge+push sequence).
# Recover ONCE in-invocation: unthrottled fetch -> merge (same safety as the
# integrate step: abort on MERGE_HEAD, defer on refusal — churn was already
# self-healed moments ago, so a NEW refusal defers) -> one retry push. Bounded
# (no loop), fail-soft, never forced. Skipped under --no-fetch (recovery needs
# its own fetch). Auth/network failures do NOT match the race signature — an
# immediate retry cannot fix those, so they keep the plain defer below.
if [ "$NO_FETCH" -eq 0 ] && printf '%s' "$PUSH_OUT" | grep -qiE 'non-fast-forward|fetch first|cannot lock ref|\[rejected\]'; then
  log "push rejected (race shape) — in-invocation recovery: unthrottled fetch + merge + one retry (g-115-2599)"
  RFETCH_OUT="$(GIT_TERMINAL_PROMPT=0 git -C "$REPO" fetch origin "$BRANCH" 2>&1)"
  RFETCH_RC=$?
  if [ "$RFETCH_RC" -ne 0 ]; then
    log "recovery fetch FAILED (rc=${RFETCH_RC}) — fail-soft, will retry next iteration: $(printf '%s' "$RFETCH_OUT" | tail -n 1)"
    soft_exit 1
  fi
  RBEHIND="$(git -C "$REPO" rev-list --count "$BRANCH..$UPSTREAM" 2>/dev/null || echo 0)"
  case "$RBEHIND" in ''|*[!0-9]*) RBEHIND=0;; esac
  if [ "$RBEHIND" -gt 0 ]; then
    RMERGE_OUT="$(GIT_TERMINAL_PROMPT=0 git -C "$REPO" merge --no-edit "$UPSTREAM" 2>&1)"
    RMERGE_RC=$?
    if [ "$RMERGE_RC" -ne 0 ]; then
      if [ -f "$GITDIR/MERGE_HEAD" ]; then
        git -C "$REPO" merge --abort >/dev/null 2>&1 || true
        log "recovery merge CONFLICT with $UPSTREAM — aborted cleanly, will retry next iteration"
      else
        log "recovery merge refused (rc=${RMERGE_RC}) — will retry next iteration: $(printf '%s' "$RMERGE_OUT" | tail -n 1)"
      fi
      soft_exit 1
    fi
  fi
  RPUSH_OUT="$(GIT_TERMINAL_PROMPT=0 git -C "$REPO" push origin "$BRANCH" 2>&1)"
  RPUSH_RC=$?
  if [ "$RPUSH_RC" -eq 0 ]; then
    log "push-race recovery OK: origin/${BRANCH} now at $(git -C "$REPO" rev-parse --short "$UPSTREAM" 2>/dev/null || echo '?') (${RBEHIND} origin commit(s) integrated in-recovery)"
    soft_exit 0
  fi
  log "push-race recovery FAILED (rc=${RPUSH_RC}) — fail-soft, will retry next iteration: $(printf '%s' "$RPUSH_OUT" | tail -n 1)"
  soft_exit 1
fi

# Auth / network / non-race shapes. Fail-soft; retry next iteration. NEVER force.
log "push FAILED (rc=${PUSH_RC}) — fail-soft, will retry next iteration: $(printf '%s' "$PUSH_OUT" | tail -n 1)"
soft_exit 1
