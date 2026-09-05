#!/usr/bin/env python3
"""Session Desync Check — advisory invariant checker for <agent>/session/.

Runs session_snapshot.py to get the current state of session/, reads the
manifest's `invariants:` block, and evaluates each invariant rule against
the snapshot. Warnings are written (append-only) to
`<agent>/session/desync-warnings.jsonl` and echoed to stdout.

Always exits 0 — this is an advisory checker, not a gate. The goal is to
surface stale state so the agent (or operator) can decide whether a
`/start --recover` is warranted. Auto-remediation is out of scope by
design: the risk of silently deleting a file that looks orphaned but is
actually in-use is higher than the cost of the warning going unread.

Invariants parsed from the manifest use a narrow, fixed schema. The rule
text in the YAML is human-readable; each rule has a corresponding
Python handler here keyed off the `id` field. Add a new invariant by:
  1. Adding it to session-manifest.yaml
  2. Adding a matching handler function below keyed by id

If an invariant id has no handler, it is logged as "unhandled" (soft fail)
rather than crashing — so adding manifest entries doesn't break the run.
"""

import argparse
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    from _paths import AGENT_DIR  # type: ignore
except Exception as e:
    print(f"ERROR: failed to import _paths: {e}", file=sys.stderr)
    sys.exit(0)  # advisory — never block

# SSOT for the own-cloud sync data-extension set (Phase 2 fail-safe heuristic).
# Used by the orphan knowledge-loss classifier below to predict whether the
# sweep will mirror an UNREGISTERED session file to S3 (data extension) or keep
# it machine-local (signal-shaped). Guarded: if the import fails, the classifier
# falls back to treating every orphan as the higher-risk signal-shaped case.
try:
    from owncloud_sync import _SESSION_DATA_EXTS  # type: ignore
except Exception:
    _SESSION_DATA_EXTS = None


# ─────────────────────────────────────────────────────────────────────
# Invariant handlers. Each takes (files_by_name: dict, agent_state: str)
# and returns True if the invariant is violated (i.e., emit a warning).
# ─────────────────────────────────────────────────────────────────────

def _mtime_age_seconds(file_record):
    """Return age in seconds of a file record, or None if missing."""
    if not file_record or not file_record.get("exists"):
        return None
    mtime_str = file_record.get("mtime")
    if not mtime_str:
        return None
    try:
        mtime = datetime.datetime.fromisoformat(mtime_str)
        return (datetime.datetime.now() - mtime).total_seconds()
    except Exception:
        return None


def _inv_heartbeat_without_running(files, agent_state):
    hb = files.get("runner-heartbeat")
    age = _mtime_age_seconds(hb)
    return age is not None and age < 120 and agent_state != "RUNNING"


def _inv_stop_requested_when_idle(files, agent_state):
    f = files.get("stop-requested")
    return bool(f and f.get("exists")) and agent_state == "IDLE"


def _inv_iteration_checkpoint_when_idle(files, agent_state):
    f = files.get("iteration-checkpoint.json")
    return bool(f and f.get("exists")) and agent_state == "IDLE"


def _inv_loop_active_when_idle(files, agent_state):
    f = files.get("loop-active")
    return bool(f and f.get("exists")) and agent_state == "IDLE"


def _inv_running_without_heartbeat(files, agent_state):
    hb = files.get("runner-heartbeat")
    return agent_state == "RUNNING" and not (hb and hb.get("exists"))


def _inv_running_without_persona(files, agent_state):
    # Gated on loop-active, NOT on agent-state alone: /start flips agent-state to
    # RUNNING *before* invoking /boot, and /boot sets persona a few steps later, so
    # a bare RUNNING+persona-false test would fire on EVERY legitimate boot.
    # loop-active is written only once the aspirations loop is actually turning —
    # past every persona-setting step — so this fires only on the real defect.
    # Tests for "false" specifically, never "not true": an absent or unreadable
    # persona-active is "unset", which user-interaction.md treats as persona ON.
    lp = files.get("loop-active")
    if agent_state != "RUNNING" or not (lp and lp.get("exists")):
        return False
    return _read_persona_active(files) == "false"


