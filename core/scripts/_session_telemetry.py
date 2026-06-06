#!/usr/bin/env python3
# domain-leak-exempt: this is a framework instrumentation module; contains no
# product names. Session telemetry records are domain-free lifecycle data.
"""Session telemetry — durable, cross-machine per-session records.

Phase 1 of the session-telemetry design (mind_api/docs/session-telemetry-design.md).

WHAT: one immutable JSON record per Claude Code session at the LOCAL path
``<WORLD_DIR>/telemetry/session-records/<agent>/<sid>.json``. Because the record
lives under ``world/``, the existing own-cloud sweep (owncloud_sync.py) carries
it to S3 cross-machine with NO new infrastructure. There is intentionally NO DDB
or get_backend() call here — DDB active-session enrichment is Phase 2.

  WHY ``session-records`` and NOT ``sessions``: owncloud_sync.py's ``_EXCLUDE_DIRS``
  walk-prunes ANY directory basenamed ``sessions`` (it targets the ephemeral
  per-SID scratch dirs ``<agent>/sessions/<SID>/``). A telemetry dir named
  ``sessions`` would collide with that basename exclusion and be SILENTLY dropped
  from the S3 sweep — records would write locally but never sync. Caught by the
  2026-06-03 adversarial review. DO NOT rename this back to ``sessions``.

WHY a pure importable library (no argparse, no ``__main__``, no .sh wrapper):
  * The crash write-point (WP4) runs from recovery-gate.sh during recovery, when
    the daemon may be DEAD — so a daemon endpoint is unusable there.
  * A CLI-subcommand .sh wrapper would risk the no-python-cli-fallback gate.
  * Callers invoke via the sanctioned ``py -3 -c`` idiom, passing values through
    ENV VARS (guard-165 — never interpolate shell vars into the python source).

DESIGN INVARIANTS (see the design doc's 13 must-address constraints):
  * WORLD_DIR resolved via _paths.py — never hardcoded relative to PROJECT_ROOT.
  * Atomic local write (tmp + os.replace) with an OneDrive WinError-5 fallback;
    does NOT import _fileops (no .history/ snapshot or changelog spam) and does
    NOT call get_backend() (avoids the CLI-lacks-MIND_*-env fragility).
  * Every public function is total: it NEVER raises. On any error it returns
    None. Callers ALSO wrap in ``|| true`` — telemetry must never block the
    session lifecycle.
  * machine_id = MACHINE_ID or socket.gethostname(), resolved once.
  * schema_version is the integer literal 1.
  * Local system time, ISO-8601 without timezone (CLAUDE.md naming rule).
"""
import os
import sys
import json
import time
import socket
import datetime
from pathlib import Path

SCHEMA_VERSION = 1

# Telemetry records live under world/telemetry/<_RECORDS_SUBDIR>/<agent>/<sid>.json.
# MUST NOT be "sessions" — owncloud_sync.py _EXCLUDE_DIRS walk-prunes that basename,
# which would silently block the S3 sweep (see module docstring; rb 2026-06-03).
_RECORDS_SUBDIR = "session-records"

# Resolve once per process (hostname can change on DHCP/VM mid-process; pinning
# it keeps started_at and ended_at machine_id consistent within one process).
_MACHINE_ID = None


def _machine_id():
    global _MACHINE_ID
    if _MACHINE_ID is None:
        mid = os.environ.get("MACHINE_ID", "").strip()
        if not mid or mid.lower() == "unknown":
            try:
                mid = socket.gethostname()
            except Exception:
                mid = "unknown"
        _MACHINE_ID = mid or "unknown"
    return _MACHINE_ID


def _now_iso_local():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _env_id():
    return os.environ.get("ENVIRONMENT_ID", "").strip() or "ayoai-mind"


