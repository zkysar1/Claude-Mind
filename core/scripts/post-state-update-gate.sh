#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# post-state-update-gate.sh
# Bash-enforced threshold check for guard-343 (post-state-update fresh-eyes
# trigger). Decides whether a fresh-eyes code review should fire based on
# the completed goal's outcome_class and the size of its material changes.
#
# Usage:
#   post-state-update-gate.sh <outcome_class>
#
# Output: single-line JSON on stdout. Always exits 0 (fail-open).
#   {"fired":true,"files":[...],"core_count":N,"loc_changed":N,"reason":"..."}
#   {"fired":false,"core_count":N,"loc_changed":N,"reason":"..."}
#
# The caller (aspirations-state-update Step 8.78) reads the JSON. If
# .fired == true, it invokes /fresh-eyes-code with the files list as
# args. The threshold decision is script-enforced (bash); the Skill
# dispatch stays with the LLM because Skill calls are LLM-only. This
# separation was the whole point of the extraction — see guard-343
# history.
#
# Thresholds (hardcoded to match guard-343's published spec):
#   - CORE_FILE_THRESHOLD = 3  (core/ files changed)
#   - LOC_THRESHOLD       = 100 (LOC delta in core/scripts)
#   - NEW_SCRIPT          = any untracked file matching core/scripts/*.{sh,py}
# Change the defaults here; thresholds belong in ONE place (single source
# of truth per .claude/rules/communication-clarity.md rule 5).

set -uo pipefail  # intentionally no -e: we fail-open on any sub-failure

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/_paths.sh"
. "$SCRIPT_DIR/_platform.sh"

OUTCOME_CLASS="${1:-}"
CORE_FILE_THRESHOLD=3
LOC_THRESHOLD=100

emit_json() {
  # Use python for clean JSON encoding — avoids bash quoting hell around
  # file paths that may contain spaces or special chars.
  CORE_COUNT="${CORE_COUNT:-0}" \
  LOC_CHANGED="${LOC_CHANGED:-0}" \
  NEW_SCRIPT="${NEW_SCRIPT:-}" \
  REASON="${REASON:-}" \
  FIRED="${FIRED:-false}" \
  CORE_FILES="${CORE_FILES:-}" \
  COMMITS_SCANNED="${COMMITS_SCANNED:-0}" \
  SCOPE_DEGRADED="${SCOPE_DEGRADED:-}" \
  python3 - <<'PYEOF'
import json, os
fired = os.environ.get("FIRED") == "true"
cc = int(os.environ.get("CORE_COUNT", "0") or "0")
loc = int(os.environ.get("LOC_CHANGED", "0") or "0")
ns = (os.environ.get("NEW_SCRIPT") or "").strip()
reason = os.environ.get("REASON") or ""
files_raw = os.environ.get("CORE_FILES") or ""
files = [l.strip() for l in files_raw.splitlines() if l.strip()][:20]
out = {"fired": fired, "core_count": cc, "loc_changed": loc, "reason": reason}
# Multi-commit committed scope (): how many goal commits were unioned.
# 0 = working-tree scope. Additive field — consumers key on fired/files/reason.
scanned = int(os.environ.get("COMMITS_SCANNED", "0") or "0")
if scanned:
    out["commits_scanned"] = scanned
# : the caller ASKED for committed scope and got none, so the run
# silently degraded to working-tree scope. Additive + only-when-true, so the
# absence of the field keeps meaning "nothing to report" for every consumer.
if os.environ.get("SCOPE_DEGRADED"):
    out["scope_degraded"] = os.environ["SCOPE_DEGRADED"]
if fired:
    out["files"] = files
    if ns:
        out["new_script"] = ns
print(json.dumps(out))
PYEOF
  exit 0
}

if [ "$OUTCOME_CLASS" != "deep" ]; then
  FIRED=false REASON="outcome_class=${OUTCOME_CLASS:-<unset>} (gate fires only on deep)" emit_json
fi

