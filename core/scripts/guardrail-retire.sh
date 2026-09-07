#!/usr/bin/env bash
# guardrail-retire.sh - D1 guardrail cluster retirement wrapper ().
#
# Thin companion over guardrail_retire.py (the engine). `scan`/`cluster` forward
# to the engine (read-only); `apply`/`restore` execute the engine's MUTATION PLAN
# via guardrails-update-field.sh. world/guardrails.jsonl is own-cloud append-JSONL
# (guard-832) - the engine NEVER writes it directly; the status flip + stamp go
# through the daemon wrapper. The engine is invoked as a TERMINAL python process
# (py -3 <file>); the plan is parsed by a TERMINAL `py -3 -c`; the mutations are
# executed by a BASH loop calling guardrails-update-field.sh - so there is no
# Python->bash->python hop (rb-225/rb-247 hang is avoided).
#
# Usage:
#   guardrail-retire.sh scan [--scope-category C] [--today YYYY-MM-DD]
#   guardrail-retire.sh cluster <guard-id>
#   guardrail-retire.sh apply <guard-id> <keep|refresh|retire|revise> [--force] [--reason R] [--today D]
#   guardrail-retire.sh restore <guard-id>
set -euo pipefail

_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_SELF/../.." && pwd)"
export PROJECT_ROOT
ENGINE="$PROJECT_ROOT/core/scripts/guardrail_retire.py"
UPD="$PROJECT_ROOT/core/scripts/guardrails-update-field.sh"

# Source _paths.sh for env (the engine resolves WORLD_DIR via `from _paths import
# WORLD_DIR`; sourcing keeps the python shim on PATH per python-invocation.md).
# shellcheck disable=SC1091
source "$PROJECT_ROOT/core/scripts/_paths.sh" 2>/dev/null || true

# Portable terminal-python runner: py -3 (Windows launcher, reliable) then
# python3 then python. Never `-c`-spawns bash, so no rb-225/rb-247 hop.
if command -v py >/dev/null 2>&1; then _PY="py -3"
elif command -v python3 >/dev/null 2>&1; then _PY="python3"
else _PY="python"; fi

CMD="${1:-}"
[ -n "$CMD" ] || { echo "Usage: guardrail-retire.sh {scan|cluster|apply|restore} ..." >&2; exit 1; }
shift || true

case "$CMD" in
    scan|cluster)
        $_PY "$ENGINE" "$CMD" "$@"
        ;;
    apply|restore)
        # 1. Compute the mutation plan (read-only).
        rc=0
        PLAN="$($_PY "$ENGINE" "$CMD" "$@")" || rc=$?
        echo "$PLAN"
        if [ "$rc" != "0" ]; then exit "$rc"; fi
        # 2. Execute the plan's mutations in BASH (py parses, bash executes the
        #    daemon wrapper). Tab-separated id<TAB>field<TAB>value lines.
        MUTS="$(printf '%s' "$PLAN" | $_PY -c "
import json, sys
plan = json.load(sys.stdin)
if not plan.get('ok'):
    sys.exit(0)
for m in plan.get('mutations', []):
    print('%s\t%s\t%s' % (m['id'], m['field'], m['value']))
")"
        exec_rc=0
        if [ -n "$MUTS" ]; then
            while IFS=$'\t' read -r mid mfield mvalue; do
                [ -n "$mid" ] || continue
                if bash "$UPD" "$mid" "$mfield" "$mvalue" >/dev/null 2>&1; then
                    echo "  mutate ${mid}.${mfield}=${mvalue} -> ok"
                else
                    echo "  mutate ${mid}.${mfield}=${mvalue} -> FAILED" >&2
                    exec_rc=1
                fi
            done <<< "$MUTS"
        fi
        # 3. TRUTH-EVENT CAPTURE for the confidence-calibration ledger — the
        #    SECOND surface required by  outcome 2, after
        #    adjudication-lane.py::cmd_resolve. A scoped CALL into the shared
        #    recorder (`_confidence_ledger.record_truth_event`), never a second
        #    ledger writer (guard-2676).
        #
        #    WHY HERE AND NOT IN THE ENGINE, which is where it looks like it
        #    belongs: `apply()` computes a mutation PLAN and returns it — it
        #    never writes (guard-832, and its own docstring). A recorder placed
        #    there would log verdicts that were merely COMPUTED, including plans
        #    this wrapper then failed to execute, manufacturing exactly the
        #    fiction this ledger exists to measure. That is guard-4238's adjacent
        #    trap: a recorder ahead of the mutator it depends on. So capture sits
        #    AFTER the mutation loop and requires exec_rc=0.
        #
        #    APPLY ONLY, never `restore`: un-retiring is an UNDO of a prior
        #    verdict, not a fresh judgement about the entry's claim.
        #
        #    THE `retire`-WITHOUT-`--reason` EXCLUSION IS THE GOAL'S, NOT A
        #    PREFERENCE:  excludes utilization-only retirements
        #    ("popularity is not truth"), and this lane's retire verdict is
        #    driven by staleness + `effective_relevance` scoring. A retire
        #    carrying an explicit --reason is a CONTENT judgement and is kept;
        #    a bare one is the utilization-only shape and is dropped.
        #    keep/refresh/revise are always content judgements about a live
        #    entry, so they are always recorded — and they are the SURVIVED rows
        #    the calibration table needs, without which it has no denominator.
        if [ "$CMD" = "apply" ] && [ "$exec_rc" = "0" ] && [ -n "$MUTS" ]; then
            _gr_id="${1:-}"; _gr_verdict="${2:-}"; _gr_reason=""; _gr_prev=""
            for _gr_a in "$@"; do
                [ "$_gr_prev" = "--reason" ] && _gr_reason="$_gr_a"
                _gr_prev="$_gr_a"
            done
            # Values travel by ENV, never interpolated into the python source:
            # a --reason carrying a quote or a $( would otherwise break or
            # execute (guard-165). Terminal `py -3 -c` like every other python
            # call in this file, so no python->bash->python hop (rb-225/rb-247).
            MIND_GR_ID="$_gr_id" \
            MIND_GR_VERDICT="$_gr_verdict" \
            MIND_GR_REASON="$_gr_reason" \
            $_PY -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ['PROJECT_ROOT'], 'core', 'scripts'))
gid = os.environ.get('MIND_GR_ID') or ''
verdict = os.environ.get('MIND_GR_VERDICT') or ''
reason = os.environ.get('MIND_GR_REASON') or ''
try:
    from guardrail_retire import truth_event_for
    from _confidence_ledger import record_truth_event
except Exception:
    sys.exit(0)
ev = truth_event_for(verdict, reason)
if ev is None or not gid:
    sys.exit(0)
mapped, evidence_ref = ev
record_truth_event(gid, 'guardrails', mapped,
                   source='guardrail-retire',
                   evidence_ref=evidence_ref,
                   extra={'verdict_raw': verdict,
                          'reason_given': evidence_ref is not None})
" || true
        fi
        exit "$exec_rc"
        ;;
    *)
        echo "Unknown command: $CMD (expected scan|cluster|apply|restore)" >&2
        exit 1
        ;;
esac
