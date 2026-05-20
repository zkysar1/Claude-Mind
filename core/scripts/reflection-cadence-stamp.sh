#!/usr/bin/env bash
# reflection-cadence-stamp.sh — G5 of Phase 1.5 (world/conventions/self-program-evolution.md).
#
# Called from /reflect SKILL.md Step 0.1 at the start of every reflection.
# Two writes:
#   1. wm.last_reflection_at  ← {ts, goal_id, mode} — "most recent reflection" marker
#   2. world/reflection-history.jsonl  ← append-only history for cadence computation
#
# Signal #13 of the Self/Program evolution metric vector reads the history
# file to compute "reflection_cadence — fraction of expected reflection
# windows that contained ≥1 fire over the last 50 goals."
#
# Fail-silent on any error. Never blocks /reflect.
#
# Usage:
#   reflection-cadence-stamp.sh <mode>
# where <mode> is one of: on-hypothesis | on-execution | extract-patterns |
# calibration-check | full-cycle | curate-memory | curate-aspirations | batch-micro

set +e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || exit 0
cd "$PROJECT_ROOT" 2>/dev/null || exit 0

AGENT="${MIND_AGENT:-}"
if [[ -z "$AGENT" ]]; then
    exit 0  # No agent bound — silent no-op.
fi

# Pass mode + script_dir via env vars (quoted heredoc — no bash substitution).
export REFLECTION_MODE="${1:-unspecified}"
export REFLECTION_SCRIPT_DIR="$SCRIPT_DIR"

python3 - <<'PYEOF'
import json, os, sys, subprocess
from datetime import datetime
from pathlib import Path

script_dir = os.environ.get("REFLECTION_SCRIPT_DIR", "")
if script_dir:
    sys.path.insert(0, script_dir)

try:
    from _paths import WORLD_DIR, AGENT_DIR, CORE_ROOT
except Exception:
    sys.exit(0)

agent = os.environ.get("MIND_AGENT", "")
if not agent or AGENT_DIR is None or WORLD_DIR is None:
    sys.exit(0)

mode = os.environ.get("REFLECTION_MODE", "unspecified")
ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

# Resolve goal_id: prefer team-state in_flight; fall back to wm.current_goal_id.
goal_id = None
try:
    ts_path = WORLD_DIR / "team-state.yaml"
    if ts_path.exists():
        import yaml
        with open(ts_path, encoding="utf-8") as f:
            team = yaml.safe_load(f) or {}
        inf = team.get("in_flight", {})
        if isinstance(inf, dict):
            agent_inf = inf.get(agent, {})
            if isinstance(agent_inf, dict):
                goal_id = agent_inf.get("goal_id")
except Exception:
    pass

if not goal_id:
    try:
        wm_py = CORE_ROOT / "scripts" / "wm.py"
        r = subprocess.run(
            [sys.executable, str(wm_py), "read", "current_goal_id"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            s = r.stdout.strip()
            if s and s != "null":
                goal_id = s.strip('"')
    except Exception:
        pass

# Write 1: wm.last_reflection_at via wm.py set
try:
    payload = {"ts": ts, "goal_id": goal_id, "mode": mode}
    wm_py = CORE_ROOT / "scripts" / "wm.py"
    subprocess.run(
        [sys.executable, str(wm_py), "set", "last_reflection_at"],
        input=json.dumps(payload), text=True,
        capture_output=True, timeout=5,
    )
except Exception:
    pass

# Write 2: append to world/reflection-history.jsonl
try:
    hist_path = WORLD_DIR / "reflection-history.jsonl"
    record = {
        "ts": ts,
        "agent": agent,
        "mode": mode,
        "goal_id": goal_id,
    }
    with open(hist_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")
except Exception:
    pass

sys.exit(0)
PYEOF
exit 0