# ── Gather changed files ─────────────────────────────────────────────────────
# Two scope modes:
#   (1) COMMITTED SCOPE () — when iteration-close.sh extracts the
#       commit_sha from iteration-commit.sh's JSON output and exports COMMIT_SHA,
#       scope detection to exactly the files THAT COMMIT landed
#       (git diff --name-only ${SHA}~1..${SHA}) and SKIP untracked detection.
#       iteration-commit already ran its 3-source cross-agent attribution filter
#       BEFORE committing, so the committed set is this-agent-only; scoping here
#       inherits that decision instead of re-deriving it from a working tree that
#       may carry partner WIP at neutral paths (core/scripts/, core/config/,
#       mind_api/src/). Closes the stranded-partner false-positive class
#       ( investigation, Option B). The gate's OWN attribution filter
#       (below) is ALSO skipped in this mode — it exists to scrub working-tree
#       noise that committed scope eliminates by construction, and re-running it
#       on fresh-mtime committed files would reintroduce the Source-1 over-drop
#       the same investigation flagged.
#   (2) WORKING-TREE SCOPE (fallback) — COMMIT_SHA unset/empty/invalid (routine
#       closes, iteration-commit no-ops, non-deep callers, or any caller that
#       bypassed commit). Preserves the prior HEAD / HEAD~1..HEAD behavior
#       verbatim.
# COMMIT_SHA_VALID is read again by the attribution-filter and new-script blocks
# below so the guard-343 "new companion script" trigger survives committed scope
# (a script added in the commit shows as status=A in the range diff, not as
# untracked).
#
# DELTA vs CUMULATIVE (): mode (1) IS per-goal-delta scoped — the
# committed range is exactly this goal's landed work — and it is the PRIMARY
# path (iteration-close.sh runs iteration-commit BEFORE this gate on every deep
# close, so a deep goal that committed core edits takes committed scope). Mode
# (2), the fallback, is CUMULATIVE against HEAD: on a box carrying standing
# working-tree divergence (e.g. an in-flight fleet-sync backlog) it can count
# pre-existing residue toward this goal's threshold. That residual over-fire is
# BOUNDED, not eliminated, by three layers — committed scope (mode 1) is the
# norm; the cross-agent attribution filter (below) drops known-partner residue;
# and the mode-only exclusion (below) drops zero-content residue, the largest
# recurring subclass (exec-bit normalization of Windows-authored *.sh on Linux,
# e.g. 's 434-file commit). A full iteration-start dirty-set baseline
# (snapshot at loop entry, subtract at gate time) was evaluated and DEFERRED:
# the only clean iteration-start capture point is a critical-path script
# (heartbeat-tick.sh, IRREDUCIBLY LOCAL) or fragile SKILL.md pseudocode, and any
# baseline-subtraction on a REVIEW gate risks FALSE-NEGATIVES — a missed
# fresh-eyes review ships unreviewed code, strictly worse than a transient
# false-positive. The residue condition is a transient sync-backlog artifact,
# not steady state (a healthy box is clean vs HEAD), so the cumulative fallback
# is intentionally retained; revisit only if telemetry shows the fallback
# firing on residue frequently in practice.
COMMIT_SHA="${COMMIT_SHA:-}"
GOAL_ID="${GOAL_ID:-}"
COMMIT_SHA_VALID=no
COMMITTED_NEW_SCRIPTS=""

# ── Multi-commit committed scope () ────────────────────────────────
# A deep goal may land MORE than one commit: the sanctioned mid-Phase-4
# iteration-commit (e.g. committing daemon code early so the post-commit hook
# restarts the daemon for live verification) plus the close-time commit. The
# close-time COMMIT_SHA alone missed the mid-goal commit entirely (canonical:
#  — new core script + 3 core files in e7cb064e evaded the gate
# because close commit 191a772b carried only docs). Fix: when the caller passes
# GOAL_ID, union in every commit whose message carries "(GOAL_ID)" —
# iteration-commit.sh stamps the goal id into every subject it composes
# (`type(goal-id): title`), so git history IS the per-goal commit ledger; no
# new state file needed. Bounds: 48h window + 50 commits (goals do not span
# longer; a reopened goal id re-matching already-reviewed commits over-fires,
# which the content_signatures cooldown then suppresses — a review gate biases
# to over-fire, never silently drop). Plain `git commit` calls without the
# goal-id stamp remain invisible — the sanctioned mid-goal path is
# iteration-commit.sh (reconcile-fleet-fork Phase 1.2).
SHA_LIST="$COMMIT_SHA"
if [ -n "$GOAL_ID" ]; then
  # Colon anchor (fresh-eyes msg-3119): match the stamp format
  # `type(goal-id): title` exactly — a prose CITATION of a goal id in a later
  # commit's body ("closes the gap ()") lacks the colon and no
  # longer cross-unions into that goal's scope. Zero recall loss: every
  # iteration-commit subject carries "(<goal-id>):".
  GOAL_SHAS=$(git log --fixed-strings --grep "(${GOAL_ID}):" --format=%H -n 50 --since=48.hours 2>/dev/null || true)
  SHA_LIST=$(printf '%s\n%s\n' "$SHA_LIST" "$GOAL_SHAS" | sed '/^$/d' | sort -u)
fi

# Valid = resolves to a commit AND has a parent (so ${SHA}~1..${SHA} is a real
# range). A root commit (no parent) fails the ~1 probe and is skipped rather
# than crashing the range diff.
VALID_SHAS=""
if [ -n "$SHA_LIST" ]; then
  while IFS= read -r _s; do
    [ -z "$_s" ] && continue
    if git rev-parse --verify --quiet "${_s}^{commit}" >/dev/null 2>&1 \
       && git rev-parse --verify --quiet "${_s}~1" >/dev/null 2>&1; then
      VALID_SHAS=$(printf '%s\n%s\n' "$VALID_SHAS" "$_s" | sed '/^$/d')
    fi
  done <<< "$SHA_LIST"
fi
COMMITS_SCANNED=0
SCOPE_DEGRADED=""
if [ -n "$VALID_SHAS" ]; then
  COMMIT_SHA_VALID=yes
  COMMITS_SCANNED=$(printf '%s\n' "$VALID_SHAS" | grep -c . || echo 0)
