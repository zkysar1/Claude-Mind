#!/usr/bin/env bash
# iteration-push.sh — Fail-soft, rate-limited push of the shared Mind tree to origin.
#
# USER DIRECTIVE (user 2026-07-02, ): loop-commits
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
#     limited, ; agents/<other>/* churn is CLEARED, ) and
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
#   - Push-race recovery (): a race-shaped push rejection
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
# Origin:  (alpha, user-directed keep-github-current-autopush).
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
NO_PUSH=0
PUSH_WORKER_REF=0
WORKER_REF_AGENT="${MIND_AGENT:-}"
WORKER_REF_SID="${MIND_SID:-}"

usage() {
  cat <<'EOF'
Usage: iteration-push.sh [--repo <path>] [--branch <name>] [--min-commits <n>]
                         [--max-age-min <m>] [--fetch-interval-min <m>]
                         [--no-fetch] [--no-push] [--dry-run] [--strict] [-h|--help]

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
  --no-push           Fetch + integrate, then STOP before the push decision.
                      The inverse of --no-fetch. This is the session-start
                      continuity pull for local-backend deployments: a starting
                      session must become current WITHOUT publishing its state
                      as a side effect of starting (g-115-3871).
  --push-worker-ref   WORKER CARRIER (g-306-264). Fetch + integrate, then push HEAD
                      to refs/workers/<agent>/<sid> and STOP — never the shared
                      branch. Lets a worker Body's framework-file edits and local
                      commits reach the reducer, which they otherwise cannot: the
                      storage backend carries world/ and meta/ but not git, so
                      core/** and .claude/** edits made on a worker box reach
                      nothing at all. --no-push's rationale is avoiding contention
                      on the shared tree; a per-Body namespaced ref has exactly ONE
                      writer by construction, so that rationale does not reach it.
                      Agent/SID come from MIND_AGENT/MIND_SID or the flags below.
  --worker-ref-agent <a>  Override the agent segment of the ref (default MIND_AGENT).
  --worker-ref-sid <s>    Override the sid segment of the ref (default MIND_SID).
  --dry-run           Compute + log every decision; do NOT fetch-merge or push.
  --strict            Exit 1 on a genuine push/merge failure (tests). Default: always 0.
  -h, --help          Show this help.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)        REPO="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --branch)      BRANCH_OVERRIDE="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --min-commits) MIN_COMMITS="${2:-5}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --max-age-min) MAX_AGE_MIN="${2:-20}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --fetch-interval-min) FETCH_INTERVAL_MIN="${2:-10}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --no-fetch)    NO_FETCH=1; shift;;
    --no-push)     NO_PUSH=1; shift;;
    --push-worker-ref) PUSH_WORKER_REF=1; shift;;
    --worker-ref-agent) WORKER_REF_AGENT="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --worker-ref-sid)  WORKER_REF_SID="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --dry-run)     DRY_RUN=1; shift;;
    --strict)      STRICT=1; shift;;
    -h|--help)     usage; exit 0;;
    *)             echo "[iteration-push] unknown arg: $1" >&2; shift;;
  esac
done

# log(): stderr for the live transcript AND a fail-open persisted copy at
# $GITDIR/iteration-push.log (). The 2026-08-01 cc-06 wedge was
# diagnosable only by external reflog forensics because every defer line
# lived solely in the loop transcript (the iteration-close call site
# deliberately does NOT redirect stderr — the loop LLM must see these live).
# The copy lives INSIDE .git/ on purpose: a plain repo file would be
# untracked on any deployment whose .gitignore lacks the rule, and an
# untracked non-agents/* path is exactly what makes the self-heal defer —
# the log would cause the wedge it exists to document.
log() {
  echo "[iteration-push] $*" >&2
  local _lf="${ITERATION_PUSH_LOG_FILE:-}"
  if [ -z "$_lf" ] && [ -n "${GITDIR:-}" ]; then _lf="$GITDIR/iteration-push.log"; fi
  if [ -n "$_lf" ]; then
    printf '%s %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$*" >>"$_lf" 2>/dev/null || true
  fi
}
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

# --- Integrate-defer streak escalation () ---------------------------
# The stranded-depth alarm below fires on the SYMPTOM (>= BULK_ALARM unpushed
# commits). Between push-worthy and that cap, a merge that fails every
# iteration is silent for 90+ minutes (measured: cc-06 2026-08-01, wedged
# 14:17->16:44; a prior episode sat 6.2h — see the alarm's own comment). This
# counts CONSECUTIVE integrate failures (dirty-defer AND conflict-abort — both
# strand the box identically) in $GITDIR/iteration-push-defer-streak and
# escalates at the Nth: a loud transcript banner plus a JSONL record in
# agents/<self>/health/ (merge=union — safe to write from the push path).
# Reset the moment an integrate succeeds or none is needed. State lives in
# .git/ for the same reason as the log (see log() comment). Fail-open; alarm
# only, never blocks or defers anything itself.
IP_DEFER_STREAK_ALARM="${ITERATION_PUSH_DEFER_STREAK_ALARM:-3}"
case "$IP_DEFER_STREAK_ALARM" in ''|*[!0-9]*) IP_DEFER_STREAK_ALARM=3;; esac

_ip_streak_file() { printf '%s' "${GITDIR:+$GITDIR/iteration-push-defer-streak}"; }

_ip_defer_streak_reset() {
  local f; f="$(_ip_streak_file)"
  { [ -n "$f" ] && rm -f "$f"; } 2>/dev/null || true
}

_ip_defer_streak_tick() {  # $1 = shape (dirty-defer | conflict-abort)
  local f n=0 since="" first=""
  f="$(_ip_streak_file)"
  [ -z "$f" ] && return 0
  if [ -f "$f" ]; then
    first="$(head -n 1 "$f" 2>/dev/null || echo "")"
    n="${first%% *}"; since="${first#* }"
    case "$n" in ''|*[!0-9]*) n=0; since="";; esac
    [ "$since" = "$first" ] && since=""
  fi
  n=$(( n + 1 ))
  [ -z "$since" ] && since="$(date +%Y-%m-%dT%H:%M:%S)"
  printf '%s %s\n' "$n" "$since" >"$f" 2>/dev/null || true
  if [ "$n" -ge "$IP_DEFER_STREAK_ALARM" ]; then
    local behind ahead _hint
    behind="$(git -C "$REPO" rev-list --count "$BRANCH..$UPSTREAM" 2>/dev/null || echo '?')"
    ahead="$(git -C "$REPO" rev-list --count "$UPSTREAM..$BRANCH" 2>/dev/null || echo '?')"
    # The remedy differs by shape and the two are not interchangeable
    # (guard-1985). Naming dirty-tree causes on a content conflict sends the
    # reader hunting staged entries and dirty files that are not there — and a
    # conflict is the one shape that retrying can NEVER clear on its own.
    _hint="read the defer reason above — a partner's staged entries (guard-741), index.lock contention, or dirty shared files — and clear it"
    if [ "${1:-}" = "conflict-abort" ]; then
      _hint="this is a TRUE cross-machine content conflict, NOT a dirty tree — resolve it by hand (git merge ${UPSTREAM}) and commit the resolution; retrying alone will never clear it"
    fi
    log "⚠ INTEGRATE-DEFER STREAK: ${n} consecutive integrate failure(s) (${1:-unknown}) since ${since} — behind=${behind}, ahead=${ahead}. The merge keeps failing, so this box CANNOT push (non-fast-forward) and is stranding (g-115-4484 class; every defer line is persisted in .git/iteration-push.log). ACT NOW: ${_hint}; do NOT wait for the stranded-depth alarm at ${ITERATION_PUSH_BULK_ALARM:-25} commits."
    if [ -n "${MIND_AGENT:-}" ] && [ -d "$REPO/agents/${MIND_AGENT}" ]; then
      local hd="$REPO/agents/${MIND_AGENT}/health"
      { mkdir -p "$hd" && printf '{"ts":"%s","source":"iteration-push","event":"integrate_defer_streak","streak":%s,"since":"%s","shape":"%s","behind":"%s","ahead":"%s"}\n' \
          "$(date +%Y-%m-%dT%H:%M:%S)" "$n" "$since" "${1:-unknown}" "$behind" "$ahead" >>"$hd/$(date +%F).jsonl"; } 2>/dev/null || true
    fi
  fi
  return 0
}

# --- Cross-agent-churn self-heal helper (, per ) ---------
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
#     + pathspec-limited commit (). Own agent-dir ledgers
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
# Resolved once: under STORAGE_BACKEND=local the storage root .mind-data/ lives
# INSIDE the repo and is git-tracked, so the agent's own world/meta writes make
# the tree dirty continuously. Under own-cloud world/ and meta/ are external and
# gitignored, so they can never block a merge — which is why this only matters
# for local. Same resolution shape as iteration-commit.sh ().
# THIRD FALLBACK IS LOAD-BEARING ON A FRESH CLONE (). Both config
# sources above are MACHINE-LOCAL: STORAGE_BACKEND is usually unset in a plain
# shell, and .env.local is gitignored by design (per-machine creds/paths), so a
# freshly-cloned repo on a NEW COMPUTER has neither. Without a third source this
# resolver returns "" there, every dirty .mind-data/ path takes the defer branch,
# the merge never runs, the push goes non-fast-forward, and that box strands —
# i.e. the exact failure this self-heal exists to prevent, re-armed on precisely
# the machine most likely to hit it. Verified 2026-07-29: .env.local untracked,
# STORAGE_BACKEND unset in-shell.
# The structural probe needs no config because tracking .mind-data/ IS what
# local-backend MEANS: measured 0 tracked files in the own-cloud repo (dir
# absent entirely) vs 407 in the local one. It also cannot produce a false
# "local" for own-cloud — an own-cloud repo keeps world/ and meta/ external, so
# there is nothing under .mind-data/ for git to track. Ordered last so an
# explicit setting always wins; `head -1` short-circuits the ls-files walk.
_ip_storage_backend() {
    local v="${STORAGE_BACKEND:-}"
    if [[ -z "$v" && -f "$REPO/.env.local" ]]; then
        v="$(grep -E '^[[:space:]]*STORAGE_BACKEND[[:space:]]*=' "$REPO/.env.local" 2>/dev/null \
             | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
    fi
    if [[ -z "$v" ]] && [[ -n "$(git -C "$REPO" ls-files .mind-data 2>/dev/null | head -1)" ]]; then
        v="local"
    fi
    printf '%s' "$v" | tr '[:upper:]' '[:lower:]'
}

# --- Blocking-set narrowing () ------------------------------------
# The classifier below is ALL-OR-NOTHING: ONE path outside agents/* (and
# .mind-data/* under the local backend) defers the whole tree. Scanning the
# ENTIRE dirty tree therefore lets a file that has nothing to do with the merge
# veto a heal that would otherwise succeed — and because nothing clears that
# file, the veto repeats EVERY iteration until something unrelated removes it.
# MEASURED 2026-08-01 (ZDS cc-06): merge-refused every cycle 14:17->16:44 UTC
# (~2.5h / 20+ iterations) with 14 dirty files, only some of which the merge
# actually touched.
#
# git NAMES the blocking set in its refusal message, on TAB-indented lines under
# one of two headers (both shapes measured on cc-04 / bash 5.2.21):
#   error: Your local changes to the following files would be overwritten by merge:
#   error: The following untracked working tree files would be overwritten by merge:
# and nothing else recovers it on this shape — the merge never STARTED, so
# MERGE_HEAD is absent and BOTH `git diff --name-only --diff-filter=U` and
# `git ls-files -u` return EMPTY (verified). guard-1985's probe answers the
# CONFLICT shape, not this REFUSAL shape; do not substitute one for the other.
#
# Format/locale-proof by construction rather than by matching the header text:
# a localized or unexpected message parses to an EMPTY set, and the caller's
# membership test reads empty as "every path blocks" — i.e. exactly the pre-4484
# whole-tree behaviour. The failure direction is therefore always toward today's
# conduct, never toward a wider blast radius.
# awk (not sed/grep -P) for the tab match: POSIX ERE \t is portable across
# gawk/mawk, while sed's \t is GNU-only and grep -P is not universal.
_ip_blocking_paths_from_merge_out() {
  printf '%s\n' "${MERGE_OUT:-}" \
    | awk '/^\t/ { sub(/^\t+/, ""); sub(/[[:space:]]+$/, ""); if (length($0)) print }'
}

# --- Repeated-refusal escalation: see _ip_defer_streak_tick above -----------
#  was implemented CONCURRENTLY by two agents. This half (streak
# counting on the CAUSE rather than the AHEAD>=25 consequence) landed twice;
# the duplicate `_ip_refusal_bump` mechanism was retired here in favour of
# `_ip_defer_streak_tick`, which is strictly better on four axes: it ticks on
# conflict-abort as well as dirty-defer, records `since` and behind/ahead in
# the alarm, writes to the agent health JSONL (a sink the fleet already reads)
# instead of a bespoke ledger, and keeps its state in $GITDIR — which works on
# deployments lacking the core/logs/ ignore rule, where an untracked state file
# would itself have tripped the self-heal defer it is reporting on.
# Two counters over one event would have double-alarmed, so this is subtraction,
# not loss. The surviving half of this goal's work is the blocking-set NARROWING
# above (_ip_blocking_paths_from_merge_out / _ip_blocks), which the other
# implementation does not have and which is the actual wedge fix.

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

  # The set git NAMED as blocking (). Consulted at exactly the THREE
  # sites below where scanning the whole dirty tree was over-broad — never used
  # to shrink what gets COMMITTED, only what gets VETOED or CLEARED:
  #   veto  (outside agents/*, and .mind-data/* off the local backend): a file
  #         that does not block this merge must not veto a heal that would
  #         otherwise succeed. THIS is the ~2.5h wedge.
  #   clear (agents/<other>/*): clearing discards a sibling's local churn, so
  #         restricting it to what actually blocks is strictly safer than today.
  # Self-namespace and storage-root churn keep being committed IN FULL whether
  # or not git named them — narrowing there would silently undo  /
  # , whose whole point is that own ledger churn re-dirties every tick
  # and must be preserved, not merely unblocked. (Caught by
  # test_selfheal_mixed_self_and_crossagent_heals_both, which is exactly the
  # shape: self churn dirty but NOT named by git.)
  # An unparseable/localized message leaves the set EMPTY, which every site
  # below reads as "treat every path as blocking" — i.e. exactly the pre-4484
  # behaviour, so this can only ever narrow, never widen.
  local -A _blocking=()
  local _blocking_n=0 b
  while IFS= read -r b; do
    [ -n "$b" ] && { _blocking["$b"]=1; _blocking_n=$(( _blocking_n + 1 )); }
  done < <(_ip_blocking_paths_from_merge_out)
  if [ "$_blocking_n" -gt 0 ] && [ "$_blocking_n" -lt "$total" ]; then
    log "self-heal: git named ${_blocking_n} of ${total} dirty path(s) as blocking this merge — the rest neither veto nor get touched (g-115-4484)"
  fi

  # Classify the dirty tree: agents/<self>/* is committed, agents/<other>/* is
  # cleared when it blocks, and a BLOCKING path outside agents/* defers the whole
  # tree untouched (never clear or commit core/world/shared work from the push
  # path). Still all-or-nothing — over the blocking set rather than over every
  # file that happens to be dirty.
  local rel name
  local self_paths=() cross_dirty=() cross_untracked=() storage_paths=()
  local _backend; _backend="$(_ip_storage_backend)"
  # blocks(): does git say THIS path blocks the merge? An empty blocking set
  # (unparseable message) answers yes for everything — the pre-4484 behaviour.
  _ip_blocks() { [ "$_blocking_n" -eq 0 ] || [ -n "${_blocking[$1]:-}" ]; }
  for rel in "${dirty[@]}"; do
    case "$rel" in
      agents/*)
        name="${rel#agents/}"; name="${name%%/*}"
        if [ "$name" = "$self" ]; then
          self_paths+=("$rel")                       # commit ALL self churn ()
        elif _ip_blocks "$rel"; then
          cross_dirty+=("$rel")                      # clear ONLY what blocks
        fi
        ;;
      .mind-data/*)
        # Storage root under local backend = THIS agent's own world/meta writes
        # (). COMMIT it like self-namespace churn — never `checkout --`
        # it, which would DISCARD encoded knowledge. Deferring instead is what
        # stranded a live agent: its tree is dirty with .mind-data writes on
        # essentially every tick, so once a second box pushed and a merge became
        # required, the merge deferred every iteration -> no merge -> no push
        # (non-fast-forward) -> 10 unpushed commits and climbing. Under own-cloud
        # this arm never fires: world/ and meta/ are external and gitignored.
        if [ "$_backend" = "local" ]; then storage_paths+=("$rel")
        elif _ip_blocks "$rel"; then
          log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
          return 1
        fi
        ;;
      *)
        if _ip_blocks "$rel"; then
          log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
          return 1
        fi
        ;;
    esac
  done
  for rel in "${untracked[@]}"; do
    case "$rel" in
      agents/*)
        name="${rel#agents/}"; name="${name%%/*}"
        if [ "$name" = "$self" ]; then
          self_paths+=("$rel")
        elif _ip_blocks "$rel"; then
          cross_untracked+=("$rel")
        fi
        ;;
      .mind-data/*)
        if [ "$_backend" = "local" ]; then storage_paths+=("$rel")
        elif _ip_blocks "$rel"; then
          log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
          return 1
        fi
        ;;
      *)
        if _ip_blocks "$rel"; then
          log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
          return 1
        fi
        ;;
    esac
  done

  # SELF-namespace churn: COMMIT it, never clear, never defer on it
  # (). guard-741/836-safe here because (a) the staged-index check
  # above proved the shared index holds ZERO staged entries, (b) staging is by
  # EXPLICIT self-namespace paths only, and (c) the commit is pathspec-limited
  # to agents/<self>/ — a partner's stage racing in between (a) and the commit
  # is excluded from it. Same namespace scope iteration-commit.sh's filter
  # enforces — NOT a bare `git commit` (which would absorb anything staged).
  # Stage self-namespace AND (local backend only) storage-root churn together;
  # the commit stays pathspec-limited to exactly the namespaces we classified,
  # so a partner's racing stage is still excluded (guard-741/836).
  local _heal_stage=() _heal_spec=()
  [ "${#self_paths[@]}" -gt 0 ]    && { _heal_stage+=("${self_paths[@]}");    _heal_spec+=("agents/$self/"); }
  [ "${#storage_paths[@]}" -gt 0 ] && { _heal_stage+=("${storage_paths[@]}"); _heal_spec+=(".mind-data/"); }
  if [ "${#_heal_stage[@]}" -gt 0 ]; then
    # Keep the ORIGINAL wording when there is no storage churn: the 
    # regression tests assert on it, and with storage_paths empty the extended
    # phrasing would also be inaccurate. Only widen the line when the local
    # backend actually contributed storage-root paths.
    if [ "${#storage_paths[@]}" -gt 0 ]; then
      log "self-heal: committing ${#self_paths[@]} self-namespace + ${#storage_paths[@]} storage-root file(s) pre-merge (g-115-2249/g-115-3877)"
    else
      log "self-heal: committing ${#self_paths[@]} SELF-namespace file(s) pre-merge (g-115-2249)"
    fi
    if ! git -C "$REPO" add -- "${_heal_stage[@]}" 2>/dev/null; then
      log "self-heal: git add of self-namespace churn failed — defer"
      return 1
    fi
    # Commit SUBJECT is likewise unchanged when no storage churn was classified
    # ( asserts on it, and it would be inaccurate otherwise).
    local _heal_msg="chore($self): pre-merge self-namespace churn (iteration-push self-heal, g-115-2249)"
    if [ "${#storage_paths[@]}" -gt 0 ]; then
      _heal_msg="chore($self): pre-merge self+storage churn (iteration-push self-heal, g-115-2249/g-115-3877)"
    fi
    if ! git -C "$REPO" commit -q -m "$_heal_msg" \
         -- "${_heal_spec[@]}" 2>/dev/null; then
      # Unstage what we staged so a failed heal leaves the index as found.
      git -C "$REPO" reset -q -- "${_heal_spec[@]}" 2>/dev/null || true
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
  # Retry still failed — TWO distinct shapes, and they need different diagnoses
  # (guard-1985). If MERGE_HEAD exists the retry actually STARTED and hit a true
  # content conflict: the churn WAS healed, so the caller's dirty-defer guidance
  # ("a partner's staged entries / index.lock contention / dirty shared files")
  # names none of the cause, and its companion line explicitly rules OUT the one
  # shape that did occur ("NOT ... cross-agent churn (auto-cleared)"). Signal it
  # separately with rc=2 so the caller ticks the right streak shape and prints
  # the conflict guidance the FIRST-merge path already prints for this shape.
  if [ -f "$GITDIR/MERGE_HEAD" ]; then
    git -C "$REPO" merge --abort >/dev/null 2>&1 || true
    log "self-heal: churn healed, but the merge retry hit a TRUE content conflict (rc=${MERGE_RC}) — aborted cleanly"
    return 2
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
        _ip_defer_streak_tick "conflict-abort"
        soft_exit 1
      fi
      # Dirty tree — merge refused before starting (MERGE_HEAD absent). Try the
      # narrow cross-agent-churn self-heal (): if the entire blocking
      # set is unstaged agents/<other>/* churn, clear it and retry the merge
      # once. Any other shape (staged entries, self/core/world dirty) DEFERS —
      # exactly the pre-1843 behaviour (log + soft_exit 1).
      _selfheal_rc=0
      _selfheal_cross_agent_churn_remerge || _selfheal_rc=$?
      if [ "$_selfheal_rc" -eq 0 ]; then
        log "integrated ${BEHIND} origin commit(s) into $BRANCH (after churn self-heal, g-115-1843/g-115-2249)"
      elif [ "$_selfheal_rc" -eq 2 ]; then
        # The self-heal WORKED and the retry then hit a true content conflict.
        # Same shape the first-merge branch above handles — print the same
        # guidance and tick the same streak shape, or the alarm sends the reader
        # hunting staged entries and dirty shared files that are not there.
        log "MERGE CONFLICT with $UPSTREAM (surfaced by the churn self-heal retry) — aborted cleanly, will retry next iteration."
        log "If this repeats every iteration it is a TRUE cross-machine content conflict (MERGE_HEAD was created — NOT the dirty-tree defer shape): resolve manually (git merge $UPSTREAM) or investigate which store conflicted."
        _ip_defer_streak_tick "conflict-abort"
        soft_exit 1
      else
        # `tail -n 1` lands on git's trailing "Updating <a>..<b>" line, which says
        # nothing about WHY (measured  — every defer in a 4-run repro
        # reported only that). Name the paths git actually blocked on, reusing the
        # set the narrowing above already parses; fall back to the old last-line
        # form when the message is unparseable.
        _defer_paths="$(_ip_blocking_paths_from_merge_out | tr '\n' ' ')"
        [ -z "$_defer_paths" ] && _defer_paths="$(printf '%s' "$MERGE_OUT" | tail -n 1)"
        log "merge DEFERRED (rc=${MERGE_RC}) — git blocked on: ${_defer_paths}"
        log "remaining defer shapes: a partner's staged index entries (guard-741) or dirty core/world/shared files — NOT self churn (auto-committed) or cross-agent churn (auto-cleared); retry next iteration"
        # Count the CAUSE, not the consequence (): this shape re-defers
        # every iteration on its own, so a streak is the earliest reliable signal
        # of a wedge — the stranded-depth alarm below only fires once the backlog
        # has already grown past its cap. The defer line ABOVE names the blocking
        # paths, which is the "read the defer reason above" the alarm points at.
        _ip_defer_streak_tick "dirty-defer"
        soft_exit 1
      fi
    else
      log "integrated ${BEHIND} origin commit(s) into $BRANCH"
    fi
  fi
fi

# Integrate did not defer this iteration (merged clean, self-healed, or none
# was needed) — clear the consecutive-failure streak (). Skipped in
# dry-run: nothing was proven, so a real streak must survive it.
[ "$DRY_RUN" = 1 ] || _ip_defer_streak_reset

# --no-push: the fetch+integrate above IS the whole job. Exit before the push
# decision (). This is the session-start continuity pull for
# local-backend deployments, where git — not S3 — is the sync mechanism, so
# owncloud-pull.sh routes here instead of no-opping. It must not push: a session
# that publishes local state merely by STARTING would make /start a write, which
# breaks reader mode's side-effect-free contract and would surprise an assistant
# session that opened a terminal to look at something.
#
# Placed at the integrate/push seam rather than guarding each push site, so a
# future push branch cannot be added below and silently escape the flag. Exiting
# HERE rather than gating the push call also leaves the stranded-depth alarm, the
# min-commits/max-age thresholds, and the push retry/recovery path completely
# untouched for the loop's normal caller.
#
# Converged from two independent implementations of  (alpha on another
# box, bravo here) that landed the same guard at the same seam — see the merge
# commit for why the duplicate happened.
if [ "$NO_PUSH" = 1 ]; then
  log "--no-push: fetch+integrate complete, skipping push decision"
  soft_exit 0
fi

# --push-worker-ref: the WORKER's carrier (). Deliberately placed at the
# SAME integrate/push seam as --no-push above, for the reason that comment gives:
# the seam is the one place a push mode cannot be added below and silently escape
# the shared-branch guard. This mode exits here too, so it never reaches the
# min-commits / max-age throttle, the stranded-depth alarm, or the shared-branch
# push — none of which apply to a single-writer ref.
#
# WHY THIS IS NOT A VIOLATION OF THE WORKER NO-PUSH RULE: that rule's stated
# rationale is contention on shared store files, so that two Bodies of one agent
# never fight over the same tree. refs/workers/<agent>/<sid> has exactly ONE
# writer by construction — the sid IS the body — so the rationale does not reach
# it. The shared branch remains the reducer's alone; this mode never touches it.
#
# MEASURED ON A REAL WORKER BOX before this was written (cc-07, uname -r
# 6.8.0-136-generic, 2026-08-08), because the design's own blocking unknowns said
# not to infer either from elsewhere: (1) push credentials — the Mind remote is
# SSH (git@github.com:...), `git ls-remote` rc=0, so the box authenticates; note
# /root/.git-credentials exists but is the HTTPS store for product repos and is
# NOT what authorises this. (2) ref acceptance — a real push of an already-remote
# sha to refs/workers/<agent>/<sid>/probe returned rc=0, was readable back via
# ls-remote, and deleted rc=0 leaving zero residue. Branch protection does not
# reach refs/workers/*. A --dry-run was NOT treated as sufficient: server-side ref
# hooks fire on a real push, so dry-run exercises a different path.
if [ "$PUSH_WORKER_REF" = 1 ]; then
  if [ -z "$WORKER_REF_AGENT" ] || [ -z "$WORKER_REF_SID" ]; then
    log "--push-worker-ref: REFUSED — agent/sid unresolved (MIND_AGENT='$WORKER_REF_AGENT', MIND_SID='$WORKER_REF_SID')."
    log "  A ref missing either segment would collide across bodies, which is the one property this carrier exists to guarantee."
    soft_exit 1
  fi
  WREF="refs/workers/${WORKER_REF_AGENT}/${WORKER_REF_SID}"
  if [ "$DRY_RUN" = 1 ]; then
    log "--push-worker-ref (dry-run): would push HEAD -> $WREF"
    soft_exit 0
  fi
  # No --force. The ref only ever advances for a given body (HEAD moves forward
  # through commits and merges), so a non-fast-forward here means an assumption
  # broke — single-writer, or a reset — and it should be LOUD rather than
  # silently overwritten.
  if git -C "$REPO" push origin "HEAD:$WREF" >/dev/null 2>&1; then
    log "--push-worker-ref: pushed HEAD ($(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)) -> $WREF"
    soft_exit 0
  fi
  log "--push-worker-ref: push FAILED for $WREF — this Body's framework edits and local commits have NOT reached the reducer"
  soft_exit 1
fi

# Commits ahead of origin (shared local ref; agents are the only pushers).
AHEAD="$(git -C "$REPO" rev-list --count "$UPSTREAM..$BRANCH" 2>/dev/null || echo 0)"
case "$AHEAD" in ''|*[!0-9]*) AHEAD=0;; esac   # force numeric
if [ "$AHEAD" -eq 0 ]; then
  log "origin/$BRANCH up to date (0 ahead) — nothing to push"; soft_exit 0
fi

# Age (minutes) of the OLDEST unpushed commit. Feeds BOTH the push throttle
# below and the stranded-depth alarm's duration. Hoisted above the alarm
# (): it used to be derived AFTER it and consumed only by the
# throttle, so the alarm could report "54 commits" but never "6.2 hours" — and
# duration is what separates a busy iteration from a wedge. cc-06 sat at
# AHEAD=54 (more than double the cap) for 6.2h with this alarm firing every
# single iteration and nobody acting on it. The accumulation was NOT silent;
# the count simply did not convey urgency the way an elapsed time does.
OLDEST_CT="$(git -C "$REPO" log "$UPSTREAM..$BRANCH" --format=%ct 2>/dev/null | tail -n 1 || echo "")"
AGE_MIN=0
case "$OLDEST_CT" in
  ''|*[!0-9]*) AGE_MIN=0;;
  *) NOW_CT="$(date +%s)"; AGE_MIN=$(( (NOW_CT - OLDEST_CT) / 60 )); [ "$AGE_MIN" -lt 0 ] && AGE_MIN=0;;
esac

# Stranded-depth alarm ( user correction). A push-blocked window
# (read-only deploy key rb-3236/guard-1021, disabled push, repeated fail-soft)
# lets AHEAD grow silently — 121 () then 281 by 2026-07-16, and the
# eventual bulk unwedge push carried a stale store base that transiently
# regressed world/aspirations.jsonl by ~184 goals (). Once depth
# crosses the cap, bang the drum EVERY iteration until it drains: the banner
# lands in iteration-close stdout where the loop LLM must act on it (fix the
# push pipe or notify the user — never let depth keep growing). Alarm only;
# never blocks or defers the push itself.
BULK_ALARM="${ITERATION_PUSH_BULK_ALARM:-25}"
case "$BULK_ALARM" in ''|*[!0-9]*) BULK_ALARM=25;; esac
if [ "$AHEAD" -ge "$BULK_ALARM" ]; then
  log "⚠ STRANDED-DEPTH ALARM: ${AHEAD} unpushed commit(s) >= ${BULK_ALARM} on ${BRANCH}, oldest $(( AGE_MIN / 60 ))h$(( AGE_MIN % 60 ))m old — bulk-push side-effect risk (stale store bases, g-115-2362 class). ACT NOW: if pushes are failing, fix the credential/remote (rb-3236/guard-1021); if the INTEGRATE is wedged on a merge conflict that ALSO blocks the push (non-fast-forward), read the integrate log above and resolve it — that is the g-115-4253 shape; if push is deliberately disabled, notify the user of the growing backlog. Do NOT let depth grow to another 281-commit unwedge (g-115-2398)."
fi

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

# --- Push-race recovery (, rb-3970 phantom window) -----------------
# A race-shaped rejection here means origin advanced AFTER the fetch/merge
# above — or the THROTTLED fetch used a stale tracking ref, so the integrate
# step saw BEHIND=0 and skipped the merge entirely. "Retry next iteration"
# COMPOUNDS under the throttle: the next run finds FETCH_HEAD fresh, throttles
# again, computes BEHIND against the SAME stale ref, skips the merge, and
# fails the identical push — up to FETCH_INTERVAL_MIN of repeated failures
# while a deep-close commit sits stranded local-only (the goal-status-vs-origin
# phantom window; both 2026-07-18 phantoms rode this shape, and 
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
