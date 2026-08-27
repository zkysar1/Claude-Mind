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

# --- Network bound for every push () -------------------------------
# Each `git push` below is a NETWORK call that carried NO bound of its own. git
# has no push-side equivalent of http.lowSpeedLimit on the ssh transport, and an
# effective ssh config with `serveraliveinterval 0` (the default, and what
# `ssh -G github.com` reports on these boxes) means keepalives are OFF — so a
# half-open connection hangs until the kernel gives up, which is tens of
# minutes. Measured on ZDS-Mind 2026-08-26: one stalled push blocked the
# autonomous loop for 681s and would NOT have self-recovered.
#
# TWO bounds, because neither reaches every stall on its own:
#   ssh keepalive — detects a DEAD PEER (answers nothing) in KEEPALIVE_S *
#                   KEEPALIVE_MAX seconds. Set HERE rather than in a
#                   machine-local ~/.ssh/config so it TRAVELS with the repo to
#                   every box; covers the fetches below for free as a result.
#   timeout(1)    — bounds wall-clock against EVERY stall cause, including an
#                   application-level wedge where the peer still answers
#                   keepalives — which the keepalive by construction cannot see.
# The keepalive alone would leave that second case unbounded; the timeout alone
# would not protect fetch. Hence both.
#
# A bounded failure is deliberately NOT special-cased: rc=124 falls through the
# same "Auth / network / non-race shapes" branch as any other failed push and
# soft_exit 1s — i.e. it degrades to retry-next-iteration, which is exactly the
# existing fail-soft contract.
IP_PUSH_TIMEOUT_S="${ITERATION_PUSH_TIMEOUT_S:-120}"   # vs a measured ~0.7s normal round-trip
IP_SSH_KEEPALIVE_S="${ITERATION_PUSH_SSH_KEEPALIVE_S:-15}"
IP_SSH_KEEPALIVE_MAX="${ITERATION_PUSH_SSH_KEEPALIVE_MAX:-4}"
# Deliberately word-split at each call site: expands to `timeout <n>`, or to
# NOTHING where timeout(1) is absent (BSD/macOS/git-bash), which preserves
# today's unbounded-but-working behaviour instead of failing the push outright.
if command -v timeout >/dev/null 2>&1; then
  IP_TMO="timeout ${IP_PUSH_TIMEOUT_S}"
else
  IP_TMO=""
  # Announced, never silent: an unbounded box is the exact condition this guard
  # exists to remove, and a silent fallback is indistinguishable from success.
  echo "[iteration-push] NOTE: timeout(1) unavailable — pushes bounded by ssh keepalive only" >&2
fi
# Never clobber a caller-supplied GIT_SSH_COMMAND (custom key, proxy, jump host).
if [ -z "${GIT_SSH_COMMAND:-}" ]; then
  export GIT_SSH_COMMAND="ssh -o ServerAliveInterval=${IP_SSH_KEEPALIVE_S} -o ServerAliveCountMax=${IP_SSH_KEEPALIVE_MAX}"
fi

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

# Skip if a CO-RESIDENT BODY holds the working tree (). Sibling of the
# index.lock skip above and deliberately placed beside it: same semantics ("some
# other process is mid-operation on this tree; come back next cycle"), different
# duration. index.lock covers the INSTANT of one git command; this covers the
# WINDOW a Body needs — the ~32 minutes of a suite run, or a unit spent between
# an edit and its commit. Both measured collisions on 2026-08-21 happened with
# index.lock free, through entirely legal git operations.
#
# ONE CHOKEPOINT, NOT ONE PER MERGE. This script has THREE `git merge` sites
# (the self-heal retry, the integrate, and the push-race recovery) plus a
# pathspec-limited self-heal COMMIT. Gating the integrate alone would have left
# the recovery merge — reached precisely when the push was rejected, i.e. when
# contention is highest — as an unguarded bypass (guard-4088 / guard-3448: a
# gate at one caller is not a gate). Skipping the whole invocation here covers
# every one of them by construction and needs no re-indentation.
#
# FAIL-OPEN IN EVERY AMBIGUOUS CASE. `tree-lock.sh check` returns 1 ONLY for a
# present, parseable, unexpired lock held by a DIFFERENT sid whose holder
# process is not provably dead; absent, unreadable, expired, mine and
# dead-holder all return 0. The missing-script guard below adds one more: a
# clone without the helper behaves exactly as before. This matters more than
# usual here — a wrongly-refusing gate does not cost one merge, it silently
# freezes framework sync for the box, and "resume on local code" becomes
# permanent staleness (the  /  wedge shape).
# ONLY rc=1 SKIPS. `if ! cmd` would treat EVERY non-zero as "held", and the
# wrapper can exit non-zero for reasons that are not a lock at all: it runs
# `set -euo pipefail` over `source _paths.sh` and `exec python3`, so a broken
# _paths.sh or a missing interpreter exits 1-or-127 with no lock in sight. Those
# are plumbing faults, and treating a plumbing fault as a held tree would freeze
# this box's framework sync indefinitely — the exact fail-CLOSED behaviour the
# comment above promises this gate does not have (guard-1562: stopping a healthy
# loop on a plumbing fault is worse than the disease). `check` is contracted to
# return 1 for held and 0 for everything else, and never 2; anything else is a
# fault, so it is reported LOUDLY and then proceeds.
if [ -f "$SCRIPT_DIR/tree-lock.sh" ]; then
  # --project-root "$REPO" scopes the lock to the tree this invocation actually
  # merges, which is the only tree the gate is about. Without it the check reads
  # the lock of whatever repo the SCRIPT lives in, so a hermetic --repo run
  # (test_iteration_push.py builds tmp origin/clone repos) would consult THIS
  # machine's real lock and skip whenever any co-resident Body happened to hold
  # it — a suite that passes or fails on unrelated global state. tree_lock.py
  # .resolve()s whatever root it is handed, so this logical path and the
  # suite runner's __file__-derived one name the same lock file.
  _TL_OUT="$(bash "$SCRIPT_DIR/tree-lock.sh" check --project-root "$REPO" 2>&1)"; _TL_RC=$?
  if [ "$_TL_RC" -eq 1 ]; then
    log "tree-lock: a co-resident Body holds this working tree — skip, retry next iteration. ${_TL_OUT}"
    soft_exit 0
  elif [ "$_TL_RC" -ne 0 ]; then
    log "tree-lock: check failed rc=${_TL_RC} (plumbing, not a lock) — proceeding unguarded. ${_TL_OUT}"
  fi
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

