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
#   bash core/scripts/worker-ref-consume.sh --check         # report + threshold banner
#       [--max-depth <N>] [--max-age-h <H>]                 #   (defaults 30 / 24; env
#                                                           #   WORKER_REF_MAX_DEPTH /
#                                                           #   WORKER_REF_MAX_AGE_H)
#   bash core/scripts/worker-ref-consume.sh --merge <ref>   # merge ONE ref
#   bash core/scripts/worker-ref-consume.sh --retire <ref>  # delete a CONSUMED ref
#                                                           #   (with tip-SHA receipt)
#       [--force-retire-live "<justification>"]             #   override the liveness
#                                                           #   refusal (logged to receipt)
#
# --retire refuses unless the ref is fully reachable from refs/remotes/origin/main
# — reachable from local HEAD is NOT enough: a merged-but-unpushed main would make
# the remote ref the ONLY remote copy of the work, and deleting it then orphans
# the content (archive-before-delete: the merge reaching origin/main IS the
# archive; the receipt records the tip SHA so `git push origin <tip>:<ref>`
# recreates the ref from any box). Receipts append to
# core/logs/worker-ref-retirements.jsonl — machine-local BY DESIGN (guard-3059
# class: the durable record is origin/main's reachability, not the receipt file).
#
# --retire ALSO refuses when a live team-state in_flight_bodies row names the
# ref's body (). Reachability answers whether the CONTENT is safe to
# delete; it says nothing about whether a RUNNING body still needs the HANDLE —
# measured during , both carrier tips were merged (reachable) while
# their bodies were mid-goal, so the old precondition would have approved
# deleting two live carriers (archive-before-delete step 7: enumerate what
# READS this data before the delete fires). The gate deliberately forms NO
# liveness opinion of its own (body_row_reaper.py owns stale-row reaping; two
# liveness opinions about one Body are worse than one): a row PRESENT = refuse,
# row ABSENT = proceed, source UNREADABLE = refuse (fail-closed — keeping a ref
# costs nothing, deleting a live one is an unrecoverable handle loss). A row
# left by an uncleanly-dead body is the reaper's to remove, after which retire
# passes; --force-retire-live "<why>" is the operator-knows-it-is-dead override
# and is recorded verbatim in the receipt.
#
# Exit: 0 = ran (whether or not refs were found; --check is advisory and exits 0
# even on breach). 1 = a merge/retire was requested and failed or was refused,
# or the repo/remote is unusable. Reporting zero refs is exit 0 — an empty fleet
# of workers is a normal state, not an error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true
REPO="${ITERATION_PUSH_REPO:-${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}}"

DO_FETCH=1
AS_JSON=0
MERGE_REF=""
RETIRE_REF=""
DO_CHECK=0
MAX_DEPTH="${WORKER_REF_MAX_DEPTH:-30}"
MAX_AGE_H="${WORKER_REF_MAX_AGE_H:-24}"
SELF_SID="${MIND_SID:-}"
FORCE_RETIRE_LIVE_JUST=""
# TEST-ONLY hermeticity seam (MIND_AGENTS_ROOT precedent): the retire liveness
# gate reads in_flight_bodies through this reader. Production NEVER sets the
# env — the default is the real sibling, and a test pins that default.
TEAM_STATE_READER="${WORKER_REF_TEAM_STATE_READER:-$SCRIPT_DIR/team-state-read.sh}"

log() { echo "[worker-ref-consume] $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-fetch)   DO_FETCH=0; shift;;
    --json)       AS_JSON=1; shift;;
    --check)      DO_CHECK=1; shift;;
    --max-depth)  MAX_DEPTH="${2:-30}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --max-age-h)  MAX_AGE_H="${2:-24}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --merge)      MERGE_REF="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --retire)     RETIRE_REF="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --force-retire-live) FORCE_RETIRE_LIVE_JUST="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --repo)       REPO="${2:-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    -h|--help)    sed -n '1,55p' "${BASH_SOURCE[0]}"; exit 0;;
    *)            log "unknown arg: $1" >&2; shift;;
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

