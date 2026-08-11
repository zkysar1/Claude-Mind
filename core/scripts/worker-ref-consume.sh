#!/usr/bin/env bash
# worker-ref-consume.sh — the REDUCER-side consumer for worker Body carrier refs.
#
# . Pairs with `iteration-push.sh --push-worker-ref`, which is the
# worker half. A worker Body pushes HEAD to refs/workers/<agent>/<sid>; this
# script is what makes that ref more than a write into the void.
#
# WHY A CONSUMER IS THE LOAD-BEARING HALF, not an afterthought: the parent goal
# () measured a finished framework fix sitting on cc-07 and 0% present
# on cc-04 while its goal record read COMPLETE the whole time. Nothing was
# broken; there was simply no carrier. A pushed ref that nobody fetches
# reproduces that defect exactly one layer out — the artifact is now durable and
# remote and still reaches no one, and the goal record still reads COMPLETE.
# That is the orphaned-sweep trap in reclaim-routed-work.md: a channel with no
# consumer is indistinguishable from no channel.
#
# REPORT-ONLY BY DEFAULT, and that is a design decision rather than caution.
#  rejected the diff/patch-slot carrier because a framework patch that
# applies with fuzz, or applies cleanly to drifted context, is WORSE than one
# that is lost: a lost artifact is visibly missing, a mis-applied one is silently
# wrong in the layer every agent trusts. Auto-merging a worker's framework edits
# unreviewed re-introduces that same hazard through a different door. So the
# default output is a diff you can read; --merge is explicit, one ref at a time.
#
# Usage:
#   bash core/scripts/worker-ref-consume.sh                 # fetch + report all
#   bash core/scripts/worker-ref-consume.sh --json          # machine-readable
#   bash core/scripts/worker-ref-consume.sh --no-fetch      # report what is local
#   bash core/scripts/worker-ref-consume.sh --merge <ref>   # merge ONE ref
#
# Exit: 0 = ran (whether or not refs were found). 1 = a merge was requested and
# failed, or the repo/remote is unusable. Reporting zero refs is exit 0 — an
# empty fleet of workers is a normal state, not an error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true
REPO="${ITERATION_PUSH_REPO:-${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}}"

DO_FETCH=1
AS_JSON=0
MERGE_REF=""
SELF_SID="${MIND_SID:-}"

log() { echo "[worker-ref-consume] $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-fetch) DO_FETCH=0; shift;;
    --json)     AS_JSON=1; shift;;
    --merge)    MERGE_REF="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --repo)     REPO="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    -h|--help)  sed -n '1,40p' "${BASH_SOURCE[0]}"; exit 0;;
    *)          log "unknown arg: $1" >&2; shift;;
  esac
done

git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { log "not a git repo: $REPO" >&2; exit 1; }

# Fetch every worker ref into the SAME namespace locally, so `refs/workers/...`
# means the same thing on both sides. --prune removes refs whose body has been
# retired upstream; without it a closed body's ref lingers locally forever and
# reads as outstanding work.
if [ "$DO_FETCH" = 1 ]; then
  if ! git -C "$REPO" fetch --prune origin "+refs/workers/*:refs/workers/*" >/dev/null 2>&1; then
    log "fetch of refs/workers/* FAILED — cannot report on carrier refs" >&2
    exit 1
  fi
fi

if [ -n "$MERGE_REF" ]; then
  git -C "$REPO" rev-parse --verify "$MERGE_REF" >/dev/null 2>&1 || {
    log "no such ref locally: $MERGE_REF (run without --merge first to fetch+list)" >&2; exit 1; }
  log "merging $MERGE_REF into $(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
  if git -C "$REPO" merge --no-edit "$MERGE_REF"; then
    log "merged $MERGE_REF"; exit 0
  fi
  log "MERGE CONFLICT on $MERGE_REF — resolve by hand; the ref is unchanged and can be re-merged" >&2
  exit 1
fi