#  (wedge shape B): the paths that made THIS iteration's dirty-defer a
# durable-cross-agent defer, space-separated, or empty. Script-scope on purpose —
# `_heal_defer` is `local` to _selfheal_cross_agent_churn_remerge and the shape is
# needed at the tick site ~350 lines later. Safe because that function is invoked
# as `_selfheal_cross_agent_churn_remerge || _selfheal_rc=$?` in the CURRENT shell
# (verified, not assumed — a subshell call would silently drop every assignment).
_IP_DEFER_DURABLE=""

_ip_streak_file() { printf '%s' "${GITDIR:+$GITDIR/iteration-push-defer-streak}"; }

_ip_defer_streak_reset() {
  local f; f="$(_ip_streak_file)"
  # Both lane markers, or the surviving one silently suppresses the NEXT streak
  # of its shape ( — the defer lane keeps its own marker so a streak
  # that changes shape still escalates).
  { [ -n "$f" ] && rm -f "$f" "${f}-escalated" "${f}-escalated-defer"; } 2>/dev/null || true
}

# Capture WHICH paths conflicted, BEFORE `git merge --abort` destroys the
# evidence (). Every conflict-abort site below logged "investigate
# which store conflicted" and then immediately aborted, discarding the only
# state that could answer it. Measured over 2026-08-01..08-18 on cc-02: 388
# integrate attempts, 5 conflict events in 4 incidents — and ZERO of them
# attributable to a store, so "agent-ledger conflict count over the window"
# was not measurable AT ALL from this log. The instrument named the question
# and destroyed its own answer one line earlier.
#
# Resolving each path's merge driver in the same breath is the point, not
# decoration: a conflicted path reading `merge=unspecified` IS the driver gap,
# named at the moment it actually bites, which is the one moment it is not a
# guess. `--diff-filter=U` is valid ONLY in the MERGE_HEAD-present shape
# (guard-1985) — every caller is already inside that branch, and the
# empty-result line below keeps a silent probe from reading as "no paths".
_ip_log_conflict_paths() {
  local paths n p drv line=""
  paths="$(git -C "$REPO" diff --name-only --diff-filter=U 2>/dev/null)"
  if [ -z "$paths" ]; then
    log "conflicted paths: NONE REPORTED — unmerged-path probe came back empty (not the same as 'no conflict'; see guard-1985)"
    return 0
  fi
  n="$(printf '%s\n' "$paths" | grep -c . || true)"
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    drv="$(git -C "$REPO" check-attr merge -- "$p" 2>/dev/null | sed 's/.*: merge: //')"
    line="${line}${line:+, }${p} (merge=${drv:-unknown})"
  done < <(printf '%s\n' "$paths" | head -12)
  log "conflicted paths (${n}): ${line}"
}

