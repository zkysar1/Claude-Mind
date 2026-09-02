#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- runs on the SessionStart critical path. Keep it local:
# no daemon hop, no MCP, no remote service. The one network call is a THROTTLED
# `git fetch` with a hard timeout, which is the minimum this check cannot do
# without (see WHY A FETCH below).
#
# local-backend-staleness-check.sh — warn at session start when a
# STORAGE_BACKEND=local clone is behind origin, or when another machine has
# pushed recently.
#
# WHY THIS EXISTS ()
# ---------------------------
# Under STORAGE_BACKEND=local the git remote is the ONLY cross-machine
# coordination point that exists — there is no shared object store keeping the
# world/meta trees in sync. Nothing consulted it at session start. Measured
# 2026-08-20 on a local-backend deployment: a box ran two full agent sessions
# while 457 commits behind origin, reading every world store from a week-stale
# working tree, and nothing reported it. Every fleet-liveness conclusion that
# box reached was wrong — and wrong SILENTLY, which is the part that matters:
# a stale clone produces confident, well-formed, entirely false readings.
#
# CONTRACT (all four are load-bearing)
# ------------------------------------
#   WARN ONLY   — never blocks, never exits non-zero. Session start must
#                 survive every failure mode of this script.
#   FAIL OPEN   — no network, no remote, no upstream, detached HEAD, not a repo:
#                 all exit 0 SILENTLY. An offline box starts clean and quiet.
#   BACKEND-GATED — own-cloud pays NOTHING (returns before the fetch). That
#                 backend has its own sync layer; this check is meaningless there.
#   STDOUT      — the warning goes to stdout, NOT stderr. guard-772: a fail-open
#                 WARN written only to stderr is INVISIBLE when the command runs
#                 inside a backgrounded subprocess, which is exactly how hooks
#                 often run. A warning nobody can see is not a warning.
#
# WHY A FETCH (and why it is throttled)
# -------------------------------------
# `git rev-list HEAD..@{upstream}` measures against the LAST FETCHED ref, so
# without a fetch a week-stale clone reports "0 behind" — the confident zero
# this check exists to prevent (guard-1568: fetch before ANY negative claim
# about remote git state). But this file is on the session-start latency path,
# so the fetch is throttled stateless via FETCH_HEAD mtime (the same mechanism
# iteration-push.sh uses) and hard-bounded by `timeout`. Past the throttle
# window we still report, using the last-known origin ref, and we SAY that the
# reading is throttled rather than presenting it as fresh.
#
# SCOPE BOUNDARY: this script touches NOTHING but its own stdout. It does not
# write state, does not edit the heartbeat or recovery paths (explicitly out of
# scope for ), and does not fetch on an own-cloud box.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true
: "${PROJECT_ROOT:="$(cd "$SCRIPT_DIR/.." && pwd)"}"

# Tunables (env-overridable; no config-file coupling on the hot path).
BEHIND_THRESHOLD="${LOCAL_STALENESS_BEHIND_THRESHOLD:-5}"
FETCH_INTERVAL_MIN="${LOCAL_STALENESS_FETCH_INTERVAL_MIN:-10}"
FETCH_TIMEOUT_S="${LOCAL_STALENESS_FETCH_TIMEOUT_S:-15}"
PEER_WINDOW_MIN="${LOCAL_STALENESS_PEER_WINDOW_MIN:-120}"
# How stale the last SUCCESSFUL fetch must be before an unmeasurable verdict is
# announced. Deliberately generous: a transient offline moment must stay quiet
# (this file's contract), but a box that has not reached origin in hours is
# reporting "clean" from refs that cannot support the claim.
UNMEASURED_WARN_MIN="${LOCAL_STALENESS_UNMEASURED_WARN_MIN:-360}"

# ─── Backend gate ───────────────────────────────────────────────────────────
# STORAGE_BACKEND is not exported into the agent shell, so resolve it the same
# cheap way heartbeat-tick.sh and check-prerequisites.sh do: live env first,
# else ONE grepped .env.local line (no secret sourcing). Anything that is not
# own-cloud is treated as local — the safe direction, since the cost of running
# this on an unrecognised backend is one throttled fetch and a possible warning,
# while the cost of skipping it on a local box is the silent-staleness bug.
_BACKEND="${STORAGE_BACKEND:-}"
if [ -z "$_BACKEND" ] && [ -f "$PROJECT_ROOT/.env.local" ]; then
    _BACKEND="$(grep -E '^[[:space:]]*STORAGE_BACKEND[[:space:]]*=' "$PROJECT_ROOT/.env.local" 2>/dev/null \
        | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