elif [ -n "$COMMIT_SHA" ] || [ -n "$GOAL_ID" ]; then
  # ── Goal-scope degradation () ────────────────────────────────────
  # The caller ASKED for committed scope — iteration-close.sh always passes
  # GOAL_ID — but no commit resolved, so the run falls through to the
  # working-tree branch below. That fallback is CUMULATIVE: it diffs whatever
  # is dirty vs HEAD, which in a live loop is a mix of this goal's edits and
  # every prior goal's uncommitted residue. It still fires the gate (biasing to
  # over-review is correct), but its verdict is no longer attributable to THIS
  # goal — and until now that swap happened in total silence, so a verdict read
  # as goal-scoped when it was not.
  #
  # This is the real defect behind . The originally-suspected one —
  # that agent bookkeeping churn makes the fallback unreachable — is real in the
  # abstract but sits on a path iteration-close never takes: measured with
  # GOAL_ID set, the gate resolved the goal's commit (commits_scanned=1) and
  # correctly reported core_count=0. The verdict quoted in that goal's own
  # description carried NO commits_scanned field, which is precisely how the
  # silent degradation was caught: it had come from working-tree scope.
  # Loud-and-attributable beats silently-plausible.
  #
  # Benign causes exist (goal closed with nothing committed yet; a commit older
  # than the 48h window; a shallow clone) — hence a warning, never a failure.
  # stderr goes to core/logs/iteration-close-stderr.log; the JSON field is what
  # a later audit can actually count.
  if [ -n "$GOAL_ID" ]; then
    SCOPE_DEGRADED="no commit matched \"(${GOAL_ID}):\" within 48h/50 commits${COMMIT_SHA:+ (COMMIT_SHA=${COMMIT_SHA} also unresolvable)}"
  else
    SCOPE_DEGRADED="COMMIT_SHA=${COMMIT_SHA} did not resolve to a commit with a parent"
  fi
  echo "[post-state-update-gate] WARNING: committed scope requested but unavailable — ${SCOPE_DEGRADED}." >&2
  echo "[post-state-update-gate] Falling back to working-tree scope: the verdict below covers ALL uncommitted changes vs HEAD, not just ${GOAL_ID:-this goal}." >&2
fi

if [ "$COMMIT_SHA_VALID" = "yes" ]; then
  # Union CHANGED + new-scripts across every valid range; RANGES feeds the
  # numstat consumers below (mode-only exclusion + LOC), replacing the former
  # single BASE_FOR_LOC string.
  CHANGED=""
  RANGES=""
  UNTRACKED=""  # committed scope — untracked detection skipped ()
  while IFS= read -r _s; do
    [ -z "$_s" ] && continue
    RANGES=$(printf '%s\n%s\n' "$RANGES" "${_s}~1..${_s}" | sed '/^$/d')
    CHANGED=$(printf '%s\n%s\n' "$CHANGED" "$(git diff --name-only "${_s}~1" "${_s}" 2>/dev/null || true)" | sed '/^$/d' | sort -u)
    # Preserve guard-343's new-script trigger under committed scope: a newly
    # added core/scripts/*.{sh,py} appears as status=A in the range diff (once
    # committed it is tracked, so `git ls-files --others` no longer sees it).
    COMMITTED_NEW_SCRIPTS=$(printf '%s\n%s\n' "$COMMITTED_NEW_SCRIPTS" "$(git diff --name-status "${_s}~1" "${_s}" 2>/dev/null | awk '$1 ~ /^A/ {print $2}' || true)" | sed '/^$/d' | sort -u)
  done <<< "$VALID_SHAS"
  if [ -z "$CHANGED" ]; then
    FIRED=false REASON="no changed files in ${COMMITS_SCANNED} scanned commit(s) (committed scope, g-115-1178/g-115-2030)" emit_json
  fi
else
  # git diff --name-only HEAD captures working-tree + staged changes vs HEAD.
  # If state-update runs BEFORE commit (typical: aspirations-execute's domain
  # post-execution step runs commit/push at end-of-goal, but state-update fires
  # between them), this captures the goal's edits exactly. If state-update runs
  # AFTER commit, HEAD diff returns empty and we fall back to HEAD~1..HEAD.
  CHANGED_VS_HEAD=$(git diff --name-only HEAD 2>/dev/null | sed '/^$/d' || true)

  # Untracked files — new scripts don't appear in `git diff --name-only HEAD`
  # until they're at least staged. guard-343 explicitly includes "introduced
  # a new companion script" as a trigger, so we must scan untracked too.
  UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null | sed '/^$/d' || true)

  CHANGED=$(printf '%s\n%s\n' "$CHANGED_VS_HEAD" "$UNTRACKED" | sed '/^$/d' | sort -u)

  # LOAD-BEARING — DO NOT COLLAPSE THESE TWO PATHS INTO ONE.
  # HEAD vs HEAD~1..HEAD serve DIFFERENT invocation timings:
  #   - BASE_FOR_LOC="HEAD" path: state-update runs BEFORE goal's commit — working
  #     tree + staged diff against HEAD captures the goal's uncommitted edits.
  #   - BASE_FOR_LOC="HEAD~1..HEAD" path: state-update runs AFTER goal's commit
  #     (post-execution.md Step 2 commits at end of goal) — HEAD diff returns
  #     empty, fall back to previous-commit-to-HEAD range.
  # Removing either path silently breaks the opposite scenario. The CHANGED-empty
  # check is the discriminator; do not replace with a timestamp heuristic.
  #
  # NOTE (, 2026-07-26): the loop's own bookkeeping under agents/<agent>/
  # (journal.jsonl, changelog.jsonl, health/<date>.jsonl, skill-invocations.jsonl) is
  # rewritten by iteration-close on EVERY goal, so in a live iteration CHANGED is
  # essentially never empty and this fallback is unreachable. That is tolerable ONLY
  # because this whole `else` branch is the degraded path: iteration-close always
  # passes GOAL_ID, which takes the goal-attributed committed scope above instead.
  # Do NOT "fix" the discriminator by filtering agents/ out of CHANGED — that was
  # tried and reverted. Without a GOAL_ID there is nothing tying HEAD~1..HEAD to the
  # current goal, so making the fallback reachable would attribute the PREVIOUS
  # goal's committed core/ files to this one on every agent-files-only iteration.
  # Unreachable-and-correct beats reachable-and-misattributing. The real defect was
  # that committed scope could degrade to here SILENTLY; that is now loud (see the
  # goal-scope degradation warning above).
  BASE_FOR_LOC="HEAD"
  if [ -z "$CHANGED" ]; then
    CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null | sed '/^$/d' | sort -u || true)
    BASE_FOR_LOC="HEAD~1..HEAD"
  fi
  # Working-tree scope is single-range; RANGES unifies the numstat consumers
  # below with the multi-range committed scope ().
  RANGES="$BASE_FOR_LOC"

  if [ -z "$CHANGED" ]; then
    FIRED=false REASON="no changed files detected (clean working tree + empty HEAD~1 diff)" emit_json
  fi