_ip_defer_streak_tick() {  # $1 = shape (dirty-defer | conflict-abort), $2 = blocking paths (defer lanes)
  local f n=0 since="" first="" _rest=""
  f="$(_ip_streak_file)"
  [ -z "$f" ] && return 0
  if [ -f "$f" ]; then
    first="$(head -n 1 "$f" 2>/dev/null || echo "")"
    # 3-field format since : "n since shape" (older 2-field files
    # parse identically — the shape field is simply absent).
    n="${first%% *}"; _rest="${first#* }"; since="${_rest%% *}"
    case "$n" in ''|*[!0-9]*) n=0; since="";; esac
    [ "$since" = "$first" ] && since=""
  fi
  n=$(( n + 1 ))
  [ -z "$since" ] && since="$(date +%Y-%m-%dT%H:%M:%S)"
  printf '%s %s %s\n' "$n" "$since" "${1:-unknown}" >"$f" 2>/dev/null || true
  # --- Repeating-conflict escalation directive () -------------------
  # A conflict-abort is the ONE shape retrying can never clear (the hint below
  # says so), yet every caller of this script is fail-soft — a worker Body ran
  # 85 commits behind on cc-08 retrying an identical conflict every cycle,
  # which CONCEALED the peer fix sitting behind the blocked merge. At the Kth
  # consecutive conflict (default 2), print an explicit caller-facing directive
  # ONCE per streak (marker file keyed on `since`; reset clears it). Fail-open:
  # a marker write failure just re-prints next cycle, which is harmless.
  local IP_CONFLICT_ESCALATE="${ITERATION_PUSH_CONFLICT_ESCALATE:-2}"
  case "$IP_CONFLICT_ESCALATE" in ''|*[!0-9]*) IP_CONFLICT_ESCALATE=2;; esac
  if [ "${1:-}" = "conflict-abort" ] && [ "$n" -ge "$IP_CONFLICT_ESCALATE" ]; then
    local mk="${f}-escalated" prev_since=""
    [ -f "$mk" ] && prev_since="$(head -n 1 "$mk" 2>/dev/null || echo "")"
    if [ "$prev_since" != "$since" ]; then
      local cbehind
      cbehind="$(git -C "$REPO" rev-list --count "$BRANCH..$UPSTREAM" 2>/dev/null || echo '?')"
      log "═══ REPEATING MERGE CONFLICT — ESCALATION REQUIRED (g-306-315) ═══"
      log "The last ${n} integrate attempts since ${since} ALL hit a TRUE content conflict (behind=${cbehind}). Retrying can NEVER clear this and every peer fix stays concealed behind the blocked merge. CALLER MUST ESCALATE NOW: (1) post a coordination-board escalation naming the conflicted path(s) from the merge output above + behind=${cbehind} + this box; (2) worker Body: append an sq-013 observation to spark_capture so the reducer files an Unblock — reducer: file the Unblock directly. Then continue the cycle on local code; do NOT hand-resolve mid-goal and do NOT stop the loop."
      printf '%s\n' "$since" >"$mk" 2>/dev/null || true
    fi
  fi
  # --- Repeating-DEFER escalation directive () ---------------------
  # THE ASYMMETRY THIS CLOSES. A repeating dirty-file DEFER strands the box
  # exactly as a repeating conflict does — retry can never clear it, because the
  # blocking condition is not transient — yet only the conflict lane emitted a
  # caller-facing directive. Measured cc-08 2026-08-20: two consecutive defers
  # on a dirty repo-root blocker-gate-overrides.jsonl, 39 commits behind and
  # climbing, ZERO escalation, while origin/main ALREADY CARRIED the fix the box
  # was refusing to integrate. Self-blocking, and silent.
  #
  # The ⚠ streak WARNING below is not a substitute and never was: it starts at
  # 3 (the conflict lane escalates at 2), it re-prints EVERY cycle rather than
  # once per streak, and no caller greps for it — worker-loop Phase -0.3
  # branches on the "— ESCALATION REQUIRED (g-" headline alone. A log line with
  # no consumer is indistinguishable from silence at the layer that matters.
  #
  # DELIBERATELY A SEPARATE HEADLINE FROM THE CONFLICT LANE (guard-2586: a
  # fallback path and a failure path must never emit the same message). The two
  # remedies are opposite — a conflict is hand-resolved, a dirty defer is
  # CLEARED, and for the durable-crossagent sub-shape clearing is the one
  # forbidden action. Callers that want both match the shared "— ESCALATION
  # REQUIRED (g-" tail, which is anchored enough not to collide with prose.
  # Its own marker file too, so a streak that changes shape still escalates the
  # new shape instead of being suppressed by the old one's marker.
  if [ "${1:-}" != "conflict-abort" ] && [ "$n" -ge "$IP_CONFLICT_ESCALATE" ]; then
    local dmk="${f}-escalated-defer" dprev=""
    [ -f "$dmk" ] && dprev="$(head -n 1 "$dmk" 2>/dev/null || echo "")"
    if [ "$dprev" != "$since" ]; then
      local dbehind dpaths
      dbehind="$(git -C "$REPO" rev-list --count "$BRANCH..$UPSTREAM" 2>/dev/null || echo '?')"
      dpaths="${2:-}"
      [ -z "$dpaths" ] && dpaths="(not recorded — read the 'git blocked on:' line above)"
      log "═══ REPEATING INTEGRATE DEFER — ESCALATION REQUIRED (g-115-6934) ═══"
      log "The last ${n} integrate attempts since ${since} ALL deferred on the SAME non-transient blocker (shape=${1:-unknown}, behind=${dbehind}). Blocking path(s): ${dpaths}. Retrying can NEVER clear this and every peer fix stays concealed behind the blocked merge — origin may ALREADY carry the fix this box is refusing to integrate. CALLER MUST ESCALATE NOW: (1) post a coordination-board escalation naming those path(s) + behind=${dbehind} + this box; (2) worker Body: append an sq-013 observation to spark_capture so the reducer files an Unblock — reducer: file the Unblock directly. Then continue the cycle on local code. For shape=dirty-defer the remedy is to CLEAR the named path(s); for shape=durable-crossagent-defer clearing DESTROYS a partner's unpushed work (g-115-6145) — follow the sanctioned superset-proof procedure in the streak hint below instead."
      printf '%s\n' "$since" >"$dmk" 2>/dev/null || true
    fi
  fi
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
    elif [ "${1:-}" = "durable-crossagent-defer" ]; then
      #  (wedge shape B). The dirty-defer hint above prescribes "clear
      # it", which for THIS shape is the one FORBIDDEN action —  defers
      # precisely to protect a partner's unpushed divergence in an identity file
      # or the learning archive, so clearing destroys exactly what the defer was
      # defending. Three operators on two boxes derived the procedure below by
      # hand because no message anywhere stated it; one box stranded 92 commits.
      # Reaching here also means git has NO commutative driver for the path (the
      #  arm commits those before the defer), so waiting cannot help:
      # nothing between the two refusals will ever resolve it.
      _hint="DURABLE cross-agent file(s) differing from BOTH HEAD and ${UPSTREAM}, with no commutative git merge driver: ${_IP_DEFER_DURABLE:-see the defer line above}. Do NOT clear them — that DISCARDS a partner's unpushed work (g-115-6145) — and do NOT wait, as this shape never self-clears. SANCTIONED PROCEDURE (hand-derived 3x, g-115-6632): prove the local side safe by comparing record-id SETS across local / HEAD / ${UPSTREAM} — safe when local is a SUPERSET, or when the whole delta is one field rolling across a recurring occurrence with local the newer side — then COMMIT the partner file with that proof in the message and let the merge reconcile it. If you cannot prove it, post a coordination-board escalation naming the path(s) and leave the file untouched; never clear on a hunch"
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
# absent entirely) vs 407 in the local one. Ordered last so an explicit setting
# always wins; `head -1` short-circuits the ls-files walk.
#
# THE "cannot produce a false local for own-cloud" CLAIM THAT USED TO SIT HERE
# IS RETIRED (). It rested on "an own-cloud repo keeps world/ and
# meta/ external, so there is nothing under .mind-data/ for git to track", and
# that stopped being true on 2026-07-28 when .mind-data/ became git-tracked on
# own-cloud deployments (see .gitignore's MACHINE-LOCAL note, which even keeps
# world+meta changelog.jsonl deliberately TRACKED). The resolver itself is still
# correct — it returns own-cloud there, which is the truth — but BACKEND STOPPED
# BEING A PROXY FOR TRACKEDNESS, and the classifier below was reading it as one.
# Use _ip_mind_data_tracked() for the "is git responsible for this path" question.
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