fi
_BACKEND="$(printf '%s' "$_BACKEND" | tr '[:upper:]' '[:lower:]')"
[ "$_BACKEND" = "own-cloud" ] && exit 0   # VERIFY clause 4: own-cloud emits nothing new.

# ─── Repo preconditions (every failure is a SILENT exit 0) ──────────────────
command -v git >/dev/null 2>&1 || exit 0
git -C "$PROJECT_ROOT" rev-parse --git-dir >/dev/null 2>&1 || exit 0
UPSTREAM="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)" || exit 0
[ -n "$UPSTREAM" ] || exit 0

# ─── Throttled fetch ────────────────────────────────────────────────────────
# Stateless throttle: FETCH_HEAD's mtime IS the last-fetch clock, so there is no
# state file to keep, stale, or clean up.
FETCH_HEAD="$(git -C "$PROJECT_ROOT" rev-parse --git-dir 2>/dev/null)/FETCH_HEAD"
_now=$(date +%s 2>/dev/null || echo 0)
_last=0
[ -f "$FETCH_HEAD" ] && _last=$(date -r "$FETCH_HEAD" +%s 2>/dev/null || echo 0)
_age_min=$(( (_now - _last) / 60 ))
THROTTLED=0
FETCH_FAILED=0
if [ "$_last" -gt 0 ] && [ "$_age_min" -lt "$FETCH_INTERVAL_MIN" ]; then
    THROTTLED=1
else
    # Hard-bounded. An offline box must not hang session start on a DNS timeout.
    # The BOUND is right; discarding its outcome was not. `|| true` threw away the
    # one fact that decides whether the measurement below means anything: on a
    # timeout (rc=124) or any fetch error, BEHIND is computed from the LAST
    # SUCCESSFUL fetch's refs, so a box that cannot reach origin reports "0 behind"
    # and this check stays SILENT — the same confident-wrong-reading failure its
    # own warning text names, turned on itself (guard-1947: an instrument that
    # cannot see is not one that saw nothing).
    timeout "$FETCH_TIMEOUT_S" git -C "$PROJECT_ROOT" fetch --quiet origin >/dev/null 2>&1 || FETCH_FAILED=1
    # Re-read the clock: a SUCCESSFUL fetch just rewrote FETCH_HEAD, so _age_min
    # must not keep describing the pre-fetch state in the report below.
    if [ "$FETCH_FAILED" = "0" ] && [ -f "$FETCH_HEAD" ]; then
        _last=$(date -r "$FETCH_HEAD" +%s 2>/dev/null || echo "$_last")
        _age_min=$(( (_now - _last) / 60 ))
    fi
fi

# ─── Measure ────────────────────────────────────────────────────────────────
BEHIND="$(git -C "$PROJECT_ROOT" rev-list --count "HEAD..$UPSTREAM" 2>/dev/null || echo 0)"
case "$BEHIND" in ''|*[!0-9]*) BEHIND=0 ;; esac

# Second-box signal. The fleet stamps the box into the commit EMAIL
# (agent@cc-09, agent@cc-10, ...), so the author email IS the machine id — no
# extra plumbing, and it works on any clone. Compare unlanded upstream commits
# in the recent window against THIS box's identity.
MY_ID="$(git -C "$PROJECT_ROOT" config user.email 2>/dev/null)"
[ -n "$MY_ID" ] || MY_ID="agent@$(hostname 2>/dev/null || echo unknown)"
PEERS=""
if [ "$BEHIND" -gt 0 ]; then
    # Filter by COMMITTER TIMESTAMP in awk — deliberately NOT `git log --since`.
    # `--since` is a TRAVERSAL CUTOFF, not a filter: git walks newest-first and
    # STOPS at the first commit older than the cutoff, so a single old-dated
    # commit at the tip hides every recent commit behind it. Measured
    # 2026-08-20 on a fixture where 7 commits were 67 SECONDS old and one
    # old-DATED commit sat at the tip: `--since="60 minutes ago"` returned
    # EMPTY. That is a FALSE NEGATIVE — the peer warning silently does not
    # fire — which is the one direction this check must never fail in.
    # Rebases, cherry-picks, amended dates and merged old branches all produce
    # a non-monotonic tip, so this is not a synthetic-only concern.
    _cutoff=$(( _now - PEER_WINDOW_MIN * 60 ))
    PEERS="$(git -C "$PROJECT_ROOT" log "HEAD..$UPSTREAM" --format='%ct %ae' 2>/dev/null \
             | awk -v c="$_cutoff" '$1 >= c { print $2 }' \
             | grep -v -x -F "$MY_ID" | sort -u | tr '\n' ' ' | sed 's/ *$//')"
