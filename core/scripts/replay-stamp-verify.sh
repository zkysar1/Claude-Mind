#!/usr/bin/env bash
# replay-stamp-verify.sh — stamp replay_metadata on N hypothesis records and
# verify EVERY stamp with a PER-ID read-back.
#
# Forged from gap-145 (times_encountered 2, type utility) after this procedure
# was hand-rolled for the third recorded time (zeta, , 2026-08-18).
#
# ── THE CONTRACT THAT IS THE WHOLE POINT (guard-1755) ────────────────────────
# The verification read is PER-ID (`pipeline-read.sh --id <rec-id>`), NEVER the
# batched `--replay-candidates` surface. This is not a style preference; a
# batched read-back is GUARANTEED WRONG here, and wrong in the worst direction.
#
# Verified by direct read of mind_api/src/world/pipeline.py:366-376 — the
# replay_candidates filter does:
#       review_date = date.fromisoformat(str(next_review)[:10])
#       if review_date > today: continue
# This operation always sets next_review_date to today+INTERVAL (default +7),
# so after a FULLY SUCCESSFUL stamp every record it just wrote is EXCLUDED from
# that surface. A batched read-back therefore reports ZERO VERIFIED on total
# success — an INVERTED signal, not a lossy one. The repair it invites is a
# re-stamp, which double-increments replay_count on healthy records and pushes
# them toward the rc>=5 archive cap early (pipeline.py:358-364).
#
# ── SHELL POSTURE ────────────────────────────────────────────────────────────
# `set -uo pipefail`, deliberately NOT `set -e` (guard-614): this wrapper emits
# structured JSON on EVERY exit path including failure, and `set -e` would kill
# it mid-report on the first non-zero daemon call — turning a per-record failure
# list into silence. Per-record failures are DATA here, not aborts: guard-2400
# notes a store that validates the whole record on write can reject a record
# whose PRE-EXISTING field violates a validator added later, and one such record
# must not cost the other N-1 their stamp.
# stderr is never suppressed on the critical path (guard-114).
#
# Usage:
#   replay-stamp-verify.sh <rec-id> [<rec-id> ...]
#   replay-stamp-verify.sh -            # ids on stdin, one per line
#   replay-stamp-verify.sh --dry-run <rec-id> ...
#   replay-stamp-verify.sh --interval-days 14 <rec-id> ...
#
# Exit: 0 iff every requested id was stamped AND its per-id read-back matched
#       all three written values. Non-zero otherwise (count of failures, capped
#       at 125 so it stays a valid shell exit status).

set -uo pipefail

_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_SELF/../.." && pwd)"
# shellcheck disable=SC1091
source "$PROJECT_ROOT/core/scripts/_paths.sh"

INTERVAL_DAYS=7
DRY_RUN=0
declare -a IDS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            sed -n '2,40p' "$0" >&2
            printf '{"ok":true,"action":"help"}\n'
            exit 0
            ;;
        --dry-run)        DRY_RUN=1; shift ;;
        --interval-days)
            # F-001 (fresh-eyes, measured rc=124): a bare `shift 2` when only this
            # flag remains cannot shift, $# never decrements, and the while-loop
            # spins forever emitting NOTHING — the worst failure shape for a
            # wrapper whose contract is "report on every exit path".
            if [[ $# -lt 2 ]]; then
                printf '{"ok":false,"error":"missing_value","flag":"--interval-days","detail":"requires a non-negative integer"}\n' >&2
                exit 2
            fi
            INTERVAL_DAYS="$2"; shift 2 ;;
        -)                while IFS= read -r _l; do [[ -n "$_l" ]] && IDS+=("$_l"); done; shift ;;
        -*)
            printf '{"ok":false,"error":"unknown_flag","flag":"%s","accepted":"--dry-run | --interval-days N | - (ids on stdin)"}\n' "$1" >&2
            exit 2
            ;;
        *)                IDS+=("$1"); shift ;;
    esac
done

