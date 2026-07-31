#!/usr/bin/env bash
# skill-quality-staleness-check.sh — surfaces when meta/skill-quality.yaml
# stops getting fresh entries. Filed as part of the skill-telemetry signal
# repair master plan (Layer 3 — eval pipeline guard).
#
# History: skill-quality.yaml went silent from 2026-04-16 to 2026-05-12
# (~25 days) because the Step 8.76 sampling gate excluded routine-class
# goals and goal.skill-null goals — the post-April workload shifted into
# both buckets. This script catches the next such silence within 7 days
# instead of 25.
#
# Behavior:
#   --check (default)  exit 0 if fresh, exit 2 if stale, exit 3 if missing
#   --file-goal        on stale: file an Investigate goal via
#                      aspirations-add-goal.sh (so the next iteration probes
#                      it) and exit 2
#   --json             machine-readable output
#
# Threshold: 7 days. Override via env STALENESS_DAYS or --days N.
#
# Knowledge tree: world/knowledge/tree/system/system-constraints-loop/skill-telemetry-signal-master-plan.md
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

DAYS="${STALENESS_DAYS:-7}"
MODE="check"
JSON=0

while [ $# -gt 0 ]; do
    case "$1" in
        --check) MODE="check"; shift ;;
        --file-goal) MODE="file-goal"; shift ;;
        --json) JSON=1; shift ;;
        --days) DAYS="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,18p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ -z "${META_DIR:-}" ]; then
    echo "META_DIR not resolvable; cannot check staleness" >&2
    exit 3
fi

QUALITY_FILE="$META_DIR/skill-quality.yaml"

# Single Python pass: read last_updated, compute age, emit verdict.
DAYS="$DAYS" JSON="$JSON" MODE="$MODE" QUALITY_FILE="$QUALITY_FILE" \
    python3 - <<'PY' || rc=$?
import json, os, sys, datetime
try:
    import yaml
except ImportError:
    print(json.dumps({"verdict":"missing","reason":"PyYAML not available"})); sys.exit(3)

qpath = os.environ["QUALITY_FILE"]
days = int(os.environ.get("DAYS","7"))
emit_json = os.environ.get("JSON","0") == "1"
mode = os.environ.get("MODE","check")

if not os.path.exists(qpath):
    out = {"verdict":"missing","reason":f"file not found: {qpath}","threshold_days":days}
    print(json.dumps(out) if emit_json else f"MISSING: {qpath}")
    sys.exit(3)

