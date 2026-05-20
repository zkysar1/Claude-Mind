#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# decision-rules-staleness.sh — Phase 8e drift backstop.
#
# Warns when decision-rules-append.sh has NOT been invoked within
# DECISION_RULES_STALENESS_HOURS (default 24). The wrapper bumps
# `<agent>/session/decision-rules-last-call` on every invocation —
# including empty-stdin "no rule emerged" calls — so this probe can
# distinguish:
#
#   (1) Wrapper never called in N hours  → drift (SKILL.md skipping Step 8e)
#   (2) Wrapper called, emit `reason=no_rule_passed` per call  → legitimate
#
# Case (1) is the primary drift mode this backstop catches. Case (2) is
# already covered by the per-call stderr signal in decision-rules-append.py.
#
# Design parity with experience-staleness-check.sh: warn-only (exit 0),
# stderr WARN line grep-parity with other iteration-close WARN lines.
# Unlike experience-staleness-check.sh there is NO gate sentinel written
# to working memory — the per-call `no_rule_passed` signal already covers
# the legitimate "no rule this iteration" case, so an aggregate gate would
# force retroactive rule composition on iterations where no rule should
# have emerged. See phase-bash-enforcement-digest.md § PHASE 8e.
#
# Wiring: called from iteration-close.sh do_productivity_check alongside
# experience-staleness-check.sh. Also usable standalone for diagnostics.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"
source "$SCRIPT_DIR/_platform.sh"

STALENESS_HOURS="${DECISION_RULES_STALENESS_HOURS:-24}"
MARKER_FILE="$AGENT_DIR/session/decision-rules-last-call"
AGENT_NAME="${MIND_AGENT:-unknown}"

# Per-session WARN throttle (, 2026-04-25). This script is called
# from iteration-close.sh do_productivity_check on every iteration. Without
# the throttle, a session with a stale marker emits the same WARN line on
# every iteration (50+ identical lines for a long session = pure noise after
# the first). Once-per-session is enough signal: the agent has been told,
# repetition adds nothing. The throttle key is `latest-session-id` (a UUID
# that changes per session), so the WARN naturally re-fires next session if
# Phase 8e is still drifting.
SESSION_ID_FILE="$AGENT_DIR/session/latest-session-id"
LAST_WARNED_FILE="$AGENT_DIR/session/decision-rules-staleness-last-warned-sid"
# `[ -f ]` guards before `cat` are required because the script runs under
# `set -euo pipefail` — a missing file would make `cat` fail, and pipefail
# would propagate that failure even though stderr is redirected, killing
# the script silently. Initialize empty defaults and only read when the
# file exists.
CURRENT_SID=""
if [ -f "$SESSION_ID_FILE" ]; then
    CURRENT_SID="$(tr -d '\r\n' < "$SESSION_ID_FILE")"
fi
LAST_WARNED_SID=""
if [ -f "$LAST_WARNED_FILE" ]; then
    LAST_WARNED_SID="$(tr -d '\r\n' < "$LAST_WARNED_FILE")"
fi
if [ -n "$CURRENT_SID" ] && [ "$CURRENT_SID" = "$LAST_WARNED_SID" ]; then
    exit 0  # already warned this session — no-op tick
fi

# No marker file at all — wrapper has never been invoked for this agent.
# That's drift only if the agent has been running long enough to have
# completed at least one deep outcome. Precheck handles the "fresh agent"
# case upstream; here we simply warn once per check.
if [ ! -f "$MARKER_FILE" ]; then
    echo "[iteration-close] WARN: decision-rules-append.sh has NEVER been invoked for $AGENT_NAME — Phase 8e (Decision Rules) may be absent from the hot path. rb-428 / g-248-17." >&2
    [ -n "$CURRENT_SID" ] && echo "$CURRENT_SID" > "$LAST_WARNED_FILE"
    exit 0
fi

# Compare marker mtime against threshold. `date -r` for mtime is portable;
# fall back to Python if `date` lacks `-r` support.
python3 - "$MARKER_FILE" "$STALENESS_HOURS" "$AGENT_NAME" "$CURRENT_SID" "$LAST_WARNED_FILE" <<'PY' || true
import sys, os
from datetime import datetime

marker = sys.argv[1]
threshold_h = float(sys.argv[2])
agent_name = sys.argv[3]
current_sid = sys.argv[4] if len(sys.argv) > 4 else ""
last_warned_path = sys.argv[5] if len(sys.argv) > 5 else ""

try:
    mtime = os.path.getmtime(marker)
except OSError:
    sys.exit(0)

age_h = (datetime.now().timestamp() - mtime) / 3600.0

if age_h > threshold_h:
    print(
        f"[iteration-close] WARN: decision-rules-append.sh stale for {agent_name} — "
        f"last call {age_h:.1f}h ago (threshold {threshold_h:.0f}h). "
        f"Phase 8e (Decision Rules Extraction) may have drifted out of hot path — "
        f"rb-428 / guard-365.",
        file=sys.stderr,
    )
    # Record this session as warned so subsequent iterations skip the noise.
    # Caller (iteration-close.sh:747) already wraps this script in `|| true`,
    # so an I/O failure here surfaces a Python traceback to stderr but cannot
    # break the iteration loop. Intentional fail-loud — see fresh-eyes review.
    if current_sid and last_warned_path:
        with open(last_warned_path, "w", encoding="utf-8") as f:
            f.write(current_sid)
PY

exit 0