if [ -n "$RETIRE_REF" ]; then
  git -C "$REPO" rev-parse --verify "$RETIRE_REF" >/dev/null 2>&1 || {
    log "no such ref locally: $RETIRE_REF (run without --retire first to fetch+list)" >&2; exit 1; }
  # The retire precondition is remote durability, so the base of comparison MUST
  # be the freshest origin/main we can get — a stale remote-tracking ref could
  # refuse a legitimately-consumed ref (annoying) while a merged-but-unpushed
  # LOCAL HEAD would approve an orphaning delete (destructive). Fetch main here
  # even under --no-fetch: --no-fetch exists to make the REPORT cheap, and a
  # retire is not a report.
  git -C "$REPO" fetch origin main >/dev/null 2>&1 \
    || log "WARN: fetch of origin main failed — testing against last-known origin/main" >&2
  if ! git -C "$REPO" merge-base --is-ancestor "$RETIRE_REF" refs/remotes/origin/main 2>/dev/null; then
    log "REFUSED: $RETIRE_REF is NOT fully reachable from origin/main." >&2
    log "Merge it and push main first — retiring now would delete the only remote copy of its commits." >&2
    exit 1
  fi

  # : LIVENESS precondition. Reachability proved the CONTENT durable;
  # this proves no RUNNING body still needs the HANDLE. The ref path carries
  # both join keys: refs/workers/<agent>/<sid>.
  ref_sid="${RETIRE_REF##*/}"
  ref_agent_seg="${RETIRE_REF%/*}"; ref_agent="${ref_agent_seg##*/}"
  body_row_state="absent"
  row_json="$(bash "$TEAM_STATE_READER" --field "agent_status.${ref_agent}.in_flight_bodies.${ref_sid}" --json 2>/dev/null)"
  reader_rc=$?
  if [ "$reader_rc" -ne 0 ] || [ -z "$row_json" ]; then
    # FAIL CLOSED: an unreadable liveness source refuses, never permits. The
    # asymmetry is absolute — keeping a ref costs nothing; deleting a live
    # body's carrier is an unrecoverable handle loss.
    if [ -z "$FORCE_RETIRE_LIVE_JUST" ]; then
      log "REFUSED: cannot read in_flight_bodies for ${ref_agent}/${ref_sid} (reader rc=$reader_rc, output '$row_json')." >&2
      log "Fail-closed: an unreadable liveness source refuses rather than permits. Fix the team-state read," >&2
      log "or — only if you KNOW the body is dead — retry with --force-retire-live \"<justification>\"." >&2
      exit 1
    fi
    body_row_state="UNREADABLE-OVERRIDDEN rc=$reader_rc"
  elif [ "$row_json" != "null" ]; then
    row_goal="$(ROW_JSON="$row_json" py -3 -c 'import json,os
try: r=json.loads(os.environ["ROW_JSON"]); print(r.get("goal_id") or "unknown")
except Exception: print("unparseable")' 2>/dev/null || echo unknown)"
    row_claimed="$(ROW_JSON="$row_json" py -3 -c 'import json,os
try: r=json.loads(os.environ["ROW_JSON"]); print(r.get("claimed_at") or "unknown")
except Exception: print("unparseable")' 2>/dev/null || echo unknown)"
    row_age_h="?"
    claimed_ct="$(date -d "$row_claimed" +%s 2>/dev/null || true)"
    case "$claimed_ct" in
      ''|*[!0-9]*) ;;
      *) row_age_h=$(( ($(date +%s) - claimed_ct) / 3600 ));;
    esac
    if [ -z "$FORCE_RETIRE_LIVE_JUST" ]; then
      log "REFUSED: a live in_flight_bodies row names this ref's body — goal=$row_goal claimed_at=$row_claimed (~${row_age_h}h ago)." >&2
      log "A RUNNING body still holds this carrier as its push target; retiring it deletes the handle mid-goal." >&2
      log "If the body is dead, its row is body_row_reaper.py's to remove (g-306-191) — reap first, then retire." >&2
      log "Or — only if you KNOW the body is dead — retry with --force-retire-live \"<justification>\"." >&2
      exit 1
    fi
    body_row_state="LIVE-OVERRIDDEN goal=$row_goal claimed_at=$row_claimed"
  fi

  tip_sha="$(git -C "$REPO" rev-parse "$RETIRE_REF")"
  main_sha="$(git -C "$REPO" rev-parse refs/remotes/origin/main 2>/dev/null || echo unknown)"
  receipt_dir="$REPO/core/logs"
  mkdir -p "$receipt_dir" 2>/dev/null || true
  # Receipt JSON is printf-composed; the two operator-supplied strings are
  # sanitized (double-quote -> single-quote) so a justification cannot break
  # the receipt line's parseability.
  safe_just="${FORCE_RETIRE_LIVE_JUST//\"/\'}"
  liveness_override_field=""
  [ -n "$safe_just" ] && liveness_override_field=",\"liveness_override\":\"$safe_just\""
  printf '{"ref":"%s","tip_sha":"%s","retired_at":"%s","retired_by_agent":"%s","verified_ancestor_of_origin_main":"%s","body_row":"%s"%s,"recreate_with":"git push origin %s:%s"}\n' \
    "$RETIRE_REF" "$tip_sha" "$(date +%Y-%m-%dT%H:%M:%S)" "${MIND_AGENT:-unknown}" "$main_sha" "${body_row_state//\"/\'}" "$liveness_override_field" "$tip_sha" "$RETIRE_REF" \
    >> "$receipt_dir/worker-ref-retirements.jsonl"
  if git -C "$REPO" push origin ":$RETIRE_REF" >/dev/null 2>&1; then
    git -C "$REPO" update-ref -d "$RETIRE_REF" 2>/dev/null || true
    log "retired $RETIRE_REF (tip $tip_sha reachable from origin/main $main_sha)"
    log "receipt: core/logs/worker-ref-retirements.jsonl — recreate with: git push origin $tip_sha:$RETIRE_REF"
    exit 0
  fi
  log "remote delete FAILED for $RETIRE_REF — receipt written but the ref still exists on origin; retry later" >&2
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