fi

# ─── Report (stdout — guard-772) ────────────────────────────────────────────
_note=""
[ "$THROTTLED" = "1" ] && _note=" [throttled: last fetch ${_age_min}m ago (< ${FETCH_INTERVAL_MIN}m) — measured against the last-known origin ref, not a fresh one]"
# Same idiom as the throttled note, for the branch that never had one: a failed
# fetch also measures against a stale ref, and BEHIND is then a LOWER BOUND.
# _last==0 means NO successful fetch is on record (no FETCH_HEAD, or unreadable).
# _age_min is then (_now/60) — a ~56-year nonsense figure, not an age. The
# original code never met this because the throttled branch requires _last>0;
# both uses added below can. Render the state, never the arithmetic.
if [ "$_last" -gt 0 ]; then _age_desc="${_age_min}m ago"; else _age_desc="never (no successful fetch on record)"; fi
[ "$FETCH_FAILED" = "1" ] && _note=" [fetch FAILED (timeout or unreachable) — measured against a ref last updated ${_age_desc}, so this count is a LOWER BOUND]"

if [ "$BEHIND" -ge "$BEHIND_THRESHOLD" ]; then
    echo "⚠ LOCAL-BACKEND STALENESS: this clone is ${BEHIND} commit(s) BEHIND ${UPSTREAM}.${_note}"
    echo "   Under STORAGE_BACKEND=local the git remote is the ONLY cross-machine sync point — a stale"
    echo "   clone reads every world/meta store from a stale tree and produces confident WRONG readings"
    echo "   (fleet liveness, partner state, goal queues). Run: git -C \"$PROJECT_ROOT\" pull --ff-only"
fi

if [ -n "$PEERS" ]; then
    echo "⚠ ANOTHER MACHINE HAS PUSHED: unlanded commits in the last ${PEER_WINDOW_MIN}m authored by: ${PEERS}"
    echo "   (this box is ${MY_ID}). A second box is active on this deployment right now — reconcile"
    echo "   before trusting any partner-liveness or claim-ownership conclusion drawn from this tree."
fi

# ─── Unearned silence ───────────────────────────────────────────────────────
# Both reports above speak only when they CAN SEE. A failed fetch is a third
# state — cannot tell — and rendering it as quiet is the one direction this
# check must never fail in, the same argument the PEERS awk comment makes about
# false negatives. Bounded by UNMEASURED_WARN_MIN so a transient offline moment
# stays quiet (this file's "an offline box starts clean and quiet" contract is
# about NOISE, not about concealing an unmeasurable verdict), and suppressed
# when the BEHIND warning already fired, since its _note carries the caveat.
# _last==0 (never fetched) is the MOST unmeasured state there is, so it must
# satisfy the age gate rather than fall through it on a nonsense comparison.
if [ "$FETCH_FAILED" = "1" ] && [ "$BEHIND" -lt "$BEHIND_THRESHOLD" ] \
   && { [ "$_last" -eq 0 ] || [ "$_age_min" -ge "$UNMEASURED_WARN_MIN" ]; }; then
    echo "⚠ LOCAL-BACKEND STALENESS: NOT MEASURED — could not reach '${UPSTREAM%%/*}' (fetch timed out after ${FETCH_TIMEOUT_S}s or the remote was unreachable)."
    echo "   The last SUCCESSFUL fetch was ${_age_desc}, so the quiet result above is NOT evidence this clone"
    echo "   is current — it is the absence of a measurement. Under STORAGE_BACKEND=local the git remote is the"
    echo "   ONLY cross-machine sync point, so an unmeasured clone can read every world/meta store from a stale"
    echo "   tree. Re-check: git -C \"$PROJECT_ROOT\" fetch origin"
    echo "   (Quiet by design below ${UNMEASURED_WARN_MIN}m — raise/lower via LOCAL_STALENESS_UNMEASURED_WARN_MIN.)"
fi

exit 0
