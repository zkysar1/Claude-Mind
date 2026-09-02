#!/usr/bin/env bash
# branch-stall-probe.sh — report-only observability probe for two git states that
# `git status` reports as perfectly healthy ().
#
# Sibling of aspirations-precheck Phase 0-pre.0c (the stash-carryover probe) and
# built to the same contract: cheap, per-iteration, REPORT-ONLY, quiet on the
# common clean case, fail-open on every error. It never blocks, defers, mutates
# state, or exits non-zero on a detection.
#
# ── WHY IT EXISTS ───────────────────────────────────────────────────────────
# 2026-08-2x, LAPTOP-3IOFCNEO: an interactive rebase left HEAD detached and
# `refs/heads/main` frozen at 3561c81e5. The loop kept running for ~25 HOURS,
# committing 19 commits onto the detached HEAD, where they were unreachable from
# any branch and could not be pushed. Nothing surfaced it: the health-ledger
# composite read 0.9659 through the entire wedge, and `git status` says
# "HEAD detached at ..." in a line nobody reads per-iteration. (Fixed in
# ; this probe is the DETECTOR, not the fix.)
#
# The detached state was not merely unnoticed — it was MEASURED AND DISCARDED.
# iteration-push.sh:291-294 computes the branch with `rev-parse --abbrev-ref
# HEAD`, which returns the literal string "HEAD" when detached, and then:
#
#     log "detached HEAD or no branch — skip"; soft_exit 0
#
# `soft_exit 0` is fail-open SUCCESS. So the one component that already knew
# reported healthy every iteration for 25h. Condition (b) is the same story one
# axis over: iteration-push computes AGE_MIN (the age of the oldest unpushed
# commit) and spends it on the push throttle and on a stranded-depth alarm keyed
# to COUNT >= 25 — so a branch sitting on 3 unpushed commits for a day trips
# nothing at all. This probe surfaces what that path already computes.
#
# ── THE TWO CONDITIONS ──────────────────────────────────────────────────────
#   (a) DETACHED HEAD — `git symbolic-ref -q HEAD` fails. Reports the commits
#       stranded off every branch (`rev-list HEAD --not --branches`) and the age
#       of the oldest, because 19-commits-over-25h is the sentence that conveys
#       urgency; "detached" alone does not.
#   (b) BRANCH STALL — the current branch is ahead of its origin ref and the
#       OLDEST unpushed commit is older than the threshold (default 180 min,
#       BRANCH_STALL_MAX_AGE_MIN). Deliberately keyed to AGE, not depth: depth
#       is already alarmed at >= 25 by iteration-push, and age is the axis that
#       catches a shallow-but-frozen branch. The threshold sits well above
#       iteration-push's 20-minute push throttle, so a healthy loop never trips
#       it.
#
# ── WHAT IT DOES NOT COVER (stated so absence is not read as coverage) ──────
# A branch with NO origin ref at all (first push never done) is a third stall
# shape that iteration-push also soft-exits on. It is out of scope here — the
# goal named two conditions and a third would be scope creep — but it is not
# silently ignored either: (b) reports `unmeasurable` rather than `clean` in
# --json when the upstream ref is missing, so a blind lane can never be read as
# an empty one (guard-4093 / guard-4157: an absent field is not a zero).
#
# ── USAGE ───────────────────────────────────────────────────────────────────
#   bash core/scripts/branch-stall-probe.sh [--repo <path>] [--json]
#                                           [--max-age-min <n>]
# Human mode prints NOTHING when both conditions are clean. Exit is ALWAYS 0.
set -uo pipefail

REPO=""
JSON=0
MAX_AGE_MIN="${BRANCH_STALL_MAX_AGE_MIN:-180}"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)        REPO="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --json)        JSON=1; shift ;;
    --max-age-min) MAX_AGE_MIN="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    *)             shift ;;
  esac
done

if [ -z "$REPO" ]; then
  _SELF="$(cd "$(dirname "$0")" && pwd)"
  REPO="$(cd "$_SELF/.." && pwd)"
fi
case "$MAX_AGE_MIN" in ''|*[!0-9]*) MAX_AGE_MIN=180;; esac

# Fail-open: not a repo / no git / unreadable -> say nothing, exit 0.
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  [ "$JSON" -eq 1 ] && echo '{"probe":"branch-stall","status":"unmeasurable","reason":"not-a-git-repo"}'
  exit 0
fi

