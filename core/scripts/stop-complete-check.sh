#!/usr/bin/env bash
# stop-complete-check.sh — has this agent finished stopping ON THIS BOX?
#
# The quiesce gate a cutover/snapshot window opens on. `agent-state: IDLE` is
# NOT stop-complete: /stop writes agent-state at graceful-stop D1 and agent-mode
# at D7, and the obligation tail between them keeps writing world stores —
# measured 2026-09-14, laptop foxtrot wrote for 40 minutes after IDLE and the
# driver's verify passed anyway (). Decision logic, the signal ordering
# and the fail-safe rationale live in core/scripts/stop_complete.py; this
# wrapper only gathers inputs for it.
#
#   bash core/scripts/stop-complete-check.sh                  # every agent on this box
#   bash core/scripts/stop-complete-check.sh --agent alpha    # one (repeatable)
#   bash core/scripts/stop-complete-check.sh --json
#
# EXIT CODES ARE THE CONTRACT, and only 0 authorizes opening the window:
#   0 = QUIESCED   every agent measured has stopped here
#   1 = WRITING    at least one is still writing (decisive)
#   2 = UNKNOWN    a signal was unreadable, or nothing was measured
# 1 and 2 are both REFUSE. Do not collapse them — a caller that only tests
# `-eq 1` treats its own inability to evaluate as a pass (guard-5093: a wrapper
# fronting a verdict-bearing instrument must not let its failure codes read as a
# verdict). This is a gate in front of a destructive, human-scheduled operation,
# so every ambiguity resolves toward refuse.
#
# NOT a machine-quiet check. Agents are only one class of writer: orphan daemons
# and host timers are a per-MACHINE census with no agent to key on (runbook
# §14.8.1 item 2) and are deliberately out of scope — see stop_complete.py.
#
# Every remote invocation of this script must carry `OWNCLOUD_GZIP_STORES` and
# `TZ=UTC` on its command line like every other per-box command in the cutover
# runbook (§14.8.1 item 3, guard-6713 / guard-1556): a non-interactive shell
# reads no profile.
#
# Source: . Prose twin: world/conventions/aws-exit-cutover-runbook.md
# §14.8.1 item 1. Sibling: fleet-live-bodies.sh derives WHO must be stopped;
# this answers whether a given box is DONE stopping.
#
# guard-614: no `set -e` — this emits a structured verdict on every exit path.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null || true
REPO="$(pwd)"
# shellcheck disable=SC1091
source "$REPO/core/scripts/_paths.sh" 2>/dev/null || true

MODE="human"
AGENTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --json)  MODE="json" ;;
    --agent) shift; [ $# -gt 0 ] && AGENTS+=("$1") ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
  esac
  shift
done

AROOT="$REPO/agents"

# Default enumeration: every agent that has ever been started on this box (an
# agent with no agent-state file has nothing here to quiesce). An explicitly
# named agent is measured whether or not the file exists — an absent state file
# is then an UNREADABLE signal, not an empty one.
if [ ${#AGENTS[@]} -eq 0 ]; then
  while IFS= read -r p; do
    [ -n "$p" ] && AGENTS+=("$(basename "$(dirname "$(dirname "$p")")")")
  done < <(ls -1 "$AROOT"/*/session/agent-state 2>/dev/null)
fi

# ---- gather ---------------------------------------------------------------
# One TSV line per agent: agent \t state \t state_mtime \t mode_mtime \t claim_rc
# (empty field = could not read). Parsed once, below.
ROWS=""
for a in ${AGENTS[@]+"${AGENTS[@]}"}; do
  SDIR="$AROOT/$a/session"
  STATE=""; SMTIME=""; MMTIME=""
  if [ -r "$SDIR/agent-state" ]; then
    STATE="$(tr -d ' \t\r\n' < "$SDIR/agent-state" 2>/dev/null)"
    SMTIME="$(stat -c %Y "$SDIR/agent-state" 2>/dev/null || stat -f %m "$SDIR/agent-state" 2>/dev/null)"
  fi
  [ -r "$SDIR/agent-mode" ] && \
    MMTIME="$(stat -c %Y "$SDIR/agent-mode" 2>/dev/null || stat -f %m "$SDIR/agent-mode" 2>/dev/null)"

  # The claim probe must NOT be piped — a pipeline's $? is the last stage's, and
  # this rc IS the signal (measured: `... | tail -4` reported rc=0 for an ABSENT
  # claim that the bare call reports rc=4).
  CLAIM_RC=""
  if [ -x "$REPO/core/scripts/runner-claim.sh" ] || [ -f "$REPO/core/scripts/runner-claim.sh" ]; then
    timeout 90 bash "$REPO/core/scripts/runner-claim.sh" status --agent "$a" >/dev/null 2>&1
    CLAIM_RC=$?
    # A timeout is not a verdict. 124 falls through as an unrecognised rc, which
    # stop_complete.py reads as UNKNOWN.
  fi

  ROWS="$ROWS$a	$STATE	$SMTIME	$MMTIME	$CLAIM_RC
"
done

# ---- one process table for the box ----------------------------------------
PROCS="$(ps -eo pid=,ppid=,comm= 2>/dev/null)"
PROC_OK=$?

# ---- decide ---------------------------------------------------------------
OUT="$(ROWS="$ROWS" PROCS="$PROCS" PROC_OK="$PROC_OK" SELF="$$" MODE="$MODE" REPO="$REPO" \
       python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["REPO"], "core", "scripts"))
from stop_complete import decide, live_claude_pids


def _num(v):
    v = (v or "").strip()
    return float(v) if v else None


rows = []
for line in os.environ.get("ROWS", "").splitlines():
    if not line.strip():
        continue
    f = (line.split("\t") + [""] * 5)[:5]
    rows.append({
        "agent": f[0],
        "agent_state": f[1].strip() or None,
        "state_mtime": _num(f[2]),
        "mode_mtime": _num(f[3]),
        "claim_rc": int(f[4].strip()) if f[4].strip() else None,
        "claude_pids": None,       # filled once, below, for the whole box
    })
pids = None
if os.environ.get("PROC_OK") == "0":
    table = []
    for line in os.environ.get("PROCS", "").splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            try:
                table.append((int(parts[0]), int(parts[1]), parts[2].strip()))
            except ValueError:
                pass
    pids = live_claude_pids(table, int(os.environ["SELF"]))
for r in rows:
    r["claude_pids"] = pids

res = decide(rows)
if os.environ.get("MODE") == "json":
    print(json.dumps(res, indent=2))
else:
    print("stop-complete: %s — %s" % (res["verdict"].upper(), res["reason"]))
    for a in res["agents"]:
        s = a["signals"]
        print("  %-10s %-9s state=%-8s mode_mtime=%s claim_rc=%s claude_pids=%s"
              % (a["agent"], a["verdict"], s["agent_state"],
                 "absent" if s["mode_mtime"] is None else int(s["mode_mtime"]),
                 s["claim_rc"],
                 "unreadable" if s["claude_pids"] is None else (s["claude_pids"] or "none")))
    print("  -> rc=%d (only 0 authorizes opening the window)" % res["rc"])
sys.exit(res["rc"])
PY
)"
RC=$?
printf '%s\n' "$OUT"
exit $RC
