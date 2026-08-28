#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Mark an agent as in-flight on a goal in the shared team state.
# Daemon path: rt_call POST /v1/team-state/in-flight (query params).
# Usage: bash core/scripts/team-state-in-flight.sh --agent <name> --goal-id <id> --title <text> --phase <n>
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Value-arg pattern: "${2-}" + safe shift; see _runtime.sh / tree-read.sh.
#
# STRICT ARGV (). The final arm below used to be `*) shift;;`, which
# silently DISCARDED any unrecognized flag. This script is SET-ONLY -- the
# canonical clear is team-state-clear-in-flight.sh -- so
# `--agent X --goal-id Y --clear` did not clear anything: it dropped --clear and
# SET in_flight, and because --title/--phase were absent it wrote an EMPTY title
# and EMPTY phase over a row that had carried both, with a NEWER claimed_at, and
# returned rc=0. in_flight is the CROSS-AGENT claim surface, so an operation
# intended to RELEASE a claim instead REFRESHED it -- the exact opposite of
# intent, on the one store where a wrong answer causes duplicate or withheld
# work. Measured live 2026-08-19T18:27 (echo, cc-03) during the 
# close; only a read-back caught it. Same shape as the  class that
# _argv_strict.sh exists for.
#
# Sourced BEFORE _runtime.sh, as _argv_strict.sh's header prescribes, so the
# refusal stays cheap and cannot be masked by a daemon failure. The whole loop
# runs before any write, so a refusal here mutates NOTHING -- which is the
# property the regression test pins, not merely that the call failed.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"
_IF_SELF="team-state-in-flight.sh"
_IF_ACCEPTED="--agent, --goal-id, --title, --phase, --author"

AGENT=""; GOAL_ID=""; TITLE=""; PHASE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent)   AGENT="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --goal-id) GOAL_ID="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --title)   TITLE="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --phase)   PHASE="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --author)  shift $(( $# >= 2 ? 2 : 1 ));;  # handled by X-Mind-Agent header
        --clear|--clear-in-flight)
            # DISCOVERABILITY HALF (). A bare unknown-flag refusal is
            # correct but unhelpful here: the caller wanted to RELEASE a claim,
            # and a script that does exactly that already exists. Name it, so
            # the refusal ends the TASK rather than merely ending the command
            # (guard-1532 -- a refusal naming a remediation must have that
            # remediation reachable from the state the refusal observed; the
            # named command is verified against a seeded row by the regression
            # test, not merely quoted here).
            # Both spellings are caught because both were tried in sequence
            # during the measured incident: --clear-in-flight is the flag of the
            # SIBLING script (team-state-update.sh), and confusing the two is
            # what produced the wrong write.
            {
                printf "%s: '%s' is not a flag of this script -- refusing.\n" "$_IF_SELF" "$1"
                printf '  This script is SET-ONLY. This flag used to be silently discarded, so\n'
                printf '  the row was SET with an empty title/phase and a newer claimed_at --\n'
                printf '  the opposite of the intended release, with exit status 0 (g-115-6829).\n'
                printf '  To CLEAR the row, use:\n'
                printf '    bash core/scripts/team-state-clear-in-flight.sh --agent <name> [--if-goal <goal-id>]\n'
                printf '  Accepted flags here: %s\n' "$_IF_ACCEPTED"
            } >&2
            exit 2;;
        -*) argv_strict_refuse_unknown "$_IF_SELF" "$1" "$_IF_ACCEPTED";;
        *)  argv_strict_refuse_extra_positional "$_IF_SELF" "$1" 0 "$_IF_ACCEPTED";;
    esac