# Collect first, emit second: supersession is a PAIRWISE property (ref A is an
# ancestor of ref B), so it cannot be computed inside a single streaming pass.
# Chains are the normal shape, not the edge case — one agent's successive
# session SIDs each push a ref, and every earlier ref is a strict ancestor of
# the next (measured 2026-08-12,  N1: 6 refs = 2 chains; the 4
# non-tip refs were pure duplication, and a report that enumerates them as
# separate work inflates the review burden 3x). Outstanding therefore counts
# TIPS ONLY: superseded refs are listed but tagged, so the reader reviews the
# tip and knows the ancestor is contained in it.
REF_LIST=()
while IFS= read -r ref; do
  [ -n "$ref" ] && REF_LIST+=("$ref")
done <<< "$REFS"

superseding_of() {
  # Print the ref that supersedes $1 (first found), or nothing.
  # Equal-tip tie-break: when two refs point at the SAME commit each is an
  # ancestor of the other; marking both superseded would vanish the work from
  # the outstanding count entirely, so only the lexically-greater ref defers.
  local a="$1" b a_sha b_sha
  a_sha="$(git -C "$REPO" rev-parse "$a" 2>/dev/null)"
  for b in "${REF_LIST[@]}"; do
    [ "$a" = "$b" ] && continue
    b_sha="$(git -C "$REPO" rev-parse "$b" 2>/dev/null)"
    if [ "$a_sha" = "$b_sha" ]; then
      [ "$a" \> "$b" ] && { echo "$b"; return 0; }
      continue
    fi
    if git -C "$REPO" merge-base --is-ancestor "$a" "$b" 2>/dev/null; then
      echo "$b"; return 0
    fi
  done
  return 0
}