_HANDLERS = {
    "heartbeat_without_running": _inv_heartbeat_without_running,
    "stop_requested_when_idle": _inv_stop_requested_when_idle,
    "iteration_checkpoint_when_idle": _inv_iteration_checkpoint_when_idle,
    "loop_active_when_idle": _inv_loop_active_when_idle,
    "running_without_heartbeat": _inv_running_without_heartbeat,
    "running_without_persona": _inv_running_without_persona,
}


def _classify_orphan(name, size):
    """Knowledge-loss classifier for an UNREGISTERED session/ file (an orphan).

    Frames the warning around what the own-cloud sweep will do with the file and
    whether knowledge in it survives a machine-move — the user-stated concern
    that "temporary files turn into living documents" whose knowledge is lost.

    Returns (severity, data_class, description):
      - data extension (in _SESSION_DATA_EXTS): the Phase-2 fail-safe heuristic
        MIRRORS it to S3, AND — because registration is what makes a session file
        eligible for the both-diverged LOCAL-WINS auto-resolve — an unregistered
        one can wedge PERMANENTLY -> severity 'warning'.
      - signal-shaped (extensionless / unknown ext): the sweep keeps it
        MACHINE-LOCAL -> any knowledge in it is LOST on a move -> severity
        'warning'. (Also the fallback when _SESSION_DATA_EXTS could not import.)

    SEVERITY HISTORY (g-115-6352, 2026-08-16). The data branch was 'info' and its
    message named only the machine-move resume gap. That understated the actual
    consequence by a lot, and it is why 11 orphans accumulated across the fleet
    UNDER A WORKING DETECTOR — the check fired every time and read as advisory.
    The severities were also inverted relative to consequence: the signal branch
    (recoverable — the file stays local and is regenerated) was 'warning' while
    the data branch (permanently wedge-capable, no healing path) was 'info'.
    Detection was never the gap; the advice was.
    """
    suffix = Path(name).suffix.lower()
    sz = f"{size}B" if isinstance(size, int) else "unknown size"
    if _SESSION_DATA_EXTS is not None and suffix in _SESSION_DATA_EXTS:
        return ("warning", "data",
                f"Orphan DATA file '{name}' ({sz}) is unregistered in "
                f"session-manifest.yaml. Session files are SYNC-BY-DEFAULT with "
                f"that manifest as a DENYLIST, so a known data extension is "
                f"MIRRORED to S3 — but the both-diverged LOCAL-WINS auto-resolve "
                f"requires manifest registration with sync_tier "
                f"continuity|ephemeral. Unregistered means INELIGIBLE, and these "
                f"single-writer session files deliberately have no merge handler "
                f"(guard-907), so nothing heals a divergence: a both-diverged "
                f"fence on this file is a PERMANENT WEDGE, not a delayed resume. "
                f"It also will not auto-resume on a machine-move. Register it — "
                f"sync_tier=continuity if it must resume on another machine, "
                f"ephemeral if losing it is harmless, machine_local if it is "
                f"per-box state that should never sync (which removes the wedge "
                f"class outright rather than healing it).")
    return ("warning", "signal",
            f"Orphan file '{name}' ({sz}) is unregistered AND signal-shaped "
            f"(no known data extension), so the own-cloud sweep keeps it "
            f"MACHINE-LOCAL — any knowledge in it is LOST on a machine-move. "
            f"If it holds learning, register sync_tier=continuity; if it is a "
            f"liveness/marker signal, register sync_tier=machine_local to confirm.")


def _read_agent_state(files):
    """Try to read agent-state from the snapshot. Returns the value or 'UNKNOWN'."""
    rec = files.get("agent-state")
    if not rec or not rec.get("exists"):
        return "UNKNOWN"
    # The manifest records mtime + size, not content. We need to read the
    # file directly. Path comes from AGENT_DIR/session/agent-state.
    if AGENT_DIR is None:
        return "UNKNOWN"
    p = Path(AGENT_DIR) / "session" / "agent-state"
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return "UNKNOWN"