fi

# ── Filter to core/ and compute thresholds ───────────────────────────────────
# Exclude core/logs/ — runtime audit JSONL output, not LLM-edited code ().
# Append-only logs accumulating during normal session activity should not trigger
# fresh-eyes-code review. Without the exclusion, the gate fired false-positive
# whenever stale-scanner or iteration-close appended to logs while goal work
# touched only one source file under core/.
CORE_FILES=$(printf '%s\n' "$CHANGED" | grep '^core/' | grep -v '^core/logs/' || true)

# ── Cross-agent attribution filter (, rb-911-sibling) ──────────────
# Mirror iteration-commit.sh's 3-source filter stack ( +  +
# ). Without this, partner WIP at neutral paths (core/scripts/,
# core/config/, mind_api/src/) gets counted toward THIS agent's threshold —
# canonical incident: bravo session 69 (2026-05-13T18:16) fired with 4 zeta
# files. Filter delegates to core/scripts/_cross_agent_attribution_filter.py
# which reads world/team-state.yaml + per-agent uncommitted-edits.jsonl and
# drops paths attributable to non-self agents. Fail-open at every layer —
# any error retains the original list (biases over-firing). Also filter
# UNTRACKED so partner-untracked scripts don't trigger NEW_SCRIPT.
ATTRIB_HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_cross_agent_attribution_filter.py"
# Skipped under committed scope (COMMIT_SHA_VALID=yes): the committed set is
# already this-agent-only (iteration-commit filtered it pre-commit), and
# re-filtering fresh-mtime committed files would reintroduce the Source-1
# over-drop the  investigation flagged ().
if [ "$COMMIT_SHA_VALID" != "yes" ] && [ -n "${MIND_AGENT:-}" ] && [ -f "$ATTRIB_HELPER" ]; then
  if [ -n "$CORE_FILES" ]; then
    ATTRIB_TMP=$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/xagent-core-$$.txt")
    if printf '%s\n' "$CORE_FILES" | py -3 "$ATTRIB_HELPER" > "$ATTRIB_TMP" 2>/dev/null; then
      CORE_FILES=$(sed '/^$/d' "$ATTRIB_TMP" || true)
    fi
    rm -f "$ATTRIB_TMP"
  fi
  if [ -n "$UNTRACKED" ]; then
    ATTRIB_TMP2=$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/xagent-untracked-$$.txt")
    if printf '%s\n' "$UNTRACKED" | py -3 "$ATTRIB_HELPER" > "$ATTRIB_TMP2" 2>/dev/null; then
      UNTRACKED=$(sed '/^$/d' "$ATTRIB_TMP2" || true)
    fi
    rm -f "$ATTRIB_TMP2"
  fi
fi

