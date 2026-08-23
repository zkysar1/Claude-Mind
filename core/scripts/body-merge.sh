#!/usr/bin/env bash
# Generalize-down body-WM merge (Phase 1C). Thin wrapper around body-merge.py.
#
# Usage:
#   bash core/scripts/body-merge.sh generalize-down --agent <mindKey> [--output json|text]
#
# Stdout: JSON summary {agent, scanned, merged[], noop[], skipped[], passes}.
#         BYTE-EXACT passthrough of body-merge.py's stdout — aspirations-consolidate
#         parses it, so nothing else may write on this channel (guard-2410: read a
#         wrapper's redirections as an output-channel POLICY before changing it).
# Stderr: human-readable error on failure; ALSO the retrospective lane's own JSON
#         summary and any fail-open warning (deliberately not muted — the hazard is
#         closed by routing the channel, not by discarding it).
# Exit: 0 success (incl. nothing-to-merge), 2 validation error, 3 io error.
#       Always body-merge.py's rc — a retrospective failure never changes it.
#
# Backward-compatible / dormant: in single-runner no closed-pending-merge Body
# manifest exists, so this is a no-op (empty summary). It activates only once a
# 2nd worker Body forks (Phase 2). See body-merge.py module docstring + the SSOT
# tree node mind-engine-identity-bridge.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── : chain the reducer-only retrospective lanes onto the merge ──
# CORRECTED  (2026-08-11, measured): the sentence that stood here —
# "worker_retrospective.py owns the surviving exp_capture encode lane" — is
# FALSE, and was false when written. That file has NO `experience` lane and no
# reference to exp_capture at all: RUN_LANES is ("team_state", "journal",
# "findings", "impk"), and `git show <rev>:core/scripts/worker_retrospective.py`
# across ALL FOUR of its revisions returns 0 occurrences of both `experience`
# and `exp_capture`. The lane has never existed in any revision. So 
# retired a WORKING drain (exp_capture_drain.py, deleted in 659dbef14) in favour
# of a lane that was never built, and exp_capture has had ZERO consumers since.
# Consequence, measured on cc-07 the same day: the slot sits at its array_limits
# cap of 20 with no drain, so the next worker capture is the first to FIFO-evict
# the oldest. Nothing is lost yet; everything after this is.
#
# The tombstone at iteration-close.sh (~L1419) rests on the same false premise —
# do not treat its "do not re-add" as settled until the lane it defers to exists.
# The remedy is to BUILD the lane here (which makes both comments true) rather
# than to revert : its merge-scoped placement argument below is sound
# and independent of the false ownership claim.
#
# ── RESOLVED 2026-08-15 (measured, alpha worker cc-07). THE REMEDY LANDED; the
# paragraphs above are now HISTORY, not a live defect. worker_retrospective.py
# RUN_LANES is ("team_state", "journal", "findings", "experience", "impk") and
# `_lane_experience` exists (~L525) with EXP_SLOT = "exp_capture" / EXP_SKILL_SLUG
# / EXP_TYPE at L138-140. So exp_capture HAS a drain, 's deferral target
# now exists, and its "do not re-add here" IS settled. Do not re-derive the lane's
# status from the text above — read the module; that text is retained because the
# reasoning about WHY the drain belongs there rather than here is still correct.
# The identical stale claim in iteration-close.sh (~L1520) was corrected in the
# same change (its cross-reference above says ~L1419; the real site is ~L1520 —
# line numbers in cross-references drift, names do not).
#
# CLOSED 2026-08-22 () — encoding_capture NOW HAS A CONSUMER, and it
# took exactly the shape this paragraph prescribed. `worker_retrospective.py`
# RUN_LANES is ("team_state","journal","findings","experience","encoding","impk")
# at L133; ENC_SLOT = "encoding_capture" (L164); `_lane_encoding` (L666) is
# dispatched at L788 and `load_enc_captures` (L496) is called at L933/L947. So
# 's reducer half landed: it feeds an existing writer rather than
# becoming a second encoder, exactly as advised. All four capture lanes now have
# drains. Corrected in the same change: wm.py (~L513), wm_write.py (~L113),
# iteration-close.sh (~L1656). ORIGINAL, retained because its census discipline
# and its "feed an existing writer" advice are still right:
#   "STILL OPEN, and it is the LAST of the four: encoding_capture has no consumer
#    at all ... Owner is  (HIGH, pending), half-shipped: producer landed,
#    reducer half did not. Copy the exp_capture shape above — feed an existing
#    writer, never become a second encoder."
#
# The surviving lanes are MERGE-scoped, not
# slot-scoped: argparse REQUIRES --goal-ids or --from-merge-summary, and
# `merged_goal_ids` is computed in body-merge.py (~L609, whose own comment calls
# it "the only place that can"). So iteration-close.sh — the site the goal
# originally named — has no input to pass it; THIS wrapper is the bash-owned
# site where the input actually exists. That satisfies guard-399's operative
# test ("WHO executes it") without needing the model to elect a SKILL.md
# `Bash:` line.
#
# Capture to a file rather than a variable: `$(...)` strips trailing newlines,
# so re-emitting from a variable is a re-serialization, not a passthrough. `cat`
# of the captured bytes is byte-exact by construction, and the same file feeds
# --from-merge-summary (which takes a PATH or `-`), so no stdin plumbing.
#
# errexit is suspended around the merge so its rc can be captured and its stdout
# still emitted — with `set -e` a non-zero merge would abort before the caller
# ever saw the summary (guard-614, the structured-output-wrapper hazard).
_bm_out="$(mktemp)"
trap 'rm -f "$_bm_out"' EXIT

set +e
py -3 "$SCRIPT_DIR/body-merge.py" "$@" > "$_bm_out"
_bm_rc=$?
set -e

cat "$_bm_out"

# Fail-open, and ONLY on a successful merge: a retrospective hiccup must never
# turn a completed merge into a failure (the fail-open contract inherited from
# the drain this replaces). Its stdout goes to stderr so the JSON contract above
# stays single-writer. An empty or malformed summary is a clean no-op —
# _goal_ids_from returns [] for a non-dict doc or a missing merged_goal_ids.
#
# `--output text` is an accepted flag and body-merge.py then SKIPS json.dumps, so
# the captured summary is not JSON. _goal_ids_from would _decode_first it, get a
# non-dict, and return [] — the lane would no-op SILENTLY, indistinguishable from
# "nothing to encode". Gate on the JSON shape so that skip is VISIBLE instead.
if [ "$_bm_rc" -eq 0 ]; then
    case "$(head -c 1 "$_bm_out" 2>/dev/null)" in
        "{")
            py -3 "$SCRIPT_DIR/worker_retrospective.py" --from-merge-summary "$_bm_out" >&2 \
                || echo "[body-merge] WARN: worker retrospective lanes did not run (rc=$?) — worker narratives stay unencoded until the next merge" >&2
            ;;
        *)
            echo "[body-merge] NOTE: summary is not JSON (--output text?) — retrospective lanes skipped; re-run with the default --output json to encode worker narratives" >&2
            ;;
    esac
fi

exit "$_bm_rc"
