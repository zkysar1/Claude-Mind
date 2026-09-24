#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# SessionStart(compact) hook -- inject context restoration after compaction.
# Called by the SessionStart hook in .claude/settings.json (matcher: compact).
# Delegates to postcompact-restore.py (reads checkpoint, prints restoration to stdout).
set -euo pipefail
# _paths.sh + _platform.sh must both be sourced on Windows so CORE_ROOT is
# in cygpath form (C:/...) -- raw `pwd` returns /c/... which Python on
# Windows cannot open. Always use $CORE_ROOT for paths handed to python3,
# never $(cd && pwd).
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

# SessionStart hooks inherit no env vars, so MIND_AGENT is unset here.
# Without binding resolution, _paths.py would set AGENT_DIR=None and
# postcompact-restore.py would crash at `AGENT_DIR / "session" / ...`.
#
# The stdin read is BOUNDED (: the guard-664 /  daemon
# thread + join pattern; rb-1568 carries it to hooks). The production caller,
# sessionstart-orchestrator.sh, always pipes a finite payload, so the bound
# never fires there. A hand-run is different: under the Bash tool stdin can be
# the harness's never-EOF socket, and the old unbounded json.load blocked
# forever (cc-02, 2026-09-22: three processes alive 46h, a 0-byte log, and a
# restore that never ran). A timeout exits 3 and says so; it must never look
# like the empty-payload exit below.
SID_RC=0
SID=$(python3 -c '
import json, os, sys, threading
try:
    t_s = float(os.environ.get("POSTCOMPACT_RESTORE_STDIN_TIMEOUT_S", "10"))
except Exception:
    t_s = 10.0
box = {}
def _reader():
    try:
        box["data"] = sys.stdin.read()
    except Exception:
        box["data"] = ""
t = threading.Thread(target=_reader, daemon=True)
t.start()
t.join(t_s)
if "data" not in box:
    sys.stderr.write("[postcompact-restore] ERROR: stdin did not reach EOF within %gs"
                     " -- no payload was piped in, so NOTHING WAS RESTORED. Pipe the"
                     " SessionStart payload (a JSON object carrying session_id) into"
                     " this script, or redirect stdin from /dev/null. (g-115-10615)\n" % t_s)
    sys.exit(3)
try:
    print(json.loads(box["data"]).get("session_id", ""))
except Exception:
    print("")
') || SID_RC=$?
if [ "$SID_RC" -eq 3 ]; then
    exit 3
fi

# No session_id -> nothing to key a restore on. Say so on stderr (stdout is the
# context-injection channel). Before  this exit wrote 0 bytes, the
# same log a read that never returns leaves, so the two could not be told apart.
if [ -z "$SID" ]; then
    echo "[postcompact-restore] no session_id on stdin (empty or non-JSON payload) -- nothing to restore" >&2
    exit 0
fi
AGENT=$(python3 "$CORE_ROOT/scripts/_resolve_agent_from_sid.py" "$SID" 2>/dev/null || echo "")

# No agent bound to this SID -> nothing to restore. Exit clean; the user
# will see an empty post-compact prompt and can /start to rebind.
[ -z "$AGENT" ] && exit 0

# Observer guard: this hook resumes the autonomous loop after autocompact.
# Observers (reader/assistant mode) that coexist with the runner ALSO fire
# SessionStart(compact) when they autocompact, and without this guard receive
# the runner's "Resume execution on THIS goal" imperative — the mode-violation
# that surfaced as the 2026-05-10 bravo "assistant entered the loop" incident.
# Discriminator: only the runner's HOOK_SID equals running-session-id AFTER
# session-save-id.sh runs. session-save-id.sh updates running-session-id only
# when its witness gate confirms a legit runner resume, so that file is
# the canonical runner-identity signal at this point.
# DO NOT reorder this hook before session-save-id.sh in .claude/settings.json
# — the discriminator depends on session-save-id.sh having run first.
# Missing/empty running-session-id (no autonomous runner active) → skip;
# nothing to resume.
#
# The `|| RUNNING_SID=""` is load-bearing (guard-568). Under `set -euo pipefail`
# an ABSENT file fails the cat, pipefail carries that through the pipe, and a
# failed command substitution in a plain assignment aborts the script with rc=1
# right here, before the worker-Body admission below can run. A cross-box
# worker box never writes running-session-id (body-manifest.sh), so until
# 2026-09-23 () this line silently skipped the compaction restore on
# exactly the boxes that admission exists for. Measured on zc-01/zc-02 that
# day: 12 of 19 worker compactions got no restore at all; the other 7 got
# through only while the file existed, the one way past this line (what wrote
# it then is not recorded). The test that pinned the admission wrote an EMPTY
# file, which cat reads fine.
RUNNING_SID=$(cat "$(agent_dir "$AGENT")/session/running-session-id" 2>/dev/null | tr -d '\r\n') || RUNNING_SID=""
if [ -z "$RUNNING_SID" ] || [ "$SID" != "$RUNNING_SID" ]; then
    # ...EXCEPT a worker Body, which is not an observer ().
    # precompact-checkpoint.sh has NO runner guard, so it fires for a worker
    # body and writes a BODY-KEYED checkpoint. This guard then refused the very
    # session that checkpoint belongs to: written, never consumed. Fired live
    # 2026-08-04 — worker body 301a45f2 autocompacted at 18:38 during the
    #  soak, restore refused, and the resumed session took a close path,
    # silently ending the soak's worker leg.
    #
    # Discriminator is the SAME rail body_state_path() keys on (_paths.py:99-111,
    # and bash-agent-inject.py's BODY_ROLE=worker derivation): a session is a
    # non-reducer Body iff it forked a per-session working-memory.yaml. Only a
    # worker forks one, so this admits bodies WITHOUT admitting the observers
    # the guard exists to exclude — a reader/assistant session never forks a
    # body WM and still exits here, preserving the 2026-05-10 bravo fix.
    # Deliberately NOT gated on RUNNING_SID: a body whose reducer has since
    # stopped still owns a real checkpoint, and the .py reads only that body's
    # own file. If the invariant above ever changes, this rail must change with
    # it — that coupling is why _paths.py names its dependents explicitly.
    # agent_session_dir (_paths.sh:139), NOT a literal "sessions" segment:
    # SESSIONS_DIRNAME is one of the three sync constants in CLAUDE.md's
    # Agent-dir Resolution table, and a hardcoded segment is invisible to the
    # constant-name audit grep — the separate literal-string-hardcoder class
    # that same table has to enumerate by hand.
    if [ ! -f "$(agent_session_dir "$AGENT" "$SID")/working-memory.yaml" ]; then
        exit 0
    fi
fi

export MIND_AGENT="$AGENT"
# Symmetry with precompact-checkpoint.sh (). LIVE since  — the
# guard above now admits worker bodies, so this export is what lets
# body_state_path() in the .py resolve the body-keyed checkpoint instead of the
# agent-wide fallback. It is load-bearing, not decorative: drop it and a body
# restores the reducer's checkpoint. (This comment read "Inert today" while the
# guard still refused every body.)
export MIND_SID="$SID"
exec python3 "$CORE_ROOT/scripts/postcompact-restore.py"