# ── Mode-only exclusion () ─────────────────────────────────────────
# A file whose ONLY change is a mode bit (exec-bit normalization, symlink/type
# flip) carries ZERO reviewable content: `git diff --numstat` reports it as
# "0<tab>0<tab><path>". Counting it toward core_files — or firing a
# fresh-eyes-code review on it — is a false positive: there is no code delta to
# review. The LOC path below already treats mode-only as 0; this makes the
# FILE-COUNT path consistent, so the gate measures CONTENT deltas, not mode
# flips. Canonical residue class:  normalized 434 Windows-authored
# *.sh exec bits (100644->100755) in one commit — mode-only, zero content —
# which would otherwise blow past core_files>=3 on the next deep close.
#
# SAFETY (zero false-negative): a file is dropped ONLY when git reports 0
# insertions AND 0 deletions (no line changed). Dropping it can never miss a
# real review. --no-renames keeps a renamed file visible as add+delete (nonzero)
# so a rename is never mis-scored as mode-only. UNTRACKED/new files (new content,
# absent from numstat vs base) are ALWAYS kept. FAIL-OPEN: when numstat produces
# no output (git error, or no tracked changes at all → every core file is
# untracked) we keep the full set — a review gate must bias to over-fire, never
# silently drop.
MODE_ONLY_DROPPED=0
if [ -n "$CORE_FILES" ]; then
  PRE_MODE_COUNT=$(printf '%s\n' "$CORE_FILES" | sed '/^$/d' | grep -c . || echo 0)
  # Concatenate numstat across all ranges ( multi-commit scope). The
  # CONTENT_CHANGED filter below admits a file when ANY range shows a nonzero
  # delta — correct union semantics (mode-only in one commit + content in
  # another = content).
  RAW_NUMSTAT=$(while IFS= read -r _r; do
    [ -z "$_r" ] && continue
    # shellcheck disable=SC2086
    git diff --numstat --no-renames $_r 2>/dev/null || true
  done <<< "$RANGES")
  CONTENT_CHANGED=$(printf '%s\n' "$RAW_NUMSTAT" \
    | awk -F'\t' 'NF>=3 && !($1=="0" && $2=="0") {print $3}' | sort -u || true)
  NUMSTAT_NONEMPTY=0
  [ -n "$RAW_NUMSTAT" ] && NUMSTAT_NONEMPTY=1
  CORE_FILES=$(CORE_FILES_VAL="$CORE_FILES" CONTENT_VAL="$CONTENT_CHANGED" \
               UNTRACKED_VAL="$UNTRACKED" NUMSTAT_NONEMPTY="$NUMSTAT_NONEMPTY" \
               python3 - <<'PYEOF'
import os
core = [l.strip() for l in os.environ.get("CORE_FILES_VAL", "").splitlines() if l.strip()]
if os.environ.get("NUMSTAT_NONEMPTY") != "1":
    # No tracked changes / numstat error — cannot verify mode-only. Fail-open:
    # keep the full set (bias to over-fire; never silently drop).
    print("\n".join(core))
else:
    content = set(l.strip() for l in os.environ.get("CONTENT_VAL", "").splitlines() if l.strip())
    untracked = set(l.strip() for l in os.environ.get("UNTRACKED_VAL", "").splitlines() if l.strip())
    # Keep a core file iff it has a nonzero content delta OR is an untracked new
    # file. Drop pure mode-only (0+0) tracked changes.
    print("\n".join(f for f in core if f in content or f in untracked))
PYEOF
)
  CORE_FILES=$(printf '%s\n' "$CORE_FILES" | sed '/^$/d')
  POST_MODE_COUNT=$(printf '%s\n' "$CORE_FILES" | sed '/^$/d' | grep -c . || echo 0)
  MODE_ONLY_DROPPED=$((PRE_MODE_COUNT - POST_MODE_COUNT))
fi

CORE_COUNT=0
if [ -n "$CORE_FILES" ]; then
  CORE_COUNT=$(printf '%s\n' "$CORE_FILES" | sed '/^$/d' | wc -l | tr -d ' ')
fi

