#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- SessionStart critical path. Keep local: never add MCP or remote-service indirection here.
# wm-contamination-check.sh -- thin SessionStart wrapper around
# wm-contamination-check.py (the REMEDIAL cross-agent WM contamination
# detector; sibling to the PREVENTIVE sid-collision-check.sh).
#
# Reads the SessionStart stdin JSON, extracts session_id, resolves the bound
# agent from it, and runs the detector with --apply (quarantine on detection).
# Daemon-independent by design -- the detector reads JSONL/YAML directly because
# the daemon may not be up yet at SessionStart.
#
# Called from sessionstart-orchestrator.sh AFTER session-save-id + recovery-gate
# (so the binding is resolvable), BEFORE /prime. Output (silent on a clean WM,
# a loud block on detection) flows to the LLM's post-SessionStart context.
#
# FAIL-OPEN: never exits non-zero; a detector failure must never block session
# start. The detector is itself fail-open; this wrapper double-guards with `|| true`.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Capture stdin (the SessionStart JSON payload) once.
STDIN_JSON=$(cat 2>/dev/null || echo "{}")

# Extract session_id. py -3 first (always works on Windows, avoids the MS Store
# stub before bash-agent-inject's PATH shim is active), python3 fallback. Same
# pattern as sessionstart-orchestrator.sh's source parse (rb-370/guard-335).
SID=$(printf '%s' "$STDIN_JSON" | (py -3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null) || echo "")

# No SID -> nothing to resolve; the detector would no-op anyway. Exit clean.
if [ -z "$SID" ]; then
    exit 0
fi

# Run the detector. --apply quarantines on detection. No --json: silent on
# clean, loud human block on detection (surfaced to the LLM).
( py -3 "$SCRIPT_DIR/wm-contamination-check.py" --sid "$SID" --apply 2>/dev/null \
    || python3 "$SCRIPT_DIR/wm-contamination-check.py" --sid "$SID" --apply 2>/dev/null \
    || true )

exit 0
