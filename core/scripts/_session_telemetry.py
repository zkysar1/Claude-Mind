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

# Agent-dir constants, never literals (CLAUDE.md "Agent-dir Resolution": a
# literal copy is invisible to the audit greps and silently resolves to nothing
# after a relocation). This module builds agent paths from an INJECTED
# project_root — the tests pass a tmp_path — so it cannot use agent_dir()/
# agent_sessions_root(), which re-derive from the real PROJECT_ROOT. Importing
# the constants keeps the injectable root AND keeps the sites greppable.
# Deliberately NOT wrapped in a literal fallback: a telemetry module that cannot
# resolve agent dirs must fail loudly at import, not mis-reap quietly.
try:
    from _paths import AGENTS_PARENT_DIR, SESSIONS_DIRNAME, SESSION_DIRNAME
except ImportError:  # pragma: no cover - direct-script import path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _paths import AGENTS_PARENT_DIR, SESSIONS_DIRNAME, SESSION_DIRNAME

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


# ── Phase 1.5: stale-active reaper (design doc §8.6 #1 + #3) ──────────────────

def _parse_local_iso(s):
    """Parse a local ISO-8601 (no-tz) timestamp to datetime, or None on any
    failure / 'unknown' (mirrors the format _now_iso_local writes)."""
    if not s or s == "unknown":
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def _runner_recently_active(agent, cutoff_dt, project_root):
    """True if an autonomous runner for `agent` ticked its heartbeat at/after
    `cutoff_dt` — i.e. the agent is (recently) alive on THIS machine.

    `project_root` is an already-resolved Path or None. The default on any
    ambiguity is True (treat as alive → do NOT reap), because reaping a live
    session's record is data corruption while leaving an orphan one cycle longer
    is harmless:
      * project_root unresolvable  → True  (cannot verify → skip)
      * heartbeat file absent      → False (no autonomous runner → reapable)
      * stat error on a present file → True  (uncertain → skip)
    The heartbeat is ticked only by the autonomous loop (Phase -0.5), so this is
    specifically "is a live autonomous runner present", which is exactly the
    case §8.6 #3 must protect (its active record may still be keyed on the
    pre-autocompact SID)."""
    if project_root is None:
        return True
    hb = (project_root / AGENTS_PARENT_DIR / agent / SESSION_DIRNAME
          / "runner-heartbeat")
    try:
        if not hb.exists():
            return False
        mtime = datetime.datetime.fromtimestamp(hb.stat().st_mtime)
        return mtime >= cutoff_dt
    except Exception:
        return True


def _body_recently_active(agent, cutoff_dt, project_root):
    """True if ANY worker Body of `agent` touched its per-session heartbeat
    at/after `cutoff_dt` — i.e. a Body is alive on THIS machine.

    WHY THIS EXISTS (g-115-6939). `_runner_recently_active` reads the AGENT-WIDE
    `runner-heartbeat`, which a worker Body is designed NEVER to write: a worker
    is `agent-state=IDLE` by design, and heartbeat-tick.sh REFUSES the agent-wide
    write on a non-RUNNING agent. So for every worker the file is ABSENT, the
    absent branch returns False ("no autonomous runner -> reapable"), and
    `reap_stale_active` flips the EXECUTING Body's live record to
    status=unknown mid-execution. Measured cc-08 2026-08-19: `reaped_ids`
    contained the executing SID. Until this predicate existed, the 24h freshness
    window was the ONLY thing protecting a live worker — protection by accident,
    not by design.

    This is the `check-team-state-before-silent` shape at the file level: an
    ABSENT signal read as evidence of death, when absence is the designed steady
    state of the population being judged.

    THE SAME-BOX SIGNAL IS THE CORRECT ONE, and the choice is load-bearing.
    heartbeat-tick.sh writes TWO per-Body heartbeats:
      * `sessions/<SID>/body-heartbeat` — same-box, pure mtime, walk-pruned from
        the sync (`sessions` is in owncloud_sync._EXCLUDE_DIRS), so it never
        leaves this machine. THIS is what we read.
      * `session/body-heartbeat-<SID>.json` — the syncable carrier. Wrong here
        twice over: its mtime does NOT survive the sync (the file is copied, so
        the timestamp is meaningless to an mtime comparison), and it can be
        written by ANOTHER machine — which would make this reaper believe a
        locally-dead agent is alive whenever it runs anywhere in the fleet, and
        defer local orphans forever.
    Reading same-box mtime is also consistent with the caller, which already
    skips records whose `machine_id` is not this machine.

    Fail-safe in the same direction as its sibling: ambiguity returns True (do
    NOT reap), because reaping a live record is data corruption while leaving an
    orphan one cycle longer is harmless."""
    if project_root is None:
        return True
    sessions_root = (project_root / AGENTS_PARENT_DIR / agent
                     / SESSIONS_DIRNAME)
    try:
        if not sessions_root.is_dir():
            return False
        for hb in sessions_root.glob("*/body-heartbeat"):
            try:
                mtime = datetime.datetime.fromtimestamp(hb.stat().st_mtime)
            except Exception:
                # A heartbeat we cannot stat is not evidence of death.
                return True
            if mtime >= cutoff_dt:
                return True
        return False
    except Exception:
        return True