# LOC delta in core/scripts. Sum added+deleted from numstat. Fail-open
# on any single-file error — partial counts are fine for a threshold
# decision.
CORE_SCRIPTS=$(printf '%s\n' "$CORE_FILES" | grep '^core/scripts/' || true)
LOC_CHANGED=0
if [ -n "$CORE_SCRIPTS" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    # Sum the file's delta across all ranges (multi-commit scope, ).
    # awk's string→number coercion keeps binary-file "-" columns at 0, matching
    # the prior single-range behavior.
    n=$(while IFS= read -r _r; do
      [ -z "$_r" ] && continue
      # shellcheck disable=SC2086
      git diff --numstat $_r -- "$f" 2>/dev/null || true
    done <<< "$RANGES" | awk 'NF>=2 {s+=$1+$2} END {print s+0}')
    [ -z "$n" ] && n=0
    LOC_CHANGED=$((LOC_CHANGED + n))
  done <<< "$CORE_SCRIPTS"
fi

# New-script detection — any new core/scripts/*.sh or *.py. Take first match
# for the reason string (full list is in CORE_FILES already). Source depends on
# scope: working-tree → UNTRACKED (post-attribution-filter); committed scope →
# COMMITTED_NEW_SCRIPTS (status=A files in the commit range), so guard-343's
# new-script trigger survives committed scope ().
NEW_SCRIPT=""
if [ "$COMMIT_SHA_VALID" = "yes" ]; then
  NEW_SCRIPT_SOURCE="$COMMITTED_NEW_SCRIPTS"
else
  NEW_SCRIPT_SOURCE="$UNTRACKED"
fi
if [ -n "$NEW_SCRIPT_SOURCE" ]; then
  NEW_SCRIPT=$(printf '%s\n' "$NEW_SCRIPT_SOURCE" | grep -E '^core/scripts/.*\.(sh|py)$' | head -1 || true)
fi

# ── Threshold decision ───────────────────────────────────────────────────────
REASONS=""
if [ "$CORE_COUNT" -ge "$CORE_FILE_THRESHOLD" ]; then
  REASONS="${REASONS}core_files=$CORE_COUNT>=$CORE_FILE_THRESHOLD; "
fi
if [ "$LOC_CHANGED" -ge "$LOC_THRESHOLD" ]; then
  REASONS="${REASONS}loc=$LOC_CHANGED>=$LOC_THRESHOLD; "
fi
if [ -n "$NEW_SCRIPT" ]; then
  REASONS="${REASONS}new_script=$NEW_SCRIPT; "
fi

# ── Cooldown check ( +  cross-agent extension) ─────────────
# Previously: each deep state-update re-fired dispatch whenever cumulative
# git-changed core files crossed threshold, even if /fresh-eyes-code had JUST
# reviewed that same set. Result was a dispatch-loop each iteration touching
# any previously-reviewed file. Fix (): track the last-fire file set +
# time in own-agent WM (`fresh_eyes_last_fire`).
#
#  / rb-593 extension: per-agent WM cannot bridge cross-agent — when
# alpha runs /fresh-eyes-code, bravo's WM slot stays untouched and bravo's next
# gate re-dispatches the files alpha just reviewed. Fix: ALSO read
# `world/team-state.yaml agent_status.*.last_fresh_eyes_run` for ALL non-self
# agents, union their fresh-within-cooldown file lists into the suppression
# set. /fresh-eyes-code Phase 5b writes that field (single canonical writer).
#
# Fail-open at every layer — a missing/corrupt WM slot OR team-state read
# failure does NOT block dispatch.
if [ -n "$REASONS" ] && [ -n "$CORE_FILES" ]; then
  # Current set for comparison — sort+dedupe for stable signature.
  CURRENT_SET=$(printf '%s\n' "$CORE_FILES" | sed '/^$/d' | sort -u)
  # Read own-agent cooldown state. wm-read --json returns JSON (vs default YAML).
  # wm-read prints "null" + exit 0 when slot missing — which is valid JSON.
  COOLDOWN_JSON=$(bash "$SCRIPT_DIR/wm-read.sh" fresh_eyes_last_fire --json 2>/dev/null || echo "null")
  # Read cooldown_hours from config (default 4).
  COOLDOWN_HOURS=$(python3 -c "
import yaml, pathlib
try:
    p = pathlib.Path('core/config/aspirations.yaml')
    d = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    print(float((d.get('post_state_update_gate') or {}).get('cooldown_hours', 12)))
except Exception:
    print(12)
" 2>/dev/null || echo "12")
  # Path to team-state.yaml (sourced from _paths.sh at top of script).
  TEAM_STATE_PATH="$WORLD_DIR/team-state.yaml"
  # Compare: is current set subset of union(own_last_fire ∪ peer_last_runs)
  # AND at least one source within cooldown? Python block handles JSON +
  # YAML parse + time deltas + set comparison. Fail-open on any error.
  # : peer-vs-self suppression audit log path. Single source of
  # truth so we can prove the cross-agent layer fires in production (not
  # just in regression tests). Fail-open: a missing META_DIR or write error
  # never blocks dispatch.
  SUPPRESSION_AUDIT_PATH="${META_DIR:-}/post-state-update-suppressions.jsonl"
  # : coverage check extracted to _fresh_eyes_coverage_check.py for
  # testability and maintainability. The helper applies sig-aware per-path
  # coverage (sig-match → covered, sig-conflict → not covered, no-sig record
  # → path-only fallback for backward-compat with pre-573 records) and emits
  # the same two-line protocol the bash side has always parsed. The audit
  # log writer () lives there too.
  GATE_OUTPUT=$(CURRENT="$CURRENT_SET" COOLDOWN_JSON="$COOLDOWN_JSON" \
             COOLDOWN_HOURS="$COOLDOWN_HOURS" TEAM_STATE_PATH="$TEAM_STATE_PATH" \
             SELF_AGENT="${MIND_AGENT:-}" \
             PROJECT_ROOT="$PROJECT_ROOT" \
             SUPPRESSION_AUDIT_PATH="$SUPPRESSION_AUDIT_PATH" \
             python3 "$CORE_ROOT/scripts/_fresh_eyes_coverage_check.py")
  # Two-line protocol: line 1 = verdict, line 2 = JSON list of union files.
  SUPPRESS=$(printf '%s\n' "$GATE_OUTPUT" | sed -n '1p')
  UNION_JSON=$(printf '%s\n' "$GATE_OUTPUT" | sed -n '2p')
  [ -z "$UNION_JSON" ] && UNION_JSON="[]"
  case "$SUPPRESS" in
    "yes:peer")
      FIRED=false REASON="cooldown active (peer review covered current set)" emit_json ;;
    "yes:self")
      FIRED=false REASON="cooldown active (own previous review still fresh)" emit_json ;;
    "yes:union")
      FIRED=false REASON="cooldown active (union of own+peer reviews covered current set)" emit_json ;;
    "yes")
      # backward-compat with older Python output (pre-)
      FIRED=false REASON="cooldown active (within cooldown_hours, no new core files since last fire)" emit_json ;;
  esac
  # : partial-overlap dedup. When verdict was "no" (current is NOT a
  # subset of union) but UNION_JSON is non-empty (cooldown is active for
  # at least one source), exclude already-covered files from CORE_FILES and
  # re-evaluate thresholds. If reduced set drops below CORE_FILE_THRESHOLD
  # AND has no new_script, suppress entirely. Otherwise replace CORE_FILES
  # with the reduced list so /fresh-eyes-code dispatches only on truly-new
  # files. LOC threshold check stays against the original set (conservative —
  # a 200-LOC change in 2 files might be worth review even if peer covered
  # those files; the reviewer's mode-flip catches different defects each pass).
  # Fail-open: parser errors leave CORE_FILES unchanged (full-set fire).
  if [ "$UNION_JSON" != "[]" ] && [ "$UNION_JSON" != "" ]; then
    REDUCED_OUT=$(CORE_FILES_VAL="$CORE_FILES" UNION_JSON_VAL="$UNION_JSON" python3 - <<'PYEOF'
import json, os
try:
    union = set(json.loads(os.environ["UNION_JSON_VAL"]))
    files = [l.strip() for l in os.environ["CORE_FILES_VAL"].splitlines() if l.strip()]
    reduced = [f for f in files if f not in union]
    print(len(reduced))
    print("\n".join(reduced))
except Exception:
    pass
PYEOF
)
    REDUCED_COUNT=$(printf '%s\n' "$REDUCED_OUT" | sed -n '1p')
    REDUCED_FILES=$(printf '%s\n' "$REDUCED_OUT" | sed '1d' | sed '/^$/d')
    # : recompute LOC against the REDUCED set so dispatch decisions
    # use the LOC of files this agent actually needs to review, not LOC from
    # peer-covered files. Prior behavior (LOC stays against original set) caused
    # dispatch on tree.yaml-only scenarios where 22 of 23 files were peer-
    # covered — the 616 LOC from peer-covered files kept the gate firing even
    # though only 1 uncovered file (tree.yaml, 0 LOC contribution) remained.
    # Each dispatch carries its OWN reviewer mode-flip; the LOC signal should
    # reflect what the dispatch covers, not what the prior peer review covered.
    REDUCED_SCRIPTS=$(printf '%s\n' "$REDUCED_FILES" | grep '^core/scripts/' || true)
    LOC_REDUCED=0
    if [ -n "$REDUCED_SCRIPTS" ]; then
      while IFS= read -r f; do
        [ -z "$f" ] && continue
        # shellcheck disable=SC2086
        n=$(git diff --numstat $BASE_FOR_LOC -- "$f" 2>/dev/null | awk 'NF>=2 {print $1+$2; exit}' || echo 0)
        [ -z "$n" ] && n=0
        LOC_REDUCED=$((LOC_REDUCED + n))
      done <<< "$REDUCED_SCRIPTS"
    fi
    # : NEW_SCRIPT dedup — when the untracked script is already in
    # the coverage union (own or peer prior review covered it), it should not
    # block suppression any more than a covered file in CORE_FILES does.
    # Without this, an untracked core/scripts/*.{sh,py} that has been
    # reviewed once stays a perpetual fire trigger across iterations while
    # every other delta dedups away. Incident: alpha session 64 cleared
    # fresh_eyes_dispatch_pending 3+ times for the same 13-14 file set —
    # CORE_COUNT deduped to 1 each iteration but NEW_SCRIPT
    # (capability-route-gate.py, untracked partner WIP) kept the gate
    # firing even though it was in own fresh_eyes_last_fire records.
    NEW_SCRIPT_COVERED="no"
    if [ -n "$NEW_SCRIPT" ] && [ "$UNION_JSON" != "[]" ] && [ -n "$UNION_JSON" ]; then
      NEW_SCRIPT_COVERED=$(NEW_SCRIPT_VAL="$NEW_SCRIPT" UNION_JSON_VAL="$UNION_JSON" python3 - <<'PYEOF' 2>/dev/null
import json, os
try:
    union = set(json.loads(os.environ["UNION_JSON_VAL"]))
    ns = os.environ.get("NEW_SCRIPT_VAL", "").strip()
    print("yes" if ns in union else "no")
except Exception:
    print("no")
PYEOF
)
    fi
    if [ -n "$REDUCED_COUNT" ] && [ "$REDUCED_COUNT" -lt "$CORE_FILE_THRESHOLD" ] && { [ -z "$NEW_SCRIPT" ] || [ "$NEW_SCRIPT_COVERED" = "yes" ]; }; then
      # Suppress when post-dedup file-count AND post-dedup LOC fall
      # below thresholds AND (no new-script trigger OR new-script already
      # covered by union). The LOC check uses LOC_REDUCED (reduced set)
      # not LOC_CHANGED — see  for the dispatch-on-stale-peer-LOC
      # bug that closed.
      if [ "$LOC_REDUCED" -lt "$LOC_THRESHOLD" ]; then
        FIRED=false REASON="cooldown active (post-dedup core_files=$REDUCED_COUNT < $CORE_FILE_THRESHOLD; loc_reduced=$LOC_REDUCED < $LOC_THRESHOLD; new_script_covered=$NEW_SCRIPT_COVERED; partner covered $((CORE_COUNT - REDUCED_COUNT)) of $CORE_COUNT files; pre-dedup loc=$LOC_CHANGED was peer-covered)" emit_json
      fi
    fi
    # Replace CORE_FILES with reduced set so dispatch list excludes
    # peer-covered files. CORE_COUNT updated for the audit log /
    # reason string. NEW_SCRIPT stays — if it's an untracked file the
    # peer hasn't seen it yet. Append a dedup suffix to REASONS so the
    # fired-with reason string reflects that partial-overlap exclusion
    # happened (the underlying threshold check still ran against the
    # original set, but the dispatched file list is the reduced one).
    # : also update LOC_CHANGED to LOC_REDUCED so the dispatch
    # reason reports the LOC of files this dispatch actually covers, not
    # the cumulative LOC including peer-covered files.
    if [ -n "$REDUCED_FILES" ] && [ "$REDUCED_COUNT" -lt "$CORE_COUNT" ]; then
      DEDUPED_FROM="$CORE_COUNT"
      LOC_PRE_DEDUP="$LOC_CHANGED"
      CORE_FILES="$REDUCED_FILES"
      CORE_COUNT="$REDUCED_COUNT"
      LOC_CHANGED="$LOC_REDUCED"
      REASONS="${REASONS}deduped_from=${DEDUPED_FROM}_to=${CORE_COUNT}; loc_reduced_from=${LOC_PRE_DEDUP}_to=${LOC_CHANGED}; "
    fi
  fi
