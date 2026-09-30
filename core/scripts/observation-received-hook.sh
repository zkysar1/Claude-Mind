#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- runs on a vessel's POST /observe path, where no mind_api daemon need be running. Keep local: never add MCP or remote-service indirection here, and call only scripts that are local too.
# observation-received-hook.sh — wake the autonomous loop when the vessel perceives a change.
#
# Zak-Code's ObservationReceived hook: its own event, declared in .zakcode/settings.json,
# a file Claude Code never reads. The vessel runtime stages each environment CHANGE frame
# its POST /observe accepts, then runs this with the lifecycle payload on stdin:
#   {"hook_event_name": "ObservationReceived", "session_id": "<the run's current session>",
#    "cwd": "<workspace root>", "data": {"kind": "change", "observation_path": "<staged file>"}}
# Heartbeats and unknown kinds never reach it: the runtime fires the hook for changes only.
#
# The frame is already on disk when this runs. This is only the EARLY wake: it raises the
# agent's `perception-received` signal, which breaks a sleeping loop at once
# (interruptible-sleep.sh, BLOCKER class). A missed wake costs latency that the next
# perception round repays. It never costs the frame.
#
# EXIT CODE = the wake disposition the runtime reports for the frame (/sidecar/health):
#   0  delivered: the signal was written, or no loop here could be sleeping
#   1  dropped:   the agent's mode was unreadable or unknown, or the write failed
#
# It replaces the runtime's built-in signal-file wake, which ran these same scripts for the
# agent its run_stop_agent setting named. The hook finds the agent itself, so it also serves
# a run that names none.
set -uo pipefail   # no -e: every path below ends in a deliberate exit code

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# py -3 on Windows, python3 elsewhere: never a bare `py -3` (, guard-1098).
source "$SCRIPTS/_python_launcher.sh"
rt_python_launcher_into PYLAUNCH || PYLAUNCH=python3

# The run's current session. An unreadable payload reads as no session; the agent can still
# come from the environment below.
session_id="$(cat | $PYLAUNCH -c 'import json, sys; print(json.load(sys.stdin).get("session_id") or "")' 2>/dev/null)" || session_id=""

# WHICH AGENT: the environment first, then this session's binding, the order the framework's
# other hooks use (rb-5578). /start binds the agent under the session id the runtime reports.
agent="${MIND_AGENT:-}"
if [ -z "$agent" ] && [ -n "$session_id" ]; then
    agent="$(bash "$SCRIPTS/session-binding-read.sh" "$session_id" 2>/dev/null)" || agent=""
fi
# No agent bound is the framework's NO_AGENT state: /start has not run in this session, so no
# loop of ours is sleeping and there is nothing to wake. A healthy answer, not a failed wake.
# The resolver cannot tell unbound from an unreadable binding; an unreadable one also leaves
# every other hook in the session without an agent, which is where it shows.
[ -n "$agent" ] || exit 0

# AUTONOMOUS ONLY. reader and assistant run no loop, so a marker would reach no sleeping
# reader and would sit until a later /start cleared it (guard-1806). An unreadable or unknown
# mode wakes nothing either, but it is a fault worth seeing, so it reads as dropped.
mode="$(env MIND_AGENT="$agent" bash "$SCRIPTS/session-mode-get.sh" 2>/dev/null)" || exit 1
case "$mode" in
    autonomous) ;;
    reader|assistant) exit 0 ;;
    *) exit 1 ;;
esac

# Write every time, even over a marker already there. The write refreshes its mtime, and a
# sleep that starts after an older marker was left consumes that one as stale without waking
# (interruptible-sleep.sh, ); a frame arriving then must still wake it.
#
# The writer's exit status is the proof of the write: session.py exits 0 only after the
# marker's touch() has returned. Do not read the marker back instead. A sleeping loop polls
# every second and deletes the marker as it wakes, so a read-back races the very wake it
# checks and reports a delivered wake as dropped. Measured 2026-09-30: a read-back finishes
# about 8 ms after the touch, so it would misreport about 1 wake in 120 while a loop sleeps.
env MIND_AGENT="$agent" bash "$SCRIPTS/session-signal-set.sh" perception-received >/dev/null 2>&1 || exit 1
exit 0