if [[ ${#IDS[@]} -eq 0 ]]; then
    printf '{"ok":false,"error":"no_ids","detail":"pass record ids as positionals or `-` to read them from stdin"}\n' >&2
    exit 2
fi

# F-002 (fresh-eyes, measured): without this, `--interval-days abc` made the
# python below raise, left NEXT_REVIEW empty, and the script wrote
# next_review_date:"" onto real records AT rc=0. An empty next_review_date is
# falsy in the replay_candidates filter (`if next_review:`), so the record is
# never excluded and resurfaces as a candidate every cycle — precisely the
# corruption this wrapper exists to prevent, self-inflicted.
if ! [[ "$INTERVAL_DAYS" =~ ^[0-9]+$ ]]; then
    printf '{"ok":false,"error":"bad_interval","value":"%s","detail":"--interval-days requires a non-negative integer"}\n' "$INTERVAL_DAYS" >&2
    exit 2
fi

TODAY="$(date +%Y-%m-%d)"
NEXT_REVIEW="$(python3 -c "import datetime,sys; print((datetime.date.today()+datetime.timedelta(days=int(sys.argv[1]))).isoformat())" "$INTERVAL_DAYS")"

if [[ -z "$TODAY" || -z "$NEXT_REVIEW" ]]; then
    printf '{"ok":false,"error":"date_compute_failed","today":"%s","next_review_date":"%s"}\n' "$TODAY" "$NEXT_REVIEW" >&2
    exit 2
fi

declare -a RESULTS=()
STAMPED=0
VERIFIED=0
FAILED=0

for rec_id in "${IDS[@]}"; do
    # 1. READ the current record per-id (never from a batched surface).
    cur="$(bash "$PROJECT_ROOT/core/scripts/pipeline-read.sh" --id "$rec_id")"
    read_rc=$?
    if [[ $read_rc -ne 0 || -z "$cur" ]]; then
        RESULTS+=("$(printf '{"id":%s,"phase":"pre-read","ok":false,"detail":"pipeline-read --id failed rc=%s bytes=%s"}' \
                     "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$rec_id")" "$read_rc" "${#cur}")")
        FAILED=$((FAILED+1))
        continue
    fi

    # 2. COMPUTE the new replay_metadata (pure stdin->stdout filter; no subprocess
    #    spawning from python — rb-225/rb-247, guard-580).
    new_meta="$(printf '%s' "$cur" | python3 -c '
import json,sys
rec=json.load(sys.stdin)
m=dict(rec.get("replay_metadata") or {})
try: rc=int(m.get("replay_count") or 0)
except (TypeError,ValueError): rc=0          # unparseable -> restart the ladder, never crash
m["replay_count"]=rc+1
m["last_replayed"]=sys.argv[1]
m["next_review_date"]=sys.argv[2]
print(json.dumps(m,sort_keys=True))
' "$TODAY" "$NEXT_REVIEW")"
    if [[ -z "$new_meta" ]]; then
        RESULTS+=("$(printf '{"id":%s,"phase":"compute","ok":false,"detail":"could not build replay_metadata from the record"}' \
                     "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$rec_id")")")
        FAILED=$((FAILED+1))
        continue
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        RESULTS+=("$(printf '{"id":%s,"phase":"dry-run","ok":true,"would_write":%s}' \
                     "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$rec_id")" "$new_meta")")
        continue
    fi

    # 3. WRITE the whole object. update-field REJECTS dotted field names
    #    (pipeline_write.py:934-938 dotted_field_rejected), so replay_metadata
    #    is written whole — which is why step 2 is a read-modify-write.
    if ! bash "$PROJECT_ROOT/core/scripts/pipeline-update-field.sh" "$rec_id" replay_metadata "$new_meta" >/dev/null; then
        RESULTS+=("$(printf '{"id":%s,"phase":"write","ok":false,"detail":"pipeline-update-field returned non-zero"}' \
                     "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$rec_id")")")
        FAILED=$((FAILED+1))
        continue
    fi
    STAMPED=$((STAMPED+1))

    # 4. VERIFY by re-reading THIS ID ALONE (guard-1755, guard-1720). A write
    #    that returned 0 is not evidence the value is on the record.
    back="$(bash "$PROJECT_ROOT/core/scripts/pipeline-read.sh" --id "$rec_id")"
    verdict="$(printf '%s' "$back" | python3 -c '
import json,sys
try: rec=json.load(sys.stdin)
except Exception as e:
    print(json.dumps({"ok":False,"detail":"read-back did not parse: %s" % e})); raise SystemExit
want=json.loads(sys.argv[1]); got=rec.get("replay_metadata") or {}
bad={k:{"want":v,"got":got.get(k)} for k,v in want.items() if got.get(k)!=v}
print(json.dumps({"ok":not bad,"mismatches":bad}))
' "$new_meta")"
    if printf '%s' "$verdict" | grep -q '"ok": *true'; then
        VERIFIED=$((VERIFIED+1))
        RESULTS+=("$(printf '{"id":%s,"phase":"verify","ok":true,"stamp":%s}' \
                     "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$rec_id")" "$new_meta")")
    else
        FAILED=$((FAILED+1))
        RESULTS+=("$(printf '{"id":%s,"phase":"verify","ok":false,"verdict":%s}' \
                     "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$rec_id")" "${verdict:-null}")")
    fi
done

# F-003 (fresh-eyes): each record is emitted on its OWN LINE and parsed as
# NDJSON. The previous regex re-parser (`\{.*?\}(?=\s*\{|\s*$)`) worked on
# every observed shape but is fragile against any string value containing
# "} {" — and there is no reason to re-derive object boundaries that the
# producer already knows.
printf '%s\n' "${RESULTS[@]:-}" | python3 -c '
import json,sys
objs=[]
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    try: objs.append(json.loads(line))
    except json.JSONDecodeError: objs.append({"ok":False,"phase":"summary","detail":"unparseable record line","raw":line[:300]})
summary=json.loads(sys.argv[1]); summary["records"]=objs
print(json.dumps(summary,indent=2))
' "$(printf '{"ok":%s,"requested":%s,"stamped":%s,"verified":%s,"failed":%s,"dry_run":%s,"last_replayed":"%s","next_review_date":"%s","verification":"per-id (guard-1755) — NEVER --replay-candidates, which excludes future next_review_date and would report zero on success"}' \
        "$([[ $FAILED -eq 0 ]] && echo true || echo false)" "${#IDS[@]}" "$STAMPED" "$VERIFIED" "$FAILED" \
        "$([[ $DRY_RUN -eq 1 ]] && echo true || echo false)" "$TODAY" "$NEXT_REVIEW")"

[[ $FAILED -eq 0 ]] && exit 0
[[ $FAILED -gt 125 ]] && exit 125
exit "$FAILED"