n_refs=0; n_outstanding=0; n_unreadable=0
NOW_CT="$(date +%s)"
CHECK_BREACHES=()
[ "$AS_JSON" = 1 ] && printf '{"refs":['
first=1
for ref in ${REF_LIST[@]+"${REF_LIST[@]}"}; do
  n_refs=$((n_refs+1))
  sid="${ref##*/}"; agent_seg="${ref%/*}"; agent="${agent_seg##*/}"
  # F-002 (): the rev-list ERROR value must not be byte-identical to
  # the healthy "fully contained" 0. A ref whose rev-list fails (bad object,
  # corrupt ref, unfetched sid) was previously reported as fully consumed and
  # silently dropped out of `outstanding` — for a visibility instrument that is
  # the wrong direction to fail (guard-2298/guard-3662 class). Errors now land
  # in their own `unreadable` bucket: never counted outstanding (the count is
  # unknowable), never folded into "nothing outstanding" either.
  unreadable=0
  ahead_all="$(git -C "$REPO" rev-list --count "HEAD..$ref" 2>/dev/null || echo '')"
  case "$ahead_all" in ''|*[!0-9]*) unreadable=1; ahead_all=0; n_unreadable=$((n_unreadable+1));; esac
  # `ahead` counts UNLANDED CONTENT, not unlanded commits ( drain,
  # 2026-08-17). A live body syncs origin/main into its branch with plain
  # "Merge remote-tracking branch 'origin/main'" commits; once main has consumed
  # the body's real work, those sync merges are the only commits main lacks and
  # they carry NOTHING main does not already have — yet the plain rev-list count
  # read them as commits_ahead=2, the three-dot diff below listed the 41
  # framework files they had pulled FROM main, and the ref reported as an
  # outstanding TIP: a perpetual false advisory at every iteration-close AND a
  # drain-lane fire every interval that would merge nothing and pay the full-suite
  # gate for it. So: non-merge commits count; a merge commit counts ONLY if it
  # carries content of its own — `git show --remerge-diff` prints exactly the
  # hunks a merge added beyond the automatic result (a conflict resolution or an
  # evil merge), and prints nothing for a clean sync. Measured on the live refs
  # before shipping: 6 sync merges across two bodies read 0 bytes each; the one
  # hand-resolved merge in main's history (fc2b0ca8d) read 5,129 — the
  # discriminator has both a negative and a positive control. Content-free sync
  # merges are reported separately as `sync_merges` so the reader can see WHY a
  # ref with commits main lacks is nevertheless nothing to consume.
  ahead=0; sync_merges=0
  if [ "$unreadable" = 0 ] && [ "$ahead_all" -gt 0 ]; then
    ahead="$(git -C "$REPO" rev-list --count --no-merges "HEAD..$ref" 2>/dev/null || echo '')"
    case "$ahead" in ''|*[!0-9]*) ahead="$ahead_all";; esac
    for m in $(git -C "$REPO" rev-list --merges "HEAD..$ref" 2>/dev/null); do
      if [ -n "$(git -C "$REPO" show --remerge-diff --format= "$m" 2>/dev/null | head -c 1)" ]; then
        ahead=$((ahead+1))          # content-bearing merge: real unlanded work
      else
        sync_merges=$((sync_merges+1))
      fi
    done
  fi
  # Framework-path files carried by those commits — the output class this
  # carrier exists for. A ref carrying only agent-store churn is not a
  # framework delivery and should not read like one.
  # Count the FULL set, then truncate only the DISPLAY. Counting after `head -N`
  # makes the count silently equal the cap, so a ref carrying 300 framework files
  # and one carrying exactly 50 report identically — a bounded reading presented
  # as a total (guard-346). Caught on this script's own first run: it printed
  # framework_files=50 against a true 125-commit delta.
  # A ref whose only unlanded commits are content-free sync merges carries no
  # framework files of its own — the three-dot diff would list what it pulled
  # FROM main, so it is skipped rather than reported (same false-reading class).
  fw_all=""
  if [ "$ahead" -gt 0 ]; then
    fw_all="$(git -C "$REPO" diff --name-only "HEAD...$ref" 2>/dev/null \
              | grep -E '^(core/|\.claude/|CLAUDE\.md)')"
  fi
  fw_count="$(printf '%s' "$fw_all" | grep -c . || true)"
  fw="$(printf '%s' "$fw_all" | head -50)"
  fw_more=$(( fw_count > 50 ? fw_count - 50 : 0 ))
  is_self=0; [ -n "$SELF_SID" ] && [ "$sid" = "$SELF_SID" ] && is_self=1
  superseded_by=""
  [ "$ahead" -gt 0 ] && superseded_by="$(superseding_of "$ref")"
  # Stranding age = the OLDEST commit this branch lacks (how long has finished
  # work been waiting), not the newest (which only says the body is alive).
  # Same content-not-commits rule as `ahead`: a sync merge older than the body's
  # real work must not age the ref past MAX_AGE_H on its own. Non-merge commits
  # first; fall back to the full list only when the unlanded content IS a merge.
  oldest_ct="$(git -C "$REPO" log --format=%ct --no-merges "HEAD..$ref" 2>/dev/null | tail -1)"
  [ -z "$oldest_ct" ] && oldest_ct="$(git -C "$REPO" log --format=%ct "HEAD..$ref" 2>/dev/null | tail -1)"
  age_h=0
  case "$oldest_ct" in
    ''|*[!0-9]*) ;;
    *) age_h=$(( (NOW_CT - oldest_ct) / 3600 )); [ "$age_h" -lt 0 ] && age_h=0;;
  esac
  is_tip=0
  if [ "$ahead" -gt 0 ] && [ "$is_self" = 0 ] && [ -z "$superseded_by" ]; then
    is_tip=1
    n_outstanding=$((n_outstanding+1))
  fi
  # Threshold evaluation — TIPS only (an ancestor's depth is contained in its
  # tip's), and evaluate BOTH axes rather than stopping at the first breach
  # (guard-3644: an AND/OR probe that reports only its first failing condition
  # hides the shape of the problem).
  if [ "$DO_CHECK" = 1 ] && [ "$is_tip" = 1 ]; then
    breach=""
    [ "$ahead" -gt "$MAX_DEPTH" ] && breach="depth=$ahead>(max $MAX_DEPTH)"
    if [ "$age_h" -gt "$MAX_AGE_H" ]; then
      [ -n "$breach" ] && breach="$breach "
      breach="${breach}oldest_unlanded=${age_h}h>(max ${MAX_AGE_H}h)"
    fi
    [ -n "$breach" ] && CHECK_BREACHES+=("$ref: $breach")
  fi
  # An unreadable ref is itself a visibility failure — surface it through the
  # same banner the thresholds use, whether or not it is a tip (tip-ness is
  # derived from `ahead`, which is exactly what could not be read).
  if [ "$DO_CHECK" = 1 ] && [ "$unreadable" = 1 ]; then
    CHECK_BREACHES+=("$ref: unreadable (rev-list failed — bad object, corrupt ref, or unfetched sid; ahead-count unknown, NOT counted outstanding)")
  fi
  if [ "$AS_JSON" = 1 ]; then
    [ "$first" = 0 ] && printf ','
    first=0
    printf '{"ref":"%s","agent":"%s","sid":"%s","commits_ahead":%s,"framework_files":%s,"is_self":%s,"superseded_by":"%s","oldest_unlanded_age_h":%s,"unreadable":%s,"sync_merges":%s}' \
      "$ref" "$agent" "$sid" "$ahead" "$fw_count" "$is_self" "$superseded_by" "$age_h" "$unreadable" "$sync_merges"
  else
    tag=""; [ "$is_self" = 1 ] && tag="  (this body — nothing to consume)"
    [ "$sync_merges" -gt 0 ] && tag="$tag  (+$sync_merges content-free sync merge(s) of origin/main — not counted)"
    echo "  $ref"
    echo "      agent=$agent sid=$sid  commits_ahead=$ahead  framework_files=$fw_count  oldest_unlanded=${age_h}h$tag"
    [ "$unreadable" = 1 ] && echo "      ⚠ UNREADABLE — rev-list failed for this ref (bad object / corrupt ref / unfetched sid); ahead-count unknown, NOT counted outstanding"
    if [ -n "$superseded_by" ]; then
      echo "      ANCESTOR of ${superseded_by} — its commits are contained there; review the tip instead"
      echo "      once the tip is merged+pushed, retire this ref: bash core/scripts/worker-ref-consume.sh --retire $ref"
    elif [ "$fw_count" -gt 0 ] && [ "$is_self" = 0 ]; then
      printf '%s\n' "$fw" | sed 's/^/        /'
      [ "$fw_more" -gt 0 ] && echo "        ... and $fw_more more (display capped at 50; framework_files above is the FULL count)"
      echo "      merge with: bash core/scripts/worker-ref-consume.sh --merge $ref"
    fi
  fi