# Is git RESPONSIBLE for .mind-data/ in this repo? ()
# This is the question the .mind-data/* classifier arm actually needs, and it is
# NOT the same question as the backend. A path that is gitignored can never be
# dirty (the dirty set comes from `ls-files --others --exclude-standard` plus the
# tracked diff), so if git tracks nothing here the arm is unreachable and this
# returns false harmlessly. If git DOES track it, then a dirty file there is this
# box's own write to a path the repo deliberately backs up — commit it, exactly
# as the local backend already does, rather than deferring forever on it.
# Memoized: the answer cannot change inside one run, and `head -1` short-circuits.
_IP_MD_TRACKED=""
_ip_mind_data_tracked() {
    if [ -z "$_IP_MD_TRACKED" ]; then
        if [ -n "$(git -C "$REPO" ls-files .mind-data 2>/dev/null | head -1)" ]; then
            _IP_MD_TRACKED=yes
        else
            _IP_MD_TRACKED=no
        fi
    fi
    [ "$_IP_MD_TRACKED" = yes ]
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

# : prove SEMANTIC identity between two git-resolvable versions of a
# path, so byte-level serialization churn is not mistaken for a partner's work.
#
# WHY THIS EXISTS. Everything below decides between RESTORING a file and
# DEFERRING the merge, and the only predicate it had was "git says this path
# differs". That cannot distinguish a partner's real uncommitted work from
# key-order churn by a re-serializing JSON writer or CRLF churn from a Windows
# box. On an own-cloud fleet the churn case is the common one and it NEVER
# self-clears — the sync layer re-churns the file, so every later iteration
# defers again. Measured on cc-03 2026-08-10: 3 consecutive defers, 49 commits
# behind, unable to push, on two files whose parsed content was byte-for-byte
# equal to HEAD (672 vs 672 record ids, zero records differing on any field).
#
# Echoes exactly one of: identical | different | unparseable.
# ONLY `identical` licenses a restore. Both other verdicts mean defer, and the
# asymmetry is the whole design: a wrong `different` costs one retried merge,
# a wrong `identical` destroys a partner's uncommitted work permanently.
#
# An EMPTY revspec prefix means the WORKING TREE, which is read from disk — not
# via `git show`, because `git show <path>` resolves a rev/object and does not
# read the worktree at all. Getting that wrong would compare the wrong bytes
# and could license a restore over a file nobody had actually compared.
_ip_semantic_verdict() {
  local _lpfx="$1" _rpfx="$2" _rel="$3" _lf _rf _v
  _lf="$(mktemp 2>/dev/null)" || { echo unparseable; return 0; }
  _rf="$(mktemp 2>/dev/null)" || { rm -f "$_lf"; echo unparseable; return 0; }
  _ip_extract_side() {   # $1 = prefix, $2 = destination
    if [ -z "$1" ]; then cat -- "$REPO/$_rel" > "$2" 2>/dev/null
    else git -C "$REPO" show "$1$_rel" > "$2" 2>/dev/null; fi
  }
  # A missing side (added/deleted path) is NOT provably identical -> defer.
  if ! _ip_extract_side "$_lpfx" "$_lf" || ! _ip_extract_side "$_rpfx" "$_rf"; then
    rm -f "$_lf" "$_rf"; echo unparseable; return 0
  fi
  # $SCRIPT_DIR, never "$REPO/core/scripts": $REPO is the repo being PUSHED,
  # which --repo can point anywhere (in tests it is a bare temp clone with no
  # core/ tree at all). Resolving the comparator against the target repo made
  # every verdict `unparseable` -> defer, i.e. the fix silently did nothing.
  _v="$(python3 "$SCRIPT_DIR/semantic_identity.py" \
          "$_lf" "$_rf" --name "$_rel" 2>/dev/null)"
  rm -f "$_lf" "$_rf"
  # An absent/garbled verdict means the comparator itself failed. Fail SAFE.
  case "$_v" in identical|different|unparseable) : ;; *) _v=unparseable ;; esac
  echo "$_v"
}

# True only when EVERY supplied path is provably identical between the two
# revspec prefixes. Short-circuits on the first non-identical verdict, so one
# genuinely-modified file in a batch defers the whole batch — which is the
# conservative reading and matches the all-or-nothing shape of the callers.
_ip_all_semantically_identical() {
  local _lpfx="$1" _rpfx="$2"; shift 2
  local _p
  [ "$#" -eq 0 ] && return 1     # nothing proven -> never license a restore
  for _p in "$@"; do
    [ "$(_ip_semantic_verdict "$_lpfx" "$_rpfx" "$_p")" = identical ] || return 1
  done
  return 0
}

# : DURABLE cross-agent state — agent IDENTITY and the learning
# archive. The agents/* arm below restores (tracked) or deletes (untracked)
# anything git names as blocking, with NO content check, at a measured ~1/day
# over 12 days on one box. Both sibling arms already refuse to do that:
# .mind-data/* COMMITS because clearing "would DISCARD encoded knowledge", and
# the shared *) arm clears only on a provable `identical` verdict. This set is
# the third arm's version of that same refusal.
#
# Matched on BASENAME first, deliberately: these names recur at several depths,
# and a path-prefix test would miss the per-record files that are the bulk of
# the archive (5595 experience/ + 488 journal/ files measured on one box).
#
# Deliberately NARROW rather than "every cross-agent file". Deferring instead of
# clearing is not free — / show a permanent stall wearing a
# transient's message when a re-dirtying path defers every iteration. Scoping to
# identity + the archive bounds that risk to files that are rewritten rarely,
# while leaving ordinary cross-agent churn (state.jsonl et al) on the existing
# clear path.
_ip_durable_crossagent() {
  case "${1##*/}" in
    aspirations.jsonl|experience.jsonl|experience-archive.jsonl|journal.jsonl|self.md)
      return 0 ;;
  esac
  case "$1" in
    agents/*/experience/*|agents/*/journal/*) return 0 ;;
  esac
  return 1
}

# : the question the deadlock poses is "who resolves a durable
# cross-agent file that is dirty on a box that does not own it". The answer is
# NOBODY — git does, at the merge, IF a commutative merge driver is configured
# for that path. Then the third option opens: neither CLEAR (destroys the
# partner's divergence, ) nor DEFER (a permanent wedge, since
# iteration-commit.sh's namespace filter correctly refuses the same file and no
# arbiter sits between the two refusals) but COMMIT, and let the driver union
# the records on the way in.
#
# BOTH LEGS ARE LOAD-BEARING. `.gitattributes` is version-controlled so the
# ATTRIBUTE is present in every clone, but the DRIVER lives in `.git/config`,
# which is NOT — it is registered per-clone by install-git-hooks.sh. On a box
# where that never ran, git silently falls back to its default text merge and
# the union guarantee does not exist. Testing the attribute alone would be a
# control that looks live and is not (guard-3130), so both are tested here.
#
# DO NOT substitute coordination_merge.merge_handler_for() for this, which is
# what the filing goal proposed. That is the OWN-CLOUD arbiter and this is a GIT
# decision, and the two disagree: measured 2026-08-17 on cc-08, merge_handler_for
# registers 1 of the 7 durable cross-agent shapes (aspirations.jsonl) while the
# git driver handles 5 — git-merge-ayoai-ledger.py carries experience.jsonl /
# experience-archive.jsonl / journal.jsonl in its own _JSONL_ID_UNION set BEFORE
# it consults that registry, and agents/*/journal/**/*.md routes to the separate
# ayoai-journal-md section-union driver. Keying on the wrong layer's classifier
# would keep deferring three files git can reconcile commutatively.
#
# self.md and agents/*/experience/*.md are `unspecified` and therefore still
# defer — correctly: an identity file and a per-record document are not
# commutative, and there is nothing to union.
#
# WORST CASE IS STRICTLY BETTER THAN THE STATUS QUO. If the driver cannot merge
# it exits 1 with %A untouched, so git keeps the conflict and the integrate
# aborts — the ordinary, self-announcing "MERGE CONFLICT ... will retry next
# iteration" state, which a human or a later merge can resolve. That replaces a
# dirty file that can be neither cleared nor committed and whose T_recovery is
# INFINITY.
_ip_git_mergeable() {
  local _drv
  _drv="$(git -C "$REPO" check-attr merge -- "$1" 2>/dev/null)"
  _drv="${_drv##*: }"
  case "$_drv" in
    ''|unspecified|unset|text|binary) return 1 ;;
  esac
  [ -n "$(git -C "$REPO" config --get "merge.$_drv.driver" 2>/dev/null)" ]
}

