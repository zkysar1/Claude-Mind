#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# session-manifest-clear.sh — single source of truth for the manifest-driven
# clear of transient session files during crashed-runner recovery.
#
# Consumes `session_snapshot.py --output json` (the canonical manifest parser
# also used by session-desync-check.sh) and rm -f's every entry with
# `recovery_action: clear` AND `exists: true`. Idempotent.
#
# Authorized callers:
#   - /start --recover Phase 0.7 (LLM-driven manual recovery)
#   - core/scripts/recovery-gate.sh (SessionStart hook auto-recovery)
#
# DO NOT inline this clear logic anywhere else. If you find yourself parsing
# the manifest in another script, call THIS instead. Editing the clear list
# (or its semantics) once and forgetting another copy is exactly the drift the
# manifest exists to prevent.
#
# WHY subprocess.run NOT shell pipe: piping `bash session-snapshot.sh | python3`
# breaks on Windows (Errno 22 OSError on producer's stdout flush). Calling
# session_snapshot.py via subprocess.run + capture_output sidesteps the pipe.
# This mirrors what /start --recover Phase 0.7 did before extraction. DO NOT
# "simplify" this back to a shell pipe — it WILL silently fail on Windows.
#
# Requires MIND_AGENT to be set (or local-paths.conf-bound). Exits 0 on
# success, 1 only if MIND_AGENT cannot be resolved (caller error).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

if [[ -z "${AGENT_NAME:-}" ]]; then
    echo "session-manifest-clear: MIND_AGENT not set and no local-paths.conf bound" >&2
    exit 1
fi

exec python3 - <<'PYEOF'
import json, os, shutil, subprocess, sys
from pathlib import Path
sys.path.insert(0, "core/scripts")
from _paths import AGENT_DIR  # type: ignore

snap = subprocess.run(
    [sys.executable, "core/scripts/session_snapshot.py", "--output", "json"],
    capture_output=True, text=True, check=True,
)
session_dir = Path(AGENT_DIR) / "session"
for f in json.loads(snap.stdout).get("files", []):
    if f.get("recovery_action") != "clear":
        continue
    if not f.get("exists"):
        continue
    # glob: true entries (e.g. aspirations-incremented-session-*.txt):
    # CRITICAL — re-glob from the live filesystem. Do NOT iterate
    # f["matches"] from the snapshot. The manifest pattern is the single
    # source of truth; both consumers (snapshot for inspection, this
    # script for action) derive their own truth at their own moment.
    # Chaining clear through snapshot would miss files that appeared
    # after the snapshot, and would force missing_ok=True to mask the
    # symmetric vanished-file case.
    # type=dir entries (e.g. scratch/): wipe contents but keep the directory.
    # Files (default): single unlink.
    if f.get("glob"):
        matches = list(session_dir.glob(f["file"]))
        for m in matches:
            m.unlink()
        print(f"cleared (glob): {f['file']} ({len(matches)} matches)")
    elif f.get("type") == "dir":
        p = session_dir / f["file"]
        for child in p.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        print(f"cleared (contents): {f['file']}/")
    else:
        p = session_dir / f["file"]
        p.unlink()
        print(f"cleared: {f['file']}")

# rb-671: clear cross-agent team-state in_flight. Without this, partner agents
# read team-state.agent_status.<agent>.in_flight and see the recovered agent
# as indefinitely mid-execution on its abandoned goal — false-positive yields
# at the partner-claim filter in goal-selector. Fail-open: recovery is the
# last line of defense and must complete even if cross-agent cleanup errors.
# Bash already exited 1 above if MIND_AGENT was unset, so the env read is safe.
agent_name = os.environ["MIND_AGENT"]
# Direct call to team-state.py (not the team-state-clear-in-flight.sh wrapper):
# the wrapper sources _platform.sh, but team-state.py self-reconfigures stdout
# to UTF-8 and uses _paths.py for path resolution, so the wrapper adds a bash
# subprocess hop with zero functional benefit here. Keep direct.
try:
    result = subprocess.run(
        [sys.executable, "core/scripts/team-state.py", "clear-in-flight", "--agent", agent_name],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if result.returncode == 0:
        print(f"cleared: team-state in_flight ({agent_name})")
    else:
        print(
            f"team-state in_flight clear non-zero ({agent_name}, rc={result.returncode}): "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
except Exception as exc:
    print(f"team-state in_flight clear failed (non-fatal): {exc}", file=sys.stderr)
PYEOF