done

if [ "$AS_JSON" = 1 ]; then
  printf '],"ref_count":%s,"outstanding":%s,"unreadable":%s}\n' "$n_refs" "$n_outstanding" "$n_unreadable"
else
  if [ "$n_refs" -eq 0 ]; then
    # Deliberately NOT phrased as "all clear". Zero refs and a broken fetch look
    # identical from here, and the fetch above is the only thing separating them.
    log "0 worker carrier refs present. Either no worker Body has pushed one, or none exists yet — this is a normal state, not a verified-empty one."
  else
    wr_unread=""; [ "$n_unreadable" -gt 0 ] && wr_unread=" ⚠ $n_unreadable UNREADABLE ref(s) excluded from that count — see per-ref lines."
    log "$n_refs carrier ref(s), $n_outstanding outstanding TIP(s) with commits this branch lacks (ancestor refs are contained in their tips and not counted).$wr_unread"
  fi
fi

if [ "$DO_CHECK" = 1 ] && [ ${#CHECK_BREACHES[@]} -gt 0 ]; then
  # Advisory banner, exit 0 regardless: the wired caller is the reducer's close
  # path, which must never fail on a visibility probe. The ACTING lane is the
  # recurring consume/disposition goal ( shape b) — this banner exists
  # so a growing backlog is impossible to not-see between that goal's runs.
  echo ""
  log "⚠ STRANDED WORKER WORK past thresholds (depth>$MAX_DEPTH or oldest>${MAX_AGE_H}h):"
  for b in "${CHECK_BREACHES[@]}"; do
    log "  ⚠ $b"
  done
  log "Disposition per ref: --merge (then push main, then --retire), carry specific hunks, or discard with a receipt."
fi
exit 0
