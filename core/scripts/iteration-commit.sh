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
    $REPO/agents/*/self.md. Disabled when $MIND_AGENT is unset OR no agent
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
# part of this commit's intent. Tolerate 5s of clock skew.
#
# INERT-BY-DESIGN IN THE NORMAL CLOSE PATH ( / , verified
# 2026-06-10). committer_claimed_at_epoch resolves to 0 — so the pre-claim
# filter below (guarded by `committer_claimed_at_epoch -gt 0`) is SKIPPED —
# during EVERY normal deep-outcome close, not merely fresh-install/test repos.
# iteration-close.sh do_verify ( Step 3) clears team-state.in_flight
# BEFORE do_state_update invokes this script, so the live in_flight.claimed_at
# read below returns null at commit time. self_claimed_at=0 is the RULE, not
# the exception. This is the SAME root cause pinned for the sibling Python
# filter _cross_agent_attribution_filter.py by , whose regression
# test (test_attribution_filter_no_self_inflight.py) records the team decision:
# do NOT add a "Source 4" claim-time anchor (e.g. snapshotting claimed_at into
# iteration-checkpoint.json and reading THAT) — the standing bias is "never
# silently drop self-authored work," and a stale/wrong anchor risks exactly
# that. The PRIMARY defense for partner WIP at neutral paths is therefore the
# partner-uncommitted-log filter (, below): it never reads claimed_at,
# so it drops logged partner edits regardless of in_flight state, and its input
# is comprehensive — every Write/Edit/MultiEdit routes through the
# uncommitted-edits-record.sh PostToolUse hook. The residual uncovered surface
# is partner files created via NON-hook paths (shell cp/touch/redirect) that
# also predate the claim and are absent from the partner's log — narrow, and
# accepted as best-effort here rather than closed with a fragile anchor.
# Fail-open: when the anchor is genuinely absent (this path), the filter simply
# does not fire.
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
      done < <(LOG_PATH="$partner_log" XAGENT_SCRIPTS="$REPO/core/scripts" XAGENT_ROOT="$REPO" py -3 - 2>/dev/null <<'PYEOF' || true
import json, os, sys, time
# Normalize recorded paths to PROJECT_ROOT-relative POSIX form so legacy
# absolute uncommitted-edits.jsonl entries (C:/...) match the relative
# git-status candidates checked below (). SINGLE SOURCE OF TRUTH
# (rb-1405): import the SAME normalizer AND age-cutoff helpers the Python
# attribution filter uses so the two uncommitted-edits.jsonl consumers cannot
# drift on path format OR staleness policy ().
sys.path.insert(0, os.environ.get("XAGENT_SCRIPTS", ""))
try:
    from _cross_agent_attribution_filter import (
        _normalize_rel_path, _entry_is_stale, _max_age_sec,
    )
except Exception:
    def _normalize_rel_path(p, root):  # fail-open: identity if import fails
        return p
    def _entry_is_stale(rec, max_age_sec, now_epoch):  # fail-open: never drop
        return False
    def _max_age_sec():
        return 48 * 3600.0
proot = os.environ.get("XAGENT_ROOT", "")
p = os.environ.get("LOG_PATH", "")
# : a partner claim older than the cutoff (edit_ts/mtime) is STALE and
# must not suppress the committer's legitimate same-session edit (msg-1904 — a
# 3-week-old charlie settings.json entry dropped alpha's edit). SSOT staleness
# policy lives in _cross_agent_attribution_filter; both consumers call it.
max_age_sec = _max_age_sec()
now_epoch = int(time.time())
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
            if fp and not _entry_is_stale(entry, max_age_sec, now_epoch):
                print(_normalize_rel_path(fp, proot))
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
    done < <(LOG_PATH="$own_log_snap" XAGENT_SCRIPTS="$REPO/core/scripts" XAGENT_ROOT="$REPO" py -3 - 2>/dev/null <<'PYEOF' || true
import json, os, sys
# Normalize own-log paths to PROJECT_ROOT-relative POSIX form so the
# committer_authored_paths membership check below (keyed on git-status
# candidate `$path`, which IS repo-relative) compares equal regardless of
# whether the entry was stored absolute (legacy C:/... entries) or relative.
# WITHOUT this, an absolute own-log entry silently misses the relative
# candidate, the  first-person-authorship exemption never fires, and
# the concurrent-partner filter drops the committer's OWN file (over-exclusion;
# guard-608 / commit 7f1df61d). SINGLE SOURCE OF TRUTH (rb-1405, ):
# this is the SAME _normalize_rel_path the partner-log snapshot above ()
# imports, so the two uncommitted-edits.jsonl consumers cannot drift on path
# format. Fix:  (the partner-log consumer got this fix at ;
# the own-log consumer added at  never did — asymmetry closed here).
#
# Deliberate asymmetry vs the partner block: the own-log applies normalization
# but NOT the _entry_is_stale staleness filter. Staleness has opposite valence
# per side — on the partner (DROP) side a stale claim must NOT suppress the
# committer's edit; on the own (RETAIN) side a stale own entry suppressing the
# exemption would cause MORE drops (over-exclusion), the very failure mode this
# fix removes. So the retain side intentionally keeps the exemption regardless
# of age. Do NOT "symmetrize" by adding staleness here.
sys.path.insert(0, os.environ.get("XAGENT_SCRIPTS", ""))
try:
    from _cross_agent_attribution_filter import _normalize_rel_path
except Exception:
    def _normalize_rel_path(p, root):  # fail-open: identity if import fails
        return p
proot = os.environ.get("XAGENT_ROOT", "")
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
                print(_normalize_rel_path(fp, proot))
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

# Content-pattern filter (g-315-?? — 2026-05-21 rotate-script incident).
# Filename-only filtering missed agents/alpha/scripts/rotate-lambda-common-pat.sh,
# which carried a 33-char identifier-truncation of a fine-grained PAT inside
# a script whose name matched none of the sensitive_regex tokens. This regex
# scans file content for token shapes (≥20 chars after the type-prefix —
# enough to be a unique identifier even when truncated). Matched files are
# added to skipped_files with a stderr warning. Defense-in-depth complement
# to Gate 8 (core/scripts/check-no-hardcoded-secrets.sh): iteration-commit
# pre-stage filter keeps files out of auto-commits, Gate 8 catches anything
# that slips into user-initiated `git commit`. Patterns must stay in sync
# with check-no-hardcoded-secrets.sh.
content_secret_regex='(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|ghu_[A-Za-z0-9]{20,}|ghs_[A-Za-z0-9]{20,}|ghr_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})'
declare -a content_skipped_files=()

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
declare -a staged_del_files=()  # : already-staged deletions (porcelain "D ") — routed OUT of the git-add pathspec batch (an already-staged-deleted path matches nothing in worktree or index, so it aborts the whole `git add -A -- ...` with rc=128 and drops the entire commit)
declare -a skipped_files=()
declare -a cross_agent_files=()
declare -a cross_agent_uncommitted=()  #  + 38fb983: untracked OR modified files predating claimed_at
declare -a cross_agent_concurrent_partner=()  # : files edited DURING a partner's active in_flight (post-committer-claim)
declare -a cross_agent_partner_uncommitted_log=()  # : files recorded in OTHER agent's uncommitted-edits.jsonl (between-claim gap)
declare -a committer_authored_exempt=()  # : files retained despite partner in_flight because committer's OWN uncommitted-edits.jsonl proves first-person authorship
declare -a committer_authored_log_exempt=()  # : files retained despite a partner ALSO recording them in uncommitted-edits.jsonl, because the committer's OWN log proves first-person authorship (the own-log check the  partner-log filter previously lacked — asymmetry with the  concurrent-partner filter)

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

  # Content-secret filter (see content_secret_regex header above).
  # Only scans existing files (additions, modifications) — deleted files
  # have no content to scan and would error on grep. Self-allowlist:
  # the scanner script itself, gitignore, and test fixtures (parallels
  # check-no-hardcoded-secrets.sh allowed_path()).
  if [[ -f "$path" ]]; then
    case "$path" in
      core/scripts/check-no-hardcoded-secrets.sh) : ;;
      core/scripts/iteration-commit.sh)           : ;;
      core/scripts/tests/fixtures/*)              : ;;
      .gitignore)                                 : ;;
      *)
        # Honor the per-line `# secret-scanner: skip` marker that the sibling
        # pre-commit gate (check-no-hardcoded-secrets.sh) already honors, so the
        # two scanners agree on ONE bypass contract instead of two divergent ones
        # (guard-1280: route every sink through a single source of truth).
        #
        # Without this, a line the pre-commit gate explicitly sanctions still makes
        # the WHOLE file permanently unstageable here — silently, since the WARN
        # below is one stderr line amid a long commit log. Observed :
        # verify-learning/SKILL.md added a check scanning for unredacted AWS key
        # IDs, which necessarily names the canonical AWS *documentation example*
        # key as its own false-positive exclusion. That literal matched the regex
        # below, so from d280d96f4 onward every edit to the framework's largest
        # regression surface (~2490 assertions) stayed uncommitted. guard-1668 is
        # the general class: a check that forbids a pattern, whose scan surface
        # includes the file defining the check, hits on its own documentation.
        # Test the OUTPUT for non-emptiness rather than the pipeline's rc — correct
        # under every grep implementation, so it cannot regress on a box whose grep
        # differs. The sibling gate uses the same output-capturing form.
        #
        # The rc form (`| grep -qv ...`) is a real trap, though NOT for this script.
        # Measured on cc-05: `grep -qv` on EMPTY stdin returns 0 under ugrep 7.5.0
        # and 1 under GNU grep 3.11 — and 0 means "skip" here, so the rc form marks
        # every CLEAN file as a hit. Scripts are safe (a script's `grep` resolves to
        # /usr/bin/grep = GNU). It bites when an agent HAND-TESTS the predicate in
        # its interactive shell, where the user profile defines a `grep` FUNCTION
        # wrapping ugrep that child processes never inherit (BASH_FUNC_grep is not
        # exported). That asymmetry is the hazard: the hand-run result is genuine
        # output from the genuine predicate, so it reads as authoritative while
        # describing an environment the script never runs in. See
        # probe-with-canonical-code-path.md, "Canonical BINARY Is Not Canonical
        # INVOCATION" — this is that class, with the shell itself as the wrong arg
        # shape. The output form is kept because it is right either way.
        unmarked_hits=$(grep -nE "$content_secret_regex" "$path" 2>/dev/null \
                          | grep -vE 'secret-scanner:[[:space:]]*skip' || true)
        if [[ -n "$unmarked_hits" ]]; then
          echo "[$SCRIPT_NAME] WARN: $path contains token-shaped content — skipping (file stays in working tree)" >&2
          echo "[$SCRIPT_NAME]       If intentional (an example/test token), append '# secret-scanner: skip' to that line." >&2
          content_skipped_files+=("$path")
          skipped_files+=("$path")
          continue
        fi
        ;;
    esac
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

  # Already-staged-deletion check (): porcelain "D " (index column D,
  # clean worktree) means the path is gone from BOTH the worktree AND the index.
  # Including it in the batched `git add -A -- "${staged_files[@]}"` pathspec at
  # the stage step makes git abort the ENTIRE batch with "pathspec did not match
  # any files" (rc=128 → exit 2), silently dropping every legitimate file in this
  # commit (confirmed empirically: a mixed `git add -A -- good staged_del
  # also_good` stages NOTHING on the abort). The deletion is ALREADY correctly
  # staged in the shared index and needs no re-staging — route it out of the add
  # pathspec; the dedicated staging block below re-runs `git rm --cached
  # --ignore-unmatch` on it (a safe rc=0 no-op on an already-removed index entry
  # that preserves the staged deletion for the commit). Distinct from the orphan
  # check above: that handles a WORKTREE deletion (" D") whose parent dir
  # vanished; this handles an INDEX-staged deletion ("D ") regardless of
  # parent-dir presence. (A " D" with parent PRESENT stages fine via add -A, so
  # it is intentionally NOT caught here.)
  if [[ "$status_code" == "D " ]]; then
    staged_del_files+=("$path")
    continue
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
    if [[ -n "${committer_authored_paths["$path"]:-}" ]]; then
      # : the committer's OWN uncommitted-edits.jsonl ALSO recorded
      # this path — first-person authorship proof overrides the partner-log
      # signal, mirroring the  own-log check in the concurrent-partner
      # filter above (line ~818). The  partner-log filter was built
      # WITHOUT this check (an asymmetry with the  filter), so a single
      # physical edit double-recorded under BOTH the committer's and a partner's
      # uncommitted-edits.jsonl (the  between-claim attribution overlap)
      # was silently DROPPED from the committer's own commit. That drop is a WARN
      # not an ERROR, so a deletion-free goal could report exit-0 success while
      # leaving its own deep-close edits uncommitted (observed ,
      # ). Retain it; genuine partner edits absent from the committer's
      # own log still drop via the else branch.
      committer_authored_log_exempt+=("$path|$pl_match_owner")
    else
      cross_agent_partner_uncommitted_log+=("$path|$pl_match_owner")
      continue
    fi
  fi

  staged_files+=("$path")
done <<< "$status_output"

if [[ ${#staged_files[@]} -eq 0 && ${#rm_only_files[@]} -eq 0 && ${#staged_del_files[@]} -eq 0 ]]; then
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

if [[ ${#staged_del_files[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] INFO: ${#staged_del_files[@]} already-staged deletion(s) (porcelain \"D \") — routed out of the git-add batch and re-staged idempotently via git rm --cached (g-115-1620: prevents the pathspec-not-found abort that would otherwise drop the whole commit)" >&2
  for f in "${staged_del_files[@]}"; do echo "  staged-del: $f" >&2; done
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

if [[ ${#committer_authored_log_exempt[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] INFO: g-115-1620 first-person-authorship exemption retained ${#committer_authored_log_exempt[@]} neutral-path file(s) the partner-uncommitted-log filter would have dropped (committer=$MIND_AGENT recorded them in its OWN uncommitted-edits.jsonl despite a partner ALSO recording them — g-115-695 between-claim double-recording)" >&2
  for entry in "${committer_authored_log_exempt[@]}"; do
    IFS='|' read -r cle_path cle_partner <<< "$entry"
    echo "  retained (committer-authored-log): $cle_path (partner=$cle_partner also recorded; own-log overrode)" >&2
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

# --- Stash-overlap filter () ---------------------------------------
# Defends against the rb-1127 / 3c4a61c4 stash-clobber incident: when agent A
# stashes work (or an external `git stash push` runs) and a partner's
# iteration-commit later sweeps up the recovered files, the partner's signature
# wrongly attributes A's authorship. Probe `git stash list`; if any stashed
# path overlaps the committer's staged set, drop those files from this commit
# and emit a warning naming the stash SHA so the original author can recover
# under their own signature via `git checkout <sha> -- <files>`.
declare -a stash_filtered_files=()
declare -a stash_overlap_log=()
if [[ ${#staged_files[@]} -gt 0 ]]; then
  while IFS=$'\t' read -r stash_sha stash_ref; do
    [[ -z "$stash_sha" ]] && continue
    declare -A _stash_paths=()
    while IFS= read -r stash_path; do
      [[ -z "$stash_path" ]] && continue
      _stash_paths["$stash_path"]=1
    done < <(git -C "$REPO" stash show --name-only "$stash_sha" 2>/dev/null || true)
    for i in "${!staged_files[@]}"; do
      sf="${staged_files[$i]}"
      if [[ -n "${_stash_paths[$sf]:-}" ]]; then
        stash_filtered_files+=("$sf")
        stash_overlap_log+=("${stash_sha:0:8}|${stash_ref}|${sf}")
        unset 'staged_files[i]'
      fi
    done
    unset _stash_paths
  done < <(git -C "$REPO" stash list --format="%H%x09%gd" 2>/dev/null || true)
  staged_files=("${staged_files[@]}")
fi

if [[ ${#stash_filtered_files[@]} -gt 0 ]]; then
  echo "[$SCRIPT_NAME] WARN: stash-overlap filter dropped ${#stash_filtered_files[@]} file(s) matching git stash entries (committer=$MIND_AGENT) — likely partner work captured by an external stash (rb-1127 / 3c4a61c4 pattern)" >&2
  for entry in "${stash_overlap_log[@]}"; do
    IFS='|' read -r so_sha so_ref so_path <<< "$entry"
    echo "  filtered (stash-overlap): $so_path (stash=$so_sha $so_ref — original author recovers via: git checkout $so_sha -- $so_path)" >&2
  done
  echo "[$SCRIPT_NAME] HINT: if these files ARE legitimately yours, recover under your signature with 'git checkout <stash-sha> -- <path>' then re-run iteration-commit. Stash entries are inspected via 'git stash list / git stash show <ref>'." >&2
  if [[ ${#staged_files[@]} -eq 0 && ${#rm_only_files[@]} -eq 0 && ${#staged_del_files[@]} -eq 0 ]]; then
    echo "[$SCRIPT_NAME] INFO: all candidate files filtered by stash-overlap — nothing to commit" >&2
    exit 0
  fi
fi

# --- Over-inclusion audit () ---------------------------------------
# Detection-only complement to the attribution filters above. Those filters are
# a DENYLIST: a neutral-path file is staged unless a partner signal fires
# (namespace / mtime-vs-claim / partner in_flight / partner-log / stash). An
# ORPHAN — a partner's uncommitted shared-path edit whose every signal has
# lapsed (partner not in_flight, edit post-dates committer claim, edit never
# recorded in any uncommitted-edits.jsonl) — is INDISTINGUISHABLE from the
# committer's own unlogged neutral edit. test_concurrent_partner_no_partner_in_
# flight_file_included pins that an own unlogged neutral file MUST stage, so an
# orphan cannot be auto-dropped here without over-excluding legitimate own work.
# Prevention at staging time is therefore impossible; this block makes the
# residual over-inclusion VISIBLE + post-hoc-correctable instead of silently
# swept under the committing goal_id (the  deep auto-override path).
# A flagged file is EITHER a recording gap (own edit missing from the own-log)
# OR a mis-attributed partner orphan — both actionable. The deny-vs-allow-list
# contract question is the design decision tracked by  / .
if [[ "$OUTCOME" == "deep" && ${#staged_files[@]} -gt 0 && -n "${MIND_AGENT:-}" && ${#known_agents[@]} -gt 0 ]]; then
  declare -a unattributed_neutral=()
  for sf in "${staged_files[@]}"; do
    # neutral-path test: first segment is not a known agent dir (mirrors the
    # agents/<agent>/ extraction the filters above use).
    if [[ "$sf" == agents/* ]]; then
      _oi_rest="${sf#agents/}"; _oi_top="${_oi_rest%%/*}"
    else
      _oi_top="${sf%%/*}"
    fi
    _oi_is_agent=0
    for a in "${known_agents[@]}"; do
      if [[ "$a" == "$_oi_top" ]]; then _oi_is_agent=1; break; fi
    done
    [[ $_oi_is_agent -eq 1 ]] && continue   # agent-dir paths handled by namespace filter
    # neutral path: flag if NOT positively attributed via the committer own-log.
    if [[ -z "${committer_authored_paths["$sf"]:-}" ]]; then
      unattributed_neutral+=("$sf")
    fi
  done
  if [[ ${#unattributed_neutral[@]} -gt 0 ]]; then
    echo "[$SCRIPT_NAME] AUDIT (g-115-1426 over-inclusion): ${#unattributed_neutral[@]} neutral-path file(s) staged under goal=$GOAL_ID WITHOUT committer own-log attribution (committer=$MIND_AGENT). A partner's unrecorded shared-path edit is indistinguishable from the committer's own unlogged edit, so these cannot be auto-dropped without over-excluding legitimate own work (prevention at staging time is impossible) — review for mis-attribution:" >&2
    for f in "${unattributed_neutral[@]}"; do echo "  unattributed (over-inclusion-risk): $f" >&2; done
    echo "[$SCRIPT_NAME] HINT: reliable attribution requires the edit to be recorded in agents/<agent>/session/uncommitted-edits.jsonl. A flagged OWN file => recording gap; a flagged PARTNER file => mis-attribution. Deny-vs-allow-list design: g-115-1182." >&2
  fi
fi

# --- Backend-conditional temp durability () ------------------------
# .gitignore carries a blanket `agents/*/temp/*`. Its stated rationale is that
# temp durability comes from the own-cloud S3 sweep, "NOT by git". That holds on
# own-cloud and is FALSE on STORAGE_BACKEND=local: no sweep runs, git ignores
# temp/, so working docs staged there have ZERO durability mechanism. Measured
# 2026-07-28 on a local-backend deployment: 11 undrained .md working docs would
# have been lost on clone.
#
# WHY THE FIX LIVES HERE AND NOT IN .gitignore: gitignore has no conditionals,
# so "track only when STORAGE_BACKEND=local" is inexpressible in it.
# .git/info/exclude is machine-local and does NOT travel to fresh boxes — that
# was precisely the  bug the blanket ignore rule was created to fix.
# The condition must live in code, and this script is the staging chokepoint.
#
# WHY CONDITIONAL AND NOT UNCONDITIONAL: measured 266 root .md/.json under
# agents/*/temp/ in this repo. This deployment is own-cloud, where S3 already
# provides the durability, so unconditional tracking would add 266 files of pure
# churn to the deployment that does not need it.
#
# SCOPE IS DELIBERATELY NARROW — three separate restrictions, each load-bearing:
#   1. OWN AGENT ONLY. Never force-add a partner's temp/. Over-inclusion of a
#      partner path is the guard-834 failure mode and is un-preventable at
#      staging time once it happens (), so it is excluded up front.
#   2. ROOT LEVEL ONLY (-maxdepth 1). Subdirs are goal-scratch and temp/drained/
#      is an archive of already-encoded material; neither is an undrained
#      working doc. This is the population the incident was actually about.
#   3. *.md ONLY. The own-cloud durable contract (owncloud_sync.py:1355-1358)
#      covers root *.md AND *.json, but that contract is itself over-broad: in
#      real usage every root .json is a scratch dump (an 847 KB table scan, a
#      1.5 MB bank dump, 0-byte probes — ~3.5 MB synced as if durable). Rather
#      than inherit a known-too-wide rule, this takes the defensible half.
#      Whether .json belongs in the own-cloud contract at all is tracked
#      separately as that goal's SECOND FINDING.
#
# Backend resolution copies the established idiom from check-prerequisites.sh
# (live env first, else one grepped line from .env.local — never sourcing the
# file, so no credentials enter this script's environment).
declare -a temp_force_files=()
_resolve_storage_backend() {
  local v="${STORAGE_BACKEND:-}"
  if [[ -z "$v" && -f "$REPO/.env.local" ]]; then
    v="$(grep -E '^[[:space:]]*STORAGE_BACKEND[[:space:]]*=' "$REPO/.env.local" 2>/dev/null \
        | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
  fi
  printf '%s' "$v" | tr '[:upper:]' '[:lower:]'
}
if [[ -n "${MIND_AGENT:-}" && "$(_resolve_storage_backend)" == "local" ]]; then
  _temp_dir="$REPO/agents/$MIND_AGENT/temp"
  if [[ -d "$_temp_dir" ]]; then
    while IFS= read -r _tf; do
      [[ -n "$_tf" ]] && temp_force_files+=("agents/$MIND_AGENT/temp/$(basename "$_tf")")
    done < <(find "$_temp_dir" -maxdepth 1 -type f -name '*.md' 2>/dev/null | sort)
  fi