_selfheal_cross_agent_churn_remerge() {
  local self="${MIND_AGENT:-}"
  if [ -z "$self" ]; then
    log "self-heal: MIND_AGENT unresolved — cannot scope safely, defer"
    return 1
  fi

  # guard-741: the shared multi-agent index can hold a CONCURRENT agent's STAGED
  # cross-agent files. NEVER discard staged work — if the index has any staged
  # entry, defer (the bare merge already refuses on staged overlap anyway).
  local _staged=() _sp
  while IFS= read -r -d '' _sp; do _staged+=("$_sp"); done \
    < <(git -C "$REPO" diff --cached --name-only -z 2>/dev/null)
  if [ "${#_staged[@]}" -gt 0 ]; then
    # : this gate is the MEASURED source of the stranding, not the
    # dirty-path arms below. A cross-agent DIRTY file never reaches a defer —
    # it is routed to cross_dirty and restored with `git checkout --`. Measured
    # on cc-07 (.git/iteration-push.log, 1719 lines): 1 of 1 deferred merges
    # came from this gate and 0 from any dirty-path arm, while the cross-agent
    # clearing path fired 12 times and healed.
    #
    # guard-741's INTENT is preserved exactly: never discard a partner's staged
    # work. When every staged entry is provably identical to HEAD there is no
    # partner work in the index to discard — that is proven, not assumed, and
    # anything short of proof still defers.
    if _ip_all_semantically_identical ":" "HEAD:" "${_staged[@]}"; then
      log "self-heal: ${#_staged[@]} staged entr(ies) SEMANTICALLY identical to HEAD — serialization churn, unstaging rather than deferring (g-115-5717)"
      if ! git -C "$REPO" reset -q -- "${_staged[@]}" 2>/dev/null; then
        log "self-heal: unstaging churn-only index entries failed — defer"
        return 1
      fi
    else
      log "self-heal: staged index entries present — guard-741, defer (never discard partner's staged work)"
      return 1
    fi
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
  # path). Still all-or-nothing over the CLEAR set — over the blocking set rather
  # than over every file that happens to be dirty.
  #
  # : a defer decided mid-classification RECORDS ITSELF and lets the
  # loop finish, instead of `return 1`-ing on the spot. The six defer arms below
  # used to return immediately, which abandoned every self-namespace path already
  # collected into self_paths — and self_paths is not STAGED until after both
  # loops. So the routine case (a partner's durable store dirty from own-cloud
  # sync) stranded this Body's own ledger churn uncommitted, where it cannot
  # travel on refs/workers/<agent>/<sid> and the reducer never sees it at
  # generalize-down. Measured cc-07 2026-08-16: 3 consecutive dirty-defer cycles,
  # behind=17 ahead=6, agents/alpha/{aspirations,changelog,experience}.jsonl
  # uncommitted the whole time while the classifier had correctly collected them.
  #
  # THE DEFER IS NOT WEAKENED — that distinction is the whole design. Recording
  # the defer instead of returning changes WHEN we leave, never WHETHER: the
  # clear-set is still discarded untouched, _heal_defer still returns 1, and
  # 's "never clobber a partner's real divergence" is untouched. Only
  # the independent self-commit, which was already unconditional on the success
  # path, now also runs on the defer path.
  #
  # Collecting the FLAG rather than breaking is load-bearing: `git diff
  # --name-only` is sorted, so whether a self path precedes the blocking partner
  # path is decided by how the agent's own name sorts. Breaking on first defer
  # would fix this for `alpha` and leave `zeta` broken — a correctness property
  # must not depend on the agent's name.
  local rel name
  local self_paths=() cross_dirty=() cross_untracked=() storage_paths=()
  local mergeable_cross=()   # : durable cross-agent paths git can union
  local _heal_defer=0
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
        elif [ "$_heal_defer" = 0 ] && _ip_blocks "$rel"; then
          # : give DURABLE cross-agent state the same content check the
          # shared *) arm below already applies. `identical` still clears, so
          #  is unregressed; `different` and `unparseable` defer,
          # because `checkout --` restores to HEAD and the uncommitted work is
          # then gone with no recovery path.
          # : the question a clear must answer is "does restoring this
          # discard work?", and after the merge the file carries the INCOMING
          # origin bytes — so local-vs-$UPSTREAM is the deciding comparison, not
          # local-vs-HEAD. Under own-cloud the sync routinely applies locally the
          # exact bytes the pending merge is about to deliver, which is identical
          # to origin and different from the (stale) HEAD: the HEAD-only test
          # then defers forever and the wedge CANNOT self-clear. Measured three
          # times by two operators, and all three were freed by hand-proving the
          # blob equal to origin (cc-07 2026-08-17: zeta/aspirations.jsonl LOCAL
          # 23 = ORIGIN 23, HEAD 22, byte-identical, wedged 3 cycles at
          # behind=11/ahead=15).
          # Either proof licenses the clear, so this only ever WIDENS the
          # clear set — a file identical to HEAD still clears exactly as
          # /6145 made it, and a file differing from BOTH still
          # defers untouched, which is the property  exists to hold.
          if _ip_durable_crossagent "$rel" \
             && [ "$(_ip_semantic_verdict "" "HEAD:" "$rel")" != identical ] \
             && [ "$(_ip_semantic_verdict "" "$UPSTREAM:" "$rel")" != identical ]; then
            # MERGE RESOLUTION 2026-08-18 ( + , resolved on
            # cc-08). The two fixes landed concurrently on the same block and
            # are COMPLEMENTARY, not competing — they act at different levels of
            # the same conditional, so taking either alone silently drops the
            # other's guarantee:
            #    widened the ENTRY CONDITION — a file byte-identical to
            #     $UPSTREAM is safe to clear (own-cloud routinely applies locally
            #     the exact bytes the pending merge will deliver), so it must not
            #     reach the defer body at all.
            #    added a third DISPOSITION inside the body — of what
            #     still differs from BOTH, anything git holds a configured
            #     commutative driver for is COMMITTED, not deferred.
            # Combined lattice, strictly better than either side alone:
            #   identical to HEAD ....................... clear (/6145)
            #   identical to $UPSTREAM .................. clear ()
            #   differs from both, git-mergeable ........ COMMIT ()
            #   differs from both, not mergeable ........ defer  ()
            # Ordering is load-bearing: the mergeable probe runs BEFORE the defer
            # so the deadlock never forms; see _ip_git_mergeable.
            if _ip_git_mergeable "$rel"; then
              log "self-heal: DURABLE cross-agent file $rel differs from HEAD and $UPSTREAM, but git has a configured commutative merge driver — COMMIT it, the driver reconciles at the merge (g-115-6572)"
              mergeable_cross+=("$rel"); continue
            fi
            log "self-heal: DURABLE cross-agent file $rel differs in CONTENT from BOTH HEAD and $UPSTREAM — defer, never clear (g-115-6145/g-115-6538)"
            # Record the CAUSE so the streak alarm can name this shape instead of
            # prescribing "clear it" (). Accumulates across paths; the
            # alarm reads it ~350 lines below, in the same shell.
            _IP_DEFER_DURABLE="${_IP_DEFER_DURABLE:+$_IP_DEFER_DURABLE }$rel"
            _heal_defer=1; continue
          fi
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
        # (non-fast-forward) -> 10 unpushed commits and climbing.
        #
        # THE OLD LINE HERE READ "Under own-cloud this arm never fires: world/
        # and meta/ are external and gitignored." That was true when written and
        # is now FALSE — and it is the whole of . Measured on ZDS-Mind
        # (cc-06, own-cloud, 2026-08-10): .mind-data/ is git-tracked there since
        # 2026-07-28, so world+meta changelog.jsonl / gate-firings.jsonl /
        # aspirations.jsonl / board/coordination.jsonl / tree/_tree.yaml /
        # reasoning-bank.jsonl / retrieval-trace.jsonl DO go dirty — every close
        # phase writes them AFTER iteration-commit staged them. `_backend` is
        # correctly "own-cloud" there, so this arm deferred on EVERY iteration:
        # a permanent stall wearing a transient's message ("retry next
        # iteration" cannot succeed, because the next iteration recreates the
        # condition), and silent, because rc=2 does not fail the loop. Observed
        # five consecutive defers and a drift to ahead-15/behind-5.
        #
        # So gate on TRACKEDNESS, not on the backend that used to imply it.
        # Where .mind-data/ is gitignored (this repo) nothing here is reachable,
        # which makes the added arm a provable no-op rather than a behaviour
        # change. Where it is tracked, committing is what the local backend has
        # always done for the identical shape, and the tracked set is already
        # curated to be merge-safe: the machine-local files that genuinely
        # cannot merge (presence/, history-save-telemetry) were UNTRACKED for
        # exactly that reason, while changelog.jsonl was kept because it routes
        # to merge_append_only_jsonl and reconciled with 0 unique records lost.
        if [ "$_backend" = "local" ] || _ip_mind_data_tracked; then storage_paths+=("$rel")
        elif [ "$_heal_defer" = 0 ] && _ip_blocks "$rel"; then
          log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
          _heal_defer=1; continue
        fi
        ;;
      *)
        if [ "$_heal_defer" = 0 ] && _ip_blocks "$rel"; then
          # : a shared file whose CONTENT is provably identical to
          # HEAD carries nothing to lose, so restoring it discards no work.
          # Only `identical` takes this branch; `different` and `unparseable`
          # both fall through to the defer below, unchanged.
          if [ "$(_ip_semantic_verdict "" "HEAD:" "$rel")" = identical ]; then
            log "self-heal: shared file $rel differs from HEAD only by serialization — restoring, not deferring (g-115-5717)"
            cross_dirty+=("$rel")
          else
            log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
            _heal_defer=1; continue
          fi
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
        elif [ "$_heal_defer" = 0 ] && _ip_blocks "$rel"; then
          # : an UNTRACKED durable cross-agent file has NO HEAD side,
          # so `git clean` destroys it outright — strictly worse than the tracked
          # case, which at least restores a committed version. No semantic
          # verdict is computable (one side does not exist), so defer flatly.
          if _ip_durable_crossagent "$rel"; then
            log "self-heal: DURABLE untracked cross-agent file $rel has no HEAD version — defer, never delete (g-115-6145)"
            _heal_defer=1; continue
          fi
          cross_untracked+=("$rel")
        fi
        ;;
      .mind-data/*)
        if [ "$_backend" = "local" ]; then storage_paths+=("$rel")
        elif [ "$_heal_defer" = 0 ] && _ip_blocks "$rel"; then
          log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
          _heal_defer=1; continue
        fi
        ;;
      *)
        if [ "$_heal_defer" = 0 ] && _ip_blocks "$rel"; then
          log "self-heal: blocking file outside agents/* ($rel) — defer (never clear core/world/shared work)"
          _heal_defer=1; continue
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
  # : the pathspec here is the EXACT file list, never a namespace dir.
  # The sibling arms can use a directory because they own that namespace; this
  # arm does not own the partner's, so limiting the commit to precisely the
  # paths classified above is what keeps guard-741/836 intact — a partner's
  # stage racing in after classification is still excluded from the commit.
  [ "${#mergeable_cross[@]}" -gt 0 ] && { _heal_stage+=("${mergeable_cross[@]}"); _heal_spec+=("${mergeable_cross[@]}"); }
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
    # Emitted as a SEPARATE line, leaving both lines above byte-identical: the
    #  regression tests assert those phrasings as exact substrings
    # (guard-695 — never change a shape test code asserts against).
    if [ "${#mergeable_cross[@]}" -gt 0 ]; then
      log "self-heal: + ${#mergeable_cross[@]} git-mergeable durable cross-agent file(s) committed rather than deferred (g-115-6572)"
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
    if [ "${#mergeable_cross[@]}" -gt 0 ]; then
      _heal_msg="chore($self): pre-merge churn + ${#mergeable_cross[@]} git-mergeable cross-agent ledger(s) (iteration-push self-heal, g-115-6572)"
    fi
    if ! git -C "$REPO" commit -q -m "$_heal_msg" \
         -- "${_heal_spec[@]}" 2>/dev/null; then
      # Unstage what we staged so a failed heal leaves the index as found.
      git -C "$REPO" reset -q -- "${_heal_spec[@]}" 2>/dev/null || true
      log "self-heal: pathspec-limited commit of self-namespace churn failed — defer"
      return 1
    fi
  fi

  # : the defer recorded during classification lands HERE — after the
  # self/storage commit above, before the clear below. That position is the
  # entire fix, and both halves of it matter:
  #   AFTER the commit  — self-namespace churn is no longer stranded behind a
  #                       partner file this Body does not own and cannot clear.
  #   BEFORE the clear  — nothing in the clear-set is touched, so the defer is
  #                       exactly as conservative as it was ().
  # Returning 1 keeps the caller's contract identical: it still reports
  # "merge DEFERRED" and still retries the merge next iteration.
  if [ "$_heal_defer" = 1 ]; then
    if [ "${#_heal_stage[@]}" -gt 0 ]; then
      log "self-heal: deferring on cross-agent/shared churn, but ${#_heal_stage[@]} self+storage file(s) were COMMITTED first — not stranded behind the defer (g-115-6373)"
    fi
    return 1
  fi

  if [ $(( ${#cross_dirty[@]} + ${#cross_untracked[@]} )) -gt 0 ]; then
    log "self-heal: clearing ${#cross_dirty[@]} tracked + ${#cross_untracked[@]} untracked cross-agent file(s), retrying merge once (g-115-1843)"
    # NAME EVERY DISCARDED PATH (). The count line above was the ONLY
    # record this branch left, which made the single destructive arm of this
    # helper the one arm with no auditable trace. Three independent reasons the
    # loss is otherwise invisible AFTER the fact, all measured 2026-08-13 (zeta,
    # hostname cc-02, uname -r 6.8.0-137-generic):
    #   1. the discarded content was never committed, so git history cannot show it;
    #   2. this line reported a COUNT and no paths, so the push log cannot either;
    #   3. the goal-record fingerprint of a reverted close (outcome_class survives,
    #      completed_date nulled) SELF-ERASES as soon as the selector re-offers the
    #      goal and it re-executes — which is how  was detected at all.
    # So "did this ever discard a goal queue?" was unanswerable by construction:
    # the branch fired 11 times in 12 days on ONE box, and nothing on disk could
    # say whether any of those 11 took an aspirations.jsonl, an experience archive,
    # a journal, or a self.md — all of which are tracked under agents/* and all of
    # which this arm is eligible to clear. A count cannot answer "what did I lose".
    # Emitted as SEPARATE lines, leaving the count line byte-identical, because
    # test_iteration_push.py asserts that phrase as an exact substring at two sites
    # (guard-695 — never change a shape test code asserts against).
    local _cleared_rel
    if [ "${#cross_dirty[@]}" -gt 0 ]; then
      for _cleared_rel in "${cross_dirty[@]}"; do
        log "self-heal: DISCARDING uncommitted tracked cross-agent work: $_cleared_rel"
      done
    fi
    if [ "${#cross_untracked[@]}" -gt 0 ]; then
      for _cleared_rel in "${cross_untracked[@]}"; do
        log "self-heal: DISCARDING untracked cross-agent file: $_cleared_rel"
      done
    fi
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
    _ip_log_conflict_paths
    git -C "$REPO" merge --abort >/dev/null 2>&1 || true
    log "self-heal: churn healed, but the merge retry hit a TRUE content conflict (rc=${MERGE_RC}) — aborted cleanly"
    return 2
  fi
  log "self-heal: merge retry still failed after clearing churn (rc=${MERGE_RC}) — defer"
  return 1
}

# --- Worker carrier push () ----------------------------------------
# The carrier push is INDEPENDENT of shared-tree integration. refs/workers/<agent>/<sid>
# has exactly ONE writer by construction and touches no shared branch, so whether
# `merge $UPSTREAM` succeeded, conflicted, or deferred says nothing about whether this
# Body's HEAD should reach the reducer. The ref carries HEAD; integrating origin/main is
# orthogonal to that.
#
# Before this helper existed the push lived ONLY in the block at the integrate/push seam
# below, and every deferral path soft_exit'd past it. Measured 2026-08-16 (alpha worker
# Body, cc-07): a 1-line diff in agents/zeta/aspirations.jsonl — a partner store file this
# Body never touched, left dirty as ordinary own-cloud read-through-cache background state
# — deferred the merge, so the commit never reached the reducer while the script exited 0.
# On an own-cloud fleet box that condition is routine, not rare, and the worker neither
# controls it nor has reason to notice it.
#
# Returns 0 when pushed / dry-run / not requested; 1 on unresolved identity or push failure.
_ip_push_worker_ref() {
  [ "$PUSH_WORKER_REF" = 1 ] || return 0
  # Idempotence guard: the deferral seam and the seam block below are mutually
  # exclusive today, but a future seam must not be able to double-push.
  [ "${_IP_WREF_DONE:-0}" = 1 ] && return "${_IP_WREF_RC:-0}"
  _IP_WREF_DONE=1
  if [ -z "$WORKER_REF_AGENT" ] || [ -z "$WORKER_REF_SID" ]; then
    log "--push-worker-ref: REFUSED — agent/sid unresolved (MIND_AGENT='$WORKER_REF_AGENT', MIND_SID='$WORKER_REF_SID')."
    log "  A ref missing either segment would collide across bodies, which is the one property this carrier exists to guarantee."
    _IP_WREF_RC=1; return 1
  fi
  WREF="refs/workers/${WORKER_REF_AGENT}/${WORKER_REF_SID}"
  if [ "$DRY_RUN" = 1 ]; then
    log "--push-worker-ref (dry-run): would push HEAD -> $WREF"
    _IP_WREF_RC=0; return 0
  fi
  # No --force. The ref only ever advances for a given body (HEAD moves forward
  # through commits and merges), so a non-fast-forward here means an assumption
  # broke — single-writer, or a reset — and it should be LOUD rather than
  # silently overwritten.
  if $IP_TMO git -C "$REPO" push origin "HEAD:$WREF" >/dev/null 2>&1; then
    log "--push-worker-ref: pushed HEAD ($(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)) -> REMOTE $WREF"
    log "  verify with 'git ls-remote origin $WREF', NOT 'git rev-parse $WREF'. This push writes the REMOTE ref only."
    log "  A local refs/workers/... ref exists only because some consumer fetched '+refs/workers/*:refs/workers/*'"
    log "  earlier; it does NOT advance on this push, so rev-parse returns a PLAUSIBLE STALE sha from a previous"
    log "  unit and the natural verification reports a false NO (g-306-313, guard-1250)."
    # DEPENDENCY-PULL PRODUCER, worker lane (). The carrier has just
    # LANDED, which is precisely the event the drain goal exists to consume — so
    # the pull is stamped HERE, as an invariant of the push transition
    # (guard-403), rather than as a second line in worker-loop's Phase 3.8.
    # Three reasons this beats the SKILL.md call the goal originally specified:
    # it fires on the real event instead of on an LLM remembering a step, it
    # costs zero bytes of the hot-path prose budget, and the detection lives in
    # exactly one place. pull-signal-set.sh is self-gating — it no-ops unless
    # this push carried non-merge framework content, and no-ops again if a live
    # signal is already stamped — so calling it unconditionally here is correct.
    # STRICTLY ADVISORY: rc swallowed. A carrier push must never fail because a
    # rank hint could not be written.
    if [ -x "$SCRIPT_DIR/pull-signal-set.sh" ]; then
      log "  pull: $(bash "$SCRIPT_DIR/pull-signal-set.sh" --if-carrier-content 2>&1 | head -1)"
    fi
    _IP_WREF_RC=0; return 0
  fi
  log "--push-worker-ref: push FAILED for $WREF — this Body's framework edits and local commits have NOT reached the reducer"
  _IP_WREF_RC=1; return 1
}

# _ip_defer_exit: EVERY integrate-deferral seam exits through here, so the worker
# carrier is flushed BEFORE the exit. Routing all three seams (conflict-abort,
# conflict-after-selfheal, dirty-defer) through one helper is the point: the original
# defect was not any one seam but that the push sat below all of them, so a fourth seam
# added later would have stranded the carrier again in exactly the same way.
_ip_defer_exit() {
  if [ "$PUSH_WORKER_REF" = 1 ]; then
    log "--push-worker-ref: integrate deferred, but the carrier is independent of it — pushing anyway"
    _ip_push_worker_ref || true
  fi
  soft_exit 1
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
    # : assert the record-aware merge driver is actually REGISTERED on
    # this clone, immediately before the one command that would use it. The
    # driver is written into .git/config by install-git-hooks.sh and git config
    # is NOT version-controlled, so a clone where that never ran has correct
    # .gitattributes pointing at nothing and git silently degrades to its
    # default text merge — which on concurrent tail appends CONFLICTS, i.e.
    # lands straight in the repeating-conflict wedge the block above escalates.
    # Placed HERE rather than at cycle top so it costs nothing on the common
    # already-up-to-date pass (measured 0.16s for 10,116 tracked paths, against
    # a real fetch+merge). ADVISORY — must never block the integrate.
    # --repo "$REPO", never the bare default: REPO honours --repo /
    # ITERATION_PUSH_REPO and can differ from PROJECT_ROOT, and a check that
    # asserted the wrong tree would report OK about a repo it never looked at.
    bash "$SCRIPT_DIR/check-merge-driver-registered.sh" --repo "$REPO" >&2 2>&1 || true
    MERGE_OUT="$(GIT_TERMINAL_PROMPT=0 git -C "$REPO" merge --no-edit "$UPSTREAM" 2>&1)"
    MERGE_RC=$?
    if [ "$MERGE_RC" -ne 0 ]; then
      # Conflict state left behind? Abort it — the tree must NEVER be left
      # mid-merge for the loop to trip over.
      if [ -f "$GITDIR/MERGE_HEAD" ]; then
        _ip_log_conflict_paths
        git -C "$REPO" merge --abort >/dev/null 2>&1 || true
        log "MERGE CONFLICT with $UPSTREAM — aborted cleanly, will retry next iteration."
        log "If this repeats every iteration it is a TRUE cross-machine content conflict (MERGE_HEAD was created — NOT the dirty-tree defer shape): resolve manually (git merge $UPSTREAM) or investigate which store conflicted."
        _ip_defer_streak_tick "conflict-abort"
        _ip_defer_exit
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
        _ip_defer_exit
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
        # : a durable-cross-agent defer and an ordinary dirty-tree
        # defer arrive here identically, but their remedies are OPPOSITE — the
        # dirty-tree hint says "clear it", which for the durable shape destroys a
        # partner's unpushed work. Split them by the cause recorded in the
        # self-heal, not by re-deriving it here.
        # $2 carries the blocking paths into the escalation directive
        # (): re-deriving them on the wedged box is exactly the work
        # the escalation exists to save, and by the time a reader sees it the
        # merge output that named them may be many cycles back in the log.
        if [ -n "${_IP_DEFER_DURABLE:-}" ]; then
          _ip_defer_streak_tick "durable-crossagent-defer" "$_defer_paths"
        else
          _ip_defer_streak_tick "dirty-defer" "$_defer_paths"
        fi
        _ip_defer_exit
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
# The push itself lives in _ip_push_worker_ref (defined above the integrate step) so
# that the deferral seams can flush the same carrier through the same code ().
# This block remains the CLEAN-INTEGRATE path: it is what runs when the merge succeeded
# or none was needed.
if [ "$PUSH_WORKER_REF" = 1 ]; then
  if _ip_push_worker_ref; then
    soft_exit 0
  fi
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
# $IP_TMO: bound the network call () — rc=124 lands in the non-race
# branch below and soft_exits, i.e. retry next iteration.
# No --force, ever. Capture combined output for the log summary. Safe to log: an
# https remote is credential-helper-backed (GCM never prints the token) and an
# ssh remote carries no credential in the URL at all. (Corrected 2026-08-26 —
# this comment asserted "the remote is a plain https URL", which is false on
# every ssh-remote box, i.e. most of the fleet.)
PUSH_OUT="$(GIT_TERMINAL_PROMPT=0 $IP_TMO git -C "$REPO" push origin "$BRANCH" 2>&1)"
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
        _ip_log_conflict_paths
        git -C "$REPO" merge --abort >/dev/null 2>&1 || true
        log "recovery merge CONFLICT with $UPSTREAM — aborted cleanly, will retry next iteration"
      else
        log "recovery merge refused (rc=${RMERGE_RC}) — will retry next iteration: $(printf '%s' "$RMERGE_OUT" | tail -n 1)"
      fi
      soft_exit 1
    fi
  fi
  RPUSH_OUT="$(GIT_TERMINAL_PROMPT=0 $IP_TMO git -C "$REPO" push origin "$BRANCH" 2>&1)"
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