def _read_persona_active(files):
    """Read persona-active's value from disk. Returns the value or 'UNKNOWN'."""
    rec = files.get("persona-active")
    if not rec or not rec.get("exists"):
        return "UNKNOWN"
    # The manifest records mtime + size, not content — read the file directly.
    if AGENT_DIR is None:
        return "UNKNOWN"
    p = Path(AGENT_DIR) / "session" / "persona-active"
    try:
        return p.read_text(encoding="utf-8").strip().lower()
    except Exception:
        return "UNKNOWN"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=(
            "Session desync check — evaluate manifest invariants against the "
            "current snapshot, log warnings advisory-only."
        )
    )
    ap.add_argument("--output", default="human", choices=["json", "human"],
                    help="Output format.")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress stdout when zero warnings (for cron use).")
    args = ap.parse_args(argv)

    # Run session_snapshot.py as a subprocess to get JSON.
    try:
        snap = subprocess.run(
            [sys.executable, str(_HERE / "session_snapshot.py"), "--output", "json"],
            capture_output=True, text=True, check=True,
        )
        snapshot = json.loads(snap.stdout)
    except Exception as e:
        print(f"ERROR: snapshot subprocess failed: {e}", file=sys.stderr)
        sys.exit(0)  # advisory — never block

    files_by_name = {f["file"]: f for f in snapshot.get("files", [])}
    agent_state = _read_agent_state(files_by_name)

    warnings = []
    for inv in snapshot.get("invariants", []):
        inv_id = inv.get("id")
        if not inv_id:
            continue
        handler = _HANDLERS.get(inv_id)
        if handler is None:
            warnings.append({
                "id": inv_id,
                "severity": "info",
                "message": f"Unhandled invariant '{inv_id}' (no handler in session_desync_check.py)",
            })
            continue
        try:
            if handler(files_by_name, agent_state):
                warnings.append({
                    "id": inv_id,
                    "severity": inv.get("severity", "warning"),
                    "description": inv.get("description", ""),
                    "rule": inv.get("rule", ""),
                    "agent_state": agent_state,
                })
        except Exception as e:
            warnings.append({
                "id": inv_id,
                "severity": "error",
                "message": f"handler raised: {type(e).__name__}: {e}",
            })

    # Orphan files (present in session/ but not in manifest) — knowledge-loss
    # sweep: classify each by what the own-cloud sweep does with it and whether
    # its content survives a machine-move (the user's "living document" concern).
    session_dir = (Path(AGENT_DIR) / "session") if AGENT_DIR is not None else None
    for orphan in snapshot.get("orphans", []):
        # Orphans are by definition NOT in snapshot["files"] (session_snapshot.py
        # excludes manifest-registered names from the orphan scan), so there is no
        # snapshot size to reuse — stat the file directly.
        size = None
        if session_dir is not None:
            try:
                size = (session_dir / orphan).stat().st_size
            except OSError:
                size = None
        sev, data_class, desc = _classify_orphan(orphan, size)
        warnings.append({
            "id": "orphan_file",
            "severity": sev,
            "file": orphan,
            "size": size,
            "data_class": data_class,
            "description": desc,
        })

    # Log to <agent>/session/desync-warnings.jsonl (append-only, advisory).
    if warnings and AGENT_DIR is not None:
        log_path = Path(AGENT_DIR) / "session" / "desync-warnings.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                stamp = datetime.datetime.now().isoformat(timespec="seconds")
                for w in warnings:
                    entry = dict(w)
                    entry["logged_at"] = stamp
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"WARN: could not write desync-warnings.jsonl: {e}", file=sys.stderr)

    result = {
        "agent": snapshot.get("agent"),
        "agent_state": agent_state,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "warnings": warnings,
        "warning_count": len(warnings),
    }

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        if not warnings and args.quiet:
            return 0
        print(f"Agent: {result['agent']}  State: {result['agent_state']}  Warnings: {len(warnings)}")
        for w in warnings:
            sev = w.get("severity", "warning")
            wid = w.get("id", "?")
            msg = w.get("description") or w.get("message") or ""
            extra = f" (file={w['file']})" if "file" in w else ""
            print(f"  [{sev}] {wid}: {msg}{extra}")
    # Always exit 0 — advisory.
    return 0


if __name__ == "__main__":
    sys.exit(main())