done

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# ── Reducer-only stamp (2026-08-03,  two-bodies; board msg-20260803-122452-alpha-5159) ──
# in_flight is keyed by AGENT NAME — one row per MIND — while bodies are per-SID.
# -d guarded the CLEAR side by claimed_by_sid ownership; this is the
# WRITE-side twin: an unconditional stamp lets a worker Body's claim clobber the
# reducer's live row (and the reducer's later verify-clear would blank the
# worker's — mutual clobber with NO self-heal: heartbeat-tick never refreshes
# in_flight, so it lasts the whole goal). Only the reducer may stamp: this box's
# running-session-id exists AND equals this session's MIND_SID. Every other
# body (cross-box worker: file absent; same-box worker: mismatch) skips loudly,
# exit 0 — a skip is not a failure, the claim must never break on it. Worker
# fleet-visibility is NOT lost: the claim's board announce still fires, and that
# is the reliable cross-box signal (guard-997); a body-keyed row is follow-up.
if [ -n "$AGENT" ]; then
    _AGENT_DIR="$(agent_dir "$AGENT")"
    _RSID_FILE="$_AGENT_DIR/session/running-session-id"
    _RSID=""
    [ -f "$_RSID_FILE" ] && _RSID="$(tr -d '[:space:]' < "$_RSID_FILE" 2>/dev/null || true)"
    if [ -z "${MIND_SID:-}" ] || [ -z "$_RSID" ] || [ "$_RSID" != "$MIND_SID" ]; then
        # ── Body-keyed visibility row () ──────────────────────────
        # The reducer row stays untouched (above) — that guarantee is the whole
        # point of -d and is NOT relaxed here. But a bare `exit 0` left
        # worker-Body goals with NO team-state row at all, which silently
        # degraded two readers that gate on in_flight: the guard-741
        # uncommitted-collision probe (goal-pickup-coordination-check) and
        # _cross_agent_attribution_filter's Source-1 work windows. Both now also
        # consume `in_flight_bodies`.
        #
        # Keyed by SID because that is the only per-INSTANCE identity available
        # (worker_close_in_flight_clear's header enumerates why: in_flight has no
        # sid, iteration-checkpoint.json is agent-wide, the Body WM records
        # nothing). One row per body, siblings never collide.
        #
        # NO SID => NO ROW. An unkeyed body row could not be cleared by its owner
        # and would strand as a permanent phantom claim — strictly worse than the
        # missing row this closes. Skip loudly, exactly as before.
        #
        # NO LOCAL AGENT DIR => NO ROW either. A real Body always runs on a box
        # where its own agent dir exists (that is where the session/ it reads
        # lives), so an absent dir means the --agent is not a resident agent —
        # a probe, a typo, or a test fixture. Writing there manufactures a
        # team-state row for an agent that does not exist, and nothing ever
        # clears it. Measured: without this gate the in_flight-guard regression
        # test (a deliberately nonexistent --agent) created a real shard in the
        # SHARED store on every suite run. Fail-safe direction — declining to
        # write is always recoverable; a phantom row is not (guard-2166).
        _NO_ROW_REASON=""
        if [ -z "${MIND_SID:-}" ]; then
            _NO_ROW_REASON="no MIND_SID to key it"
        elif [ ! -d "$_AGENT_DIR" ]; then
            _NO_ROW_REASON="no local agent dir ($_AGENT_DIR) — not a resident agent"
        fi
        if [ -n "$_NO_ROW_REASON" ]; then
            # Keep the sid=/rsid= diagnostic SHAPE of the fall-through skip
            # below — it is the operator-facing "why did this skip" contract and
            # test_team_state_in_flight_guard.py pins `sid=unset` / `rsid=absent`
            # on it. The first cut of this branch invented a new wording, which
            # read fine in isolation and broke that test (guard-695: fix the
            # message, do not re-pin a new literal in the test).
            echo "[team-state-in-flight] SKIP stamp: non-reducer body (sid=${MIND_SID:-unset}, rsid=${_RSID:-absent}) — no body row: $_NO_ROW_REASON" >&2
            exit 0
        fi
        _BODY_VALUE="$(GOAL_ID="$GOAL_ID" TITLE="$TITLE" PHASE="$PHASE" \
            $(rt_python_launcher) -c '