try:
    with open(qpath, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
except Exception as e:
    out = {"verdict":"missing","reason":f"parse error: {e}","threshold_days":days}
    print(json.dumps(out) if emit_json else f"MISSING: parse error {e}")
    sys.exit(3)

last_raw = d.get("last_updated")
if not last_raw:
    out = {"verdict":"missing","reason":"no last_updated field","threshold_days":days}
    print(json.dumps(out) if emit_json else "MISSING: no last_updated")
    sys.exit(3)

last_str = str(last_raw)
# Accept both ISO and ISO with T
try:
    if "T" in last_str:
        # : strip tzinfo (not just Z) so `now - last_dt` below never
        # hits the aware/naive TypeError on an offset-bearing last_updated.
        _ls = last_str.replace("Z", "+00:00")
        last_dt = datetime.datetime.fromisoformat(_ls)
        if last_dt.tzinfo is not None:
            last_dt = last_dt.astimezone().replace(tzinfo=None)
    else:
        last_dt = datetime.datetime.fromisoformat(last_str)
except ValueError:
    out = {"verdict":"missing","reason":f"invalid last_updated: {last_str}","threshold_days":days}
    print(json.dumps(out) if emit_json else f"MISSING: invalid date {last_str}")
    sys.exit(3)

now = datetime.datetime.now()
age_seconds = (now - last_dt).total_seconds()
age_days = age_seconds / 86400.0

# Counts (informational): how many skills, how many evals
skills = d.get("skills") or {}
total_skills = len(skills) if isinstance(skills, dict) else 0
total_evals = 0
if isinstance(skills, dict):
    for v in skills.values():
        if isinstance(v, dict):
            total_evals += int(v.get("total_evaluations") or 0)

if age_days <= days:
    out = {"verdict":"fresh","age_days":round(age_days,2),"threshold_days":days,
           "last_updated":last_str,"total_skills":total_skills,"total_evaluations":total_evals}
    if emit_json: print(json.dumps(out))
    else: print(f"FRESH: skill-quality.yaml updated {round(age_days,2)}d ago (threshold {days}d)")
    sys.exit(0)

# Stale
out = {"verdict":"stale","age_days":round(age_days,2),"threshold_days":days,
       "last_updated":last_str,"total_skills":total_skills,"total_evaluations":total_evals,
       "mode":mode}
if emit_json: print(json.dumps(out))
else:
    print(f"STALE: skill-quality.yaml last_updated {last_str} = {round(age_days,2)}d ago "
          f"(threshold {days}d). Skills tracked: {total_skills}; total evals: {total_evals}.")
    print("Likely cause: aspirations-state-update Step 8.76 sampling gate is excluding")
    print("the current workload. Check that goals carry the 'skill' field and that the")
    print("'outcome_class != routine' guard fires reasonably often.")
sys.exit(2)
PY

# Surface the rc; if --file-goal AND stale, file an Investigate goal.
rc="${rc:-0}"
if [ "$MODE" = "file-goal" ] && [ "$rc" = "2" ]; then
    title="Investigate: skill-quality.yaml staleness (>${DAYS}d)"
    desc="Auto-filed by skill-quality-staleness-check.sh. meta/skill-quality.yaml has not been updated in more than ${DAYS} days. Likely cause: Step 8.76 sampling gate excluding current workload (routine outcomes or goal.skill=null). Reference: world/knowledge/tree/system/system-constraints-loop/skill-telemetry-signal-master-plan.md"
    if [ -x "$CORE_ROOT/scripts/aspirations-add-goal.sh" ]; then
        # Goal fields go in the JSON BODY on stdin. --title/--description/
        # --priority are hard-rejected CLI flags (exit 2, aspirations-add-goal.sh
        # ~L98) -- this branch passed all three, so it could never file anything,
        # and `2>&1 | tail -3 || true` swallowed the rejection. Dead from the day
        # it was written; found by the  sweep, never masked by a
        # successful filing (zero goals carry this title, any status).
        # Values reach python via ENV, single-quoted source (guard-165).
        _sq_payload="$(SQ_TITLE="$title" SQ_DESC="$desc" py -3 -c '
import json, os
print(json.dumps({
    "title": os.environ["SQ_TITLE"],
    "priority": "MEDIUM",
    "participants": ["agent"],
    "category": "framework-architecture",
    "origin_signal": "investigate:skill-quality-staleness",
    "description": os.environ["SQ_DESC"],
}))
')"
        if [ -z "$_sq_payload" ]; then
            echo "WARN: skill-quality staleness payload build failed; goal not filed" >&2
        else
            # : resolve per deployment — a literal  is the
            # UPSTREAM queue and files nothing downstream (aspiration_not_found).
            _sq_et="$(bash "$CORE_ROOT/scripts/escalation-target.sh")" || _sq_et="asp-115 world"
            _sq_err="$(printf '%s' "$_sq_payload" \
                | bash "$CORE_ROOT/scripts/aspirations-add-goal.sh" \
                      --source "${_sq_et##* }" --aspiration "${_sq_et%% *}" 2>&1 >/dev/null)" \
                || echo "WARN: skill-quality staleness goal-file failed (non-fatal): ${_sq_err}" >&2
        fi
    else
        echo "WARN: aspirations-add-goal.sh not available; goal not filed" >&2
    fi
fi
exit "$rc"
