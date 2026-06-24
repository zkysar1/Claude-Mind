#!/usr/bin/env bash
# Theory-of-Mind contradiction-triggered forced reflection ().
#
# The CONSUME-side completion of the partner-belief loop ( storage ->
#  write+consume ->  contradiction-revision). Once per precheck
# iteration (Phase 0-pre.0, right after the live partner snapshot) this compares
# every partner's freshly OBSERVED focus (agent_status.<partner>.current_focus)
# against the domain-belief THIS agent holds about that partner. On N CONSECUTIVE
# contradicting observations (default 2 -> no false-trigger on the FIRST), it
# REVISES the held belief (lowers its confidence) and records the surprise.
#
# Orchestrator only — composes daemon read + pure compute (_belief_contradiction.py)
# + conditional daemon write, identical to team-belief-write.sh. The belief
# revised is the calling agent's OWN sublist (agent_status.<self>.beliefs), of
# which it is the sole writer, so the read-modify-write is race-free at the field
# level under the shared team-state lock (see _team_belief.py docstring).
#
# FAIL-OPEN by contract: this runs in the precheck hot path and MUST NEVER block
# the loop. `set -uo pipefail` (NOT -e); every daemon call is guarded; the script
# always exits 0. A detector bug degrades to "no revision this iteration", never
# a stalled precheck.
set -uo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_SELF_DIR/../.." && pwd)"

# _paths.sh puts the .python-shim on PATH so `python3` resolves inside this .sh
# (canonical "python3 only inside a .sh that sources _paths.sh" pattern).
# shellcheck disable=SC1091
source "$PROJECT_ROOT/core/scripts/_paths.sh" 2>/dev/null || true

SELF="${MIND_AGENT:-}"
if [ -z "$SELF" ]; then
    echo "belief-contradiction-check: MIND_AGENT unset — skipping (fail-open)." >&2
    exit 0
fi

# Tunables (env-overridable; defaults match _belief_contradiction.py).
N_REQUIRED="${BELIEF_CONTRADICTION_N:-2}"
MODE="${BELIEF_CONTRADICTION_MODE:-lower}"   # lower | supersede

# 1. READ the live team-state (all partners' current_focus + self.beliefs).
TS="$(bash "$PROJECT_ROOT/core/scripts/team-state-read.sh" --json 2>/dev/null || echo '{}')"
[ -z "$TS" ] && TS='{}'

# 2. READ the per-partner consecutive-contradiction streaks (agent-private WM).
PREV="$(bash "$PROJECT_ROOT/core/scripts/wm-read.sh" belief_contradiction_streaks --json 2>/dev/null || echo '{}')"
[ -z "$PREV" ] || [ "$PREV" = "null" ] && PREV='{}'

# 3. COMPUTE (pure; team-state via stdin, params via argv -> guard-165 safe).
NOW="$(date +%Y-%m-%dT%H:%M:%S)"
RESULT="$(printf '%s' "$TS" | python3 "$PROJECT_ROOT/core/scripts/_belief_contradiction.py" \
            --self "$SELF" --prev-streaks "$PREV" --now "$NOW" \
            --n-required "$N_REQUIRED" --mode "$MODE" 2>/dev/null || echo '{}')"
[ -z "$RESULT" ] && RESULT='{}'

# 4. EXTRACT pieces. Single-quoted python source reads RESULT from stdin — no
#    bash var is interpolated into the source (guard-165 safe).
REVISED="$(printf '%s' "$RESULT" | python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: d={}
rb=d.get("revised_beliefs")
print(json.dumps(rb) if rb is not None else "")' 2>/dev/null || echo '')"

NEW_STREAKS="$(printf '%s' "$RESULT" | python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: d={}
print(json.dumps(d.get("new_streaks", {})))' 2>/dev/null || echo '{}')"
[ -z "$NEW_STREAKS" ] && NEW_STREAKS='{}'

# 5. WRITE revised beliefs back (only when a revision actually fired).
if [ -n "$REVISED" ]; then
    bash "$PROJECT_ROOT/core/scripts/team-state-update.sh" \
        --field "agent_status.${SELF}.beliefs" --operation set --value "$REVISED" \
        >/dev/null 2>&1 || echo "belief-contradiction-check: belief write failed (fail-open)" >&2
    # Record the surprise durably in the metacognitive event log (the forced
    # reflection fired). Belief annotations (prior_domain/prior_confidence/
    # revised_at) carry the per-belief surprise; this is the session-level trace.
    printf '%s' "$RESULT" | PR="$PROJECT_ROOT" NOW="$NOW" python3 -c 'import sys,json,subprocess,os
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
root=os.environ.get("PR",".")
now=os.environ.get("NOW","")
for e in d.get("events", []):
    if not e.get("should_revise"): continue
    rec={"date": now[:10], "event": "belief_contradiction_revision",
         "details": "forced reflection: revised belief about %s (held=%s observed=%s mode=%s, g-306-29)"
                    % (e.get("partner"), e.get("held_domain"), e.get("observed_domain"), e.get("revised_mode"))}
    try:
        subprocess.run(["bash", os.path.join(root,"core","scripts","evolution-log-append.sh")],
                       input=json.dumps(rec), text=True, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass' 2>/dev/null || true
fi

# 6. PERSIST the updated streaks (always — clears stale streaks too).
printf '%s' "$NEW_STREAKS" | bash "$PROJECT_ROOT/core/scripts/wm-set.sh" \
    belief_contradiction_streaks >/dev/null 2>&1 || true

# 7. SUMMARY line for the precheck surface (one line; revision detail if any).
printf '%s' "$RESULT" | PR="$PROJECT_ROOT" NOW="$NOW" python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: d={}
evs=d.get("events", [])
contra=sum(1 for e in evs if e.get("status")=="contradiction")
revs=[e for e in evs if e.get("should_revise")]
if revs or contra:
    print("belief-contradiction: %d contradiction(s), %d revision(s)" % (contra, len(revs)))
    for e in revs:
        print("  >> REVISED belief about %s: held %s but observed %s (mode=%s) -- confidence lowered/superseded"
              % (e.get("partner"), e.get("held_domain"), e.get("observed_domain"), e.get("revised_mode")))
else:
    print("belief-contradiction: clean (no domain-belief contradictions)")' 2>/dev/null || \
    echo "belief-contradiction: check ran (summary unavailable)"

exit 0