fi

if [ -n "$REASONS" ]; then
  # Write cooldown state BEFORE emit_json (which exits). Fail-open: a wm-set
  # failure does not block dispatch — worst case the cooldown has no anchor
  # and the next iteration re-fires (same as pre-fix behavior).
  if [ -n "$CORE_FILES" ]; then
    CURRENT_SET=$(printf '%s\n' "$CORE_FILES" | sed '/^$/d' | sort -u)
    # : list-of-records schema. Each dispatch prepends a new record
    # to the existing list, then prunes entries older than 2x cooldown_hours
    # (safety margin). Without this, single-record overwrite caused dispatch
    # fatigue: prior recent reviews lost their coverage signal on every new
    # dispatch, even when distinct file-sets had been reviewed minutes apart.
    # Reuse $COOLDOWN_JSON read at line 170 — same single-agent context, no
    # race window. Falls back to wm-read on the rare path where COOLDOWN_JSON
    # was never set (defensive — current code reaches here only via the
    # cooldown block where it IS set, but the fallback keeps the writer
    # robust against future control-flow changes).
    EXISTING_JSON="${COOLDOWN_JSON:-$(bash "$SCRIPT_DIR/wm-read.sh" fresh_eyes_last_fire --json 2>/dev/null || echo "null")}"
    CURRENT_SET="$CURRENT_SET" EXISTING_JSON="$EXISTING_JSON" \
    COOLDOWN_HOURS="${COOLDOWN_HOURS:-12}" \
    PROJECT_ROOT="$PROJECT_ROOT" \
    python3 - <<'PYEOF' 2>/dev/null | bash "$SCRIPT_DIR/wm-set.sh" fresh_eyes_last_fire >/dev/null 2>&1 || true
