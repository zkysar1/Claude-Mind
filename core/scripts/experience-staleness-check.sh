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

# MIND_EXPERIENCE_FILE is a TEST-hermeticity override only () — same
# posture as MIND_AGENTS_ROOT in gates/goal_duplication.py. It lets the
# regression test drive the real code path over a tmp file instead of
# manufacturing a fake agent dir under agents/, which on a live box would appear
# in every cross-agent `*/local-paths.conf` enumeration mid-run. Production never
# sets it.
EXP_FILE="${MIND_EXPERIENCE_FILE:-$AGENT_DIR/experience.jsonl}"

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

# Read the NEWEST entry's timestamp — the MAX over all parsed entries, NOT the
# last line. Line order is not timestamp order here (): bravo's file
# ends with a 2026-07-10 entry while 2026-07-26 entries sit earlier, so the
# last-line read reported 383h stale against an archive written 15 min prior and
# false-fired force_experience_archival every iteration. Fallback: if nothing
# parses, exit silent (this is informational).
python3 - "$EXP_FILE" "$STALENESS_HOURS" "${MIND_AGENT:-unknown}" <<'PY' || true
import sys, json, os, subprocess
from datetime import datetime, timedelta

exp_file = sys.argv[1]
threshold_h = float(sys.argv[2])
agent_name = sys.argv[3]

# : route the freshness read through the storage backend. On
# own-cloud boxes the authoritative store is S3 and the local file is only
# write-through-current on the box whose daemon last appended — a lagging
# mirror yields phantom staleness (observed 8-day divergence -> false
# force_experience_archival sentinel). refresh() pulls the latest remote
# state before the read; no-op on LocalBackend. Fail-open: any backend
# error falls back to the raw local read (pre-fix behavior).
try:
    _pr = os.environ.get("PROJECT_ROOT", "").rstrip("/")
    if _pr:
        sys.path.insert(0, os.path.join(_pr, "core", "scripts"))
    from pathlib import Path as _Path
    from storage_backend import get_backend
    get_backend().refresh(_Path(exp_file))
except Exception:
    pass

def _parse_ts(raw):
    """Naive-datetime or None. Tolerates naive ISO, Z-suffixed, and offset-bearing
    stamps (g-115-3027) so mixed forms in one file still compare against each other."""
    try:
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


# Newest-by-TIMESTAMP, not last-by-POSITION (). Comparison is on parsed
# datetimes, never on the raw strings — mixed naive/offset forms do not sort
# lexicographically.
last_dt = None
last_ts = None
last_id = None

# guard-980 / : on own-cloud boxes the local experience.jsonl is a
# write-through cache ONLY on the daemon's own box; on every other box it is a
# stale git-sync mirror. A raw read makes a false staleness decision (observed
# 8-day divergence: bravo local 07-02 vs S3 07-10) and false-fires
# force_experience_archival. Route the read through the backend first: refresh()
# force-fetches the authoritative copy on OwnCloudBackend and is a No-op on
# LocalBackend (canonical pattern — mind_api/src/endpoints/experience_write.py
# _read_jsonl). Fail-open: if the backend can't resolve (bare subprocess without
# MIND_WORLD/MIND_META) degrade to the raw read rather than skip it — this
# canary is informational (rb-428) and must never hard-fail the iteration-close
# hot path.
try:
    sys.path.insert(0, os.path.join(os.environ.get("PROJECT_ROOT", ""), "core", "scripts"))
    from storage_backend import get_backend
    get_backend().refresh(exp_file)
except Exception:
    pass

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
        if not ts:
            continue
        dt = _parse_ts(ts)
        if dt is None:
            # Unparseable stamp on one entry must not decide the file's freshness.
            continue
        if last_dt is None or dt > last_dt:
            last_dt = dt
            last_ts = ts
            last_id = e.get("id") or "?"

if last_dt is None:
    # Empty, unparseable, or no timestamped entries — silent (not our alarm to raise)
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
    # The sentinel asserts "THIS AGENT's archive is stale" and gates the
    # precheck Phase 0-pre2 obligation. Under MIND_EXPERIENCE_FILE the input
    # is NOT this agent's archive, so that assertion is false by construction —
    # writing it poisons the live WM slot with a verdict about some other file.
    # Observed 2026-07-26 (): the  regression tests pointed
    # the override at a tmp fixture and their `exp-old` / 400.0h payload landed
    # in the production slot, false-firing the archival gate on a 0.7h-fresh
    # archive one iteration later. The tests isolated the INPUT and missed the
    # WRITE. Warning still prints (diagnostic value, no side effect).
    pr = os.environ.get("PROJECT_ROOT", "").rstrip("/")
    if os.environ.get("MIND_EXPERIENCE_FILE"):
        print("[experience-staleness-check] override file in use — sentinel write SKIPPED "
              "(the slot is about this agent's own archive)", file=sys.stderr)
        pr = ""
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