import json, os, datetime
print(json.dumps({
    "goal_id": os.environ.get("GOAL_ID") or None,
    "title": os.environ.get("TITLE") or "",
    "claimed_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    "phase": os.environ.get("PHASE") or None,
}, sort_keys=True))' 2>/dev/null)" || _BODY_VALUE=""
        if [ -n "$_BODY_VALUE" ]; then
            # ── Birth carrier () ─────────────────────────────────
            # WHY A ROW CAN EXIST THAT NOTHING CAN EVER REAP: this row write
            # and the carrier write gate on DIFFERENT predicates. Here the
            # gate is `-d "$_AGENT_DIR"` (checked above); heartbeat-tick.sh
            # writes the carrier only under `-d "$AGENT_DIR/$SESSIONS_DIRNAME/
            # $MIND_SID"` — the per-SID dir. So a Body whose per-SID dir is
            # absent gets a ROW and can never get a CARRIER, and
            # body_row_reaper.decide_row returns K_NO_CARRIER unconditionally
            # for a carrier-absent row (line ~212, upstream of the reap
            # branch) — unreapable AT ANY AGE, so more sweeps cannot help.
            # Measured 2026-08-22 (alpha, cc-07): two such rows live at 5.3h
            # and 3.2h, both already past DEFAULT_REAP_STALE_MINUTES (180).
            # The population REGENERATES; it is not a backlog of old rows.
            #
            # The carrier FILE lives in the agent-wide `session/` dir, never
            # under `sessions/<SID>/`, so it never needed the narrower gate.
            # Using the IDENTICAL predicate as the row is what guard-2611
            # requires of any per-principal writer — an asymmetric pair
            # re-opens the hole from the other end.
            #
            # WRITTEN BEFORE THE ROW, and the order is the point: a crash
            # between the two must land on the carrier side, because the
            # reaper iterates ROWS (an orphan carrier is inert) while an
            # orphan row is the phantom this closes.
            #
            # This weakens no KEEP and touches no DELETE path — it changes the
            # POPULATION. A Body that dies leaves a carrier that simply stops
            # being refreshed, so it goes CV_STALE and the EXISTING reap branch
            # collects it on schedule. That is what makes the bound
            # aspirations-select already documents ("a dead Body can withhold a
            # goal for at most ~3h") true for these rows; today it is false for
            # exactly this population.
            #
            # Created ONLY when absent: refreshing a live carrier is
            # heartbeat-tick's job, and clobbering its timestamp here would put
            # two writers on one signal.
            #
            # `body_state` is deliberately empty — no body-manifest exists at
            # claim time, and the reader renders an empty value
            # `stale_state_unknown`, which never alerts (fail-open, matching
            # heartbeat-tick.sh's own empty-value semantics).
            #
            # BARE `|| true`-style fallback, never `2>/dev/null` (rb-400): a
            # silenced carrier failure recreates the very invisible
            # no-carrier row this block exists to prevent.
            # mkdir -p, and it is NOT defensive boilerplate: the residency gate
            # above proves the AGENT dir exists, while this write lands in
            # `<agent>/session/` — a DIFFERENT directory. Measured 2026-08-22
            # (fresh-eyes on this very change): 2 of 11 agent dirs on this box
            # have no `session/` at all. Without the mkdir the redirect fails,
            # the `||` prints a WARN nobody reads, and the row is created with
            # NO CARRIER — reproducing, one level down, the exact
            # gate-narrower-than-the-write defect this whole block exists to
            # close. Safe under guard-2611: residency is already established,
            # so this only ever creates a resident agent's own state dir.
            _IF_STATE_DIR="$(agent_state_dir "$AGENT")"
            mkdir -p "$_IF_STATE_DIR" 2>&1 || echo "[team-state-in-flight] WARN: could not create $_IF_STATE_DIR (birth carrier will be skipped; claim unaffected)" >&2
            _IF_CARRIER="$_IF_STATE_DIR/body-heartbeat-$MIND_SID.json"
            if [ -d "$_IF_STATE_DIR" ] && [ ! -f "$_IF_CARRIER" ]; then
                printf '{"sid":"%s","agent":"%s","host":"%s","ts":"%s","body_state":"%s"}\n' \
                    "$MIND_SID" "$AGENT" "$(hostname || echo unknown)" \
                    "$(date +%Y-%m-%dT%H:%M:%S)" "" > "$_IF_CARRIER.tmp" \
                    && mv -f "$_IF_CARRIER.tmp" "$_IF_CARRIER" \
                    && echo "[team-state-in-flight] birth carrier written: body-heartbeat-${MIND_SID}.json" >&2 \
                    || echo "[team-state-in-flight] WARN: birth carrier write failed for ${AGENT}/${MIND_SID} (claim unaffected)" >&2
            fi
            # FAIL-OPEN, like the reducer stamp the claim wraps: a visibility
            # row must never fail a claim that already committed in the daemon.
            MIND_AGENT="$AGENT" bash "$CORE_ROOT/scripts/team-state-update.sh" \
                --field "agent_status.${AGENT}.in_flight_bodies.${MIND_SID}" \
                --value "$_BODY_VALUE" >/dev/null 2>&1 \
                && echo "[team-state-in-flight] body row written: ${AGENT}.in_flight_bodies.${MIND_SID} -> ${GOAL_ID}" >&2 \
                || echo "[team-state-in-flight] WARN: body row write failed for ${AGENT}/${MIND_SID} (claim unaffected)" >&2
        fi
        echo "[team-state-in-flight] SKIP stamp: non-reducer body (sid=${MIND_SID:-unset}, rsid=${_RSID:-absent}) — in_flight is reducer-owned" >&2
        exit 0
    fi
fi

QUERY=""
_append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
[ -n "$AGENT" ]   && _append_q "agent=$(rt_url_encode "$AGENT")"
[ -n "$GOAL_ID" ] && _append_q "goal_id=$(rt_url_encode "$GOAL_ID")"
[ -n "$TITLE" ]   && _append_q "title=$(rt_url_encode "$TITLE")"
[ -n "$PHASE" ]   && _append_q "phase=$(rt_url_encode "$PHASE")"

_translate() {
    # Reproduce CLI stdout: "in_flight set for {agent}: {goal_id} phase={phase}"
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print(f\"in_flight set for {resp['agent']}: {resp['goal_id']} phase={resp['phase']}\")
"
}

rc=0
RESPONSE="$(rt_call POST /v1/team-state/in-flight --query "$QUERY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/team-state/in-flight --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "team-state-in-flight.sh";;
    *) exit $rc;;
esac