import json, os, hashlib
from datetime import datetime, timedelta

def _file_sig(rel_path, root):
    """sha1[:12] of file content (g-115-573 amend-detection). None if file
    missing/unreadable — caller treats absence as no signature available, NOT
    a coverage failure (paths in record.files without a signature entry fall
    through to the path-only backward-compat branch in the reader)."""
    full = os.path.join(root, rel_path) if root else rel_path
    try:
        with open(full, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:12]
    except (OSError, IOError):
        return None

files = [l.strip() for l in os.environ.get("CURRENT_SET", "").splitlines() if l.strip()]
project_root = os.environ.get("PROJECT_ROOT", "")
content_signatures = {}
for _p in files:
    _sig = _file_sig(_p, project_root)
    if _sig is not None:
        content_signatures[_p] = _sig
new_record = {
    "time": datetime.now().isoformat(timespec="seconds"),
    "files": files,
    "content_signatures": content_signatures,  #  amend-detection
}
existing_raw = (os.environ.get("EXISTING_JSON") or "").strip()
records = []
if existing_raw and existing_raw != "null":
    try:
        existing = json.loads(existing_raw)
        if isinstance(existing, list):
            records = [r for r in existing if isinstance(r, dict)]
        elif isinstance(existing, dict):
            records = [existing]
    except (ValueError, TypeError, json.JSONDecodeError):
        records = []
records.insert(0, new_record)
try:
    cooldown_h = float(os.environ.get("COOLDOWN_HOURS", "12"))
except (ValueError, TypeError):
    cooldown_h = 12.0
prune_cutoff = datetime.now() - timedelta(hours=2 * cooldown_h)
pruned = []
for r in records:
    try:
        t = datetime.fromisoformat(r.get("time", ""))
        if t >= prune_cutoff:
            pruned.append(r)
    except (ValueError, TypeError):
        pass  # drop malformed records silently
print(json.dumps(pruned))
PYEOF
  fi
  FIRED=true REASON="${REASONS% }" emit_json
else
  #  observability: name the mode-only exclusion when it reduced the
  # count, so a debugger seeing core_files=0 after an exec-bit-only commit sees
  # WHY (the fix worked) instead of an unexplained non-fire.
  MODE_SUFFIX=""
  [ "${MODE_ONLY_DROPPED:-0}" -gt 0 ] && MODE_SUFFIX=", mode_only_excluded=$MODE_ONLY_DROPPED"
  FIRED=false REASON="below thresholds (core_files=$CORE_COUNT<$CORE_FILE_THRESHOLD, loc=$LOC_CHANGED<$LOC_THRESHOLD, no new script${MODE_SUFFIX})" emit_json
fi
