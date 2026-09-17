#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-15 (H2 Wave 2). No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#   zeta/reports/phase3-h2-wave-plan.md (generic store endpoint)
# guardrails-increment — daemon-aware wrapper. Atomic counter increment +
# utilization_score recompute on a guardrails record.
#
# Usage:  guardrails-increment.sh <id> <field>
#   e.g.  guardrails-increment.sh guard-001 utilization.times_helpful
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
REC_ID=""
FIELD=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -*) shift;;
        *)
            if [ -z "$REC_ID" ]; then REC_ID="$1"
            elif [ -z "$FIELD" ]; then FIELD="$1"
            fi
            shift;;
    esac
done

if [ -z "$REC_ID" ] || [ -z "$FIELD" ]; then
    echo "Usage: guardrails-increment.sh <id> <field>" >&2
    exit 1
fi

# : REFUSE A CROSS-STORE ID BEFORE IT SPOOLS. The spool lane
# deliberately does not resolve the id against the content store — that read is
# the ~51 GB/day cost  removed — so nothing downstream notices an rb-*
# id handed to this wrapper, or a guard-* id handed to its sibling. The delta
# spools, flushes, and lands as a permanent row in the WRONG sidecar, where
# utilization_of() for that record never reads it. MEASURED 2026-09-17 (echo,
# cc-03): 8 such rows already exist — 7 guard-* in reasoning-bank-utilization
# .jsonl and 1 rb-* here. guard-514 is the cost: one genuine times_helpful
# credit stranded in the other store, so it reads times_helpful=0 — the exact
# signal the retirement sweeps use to propose deleting an entry.
# A PREFIX check is the whole fix and costs one string compare: 6,549/6,549
# guardrail ids start with `guard-` and 10,634/10,634 rb ids with `rb-`
# (measured, zero exceptions), so no ordinary increment is refused. It
# deliberately does NOT check that the id EXISTS — that needs the content read
# this lane exists to avoid, and a bogus SAME-store id is inert because nothing
# ever looks it up (guard-5619).
# ONE LEGITIMATE CALL *IS* REFUSED, and this comment claimed none was until a
# fresh-eyes pass measured it (2026-09-17, echo, cc-03): utilization-correct.sh
# picks its wrapper by STORE and passes the record id through unchecked
# (_utilization_correct.py INCREMENT_WRAPPER + run_correction), so ZEROING an
# already-misrouted row is `--store reasoning-bank --id guard-514`, which lands
# in the SIBLING wrapper and is refused (measured rc=1). Scope that precisely
# before acting on it: the CREDIT-RESTORATION path is open — `guardrails-
# increment.sh guard-514 utilization.times_helpful` passes this check and is
# what actually recovers the stranded credit — so only the cosmetic cleanup of
# the 8 phantom rows is closed, and those rows are inert for reading anyway
# (utilization_of never consults the other store's sidecar). Whoever decides to
# clean them must change this check, not route around it. guard-3164 is the
# general form: before adding a refusal, trace what the newly-failing call
# returns to its CALLER, not only which call sites will newly error.
case "$REC_ID" in
    guard-*) ;;
    *)  printf '{"error": "wrong_store_id", "detail": "guardrails-increment.sh takes a guard-* id, got: %s — a cross-store id spools to the wrong sidecar and its credit becomes unreadable (g-115-6903)."}\n' "$REC_ID" >&2
        exit 1;;
esac

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_print_record() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
rec = resp.get('record') or resp
# : SURFACE THE SPOOL LANE. On a spooled increment () the
# daemon returns {'ok':true,'spooled':true,'record':{'id':...}} and that record
# is a bare id BY DESIGN — the spool path deliberately does not read the content
# store. Printing only the record made a working write indistinguishable from a
# no-op: the caller sees {'id':'guard-953'}, re-reads the CONTENT store, finds
# the embedded utilization block unchanged (frozen by design post-cutover) and
# concludes the counter was lost. It was not; it lands in the sidecar at flush.
# Two agents reached that wrong conclusion before this line existed.
if resp.get('spooled'):
    rec = dict(rec)
    rec['spooled'] = True
    rec['where'] = ('appended to this box local spool; lands in the '
                    '<kind>-utilization.jsonl SIDECAR at flush, NOT in the '
                    'content record. Read counters via utilization_of(), never '
                    'from the record embedded utilization block.')
print(json.dumps(rec, indent=2, ensure_ascii=False))
"
}

QUERY="store=guardrails&id=$(rt_url_encode "$REC_ID")&field=$(rt_url_encode "$FIELD")"

rc=0
RESPONSE="$(rt_call POST /v1/store/increment \
    --query "$QUERY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-15 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/store/increment \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "guardrails-increment.sh";;
    *) exit $rc;;
esac