def _resolve_world_dir(world_dir=None):
    """World dir for telemetry records. Test callers pass world_dir explicitly;
    production lazily imports _paths.WORLD_DIR (deferred so a resolution failure
    on the crash path degrades to None rather than an import-time crash)."""
    if world_dir is not None:
        return Path(world_dir)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import WORLD_DIR  # type: ignore
        return Path(WORLD_DIR) if WORLD_DIR else None
    except Exception:
        return None


def _resolve_project_root(project_root=None):
    if project_root is not None:
        return Path(project_root)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import PROJECT_ROOT  # type: ignore
        return Path(PROJECT_ROOT) if PROJECT_ROOT else None
    except Exception:
        return None


def _unsafe_segment(s):
    """Reject path-traversal / separator chars in agent or sid before they are
    joined into a filesystem path. sid (a Claude SID) and agent (a validated
    agent name) are trusted inputs, so this is defense-in-depth — but it makes a
    stray '../' or separator impossible to escape the telemetry tree with."""
    return (not isinstance(s, str)) or (s in ("", ".", "..")) \
        or ("/" in s) or ("\\" in s) or ("\x00" in s)


def _record_path(agent, sid, world_dir=None):
    wd = _resolve_world_dir(world_dir)
    if wd is None:
        return None
    if _unsafe_segment(agent) or _unsafe_segment(sid):
        return None
    return wd / "telemetry" / _RECORDS_SUBDIR / agent / (sid + ".json")