def _agent_recently_active(agent, cutoff_dt, project_root):
    """True if `agent` has ANY live session on this machine — reducer OR worker.

    The predicate `reap_stale_active` actually needs. `_runner_recently_active`
    alone answers only "is a live AUTONOMOUS RUNNER present", which is a strictly
    narrower question than "is it safe to reap this agent's active records", and
    the gap between the two is the entire worker-Body population."""
    return (_runner_recently_active(agent, cutoff_dt, project_root)
            or _body_recently_active(agent, cutoff_dt, project_root))


def reap_stale_active(world_dir=None, project_root=None, freshness_hours=24,
                      liveness_hours=6, now=None, machine_id=None):
    """Phase 1.5 stale-active reaper. Flips ORPHANED ``status=active`` records to
    ``status=unknown`` / ``ended_reason=unknown`` (design doc §8.6 #1 + #3).

    A record is reaped only when ALL hold:
      * ``status == "active"`` — idempotent; a closed record is never re-touched;
      * ``machine_id`` equals this machine — cross-machine records are left to
        THAT machine's reaper (their runner liveness can't be checked locally),
        so one machine never clobbers another's live record;
      * ``started_at`` is older than ``freshness_hours`` — the §8.6 #1 grace
        window; a just-force-closed record gets time to close normally first;
      * the owning agent has NO live session on this machine — neither an
        autonomous runner (agent-wide ``runner-heartbeat``) nor any worker Body
        (``sessions/<SID>/body-heartbeat``), both by mtime against
        ``liveness_hours``. The worker half is NOT belt-and-suspenders: a worker
        is ``agent-state=IDLE`` by design and therefore NEVER writes the
        agent-wide heartbeat, so before g-115-6939 every live worker read as
        reapable and its in-flight record was corrupted mid-execution.

    The liveness guard is load-bearing, NOT belt-and-suspenders: an autonomous
    runner stays ``active`` for the WHOLE session (the record is immutable
    between write-points) and routinely runs >24h, and after autocompact its
    live record may still be keyed on the pre-rotation SID (§8.6 #3 — the
    old-SID→new-SID re-key is a separate, not-yet-built fix). Reaping on age
    alone would therefore mark a live long-running runner ``unknown`` and drop
    it from the live-sessions view. Gating on heartbeat freshness defers a live
    agent's orphans until it idles, then reaps them — never corrupting a live
    session.

    Total (never raises → returns the partial summary). Returns a dict:
    ``{scanned, reaped, skipped_fresh, skipped_live, skipped_other_machine,
    reaped_ids}``."""
    summary = {"scanned": 0, "reaped": 0, "skipped_fresh": 0,
               "skipped_live": 0, "skipped_other_machine": 0, "reaped_ids": []}
    try:
        wd = _resolve_world_dir(world_dir)
        if wd is None:
            return summary
        records_root = wd / "telemetry" / _RECORDS_SUBDIR
        if not records_root.exists():
            return summary
        pr = _resolve_project_root(project_root)
        this_machine = machine_id or _machine_id()
        now_dt = now or datetime.datetime.now()
        fresh_cutoff = now_dt - datetime.timedelta(hours=freshness_hours)
        live_cutoff = now_dt - datetime.timedelta(hours=liveness_hours)
        ended_at = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
        live_cache = {}
        for agent_dir in sorted(records_root.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent = agent_dir.name
            for rec_path in sorted(agent_dir.glob("*.json")):
                summary["scanned"] += 1
                try:
                    record = json.loads(rec_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if record.get("status") != "active":
                    continue
                rec_machine = record.get("machine_id")
                if rec_machine and rec_machine != this_machine:
                    summary["skipped_other_machine"] += 1
                    continue
                started_dt = _parse_local_iso(record.get("started_at"))
                if started_dt is not None and started_dt > fresh_cutoff:
                    summary["skipped_fresh"] += 1
                    continue
                if agent not in live_cache:
                    live_cache[agent] = _agent_recently_active(
                        agent, live_cutoff, pr)
                if live_cache[agent]:
                    summary["skipped_live"] += 1
                    continue
                record["status"] = "unknown"
                record["ended_reason"] = "unknown"
                record["ended_at"] = ended_at
                record["end_machine_id"] = this_machine
                record["duration_seconds"] = _duration_seconds(
                    record.get("started_at"), ended_at)
                record.setdefault("schema_version", SCHEMA_VERSION)
                if _atomic_dump(rec_path, record):
                    summary["reaped"] += 1
                    summary["reaped_ids"].append(
                        record.get("session_id") or rec_path.stem)
        return summary
    except Exception:
        return summary
