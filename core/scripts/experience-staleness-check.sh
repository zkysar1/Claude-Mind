#!/usr/bin/env bash
# experience-staleness-check.sh — Phase 4.25 drift compliance check ().
#
# Reads the current agent's experience.jsonl and emits a one-line warning
# when the most-recent entry is older than EXPERIENCE_STALENESS_HOURS
# (default 12). This catches the Phase 4.25 (Experience Archival) drift
# pattern documented in rb-428: the bash consolidation (iteration-close.sh)
# handles verify/state/learn/productivity mechanical bookkeeping, but
# experience-add.sh is LLM-only and can silently disappear from the hot
# path. Both alpha (~30h) and bravo (~76h) experience.jsonl files were
# stale when this was discovered during  iter-45.
#
# Design choice (original ): warning only. As of rb-428 follow-up,
# the script ALSO writes a `force_experience_archival` WM sentinel. The
# sentinel is consumed by aspirations-precheck Phase 0-pre2, which blocks
# goal selection until the LLM composes the missed experience record
# retroactively. Mirrors the force_tree_maintain / Phase 0-pre pattern.
#
# Exit code: still always 0 (informational only). Warnings go to stderr so
# they surface in the iteration-close output without polluting JSON consumers.
# The sentinel write is best-effort inside the Python block — any failure is
# swallowed so the script's calling contract (`|| true` safe) is preserved.
#
# Wiring: called from iteration-close.sh do_productivity_check. Also usable
# standalone for diagnostics.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"
# _platform.sh converts MSYS /c/... paths to Windows C:/... so python3 can
# find the file. Same pattern as test-wm-prune-cadence-protection.sh.
source "$SCRIPT_DIR/_platform.sh"

EXP_FILE="$AGENT_DIR/experience.jsonl"

# No file — silent. Fresh agent, init-agent.sh will seed.
[ -f "$EXP_FILE" ] || exit 0

# Read staleness_hours from aspirations.yaml — single source of truth. Fails
# loud (exit 1) if the block is missing, so misconfig surfaces during
# standalone diagnostic runs; iteration-close.sh shields the hot path with
# `|| true`.
STALENESS_HOURS="$(python3 -c "
import sys, os
sys.path.insert(0, os.path.join(r'$PROJECT_ROOT', 'core', 'scripts'))
import yaml
with open(os.path.join(r'$PROJECT_ROOT', 'core', 'config', 'aspirations.yaml'), 'r', encoding='utf-8') as f:
    cfg = yaml.safe_load(f) or {}
block = cfg.get('experience_archival_gate') or {}
if 'staleness_hours' not in block:
    sys.stderr.write('ERROR: aspirations.yaml missing experience_archival_gate.staleness_hours\n')
    sys.exit(1)
print(block['staleness_hours'])
")"
# Export Windows-form PROJECT_ROOT — the python subprocess launches bash.exe,
# which interprets `/c/...` as a non-existent root (unlike interactive MSYS
# bash which maps it). Needs `C:/...` form. _platform.sh already cygpath-ed it.
export PROJECT_ROOT

# Read last non-empty line's timestamp. JSONL append-only means last line is
# newest. Fallback: if tail/parse fails, exit silent (this is informational).
python3 - "$EXP_FILE" "$STALENESS_HOURS" "${MIND_AGENT:-unknown}" <<'PY' || true
import sys, json, os, subprocess
from datetime import datetime, timedelta

exp_file = sys.argv[1]
threshold_h = float(sys.argv[2])
agent_name = sys.argv[3]

last_ts = None
last_id = None
with open(exp_file, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        ts = e.get("created") or e.get("timestamp")
        if ts:
            last_ts = ts
            last_id = e.get("id") or "?"

if last_ts is None:
    # Empty or unparseable — silent (not our alarm to raise)
    sys.exit(0)

try:
    # Tolerate naive ISO timestamps (no tz) — local system time is our convention
    last_dt = datetime.fromisoformat(last_ts.replace("Z", ""))
except Exception:
    sys.exit(0)

now = datetime.now()
age = now - last_dt
age_h = age.total_seconds() / 3600.0

if age_h > threshold_h:
    # Warn to stderr — iteration-close.sh surfaces stderr in its output.
    # Format mirrors other [iteration-close] WARN lines for grep parity.
    print(
        f"[iteration-close] WARN: experience.jsonl stale for {agent_name} — "
        f"last entry {last_id} {age_h:.1f}h ago (threshold {threshold_h:.0f}h). "
        f"Phase 4.25 Experience Archival may have drifted out of hot path — "
        f"rb-428 / g-248-16.",
        file=sys.stderr,
    )

    # Compliance-gate sentinel write (rb-428 follow-up). Best-effort: any
    # failure here leaves behavior identical to the pre-gate canary alone.
    # Invoke wm.py directly via the current Python interpreter — bypasses
    # the bash wrapper (wm-set.sh) entirely. Rationale: subprocess-launched
    # bash.exe on Windows (both C:/... and /c/... forms tested) cannot
    # resolve the wrapper's path reliably, because it's a different bash
    # binary than interactive MSYS bash and has no drive-mount knowledge.
    # Since wm.py is pure Python, we skip the shell layer. This preserves
    # the wrapper's behavior (set slot from stdin JSON) because wm.py is
    # exactly what the wrapper exec's anyway.
    pr = os.environ.get("PROJECT_ROOT", "").rstrip("/")
    if pr:
        wm_py_path = pr + "/core/scripts/wm.py"
        payload = json.dumps({
            "triggered_at": now.isoformat(timespec="seconds"),
            "last_entry_id": str(last_id),
            "age_hours": round(age_h, 1),
        })
        try:
            subprocess.run(
                [sys.executable, wm_py_path, "set", "force_experience_archival"],
                input=payload,
                text=True,
                capture_output=True,
                timeout=5,
                cwd=pr,
            )
        except Exception:
            pass
PY

exit 0
