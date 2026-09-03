#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# experience-add — daemon-aware wrapper. Appends an experience record
# from stdin JSON.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Read stdin body (JSON experience record)
#   3. POST /v1/experience/add
#   4. On 200, print the record to stdout (indent=2, ensure_ascii=False)
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SCHEMA=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --schema) SCHEMA=1; shift;;
        --help|-h)
            # A stdin-body reader HANGS on --help without this branch (guard-3145).
            echo "Usage: bash $0 < record.json   — the record is JSON on STDIN; there are NO field flags." >&2
            echo "Canonical form: core/config/conventions/stdin-json-inputs.md" >&2
            exit 0;;
        *)
            # Was `*) shift;;` — silently discarded flags, then blocked forever in
            # BODY="$(cat)" wherever stdin never delivers EOF ().
            echo "Error: '$1' is not a CLI flag for this script — the record goes in the JSON body via stdin." >&2
            echo "Run: bash $0 --help" >&2
            exit 2;;
    esac
done

if [ "$SCHEMA" = "1" ]; then
    echo "Error: --schema is no longer available. See mind_api/src/endpoints/experience_write.py for the experience record contract." >&2
    exit 1
fi

# Read stdin (the JSON experience record) BEFORE invoking the daemon.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

#  (2026-09-03) — WORKER SCOPING GATE. Supersedes the blanket
#  rail that stood here, which skipped EVERY worker write and exited 0.
#
# THE INVARIANT, AND ITS STEELMAN. The convergence forbids N encoders: if every
# Body wrote shared knowledge each would be a reducer, and the stores would
# split-brain. That is why tree / reasoning-bank / guardrail / journal writes
# stay refused for a worker here and everywhere else. Do not read this gate as
# softening that; it removes ONE store from the refusal and names why.
#
# WHY AN EXPERIENCE RECORD IS NOT THAT (owner ruling, ). The record is
# the raw TRACE of what happened while executing one goal — the INPUT to
# reflection, not a learned artifact — and the store is append-only with locked
# appends. N workers each appending the trace of the goal THEY executed does not
# make N encoders; it makes one archive with N honest authors. Reflection over
# the merged result stays the reducer's.
#
# THE SCOPE IS THE ENTIRE SAFETY PROPERTY: a worker may write the record for a
# goal IT HOLDS and nothing else. Holding is checked against the LIVE claim,
# never asserted by the payload.
#
# REFUSE ON UNVERIFIABILITY, per the reducer-promotion precedent
# (core/config/conventions/reducer-promotion.md): a claim that cannot be READ is
# not a claim that may be ASSUMED. A daemon blip therefore refuses rather than
# allows — the safe direction for a shared store.
#
# RC 3, NOT 0. The old rail exited 0 because it was defensive and never meant to
# fire, so a silent no-op was tolerable. Workers now reach this writer in NORMAL
# operation, so a refusal is an ordinary outcome the caller must be able to
# DETECT — and an exit-0 skip that callers read as "landed" is a measured defect
# class ( on the journal sibling; guard-5596). Every caller invokes this
# script at the END of a pipe (`echo '<json>' | bash core/scripts/experience-add.sh`),
# so the script's rc IS the pipeline's rc and the signal survives.
#
# Predicate DERIVED LOCALLY from the body-WM file, never from BODY_ROLE
# (guard-2445 — BODY_ROLE is present in Bash-tool context and inert elsewhere).
# Skinny-resolve contract: the two path segments are inlined and mirror
# AGENTS_PARENT_DIR / SESSIONS_DIRNAME (CLAUDE.md "Agent-dir Resolution").
_APD="agents"
_SDN="sessions"
if [ -n "${MIND_SID:-}" ] && [ -n "${MIND_AGENT:-}" ] && \
   [ -f "$PROJECT_ROOT/$_APD/$MIND_AGENT/$_SDN/$MIND_SID/working-memory.yaml" ]; then

    # Resolve the record's goal the SAME way the store does. `goal_id` is
    # OPTIONAL and is routinely null on caller-formed records; the daemon
    # backfills it from an `exp-{goal-id}-{slug}` id (). Testing the
    # FIELD alone would refuse most real worker records as "unscoped" — a gate
    # that reads correctly and is inert on the population it governs (rb-9476).
    # Import the existing derivation; never re-implement it (guard-2676).
    #
    # THE COPY THIS GATE READS IS THE CORE-SIDE ONE, and it is a guard-130 TWIN of
    # mind_api/src/endpoints/experience_write.py::GOAL_ID_IN_EXP_ID_RE. The two must
    # stay literally identical, and this gate is now a REASON that matters: if the
    # daemon's copy widened and this one did not, the gate would REFUSE a record the
    # daemon would have happily attributed to its goal — a divergence that used to be
    # a silent no-op and is now a refusal ( is the precedent: the two
    # diverged for months under a comment claiming they were lifted verbatim).
    # shellcheck disable=SC2086
    _SCOPE_GOAL="$(printf '%s' "$BODY" | EXP_PROOT="$PROJECT_ROOT" $(rt_python_launcher) -c '
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["EXP_PROOT"], "core", "scripts"))
try:
    from experience import derive_goal_id_from_id
except Exception:
    def derive_goal_id_from_id(_rec_id): return None
try:
    rec = json.load(sys.stdin)
except Exception:
    rec = None
if isinstance(rec, dict):
    print(rec.get("goal_id") or derive_goal_id_from_id(rec.get("id")) or "")
' 2>/dev/null)" || _SCOPE_GOAL=""

    if [ -z "$_SCOPE_GOAL" ]; then
        echo "[experience-add] REFUSED rc=3 — BODY=worker and this record names no goal (no goal_id field, and the id does not embed one). A worker may write ONLY the experience record of a goal it holds; agent-wide and unscoped writes remain reducer-only (g-306-418)." >&2
        exit 3
    fi

    # Verify the claim from the live store. Calls the sibling wrapper rather
    # than hand-building the query (guard-2676); that wrapper is union-only over
    # world+agent by design, which is what we want — a worker may hold a goal
    # from either queue ().
    # shellcheck disable=SC2086
    _CLAIM_SID="$(bash "$CORE_ROOT/scripts/aspirations-query.sh" \
        --goal-field id "$_SCOPE_GOAL" --full 2>/dev/null | $(rt_python_launcher) -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rows = d if isinstance(d, list) else [d]
for g in rows:
    if isinstance(g, dict) and g.get("claimed_by_sid"):
        print(g["claimed_by_sid"]); break
' 2>/dev/null)" || _CLAIM_SID=""

    if [ "$_CLAIM_SID" != "$MIND_SID" ]; then
        echo "[experience-add] REFUSED rc=3 — BODY=worker: $_SCOPE_GOAL is not held by this Body (claimed_by_sid='${_CLAIM_SID:-<unreadable>}', this SID='$MIND_SID'). An unreadable claim REFUSES rather than allows: a claim that cannot be read is not one that may be assumed (g-306-418, reducer-promotion precedent)." >&2
        exit 3
    fi
    # Held. Fall through to the normal write — this Body is writing the trace of
    # its own executed goal, which is what  sanctions.
fi

_print_record() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
rec = resp.get('record') or resp
print(json.dumps(rec, indent=2, ensure_ascii=False))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/experience/add \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/experience/add \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "experience-add.sh";;
    *) exit $rc;;
esac
