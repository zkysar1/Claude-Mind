#!/usr/bin/env bash
# iteration-commit.sh — Wrap the per-iteration commit ceremony.
#
# Composes a conventional-format commit message from goal-id + title +
# outcome class, stages uncommitted tracked-file changes in the named
# repo, and commits with Co-Authored-By signing the executing agent.
#
# Does NOT push. Does NOT run build/test gates. Those are separate
# concerns (post-execution.md Step 2b.1 owns the build gate; push is
# manual or via a future iteration-push.sh).
#
# Skips on outcome=routine (no changes typical) — exit 0 with no-op.
# Skips on empty git status — exit 0 with no-op.
#
# Origin:  (bravo session-62 auto-commit theme1).

set -euo pipefail

SCRIPT_NAME="iteration-commit.sh"

usage() {
  cat <<'EOF'
Usage: iteration-commit.sh --goal-id <id> --title "<title>" --outcome <routine|deep> --repo <path>
                           [--type <type>] [--message "<body>"] [--dry-run] [-h|--help]

Compose and execute a conventional-format git commit for the current iteration.

Required arguments:
  --goal-id <id>        Goal ID owning these changes (e.g., g-280-02).
  --title "<title>"     Goal title (becomes commit summary, truncated if long).
  --outcome <class>     Outcome class — routine|deep. Routine = no-op.
  --repo <path>         Repo directory to commit in (absolute or relative).

Optional:
  --type <type>         Conventional commit type. Auto-derived from title prefix
                        if absent: Apply:→feat, Fix:→fix, Maintain:→chore,
                        Investigate:→docs, Verify:→test, Idea:→feat, Forge:→feat.
                        Defaults to "chore" if no prefix matches.
  --message "<body>"    Additional commit body (multi-line OK).
  --dry-run             Print commit message + planned action, do NOT commit.
  --include-untracked   Disable the cross-agent uncommitted-file filter (g-248-87
                        extended for M-status, 38fb983 incident). Despite the
                        flag name (kept for backward compat), the filter covers
                        BOTH untracked (??) and modified (' M', 'MM', 'AM', ' A')
                        status codes. Default: filter ON when MIND_AGENT is set
                        and team-state has claimed_at — uncommitted files at
                        non-agent paths whose mtime predates the committer's
                        claimed_at are skipped (signal: partner WIP captured by
                        the snapshot before this agent took ownership of its
                        goal). Pass this flag for legitimate cross-agent commits
                        (rare) OR when this agent legitimately edited the file
                        more than 5s before calling team-state-update claim.
  -h, --help            Show this help.

Behavior:
  - Routine outcome → exit 0 (no-op).
  - Empty git status → exit 0 (no-op).
  - Filters sensitive patterns from staging: .env*, *.key, *.pem,
    credentials*, secrets* (warns + skips matching paths).
  - Routes orphan deletions ("D" in worktree, parent dir missing) via
    git rm --cached --ignore-unmatch (g-280-08).
  - Retries git commit up to 3 times with 1s backoff on transient
    failures (g-280-04 — Windows-specific antivirus/sharing issues).
  - Wraps status→add→commit in $REPO/.iteration-commit-lock/ mkdir mutex
    to prevent cross-agent authorship bleed (g-280-11, rb-356 pattern).
    Configurable via ITERATION_COMMIT_LOCK_WAIT_S (default 30) and
    ITERATION_COMMIT_LOCK_STALE_S (default 30).
  - Namespace filter (g-280-12): drops paths under OTHER agents'
    directories so an agent's iteration-commit never absorbs partner
    files in agent-local namespaces. Discovers agent dirs by scanning
    $REPO/*/self.md. Disabled when $MIND_AGENT is unset OR no agent
    dirs are discovered (test repos, fresh installs). Override the
    filter explicitly with --no-namespace-filter when committing
    legitimate cross-agent edits (rare).
  - Co-Authored-By signed with $MIND_AGENT (or "agent" if unset).
  - Outputs JSON on success: {commit_sha, files_committed, repo}.

Exit codes:
  0 — committed (or no-op for routine/empty)
  1 — usage / validation error
  2 — git operation failed
EOF
}

# --- Arg parsing -------------------------------------------------------------
GOAL_ID=""
TITLE=""
OUTCOME=""
REPO=""
TYPE=""
EXTRA_MSG=""
DRY_RUN=0
NO_NAMESPACE_FILTER=0
INCLUDE_UNTRACKED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --goal-id) GOAL_ID="${2:-}"; shift 2 ;;
    --title) TITLE="${2:-}"; shift 2 ;;
    --outcome) OUTCOME="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --type) TYPE="${2:-}"; shift 2 ;;
    --message) EXTRA_MSG="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-namespace-filter) NO_NAMESPACE_FILTER=1; shift ;;
    --include-untracked) INCLUDE_UNTRACKED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[$SCRIPT_NAME] ERROR: unknown arg '$1'" >&2; usage >&2; exit 1 ;;
  esac
done

# --- Validation --------------------------------------------------------------
if [[ -z "$GOAL_ID" || -z "$TITLE" || -z "$OUTCOME" || -z "$REPO" ]]; then
  echo "[$SCRIPT_NAME] ERROR: missing required flag(s): --goal-id, --title, --outcome, --repo are all required" >&2
  usage >&2
  exit 1
fi

if [[ "$OUTCOME" != "routine" && "$OUTCOME" != "deep" ]]; then
  echo "[$SCRIPT_NAME] ERROR: --outcome must be 'routine' or 'deep' (got '$OUTCOME')" >&2
  exit 1
fi

if [[ ! -d "$REPO" ]]; then
  echo "[$SCRIPT_NAME] ERROR: --repo '$REPO' is not a directory" >&2
  exit 1
fi

if [[ ! -d "$REPO/.git" ]]; then
  # Soft-skip when the repo isn't a git work tree (Phase 2.2 packaging
  # cleanup, 2026-05-17). The framework is designed to function without
  # git — only the iteration audit trail, pre-commit gates, post-commit
  # daemon recycle, and cross-agent change attribution are lost. State
  # update / encoding / reflection / aspiration management are
  # git-independent. Returns exit 0 so the calling loop continues.
  # check-prerequisites.sh surfaces the git warning at /start time so
  # the user sees the capability loss explicitly.
  echo "[$SCRIPT_NAME] git not available — iteration commit skipped (audit trail disabled; loop continues)" >&2
  exit 0
fi

# --- Routine skip ------------------------------------------------------------
if [[ "$OUTCOME" == "routine" ]]; then
  echo "[$SCRIPT_NAME] skip: outcome=routine (no commit by design)"
  exit 0
fi

# --- Agent identity (needed by lock holder + commit signature) ---------------
agent_name="${MIND_AGENT:-agent}"

# --- Cross-agent serialization lock (, rb-356) -----------------------
# Wraps the status→parse→add→commit critical section in a mkdir-based mutex.
# Without this, two agents racing to commit can each include the partner's
# still-uncommitted files in their staged_files array (zeta investigation
#  measured 56% bleed rate in last-24h multi-agent commits).
# git's native .git/index.lock serializes add+commit but does NOT cover the
# status-snapshot step — that's the bleed window this lock closes.
#
# Pattern mirrors precompact-serialize.sh / .autocompact-serialize-lock/ —
# see core/scripts/lock-symmetry-lint.sh for the release-predicate-symmetry
# guard. The trap below pairs with the explicit `rm -rf` in stale recovery.
LOCK_DIR="$REPO/.iteration-commit-lock"
LOCK_HOLDER="$LOCK_DIR/holder"
LOCK_TIMESTAMP_FILE="$LOCK_DIR/timestamp"
LOCK_STALE_S="${ITERATION_COMMIT_LOCK_STALE_S:-30}"
LOCK_MAX_WAIT_S="${ITERATION_COMMIT_LOCK_WAIT_S:-30}"
LOCK_TICK_S=1

# Pre-acquire stale cleanup (handles crashed-script case)
if [[ -d "$LOCK_DIR" ]]; then
  holder_ts=$(cat "$LOCK_TIMESTAMP_FILE" 2>/dev/null || echo 0)
  now_ts=$(date +%s)
  if [[ $((now_ts - holder_ts)) -gt $LOCK_STALE_S ]]; then
    holder_name=$(cat "$LOCK_HOLDER" 2>/dev/null || echo "?")
    echo "[$SCRIPT_NAME] WARN: stale lock detected (age $((now_ts - holder_ts))s, holder=$holder_name), forcing release" >&2
    rm -rf "$LOCK_DIR" 2>/dev/null || true
  fi
fi

# Acquire with bounded backoff. Use `if mkdir` form (not `while ! mkdir`) so
# core/scripts/lock-symmetry-lint.py picks up the ACQUIRE site — its regex
# matches `mkdir` or `if mkdir`, not `while ! mkdir`.
lock_waited=0
lock_acquired=0
while [[ $lock_waited -le $LOCK_MAX_WAIT_S ]]; do
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    lock_acquired=1
    break
  fi
  sleep "$LOCK_TICK_S"
  lock_waited=$((lock_waited + LOCK_TICK_S))
  # Re-check staleness mid-wait (holder may have crashed while we waited)
  if [[ -d "$LOCK_DIR" ]]; then
    holder_ts=$(cat "$LOCK_TIMESTAMP_FILE" 2>/dev/null || echo 0)
    now_ts=$(date +%s)
    if [[ $((now_ts - holder_ts)) -gt $LOCK_STALE_S ]]; then
      holder_name=$(cat "$LOCK_HOLDER" 2>/dev/null || echo "?")
      echo "[$SCRIPT_NAME] WARN: stale lock detected mid-wait (age $((now_ts - holder_ts))s, holder=$holder_name), forcing release" >&2
      rm -rf "$LOCK_DIR" 2>/dev/null || true
    fi
  fi
done
if [[ $lock_acquired -eq 0 ]]; then
  echo "[$SCRIPT_NAME] ERROR: failed to acquire lock after ${LOCK_MAX_WAIT_S}s in $REPO" >&2
  exit 2
fi

# Lock acquired — record holder, register release trap
echo "$agent_name" > "$LOCK_HOLDER"
date +%s > "$LOCK_TIMESTAMP_FILE"
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

# --- Discover known agent dirs ( namespace filter) -------------------
# Dirs under $REPO/agents/ containing self.md are agent dirs (Phase 2.5.D
# layout). Without MIND_AGENT set OR with no agent dirs discovered, the
# filter is inactive (current behavior — backward-compat). With both set,
# paths whose prefix is an OTHER agent's dir (agents/<other>/) are dropped
# from staging.
declare -a known_agents=()
if [[ $NO_NAMESPACE_FILTER -eq 0 && -n "${MIND_AGENT:-}" ]]; then
  for d in "$REPO"/agents/*/; do
    [[ -d "$d" ]] || continue
    if [[ -f "$d/self.md" ]]; then
      known_agents+=("$(basename "$d")")
    fi
  done
fi

# --- Cross-agent untracked-file filter () ----------------------------
# Untracked files (?? status) at non-agent paths whose mtime predates the
# committer's claimed_at are partner-WIP that the snapshot captured. The
# canonical incident: zeta was mid-development on parent-supersession-sweep.sh
# (untracked under core/scripts/) when alpha closed a goal — iteration-commit
# swept zeta's files into alpha's commit because the namespace filter only
# handles AGENT-DIR paths, not neutral paths like core/scripts/.
#
# Heuristic: an untracked file whose mtime is BEFORE committer.claimed_at
# existed BEFORE this agent took ownership of its goal, therefore it is not
# part of this commit's intent. Tolerate 5s of clock skew. The filter is OFF
# when claimed_at is unavailable (fresh install, test repo) — fail-open since
# we can't discriminate without the timestamp anchor.
committer_claimed_at_epoch=0
committer_claimed_at_iso=""
if [[ $INCLUDE_UNTRACKED -eq 0 && -n "${MIND_AGENT:-}" ]]; then
  paths_sh="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/team-state-read.sh"
  if [[ -x "$paths_sh" ]]; then
    raw_claimed_at=$("$paths_sh" --field "agent_status.${MIND_AGENT}.in_flight.claimed_at" --json 2>/dev/null || echo '""')
    committer_claimed_at_iso="${raw_claimed_at//\"/}"  # strip surrounding quotes
    if [[ -n "$committer_claimed_at_iso" && "$committer_claimed_at_iso" != "null" ]]; then
      # Convert ISO timestamp to epoch via py -3 (POSIX date doesn't parse ISO portably on Windows).
      committer_claimed_at_epoch=$(CLAIMED_AT_E="$committer_claimed_at_iso" py -3 - 2>/dev/null <<'PYEOF' || echo 0
import os, sys, datetime
try:
    t = os.environ.get("CLAIMED_AT_E", "")
    dt = datetime.datetime.fromisoformat(t)
    print(int(dt.timestamp()))
except Exception:
    print(0)
PYEOF
)
    fi
  fi
fi

# --- Partner in_flight snapshots () ---------------------------------
# Catches CONCURRENT partner edits: files modified during a partner's active
# in_flight goal. Falsified by : alpha's commit 79ce711 (09:25:08)
# swept zeta's edit to .claude/skills/analyze-npc-behavior/SKILL.md because
# the pre-claim filter (above) only catches partner WIP that PREDATES the
# committer's claim. Gather all OTHER agents' active in_flight.claimed_at so
# the file-loop below can filter neutral-path files whose mtime falls
# at-or-after any partner's claim. Same gating as the pre-claim filter:
# inactive when --include-untracked is set, MIND_AGENT is unset, or no
# agent dirs were discovered.
declare -a partner_claimed_at_epochs=()
declare -a partner_claimed_at_isos=()
declare -a partner_names_with_in_flight=()
if [[ $INCLUDE_UNTRACKED -eq 0 && -n "${MIND_AGENT:-}" && ${#known_agents[@]} -gt 0 ]]; then
  paths_sh_partner="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/team-state-read.sh"
  if [[ -x "$paths_sh_partner" ]]; then
    for partner in "${known_agents[@]}"; do
      if [[ "$partner" == "$MIND_AGENT" ]]; then
        continue
      fi
      raw_p=$("$paths_sh_partner" --field "agent_status.${partner}.in_flight.claimed_at" --json 2>/dev/null || echo '""')
      iso_p="${raw_p//\"/}"
      if [[ -n "$iso_p" && "$iso_p" != "null" ]]; then
        ep_p=$(CLAIMED_AT_E="$iso_p" py -3 - 2>/dev/null <<'PYEOF' || echo 0
import os, sys, datetime
try:
    t = os.environ.get("CLAIMED_AT_E", "")
    dt = datetime.datetime.fromisoformat(t)
    print(int(dt.timestamp()))
except Exception:
    print(0)
PYEOF
)
        if [[ "$ep_p" -gt 0 ]]; then
          partner_claimed_at_epochs+=("$ep_p")
          partner_claimed_at_isos+=("$iso_p")
          partner_names_with_in_flight+=("$partner")
        fi
      fi
    done
  fi
fi

# --- Partner uncommitted-edits snapshot () --------------------------
# Closes the between-claim attribution gap that  left open. The
# concurrent-partner filter above only catches edits during a partner's
# CURRENT in_flight window; partner-authored neutral-path edits that
# survived past in_flight clear (inter-claim gap, pause-then-edit window)
# slip through and get absorbed by the committer's iteration-commit.
#
# Each agent writes its own session/uncommitted-edits.jsonl via the
# uncommitted-edits-record.sh script chained from tree-sync-check.sh
# (PostToolUse[Write,Edit,MultiEdit] hook). Path is recorded at EDIT
# time as repo-relative. The log is cleared by THAT agent's
# iteration-commit on successful commit (further down this script).
#
# Build a single set of all OTHER agents' currently-uncommitted paths.
# Membership check below is O(1) per candidate.
declare -A partner_uncommitted_paths=()
declare -A partner_uncommitted_owner=()  # path -> agent name (first writer wins)
if [[ $INCLUDE_UNTRACKED -eq 0 && -n "${MIND_AGENT:-}" && ${#known_agents[@]} -gt 0 ]]; then
  for partner in "${known_agents[@]}"; do
    if [[ "$partner" == "$MIND_AGENT" ]]; then
      continue
    fi
    partner_log="$REPO/agents/$partner/session/uncommitted-edits.jsonl"
    if [[ -f "$partner_log" ]]; then
      # Extract `file` field from each JSONL record. Skip malformed lines.
      # Strip CR (Windows line endings) — py -3 on Windows emits \r\n by
      # default which read -r preserves the \r in the variable.
      while IFS= read -r recorded_path; do
        recorded_path="${recorded_path%$'\r'}"
        [[ -z "$recorded_path" ]] && continue
        partner_uncommitted_paths["$recorded_path"]=1
        # Don't overwrite — first partner to claim authorship wins. Rare
        # edge case: two agents edited the same file mid-iteration.
        if [[ -z "${partner_uncommitted_owner[$recorded_path]:-}" ]]; then
          partner_uncommitted_owner["$recorded_path"]="$partner"
        fi
      done < <(LOG_PATH="$partner_log" py -3 - 2>/dev/null <<'PYEOF' || true
import json, os, sys
p = os.environ.get("LOG_PATH", "")
try:
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            fp = entry.get("file", "")
            if fp:
                print(fp)
except OSError:
    pass
PYEOF
)
    fi
  done
fi

# --- Committer own uncommitted-edits snapshot () --------------------
# First-person authorship signal for the concurrent-partner filter below.
# The  concurrent-partner filter keys ONLY on (partner in_flight
# non-null + neutral-path mtime at/after partner.claimed_at) with ZERO
# authorship signal — it dropped alpha's own  deliverables TWICE
# (commits 8be45e1, 28a3b7a) because charlie held an unrelated OHS in_flight.
# The committer's OWN session/uncommitted-edits.jsonl is the per-edit SSOT
# (uncommitted-edits-record.sh, PostToolUse[Write,Edit,MultiEdit] hook,
# repo-relative `file` — the same store the  partner block parses).
# A neutral path present here PROVES the committer authored it this
# iteration — a stronger signal than a partner's mtime-coincident claim
# window. guard-464: verify the owning agent before cross-agent attribution.
# Fail-safe: missing/empty own log => empty set => concurrent-partner filter
# behaves exactly as before (strictly narrows false-positives, never widens
# true-positive drops). Same gating as the partner snapshots above.
declare -A committer_authored_paths=()
if [[ $INCLUDE_UNTRACKED -eq 0 && -n "${MIND_AGENT:-}" && ${#known_agents[@]} -gt 0 ]]; then
  own_log_snap="$REPO/agents/$MIND_AGENT/session/uncommitted-edits.jsonl"
  if [[ -f "$own_log_snap" ]]; then
    while IFS= read -r recorded_path; do
      recorded_path="${recorded_path%$'\r'}"
      [[ -z "$recorded_path" ]] && continue
      committer_authored_paths["$recorded_path"]=1
    done < <(LOG_PATH="$own_log_snap" py -3 - 2>/dev/null <<'PYEOF' || true
import json, os, sys
p = os.environ.get("LOG_PATH", "")
try:
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            fp = entry.get("file", "")
            if fp:
                print(fp)
except OSError:
    pass
PYEOF
)
  fi
fi

# --- Empty status skip -------------------------------------------------------
# Filter our own lock dir from status output so its presence never influences
# the empty-status decision or downstream parse (). .gitignore handles
# this in real repos; defense-in-depth covers test repos without .gitignore.
status_output=$(git -C "$REPO" status --porcelain 2>&1 | grep -vE '^.. \.iteration-commit-lock(/|$)' || true)
if [[ -z "$status_output" ]]; then
  echo "[$SCRIPT_NAME] skip: no uncommitted changes in $REPO"
  exit 0
fi

# --- Auto-derive type from title prefix --------------------------------------
if [[ -z "$TYPE" ]]; then
  case "$TITLE" in
    Apply:*) TYPE="feat" ;;
    Fix:*) TYPE="fix" ;;
    Maintain:*) TYPE="chore" ;;
    Investigate:*) TYPE="docs" ;;
    Verify:*) TYPE="test" ;;
    Idea:*) TYPE="feat" ;;
    Forge:*) TYPE="feat" ;;
    *) TYPE="chore" ;;
  esac
fi

# --- Compose commit message --------------------------------------------------
# Strip the prefix from the title to avoid duplication: "Apply: foo" → "foo".
clean_title="${TITLE#Apply: }"
clean_title="${clean_title#Fix: }"
clean_title="${clean_title#Maintain: }"
clean_title="${clean_title#Investigate: }"
clean_title="${clean_title#Verify: }"
clean_title="${clean_title#Idea: }"
clean_title="${clean_title#Forge: }"

# Truncate summary line to 72 chars (conventional commit body wrap point).
summary="$TYPE($GOAL_ID): $clean_title"
if [[ ${#summary} -gt 72 ]]; then
  summary="${summary:0:69}..."
fi

# agent_name is computed at lock-acquire time (above) — re-using here.

# Body: goal-id + outcome + optional extra + signature.
body_lines=()
body_lines+=("$GOAL_ID: $TITLE")
body_lines+=("outcome: $OUTCOME")
if [[ -n "$EXTRA_MSG" ]]; then
  body_lines+=("")
  body_lines+=("$EXTRA_MSG")
fi
body_lines+=("")
body_lines+=("Co-Authored-By: $agent_name <noreply@anthropic.com>")

commit_msg="$summary"$'\n\n'"$(printf '%s\n' "${body_lines[@]}")"

# --- File filtering ----------------------------------------------------------
# Sensitive patterns (CLAUDE.md: never commit .env, credentials, etc.).
sensitive_regex='^(\.env|.*\.key$|.*\.pem$|credentials.*|secrets.*)'

# Parse porcelain output: positions 0-1 are status codes, then space, then path.
# Renames "R  old -> new" need special handling — we want the destination.
# Orphan-deletion split (): " D" entries (deleted in worktree) whose
# parent dir no longer exists on disk cause `git add -A -- <path>` to fail
# with "fatal: pathspec did not match any files" because git can't walk a
# missing parent to verify the path. Such entries must be staged via
# `git rm --cached --ignore-unmatch` instead. Canonical trigger: stranded
# .autocompact-serialize-lock/ entries after a PreCompact crash (the
# .gitignore rule prevents new occurrences; this branch handles legacy
# index entries until they fully reconcile).
declare -a staged_files=()
declare -a rm_only_files=()
declare -a skipped_files=()
declare -a cross_agent_files=()
declare -a cross_agent_uncommitted=()  #  + 38fb983: untracked OR modified files predating claimed_at
declare -a cross_agent_concurrent_partner=()  # : files edited DURING a partner's active in_flight (post-committer-claim)
declare -a cross_agent_partner_uncommitted_log=()  # : files recorded in OTHER agent's uncommitted-edits.jsonl (between-claim gap)
declare -a committer_authored_exempt=()  # : files retained despite partner in_flight because committer's OWN uncommitted-edits.jsonl proves first-person authorship

while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  status_code="${line:0:2}"
  # Skip the first 3 chars (status codes + space).
  path="${line:3}"
  # Handle renames: "old -> new"
  if [[ "$path" == *" -> "* ]]; then
    path="${path##* -> }"
  fi
  # Strip surrounding quotes if present (git escapes paths with special chars).
  path="${path#\"}"
  path="${path%\"}"

  base=$(basename "$path")
  if [[ "$base" =~ $sensitive_regex ]]; then
    skipped_files+=("$path")
    continue
  fi

  # Defensive filter: never commit our own lock dir contents ().
  # The .gitignore root rule prevents this in real repos, but in test
  # repos without .gitignore the lock files would otherwise leak into
  # commits — and even with .gitignore, defense-in-depth is cheap.
  if [[ "$path" == .iteration-commit-lock/* ]]; then
    continue
  fi

  # Root-runtime denylist (plan v1 step 0.8, 2026-05-19) — second layer
  # of defense behind .gitignore. The .gitignore rules added in step 0.21
  # cover .stop-hook-log, .stop-hook-timing.jsonl, .sid-collisions.jsonl,
  # .bash-inject-misses.jsonl, .hook-fires/, .runtime/ (legacy daemon-state
  # dir — relocated to mind_api/state/ in Phase 2, still denylisted here as
  # defense against stale leftovers), .pytest_cache/, .migration-tmp/,
  # .runtime-tmp/, g115873test/, and the misplaced core/.pycache/ +
  # core/.pytest_cache/ caches. If any of these slip past .gitignore (e.g.,
  # a stale test fixture rerun or a manually-removed .gitignore line),
  # this filter still refuses to commit them. The patterns are anchored at
  # the path start so subdirectory files are caught too (e.g.,
  # .hook-fires/foo, .runtime-tmp/bar).
  case "$path" in
    .stop-hook-log|.stop-hook-timing.jsonl|.sid-collisions.jsonl|.bash-inject-misses.jsonl)
      skipped_files+=("$path")
      continue ;;
    .hook-fires/*|.runtime/*|.runtime-tmp/*|.migration-tmp/*|.pytest_cache/*)
      skipped_files+=("$path")
      continue ;;
    core/.pycache/*|core/.pytest_cache/*)
      skipped_files+=("$path")
      continue ;;
    g115873test|g115873test/*)
      skipped_files+=("$path")
      continue ;;
    .bash-inject-no-binding-*)
      skipped_files+=("$path")
      continue ;;
  esac

  # Namespace filter (): drop paths under OTHER agents' dirs.
  # Only active when known_agents was populated (MIND_AGENT set AND agent
  # dirs discovered). Filters by agent-segment after the agents/ parent
  # (Phase 2.5.D layout) to avoid false-matches on look-alike top-level
  # dirs (e.g., "alpha-zoo/" is NOT alpha's dir).
  if [[ ${#known_agents[@]} -gt 0 ]]; then
    if [[ "$path" == agents/* ]]; then
      rest_seg="${path#agents/}"
      top_seg="${rest_seg%%/*}"
    else
      top_seg="${path%%/*}"
    fi
    is_other_agent=0
    for a in "${known_agents[@]}"; do
      if [[ "$a" == "$top_seg" && "$a" != "$MIND_AGENT" ]]; then
        is_other_agent=1
        break
      fi
    done
    if [[ $is_other_agent -eq 1 ]]; then
      cross_agent_files+=("$path")
      continue
    fi
  fi

  # Orphan-deletion check: " D" + missing parent dir → git rm --cached path
  if [[ "$status_code" == " D" ]]; then
    parent="$(dirname "$path")"
    if [[ -n "$parent" && "$parent" != "." && ! -d "$REPO/$parent" ]]; then
      rm_only_files+=("$path")
      continue
    fi
  fi

  # Cross-agent uncommitted filter ( + 38fb983 extension): for files
  # at NEUTRAL paths (not under ANY known agent dir, including the committer's
  # own), skip if mtime predates committer's claimed_at by >5s (partner WIP
  # captured by snapshot). Files under the committer's own agent dir are
  # unconditionally legitimate own-work across iterations and must not be
  # filtered — the namespace filter above already drops OTHER agents' files;
  # this filter only addresses the gap at neutral paths (core/, .claude/,
  # .gitignore, PROJECT_ROOT-level files) where the agent attribution is
  # ambiguous.
  #
  # Status codes covered (pre-stage git porcelain):
  #   "??" — untracked (original  case)
  #   " M" — modified in worktree, not staged
  #   "MM" — staged-modified then re-modified
  #   "AM" — staged-add then modified
  #   " A" — added to index but with worktree changes (rare; git add -N)
  # All five share the same failure mode: partner's uncommitted work at a
  # neutral path gets swept by `git add -A` during this agent's commit
  # ceremony. The M-status extension was motivated by commit 38fb983
  # (2026-05-13, 3rd observed recurrence — alpha's iteration-commit for
  #  swept bravo's 9  M-status files at core/scripts/).
  #
  # False-positive risk: agent edits a file BEFORE calling claim. mtime then
  # predates claimed_at and the filter mistakenly drops own-work. Mitigation:
  # the 5s tolerance window catches near-simultaneous claim+edit; for >5s
  # pre-claim edits the agent must pass --include-untracked. Acceptable
  # tradeoff — the partner-sweep failure mode is observably 3x more common
  # in practice than the pre-claim-edit pattern.
  case "$status_code" in
    "??"|" M"|"MM"|"AM"|" A") cross_agent_check_applies=1 ;;
    *) cross_agent_check_applies=0 ;;
  esac
  if [[ $cross_agent_check_applies -eq 1 && $committer_claimed_at_epoch -gt 0 && ${#known_agents[@]} -gt 0 ]]; then
    # Phase 2.5.D: agent dirs live under agents/<agent>/. Extract the
    # owning-agent segment from path prefix `agents/<agent>/...`. For
    # non-agents-prefixed paths (core/, .claude/, world-level), the
    # top segment is checked but won't match any agent → treated as
    # neutral path (cross-agent filter applies).
    if [[ "$path" == agents/* ]]; then
      pc_rest="${path#agents/}"
      pc_top_seg="${pc_rest%%/*}"
    else
      pc_top_seg="${path%%/*}"
    fi
    pc_is_agent_dir=0
    for a in "${known_agents[@]}"; do
      if [[ "$a" == "$pc_top_seg" ]]; then
        pc_is_agent_dir=1
        break
      fi
    done
    if [[ $pc_is_agent_dir -eq 0 ]]; then
      file_full="$REPO/$path"
      if [[ -f "$file_full" || -d "$file_full" ]]; then
        file_mtime=$(FILE_E="$file_full" py -3 - 2>/dev/null <<'PYEOF' || echo 0
import os, sys
try:
    print(int(os.path.getmtime(os.environ["FILE_E"])))
except Exception:
    print(0)
PYEOF
)
        if [[ $file_mtime -gt 0 && $((committer_claimed_at_epoch - file_mtime)) -gt 5 ]]; then
          cross_agent_uncommitted+=("$path")
          continue
        fi
      fi
    fi
  fi

  # Concurrent-partner filter (): if any partner has non-null in_flight
  # AND the file was modified at-or-after that partner's claim (within 5s
  # tolerance for clock skew), this is a concurrent partner edit. The pre-claim
  # check above catches partner WIP that PREDATES the committer's claim; this
  # branch catches active partner work happening DURING the committer's
  # iteration. Falsified by : alpha's commit at 09:25 swept zeta's
  # edit at ~09:20 because zeta's claim preceded the edit, but the pre-claim
  # check only filtered files predating alpha's claim (~09:00).
  #
  # Same path-eligibility gating as the pre-claim filter: neutral paths only
  # (not under ANY known agent dir). Files under the committer's own agent dir
  # are legitimate own work; files under another agent's dir are dropped by
  # the namespace filter higher up. This branch only addresses the gap at
  # neutral paths (core/, .claude/, world-level files) where attribution is
  # ambiguous and the partner's active claim is the strongest signal.
  if [[ $cross_agent_check_applies -eq 1 && ${#partner_claimed_at_epochs[@]} -gt 0 && ${#known_agents[@]} -gt 0 ]]; then
    # Phase 2.5.D: same agents/<agent>/ extraction as the pre-claim filter.
    if [[ "$path" == agents/* ]]; then
      pc_rest_p="${path#agents/}"
      pc_top_seg_p="${pc_rest_p%%/*}"
    else
      pc_top_seg_p="${path%%/*}"
    fi
    pc_is_agent_dir_p=0
    for a in "${known_agents[@]}"; do
      if [[ "$a" == "$pc_top_seg_p" ]]; then
        pc_is_agent_dir_p=1
        break
      fi
    done
    if [[ $pc_is_agent_dir_p -eq 0 ]]; then
      file_full_p="$REPO/$path"
      if [[ -f "$file_full_p" || -d "$file_full_p" ]]; then
        file_mtime_p=$(FILE_E="$file_full_p" py -3 - 2>/dev/null <<'PYEOF' || echo 0
import os, sys
try:
    print(int(os.path.getmtime(os.environ["FILE_E"])))
except Exception:
    print(0)
PYEOF
)
        if [[ $file_mtime_p -gt 0 ]]; then
          matched_partner=""
          matched_iso=""
          for i in "${!partner_claimed_at_epochs[@]}"; do
            partner_ep="${partner_claimed_at_epochs[$i]}"
            # partner.claimed_at <= file_mtime within 5s tolerance:
            # file_mtime + 5 >= partner_ep (allows 5s clock skew).
            if [[ $((file_mtime_p + 5)) -ge $partner_ep ]]; then
              matched_partner="${partner_names_with_in_flight[$i]}"
              matched_iso="${partner_claimed_at_isos[$i]}"
              break
            fi
          done
          if [[ -n "$matched_partner" ]]; then
            if [[ -n "${committer_authored_paths["$path"]:-}" ]]; then
              # : the committer's OWN uncommitted-edits.jsonl
              # recorded this neutral path — first-person authorship proof
              # overrides the mtime-coincident partner in_flight signal
              # (guard-464: owning agent verified before cross-agent
              # attribution). Genuine partner edits, absent from the
              # committer's own log, still drop via the else branch.
              committer_authored_exempt+=("$path|$matched_partner")
            else
              # Encode as path|partner|iso for the WARN block (PIPE chosen because
              # neither legal POSIX paths nor ISO timestamps contain it).
              cross_agent_concurrent_partner+=("$path|$matched_partner|$matched_iso")
              continue
            fi
          fi
        fi
      fi
    fi
  fi

  # Partner-uncommitted-log filter (): if any OTHER agent's
  # uncommitted-edits.jsonl recorded this path, the partner authored it
  # (regardless of current in_flight state). Closes the between-claim
  # attribution gap by using per-edit recording as the SSOT rather than
  # team-state.in_flight (which clears between claims).
  #
  # Same neutral-path-only intent as the sibling filters above: the log
  # only records neutral-path edits (uncommitted-edits-record.sh gates
  # internally). No re-gating needed here.
  #
  # Git porcelain collapses untracked dirs into a single entry ending with
  # `/` (e.g., `?? mind_api/src/` when the entire dir is new). The record
  # script writes file-level entries (`mind_api/src/agent_paths.py`). The
  # exact-match check below catches modified tracked files; the
  # prefix-match catches candidate-dir entries that CONTAIN a recorded
  # partner file.
  pl_match_owner=""
  if [[ -n "${partner_uncommitted_paths["$path"]:-}" ]]; then
    pl_match_owner="${partner_uncommitted_owner[$path]}"
  else
    # Candidate is dir-ish (ends with /) — scan recorded paths for any that
    # begin with candidate's prefix. Linear scan, but the log is small
    # (cleared per commit) so this is cheap.
    case "$path" in
      */)
        for recorded in "${!partner_uncommitted_paths[@]}"; do
          case "$recorded" in
            "$path"*)
              pl_match_owner="${partner_uncommitted_owner[$recorded]}"
              break
              ;;
          esac
        done
        ;;
    esac
  fi
  if [[ -n "$pl_match_owner" ]]; then
    cross_agent_partner_uncommitted_log+=("$path|$pl_match_owner")
    continue
  fi

  staged_files+=("$path")
done <<< "$status_output"

if [[ ${#staged_files[@]} -eq 0 && ${#rm_only_files[@]} -eq 0 ]]; then
  # Additive reporting: print every filter category that fired non-zero
  # AND aggregate the totals. Replaces the prior mutually-exclusive branches
  # which dropped the partner-uncommitted-log message when other filters also
  # fired ().
  total=$(( ${#cross_agent_uncommitted[@]} + ${#cross_agent_concurrent_partner[@]} + ${#cross_agent_partner_uncommitted_log[@]} + ${#cross_agent_files[@]} + ${#skipped_files[@]} ))
  if [[ $total -eq 0 ]]; then
    echo "[$SCRIPT_NAME] skip: no uncommitted changes for $MIND_AGENT (after filters)"
  else
    echo "[$SCRIPT_NAME] skip: all uncommitted files filtered (total=$total: ${#cross_agent_uncommitted[@]} pre-claim, ${#cross_agent_concurrent_partner[@]} concurrent-partner, ${#cross_agent_partner_uncommitted_log[@]} partner-log, ${#cross_agent_files[@]} namespace, ${#skipped_files[@]} sensitive; committer=$MIND_AGENT)" >&2
    for f in "${cross_agent_uncommitted[@]}"; do echo "  filtered (cross-agent-uncommitted): $f" >&2; done
    for entry in "${cross_agent_concurrent_partner[@]}"; do
      IFS='|' read -r cp_path cp_partner cp_iso <<< "$entry"
      echo "  filtered (concurrent-partner): $cp_path (partner=$cp_partner in_flight @ $cp_iso)" >&2
    done
    for entry in "${cross_agent_partner_uncommitted_log[@]}"; do
      IFS='|' read -r pl_path pl_partner <<< "$entry"
      echo "  filtered (partner-uncommitted-log): $pl_path (partner=$pl_partner)" >&2
    done
    for f in "${cross_agent_files[@]}"; do echo "  filtered (cross-agent): $f" >&2; done
    for f in "${skipped_files[@]}"; do echo "  filtered (sensitive): $f" >&2; done
  fi
  exit 0
fi

if [[ ${#skipped_files[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] WARN: skipping sensitive files:" >&2
  for f in "${skipped_files[@]}"; do echo "  $f" >&2; done
fi

if [[ ${#rm_only_files[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] INFO: ${#rm_only_files[@]} orphan deletion(s) (parent dir missing) — will stage via git rm --cached" >&2
  for f in "${rm_only_files[@]}"; do echo "  rm: $f" >&2; done
fi

if [[ ${#cross_agent_files[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] INFO: namespace filter dropped ${#cross_agent_files[@]} cross-agent file(s) (committer=$MIND_AGENT, known agents=${known_agents[*]})" >&2
  for f in "${cross_agent_files[@]}"; do echo "  filtered (cross-agent): $f" >&2; done
fi

if [[ ${#cross_agent_uncommitted[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] WARN: cross-agent mtime filter dropped ${#cross_agent_uncommitted[@]} file(s) (untracked OR modified) predating committer.claimed_at ($committer_claimed_at_iso) — likely partner WIP at neutral paths" >&2
  for f in "${cross_agent_uncommitted[@]}"; do echo "  filtered (cross-agent-uncommitted): $f" >&2; done
  echo "[$SCRIPT_NAME] HINT: pass --include-untracked to override the filter (rare; only when committer legitimately authored these files before claiming)" >&2
fi

if [[ ${#cross_agent_concurrent_partner[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] WARN: concurrent-partner filter dropped ${#cross_agent_concurrent_partner[@]} file(s) edited during a partner's active in_flight at neutral paths (committer=$MIND_AGENT) — likely partner work in another iteration" >&2
  for entry in "${cross_agent_concurrent_partner[@]}"; do
    IFS='|' read -r cp_path cp_partner cp_iso <<< "$entry"
    echo "  filtered (concurrent-partner): $cp_path (partner=$cp_partner in_flight @ $cp_iso)" >&2
  done
  echo "[$SCRIPT_NAME] HINT: pass --include-untracked to override the filter (rare; only when committer legitimately authored these files during the partner's iteration)" >&2
fi

if [[ ${#committer_authored_exempt[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] INFO: g-115-828 first-person-authorship exemption retained ${#committer_authored_exempt[@]} neutral-path file(s) the concurrent-partner filter would have dropped (committer=$MIND_AGENT authored them per own uncommitted-edits.jsonl despite partner in_flight)" >&2
  for entry in "${committer_authored_exempt[@]}"; do
    IFS='|' read -r ce_path ce_partner <<< "$entry"
    echo "  retained (committer-authored): $ce_path (partner=$ce_partner had concurrent in_flight; own-log overrode)" >&2
  done
fi

if [[ ${#cross_agent_partner_uncommitted_log[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] WARN: partner-uncommitted-log filter dropped ${#cross_agent_partner_uncommitted_log[@]} file(s) recorded in OTHER agent's uncommitted-edits.jsonl (committer=$MIND_AGENT) — closes g-115-695 between-claim attribution gap" >&2
  for entry in "${cross_agent_partner_uncommitted_log[@]}"; do
    IFS='|' read -r pl_path pl_partner <<< "$entry"
    echo "  filtered (partner-uncommitted-log): $pl_path (partner=$pl_partner)" >&2
  done
  echo "[$SCRIPT_NAME] HINT: pass --include-untracked to override the filter (rare; only when committer legitimately authored these files during the partner's between-claim window)" >&2
fi

# --- Dry-run output ----------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
  echo "[$SCRIPT_NAME] DRY-RUN — would commit in $REPO:"
  echo "---"
  echo "$commit_msg"
  echo "---"
  echo "files to stage (git add):"
  for f in "${staged_files[@]}"; do echo "  $f"; done
  if [[ ${#rm_only_files[@]} -gt 0 ]]; then
    echo "files to stage (git rm --cached):"
    for f in "${rm_only_files[@]}"; do echo "  $f"; done
  fi
  exit 0
fi

# --- Stage + commit ----------------------------------------------------------
if [[ ${#staged_files[@]} -gt 0 ]]; then
  git -C "$REPO" add -A -- "${staged_files[@]}" 2>&1 || {
    echo "[$SCRIPT_NAME] ERROR: git add failed in $REPO" >&2
    exit 2
  }
fi

if [[ ${#rm_only_files[@]} -gt 0 ]]; then
  # --ignore-unmatch tolerates paths already absent from the index (idempotent).
  git -C "$REPO" rm --cached --ignore-unmatch -- "${rm_only_files[@]}" 2>&1 || {
    echo "[$SCRIPT_NAME] WARN: git rm --cached failed for orphan deletions (continuing)" >&2
  }
  # Append for JSON output so callers see what was staged.
  staged_files+=("${rm_only_files[@]}")
fi

# Commit via stdin to avoid arg-length issues with long messages.
# Multi-agent retry loop (): git natively serializes via .git/index.lock,
# but Windows-specific transient issues (antivirus scan holding files, sharing
# violations) can produce spurious commit failures even when conceptually correct.
# Retry up to 3 times with 1s backoff. Scope intentionally limited to commit (not
# add): the add operation is fast and less likely to collide. The mkdir-lock
# above () now closes the cross-agent authorship-bleed race; this retry
# handles transient infrastructure faults orthogonal to coordination semantics.
MAX_RETRIES=3
RETRY_BACKOFF_S=1
commit_attempt=0
commit_success=0
commit_last_output=""
while [[ $commit_attempt -lt $MAX_RETRIES ]]; do
  commit_attempt=$((commit_attempt + 1))
  commit_last_output=$(echo "$commit_msg" | git -C "$REPO" commit -F - 2>&1) && {
    commit_success=1
    if [[ $commit_attempt -gt 1 ]]; then
      echo "[$SCRIPT_NAME] INFO: commit succeeded on retry $commit_attempt/$MAX_RETRIES" >&2
    fi
    break
  }
  if [[ $commit_attempt -lt $MAX_RETRIES ]]; then
    echo "[$SCRIPT_NAME] WARN: commit attempt $commit_attempt/$MAX_RETRIES failed (will retry in ${RETRY_BACKOFF_S}s): $commit_last_output" >&2
    sleep "$RETRY_BACKOFF_S"
  fi
done

if [[ $commit_success -eq 0 ]]; then
  echo "[$SCRIPT_NAME] ERROR: git commit failed after $MAX_RETRIES attempts in $REPO" >&2
  echo "[$SCRIPT_NAME] last error: $commit_last_output" >&2
  exit 2
fi

commit_sha=$(git -C "$REPO" rev-parse HEAD)

# --- Clear own uncommitted-edits.jsonl on committed paths () --------
# After a successful self-commit, prune entries from THIS agent's
# uncommitted-edits.jsonl whose `file` matches a path we just committed.
# Entries for files we haven't committed yet (in-flight edits to other
# files) remain — partner iteration-commits still need that signal until
# this agent commits them too.
#
# Idempotent: if the log doesn't exist, exit silently. If a committed path
# wasn't recorded, no-op. The clear is a filter (read-rewrite), not a
# truncate — preserves not-yet-committed entries.
if [[ -n "${MIND_AGENT:-}" && ${#staged_files[@]} -gt 0 ]]; then
  own_log="$REPO/agents/$MIND_AGENT/session/uncommitted-edits.jsonl"
  if [[ -f "$own_log" ]]; then
    # Build newline-delimited set of committed rel paths.
    committed_set=$(printf '%s\n' "${staged_files[@]}")
    OWN_LOG="$own_log" COMMITTED="$committed_set" py -3 - 2>/dev/null <<'PYEOF' || true
import json, os, tempfile, sys
own = os.environ.get("OWN_LOG", "")
committed = set(p.strip() for p in os.environ.get("COMMITTED", "").splitlines() if p.strip())
if not own or not os.path.exists(own):
    sys.exit(0)
kept = []
try:
    with open(own, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            fp = entry.get("file", "")
            if fp and fp not in committed:
                kept.append(line)
except OSError:
    sys.exit(0)
# Atomic rewrite: write to tempfile then rename. Truncate-to-zero-then-write
# would race with partner reads under high concurrency.
dir_ = os.path.dirname(own) or "."
with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=dir_, delete=False, suffix=".tmp"
) as tf:
    for line in kept:
        tf.write(line + "\n")
    tmp_path = tf.name
os.replace(tmp_path, own)
PYEOF
  fi
fi

# --- Output JSON -------------------------------------------------------------
files_json=""
for f in "${staged_files[@]}"; do
  if [[ -n "$files_json" ]]; then files_json="$files_json,"; fi
  # Escape backslashes and quotes for JSON.
  esc="${f//\\/\\\\}"
  esc="${esc//\"/\\\"}"
  files_json="$files_json\"$esc\""
done

printf '{"commit_sha":"%s","files_committed":[%s],"repo":"%s","goal_id":"%s","outcome":"%s","type":"%s"}\n' \
  "$commit_sha" "$files_json" "$REPO" "$GOAL_ID" "$OUTCOME" "$TYPE"