# Report. For each worker ref: how many commits it carries that HEAD does not,
# and which of those touch the framework paths that motivated the carrier.
# PREFIX form 'refs/workers/', NOT the glob 'refs/workers/*'. for-each-ref
# matches patterns with PATHNAME semantics, so `*` does not cross a `/` — and
# every carrier ref is refs/workers/<agent>/<sid>, three levels deep. The glob
# form matched ZERO refs while the push and the fetch had both succeeded and the
# ref was sitting in the local ref store. Measured 2026-08-08 on cc-07 while
# building this script: 'refs/workers/*' -> 0, 'refs/workers/**' -> 1,
# 'refs/workers/' -> 1. The failure is silent and success-shaped — a consumer
# that lists nothing is indistinguishable from a fleet with no workers, which is
# the exact defect this whole carrier exists to close, reproduced one layer out.
REFS="$(git -C "$REPO" for-each-ref --format='%(refname)' 'refs/workers/' 2>/dev/null)"

n_refs=0; n_outstanding=0
[ "$AS_JSON" = 1 ] && printf '{"refs":['
first=1
while IFS= read -r ref; do
  [ -z "$ref" ] && continue
  n_refs=$((n_refs+1))
  sid="${ref##*/}"; agent_seg="${ref%/*}"; agent="${agent_seg##*/}"
  ahead="$(git -C "$REPO" rev-list --count "HEAD..$ref" 2>/dev/null || echo 0)"
  case "$ahead" in ''|*[!0-9]*) ahead=0;; esac
  # Framework-path files carried by those commits — the output class this
  # carrier exists for. A ref carrying only agent-store churn is not a
  # framework delivery and should not read like one.
  # Count the FULL set, then truncate only the DISPLAY. Counting after `head -N`
  # makes the count silently equal the cap, so a ref carrying 300 framework files
  # and one carrying exactly 50 report identically — a bounded reading presented
  # as a total (guard-346). Caught on this script's own first run: it printed
  # framework_files=50 against a true 125-commit delta.
  fw_all="$(git -C "$REPO" diff --name-only "HEAD...$ref" 2>/dev/null \
            | grep -E '^(core/|\.claude/|CLAUDE\.md)')"
  fw_count="$(printf '%s' "$fw_all" | grep -c . || true)"
  fw="$(printf '%s' "$fw_all" | head -50)"
  fw_more=$(( fw_count > 50 ? fw_count - 50 : 0 ))
  is_self=0; [ -n "$SELF_SID" ] && [ "$sid" = "$SELF_SID" ] && is_self=1
  [ "$ahead" -gt 0 ] && [ "$is_self" = 0 ] && n_outstanding=$((n_outstanding+1))
  if [ "$AS_JSON" = 1 ]; then
    [ "$first" = 0 ] && printf ','
    first=0
    printf '{"ref":"%s","agent":"%s","sid":"%s","commits_ahead":%s,"framework_files":%s,"is_self":%s}' \
      "$ref" "$agent" "$sid" "$ahead" "$fw_count" "$is_self"
  else
    tag=""; [ "$is_self" = 1 ] && tag="  (this body — nothing to consume)"
    echo "  $ref"
    echo "      agent=$agent sid=$sid  commits_ahead=$ahead  framework_files=$fw_count$tag"
    if [ "$fw_count" -gt 0 ] && [ "$is_self" = 0 ]; then
      printf '%s\n' "$fw" | sed 's/^/        /'
      [ "$fw_more" -gt 0 ] && echo "        ... and $fw_more more (display capped at 50; framework_files above is the FULL count)"
      echo "      merge with: bash core/scripts/worker-ref-consume.sh --merge $ref"
    fi
  fi
done <<< "$REFS"

if [ "$AS_JSON" = 1 ]; then
  printf '],"ref_count":%s,"outstanding":%s}\n' "$n_refs" "$n_outstanding"
else
  if [ "$n_refs" -eq 0 ]; then
    # Deliberately NOT phrased as "all clear". Zero refs and a broken fetch look
    # identical from here, and the fetch above is the only thing separating them.
    log "0 worker carrier refs present. Either no worker Body has pushed one, or none exists yet — this is a normal state, not a verified-empty one."
  else
    log "$n_refs carrier ref(s), $n_outstanding with commits this branch lacks."
  fi
fi
exit 0
