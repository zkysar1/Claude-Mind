#!/usr/bin/env bash
# claim-liveness-check.sh <goal-id> — is MY claim on <goal-id> still live?
#
# Layer B automation for guard-1151 (3). Canonical incident:
# 7 ran 47 minutes past its own supersession (claim released +
# supersession posted while the agent executed) and performed a redundant
# daemon restart inside the blind window (2026-07-16, rb-3735).
#
# Verdict path: aspirations-query.sh resolves the goal's asp_id + source,
# aspirations-read.sh supplies the FULL record (query projects 6 fields and
# drops claimed_by), _claim_liveness.py classifies:
#   exit 0 — LIVE (claim intact) or INDETERMINATE (probe failed; fail-open)
#   exit 1 — STALE (status no longer in-progress, or claimed_by != me)
#   exit 2 — usage error
#
# FAIL-OPEN BY DESIGN: no MIND_AGENT, daemon unreachable, goal not found,
# or helper error all exit 0. The wired chokepoint (mind-api-start.sh
# FORCE_RESTART-on-HEALTHY-daemon branch) is the only gated path — the
# unhealthy/stale recovery restart paths never consult this script, so a
# down daemon can always be recovered.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GOAL_ID="${1:-}"
if [ -z "$GOAL_ID" ]; then
    echo "[claim-liveness] usage: claim-liveness-check.sh <goal-id>" >&2
    exit 2
fi

AGENT="${MIND_AGENT:-}"
if [ -z "$AGENT" ]; then
    echo "[claim-liveness] no MIND_AGENT bound — INDETERMINATE (fail-open)"
    exit 0
fi

# Step 1: locate the goal (asp_id + source). Query projection is enough here.
# Goal-id reaches python via env (guard-165: never interpolate bash vars
# into python source text).
meta=$(bash "$SCRIPT_DIR/aspirations-query.sh" --goal-field id "$GOAL_ID" 2>/dev/null) || meta=""
loc=$(printf '%s' "$meta" | CLC_GOAL_ID="$GOAL_ID" py -3 -c '
import json, os, sys
gid = os.environ["CLC_GOAL_ID"]
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit
goals = d.get("goals", d) if isinstance(d, dict) else d
if not isinstance(goals, list):
    raise SystemExit
g = next((x for x in goals if isinstance(x, dict)
          and (x.get("goal_id") == gid or x.get("id") == gid)), None)
if g and g.get("asp_id"):
    print(g["asp_id"], g.get("source") or "world")
' 2>/dev/null) || loc=""
if [ -z "$loc" ]; then
    echo "[claim-liveness] goal $GOAL_ID not resolvable via query — INDETERMINATE (fail-open)"
    exit 0
fi
read -r ASP_ID SRC <<<"$loc"

# Step 2: full record (claimed_by lives here) -> pure verdict helper.
verdict=$(bash "$SCRIPT_DIR/aspirations-read.sh" --source "${SRC:-world}" --id "$ASP_ID" 2>/dev/null \
    | py -3 "$SCRIPT_DIR/_claim_liveness.py" --agent "$AGENT" --goal-id "$GOAL_ID" 2>/dev/null) \
    || verdict="INDETERMINATE: helper error (fail-open)"

echo "[claim-liveness] $GOAL_ID ($AGENT): $verdict"
case "$verdict" in
    STALE:*) exit 1 ;;
    *)       exit 0 ;;
esac