def _atomic_dump(path, record):
    """Local atomic write (tmp + os.replace), OneDrive WinError-5 tolerant.
    No _fileops, no history/changelog, no get_backend(). Returns True/False."""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(record, indent=2, ensure_ascii=False)
        # pid-suffixed tmp so two processes writing the same record never share a
        # tmp path (still ends in .tmp → excluded from the S3 sweep via *.tmp).
        tmp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        for _ in range(5):
            try:
                os.replace(str(tmp), str(path))
                return True
            except OSError:
                time.sleep(0.1)
        # Fallback: direct (non-atomic) write if os.replace keeps failing
        # (OneDrive Files-On-Demand reparse points block rename — WinError 5).
        path.write_text(data, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass
        return True
    except Exception:
        # Best-effort cleanup so a failed write never strands a .tmp sibling
        # (the .tmp is excluded from the S3 sweep via *.tmp, but a local leak is
        # still untidy). tmp may be None if we failed before creating it.
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass
        return False


def _read_existing_record(agent, sid, world_dir=None):
    path = _record_path(agent, sid, world_dir)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_binding_for_synthesis(agent, sid, project_root=None):
    """Read agents/<agent>/sessions/<sid>/binding.yaml for missing-WP1 synthesis.
    Returns {started_at, mode, started_by} (any may be None) or None if absent."""
    pr = _resolve_project_root(project_root)
    if pr is None:
        return None
    binding = pr / "agents" / agent / "sessions" / sid / "binding.yaml"
    if not binding.exists():
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _session_binding import _parse_yaml_min, _coerce_str_field
        d = _parse_yaml_min(binding.read_text(encoding="utf-8"))
        return {
            "started_at": _coerce_str_field(d.get("started_at")),
            "mode": _coerce_str_field(d.get("mode")),
            "started_by": _coerce_str_field(d.get("started_by")),
        }
    except Exception:
        return None


def _duration_seconds(started_at, ended_at):
    """Seconds between two local ISO-8601 (no-tz) timestamps; -1 if uncomputable."""
    if not started_at or not ended_at or started_at == "unknown":
        return -1
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        s = datetime.datetime.strptime(started_at, fmt)
        e = datetime.datetime.strptime(ended_at, fmt)
        # Clamp to >= 0: a parseable computation that goes negative means
        # ended_at precedes started_at (cross-machine clock skew / DST fall-back).
        # Floor at 0 so a real (parseable) duration is NEVER confused with the
        # -1 "uncomputable" sentinel returned by the unparseable/unknown paths.
        return max(0, int((e - s).total_seconds()))
    except Exception:
        return -1


def write_open(sid, agent, mode, started_by, env_id=None, world_dir=None):
    """WP1 — create the initial active record. Idempotent: returns None without
    overwriting if a record for this SID already exists. Returns Path or None."""
    try:
        if not sid or not agent:
            return None
        path = _record_path(agent, sid, world_dir)
        if path is None:
            return None
        if path.exists():
            return None  # idempotency guard — do not clobber an existing record
        record = {
            "schema_version": SCHEMA_VERSION,
            "session_id": sid,
            "env_id": env_id or _env_id(),
            "agent": agent,
            "mode": mode,
            "mode_at_end": None,
            "status": "active",
            "machine_id": _machine_id(),
            "end_machine_id": None,
            "started_at": _now_iso_local(),
            "started_by": started_by,
            "ended_at": None,
            "duration_seconds": None,
            "ended_reason": None,
            "iterations_completed": 0,
            "goals_completed": 0,
            "goals_filed": 0,
            "tree_writes": 0,
        }
        return path if _atomic_dump(path, record) else None
    except Exception:
        return None


def write_close(sid, agent, status, ended_reason, mode_at_end=None,
                iterations_completed=0, goals_completed=0, goals_filed=0,
                tree_writes=0, world_dir=None, project_root=None):
    """WP2/WP3/WP5 — finalize a session record. Reads the existing open record
    (preserving started_at/env_id/machine_id); if absent, synthesizes from
    binding.yaml with wp1_missing=True so a close-only record is still captured.
    Returns Path or None. Never raises."""
    try:
        if not sid or not agent:
            return None
        path = _record_path(agent, sid, world_dir)
        if path is None:
            return None
        existing = _read_existing_record(agent, sid, world_dir)
        if existing is not None and existing.get("status") in ("completed", "crashed"):
            # Finalization is idempotent — the record is immutable once closed.
            # The FIRST close (graceful-stop / user-stop / crash) wins; a later
            # close on the same SID (e.g. a second /stop on an already
            # graceful-stopped agent, or a stray crash close) is a no-op rather
            # than clobbering ended_reason / ended_at / duration. (adversarial
            # review 2026-06-03: double-close-clobber finding.)
            return path
        if existing is None:
            existing = {}
            binding = _read_binding_for_synthesis(agent, sid, project_root)
            started_at = (binding or {}).get("started_at") or "unknown"
            record = {
                "schema_version": SCHEMA_VERSION,
                "session_id": sid,
                "env_id": _env_id(),
                "agent": agent,
                "mode": (binding or {}).get("mode"),
                "machine_id": None,
                "started_at": started_at,
                "started_by": (binding or {}).get("started_by"),
                "iterations_completed": 0,
                "goals_completed": 0,
                "goals_filed": 0,
                "tree_writes": 0,
                "wp1_missing": True,
            }
        else:
            record = dict(existing)  # preserve all open-time keys (no data loss)

        ended_at = _now_iso_local()
        record["status"] = status
        record["ended_reason"] = ended_reason
        record["ended_at"] = ended_at
        record["end_machine_id"] = _machine_id()
        record["mode_at_end"] = mode_at_end
        record["duration_seconds"] = _duration_seconds(record.get("started_at"), ended_at)
        record["iterations_completed"] = iterations_completed
        record["goals_completed"] = goals_completed
        record["goals_filed"] = goals_filed
        record["tree_writes"] = tree_writes
        record.setdefault("schema_version", SCHEMA_VERSION)
        return path if _atomic_dump(path, record) else None
    except Exception:
        return None


def write_crash(sid, agent, iterations_completed=0, world_dir=None, project_root=None):
    """WP4 — convenience wrapper enforcing crash invariants (status=crashed,
    ended_reason=recovery-gate, goals_completed=-1). Callers cannot accidentally
    record real goal counts for a crashed session whose outcome is unknown."""
    return write_close(
        sid=sid, agent=agent, status="crashed", ended_reason="recovery-gate",
        iterations_completed=iterations_completed, goals_completed=-1,
        world_dir=world_dir, project_root=project_root,
    )