fi

# --- Dry-run output ----------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
  echo "[$SCRIPT_NAME] DRY-RUN — would commit in $REPO:"
  echo "---"
  echo "$commit_msg"
  echo "---"
  echo "files to stage (git add):"
  for f in "${staged_files[@]}"; do echo "  $f"; done
  if [[ ${#temp_force_files[@]} -gt 0 ]]; then
    echo "files to stage (git add -f — local-backend temp durability, g-115-3759):"
    for f in "${temp_force_files[@]}"; do echo "  $f"; done
  fi
  if [[ ${#rm_only_files[@]} -gt 0 ]]; then
    echo "files to stage (git rm --cached):"
    for f in "${rm_only_files[@]}"; do echo "  $f"; done
  fi
  if [[ ${#staged_del_files[@]} -gt 0 ]]; then
    echo "files already staged for deletion (git rm --cached, idempotent):"
    for f in "${staged_del_files[@]}"; do echo "  $f"; done
  fi
  exit 0
fi

# --- Stale git-lock auto-recovery (, guard-883) --------------------
# A git process that crashes mid add/commit on the shared 6-agent tree leaves
# .git/index.lock (+ a sibling .git/<op>-<PID>.lock), which blocks EVERY agent's
# next commit until cleared. Incident 2026-06-27: PID 51452 crashed mid git-add,
# silently blocking bravo 's deep-close commit and echo's commit until
# the lock was cleared by hand. This helper clears a VERIFIABLY-STALE lock and
# lets the caller retry ONCE; it returns 0 only when ALL guard-883 conditions
# hold (any doubt -> 1, caller surfaces the original failure). It MUST NOT clear
# a LIVE lock (a partner's in-progress commit) -- that corrupts the index
# (guard-853/guard-883). Every branch errs toward NOT clearing.
GIT_LOCK_STALE_S="${ITERATION_COMMIT_GIT_LOCK_STALE_S:-30}"
clear_stale_git_lock_if_dead() {
  local gitdir="$REPO/.git"
  local idxlock="$gitdir/index.lock"
  [[ -f "$idxlock" ]] || return 1                  # no index.lock -- nothing to clear
  # (c.1) age gate -- a lock younger than the stale threshold may be a live commit
  local mt1 now age
  mt1=$(stat -c %Y "$idxlock" 2>/dev/null || echo 0)
  now=$(date +%s)
  age=$(( now - mt1 ))
  [[ $age -ge $GIT_LOCK_STALE_S ]] || return 1     # too fresh -- assume live
  # (a) parse the holder PID from a sibling .git/<op>-<PID>.lock (e.g. next-index-51452.lock)
  local sib pid=""
  for sib in "$gitdir"/*-[0-9]*.lock; do
    [[ -e "$sib" ]] || continue
    [[ "$sib" =~ -([0-9]+)\.lock$ ]] && pid="${BASH_REMATCH[1]}"
  done
  # (b) if a PID was found it MUST be dead (absent from `ps -W`). grep -w matches
  # the PID as a whole word anywhere in ps output (PID or WINPID column); a
  # coincidental match errs toward "alive" -- the SAFE direction (skip clearing).
  if [[ -n "$pid" ]] && ps -W 2>/dev/null | grep -qw "$pid"; then
    echo "[$SCRIPT_NAME] WARN: .git/index.lock holder PID $pid is ALIVE -- NOT clearing (guard-853/883)" >&2
    return 1
  fi
  # (c.2) mtime FROZEN across two reads -- a live holder advances the lock mtime.
  sleep 2
  local mt2
  mt2=$(stat -c %Y "$idxlock" 2>/dev/null || echo 0)
  if [[ "$mt1" != "$mt2" ]]; then
    echo "[$SCRIPT_NAME] WARN: .git/index.lock mtime advancing ($mt1->$mt2) -- live holder, NOT clearing (guard-883)" >&2
    return 1
  fi
  # All guard-883 conditions hold: verifiably stale. Remove index.lock + the
  # sibling PID lock(s). This rm is the SANCTIONED clear (guard-853 forbids the
  # BLIND rm; this path has proven the lock dead + frozen first).
  rm -f "$idxlock" 2>/dev/null || true
  for sib in "$gitdir"/*-[0-9]*.lock; do rm -f "$sib" 2>/dev/null || true; done
  echo "[$SCRIPT_NAME] WARN: cleared VERIFIABLY-STALE git lock (pid=${pid:-none} dead, mtime frozen, age ${age}s>=${GIT_LOCK_STALE_S}s) -- guard-883, retrying once" >&2
  return 0
}

# --- Stage + commit ----------------------------------------------------------
if [[ ${#staged_files[@]} -gt 0 ]]; then
  add_output=$(git -C "$REPO" add -A -- "${staged_files[@]}" 2>&1) || {
    # Stale-lock auto-recovery (): if the add failed on an index.lock
    # collision and the lock is verifiably stale, clear it and retry ONCE.
    if printf '%s' "$add_output" | grep -qi -e "index.lock" -e "Another git process" \
       && clear_stale_git_lock_if_dead; then
      git -C "$REPO" add -A -- "${staged_files[@]}" 2>&1 || {
        echo "[$SCRIPT_NAME] ERROR: git add failed in $REPO (after stale-lock clear+retry)" >&2
        exit 2
      }
      echo "[$SCRIPT_NAME] INFO: git add succeeded after stale-lock clear+retry (g-115-1673)" >&2
    else
      echo "[$SCRIPT_NAME] ERROR: git add failed in $REPO" >&2
      [[ -n "$add_output" ]] && echo "[$SCRIPT_NAME] last error: $add_output" >&2
      exit 2
    fi
  }
fi

# Local-backend temp durability (). `git add -A` above respects
# .gitignore, so these paths are invisible to it — an ignored file never appears
# in `git status --porcelain` and so never reaches staged_files. -f is the only
# way in, and it is why this is a SEPARATE add rather than a filter change.
#
# WARN-not-fail is deliberate: this is a durability ENHANCEMENT for one backend,
# never a precondition for the iteration commit itself. A failure here must not
# cost the agent its actual work commit, so it can never exit non-zero.
if [[ ${#temp_force_files[@]} -gt 0 ]]; then
  if git -C "$REPO" add -f -- "${temp_force_files[@]}" 2>/dev/null; then
    staged_files+=("${temp_force_files[@]}")
    echo "[$SCRIPT_NAME] INFO: force-added ${#temp_force_files[@]} local-backend temp doc(s) for durability (g-115-3759)" >&2
  else
    echo "[$SCRIPT_NAME] WARN: git add -f failed for temp durability paths (continuing — the work commit is unaffected)" >&2
  fi
fi

if [[ ${#rm_only_files[@]} -gt 0 ]]; then
  # --ignore-unmatch tolerates paths already absent from the index (idempotent).
  git -C "$REPO" rm --cached --ignore-unmatch -- "${rm_only_files[@]}" 2>&1 || {
    echo "[$SCRIPT_NAME] WARN: git rm --cached failed for orphan deletions (continuing)" >&2
  }
  # Append for JSON output so callers see what was staged.
  staged_files+=("${rm_only_files[@]}")
fi

if [[ ${#staged_del_files[@]} -gt 0 ]]; then
  # : already-staged deletions ("D "). The deletion is ALREADY in the
  # shared index, so this git rm --cached --ignore-unmatch is a defensive rc=0
  # no-op (the path is already absent from the index) — its real purpose was to
  # keep these paths OUT of the `git add -A` pathspec above (done at routing
  # time). --ignore-unmatch guarantees idempotence even on the already-removed
  # entry. Runs AFTER the add (line above) so the deletion paths never re-enter
  # the failing add batch.
  git -C "$REPO" rm --cached --ignore-unmatch -- "${staged_del_files[@]}" 2>&1 || {
    echo "[$SCRIPT_NAME] WARN: git rm --cached failed for staged deletions (continuing — deletion already in index)" >&2
  }
  # Append for JSON output so callers see the deletion was committed.
  staged_files+=("${staged_del_files[@]}")
fi

# Commit via stdin to avoid arg-length issues with long messages.
# Multi-agent retry loop (): git natively serializes via .git/index.lock,
# but Windows-specific transient issues (antivirus scan holding files, sharing
# violations) can produce spurious commit failures even when conceptually correct.
# Retry up to 3 times with 1s backoff. Scope intentionally limited to commit (not
# add): the add operation is fast and less likely to collide. The mkdir-lock
# above () now closes the cross-agent authorship-bleed race; this retry
# handles transient infrastructure faults orthogonal to coordination semantics.
#
# PATHSPEC-SCOPED COMMIT (, guard-741, rb-1907): commit ONLY the
# attribution-filtered staged_files[] (which includes rm_only_files appended
# above), NOT the whole index. The cross-agent filters above gate what THIS
# script `git add`s, but a bare `git commit` commits the ENTIRE index -- so a
# concurrent partner's PRE-STAGED WIP (staged outside this script, e.g. by a
# partner mid-commit or a non-iteration-commit `git add`) was swept into this
# agent's commit regardless of the filters (observed: , ;
# alpha's deliverable swept into zeta's commit despite the partner-log filter
# correctly excluding it from staged_files[]). The `-- "${staged_files[@]}"`
# pathspec restricts the commit to exactly the intended paths (verified: git
# commit --only correctly commits adds+modifies+deletions and LEAVES foreign
# pre-staged entries staged+uncommitted for the partner's own commit). This is
# COMPLEMENTARY to the  detection audit, which covers the orthogonal
# orphan-IN-staged_files case (indistinguishable from own work, so detect-only).
# staged_files[] is guaranteed non-empty here (both-empty cases exit 0 at the
# filter-skip guards ~L882/L992 before reaching this commit).
MAX_RETRIES=3
RETRY_BACKOFF_S=1
commit_attempt=0
commit_success=0
commit_last_output=""
while [[ $commit_attempt -lt $MAX_RETRIES ]]; do
  commit_attempt=$((commit_attempt + 1))
  commit_last_output=$(echo "$commit_msg" | git -C "$REPO" commit -F - -- "${staged_files[@]}" 2>&1) && {
    commit_success=1
    if [[ $commit_attempt -gt 1 ]]; then
      echo "[$SCRIPT_NAME] INFO: commit succeeded on retry $commit_attempt/$MAX_RETRIES" >&2
    fi
    break
  }
  if [[ $commit_attempt -lt $MAX_RETRIES ]]; then
    # Stale-lock auto-recovery (): an index.lock-collision failure may
    # be a crashed holder's stale lock -- clear it (only if verifiably dead) so
    # the next retry can proceed. Never clears a live lock (guard-883).
    if printf '%s' "$commit_last_output" | grep -qi -e "index.lock" -e "Another git process"; then
      clear_stale_git_lock_if_dead || true
    fi
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
    OWN_LOG="$own_log" COMMITTED="$committed_set" XAGENT_SCRIPTS="$REPO/core/scripts" XAGENT_ROOT="$REPO" py -3 - 2>/dev/null <<'PYEOF' || true
import json, os, tempfile, sys
# : normalize the own-log `file` to PROJECT_ROOT-relative POSIX before
# the committed-set membership test — the SAME _normalize_rel_path the two
# construction consumers use ( partner snapshot,  own-log
# snapshot), so this THIRD uncommitted-edits.jsonl consumer cannot drift on
# path format (rb-1405 SSOT). WITHOUT this, a legacy-absolute or backslash-form
# own-log entry never string-equals the relative committed path, so it is NEVER
# pruned after the committer's own commit — it persists as a stale record that
# then false-drops a partner's later legitimate edit at that path (the 
# stale-record precondition / rb-2186). Normalizing can only ADD correct prunes
# (canonical form never collides distinct files); it never removes a
# legitimately-kept entry. Fail-open: identity normalizer if the import fails.
sys.path.insert(0, os.environ.get("XAGENT_SCRIPTS", ""))
try:
    from _cross_agent_attribution_filter import _normalize_rel_path
except Exception:
    def _normalize_rel_path(p, root):  # fail-open: identity if import fails
        return p
proot = os.environ.get("XAGENT_ROOT", "")
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
            fp = _normalize_rel_path(entry.get("file", ""), proot)
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

stash_filtered_json=""
for f in "${stash_filtered_files[@]}"; do
  if [[ -n "$stash_filtered_json" ]]; then stash_filtered_json="$stash_filtered_json,"; fi
  esc="${f//\\/\\\\}"
  esc="${esc//\"/\\\"}"
  stash_filtered_json="$stash_filtered_json\"$esc\""
done

printf '{"commit_sha":"%s","files_committed":[%s],"stash_filtered_files":[%s],"repo":"%s","goal_id":"%s","outcome":"%s","type":"%s"}\n' \
  "$commit_sha" "$files_json" "$stash_filtered_json" "$REPO" "$GOAL_ID" "$OUTCOME" "$TYPE"