NOW_CT="$(date +%s 2>/dev/null || echo 0)"
DETACHED=0
STRANDED=0
STRANDED_AGE_MIN=0
BRANCH=""
B_STATUS="clean"
AHEAD=0
AGE_MIN=0

if git -C "$REPO" symbolic-ref -q HEAD >/dev/null 2>&1; then
  BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
else
  DETACHED=1
  STRANDED="$(git -C "$REPO" rev-list --count HEAD --not --branches 2>/dev/null || echo 0)"
  case "$STRANDED" in ''|*[!0-9]*) STRANDED=0;; esac
  _OCT="$(git -C "$REPO" log HEAD --not --branches --format=%ct 2>/dev/null | tail -n 1 || echo "")"
  case "$_OCT" in
    ''|*[!0-9]*) STRANDED_AGE_MIN=0;;
    *) STRANDED_AGE_MIN=$(( (NOW_CT - _OCT) / 60 )); [ "$STRANDED_AGE_MIN" -lt 0 ] && STRANDED_AGE_MIN=0;;
  esac
fi

# Condition (b) only applies on an attached branch.
if [ "$DETACHED" -eq 0 ] && [ -n "$BRANCH" ]; then
  UPSTREAM="origin/$BRANCH"
  if ! git -C "$REPO" rev-parse --verify "$UPSTREAM" >/dev/null 2>&1; then
    B_STATUS="unmeasurable"        # no origin ref — NOT clean; see scope note above
  else
    AHEAD="$(git -C "$REPO" rev-list --count "$UPSTREAM..$BRANCH" 2>/dev/null || echo 0)"
    case "$AHEAD" in ''|*[!0-9]*) AHEAD=0;; esac
    if [ "$AHEAD" -gt 0 ]; then
      _OCT="$(git -C "$REPO" log "$UPSTREAM..$BRANCH" --format=%ct 2>/dev/null | tail -n 1 || echo "")"
      case "$_OCT" in
        ''|*[!0-9]*) AGE_MIN=0;;
        *) AGE_MIN=$(( (NOW_CT - _OCT) / 60 )); [ "$AGE_MIN" -lt 0 ] && AGE_MIN=0;;
      esac
      [ "$AGE_MIN" -ge "$MAX_AGE_MIN" ] && B_STATUS="stalled"
    fi
  fi
elif [ "$DETACHED" -eq 1 ]; then
  B_STATUS="not-applicable"
fi

if [ "$JSON" -eq 1 ]; then
  printf '{"probe":"branch-stall","detached":%s,"stranded_commits":%s,"stranded_age_min":%s,"branch":"%s","branch_status":"%s","ahead":%s,"oldest_unpushed_age_min":%s,"max_age_min":%s}\n' \
    "$( [ "$DETACHED" -eq 1 ] && echo true || echo false )" \
    "$STRANDED" "$STRANDED_AGE_MIN" "$BRANCH" "$B_STATUS" "$AHEAD" "$AGE_MIN" "$MAX_AGE_MIN"
  exit 0
fi

if [ "$DETACHED" -eq 1 ]; then
  echo "▸ ⚠ DETACHED HEAD: ${STRANDED} commit(s) sit off every branch, oldest $(( STRANDED_AGE_MIN / 60 ))h$(( STRANDED_AGE_MIN % 60 ))m old — invisible to \`git status\`'s clean-tree line and SILENTLY SKIPPED by iteration-push.sh (\"detached HEAD or no branch — skip\", soft_exit 0), so nothing is being pushed and no alarm fires."
  echo "    Recover: \`git log --oneline HEAD --not --branches\` to see them, then re-attach with \`git branch -f <branch> HEAD && git checkout <branch>\` (or \`git rebase --continue\` if a rebase is still in flight). A 25h/19-commit wedge is what this looks like unattended (g-115-8145)."
elif [ "$B_STATUS" = "stalled" ]; then
  echo "▸ ⚠ BRANCH STALL: ${BRANCH} is ${AHEAD} commit(s) ahead of origin/${BRANCH}, oldest $(( AGE_MIN / 60 ))h$(( AGE_MIN % 60 ))m old (>= ${MAX_AGE_MIN}m) — under iteration-push's stranded-depth alarm (fires at 25 commits), so depth alone will never surface this."
  echo "    Check the push pipe: \`bash core/scripts/iteration-push.sh --no-push\` and read its integrate/defer line; a repeating non-transient defer needs the ESCALATION path, not another retry (g-115-6934)."
fi
# ELSE: clean — emit nothing (quiet on the common case).
exit 0
