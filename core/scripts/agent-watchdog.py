#!/usr/bin/env python3
"""agent-watchdog.py — per-agent session observability probes.

WHY THIS EXISTS
---------------
Periodic diagnostic probes that watch one agent's session/ state and log
anomalies that the LLM's own loop can't catch (because the LLM can't poll
between iterations and doesn't see process-level events).

PERIODIC, NOT DAEMON
--------------------
Earlier this script ran as a detached daemon spawned by /start. That model
died on Git Bash for Windows because nohup+disown semantics there don't
reliably keep child processes alive past parent-shell exit. Even on POSIX
the daemon required PID-file lifecycle, kill ceremony, and orphan sweeps —
~250 lines of process management for a diagnostic feature.

The current model invokes this script once per iteration from
iteration-close.sh's productivity-check phase. Probe state persists across
calls via <agent>/session/watchdog-prev-state.json (atomic .tmp+replace).
No detachment, no PID file, no platform branches — pure file I/O works
identically on Windows/macOS/Linux. The trade-off: detection latency
matches iteration cadence (typically sub-minute to several minutes) instead
of the previous 5s daemon poll. Acceptable because the events being
detected (state corruption, dead background jobs, stop-hook thrash) persist
for minutes-to-hours once they fire — historical incidents (2026-04-25
alpha, 2026-05-11 bravo) stayed broken for hours.

PROBE REGISTRY
--------------
Probes are pluggable. Add a new probe by:
  1. Subclassing Probe with .name, .check(), .to_dict(), .from_dict(state).
  2. Appending it to build_probes() in this file.
  3. Adding a verify-learning Section MON check.

Active probes:
  - RunningSidProbe — running-session-id state transitions (2026-04-25 +
    2026-05-11 + 2026-05-12 incident class).
  - HeartbeatProbe — runner-heartbeat staleness during RUNNING state.
  - StalledProbe — fresh-heartbeat + stale-execution-diary wedge (loop alive
    but not progressing; the false-OK HeartbeatProbe structurally misses,
    g-328-24 / root-cause-#5 of the 2026-07-04 fleet-wedge g-328-19).
  - BackgroundJobProbe — dead-PID / max-duration in background-jobs.yaml.
  - StopHookBlockProbe — stop-hook BLOCK thrash without heartbeat
    advancement (cross-binding stomp, rb-739).
  - DaemonHealthProbe — proactive daemon-death detection + race-safe respawn
    on the tick cadence (g-240-97); guard-597 confirmation re-probe before
    declaring death so a slow-but-alive daemon is never spuriously respawned.
  - FreshnessProbe — pointer-doc freshness: deterministic content-hash
    auto-bump of stale-but-unchanged pointers; deduped Investigate goal on
    canonical drift. Replaces the clock-based recurring re-verify goal
    (logic in pointer_freshness.py).
  - GitDriftProbe — per-box ahead/behind vs origin/main, LIVE-Body carrier-ref
    unconsumed depth, and host disk used% (g-115-6128). Throttled fetch; files
    ONE box-scoped Investigate goal and retires it when the drift clears.

LOG FORMATS
-----------
Event log (core/logs/watchdog-<agent>.jsonl):
  {
    "ts": "2026-05-12T03:45:12",
    "agent": "bravo",
    "probe": "running-sid",
    "event": "deleted" | "modified" | "stale_during_running" | ...,
    "severity": "critical" | "info",
    "payload": { ...probe-specific fields... },
    "processes": "..."   # optional, only when probe asks for it
  }

Stderr: one short line per event, prefixed with "!!" for critical. When
invoked from iteration-close.sh, stderr is appended to
core/logs/iteration-close-stderr.log.

State file (<agent>/session/watchdog-prev-state.json):
  {
    "running-sid":      { "exists": bool, "mtime": float, "sid": str },
    "heartbeat":        { "last_state": "fresh|stale|missing|unknown" },
    "background-job":   { "reported": [[job_id, event_type], ...] },
    "stop-hook-block":  { "last_pos": int, "consecutive_blocks": int, ... },
    "daemon-health":    { "prev_reachable": bool|null, "consecutive_unreachable": int }
  }
First tick (file missing or corrupt) → each probe captures current state
as baseline; no events emitted. Subsequent ticks compare and emit.

USAGE
-----
Canonical: invoked once per iteration from iteration-close.sh productivity-
check. MIND_AGENT inherited from the iteration context.

Ad-hoc inspection (human or test):
  bash core/scripts/agent-watchdog.sh         # one tick, exit
  bash core/scripts/agent-watchdog.sh --once  # snapshot all probes, no diff

MIND_AGENT is REQUIRED in env. Refuses to run without it.

DESIGN NOTES
------------
- Pure file-stat polling. No inotify / ReadDirectoryChangesW dependency.
- Fail-open: any exception in a probe is caught and logged to stderr;
  other probes still run, state still saved.
- Process capture (psutil) is shared across probes via WatchdogContext —
  one process_iter() per tick, cached, only fetched when a probe asks
  (RunningSidProbe / StopHookBlockProbe critical events).
- Cross-platform: no os.fork, no nohup, no PID file. Just JSON state
  files and atomic renames.
"""

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

try:
    import psutil  # cross-platform process introspection — Windows/macOS/Linux
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Shared utilities
# ─────────────────────────────────────────────────────────────────────────────

def project_root() -> Path:
    """Resolve PROJECT_ROOT from the script location (../../ from this file)."""
    return Path(__file__).resolve().parent.parent.parent


# Import the canonical agent_dir() helper. Late import (after project_root)
# avoids load-order issues with _paths.py which uses Path.cwd-relative lookup
# in some startup paths. The helper centralizes AGENTS_PARENT_DIR resolution
# per CLAUDE.md "Agent-dir Resolution" — direct `root / "agents" / name` is
# a sync-drift landmine (fresh-eyes-code F2, 2026-05-19).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import agent_dir as _paths_agent_dir  # noqa: E402

# : never hardcode the escalation aspiration —  is the UPSTREAM
# deployment's queue and does not exist elsewhere, so a literal files nothing.
try:
    from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR  # noqa: E402
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ESCALATION_ASP, _ESCALATION_ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ESCALATION_SOURCE = _asp_source(ESCALATION_ASP, WORLD_DIR, AGENT_DIR)
except Exception:
    ESCALATION_ASP, _ESCALATION_ASP_VIA, ESCALATION_SOURCE = (
        "asp-115", "fallback:import-failed", "world")


def _box_id() -> str:
    """Stable per-BOX identity, for probes whose dedup key must be box-scoped.

    Delegates to the fleet SSOT (`_session_telemetry._machine_id`: MACHINE_ID
    env, else hostname, else "unknown") rather than re-deriving it, so a box
    that renames itself in one place renames everywhere. `reducer_self_fence`
    already carries a second variant of this resolution; do not add a third.

    TOTAL BY CONTRACT — it must never raise. Both callers invoke it OUTSIDE
    their fail-open try blocks (`_file_*_goal` computes the signal before the
    try, and `_close_*_goal` does the same), so a raise here would propagate
    out of the probe and kill the whole tick.

    The "unknown" floor deliberately re-creates the pre-g-115-6369 collision
    for boxes that cannot resolve an identity at all: two such boxes share a
    key and suppress each other. That is the FAIL-SAFE direction — it degrades
    to exactly the old behaviour rather than to something worse — but it is a
    floor, not a design goal. A box reaching it has a real configuration gap
    (no MACHINE_ID and no resolvable hostname) worth fixing at the source.
    """
    try:
        from _session_telemetry import _machine_id
        return _machine_id()
    except Exception:
        return "unknown"


def read_text_safe(p: Path) -> Optional[str]:
    """Read a file as text. Returns None if missing or unreadable."""
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def tail_lines(p: Path, n: int) -> list[str]:
    """Return last n lines of a file as stripped strings, or [] if unreadable."""
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in f.readlines()[-n:]]
    except OSError:
        return []


def tail_jsonl(p: Path, n: int) -> list[dict]:
    """Return last n parsed JSON objects from a JSONL file."""
    out = []
    for line in tail_lines(p, n):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


_PROCESS_KEYWORDS = ("bash", "python", "claude", "git", "cmd", "powershell", "sh")


def capture_processes(max_records: int = 40) -> str:
    """Cross-platform process snapshot via psutil. Returns a short text capture
    filtered to forensically-relevant processes (bash, python, claude, git).

    Two-pass strategy:
      1. process_iter(['pid', 'ppid', 'name']) — cheap on Linux/macOS (<1s),
         can be slow on Windows under AV/OneDrive scanning (10-15s observed
         before Defender exclusions; sub-second after).
      2. proc.cmdline() only on the filtered subset — fast even on Windows
         because we're hitting <100 handles instead of 400+.

    Why psutil and not shell tools:
      - tasklist /FO CSV: 20s+ timeout on the Win10 dev machine before
        Defender exclusions (suspected AV scanning of the spawned process).
      - wmic: deprecated on Win11, not always installed.
      - ps -ef: POSIX-only.
      - psutil: pure Python, works on Windows 10/11, macOS, Linux; no
        deprecation horizon. Slowness on Windows is OS-level AV tax, not
        psutil's fault.

    Fail-open: returns a stub string on any error so the watchdog keeps running.
    """
    if not _PSUTIL_AVAILABLE:
        return "(psutil not installed — `pip install psutil` to enable process capture)"
    try:
        matches = []
        for proc in psutil.process_iter(["pid", "ppid", "name"]):
            info = proc.info
            name = (info.get("name") or "").lower()
            if not name:
                continue
            if any(k in name for k in _PROCESS_KEYWORDS):
                matches.append(proc)
                if len(matches) >= max_records:
                    break
        lines = []
        for proc in matches:
            try:
                cmdline = " ".join(proc.cmdline()) if proc.is_running() else ""
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                cmdline = "<no-access>"
            info = proc.info
            cmdline = cmdline.replace("\n", " ").replace("\r", " ")[:200]
            lines.append(
                f"pid={info['pid']} ppid={info['ppid']} {info['name']}: {cmdline}"
            )
        return "\n".join(lines) or "(no matching processes)"
    except Exception as e:
        return f"capture failed: {type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Context shared across probes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WatchdogContext:
    """State shared by all probes in one watchdog process. Lives for the life
    of the watchdog. Probes mutate their own attributes on `self`; cross-probe
    state goes here.

    Process capture caching: any probe that wants a process snapshot calls
    ctx.get_processes() — first call in a cycle pays the enumeration cost,
    subsequent calls in the same cycle reuse the cache. The cache resets at
    the top of each polling cycle via ctx.new_cycle().
    """
    agent_name: str
    agent_dir: Path
    project_root_path: Path
    cycle: int = 0
    # "reducer" (the Body holding running-session-id) or "worker". Decides which
    # probes build_probes() registers — a worker's state shape makes five of them
    # structurally unable to fire (). Defaults to reducer so every
    # existing caller and test keeps its current behaviour unchanged.
    body_role: str = "reducer"
    _process_cache: Optional[str] = field(default=None, repr=False)

    def new_cycle(self) -> None:
        self.cycle += 1
        self._process_cache = None

    def get_processes(self) -> str:
        """Lazy + cached process snapshot for the current cycle. Probes that
        need forensic context call this; probes that don't, don't pay the cost."""
        if self._process_cache is None:
            self._process_cache = capture_processes()
        return self._process_cache


# ─────────────────────────────────────────────────────────────────────────────
# Event + Probe base
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Event:
    probe: str
    event: str
    severity: str  # "critical" or "info"
    payload: dict
    include_processes: bool = False  # probe asks watchdog to attach a snapshot
    summary: str = ""  # one-line stderr summary


class Probe:
    """Base class for watchdog probes.

    Contract:
      - .name: short identifier (kebab-case). Appears in event log.
      - .initialize(): one-time setup (e.g., capture initial file state). Called
                       once before the first .check(). Exceptions are fatal in
                       loop mode; in tick mode they're caught.
      - .check(): runs once per cycle. Returns a list of Events (empty on no
                  change). Exceptions are caught and logged to stderr.
      - .to_dict() / .from_dict(d): serialize probe state for tick-mode
                                    persistence across separate invocations.
                                    Default: no state to persist. Override per
                                    probe that has cross-cycle state (prev_state,
                                    counters, dedup sets, etc.).

    Probes own their own state. Cross-probe state lives in WatchdogContext.
    """
    name: str = "unnamed"

    def __init__(self, ctx: WatchdogContext) -> None:
        self.ctx = ctx

    def initialize(self) -> None:
        return None

    def check(self) -> list[Event]:
        raise NotImplementedError

    def to_dict(self) -> dict:
        """Serialize persistent state for tick mode. Default: empty (no state)."""
        return {}

    def from_dict(self, state: dict) -> None:
        """Restore persistent state from a saved dict. Default: no-op."""
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RunningSidProbe — the first registered probe (formerly running-sid-watcher)
# ─────────────────────────────────────────────────────────────────────────────

def snapshot_running_sid(p: Path) -> dict:
    """Capture current state of a running-session-id file. Never raises."""
    try:
        if not p.exists():
            return {"exists": False, "mtime": None, "sid": None}
        st = p.stat()
        sid = p.read_text(encoding="utf-8", errors="replace").strip()
        return {"exists": True, "mtime": st.st_mtime, "sid": sid}
    except OSError as e:
        return {"exists": False, "mtime": None, "sid": None, "error": str(e)}


def classify_sid_transition(old: dict, new: dict) -> Optional[str]:
    """Identify the running-session-id transition. None if no change."""
    if old["exists"] and not new["exists"]:
        return "deleted"
    if not old["exists"] and new["exists"]:
        return "created"
    if old["exists"] and new["exists"]:
        old_mtime = old["mtime"] or 0
        new_mtime = new["mtime"] or 0
        if abs(new_mtime - old_mtime) > 0.001:
            return "modified"
        if (old.get("sid") or "") != (new.get("sid") or ""):
            return "content_changed_no_mtime"
    return None


class RunningSidProbe(Probe):
    """Watches <agent>/session/running-session-id for unexplained transitions.

    The canonical bug-class trigger: deletion while agent-state=RUNNING. The
    recovery-gate's Path B has fired on this twice (2026-04-25 alpha,
    2026-05-11 bravo); both times the original deleter remained unidentified
    because no watcher was running at the moment. This probe is the active
    instrumentation that catches it on the next occurrence.

    All non-deletion events are logged as "info" severity (created at /start,
    modified at session-save-id during compact, deleted at /stop graceful-stop).
    """
    name = "running-sid"

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.sid_path = ctx.agent_dir / "session" / "running-session-id"
        self.prev_state: dict = {"exists": False, "mtime": None, "sid": None}

    def initialize(self) -> None:
        self.prev_state = snapshot_running_sid(self.sid_path)

    def check(self) -> list[Event]:
        new = snapshot_running_sid(self.sid_path)
        transition = classify_sid_transition(self.prev_state, new)
        if transition is None:
            return []

        severity = self._classify_severity(transition)
        agent_state = read_text_safe(self.ctx.agent_dir / "session" / "agent-state")

        payload = {
            "prev": {"sid": self.prev_state.get("sid"), "mtime": self.prev_state.get("mtime")},
            "new": {"sid": new.get("sid"), "mtime": new.get("mtime")},
            "correlated": self._correlated_context(agent_state),
        }
        prev_sid = (self.prev_state.get("sid") or "")[:8] or "-"
        new_sid = (new.get("sid") or "")[:8] or "-"
        summary = (
            f"{self.name}: {transition} "
            f"(prev={prev_sid} new={new_sid} state={agent_state})"
        )

        event = Event(
            probe=self.name,
            event=transition,
            severity=severity,
            payload=payload,
            include_processes=True,
            summary=summary,
        )
        self.prev_state = new
        return [event]

    def _classify_severity(self, transition: str) -> str:
        """Deletion while RUNNING is the canonical bug. Everything else is
        normal lifecycle."""
        if transition != "deleted":
            return "info"
        state = read_text_safe(self.ctx.agent_dir / "session" / "agent-state") or ""
        return "critical" if state.strip() == "RUNNING" else "info"

    def _correlated_context(self, agent_state: Optional[str]) -> dict:
        """Snapshot related state at the moment of an event."""
        session_dir = self.ctx.agent_dir / "session"
        root = self.ctx.project_root_path
        return {
            "agent_state": agent_state,
            "latest_sid": read_text_safe(session_dir / "latest-session-id"),
            # 2026-05-12 hardening Tier 3a: include framework-owned uniqueness
            # token. SID-collision shows up as "same SID, different
            # runner_token" between consecutive RunningSidProbe events.
            "runner_token": read_text_safe(session_dir / "runner-token"),
            "stop_requested_exists": (session_dir / "stop-requested").exists(),
            "stop_loop_exists": (session_dir / "stop-loop").exists(),
            "compact_pending_exists": (session_dir / "compact-pending").exists(),
            # 2026-05-19 (plan v1 step 0.15): stop-hook log relocated to core/logs/.
            # Read new path if present; legacy path otherwise so transition
            # period still surfaces tail for diagnosis.
            "stop_hook_tail": tail_lines(
                root / "core" / "logs" / "stop-hook.log"
                if (root / "core" / "logs" / "stop-hook.log").exists()
                else root / ".stop-hook-log",
                5,
            ),
            "recent_recovery_log": tail_jsonl(session_dir / "recovery-log.jsonl", 2),
        }

    def to_dict(self) -> dict:
        return dict(self.prev_state)

    def from_dict(self, state: dict) -> None:
        if state:
            self.prev_state = {
                "exists": bool(state.get("exists", False)),
                "mtime": state.get("mtime"),
                "sid": state.get("sid"),
            }


# ─────────────────────────────────────────────────────────────────────────────
# HeartbeatProbe — runner liveness watcher
# ─────────────────────────────────────────────────────────────────────────────

def _heartbeat_stale_threshold_seconds() -> float:
    """Read runner_heartbeat.stale_minutes from aspirations.yaml. Returns the
    threshold in seconds. Falls back to 30 minutes when the config is missing
    or malformed — same fallback shape as heartbeat-stale.sh's error stderr."""
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "aspirations.yaml"
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        m = ((cfg.get("runner_heartbeat") or {}).get("stale_minutes"))
        if isinstance(m, (int, float)) and m > 0:
            return float(m) * 60.0
    except Exception:
        pass
    return 30.0 * 60.0


class HeartbeatProbe(Probe):
    """Watches <agent>/session/runner-heartbeat mtime. Emits an event when the
    heartbeat ages past the staleness threshold while agent-state=RUNNING. A
    stale heartbeat under RUNNING means the loop is wedged or the runner died;
    recovery-gate.sh / heartbeat-stale.sh check the same threshold from the
    SessionStart hook side, but this probe catches it during an active session
    when no SessionStart event has fired.

    Transitions:
      - stale_during_running (critical): mtime crossed threshold while
        agent-state=RUNNING. Caller may use this to escalate / notify.
      - heartbeat_recovered (info): mtime returned to fresh after a prior
        stale event — runner came back to life (e.g., a long goal finished).
      - heartbeat_missing (info): file does not exist. Logged once per probe
        lifetime; common during /stop transitions or right at /start before
        the first iteration ticks.

    Dedup is by state. Once stale_during_running fires, the probe waits for
    a fresh observation before it will fire stale again (treats stale→fresh
    as a recovery transition that resets the dedup gate).
    """

    name = "heartbeat"

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.hb_path = ctx.agent_dir / "session" / "runner-heartbeat"
        self.state_path = ctx.agent_dir / "session" / "agent-state"
        self.threshold_seconds = _heartbeat_stale_threshold_seconds()
        self.last_state: str = "unknown"   # one of: fresh, stale, missing, unknown

    def check(self) -> list[Event]:
        agent_state = read_text_safe(self.state_path) or ""
        agent_state = agent_state.strip()
        events: list[Event] = []

        if not self.hb_path.exists():
            if self.last_state != "missing":
                self.last_state = "missing"
                events.append(self._build_event(
                    "heartbeat_missing",
                    severity="info",
                    payload={"agent_state": agent_state, "path": str(self.hb_path)},
                ))
            return events

        try:
            mtime = self.hb_path.stat().st_mtime
        except OSError:
            return events

        age_seconds = time.time() - mtime
        is_stale = age_seconds > self.threshold_seconds

        if is_stale:
            if agent_state == "RUNNING" and self.last_state != "stale":
                self.last_state = "stale"
                events.append(self._build_event(
                    "stale_during_running",
                    severity="critical",
                    payload={
                        "agent_state": agent_state,
                        "age_seconds": round(age_seconds, 1),
                        "threshold_seconds": self.threshold_seconds,
                        "heartbeat_mtime": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                    },
                ))
            elif agent_state != "RUNNING":
                # Stale heartbeat while IDLE is expected — the runner stopped.
                # Track state without emitting noise.
                self.last_state = "stale"
        else:
            if self.last_state == "stale":
                events.append(self._build_event(
                    "heartbeat_recovered",
                    severity="info",
                    payload={
                        "agent_state": agent_state,
                        "age_seconds": round(age_seconds, 1),
                        "heartbeat_mtime": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                    },
                ))
            self.last_state = "fresh"

        return events

    def _build_event(self, event_type: str, severity: str, payload: dict) -> Event:
        summary = (
            f"{self.name}: {event_type} "
            f"(state={payload.get('agent_state','?')} "
            f"age={payload.get('age_seconds','-')}s "
            f"threshold={int(self.threshold_seconds)}s)"
        )
        return Event(
            probe=self.name,
            event=event_type,
            severity=severity,
            payload=payload,
            include_processes=(severity == "critical"),
            summary=summary,
        )

    def to_dict(self) -> dict:
        return {"last_state": self.last_state}

    def from_dict(self, state: dict) -> None:
        if state and "last_state" in state:
            self.last_state = str(state["last_state"])


# ─────────────────────────────────────────────────────────────────────────────
# ClaimHeartbeatProbe — the reader for claim-heartbeat-failure ()
# ─────────────────────────────────────────────────────────────────────────────

def _claim_stale_window_seconds() -> float:
    """The claim's ownership-stale window, in seconds.

    Deliberately the SAME source heartbeat-tick.sh uses for its own banner
    (`OWNERSHIP_STALE_SECONDS`, default 3900), so writer and reader agree about
    when an outage has entered the window in which a peer may legally seize the
    claim.

    NOT an identity, and an earlier draft of this docstring wrongly claimed the
    two "can never disagree". They agree for unset and for any positive integer,
    which is every real case. They diverge on values bash accepts and this guard
    rejects — `0`, negatives, and floats: `${OWNERSHIP_STALE_SECONDS:-3900}` keeps
    a literal `0` while `isdigit() and >0` falls back to 3900 here (and bash `$((
    ))` would itself fail on a float). Rejecting those is the right call for a
    reader, since a 0-second window would make every marker instantly critical —
    but say so rather than asserting an identity that does not hold.
    """
    raw = (os.environ.get("OWNERSHIP_STALE_SECONDS") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return float(raw)
    return 3900.0


def parse_claim_heartbeat_marker(text: Optional[str]) -> Optional[dict]:
    """Parse heartbeat-tick.sh's `key=value` marker. None when absent or unusable.

    Mirrors reducer_self_fence.read_failure_elapsed's tolerance deliberately: a
    corrupt or half-written marker must never be read as a LONG outage, so a
    non-numeric first_failed_at yields None rather than a guess.
    """
    if not text:
        return None
    fields: dict = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        fields[k.strip()] = v.strip()
    first = fields.get("first_failed_at", "")
    if not first.isdigit():
        return None
    count = fields.get("count", "")
    return {
        "first_failed_at": int(first),
        "count": int(count) if count.isdigit() else 0,
        "last_rc": fields.get("last_rc", ""),
        "last_error": fields.get("last_error", ""),
    }


class ClaimHeartbeatProbe(Probe):
    """Surfaces <agent>/session/claim-heartbeat-failure, which had NO reader.

    heartbeat-tick.sh (g-306-221) writes this marker when the DDB runner-claim
    heartbeat leg fails, and escalates to a loud stderr banner past half the
    ownership-stale window. But guard-772: stderr is invisible when the tick runs
    inside a backgrounded Bash call, which is the normal case — so on the box
    where it matters most (an unattended reducer) the durable marker was the only
    surviving evidence, and nothing looked at it.

    THIS PROBE MUST NEVER DELETE THE MARKER, and that is the load-bearing
    constraint rather than a style note. reducer_self_fence.read_failure_elapsed
    treats an ABSENT marker as "the last renewal SUCCEEDED" and returns 0, so a
    CONSUMING reader would reset first_failed_at at every read, elapsed could
    never accumulate, and the sustained-renewal-gap stepdown could NEVER fire —
    the gate that stops a box acting as reducer after it has lost the claim (the
    2026-08-05 dual-reducer incident this marker family exists to catch). This is
    also why /prime is the wrong host: its recovery-notice precedent is `cat + rm`.
    guard-2760 states the general form — the marker already has a consumer whose
    remedy is DESTRUCTIVE (step the reducer down); this one is deliberately
    REVERSIBLE, it reports and does not act.

    Three transitions per episode:
      - claim_heartbeat_failing (info): marker present, outage under way.
      - claim_heartbeat_stepdown_window (critical): elapsed reached half the
        ownership-stale window, so a peer may soon legally take the claim while
        this box keeps running. Same threshold expression as the writer's banner.
      - claim_heartbeat_recovered (info): marker gone after a prior failure.

    The MIDDLE transition is the point: a lone appear-time event on a 30-minute
    outage leaves the reader with a notice from half an hour ago and silence since.

    Worker-inert BY CONSTRUCTION: "claim-heartbeat" is absent from
    WORKER_SAFE_PROBES, so build_probes filters it out on a worker Body — where
    heartbeat-tick refuses on IDLE before ever reaching the DDB leg and the marker
    is sync_tier machine_local. Four probes are already worker-inert and a fifth
    should not arrive unannounced.
    """

    name = "claim-heartbeat"

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.marker_path = ctx.agent_dir / "session" / "claim-heartbeat-failure"
        self.stale_window_seconds = _claim_stale_window_seconds()
        # absent | failing | stepdown_window. Persisted via to_dict/from_dict:
        # --tick is a FRESH PROCESS each iteration, so "emit once per transition"
        # lives entirely in serialization. An in-process-only dedup would test
        # green and still spam every iteration in production.
        self.last_state: str = "absent"
        # Dedup for the unreadable-marker warning, kept SEPARATE from last_state
        # on purpose: an unreadable read must not overwrite our memory of whether
        # an outage was under way, or unreadable->absent would emit a recovery we
        # never observed (and absent->unreadable->absent would invent one).
        self.last_unreadable: bool = False

    def check(self) -> list[Event]:
        events: list[Event] = []
        # read_text_safe never unlinks. Do not "simplify" this to a consuming
        # read — see the class docstring.
        parsed = parse_claim_heartbeat_marker(read_text_safe(self.marker_path))

        if parsed is None and self.marker_path.exists():
            # PRESENT but unreadable or unparseable. read_text_safe collapses
            # "missing" and "unreadable" into the same None, and reducer_self_fence
            # deliberately reads both as "renewal succeeded" — correct THERE, because
            # its remedy is destructive (stepdown) and must not fire on ambiguity.
            # This probe only REPORTS, so the fail-safe direction INVERTS: silence on
            # ambiguity is the exact failure mode the probe exists to end. Surface it.
            # exists() is sound here specifically because this marker is sync_tier
            # machine_local (session-manifest.yaml) — guard-980's "never stat the
            # own-cloud mirror" does not apply, so do not "fix" this to a backend read.
            if not self.last_unreadable:
                events.append(Event(
                    probe=self.name,
                    event="claim_heartbeat_unreadable",
                    # "info", not "warn"/"warning": the declared vocabulary is
                    # exactly {"critical", "info"} (Event, ~L287) and the renderer
                    # treats every non-"critical" value identically. Informational
                    # is also the honest level — this says "I cannot tell", not
                    # "an outage is confirmed".
                    severity="info",
                    payload={"path": str(self.marker_path)},
                    summary=(
                        f"{self.name}: claim-heartbeat marker is PRESENT but "
                        f"unreadable/unparseable — a renewal outage may be in "
                        f"progress and invisible; check permissions and the file"
                    ),
                ))
            self.last_unreadable = True
            return events            # last_state deliberately UNCHANGED
        self.last_unreadable = False

        if parsed is None:
            if self.last_state != "absent":
                events.append(Event(
                    probe=self.name,
                    event="claim_heartbeat_recovered",
                    severity="info",
                    payload={"path": str(self.marker_path)},
                    summary=f"{self.name}: claim heartbeat recovered — marker cleared",
                ))
            self.last_state = "absent"
            return events

        elapsed = max(0, int(time.time()) - parsed["first_failed_at"])
        phase = ("stepdown_window" if elapsed >= self.stale_window_seconds / 2.0
                 else "failing")

        if phase != self.last_state:
            payload = {
                "elapsed_seconds": elapsed,
                "count": parsed["count"],
                "last_rc": parsed["last_rc"],
                "last_error": parsed["last_error"],
                "stale_window_seconds": int(self.stale_window_seconds),
                "path": str(self.marker_path),
            }
            if phase == "stepdown_window":
                events.append(Event(
                    probe=self.name,
                    event="claim_heartbeat_stepdown_window",
                    severity="critical",
                    payload=payload,
                    include_processes=True,
                    summary=(
                        f"{self.name}: CLAIM HEARTBEAT FAILING {elapsed}s "
                        f"({parsed['count']} consecutive) — at "
                        f"{int(self.stale_window_seconds)}s a peer /start will see a "
                        f"STALE claim and come up as a SECOND REDUCER while this one "
                        f"keeps running"
                    ),
                ))
            else:
                events.append(Event(
                    probe=self.name,
                    event="claim_heartbeat_failing",
                    severity="info",
                    payload=payload,
                    summary=(
                        f"{self.name}: DDB claim heartbeat failing {elapsed}s "
                        f"({parsed['count']} consecutive, rc={parsed['last_rc']}) — "
                        f"claim ages out at {int(self.stale_window_seconds)}s"
                    ),
                ))
        self.last_state = phase
        return events

    def to_dict(self) -> dict:
        # BOTH fields must persist. --tick is a fresh PROCESS per iteration, so
        # every "emit once" this probe promises lives entirely here; a field kept
        # only in memory dedups perfectly in-process and spams every iteration in
        # production, which is precisely the mutation MU4 caught for last_state.
        return {"last_state": self.last_state,
                "last_unreadable": self.last_unreadable}

    def from_dict(self, state: dict) -> None:
        if state and "last_state" in state:
            self.last_state = str(state["last_state"])
        if state and "last_unreadable" in state:
            self.last_unreadable = bool(state["last_unreadable"])


# ─────────────────────────────────────────────────────────────────────────────
# StalledProbe — fresh-heartbeat + frozen-diary wedge detector ()
# ─────────────────────────────────────────────────────────────────────────────

def _diary_stale_threshold_seconds() -> float:
    """Read runner_heartbeat.stalled_diary_stale_minutes from aspirations.yaml.
    Returns the threshold in seconds. Env override STALLED_DIARY_STALE_MINUTES
    (used by tests). Falls back to 180 minutes (3h) when config + env are both
    missing or malformed.

    INVARIANT (mirrors the g-328-25 wedge_stale > stale invariant): this
    threshold MUST exceed the heartbeat stale threshold
    (runner_heartbeat.stale_minutes, default 60). WHY: a FRESH heartbeat and a
    stale diary can only COEXIST when the diary threshold is the LARGER of the
    two. Deep-close LLM work legitimately freezes the diary AND ages the
    heartbeat together for 30-45 min (see the aspirations.yaml runner_heartbeat
    comment). If the diary threshold were <= the heartbeat threshold, a diary
    stale past it would imply a heartbeat also stale past ITS threshold, so
    classify_stalled's fresh-heartbeat gate fails first and the STALLED verdict
    is INERT. Only a genuine wedge — heartbeat re-ticked FRESH (loop iterating)
    while the diary stays frozen far longer — presents fresh-heartbeat WITH
    diary-stale > this threshold. 180 = 3x the 60-min heartbeat window, well
    above the 45-min deep-close gap. Guarded by
    test_agent_watchdog_stalled.py::test_config_invariant_diary_stale_exceeds_heartbeat_stale."""
    env = os.environ.get("STALLED_DIARY_STALE_MINUTES")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v * 60.0
        except ValueError:
            pass
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "aspirations.yaml"
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        m = ((cfg.get("runner_heartbeat") or {}).get("stalled_diary_stale_minutes"))
        if isinstance(m, (int, float)) and m > 0:
            return float(m) * 60.0
    except Exception:
        pass
    return 180.0 * 60.0


def classify_stalled(
    agent_state: str,
    heartbeat_age_s: Optional[float],
    diary_age_s: Optional[float],
    heartbeat_stale_threshold_s: float,
    diary_stale_threshold_s: float,
) -> Optional[str]:
    """Pure classification of the fresh-heartbeat + stale-diary wedge signature.

    Returns:
      "stalled"  — agent RUNNING, heartbeat FRESH (age <= heartbeat threshold =
                   the loop is actively re-ticking each iteration = process
                   alive), AND diary STALE (age > diary threshold = no
                   goal-execution progress). The conjunction is the wedge:
                   alive but not progressing.
      "progress" — agent RUNNING, heartbeat fresh, diary fresh (age <= diary
                   threshold). Normal healthy state; used to reset the STALLED
                   dedup gate when progress resumes.
      None       — not classifiable: not RUNNING (a stale diary while IDLE is
                   expected — the runner stopped), heartbeat missing or STALE (a
                   stale heartbeat is HeartbeatProbe's stale_during_running, NOT
                   this wedge), or a signal file is absent (fresh session before
                   the first goal executes).

    Note the asymmetry with HeartbeatProbe: THAT probe fires on a STALE heartbeat
    (loop died / wedged with no re-tick). THIS classifier fires on a FRESH
    heartbeat paired with a frozen diary (loop alive + re-ticking but not
    progressing) — the false-OK HeartbeatProbe structurally cannot see (a fresh
    heartbeat sets its last_state='fresh' and it emits nothing). g-328-24 /
    root-cause-#5 of the 2026-07-04 own-cloud fleet-wedge (g-328-19), where
    own-cloud write deadlock froze goal execution for DAYS while the loop kept
    re-entering and ticking the heartbeat, and the watchdog reported OK the whole
    time because it only ever looked at heartbeat freshness.

    The fresh-heartbeat gate is checked FIRST (mirrors the g-328-25 ordering:
    fresh-heartbeat gate before the stale-progress check) so a stale-heartbeat
    reading can never be mislabelled STALLED."""
    if agent_state != "RUNNING":
        return None
    if heartbeat_age_s is None or diary_age_s is None:
        return None
    # Heartbeat must be FRESH — a stale heartbeat is a dead / wedged-without-retick
    # loop (HeartbeatProbe's job), not the alive-but-stalled signature.
    if heartbeat_age_s > heartbeat_stale_threshold_s:
        return None
    # Strict greater (mirrors phase-wedge's strict-greater boundary): at exactly
    # the threshold the diary is not YET stalled.
    if diary_age_s > diary_stale_threshold_s:
        return "stalled"
    return "progress"


class StalledProbe(Probe):
    """Watches for the WEDGE signature the HeartbeatProbe structurally misses:
    a FRESH runner-heartbeat (the loop is alive and re-ticking every iteration)
    paired with a STALE execution-diary (no goal-execution progress for far
    longer than any legitimate gap). The conjunction is the fingerprint of a
    loop that is spinning but not progressing — root cause #5 of the 2026-07-04
    own-cloud fleet-wedge (g-328-19), where own-cloud write deadlock froze goal
    execution for DAYS while the loop kept re-entering and ticking the heartbeat,
    and the watchdog reported OK the whole time because every probe only looked
    at heartbeat freshness.

    Relationship to sibling detectors:
      - HeartbeatProbe fires on a STALE heartbeat (loop died / stopped ticking).
        A FRESH heartbeat makes it emit nothing — the false-OK this probe closes.
      - recovery-gate Path D (phase-wedge-check.py, g-328-23) catches the wedge
        sub-case where a phase_start is left UNCLOSED, but only from the
        SessionStart hook on the NEXT session. This probe runs every watchdog
        tick (in-session) and fires on TOTAL diary freeze regardless of whether
        a phase is open — catching the between-goals freeze (diary's last entry
        a clean phase_end) that Path D's unclosed-phase_start detector misses.

    Transitions (state-deduped, mirroring HeartbeatProbe):
      - stalled_during_running (critical): entered the STALLED state — fresh
        heartbeat + diary stale beyond threshold while RUNNING. Fires once per
        episode; re-arms only after a fresh-diary (progress) observation.
      - stall_recovered (info): diary advanced after a prior stalled event —
        progress resumed.
    """

    name = "stalled"

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.hb_path = ctx.agent_dir / "session" / "runner-heartbeat"
        self.diary_path = ctx.agent_dir / "session" / "execution-diary.jsonl"
        self.state_path = ctx.agent_dir / "session" / "agent-state"
        self.heartbeat_threshold_seconds = _heartbeat_stale_threshold_seconds()
        self.diary_threshold_seconds = _diary_stale_threshold_seconds()
        self.last_state: str = "unknown"   # one of: ok, stalled, unknown

    def _mtime_age(self, p: Path) -> Optional[float]:
        """Seconds since p was last modified, or None if missing/unreadable."""
        try:
            if not p.exists():
                return None
            return time.time() - p.stat().st_mtime
        except OSError:
            return None

    def check(self) -> list[Event]:
        agent_state = (read_text_safe(self.state_path) or "").strip()
        hb_age = self._mtime_age(self.hb_path)
        diary_age = self._mtime_age(self.diary_path)
        verdict = classify_stalled(
            agent_state, hb_age, diary_age,
            self.heartbeat_threshold_seconds, self.diary_threshold_seconds,
        )
        events: list[Event] = []
        if verdict == "stalled":
            if self.last_state != "stalled":
                self.last_state = "stalled"
                events.append(self._build_stalled_event(hb_age, diary_age, agent_state))
        elif verdict == "progress":
            if self.last_state == "stalled":
                events.append(Event(
                    probe=self.name,
                    event="stall_recovered",
                    severity="info",
                    payload={
                        "agent_state": agent_state,
                        "diary_age_seconds": round(diary_age, 1) if diary_age is not None else None,
                    },
                    include_processes=False,
                    summary=f"{self.name}: stall_recovered (diary advanced, progress resumed)",
                ))
            self.last_state = "ok"
        # verdict is None -> not classifiable (IDLE / missing signal / stale
        # heartbeat) -> leave last_state untouched, emit nothing.
        return events

    def _build_stalled_event(self, hb_age: Optional[float], diary_age: Optional[float],
                             agent_state: str) -> Event:
        try:
            diary_mtime_iso = datetime.fromtimestamp(
                self.diary_path.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            diary_mtime_iso = None
        payload = {
            "agent_state": agent_state,
            "heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
            "diary_age_seconds": round(diary_age, 1) if diary_age is not None else None,
            "heartbeat_stale_threshold_seconds": self.heartbeat_threshold_seconds,
            "diary_stale_threshold_seconds": self.diary_threshold_seconds,
            "diary_mtime": diary_mtime_iso,
            "diary_path": str(self.diary_path),
        }
        hb_s = round(hb_age, 1) if hb_age is not None else "-"
        diary_s = round(diary_age, 1) if diary_age is not None else "-"
        summary = (
            f"{self.name}: stalled_during_running "
            f"(heartbeat FRESH age={hb_s}s < {int(self.heartbeat_threshold_seconds)}s, "
            f"diary STALE age={diary_s}s > {int(self.diary_threshold_seconds)}s) "
            f"— loop alive but not progressing"
        )
        return Event(
            probe=self.name,
            event="stalled_during_running",
            severity="critical",
            payload=payload,
            include_processes=True,
            summary=summary,
        )

    def to_dict(self) -> dict:
        return {"last_state": self.last_state}

    def from_dict(self, state: dict) -> None:
        if state and "last_state" in state:
            self.last_state = str(state["last_state"])


# ─────────────────────────────────────────────────────────────────────────────
# BackgroundJobProbe — leftover-job detector
# ─────────────────────────────────────────────────────────────────────────────

def _probe_pid_alive(pid: Optional[int]) -> bool:
    """Lightweight PID liveness check for probes. False positives (live process
    reported dead) yield noisier logs but are harmless — the probe never kills
    anything. The canonical liveness check with WMI fallback lives in
    background-jobs.py; this is a smaller copy used only for observability."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class BackgroundJobProbe(Probe):
    """Watches <agent>/session/background-jobs.yaml for stale entries.

    Two failure shapes:

      1. **dead_pid** — a registered job's PID is no longer alive but the YAML
         still lists it as running. The worker exited (clean or crashed) without
         the registering caller deregistering. Leaves bookkeeping that blocks
         /stop's Gate 2 and recovery-gate's Cond 4 from accepting the agent as
         idle, even though no real work is running.
      2. **max_duration_exceeded** — launched_at is older than the job's
         metadata.max_duration_hours (or the default ceiling). The worker is
         either hung or wildly slower than its caller assumed.

    Each (job_id, event_type) tuple is reported at most once per probe
    lifetime. When a job is deregistered (no longer in the YAML), its dedup
    keys are forgotten so a re-registration is reportable again.
    """

    name = "background-job"
    DEFAULT_MAX_DURATION_HOURS = 8.0  # ceiling when metadata does not declare one

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.bg_path = ctx.agent_dir / "session" / "background-jobs.yaml"
        self.reported: set = set()  # (job_id, event_type) tuples already emitted

    def check(self) -> list[Event]:
        if not self.bg_path.exists():
            if self.reported:
                self.reported.clear()
            return []
        try:
            data = yaml.safe_load(self.bg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return []
        jobs = data.get("jobs") or []
        events: list[Event] = []
        live_ids: set = set()
        for job in jobs:
            jid = job.get("job_id")
            if not jid:
                continue
            live_ids.add(jid)
            pid = job.get("pid")
            if pid is not None and not _probe_pid_alive(pid):
                key = (jid, "dead_pid")
                if key not in self.reported:
                    self.reported.add(key)
                    events.append(self._build_event(job, "dead_pid"))
            launched = job.get("launched_at")
            metadata = job.get("metadata") or {}
            max_h = metadata.get("max_duration_hours") or self.DEFAULT_MAX_DURATION_HOURS
            try:
                elapsed_h = (datetime.now() - datetime.fromisoformat(launched)).total_seconds() / 3600
                if elapsed_h > max_h:
                    key = (jid, "max_duration_exceeded")
                    if key not in self.reported:
                        self.reported.add(key)
                        events.append(self._build_event(
                            job,
                            "max_duration_exceeded",
                            extra={"elapsed_hours": round(elapsed_h, 2), "max_duration_hours": max_h},
                        ))
            except (ValueError, TypeError):
                pass
        # Forget reports for jobs that have been deregistered.
        self.reported = {k for k in self.reported if k[0] in live_ids}
        return events

    def _build_event(self, job: dict, event_type: str, extra: Optional[dict] = None) -> Event:
        payload = {
            "job_id": job.get("job_id"),
            "type": job.get("type"),
            "pid": job.get("pid"),
            "goal_id": job.get("goal_id"),
            "launched_at": job.get("launched_at"),
        }
        if extra:
            payload.update(extra)
        severity = "critical" if event_type == "dead_pid" else "info"
        summary = (
            f"{self.name}: {event_type} job={job.get('job_id')} "
            f"pid={job.get('pid')} type={job.get('type')}"
        )
        return Event(
            probe=self.name,
            event=event_type,
            severity=severity,
            payload=payload,
            include_processes=False,
            summary=summary,
        )

    def to_dict(self) -> dict:
        # Convert set of tuples to list-of-lists for JSON.
        return {"reported": [list(t) for t in self.reported]}

    def from_dict(self, state: dict) -> None:
        if state and "reported" in state:
            try:
                self.reported = {tuple(item) for item in state["reported"]}
            except (TypeError, ValueError):
                self.reported = set()


# ─────────────────────────────────────────────────────────────────────────────
# StopHookBlockProbe — cross-binding stomp / loop-thrash detector
# ─────────────────────────────────────────────────────────────────────────────

class StopHookBlockProbe(Probe):
    """Tails core/logs/stop-hook.log for BLOCK events bound to this agent's
    runner SID and emits a 'frequency_anomaly' event when N consecutive BLOCKs
    accumulate without intervening heartbeat advancement (i.e., without proof
    that the loop produced an iteration).

    Canonical trigger: cross-binding stomp (rb-739, g-115-492, 2026-05-09).
    An observer or partner-agent session overwrites the runner's
    running-session-id; the stop hook keeps firing BLOCK against the
    no-longer-bound runner SID; the BLOCK cadence looks healthy from the log
    side but heartbeat freezes, no diary entries advance, no goal completes.
    This probe is the active detector for that shape: BLOCK count rising
    while heartbeat mtime is frozen = thrashing.

    Progress signal: <agent>/session/runner-heartbeat mtime. Phase -0.5
    heartbeat-tick.sh advances it once per iteration. If heartbeat mtime has
    not moved between BLOCK observations, no iteration ran — those BLOCKs
    are pure thrash.

    Episode dedup: at most one frequency_anomaly per (runner_sid, episode).
    Episode resets on (a) runner_sid change, (b) heartbeat advancement.
    """

    name = "stop-hook-block"
    DEFAULT_THRESHOLD = 5

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        # 2026-05-19 (plan v1 step 0.15): stop-hook log relocated to core/logs/.
        # Prefer new path; fall back to legacy PROJECT_ROOT path so probes
        # don't go blind during the transition window before Phase 1.5 cleanup.
        new_log = ctx.project_root_path / "core" / "logs" / "stop-hook.log"
        legacy_log = ctx.project_root_path / ".stop-hook-log"
        self.log_path = new_log if new_log.exists() else legacy_log
        self.runner_sid_path = ctx.agent_dir / "session" / "running-session-id"
        self.heartbeat_path = ctx.agent_dir / "session" / "runner-heartbeat"
        self.last_pos: int = 0
        self.consecutive_blocks: int = 0
        self.last_heartbeat_mtime: float = 0.0
        self.last_runner_sid: Optional[str] = None
        self.anomaly_emitted_for_sid: Optional[str] = None

    def initialize(self) -> None:
        try:
            self.last_pos = self.log_path.stat().st_size if self.log_path.exists() else 0
        except OSError:
            self.last_pos = 0
        try:
            self.last_heartbeat_mtime = (
                self.heartbeat_path.stat().st_mtime if self.heartbeat_path.exists() else 0.0
            )
        except OSError:
            self.last_heartbeat_mtime = 0.0
        self.last_runner_sid = self._current_runner_sid()

    def _current_runner_sid(self) -> Optional[str]:
        try:
            if not self.runner_sid_path.exists():
                return None
            return self.runner_sid_path.read_text(encoding="utf-8", errors="replace").strip() or None
        except OSError:
            return None

    def _current_heartbeat_mtime(self) -> float:
        try:
            return self.heartbeat_path.stat().st_mtime if self.heartbeat_path.exists() else 0.0
        except OSError:
            return 0.0

    def _read_new_lines(self) -> list[str]:
        if not self.log_path.exists():
            return []
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return []
        if size < self.last_pos:
            self.last_pos = 0
        if size == self.last_pos:
            return []
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.last_pos)
                chunk = f.read()
                self.last_pos = f.tell()
        except OSError:
            return []
        return [line for line in chunk.splitlines() if line.strip()]

    def check(self) -> list[Event]:
        runner_sid = self._current_runner_sid()
        if runner_sid != self.last_runner_sid:
            self.consecutive_blocks = 0
            self.anomaly_emitted_for_sid = None
            self.last_runner_sid = runner_sid

        if runner_sid is None:
            self._read_new_lines()
            return []

        heartbeat_mtime = self._current_heartbeat_mtime()
        if heartbeat_mtime > self.last_heartbeat_mtime:
            self.consecutive_blocks = 0
            self.last_heartbeat_mtime = heartbeat_mtime
            self.anomaly_emitted_for_sid = None

        agent_filter = f"agent={self.ctx.agent_name}"
        sid_filter = f"sid={runner_sid}"
        for line in self._read_new_lines():
            if " BLOCK " not in line:
                continue
            if agent_filter not in line:
                continue
            if sid_filter not in line:
                continue
            self.consecutive_blocks += 1

        events: list[Event] = []
        if (
            self.consecutive_blocks >= self.DEFAULT_THRESHOLD
            and self.anomaly_emitted_for_sid != runner_sid
        ):
            self.anomaly_emitted_for_sid = runner_sid
            hb_iso = (
                datetime.fromtimestamp(self.last_heartbeat_mtime).isoformat(timespec="seconds")
                if self.last_heartbeat_mtime
                else None
            )
            short_sid = runner_sid[:8] if runner_sid else "?"
            summary = (
                f"{self.name}: frequency_anomaly "
                f"blocks={self.consecutive_blocks} sid={short_sid}..."
            )
            events.append(Event(
                probe=self.name,
                event="frequency_anomaly",
                severity="critical",
                payload={
                    "consecutive_blocks": self.consecutive_blocks,
                    "runner_sid": runner_sid,
                    "threshold": self.DEFAULT_THRESHOLD,
                    "last_heartbeat_mtime": hb_iso,
                    "log_path": str(self.log_path),
                },
                include_processes=True,
                summary=summary,
            ))
        return events

    def to_dict(self) -> dict:
        return {
            "last_pos": self.last_pos,
            "consecutive_blocks": self.consecutive_blocks,
            "last_heartbeat_mtime": self.last_heartbeat_mtime,
            "last_runner_sid": self.last_runner_sid,
            "anomaly_emitted_for_sid": self.anomaly_emitted_for_sid,
        }

    def from_dict(self, state: dict) -> None:
        if not state:
            return
        self.last_pos = int(state.get("last_pos", 0) or 0)
        self.consecutive_blocks = int(state.get("consecutive_blocks", 0) or 0)
        self.last_heartbeat_mtime = float(state.get("last_heartbeat_mtime", 0.0) or 0.0)
        self.last_runner_sid = state.get("last_runner_sid")
        self.anomaly_emitted_for_sid = state.get("anomaly_emitted_for_sid")


# ─────────────────────────────────────────────────────────────────────────────
# DaemonHealthProbe — proactive daemon-death detection + respawn ()
# ─────────────────────────────────────────────────────────────────────────────

def _rt_port_file(root: Path) -> Path:
    """Resolve the daemon port file exactly as _runtime.sh does
    (RT_PORT_FILE, else RT_DIR/daemon.port, else
    PROJECT_ROOT/mind_api/state/daemon.port). Mirrors _runtime.sh lines 33-35
    and honors the same env overrides so a relocated state dir stays in sync."""
    pf = os.environ.get("RT_PORT_FILE")
    if pf:
        return Path(pf)
    rt_dir = os.environ.get("RT_DIR")
    base = Path(rt_dir) if rt_dir else (root / "mind_api" / "state")
    return base / "daemon.port"


def daemon_health_probe(root: Path, timeout: float = 1.0) -> bool:
    """Pure-Python faithful replica of _runtime.sh rt_is_up: read the port
    file, GET http://127.0.0.1:<port>/v1/admin/health, return True iff HTTP
    2xx within `timeout` seconds (mirrors curl --max-time 1).

    WHY pure Python and not `bash -c 'source _runtime.sh && rt_is_up'`: on
    Windows, subprocess "bash" resolves to WSL bash (rb-225/rb-247), which
    cannot read the Windows RT_PORT_FILE and would false-negative — a
    spurious respawn against a live daemon (the exact guard-597 failure).
    This is the FREQUENT path (every watchdog tick), so it must be
    lottery-free and stays in-process. The coupling to _runtime.sh is only
    three things — port-file path (env-mirrored above), endpoint, and
    timeout — pinned by this comment. Monkeypatched in tests to simulate
    up/down without a real daemon."""
    try:
        port = _rt_port_file(root).read_text(encoding="utf-8").strip()
    except OSError:
        # No port file == daemon not running (matches rt_base_url empty -> rt_is_up rc1).
        return False
    if not port:
        return False
    url = f"http://127.0.0.1:{port}/v1/admin/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def daemon_respawn(root: Path) -> dict:
    """Delegate respawn to _runtime.sh rt_ensure_running. NOT reimplemented:
    rt_ensure_running owns the wrapper-side spawn mutex (rt_acquire_spawn_lock),
    the already-up early-return, the detached rt_spawn, and rt_wait_for_ready.
    Reimplementing any of that in Python would duplicate the race-safety
    surface (implementation-discipline rule 3) and lose the mutex that prevents
    multi-daemon launches.

    Bash resolution: RT_BASH env (if a Git Bash invoker exports it) else "bash".
    The WSL-bash lottery (rb-225/rb-247) is contained here by (a) this being
    the RARE path — reached only on guard-597-confirmed death — and (b) the
    caller re-probing health (daemon_health_probe) afterward for the
    authoritative outcome, so a wrong-bash subprocess can never produce a
    false 'respawned' claim. Returns a result dict; never raises.
    Monkeypatched in tests so no real daemon is spawned."""
    bash = os.environ.get("RT_BASH") or "bash"
    try:
        r = subprocess.run(
            [bash, "-c", "source core/scripts/_runtime.sh && rt_ensure_running"],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
        return {"attempted": True, "subprocess_rc": r.returncode,
                "detail": ((r.stderr or "") + (r.stdout or "")).strip()[-300:]}
    except subprocess.TimeoutExpired:
        return {"attempted": True, "subprocess_rc": None,
                "detail": "rt_ensure_running timed out (>60s)"}
    except OSError as e:
        return {"attempted": True, "subprocess_rc": None,
                "detail": f"{type(e).__name__}: {e}"}


class DaemonHealthProbe(Probe):
    """Pings the daemon health endpoint on the watchdog tick cadence and
    triggers a race-safe respawn when it is confirmed dead — so death is
    caught proactively instead of by the unlucky next wrapper call (g-240-97).

    Unlike the transition-based probes above (running-sid, background-job),
    daemon-down is an ABSOLUTE state, not a transition: a dead daemon is worth
    respawning regardless of whether this is the first observation. So this
    probe does NOT capture an initialize() baseline — a dead daemon on the
    very first tick is reported and respawned.

    guard-597 compliance: a SINGLE rt_is_up timeout does NOT establish death.
    OneDrive write-lock contention can stall a live daemon past the 1s probe
    (retry storms up to ~22s observed). On the first 'down' reading the probe
    runs CONFIRM_PROBES additional health probes; only when ALL of them also
    fail is the daemon treated as genuinely dead. A daemon that answers on any
    re-probe is logged once as daemon_slow (info) and NOT respawned —
    respawning a live-but-slow daemon is the precise false-positive guard-597
    exists to prevent.

    Respawn is delegated to rt_ensure_running (see daemon_respawn) and its
    success is VERIFIED by a fresh pure-Python health probe, so the emitted
    event reports the real post-respawn state, not a subprocess exit code.

    Observe-only: when RT_NO_AUTOSPAWN=1 the probe still detects + emits but
    does not respawn (mirrors rt_ensure_running's own opt-out; lets the
    daemon-down test suite exercise detection without spawning a daemon).

    Cross-tick state: prev_reachable (last settled reachability — None until
    the first reading) and consecutive_unreachable (count of confirmed-dead
    ticks; reset on recovery)."""

    name = "daemon-health"
    CONFIRM_PROBES = 2     # extra health probes after the first 'down' before declaring death
    CONFIRM_GAP_S = 0.5    # brief pause between confirmation probes

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.prev_reachable: Optional[bool] = None
        self.consecutive_unreachable: int = 0

    def check(self) -> list[Event]:
        root = self.ctx.project_root_path
        no_autospawn = os.environ.get("RT_NO_AUTOSPAWN") == "1"

        if daemon_health_probe(root):
            events: list[Event] = []
            if self.prev_reachable is False:
                events.append(Event(
                    probe=self.name, event="daemon_recovered", severity="info",
                    payload={"cleared_consecutive_unreachable": self.consecutive_unreachable},
                    summary=f"{self.name}: daemon_recovered (was unreachable x{self.consecutive_unreachable})",
                ))
            self.prev_reachable = True
            self.consecutive_unreachable = 0
            return events

        # First 'down' reading — guard-597 confirmation re-probe.
        confirmed_down = True
        for _ in range(self.CONFIRM_PROBES):
            time.sleep(self.CONFIRM_GAP_S)
            if daemon_health_probe(root):
                confirmed_down = False
                break

        if not confirmed_down:
            events = []
            if self.prev_reachable is False:
                # Was confirmed dead, now answering on re-probe — recovery.
                events.append(Event(
                    probe=self.name, event="daemon_recovered", severity="info",
                    payload={"cleared_consecutive_unreachable": self.consecutive_unreachable,
                             "note": "answered on guard-597 confirmation re-probe"},
                    summary=f"{self.name}: daemon_recovered (answered on re-probe)",
                ))
            else:
                # Previously alive (or first tick) — a transient slow blip.
                events.append(Event(
                    probe=self.name, event="daemon_slow", severity="info",
                    payload={"note": "rt_is_up timed out once then answered on re-probe — "
                                     "slow-but-alive, no respawn (guard-597)"},
                    summary=f"{self.name}: daemon_slow (timeout then alive on re-probe — no respawn)",
                ))
            self.prev_reachable = True
            self.consecutive_unreachable = 0
            return events

        # Confirmed dead across the initial probe + CONFIRM_PROBES re-probes.
        self.consecutive_unreachable += 1
        if no_autospawn:
            respawn = {"attempted": False, "reason": "RT_NO_AUTOSPAWN=1 — observe-only",
                       "verified_up": False}
        else:
            respawn = daemon_respawn(root)
            # Authoritative post-probe: the subprocess rc alone is not trusted
            # (a wrong-bash subprocess could exit 0 without spawning).
            respawn["verified_up"] = daemon_health_probe(root)

        event = Event(
            probe=self.name, event="daemon_unreachable", severity="critical",
            payload={
                "confirmation_probes": self.CONFIRM_PROBES,
                "consecutive_unreachable": self.consecutive_unreachable,
                "respawn": respawn,
            },
            include_processes=True,
            summary=(f"{self.name}: daemon_unreachable "
                     f"(confirmed x{1 + self.CONFIRM_PROBES}) "
                     f"respawn_verified_up={respawn.get('verified_up', False)}"),
        )
        # Settle reachability from the VERIFIED post-probe. If respawn brought
        # it up, the next tick sees up and won't re-emit; if still down,
        # prev_reachable stays False so the next tick re-confirms and
        # re-attempts (death persists → keep trying).
        self.prev_reachable = bool(respawn.get("verified_up", False))
        return [event]

    def to_dict(self) -> dict:
        return {"prev_reachable": self.prev_reachable,
                "consecutive_unreachable": self.consecutive_unreachable}

    def from_dict(self, state: dict) -> None:
        if state:
            self.prev_reachable = state.get("prev_reachable", None)
            self.consecutive_unreachable = int(state.get("consecutive_unreachable", 0) or 0)


# ─────────────────────────────────────────────────────────────────────────────
# Event log writer
# ─────────────────────────────────────────────────────────────────────────────

def write_log_entry(log_path: Path, entry: dict) -> None:
    """Append entry as a single JSON line."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def emit_event(ctx: WatchdogContext, log_path: Path, event: Event) -> None:
    """Build the JSONL record from a probe Event and append it. Stderr gets a
    short summary line per event."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "agent": ctx.agent_name,
        "probe": event.probe,
        "event": event.event,
        "severity": event.severity,
        "payload": event.payload,
    }
    if event.include_processes:
        entry["processes"] = ctx.get_processes()
    write_log_entry(log_path, entry)

    marker = "!!" if event.severity == "critical" else "  "
    sys.stderr.write(f"{marker} [{entry['ts']}] {ctx.agent_name} {event.summary}\n")
    sys.stderr.flush()


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog modes
# ─────────────────────────────────────────────────────────────────────────────

class FreshnessProbe(Probe):
    """Pointer-doc freshness: auto-bumps stale-but-unchanged pointer docs and
    files a deduped Investigate goal when a tracked canonical has drifted.

    Heavy logic lives in pointer_freshness.py (lazy-imported inside check() so a
    syntax error there can never crash the watchdog tick). Transition semantics:
    a drift / canonical_missing / error event is emitted only when a pointer's
    status or canonical hash changes from the prior tick, so a persistent drift
    logs once per episode rather than every tick. Goal filing is deduped
    independently inside scan() via an open-goal scan, so the Investigate goal is
    filed at most once regardless of event emission.

    Unlike the liveness probes, first-tick ACTION (auto-bump / drift-escalation)
    is the intended work, not a false alarm -- so initialize() starts from an
    empty baseline instead of capturing current-as-prev.
    """
    name = "freshness"

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.prev: dict = {}  # {pointer_path: {"status": str, "hash": str|None}}

    def initialize(self) -> None:
        self.prev = {}

    def _world_dir(self):
        try:
            from _paths import WORLD_DIR  # type: ignore
            return WORLD_DIR
        except Exception:
            return None

    def check(self) -> list[Event]:
        world_dir = self._world_dir()
        if not world_dir:
            return []
        try:
            from pointer_freshness import scan as freshness_scan  # type: ignore
        except Exception as e:
            sys.stderr.write(
                f"agent-watchdog: freshness probe import failed: "
                f"{type(e).__name__}: {e}\n"
            )
            return []
        out = freshness_scan(
            world_dir,
            agent_dir=self.ctx.agent_dir,
            project_root=self.ctx.project_root_path,
            dry_run=False,
        )
        events: list[Event] = []
        new_prev: dict = {}
        for r in out["results"]:
            path = r["path"]
            status = r["status"]
            cur_hash = r.get("current_hash")
            new_prev[path] = {"status": status, "hash": cur_hash}
            prior = self.prev.get(path, {})
            changed = (prior.get("status") != status) or (prior.get("hash") != cur_hash)
            if status == "bumped":
                events.append(Event(
                    probe=self.name, event="auto_bumped", severity="info",
                    payload={"pointer": path, "age_days": r["age_days"],
                             "max_age_days": r["max_age_days"]},
                    summary=(f"freshness: auto-bumped {r['slug']} "
                             f"(was {r['age_days']}d stale, canonical unchanged)"),
                ))
            elif status == "drift" and changed:
                if r["goal_filed"]:
                    tail = f" (filed {r['goal_id']})"
                elif r["dedup_skipped"]:
                    tail = " (goal already open)"
                else:
                    tail = " (goal-file FAILED)"
                events.append(Event(
                    probe=self.name, event="drift", severity="info",
                    payload={"pointer": path, "canonical": r["canonical"],
                             "recorded_hash": r["recorded_hash"],
                             "current_hash": cur_hash, "goal_filed": r["goal_filed"],
                             "goal_id": r["goal_id"], "dedup_skipped": r["dedup_skipped"],
                             "error": r["error"]},
                    summary=f"freshness: DRIFT {r['slug']} -- canonical changed{tail}",
                ))
            elif status == "canonical_missing" and changed:
                events.append(Event(
                    probe=self.name, event="canonical_missing", severity="info",
                    payload={"pointer": path, "canonical": r["canonical"]},
                    summary=f"freshness: canonical missing for {r['slug']} ({r['canonical']})",
                ))
            elif status == "error" and changed:
                events.append(Event(
                    probe=self.name, event="probe_error", severity="info",
                    payload={"pointer": path, "error": r["error"]},
                    summary=f"freshness: error on {r['slug']}: {r['error']}",
                ))
        self.prev = new_prev
        return events

    def to_dict(self) -> dict:
        return {"prev": self.prev}

    def from_dict(self, state: dict) -> None:
        # `or {}` coalesces BOTH missing-key and explicit-null ({"prev": null}
        # from a torn write) to {} -- without it self.prev=None permanently
        # breaks check() (self.prev.get raises AttributeError every tick).
        self.prev = (state.get("prev") or {}) if isinstance(state, dict) else {}


class MirrorWedgeProbe(Probe):
    """Own-cloud mirror-wedge visibility (): classifies the sweep's
    conflict-streaks artifact (mirror_health.probe()) every tick. A both-
    diverged conflict silently freezes a file's mirror refresh — this box
    served days-stale world reads for ~21h (30 files, g-115-2548 wedge)
    because the skip only appeared in spawn.log. Advisory: the REPAIR is the
    g-115-2548 /reconcile-owncloud-conflicts protocol; this probe surfaces
    the condition where the agent looks (watchdog event log + a deduped
    Investigate goal).

    Transitions (state-deduped, mirroring StalledProbe episodes):
      - mirror_wedged (critical): verdict=wedged on WEDGED_TICKS_TO_FILE
        consecutive ticks. Emits once per episode AND files an Investigate
        goal deduped by origin_signal (pointer_freshness.open_goal_exists
        scan — at most one open goal regardless of episodes).
      - mirror_wedge_cleared (info): healthy after a fired episode.
    'unknown' verdicts (sweep not running / not own-cloud) neither advance
    nor reset the streak — absence of signal is not health (guard-980 class).
    Heavy logic lazy-imports inside check() so a mirror_health syntax error
    can never crash the watchdog tick (FreshnessProbe convention).
    """

    name = "mirror-wedge"
    WEDGED_TICKS_TO_FILE = 2
    # Re-validation interval for the `fired` episode latch ().
    #
    # `fired` caches "an open goal already covers this box's wedge". It is
    # written False ONLY on the `healthy` branch below, so while a wedge
    # PERSISTS it never writes the negative and the cache can never invalidate
    # -- a latch by guard-4870's definition ("a store written ONLY inside its
    # own fire condition"). That is how cc-04 reached consecutive_wedged=981
    # with fired=true and ZERO open wedge goals: the covering goal was gone, the
    # box stayed wedged, and `not self.fired` short-circuited BEFORE
    # _file_wedge_goal()'s own open_goal_exists() dedup could ever run. The box
    # was left wedged, un-goaled, and un-refileable.
    #
    # Every N wedged ticks the latch re-opens and the filer re-decides. The
    # store read stays INSIDE _file_wedge_goal() where it already lives; this
    # deliberately does NOT move a store read into the fire decision. It is
    # self-limiting: if a goal IS open the filer returns dedup and `fired` goes
    # straight back to True for another N ticks. It cannot double-dispatch live
    # work (guard-1125's hazard) because the filer's dedup, not this counter,
    # decides whether anything is actually filed.
    #
    # A close-path re-arm was considered and REJECTED: _close_wedge_goal() runs
    # only on the `healthy` branch, which by definition never executes while the
    # wedge persists, so it cannot break a latch the wedge is holding open
    # (rb-6714 -- a completion signal that gates its own next cycle cannot
    # recover "next cycle").
    #
    # Why 50, stated in full because a constant must not smuggle its real reason
    # (guard-3560): (a) it bounds un-goaled wedge time to at most 50 ticks after
    # a covering goal disappears, against a condition measured persisting 981
    # ticks unnoticed; (b) it keeps the extra open_goal_exists() read to one per
    # 50 wedged ticks rather than one per tick; (c) it is 10x the longest tick
    # window any existing test in test_mirror_health.py drives (max 5), so those
    # tests keep asserting the SHORT-window semantics they were written for --
    # fires once, retries until landed, dedup does not respam -- unmodified.
    # (c) is a real reason and is disclosed rather than hidden. No test asserts
    # this value literally; the latch test reads it off the class, so correcting
    # the number never requires editing a test (guard-3560 step 3).
    WEDGED_TICKS_TO_REVALIDATE = 50

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.consecutive_wedged = 0
        self.fired = False

    def check(self) -> list[Event]:
        try:
            import mirror_health
            verdict = mirror_health.probe()
        except Exception as e:  # noqa: BLE001 — probe must never kill the tick
            sys.stderr.write(f"[mirror-wedge] probe error (skipped): {e}\n")
            return []
        events: list[Event] = []
        v = verdict.get("verdict")
        if v == "wedged":
            self.consecutive_wedged += 1
            if self.consecutive_wedged >= self.WEDGED_TICKS_TO_FILE and (
                    not self.fired
                    or self.consecutive_wedged % self.WEDGED_TICKS_TO_REVALIDATE == 0
            ):
                goal = self._file_wedge_goal(verdict)
                # Mark the episode fired ONLY when the goal actually landed
                # (). Setting fired=True unconditionally (before this
                # fix) meant a single filing FAILURE — a gate block that the
                # --override-duplication above now prevents, or a transient
                # daemon error — permanently lost the wedge goal for the whole
                # episode while the freeze persisted (observed: working-memory.yaml
                # frozen ~411 sweeps, filed:false 2026-07-18 + 2026-07-20, never
                # queued). Leaving fired=False on failure re-attempts the filing
                # on the next wedged tick; the Event still emits each attempt so a
                # persistent failure stays loud rather than silent. The dedup
                # return (an open wedge goal ALREADY covers this box) also counts
                # as covered — else a wedge that clears then reappears while the
                # prior goal is still open would re-emit a critical event EVERY
                # tick ( review). ONLY a genuine no-goal failure keeps
                # fired=False so it retries.
                if goal.get("filed") or goal.get("dedup"):
                    self.fired = True
                events.append(Event(
                    probe=self.name, event="mirror_wedged", severity="critical",
                    payload={"wedged_count": verdict.get("wedged_count"),
                             "files": verdict.get("files"),
                             "consecutive_ticks": self.consecutive_wedged,
                             "goal": goal},
                    summary=(f"mirror WEDGED: {verdict.get('wedged_count')} file(s) "
                             f"both-diverged {self.consecutive_wedged} ticks — "
                             f"stale reads until reconciled ({goal.get('goal_id') or goal.get('error')})"),
                ))
        elif v == "healthy":
            # Deliberately NOT gated on self.fired. `fired` is per-episode and
            # lives in watchdog-prev-state.json, which is box-local and
            # ephemeral — a reset or a fresh box zeroes it, and a goal filed by
            # the previous episode would then be unclosable by this mechanism
            # forever. That is the same "filed, never closed" defect one level
            # up, so the close has to be able to run without it. Cost is one
            # local JSONL read per tick (no daemon, early-returns on no match);
            # the subprocess only runs when a closable goal actually exists.
            closed = self._close_wedge_goal()
            if self.fired or closed.get("closed"):
                events.append(Event(
                    probe=self.name, event="mirror_wedge_cleared", severity="info",
                    payload={"after_ticks": self.consecutive_wedged,
                             "closed": closed},
                    summary=("mirror wedge cleared — conflict streaks drained"
                             + (f" ({closed.get('detail')})" if closed.get("detail") else "")),
                ))
            self.consecutive_wedged = 0
            self.fired = False
        # 'unknown': no live signal — hold state unchanged.
        return events

    def _origin_signal(self) -> str:
        """Dedup key for this probe's escalation goal. ONE builder, two callers.

        BOX-scoped, and now genuinely so: the wedge is BOX-LOCAL state, the
        repair must run ON the wedged box, and a box-agnostic key would let box
        A's open goal mask box B's simultaneous wedge.

        WHY THIS IS A METHOD RATHER THAN A LITERAL (g-115-6369): the key was
        previously written out twice — once here and once in `_close_wedge_goal`
        — and guard-3419 makes those two the SAME feature, because the file path
        takes a lease and the close path is the only thing that releases it. Two
        literals can drift apart silently, and the failure mode of that drift is
        the worst one available: goals that can be filed and never closed, i.e.
        a permanently disabled detector. GitDriftProbe already had the builder;
        this probe did not. Do not inline this back.

        WHY IT NEEDED A BOX COMPONENT AT ALL, since the old comment claimed to
        be box-scoped and was not lying when it was written: commit a6093f817
        ("box-scope the mirror-wedge Investigate signal") moved this key from
        FLEET-global to AGENT-scoped on 2026-07-18, when one agent ran on one
        box and the agent name was a faithful box proxy. The Mind/Body split
        (first live worker Body ~2026-08-05) broke that equivalence, and NOTHING
        FAILED — the code kept doing exactly what it always did; only the world
        changed underneath it. The comment silently became a false guarantee.
        """
        return (f"investigate:mirror-wedge-detected-"
                f"{self.ctx.agent_name}-on-{_box_id()}")

    def _file_wedge_goal(self, verdict: dict) -> dict:
        """File one deduped Investigate goal. Fail-open ({filed: False, error})."""
        origin_signal = self._origin_signal()
        # Name the BOX, not just the agent (). The dedup key
        # (_origin_signal) is already BOX-scoped, but the title was only
        # AGENT-scoped, so ONE agent running on TWO boxes produced two
        # byte-identical titles for two genuinely different wedges — a reader
        # could not tell which box to repair, and cc-04's wedge was
        # indistinguishable from any other alpha wedge in a title scan.
        # guard-860 ("keep run-specific identifiers OUT of titles") points the
        # OTHER way and does not apply: its hazard is a SHARED id causing a
        # false MATCH between unrelated goals; here the box id makes
        # otherwise-identical titles DISTINCT.
        # Computed HERE, outside the fail-open try, so _box_id()'s documented
        # "both callers invoke it outside their try" invariant stays true.
        box = _box_id()
        try:
            from _paths import WORLD_DIR
            import importlib
            pf = importlib.import_module("pointer_freshness")
            if pf.open_goal_exists(origin_signal, WORLD_DIR, self.ctx.agent_dir):
                # dedup=True: an open wedge goal ALREADY covers this box, so the
                # episode is covered even though we filed nothing NOW. check()
                # treats this like a success (fired=True) — a genuine no-goal
                # FAILURE has no dedup key and retries. ( review)
                return {"filed": False, "dedup": True, "goal_id": None,
                        "error": "open goal exists (dedup)"}
            files = sorted((verdict.get("files") or {}).items())
            listing = "; ".join(f"{p} ({n} sweeps)" for p, n in files[:8])
            body = {
                "title": (f"Investigate: own-cloud mirror wedge on {self.ctx.agent_name}'s box "
                          f"[{box}] — both-diverged conflict-skips frozen"),
                "priority": "HIGH",
                "participants": ["agent"],
                "description": (
                    f"The watchdog mirror-wedge probe on {self.ctx.agent_name}'s box "
                    f"[{box}] found "
                    f"{verdict.get('wedged_count')} "
                    f"file(s) both-diverged for >= {mh_threshold()} consecutive sweeps "
                    f"across {self.consecutive_wedged}+ watchdog ticks — their mirror "
                    f"refresh is frozen and every consumer on THAT box reads stale data "
                    f"(repair must run ON BOX {box}, {self.ctx.agent_name}'s box) "
                    f"(the g-115-2548 class; 21h undetected last time). Files: {listing}. "
                    f"Run: bash core/scripts/mirror-health.sh for the live list, then "
                    f"repair via the /reconcile-owncloud-conflicts protocol "
                    f"(per-file direction decision + manifest rebaseline; see "
                    f"g-115-2548 + world board msg-20260718-002816). Auto-filed by "
                    f"agent-watchdog MirrorWedgeProbe (g-115-2549)."
                ),
                "category": "framework-infrastructure",
                "origin_signal": origin_signal,
                # Box-scoped repair must reach the box that can perform it
                # ( defect 2, measured by zeta 2026-08-11). Without
                # this, routing falls through to `category`, which is
                # box-agnostic: every peer's wedge goal landed on ONE agent
                # (filed_by bravo/alpha/echo/foxtrot -> intended_agent zeta in
                # all four cases) who could only ever repair their own box,
                # while HIGH + role_affinity kept those goals at the top of that
                # agent's selector. Maximally attractive and structurally
                # unfinishable is the worst combination for a dedup lease — it
                # is why these aged for 7-17 days instead of closing.
                "intended_agent": self.ctx.agent_name,
            }
            from _runtime_bash import BASH as _bash
            # --override-duplication (): this probe ALREADY owns exact
            # box-scoped dedup via open_goal_exists(origin_signal) above (at most
            # one OPEN wedge goal per box). The goal-dup-gate's fuzzy keyword
            # check additionally false-positives on the wedge goal's generic
            # owncloud/mirror tokens — and does so MOST during incident-heavy
            # windows when overlapping owncloud goals sit in-queue, i.e. exactly
            # when a wedge is most likely. Without the override the auto-file was
            # silently defeated (filed:false observed 2026-07-18 + 2026-07-20 on
            # zeta's box while working-memory.yaml stayed frozen ~411 sweeps).
            # The exact origin_signal dedup above remains the real guard; this
            # bypasses only the redundant fuzzy layer. Same idiom as
            # stale-sentinel-canary.py:336 / cargo-cult-detector.py:239.
            _override_reason = (
                "MirrorWedgeProbe owns exact box-scoped dedup via "
                "open_goal_exists(origin_signal); the goal-dup-gate keyword "
                "check false-positives on generic owncloud/mirror tokens during "
                "incident-heavy windows, silently defeating the wedge auto-file "
                "(g-115-2803).")
            proc = subprocess.run(
                [_bash, "core/scripts/aspirations-add-goal.sh", ESCALATION_ASP,
                 "--source", ESCALATION_SOURCE,
                 "--override-duplication", _override_reason],
                input=json.dumps(body, ensure_ascii=True),
                capture_output=True, text=True,
                cwd=str(self.ctx.project_root_path), timeout=60,
            )
            if proc.returncode != 0:
                return {"filed": False, "goal_id": None,
                        "error": (proc.stderr or proc.stdout or "non-zero exit").strip()[:200]}
            try:
                goal_id = json.loads(proc.stdout).get("id")
            except (json.JSONDecodeError, AttributeError):
                goal_id = None
            return {"filed": True, "goal_id": goal_id, "error": None}
        except Exception as e:  # noqa: BLE001 — filing failure must not kill the event
            return {"filed": False, "goal_id": None, "error": f"{type(e).__name__}: {e}"}

    def _close_wedge_goal(self) -> dict:
        """Retire the goal THIS probe filed, once the wedge it describes is gone.

        The probe filed and never closed, so its goals outlived their condition:
        measured 2026-08-11, three were open fleet-wide at 7, 14 and 17 days,
        and this box's own log shows the wedge cleared 4h after filing
        (mirror_wedged 2026-08-04T03:50:37 -> mirror_wedge_cleared 07:50:52).
        Detection was never lost -- the critical event still emits on the dedup
        path -- but the queue kept a HIGH goal whose description is a snapshot of
        the OLD episode's file list and sweep counts, and a later wedge silently
        reuses it, so whoever picks it up is handed the wrong evidence.

        Closed as `skipped`, never `completed`: the investigation did not happen,
        the need evaporated. Calling that completion would buy a completion-rate
        point with a false claim.

        Two guards, both narrowing, because auto-mutating queue state from a
        background probe must only ever touch what this probe itself created:
          - origin_signal equality -- box-scoped via the shared _origin_signal()
            builder, so only goals THIS probe filed on THIS box match. A
            hand-filed wedge goal is untouched. Both this path and the file path
            MUST read the key from that one builder: they are the take and the
            release of a single lease (guard-3419), and a key they disagree on
            is a goal that can be filed and never closed.
          - status == pending AND no claimed_by -- never yank work a partner (or
            this agent) has started. guard-1007: route via the board instead.
            An in-progress goal is therefore left open BY DESIGN; its owner
            closes it, and this method reports that rather than forcing it.
        Fail-open throughout: a close failure degrades to a stale goal, which is
        the status quo ante, and must never kill the tick.
        """
        origin_signal = self._origin_signal()
        try:
            from _paths import WORLD_DIR
            import importlib
            pf = importlib.import_module("pointer_freshness")
            open_goals = pf.open_goal_records(origin_signal, WORLD_DIR, self.ctx.agent_dir)
            if not open_goals:
                return {"attempted": False, "detail": None}
            closed, held = [], []
            from _runtime_bash import BASH as _bash
            for g in open_goals:
                gid = g.get("id")
                if not gid:
                    continue
                if g.get("status") != "pending" or g.get("claimed_by"):
                    held.append(f"{gid}:{g.get('status')}"
                                f"{'/claimed' if g.get('claimed_by') else ''}")
                    continue
                # Word this as an OBSERVATION, never as "auto-closed". It is written
                # BEFORE the status write (so a partial failure leaves the goal open
                # WITH an explanation) — which means a note asserting the close would
                # be a false claim sitting on a still-open goal in exactly the case
                # the write-order exists to protect. What IS true at this instant is
                # the mirror-health verdict; state that, and let `status` carry the
                # close. ( fresh-eyes.)
                note = (f"agent-watchdog MirrorWedgeProbe observed mirror-health "
                        f"healthy on {self.ctx.agent_name}'s box, so the wedge this "
                        f"goal was filed for is gone"
                        + (f" (cleared after {self.consecutive_wedged} wedged tick(s))"
                           if self.consecutive_wedged else "")
                        + ". No investigation was performed — the condition resolved on "
                          "its own. The probe is retiring this goal as `skipped`; if the "
                          "status still reads open, that write did not land and the goal "
                          "is safe to close by hand (g-115-4868).")
                ok = True
                for field, value in (("outcome_note", note), ("status", "skipped")):
                    proc = subprocess.run(
                        [_bash, "core/scripts/aspirations-update-goal.sh", gid,
                         field, value, "--source", g.get("_source", "world")],
                        capture_output=True, text=True,
                        cwd=str(self.ctx.project_root_path), timeout=60,
                    )
                    if proc.returncode != 0:
                        held.append(f"{gid}:close-failed")
                        ok = False
                        break
                if ok:
                    closed.append(gid)
            parts = []
            if closed:
                parts.append("closed " + ",".join(closed))
            if held:
                parts.append("held " + ",".join(held))
            return {"attempted": True, "closed": closed, "held": held,
                    "detail": "; ".join(parts) or None}
        except Exception as e:  # noqa: BLE001 — a close failure must not kill the tick
            return {"attempted": True, "closed": [], "held": [],
                    "detail": f"error: {type(e).__name__}: {e}"}

    def to_dict(self) -> dict:
        return {"consecutive_wedged": self.consecutive_wedged, "fired": self.fired}

    def from_dict(self, state: dict) -> None:
        if isinstance(state, dict):
            self.consecutive_wedged = int(state.get("consecutive_wedged") or 0)
            self.fired = bool(state.get("fired"))


def mh_threshold() -> int:
    """Probe threshold for goal text — import-guarded so a mirror_health
    import error degrades to the documented default rather than crashing."""
    try:
        import mirror_health
        return mirror_health.DEFAULT_THRESHOLD
    except Exception:  # noqa: BLE001
        return 3


def daemon_health_json(root: Path, timeout: float = 1.0) -> Optional[dict]:
    """Fetch the daemon's /v1/admin/health BODY (vs daemon_health_probe, which
    returns only reachability). Returns None when unreachable or unparseable —
    the caller must fail open, because reachability is DaemonHealthProbe's job
    and guard-597 forbids concluding death from a single timed-out probe."""
    try:
        port = _rt_port_file(root).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not port:
        return None
    url = f"http://127.0.0.1:{port}/v1/admin/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if not (200 <= int(status) < 300):
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError,
            http.client.HTTPException):
        # http.client.HTTPException (IncompleteRead, BadStatusLine, ...) derives
        # from Exception ONLY — not OSError, not ValueError — so a truncated or
        # malformed response escaped this tuple and broke the docstring promise
        # above. The tick loop would have caught it, but the probe then dies
        # silently instead of failing open by contract (fresh-eyes F-003).
        return None


class ClockSkewProbe(Probe):
    """Fails loud when a naive-stamp-minting process is not on UTC ().

    THE DEFECT. A long-lived process keeps the TZ env it started with, so one
    started before the TZ=UTC posture landed mints every naive stamp at an
    offset. The whole fleet compares naive stamps (board `--since`,
    `last_active` staleness, LWW merges), so a behind-clock writer reads as
    OLDER than it is and systematically LOSES last-write-wins races: its newer
    content is discarded as stale, with no error on the losing side. Measured
    2026-07-30 on a peer deployment: a diary entry stamped 03:46:43 in a file
    written at 07:46:43 — exactly UTC-4.

    WHY A FRESH-SUBPROCESS ASSERTION CANNOT FIND THIS, and why this probe is
    shaped the way it is. The watchdog tick runs in a NEW process, which
    inherits the CURRENT environment and is therefore UTC-correct even while
    the long-lived daemon beside it is four hours behind. An assertion that
    only checked its own clock would pass on precisely the box that has the
    bug — the same hand-tested-green trap that left pre-edit-context-gate
    inert for 59 days (.claude/rules/read-before-edit.md Rule 4). So the
    load-bearing reading here is the DAEMON's self-measured `tz_offset_s`,
    not this process's.

    TWO INDEPENDENT NUMBERS, deliberately not derived from each other:
      - daemon `tz_offset_s`   — measured inside the daemon (now() - utc).
      - this process's offset  — measured here, the same way.
    Diffing the daemon's reported stamp against OUR clock would cancel to zero
    whenever both are skewed by the same amount, which is the fleet-wide case
    that matters most.

    A MISSING FIELD IS NOT A PASS. A daemon old enough to predate
    `tz_offset_s` is, by construction, a long-lived process — the exact
    population most likely to be skewed. Reporting that as clean would be a
    vacuous zero, so it emits `clock_posture_unverifiable` instead.

    Fail-open on unreachable: emits nothing and lets DaemonHealthProbe own
    reachability (guard-597).
    """

    name = "clock-skew"
    # 60s sits far above NTP jitter and far below the smallest real TZ offset
    # (15 min = 900s), so it separates "clock drift" from "wrong zone" cleanly.
    THRESHOLD_S = 60

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.prev_state: Optional[str] = None  # "clean" | "skewed" | "unverifiable"

    @staticmethod
    def _self_offset_s() -> int:
        now = datetime.now()
        utc = datetime.now(timezone.utc).replace(tzinfo=None)
        return round((now - utc).total_seconds())

    def check(self) -> list[Event]:
        self_off = self._self_offset_s()
        health = daemon_health_json(self.ctx.project_root_path)
        if health is None:
            return []  # unreachable — not our call to make (guard-597)

        daemon_off = health.get("tz_offset_s")
        if daemon_off is None:
            state = "unverifiable"
            payload = {
                "self_offset_s": self_off,
                "daemon_offset_s": None,
                "daemon_pid": health.get("pid"),
                "daemon_uptime_s": health.get("uptime_s"),
                "note": "daemon health has no tz_offset_s — it predates the field, "
                        "so it is a long-lived process whose clock posture cannot be "
                        "confirmed. Absence is NOT a clean reading; restart to verify.",
            }
            summary = (f"{self.name}: clock_posture_unverifiable "
                       f"(daemon pid={health.get('pid')} predates tz_offset_s)")
            event_name = "clock_posture_unverifiable"
            severity = "info"
        elif abs(int(daemon_off)) >= self.THRESHOLD_S or abs(self_off) >= self.THRESHOLD_S:
            state = "skewed"
            # Name the process that is ACTUALLY skewed. A blanket "restart the
            # daemon" is wrong advice in the daemon-clean/self-skewed case, and
            # it is wrong while the payload's own daemon_offset_s reads 0 —
            # exactly the concluding-a-cause-the-fields-contradict shape
            # guard-1955 names. That case is reachable and is the more urgent
            # one: it means the BOX TZ regressed after the daemon started
            # correctly, so every NEW process is now minting skewed stamps
            # while the daemon looks fine (fresh-eyes F-002).
            daemon_bad = abs(int(daemon_off)) >= self.THRESHOLD_S
            self_bad = abs(self_off) >= self.THRESHOLD_S
            if daemon_bad and self_bad:
                who = "both the daemon and this process"
                remedy = ("the BOX TZ posture is wrong, not just one process: fix "
                          "TZ=UTC at the environment/settings level, then restart "
                          "the daemon AND the agent session")
            elif daemon_bad:
                who = "the daemon"
                remedy = ("restart the daemon so it inherits TZ=UTC — the code is "
                          "correct, the process env is stale (same class as "
                          "guard-559 / rb-2022; no edit will fix it)")
            else:
                who = "this watchdog process (the daemon is clean)"
                remedy = ("the daemon is on UTC but freshly-spawned processes are "
                          "NOT — the box TZ regressed AFTER the daemon started. Fix "
                          "TZ=UTC in the environment; restarting the daemon would "
                          "make things WORSE by moving it onto the wrong zone too")
            payload = {
                "self_offset_s": self_off,
                "daemon_offset_s": int(daemon_off),
                "threshold_s": self.THRESHOLD_S,
                "skewed_side": ("both" if (daemon_bad and self_bad)
                                else "daemon" if daemon_bad else "self"),
                "daemon_pid": health.get("pid"),
                "daemon_uptime_s": health.get("uptime_s"),
                "daemon_naive_now": health.get("naive_now"),
                "remedy": remedy,
            }
            summary = (f"{self.name}: clock_skew_detected in {who} — "
                       f"daemon={daemon_off}s self={self_off}s "
                       f"(threshold {self.THRESHOLD_S}s) — naive stamps LOSE LWW races")
            event_name = "clock_skew_detected"
            severity = "critical"
        else:
            state = "clean"
            payload = {"self_offset_s": self_off, "daemon_offset_s": int(daemon_off)}
            # event_name/summary for this branch depend on what we are arriving
            # FROM, which is not known until the dedup step below resolves `was`.
            summary = ""
            event_name = ""
            severity = "info"

        # State-deduped: emit only on transition, so a standing skew does not
        # spam every tick but a NEW one is never missed. "clean" emits only as
        # a recovery from a prior non-clean state.
        if state == self.prev_state:
            return []
        was = self.prev_state
        self.prev_state = state
        if state == "clean" and was is None:
            return []

        if state == "clean":
            # Distinguish the two ways of arriving at clean. Emitting
            # `clock_skew_cleared` out of `unverifiable` asserts that a skew was
            # detected and then fixed, when none was ever measured — a signal
            # whose NAME carries semantics the observation does not support
            # (guard-1008). The prev_state payload disambiguated it, but the
            # event name and summary line are what a log reader sees first, and
            # the arrival from `unverifiable` is the COMMON case: it is what any
            # daemon predating tz_offset_s emits on its first post-restart tick
            # (fresh-eyes F-001).
            if was == "unverifiable":
                event_name = "clock_posture_verified"
                summary = (f"{self.name}: clock_posture_verified — daemon now reports "
                           f"tz_offset_s (daemon={daemon_off}s self={self_off}s); no "
                           f"skew was ever measured, the posture was merely unreadable")
            else:
                event_name = "clock_skew_cleared"
                summary = (f"{self.name}: clock_skew_cleared "
                           f"(daemon={daemon_off}s self={self_off}s)")

        return [Event(probe=self.name, event=event_name, severity=severity,
                      payload={**payload, "prev_state": was}, summary=summary)]

    def to_dict(self) -> dict:
        return {"prev_state": self.prev_state}

    def from_dict(self, state: dict) -> None:
        if state:
            self.prev_state = state.get("prev_state")


def _mem_headroom_threshold() -> float:
    """Fraction of MemTotal at which a single agent process is reported.
    Override with AGENT_WATCHDOG_MEM_PCT (integer percent, 1-100)."""
    raw = os.environ.get("AGENT_WATCHDOG_MEM_PCT", "").strip()
    if raw:
        try:
            v = float(raw)
            if 0 < v <= 100:
                return v / 100.0
        except ValueError:
            pass
    return 0.60


def _mem_total_kb() -> Optional[int]:
    """MemTotal in kB, or None where /proc/meminfo is absent/unreadable."""
    txt = read_text_safe(Path("/proc/meminfo"))
    if not txt:
        return None
    for line in txt.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _mem_available_kb() -> Optional[int]:
    """MemAvailable in kB, or None where /proc/meminfo is absent/unreadable.

    The HOST-TRUTH half of this probe. Inside an LXC/LXD container without
    lxcfs, /proc/meminfo reports the HOST's memory, so this reading reflects
    real host pressure even though `_claude_rss_kb` below can only enumerate
    this container's OWN pid namespace. That asymmetry is exactly what made
    the single-process signal blind on a container host — see the probe
    docstring's SECOND INCIDENT.
    """
    txt = read_text_safe(Path("/proc/meminfo"))
    if not txt:
        return None
    for line in txt.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _mem_avail_floor() -> float:
    """Fraction of MemTotal below which FREE memory is reported, whoever is
    consuming it. Override with AGENT_WATCHDOG_MEM_AVAIL_PCT (1-100).

    Deliberately low (10%): this is the "about to have real trouble" line, not
    a capacity-planning line, and it must not compete for attention with the
    disk advisory that fires at a benign 80%.
    """
    raw = os.environ.get("AGENT_WATCHDOG_MEM_AVAIL_PCT", "").strip()
    if raw:
        try:
            v = float(raw)
            if 0 < v <= 100:
                return v / 100.0
        except ValueError:
            pass
    return 0.10


def _swap_kb() -> tuple[Optional[int], Optional[int]]:
    """(SwapTotal, SwapFree) in kB, or (None, None) where unreadable.

    THE EARLIEST SIGNAL IN THE CHAIN, and the one the 2026-08-22 zakbox1
    incident proved nothing was watching. Swap fills BEFORE MemAvailable
    moves: at the moment that box was unreachable with load 306-364, a
    contemporaneous reading called RAM healthy at 38/62 GiB while swap was
    94% consumed. Both were true. Sustained swap pressure means the kernel is
    paging to disk, and pages under reclaim are uninterruptible I/O, so the
    D-state task count — which Linux counts in load average — explodes while
    CPU looks unremarkable. That is what starves sshd's fork and drops
    tailscaled off the control plane.
    """
    txt = read_text_safe(Path("/proc/meminfo"))
    if not txt:
        return (None, None)
    total = free = None
    for line in txt.splitlines():
        for key, setter in (("SwapTotal:", "t"), ("SwapFree:", "f")):
            if line.startswith(key):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    if setter == "t":
                        total = int(parts[1])
                    else:
                        free = int(parts[1])
    return (total, free)


def _swap_floor() -> float:
    """Fraction of SwapTotal below which FREE swap is reported. Override with
    AGENT_WATCHDOG_SWAP_FREE_PCT (1-100).

    25% by default: routine Linux swap use is normal and must not alarm, but
    three quarters consumed means the kernel has already pushed out what it
    easily can and is working for the rest.
    """
    raw = os.environ.get("AGENT_WATCHDOG_SWAP_FREE_PCT", "").strip()
    if raw:
        try:
            v = float(raw)
            if 0 < v <= 100:
                return v / 100.0
        except ValueError:
            pass
    return 0.25


def _notify_critical_memory(subject: str, message: str) -> dict:
    """Best-effort human alert through the FRAMEWORK notification chokepoint.

    Routes via notify-user.sh, which resolves the domain's transport slot --
    core never names an address or a transport (domain-free-examples.md).
    The probe's box-local alert state (MemoryHeadroomProbe._save_box_state)
    is the ONLY thing standing between this call and a per-tick mail loop:
    notify_dispatch's duplicate gate fingerprints the first 400 chars of the
    body, and every tick's numbers differ, so it never matches; and the
    category is decision-needed, which is always sent. Until 2026-09-02 the
    dedup rode the per-agent tick state, which is mirrored across boxes and
    re-armed on every cross-box tick -- measured 1,696 emails in four days.

    FAIL-OPEN, always: a watchdog that dies because email is down is worse
    than the condition it was reporting. Every failure mode is captured into
    the returned dict and folded into the event payload instead of raised.
    """
    try:
        from _runtime_bash import bash_cmd
        script = Path(__file__).resolve().parent / "notify-user.sh"
        if not script.is_file():
            return {"sent": False, "reason": "notify-user.sh absent"}
        # bash_cmd resolves BASH explicitly (guard-580: a bare "bash" argv[0]
        # reaches the System32 WSL stub on win32 and can HANG FOREVER -- the
        # worst possible failure for an alert path) and passes the script via
        # as_posix (guard-581: bash strips the backslashes of a str(WindowsPath)
        # and silently resolves a nonexistent file).
        proc = subprocess.run(
            # decision-needed, NOT blocker. Verified by dry-run: the routing
            # gate SUPPRESSES 'blocker' as "a status report the fleet can handle
            # itself" and re-routes it to the findings board -- the same silent
            # fate this whole notification exists to escape. 'decision-needed'
            # is an ALWAYS_SEND category, and it is the honest one: the fleet
            # cannot add RAM, set a per-container cap, or reboot a host, so a
            # human genuinely has to decide.
            bash_cmd(script, "--category", "decision-needed",
                     "--subject", subject, "--message", message),
            capture_output=True, text=True, timeout=60,
        )
        # 0 sent | 3 suppressed (re-routed to board) | 4 duplicate |
        # 5 no transport | 6 transport failed -- all are outcomes, not crashes.
        return {"sent": proc.returncode == 0, "rc": proc.returncode,
                "detail": (proc.stderr or proc.stdout or "").strip()[:300]}
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return {"sent": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}


def _claude_rss_kb() -> list[tuple[int, str, int]]:
    """(pid, comm, VmRSS_kB) for each live Claude Code process.

    Linux-only by construction: returns [] wherever /proc is absent, so a
    Windows or macOS box fails OPEN rather than emitting a false reading.
    Matches both comm spellings seen in the g-115-4699 OOM record on WSL2
    ('claude.exe' for the victim, 'claude' for a sibling).
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    out: list[tuple[int, str, int]] = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        comm = (read_text_safe(entry / "comm") or "").strip()
        if comm not in ("claude", "claude.exe"):
            continue
        for line in (read_text_safe(entry / "status") or "").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    out.append((int(entry.name), comm, int(parts[1])))
                break
    return out


class MemoryHeadroomProbe(Probe):
    """Fires BEFORE the OOM killer rather than after it ().

    THE INCIDENT. 2026-08-02T14:41:13, hostname LAPTOP-3IOFCNEO, uname -r
    6.6.87.2-microsoft-standard-WSL2. A Claude Code process that had run 6.5
    days held 6.06 GiB anon-rss on a 7.7 GiB box — 88.6% of all resident
    process memory in the kernel's own victim table. A global OOM fired
    (CONSTRAINT_NONE) and the kernel selected that process precisely BECAUSE
    it was the largest. systemd then marked foxtrot-tmux.service failed
    (OOMPolicy=stop, Restart=no) and restarted nothing, so the agent stayed
    dead ~76 minutes until a human reopened it.

    WHY THIS SIGNAL, AND WHY IT HAS TO BE THIS ONE. Every liveness defense the
    loop already carries — the ScheduleWakeup deadman pair, the SessionStart
    recovery-gate, the stop-hook BLOCK — executes INSIDE the Claude Code
    process. Once that process is gone, all of them are gone with it, so by
    construction none can recover it. The only defense that can act is one
    that fires while the process is still alive. RSS-as-a-fraction-of-MemTotal
    is that signal: it rises monotonically enough over a long session to give
    days of warning, and it is readable in two file reads with no daemon.

    WHY NOT oom_score_adj, which is the obvious first idea. Measured from the
    same victim table: every process OTHER than Claude summed to 0.78 GiB. So
    de-prioritising Claude as an OOM victim only makes the kernel kill all of
    that instead and then OOM again moments later, having freed under a
    gigabyte. Protecting the victim does not create headroom. Ending the
    session does — which is why this probe reports rather than re-nices.

    SECOND INCIDENT (2026-08-22, zakbox1, Ubuntu 24.04.4, 6.8.0-137-generic,
    ~64 GiB, ~17 LXD containers). `Out of memory: Killed process 279635
    (claude.exe) total-vm:25473820kB`, preceded by ~55 minutes of tasks
    blocked >368s in D state; tailscaled and ayoai-fleet-sweep.service both
    died, the host stayed pingable, and sshd accepted TCP without ever
    reaching its banner. The probe above did NOT fire, and could not have:
    it measured `max(per-process RSS) / MemTotal` against a 60% floor, so on
    a 64 GiB box ONE process had to reach ~38 GiB. Ten containers at ~6 GiB
    each sum to a fatal 60 GiB while every individual reading is ~9% — and
    worse, each container enumerates only its OWN pid namespace while
    /proc/meminfo reports the WHOLE HOST, so the numerator is container-local
    and the denominator host-wide. Every container independently concluded it
    was fine. The design was correct for the single-agent 7.7 GiB box it was
    born on, where one process IS the whole population, and silently wrong
    the moment it moved to a multi-tenant host.

    So the probe now carries TWO independent triggers and fires on either:
      - `largest_process` — the original signal, unchanged.
      - `host_available`  — MemAvailable below a floor, whoever is consuming
        it. Namespace-independent, catches non-Claude consumers, and needs no
        process enumeration, so it survives the blindness above.

    Advisory by contract: it emits an event and never mutates state. The
    remedy (a deliberate /stop + /start rotation, or a per-container memory
    limit) belongs to a human or to a later goal, not to a probe running
    inside the process it is measuring.
    """

    name = "memory-headroom"

    # Re-alert cadence while the box STAYS over: one human email per episode,
    # then at most one every REALERT window. Override (seconds, > 0) with
    # AGENT_WATCHDOG_MEM_REALERT_SECONDS.
    REALERT_ENV = "AGENT_WATCHDOG_MEM_REALERT_SECONDS"
    REALERT_DEFAULT_SECONDS = 6 * 3600
    BOX_STATE_NAME = "watchdog-memory-alert.json"

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.over = False
        self._last_alert_ts: Optional[float] = None
        self._load_box_state()

    # ── box-local alert state ────────────────────────────────────────────────
    # Memory pressure is a property of the BOX, but tick state is persisted
    # per AGENT (watchdog-prev-state.json under agents/<agent>/session/), and
    # that directory is mirrored across boxes. An agent whose Bodies tick on
    # two hosts alternates the file between over=True (the starved host) and
    # over=False (the healthy one), so the "emit once per episode" dedup
    # re-armed on nearly every tick. Measured 2026-09-02 (outreach ledger):
    # 1,696 "Memory pressure on <box>" decision-needed emails in four days
    # (662 / 392 / 252 / 390), 332 of one day's from a single agent, and the
    # user's real mail was buried under them. The alert state therefore lives
    # in core/logs/ (box-local, never synced) and is keyed by nothing but the
    # box. Fail-open at every step: an unreadable or unwritable state file
    # degrades to the in-process behaviour, never to a crash.
    #
    # Under pytest the file is NOT written unless BOX_STATE_DIR_ENV points at
    # a directory: the probe tests build their context on the REAL project
    # root (a tmp root does not carry the worker-shaped state they need), so
    # an induced-pressure test would otherwise stamp over=True into the live
    # box's file and silence a genuine alert for a whole re-alert window.
    # Same shape as the daemon-spawn refusal under PYTEST_CURRENT_TEST
    # (): a test that wants the file says where, loudly.
    BOX_STATE_DIR_ENV = "AGENT_WATCHDOG_BOX_STATE_DIR"

    def _box_state_path(self) -> Optional[Path]:
        override = os.environ.get(self.BOX_STATE_DIR_ENV, "").strip()
        if override:
            return Path(override) / self.BOX_STATE_NAME
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return None
        root = getattr(self.ctx, "project_root_path", None)
        if root is None:
            return None
        return Path(root) / "core" / "logs" / self.BOX_STATE_NAME

    def _load_box_state(self) -> None:
        path = self._box_state_path()
        if path is None:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                st = json.load(f) or {}
            self.over = bool(st.get("over", False))
            ts = st.get("last_alert_ts")
            self._last_alert_ts = float(ts) if isinstance(ts, (int, float)) else None
        except (OSError, ValueError, TypeError):
            return

    def _save_box_state(self, triggers: list) -> None:
        path = self._box_state_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"over": self.over, "last_alert_ts": self._last_alert_ts,
                           "triggers": triggers, "box": _box_id()}, f)
            os.replace(tmp, path)
        except OSError:
            return

    @classmethod
    def _realert_seconds(cls) -> float:
        raw = os.environ.get(cls.REALERT_ENV, "").strip()
        if raw:
            try:
                v = float(raw)
                if v > 0:
                    return v
            except ValueError:
                pass
        return float(cls.REALERT_DEFAULT_SECONDS)

    def _realert_due(self, now: float) -> bool:
        # An episode with no stamp (state restored from an older writer) is
        # NOT due: unknown must fail towards silence, since silence is bounded
        # by the recovery transition and noise is bounded by nothing.
        if self._last_alert_ts is None:
            return False
        return (now - self._last_alert_ts) >= self._realert_seconds()

    def check(self) -> list[Event]:
        total_kb = _mem_total_kb()
        if not total_kb:
            return []  # not Linux, or /proc unreadable — fail open
        procs = _claude_rss_kb()
        avail_kb = _mem_available_kb()
        # NOTE: no early return on an empty `procs`. A container that can see
        # NO claude process is precisely the pid-namespace case the second
        # incident turned on — the host can still be starved, and MemAvailable
        # is readable from inside. Bail only when NEITHER signal is available.
        swap_total_kb, swap_free_kb = _swap_kb()
        if not procs and avail_kb is None and not swap_total_kb:
            return []  # nothing measurable — fail open

        threshold = _mem_headroom_threshold()
        floor = _mem_avail_floor()
        swap_floor = _swap_floor()

        pid: Optional[int] = None
        comm: Optional[str] = None
        rss_kb = 0
        frac = 0.0
        if procs:
            pid, comm, rss_kb = max(procs, key=lambda r: r[2])
            frac = rss_kb / float(total_kb)
        rss_total_kb = sum(r[2] for r in procs)
        avail_frac = (avail_kb / float(total_kb)) if avail_kb is not None else None

        # A box with swap disabled (SwapTotal 0) must never register as
        # "no swap free" -- that is not pressure, it is a configuration.
        swap_free_frac = (swap_free_kb / float(swap_total_kb)
                          if swap_total_kb and swap_free_kb is not None else None)

        triggers = []
        if procs and frac >= threshold:
            triggers.append("largest_process")
        if avail_frac is not None and avail_frac < floor:
            triggers.append("host_available")
        if swap_free_frac is not None and swap_free_frac < swap_floor:
            triggers.append("swap_exhausted")

        payload = {
            "pid": pid,
            "comm": comm,
            "rss_kb": rss_kb,
            "rss_gib": round(rss_kb / 1048576.0, 2),
            "mem_total_kb": total_kb,
            "mem_total_gib": round(total_kb / 1048576.0, 2),
            "pct_of_memtotal": round(frac * 100.0, 1),
            "threshold_pct": round(threshold * 100.0, 1),
            "claude_process_count": len(procs),
            # Aggregate, not just the max: N sibling agents each individually
            # under threshold are what actually exhausts a multi-tenant host.
            "claude_rss_total_kb": rss_total_kb,
            "claude_rss_total_gib": round(rss_total_kb / 1048576.0, 2),
            "mem_available_kb": avail_kb,
            "mem_available_gib": (None if avail_kb is None
                                  else round(avail_kb / 1048576.0, 2)),
            "pct_available": (None if avail_frac is None
                              else round(avail_frac * 100.0, 1)),
            "avail_floor_pct": round(floor * 100.0, 1),
            "swap_total_kb": swap_total_kb,
            "swap_free_kb": swap_free_kb,
            "pct_swap_free": (None if swap_free_frac is None
                              else round(swap_free_frac * 100.0, 1)),
            "swap_floor_pct": round(swap_floor * 100.0, 1),
            "triggers": triggers,
        }

        now = time.time()
        # First tick over = a new episode; still over after the re-alert window
        # = remind. Everything in between is silent: the ledger already holds
        # the alert, and a second email teaches the reader to ignore the first.
        realert = bool(triggers and self.over and self._realert_due(now))
        if triggers and (not self.over or realert):
            self.over = True
            self._last_alert_ts = now
            payload["realert"] = realert
            if "swap_exhausted" in triggers:
                why = (f"swap {payload['pct_swap_free']}% free "
                       f"(floor {payload['swap_floor_pct']}%) — the kernel is "
                       f"paging to disk; sustained swap pressure shows up as "
                       f"D-state tasks and a load-average spike, NOT as low "
                       f"RAM. Host has {payload['pct_available']}% available")
            elif "host_available" in triggers:
                why = (f"host has {payload['mem_available_gib']} GiB available "
                       f"= {payload['pct_available']}% of "
                       f"{payload['mem_total_gib']} GiB "
                       f"(floor {payload['avail_floor_pct']}%); "
                       f"{payload['claude_process_count']} visible agent "
                       f"process(es) hold {payload['claude_rss_total_gib']} GiB")
            else:
                why = (f"{comm} pid {pid} at {payload['rss_gib']} GiB = "
                       f"{payload['pct_of_memtotal']}% of "
                       f"{payload['mem_total_gib']} GiB "
                       f"(threshold {payload['threshold_pct']}%)")
            summary = (f"{self.name}: memory_pressure [{'+'.join(triggers)}] — "
                       f"{why}. An OOM kill selects the largest process and "
                       f"NOTHING restarts it (g-115-4699) — rotate the "
                       f"session, and cap per-container memory on a shared "
                       f"host (2026-08-22 zakbox1).")
            # Reach a human. This probe logged to a box-local jsonl and nothing
            # else, which is why the 2026-08-22 escalation was invisible until
            # someone walked to the console.
            try:
                payload["notified"] = _notify_critical_memory(
                    f"Memory pressure on {_box_id()}: {'+'.join(triggers)}",
                    summary + "\n\n" + json.dumps(payload, indent=2, default=str),
                )
            except Exception as exc:  # noqa: BLE001 - fail-open at BOTH layers
                # _notify_critical_memory swallows its own failures, so reaching
                # here means the notifier itself was replaced or is broken in a
                # way it could not catch. The tick still has to finish: a
                # watchdog that dies because mail is down is strictly worse than
                # the condition it was trying to report.
                payload["notified"] = {
                    "sent": False,
                    "reason": f"{type(exc).__name__}: {exc}"[:200],
                }
            self._save_box_state(triggers)
            return [Event(
                probe=self.name, event="memory_pressure", severity="critical",
                payload=payload, include_processes=True, summary=summary,
            )]

        # Hysteresis: only clear once well back under, so a reading parked at
        # the boundary cannot flap a critical event every tick. BOTH signals
        # must have recovered — an unreadable one cannot veto the recovery.
        proc_recovered = frac < threshold * 0.9
        avail_recovered = avail_frac is None or avail_frac > floor * 1.1
        swap_recovered = swap_free_frac is None or swap_free_frac > swap_floor * 1.1
        if (self.over and not triggers and proc_recovered and avail_recovered
                and swap_recovered):
            self.over = False
            self._save_box_state([])
            return [Event(
                probe=self.name, event="memory_pressure_cleared", severity="info",
                payload=payload,
                summary=(f"{self.name}: memory_pressure_cleared — "
                         f"{payload['pct_of_memtotal']}% of MemTotal, "
                         f"{payload['pct_available']}% available"),
            )]

        return []

    def to_dict(self) -> dict:
        return {"over": self.over, "last_alert_ts": self._last_alert_ts}

    def from_dict(self, state: dict) -> None:
        # The per-agent tick state is mirrored across boxes and cannot describe
        # THIS box's memory; the box-local file (loaded in __init__) is the
        # truth wherever it exists. Honour the per-agent value only where no
        # box-local store is available (a context without a project root).
        if state and self._box_state_path() is None:
            self.over = bool(state.get("over", False))
            ts = state.get("last_alert_ts")
            self._last_alert_ts = float(ts) if isinstance(ts, (int, float)) else None


# ─────────────────────────────────────────────────────────────────────────────
# GitDriftProbe — per-box git divergence + carrier depth + host capacity
# ─────────────────────────────────────────────────────────────────────────────

_GIT_DRIFT_ENV = {
    "ahead_threshold": "GIT_DRIFT_AHEAD",
    "behind_threshold": "GIT_DRIFT_BEHIND",
    "unconsumed_threshold": "GIT_DRIFT_UNCONSUMED",
    "disk_used_pct_threshold": "GIT_DRIFT_DISK_PCT",
    "disk_free_floor_gib": "GIT_DRIFT_DISK_FREE_GIB",
    "fetch_throttle_minutes": "GIT_DRIFT_FETCH_THROTTLE_MIN",
    "ticks_to_file": "GIT_DRIFT_TICKS_TO_FILE",
    "ticks_to_revalidate": "GIT_DRIFT_TICKS_TO_REVALIDATE",
}


def _git_drift_config() -> dict:
    """Read the `git_drift` block from aspirations.yaml; env overrides win.

    guard-308: a probe's defaults must REFERENCE the source of truth, never
    restate it. The literals below are a last-resort floor for a corrupt or
    missing config file, not a second copy of the policy — every one of them
    is the same value the YAML carries, and the YAML is what documents WHY.
    """
    cfg: dict = {}
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "aspirations.yaml"
        with cfg_path.open(encoding="utf-8") as f:
            cfg = ((yaml.safe_load(f) or {}).get("git_drift") or {})
    except Exception:  # noqa: BLE001 — a config read must never kill the tick
        cfg = {}
    out = {
        "ahead_threshold": 25,
        "behind_threshold": 50,
        "unconsumed_threshold": 100,
        "disk_used_pct_threshold": 80,
        "disk_free_floor_gib": 15,
        "fetch_throttle_minutes": 30,
        "ticks_to_file": 2,
        # Re-validation interval for the `fired` latch below. GitDriftProbe
        # takes its thresholds from THIS config dict (env-overridable,
        # per-box tunable), so the interval is a config key here rather
        # than the class constant MirrorWedgeProbe uses — same defect,
        # each fix matching its own probe's idiom. Value matches the twin's
        # WEDGED_TICKS_TO_REVALIDATE so the two probes re-file on one cadence.
        "ticks_to_revalidate": 50,
    }
    for key in out:
        val = cfg.get(key)
        if isinstance(val, (int, float)) and val >= 0:
            out[key] = val
        raw = os.environ.get(_GIT_DRIFT_ENV[key], "").strip()
        if raw:
            try:
                parsed = float(raw) if "." in raw else int(raw)
            except ValueError:
                continue
            # The SAME >= 0 guard the config path uses. Without it an env typo
            # like GIT_DRIFT_AHEAD=-5 parses cleanly and makes every metric
            # breach forever — a sign error is the one malformed value that does
            # NOT look malformed, so it is the one that reaches production.
            if parsed >= 0:
                out[key] = parsed
    return out


def _git(root: Path, *args: str, timeout: float = 20.0) -> tuple[int, str, str]:
    """Run one git command in `root`. Never raises; a failure is (rc, '', err)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as e:  # noqa: BLE001 — includes TimeoutExpired and FileNotFoundError
        return 127, "", f"{type(e).__name__}: {e}"


def _disk_used_pct(path: Path) -> Optional[float]:
    """Host filesystem usage as `df` reports Use%, or None if unreadable.

    df's Use% is used/(used+avail) — it EXCLUDES root-reserved blocks, so it
    runs a few points HIGHER than used/total and is both the number a human
    sees and the more conservative of the two (cc-02 2026-08-14: 92% vs 88%).
    shutil.disk_usage is used rather than shelling out to df because guard-326
    requires a probe to be implementable on every platform the fleet runs —
    df is absent on Windows, /proc is absent on macOS, and this call is neither.
    """
    try:
        usage = shutil.disk_usage(str(path))
    except Exception:  # noqa: BLE001
        return None
    denom = usage.used + usage.free
    if denom <= 0:
        return None
    return round(100.0 * usage.used / denom, 1)


def _disk_free_gib(path: Path) -> Optional[float]:
    """Free space in GiB, or None if unreadable.

    The SCALE half of the disk breach. A percentage is scale-blind, and that is
    not a theoretical complaint: on 2026-08-22 a 905 GiB volume at 81% used --
    169 GiB free, entirely healthy -- filed five HIGH `Investigate` goals in one
    hour across four containers. Nobody actioned them, correctly, because they
    were noise; and the noise was running while the box was actually dying of
    something else. An alert that cries wolf trains its reader to ignore it.
    """
    try:
        usage = shutil.disk_usage(str(path))
    except Exception:  # noqa: BLE001
        return None
    return round(usage.free / (1024.0 ** 3), 1)


def _live_body_sids(world_dir) -> Optional[set]:
    """SIDs of Bodies the fleet currently reports as live, or None if unreadable.

    Source is team-state `in_flight_bodies`, the same store worker-ref-consume.sh
    consults before it will retire a carrier ref. None means "could not tell" and
    the caller must SKIP the carrier axis rather than assume — counting a CLOSED
    body's ref is the false positive the first draft of g-115-6128 fell into
    (that history is frozen by design, not unconsumed work).
    """
    try:
        import _team_state
        rows = _team_state.load_rows(world_dir) or {}
    except Exception:  # noqa: BLE001
        return None
    sids = set()
    for row in rows.values():
        if not isinstance(row, dict):
            continue
        for sid, body in (row.get("in_flight_bodies") or {}).items():
            if isinstance(body, dict) and sid:
                sids.add(str(sid))
    return sids


class GitDriftProbe(Probe):
    """Per-box git divergence, carrier-ref depth, and host disk ().

    THE GAP. Until 2026-08-14 nothing monitored any of these three, and every
    divergence measured on 2026-08-13 was found the same way — an operator asked
    a question. cc-07 forked at 342 ahead since 2026-08-08; cc-08 accumulated 223
    main-bound commits in 3 days; cc-03 sat DORMANT 163 behind (0 ahead, 0 dirty,
    so no launch and no loop ever fired a heal path). The companion metric is here
    for the same reason and not as scope creep: zakbox1 reached 90% of 905G with
    450G of policy-retained .history payload, and was exactly as invisible. One
    `shutil.disk_usage` call closes it while the probe is already running per-box.

    WHY ALL THREE IN ONE PROBE. They share the only expensive thing — the
    throttled fetch — and they share a single question: "is this box's work
    reaching, or able to reach, everyone else?" Splitting them would triple the
    per-tick fetch cost to report one condition each.

    THE CARRIER AXIS IS LIVENESS-GATED, and that gate is the whole correctness
    argument for it. `refs/workers/<agent>/<sid>` belongs to a Body; when that
    Body closes, its ref is frozen HISTORY, not outstanding work. Counting closed
    refs is the false positive g-115-6128's first draft fell into, so an
    unreadable liveness source SKIPS the axis (payload records why) rather than
    guessing. Absence of signal is not health — but here the fail-open direction
    is silence, because the alternative is a standing false alarm on every box.

    ADVISORY BY CONTRACT. It emits an event, files ONE deduped Investigate goal,
    and posts once to the findings board. It never pushes, never merges, never
    prunes. The remedies (push the branch, consume the carrier, reclaim disk) are
    deliberate acts with their own goals — g-115-5945 owns the consume cadence.

    THE GOAL IT FILES IS A LEASE, SO IT HAS A RELEASE PATH (guard-3419). Dedup
    keyed on open-goal existence suppresses re-filing for as long as the goal is
    open, which means a dedup with no close silently disables the detector
    forever. `_close_drift_goal` retires the goal as `skipped` once every metric
    is back under threshold — never `completed`, because no investigation
    happened; the condition resolved. MirrorWedgeProbe learned this the
    expensive way (three goals open at 7, 14 and 17 days) and this probe inherits
    the fix rather than the defect.
    """

    name = "git-drift"

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.consecutive_breach = 0
        self.fired = False
        self.last_fetch_ts = 0.0
        # Where this box's commits actually GO when `remote.origin.pushurl`
        # points somewhere other than the fetch URL: {"url": masked, "tip": sha}
        # resolved on the fetch cadence, or None when pushes go to origin itself.
        self.push_basis: Optional[dict] = None

    # ---- measurement -----------------------------------------------------

    def _maybe_fetch(self, root: Path, cfg: dict) -> bool:
        """Throttled fetch of origin/main + the worker-ref namespace.

        Returns True when a fetch ran THIS tick. The tick runs every iteration
        and this is its only network I/O, so the throttle is what keeps the
        probe off the loop's critical path; between fetches the counts come from
        the last-known remote refs and the payload says so (stale_basis).
        """
        throttle_s = float(cfg["fetch_throttle_minutes"]) * 60.0
        now = time.time()
        if throttle_s > 0 and (now - self.last_fetch_ts) < throttle_s:
            return False
        self.last_fetch_ts = now
        _git(root, "fetch", "--quiet", "origin", "main", timeout=30.0)
        _git(root, "fetch", "--quiet", "--prune", "origin",
             "+refs/workers/*:refs/workers/*", timeout=45.0)
        self.push_basis = self._resolve_push_basis(root)
        return True

    def _resolve_push_basis(self, root: Path) -> Optional[dict]:
        """The tip of `main` at `remote.origin.pushurl`, when one is configured.

        `ahead` answers "is this box's work reaching everyone else?", and the
        answer lives at the URL the box PUSHES to. A deployment whose origin is
        a read-only upstream pushes elsewhere (measured 2026-08-30, coach@zc-03:
        `url` = the staging repo it pulls from, `pushurl` = the box-local bare
        repo every commit lands in — HEAD was 0 ahead of that tip and 52 ahead
        of the fetch ref, so the probe fired on 28 consecutive ticks against
        work that had been delivered on every one of them). Same cadence as the
        fetch, so it adds no per-tick I/O. None = pushes go to the fetch URL.
        An unreachable pushurl is recorded, not guessed around: the caller
        falls back to the fetch ref and says so in the payload.
        """
        rc, pushurl, _ = _git(root, "config", "--get", "remote.origin.pushurl")
        pushurl = pushurl.strip()
        if rc != 0 or not pushurl:
            return None
        masked = re.sub(r"://[^/@]+@", "://***@", pushurl)
        rc, out, err = _git(root, "ls-remote", "--quiet", "--heads", pushurl, "main",
                            timeout=30.0)
        tip = out.split()[0] if rc == 0 and out.split() else None
        return {"url": masked, "tip": tip,
                "error": None if tip else (err[:160] or f"ls-remote rc={rc}")}

    def _ahead_behind(self, root: Path) -> dict:
        rc, out, err = _git(root, "rev-list", "--left-right", "--count",
                            "origin/main...HEAD")
        if rc != 0:
            return {"ahead": None, "behind": None, "ahead_basis": "origin/main",
                    "error": err[:160] or f"rc={rc}"}
        try:
            behind, ahead = (int(x) for x in out.split())
        except (ValueError, TypeError):
            return {"ahead": None, "behind": None, "ahead_basis": "origin/main",
                    "error": f"unparseable rev-list output: {out!r}"}
        basis = "origin/main"
        note = None
        pb = self.push_basis
        if pb:
            if pb.get("tip"):
                rc2, cnt, err2 = _git(root, "rev-list", "--count", f"{pb['tip']}..HEAD")
                if rc2 == 0 and cnt.isdigit():
                    ahead, basis = int(cnt), f"pushurl:{pb['url']}"
                else:
                    note = (f"pushurl tip {pb['tip'][:12]} not usable locally "
                            f"({err2[:80] or f'rc={rc2}'}); ahead measured against origin/main")
            else:
                note = (f"pushurl {pb['url']} unreachable ({pb.get('error')}); "
                        f"ahead measured against origin/main")
        return {"ahead": ahead, "behind": behind, "ahead_basis": basis,
                "error": note}

    def _carrier_depths(self, root: Path) -> dict:
        # Local import, matching MirrorWedgeProbe: WORLD_DIR's module-level bind
        # sits inside a try/except, so a failed _paths import leaves the NAME
        # undefined rather than None — importing here turns that into a caught
        # ImportError and a clean SKIP instead of a NameError mid-probe.
        try:
            from _paths import WORLD_DIR as _world
        except Exception:  # noqa: BLE001
            _world = None
        live = _live_body_sids(_world) if _world is not None else None
        if live is None:
            return {"skipped": True,
                    "reason": "team-state in_flight_bodies unreadable — carrier "
                              "axis skipped rather than counting closed-body refs",
                    "refs": [], "max_unconsumed": None}
        rc, out, _ = _git(root, "for-each-ref", "--format=%(refname)", "refs/workers/")
        if rc != 0:
            return {"skipped": True, "reason": f"for-each-ref rc={rc}",
                    "refs": [], "max_unconsumed": None}
        refs = []
        for refname in [ln.strip() for ln in out.splitlines() if ln.strip()]:
            parts = refname.split("/")
            sid = parts[-1] if len(parts) >= 4 else ""
            if sid not in live:
                continue  # closed body — frozen history, not unconsumed work
            rc2, cnt, _ = _git(root, "rev-list", "--count", f"origin/main..{refname}")
            if rc2 != 0 or not cnt.isdigit():
                continue
            refs.append({"ref": refname, "sid": sid, "unconsumed": int(cnt)})
        return {"skipped": False, "reason": None, "refs": refs,
                "max_unconsumed": max((r["unconsumed"] for r in refs), default=0)}

    # ---- probe -----------------------------------------------------------

    def check(self) -> list[Event]:
        root = self.ctx.project_root_path
        cfg = _git_drift_config()
        rc, _, _ = _git(root, "rev-parse", "--git-dir", timeout=10.0)
        if rc != 0:
            return []  # not a git repo — fail open, nothing to report

        fetched = self._maybe_fetch(root, cfg)
        ab = self._ahead_behind(root)
        carrier = self._carrier_depths(root)
        disk_pct = _disk_used_pct(root)
        disk_free_gib = _disk_free_gib(root)

        breaches = []
        if ab["ahead"] is not None and ab["ahead"] > cfg["ahead_threshold"]:
            breaches.append(f"ahead={ab['ahead']} (>{cfg['ahead_threshold']})")
        if ab["behind"] is not None and ab["behind"] > cfg["behind_threshold"]:
            breaches.append(f"behind={ab['behind']} (>{cfg['behind_threshold']})")
        if (carrier["max_unconsumed"] is not None
                and carrier["max_unconsumed"] > cfg["unconsumed_threshold"]):
            breaches.append(f"live-carrier-unconsumed={carrier['max_unconsumed']} "
                            f"(>{cfg['unconsumed_threshold']})")
        # BOTH conditions must hold. Percentage alone is scale-blind (81% of
        # 905 GiB is 169 GiB free -- five false HIGH goals in one hour on
        # 2026-08-22); an absolute floor alone would miss a small volume at 97%
        # where the percentage is the meaningful signal. Requiring both keeps
        # the small-volume protection and drops the large-volume noise.
        # An UNREADABLE free reading does not suppress: it cannot confirm
        # headroom, so the percentage is allowed to speak alone (fail toward
        # alerting, the same direction guard-1448 argues for).
        if (disk_pct is not None and disk_pct > cfg["disk_used_pct_threshold"]
                and (disk_free_gib is None
                     or disk_free_gib < cfg["disk_free_floor_gib"])):
            # Say WHOSE filesystem this is. `shutil.disk_usage` measures the
            # filesystem CONTAINING the path, and on a shared-fs container that
            # is the HOST volume, not the container's own tree -- the docstring
            # on _disk_used_pct has always said "host filesystem" but the breach
            # string did not, and the breach string is what becomes the filed
            # goal's TITLE. Measured 2026-08-19 by omni (ZDS /529) and
            # re-confirmed on cc-06 2026-08-23 (905G/694G/174G/80%, same device
            # as zakbox1): the LXD pool is shared by 16 containers, and one
            # agent's whole footprint is ~31G. So a goal reading "disk=81.3%"
            # filed into a container's queue is UNDISCHARGEABLE BY CONSTRUCTION:
            # deleting everything the agent owns moves the host 82% -> ~78%,
            # while the goal keeps scoring well on urgency and keeps being
            # selected. Naming the surface in the title is what lets the reader
            # route it instead of attempting it (guard-846).
            #
            # This is the SECOND half of the fix. The floor above stops the
            # large-volume false alarm from firing at all; this stops the ones
            # that DO fire from being mis-routed. Back-ported up from ZDS-Mind
            # during the 2026-08-23 Claude-Mind -> ZDS-Mind promotion reconcile
            # (guard-119): the two deployments fixed the same defect from
            # opposite ends and both halves were worth keeping.
            breaches.append(
                f"host-disk={disk_pct}% with {disk_free_gib}GiB free "
                f"(>{cfg['disk_used_pct_threshold']}% AND "
                f"<{cfg['disk_free_floor_gib']}GiB); filesystem containing "
                f"{root} -- on a shared-fs container this is the HOST volume "
                f"and is not reclaimable from inside)")

        # Every field a reader needs to act is in the payload, including the
        # ones that explain a SILENCE (guard-1955: a probe whose schema has no
        # field for a cause cannot be used to rule that cause out).
        payload = {
            "agent": self.ctx.agent_name,
            "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD")[1] or None,
            "ahead": ab["ahead"], "behind": ab["behind"],
            "ahead_basis": ab.get("ahead_basis"),
            "ahead_behind_error": ab["error"],
            "carrier_refs": carrier["refs"],
            "carrier_max_unconsumed": carrier["max_unconsumed"],
            "carrier_skipped": carrier["skipped"],
            "carrier_skip_reason": carrier["reason"],
            "disk_used_pct": disk_pct,
            "disk_free_gib": disk_free_gib,
            "fetched_this_tick": fetched,
            "stale_basis": not fetched,
            "thresholds": cfg,
            "breaches": breaches,
            "consecutive_breach": self.consecutive_breach,
        }

        if breaches:
            self.consecutive_breach += 1
            payload["consecutive_breach"] = self.consecutive_breach
            # `fired` is written ONLY in the cleared branch below, so while a
            # breach PERSISTS it never takes the negative — a latch by
            # guard-4870's definition. Once fired, if the covering goal is
            # closed (or never landed) while the breach continues, the box is
            # breached + un-goaled + UN-REFILEABLE, because `not self.fired`
            # short-circuits BEFORE _file_drift_goal's own dedup can run. A
            # latched detector has silently stopped detecting, which is worse
            # than an absent one: the fleet reads the quiet as health.
            # Re-validate on a cadence so a persistent breach can re-file.
            _revalidate = cfg.get("ticks_to_revalidate") or 0
            if self.consecutive_breach >= cfg["ticks_to_file"] and (
                    not self.fired
                    or (_revalidate and self.consecutive_breach % _revalidate == 0)
            ):
                goal = self._file_drift_goal(payload)
                # Mark fired only when the goal LANDED or an open one already
                # covers this box; a genuine filing failure retries next tick
                # rather than losing the episode (the  lesson).
                if goal.get("filed") or goal.get("dedup"):
                    self.fired = True
                payload["goal"] = goal
                payload["board"] = self._post_board_alert(payload, goal)
                return [Event(
                    probe=self.name, event="git_drift", severity="critical",
                    payload=payload,
                    summary=(f"{self.name}: {self.ctx.agent_name}'s box breached "
                             f"{len(breaches)} threshold(s) on "
                             f"{self.consecutive_breach} consecutive ticks — "
                             f"{'; '.join(breaches)} "
                             f"({goal.get('goal_id') or goal.get('error')})"),
                )]
            return []

        cleared = self._close_drift_goal()
        self.consecutive_breach = 0
        was_fired = self.fired
        self.fired = False
        if was_fired or cleared.get("closed"):
            payload["closed"] = cleared
            return [Event(
                probe=self.name, event="git_drift_cleared", severity="info",
                payload=payload,
                summary=("git drift cleared — every metric back under threshold"
                         + (f" ({cleared.get('detail')})" if cleared.get("detail") else "")),
            )]
        return []

    # ---- escalation ------------------------------------------------------

    def _origin_signal(self) -> str:
        # BOX-scoped, like MirrorWedgeProbe's: the drift is box-local state and
        # the remedy (push, consume, reclaim disk) must run ON this box, so a
        # box-agnostic key would let box A's open goal mask box B's divergence.
        #
        # This probe inherited BOTH the agent-name-as-box-proxy shape AND the
        # comment above from MirrorWedgeProbe, so it carried the identical
        #  defect: `agent_name` is os.environ MIND_AGENT and nothing
        # more, which stopped being a box proxy the moment one agent could run
        # two Bodies on two boxes. Fixed here in the same change, because a
        # fleet-wide assumption fixed in one of its two homes is the shape that
        # reads as fixed while still being live.
        return (f"investigate:git-drift-detected-"
                f"{self.ctx.agent_name}-on-{_box_id()}")

    def _file_drift_goal(self, payload: dict) -> dict:
        """File one deduped Investigate goal. Fail-open ({filed: False, error})."""
        origin_signal = self._origin_signal()
        try:
            from _paths import WORLD_DIR
            import importlib
            pf = importlib.import_module("pointer_freshness")
            if pf.open_goal_exists(origin_signal, WORLD_DIR, self.ctx.agent_dir):
                return {"filed": False, "dedup": True, "goal_id": None,
                        "error": "open goal exists (dedup)"}
            refs = "; ".join(f"{r['ref']} ({r['unconsumed']} unconsumed)"
                             for r in payload.get("carrier_refs") or []) or "none live"
            body = {
                "title": (f"Investigate: git/capacity drift on {self.ctx.agent_name}'s box "
                          f"— {'; '.join(payload['breaches'])}"),
                "priority": "HIGH",
                "participants": ["agent"],
                "description": (
                    f"agent-watchdog GitDriftProbe on {self.ctx.agent_name}'s box "
                    f"breached threshold on {payload['consecutive_breach']} consecutive "
                    f"ticks: {'; '.join(payload['breaches'])}. "
                    f"branch={payload['branch']} ahead={payload['ahead']} "
                    f"behind={payload['behind']} disk_used={payload['disk_used_pct']}% "
                    f"live-carrier-refs: {refs}. "
                    f"Counts are {'FRESH (fetched this tick)' if payload['fetched_this_tick'] else 'from the LAST-KNOWN remote ref (no fetch this tick — throttled)'}. "
                    f"REMEDY IS BOX-LOCAL and belongs to whoever runs on this box: "
                    f"push the shared branch for `ahead`; merge origin/main for `behind`; "
                    f"`bash core/scripts/worker-ref-consume.sh` for carrier depth "
                    f"(g-115-5945 owns the standing cadence); reclaim disk for capacity. "
                    # --tick, NOT --once: run_once only calls initialize() and
                    # snapshots RunningSidProbe, so it reports NOTHING about this
                    # probe. Naming the wrong flag here would hand the reader a
                    # command that returns silence and reads as "condition gone".
                    f"Re-measure with `MIND_AGENT={self.ctx.agent_name} py -3 "
                    f"core/scripts/agent-watchdog.py --tick` before acting — this "
                    f"goal is a SNAPSHOT and the probe retires it automatically "
                    f"once every metric is back under threshold. "
                    f"Auto-filed by GitDriftProbe (g-115-6128)."
                ),
                "category": "framework-infrastructure",
                "origin_signal": origin_signal,
                # Box-scoped repair must reach the box that can perform it
                # (the  lesson MirrorWedgeProbe carries).
                "intended_agent": self.ctx.agent_name,
            }
            from _runtime_bash import BASH as _bash
            _override_reason = (
                "GitDriftProbe owns exact box-scoped dedup via "
                "open_goal_exists(origin_signal) plus a close path; the "
                "goal-dup-gate's keyword check false-positives on the generic "
                "git/drift/push tokens that every fleet-git goal shares (g-115-6128).")
            proc = subprocess.run(
                [_bash, "core/scripts/aspirations-add-goal.sh", ESCALATION_ASP,
                 "--source", ESCALATION_SOURCE,
                 "--override-duplication", _override_reason],
                input=json.dumps(body, ensure_ascii=True),
                capture_output=True, text=True,
                cwd=str(self.ctx.project_root_path), timeout=60,
            )
            if proc.returncode != 0:
                return {"filed": False, "goal_id": None,
                        "error": (proc.stderr or proc.stdout or "non-zero exit").strip()[:200]}
            try:
                goal_id = json.loads(proc.stdout).get("id")
            except (json.JSONDecodeError, AttributeError):
                goal_id = None
            return {"filed": True, "goal_id": goal_id, "error": None}
        except Exception as e:  # noqa: BLE001 — filing must not kill the event
            return {"filed": False, "goal_id": None, "error": f"{type(e).__name__}: {e}"}

    def _post_board_alert(self, payload: dict, goal: dict) -> dict:
        """One findings post per episode — the FLEET-visible half.

        Not redundant with the goal: the goal carries intended_agent=<this
        agent> so the box that can fix it sees it, which is exactly what keeps
        it OFF every partner's selector. Divergence on this box is a fact
        partners need (they read origin/main too), and the board is where
        cross-box awareness lives.
        """
        try:
            from _runtime_bash import BASH as _bash
            text = (f"GitDriftProbe: {self.ctx.agent_name}'s box breached "
                    f"{'; '.join(payload['breaches'])} on "
                    f"{payload['consecutive_breach']} consecutive watchdog ticks. "
                    f"branch={payload['branch']} ahead={payload['ahead']} "
                    f"behind={payload['behind']} disk={payload['disk_used_pct']}% "
                    f"live-carrier-max-unconsumed={payload['carrier_max_unconsumed']}. "
                    f"Goal: {goal.get('goal_id') or goal.get('error')}. "
                    f"Remedy is box-local (g-115-6128).")
            proc = subprocess.run(
                [_bash, "core/scripts/board-post.sh", "--channel", "findings",
                 "--type", "finding",
                 "--tags", f"git-drift,{self.ctx.agent_name},auto-probe"],
                input=text, capture_output=True, text=True,
                cwd=str(self.ctx.project_root_path), timeout=60,
            )
            if proc.returncode != 0:
                return {"posted": False,
                        "error": (proc.stderr or proc.stdout or "non-zero exit").strip()[:160]}
            return {"posted": True, "msg_id": (proc.stdout or "").strip()[:64]}
        except Exception as e:  # noqa: BLE001
            return {"posted": False, "error": f"{type(e).__name__}: {e}"}

    def _close_drift_goal(self) -> dict:
        """Release the lease this probe took (guard-3419).

        Two narrowing guards, both deliberate: origin_signal equality means only
        goals THIS probe filed on THIS box can match, and pending-and-unclaimed
        means work someone has started is never yanked out from under them
        (guard-1007 — route via the board instead). Closed as `skipped`, never
        `completed`: no investigation happened, the condition resolved.

        Deliberately NOT gated on self.fired. `fired` lives in
        watchdog-prev-state.json, which is box-local and ephemeral — a reset or
        a fresh box zeroes it, and a goal filed by a previous episode would then
        be unclosable by this mechanism forever, which is the same
        filed-never-closed defect one level up.
        """
        origin_signal = self._origin_signal()
        try:
            from _paths import WORLD_DIR
            import importlib
            pf = importlib.import_module("pointer_freshness")
            open_goals = pf.open_goal_records(origin_signal, WORLD_DIR, self.ctx.agent_dir)
            if not open_goals:
                return {"attempted": False, "detail": None}
            closed, held = [], []
            from _runtime_bash import BASH as _bash
            for g in open_goals:
                gid = g.get("id")
                if not gid:
                    continue
                if g.get("status") != "pending" or g.get("claimed_by"):
                    held.append(f"{gid}:{g.get('status')}"
                                f"{'/claimed' if g.get('claimed_by') else ''}")
                    continue
                # Stated as an OBSERVATION and written BEFORE the status write, so
                # a partial failure leaves an open goal carrying a TRUE sentence
                # rather than a false claim that it was closed.
                note = ("agent-watchdog GitDriftProbe re-measured this box and every "
                        "git/capacity metric is back under threshold, so the drift this "
                        "goal was filed for is gone. No investigation was performed — the "
                        "condition resolved. The probe is retiring this goal as `skipped`; "
                        "if the status still reads open, that write did not land and the "
                        "goal is safe to close by hand (g-115-6128).")
                ok = True
                for field, value in (("outcome_note", note), ("status", "skipped")):
                    proc = subprocess.run(
                        [_bash, "core/scripts/aspirations-update-goal.sh", gid,
                         field, value, "--source", g.get("_source", "world")],
                        capture_output=True, text=True,
                        cwd=str(self.ctx.project_root_path), timeout=60,
                    )
                    if proc.returncode != 0:
                        held.append(f"{gid}:close-failed")
                        ok = False
                        break
                if ok:
                    closed.append(gid)
            parts = []
            if closed:
                parts.append("closed " + ",".join(closed))
            if held:
                parts.append("held " + ",".join(held))
            return {"attempted": True, "closed": closed, "held": held,
                    "detail": "; ".join(parts) or None}
        except Exception as e:  # noqa: BLE001 — a close failure must not kill the tick
            return {"attempted": True, "closed": [], "held": [],
                    "detail": f"error: {type(e).__name__}: {e}"}

    def to_dict(self) -> dict:
        return {"consecutive_breach": self.consecutive_breach,
                "fired": self.fired,
                "last_fetch_ts": self.last_fetch_ts,
                "push_basis": self.push_basis}

    def from_dict(self, state: dict) -> None:
        if isinstance(state, dict):
            self.consecutive_breach = int(state.get("consecutive_breach") or 0)
            self.fired = bool(state.get("fired"))
            try:
                self.last_fetch_ts = float(state.get("last_fetch_ts") or 0.0)
            except (TypeError, ValueError):
                self.last_fetch_ts = 0.0
            pb = state.get("push_basis")
            self.push_basis = pb if isinstance(pb, dict) and pb.get("url") else None


# ─────────────────────────────────────────────────────────────────────────────
# DependencyFunnelProbe — the claimable frontier against the fleet
# ─────────────────────────────────────────────────────────────────────────────

def _dependency_funnel_config() -> dict:
    """Read the `dependency_funnel` block from aspirations.yaml.

    guard-308: the literals below are a last-resort floor for a missing or
    corrupt config file, never a second copy of the policy — the YAML carries
    the same values and is what documents WHY.
    """
    cfg: dict = {}
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "aspirations.yaml"
        with cfg_path.open(encoding="utf-8") as f:
            cfg = ((yaml.safe_load(f) or {}).get("dependency_funnel") or {})
    except Exception:  # noqa: BLE001 — a config read must never kill the tick
        cfg = {}
    out = {
        "min_gated": 3,
        "ticks_to_file": 1,
        "ticks_to_revalidate": 50,
        "lookback_hours": 6,
        "liveness_hours": 1,
    }
    for key in out:
        val = cfg.get(key)
        if isinstance(val, (int, float)) and val >= 0:
            out[key] = val
    return out


class DependencyFunnelProbe(Probe):
    """The claimable frontier is ZERO while pending goals wait on a live goal.

    A fleet of N Bodies is at most as parallel as its frontier is wide, and
    until this probe nothing measured the width. Measured 2026-08-29 on a live
    8-Body deployment: 15 pending goals, every one gated (directly or through
    a chain) on ONE in-progress goal — five of eight Bodies closed for lack of
    work while one Body wrote the gate's module, and each worker's own close
    message ("all goals dependency-blocked") was the only trace. The
    dependencies were over-specified: the consumers needed the module's
    INTERFACE (its public names, already on disk), not its completion.

    Fires on `claimable_count == 0 and gated_count >= min_gated`. Files ONE
    Investigate goal into the root's own aspiration — the goal prescribes the
    interface-contract relaxation with exact commands, so an idle Body can do
    it — posts a coordination alert, and RETIRES the goal itself once the
    frontier is non-zero again (guard-3419: a dedup keyed on open-goal
    existence with no release path disables the detector permanently).
    Reducer-only: the reducer owns the queue and runs this tick every
    iteration close; a worker never calls iteration-close.

    The census is `_frontier.frontier_census` — the same implementation the
    `frontier-check.sh` CLI prints, so what a reader re-measures is what fired.
    """
    name = "dependency-funnel"

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.consecutive = 0
        self.fired = False

    # ---- measurement -----------------------------------------------------

    def _census(self, cfg: dict) -> dict:
        from _paths import WORLD_DIR, agents_root
        import _frontier
        return _frontier.frontier_census(
            WORLD_DIR, agents_root(),
            lookback_hours=float(cfg["lookback_hours"]),
            liveness_hours=float(cfg["liveness_hours"]))

    # ---- probe -----------------------------------------------------------

    def check(self) -> list[Event]:
        cfg = _dependency_funnel_config()
        try:
            census = self._census(cfg)
        except Exception as e:  # noqa: BLE001 — a census failure is not a funnel
            return [Event(
                probe=self.name, event="dependency_funnel_unmeasured",
                severity="info",
                payload={"agent": self.ctx.agent_name,
                         "error": f"{type(e).__name__}: {e}"},
                summary=f"{self.name}: census failed — {type(e).__name__}: {e}",
            )]

        funnel = (census["claimable_count"] == 0
                  and census["gated_count"] >= cfg["min_gated"])
        roots = census.get("roots") or []
        payload = {
            "agent": self.ctx.agent_name,
            "claimable_count": census["claimable_count"],
            "gated_count": census["gated_count"],
            "gated": census["gated"],
            "roots": roots[:5],
            "unknown_blockers": census["unknown_blockers"][:10],
            "in_progress": census["in_progress"],
            "deferred": census["deferred"],
            "blocked": census["blocked"],
            "bodies": census["bodies"],
            "parse_skipped": census["parse_skipped"],
            "thresholds": cfg,
            "consecutive": self.consecutive,
        }

        if funnel:
            self.consecutive += 1
            payload["consecutive"] = self.consecutive
            if not roots:
                # Gated on ids no queue holds: a dangling-reference defect, not
                # a funnel a relaxation can open. Say so rather than filing a
                # goal that names no root (guard-1955).
                return [Event(
                    probe=self.name, event="dependency_funnel_unresolvable",
                    severity="info", payload=payload,
                    summary=(f"{self.name}: {census['gated_count']} pending goals "
                             f"gated only on ids absent from every queue — "
                             f"{payload['unknown_blockers'][:3]}"),
                )]
            root = roots[0]
            _revalidate = cfg.get("ticks_to_revalidate") or 0
            if self.consecutive >= cfg["ticks_to_file"] and (
                    not self.fired
                    or (_revalidate and self.consecutive % _revalidate == 0)
            ):
                goal = self._file_funnel_goal(census, root, cfg)
                if goal.get("filed") or goal.get("dedup"):
                    self.fired = True
                payload["goal"] = goal
                payload["board"] = self._post_board_alert(census, root, goal)
                # A goal filed for an EARLIER root is stale the moment the
                # funnel moves; retire it in the same tick (keep only this root).
                payload["retired_other_roots"] = self._retire_funnel_goals(
                    keep_roots={root["id"]})
                return [Event(
                    probe=self.name, event="dependency_funnel", severity="critical",
                    payload=payload,
                    summary=(f"{self.name}: frontier 0 — {census['gated_count']} pending "
                             f"goals gated on {root['id']} ({root['status']}"
                             f"{', claimed by ' + str(root['claimed_by']) if root.get('claimed_by') else ''}); "
                             f"bodies active={census['bodies']['active']} "
                             f"closed-recent={census['bodies']['closed_recent']} "
                             f"({goal.get('goal_id') or goal.get('error')})"),
                )]
            return []

        cleared = self._retire_funnel_goals(keep_roots=set())
        self.consecutive = 0
        was_fired = self.fired
        self.fired = False
        if was_fired or cleared.get("closed"):
            payload["closed"] = cleared
            return [Event(
                probe=self.name, event="dependency_funnel_cleared", severity="info",
                payload=payload,
                summary=(f"dependency funnel cleared — frontier "
                         f"{census['claimable_count']} claimable"
                         + (f" ({cleared.get('detail')})" if cleared.get("detail") else "")),
            )]
        return []

    # ---- escalation ------------------------------------------------------

    @staticmethod
    def _origin_signal(root_id: str) -> str:
        # ROOT-scoped: the funnel is a property of the gate goal, not of a box
        # or an agent, so any Body on any box may open it and the same root
        # must never carry two open goals.
        import _frontier
        return f"{_frontier.FUNNEL_SIGNAL_PREFIX}{root_id}"

    def _file_funnel_goal(self, census: dict, root: dict, cfg: dict) -> dict:
        """File one deduped Investigate goal into the ROOT's aspiration.
        Fail-open ({filed: False, error})."""
        origin_signal = self._origin_signal(root["id"])
        try:
            from _paths import WORLD_DIR
            import importlib
            pf = importlib.import_module("pointer_freshness")
            if pf.open_goal_exists(origin_signal, WORLD_DIR, self.ctx.agent_dir):
                return {"filed": False, "dedup": True, "goal_id": None,
                        "error": "open goal exists (dedup)"}
            gated = census["gated"]
            gated_txt = ", ".join(gated[:12]) + (" …" if census["gated_count"] > 12 else "")
            bodies = census["bodies"]
            body = {
                "title": (f"Investigate: dependency funnel — {census['gated_count']} pending "
                          f"goals gated on {root['id']} ({root['title'][:60]}); "
                          f"claimable frontier is 0"),
                "priority": "HIGH",
                "participants": ["agent"],
                "category": "framework-coordination",
                "origin_signal": origin_signal,
                "description": (
                    f"agent-watchdog DependencyFunnelProbe measured the claimable frontier "
                    f"at 0: {census['pending_total']} pending goals, {census['gated_count']} "
                    f"of them gated (directly or through a chain) on {root['id']} "
                    f"\"{root['title']}\" (status {root['status']}"
                    f"{', claimed by ' + str(root['claimed_by']) if root.get('claimed_by') else ''}), "
                    f"with {bodies['active']} Bodies active and {bodies['closed_recent']} "
                    f"closed for lack of work in the last {cfg['lookback_hours']}h. "
                    f"A fleet is at most as parallel as its frontier is wide: every Body "
                    f"beyond the frontier idles. Gated goals: {gated_txt}. "
                    f"REMEDY — relax each consumer to the root's INTERFACE, not its "
                    f"completion (rb-4003 pattern). For each gated goal: "
                    f"(1) read the root goal's description and name the artifact it "
                    f"produces (a module, script or file) and the public names it promises; "
                    f"(2) probe the artifact ON DISK — `ls <path>` and "
                    f"`grep -n '^def \\|^class ' <path>`; if the promised names exist the "
                    f"consumer needs only the interface: clear the dependency with "
                    f"`bash core/scripts/aspirations-update-goal.sh <consumer-id> blocked_by '[]'` "
                    f"and append a coordination note to its description via "
                    f"`aspirations-update-goal.sh <consumer-id> description \"<existing text> "
                    f"COORDINATION: build against <artifact>'s interface as it exists on disk; "
                    f"do NOT edit <artifact> — another Body owns it; work around locally and "
                    f"file an Idea goal for anything missing\"`; "
                    f"(3) if the artifact does not exist yet, split the root instead: file a "
                    f"small `Build: <artifact> interface stub + fixtures` goal (the promised "
                    f"names with docstrings and NotImplementedError bodies, plus the fixtures "
                    f"the consumers test against) and re-point the consumers' blocked_by at "
                    f"that stub goal; "
                    f"(4) never edit the root's artifact from a consumer goal and never mark "
                    f"the root completed to unblock — the status field is the queue's state "
                    f"(guard-2852). Re-measure with `bash core/scripts/frontier-check.sh`. "
                    f"This goal is a SNAPSHOT: the probe retires it automatically once the "
                    f"frontier is non-zero. Auto-filed by DependencyFunnelProbe."
                ),
            }
            from _runtime_bash import BASH as _bash
            _override_reason = (
                "DependencyFunnelProbe owns exact root-scoped dedup via "
                "open_goal_exists(origin_signal) plus a retire path; the "
                "goal-dup-gate's structural check false-positives on the goal ids "
                "and file paths this goal must quote to be actionable.")
            proc = subprocess.run(
                [_bash, "core/scripts/aspirations-add-goal.sh", root["asp_id"],
                 "--source", root.get("source") or "world",
                 "--override-duplication", _override_reason],
                input=json.dumps(body, ensure_ascii=True),
                capture_output=True, text=True,
                cwd=str(self.ctx.project_root_path), timeout=60,
            )
            if proc.returncode != 0:
                return {"filed": False, "goal_id": None,
                        "error": (proc.stderr or proc.stdout or "non-zero exit").strip()[:200]}
            try:
                goal_id = json.loads(proc.stdout).get("id")
            except (json.JSONDecodeError, AttributeError):
                goal_id = None
            return {"filed": True, "goal_id": goal_id, "error": None}
        except Exception as e:  # noqa: BLE001 — filing must not kill the event
            return {"filed": False, "goal_id": None, "error": f"{type(e).__name__}: {e}"}

    def _post_board_alert(self, census: dict, root: dict, goal: dict) -> dict:
        """One coordination post per episode — the FLEET-visible half. Every
        Body reads the coordination channel, and the Bodies that closed for
        lack of work are exactly the audience."""
        try:
            from _runtime_bash import BASH as _bash
            bodies = census["bodies"]
            text = (f"DependencyFunnelProbe: claimable frontier is 0 — "
                    f"{census['gated_count']} pending goals gated on {root['id']} "
                    f"({root['status']}); bodies active={bodies['active']} "
                    f"closed-recent={bodies['closed_recent']}. "
                    f"Goal: {goal.get('goal_id') or goal.get('error')}. "
                    f"Remedy: relax consumers to the root's interface (see the goal).")
            proc = subprocess.run(
                [_bash, "core/scripts/board-post.sh", "--channel", "coordination",
                 "--type", "blocked",
                 "--tags", f"dependency-funnel,{root['id']},auto-probe"],
                input=text, capture_output=True, text=True,
                cwd=str(self.ctx.project_root_path), timeout=60,
            )
            if proc.returncode != 0:
                return {"posted": False,
                        "error": (proc.stderr or proc.stdout or "non-zero exit").strip()[:160]}
            return {"posted": True, "msg_id": (proc.stdout or "").strip()[:64]}
        except Exception as e:  # noqa: BLE001
            return {"posted": False, "error": f"{type(e).__name__}: {e}"}

    def _retire_funnel_goals(self, keep_roots: set) -> dict:
        """Retire every open, unclaimed funnel goal whose root is not in
        `keep_roots` (guard-3419 release path). Closed as `skipped`, never
        `completed`: no investigation happened, the frontier reopened.

        Reads the queues directly rather than trusting `self.fired`: the flag
        lives in box-local, ephemeral tick state, and a goal filed by an
        earlier episode (or another box) must still be retirable here.
        Pending-and-unclaimed only — work someone has started is never yanked
        out from under them (guard-1007).
        """
        try:
            from _paths import WORLD_DIR, agents_root
            import _frontier
            goal_index, _asps, _stats = _frontier.load_goal_index(WORLD_DIR, agents_root())
            candidates = [g for g in _frontier.open_funnel_goals(goal_index)
                          if g["root"] not in keep_roots]
            if not candidates:
                return {"attempted": False, "detail": None}
            from _runtime_bash import BASH as _bash
            closed, held = [], []
            for g in candidates:
                gid = g["id"]
                note = ("agent-watchdog DependencyFunnelProbe re-measured the queues and the "
                        "claimable frontier is no longer 0 for this root, so the funnel this "
                        "goal was filed for is gone. No investigation was performed — the "
                        "condition resolved. The probe is retiring this goal as `skipped`; "
                        "if the status still reads open, that write did not land and the "
                        "goal is safe to close by hand.")
                ok = True
                for field, value in (("outcome_note", note), ("status", "skipped")):
                    proc = subprocess.run(
                        [_bash, "core/scripts/aspirations-update-goal.sh", gid,
                         field, value, "--source", g.get("source") or "world"],
                        capture_output=True, text=True,
                        cwd=str(self.ctx.project_root_path), timeout=60,
                    )
                    if proc.returncode != 0:
                        held.append(f"{gid}:close-failed")
                        ok = False
                        break
                if ok:
                    closed.append(gid)
            parts = []
            if closed:
                parts.append("closed " + ",".join(closed))
            if held:
                parts.append("held " + ",".join(held))
            return {"attempted": True, "closed": closed, "held": held,
                    "detail": "; ".join(parts) or None}
        except Exception as e:  # noqa: BLE001 — a close failure must not kill the tick
            return {"attempted": True, "closed": [], "held": [],
                    "detail": f"error: {type(e).__name__}: {e}"}

    def to_dict(self) -> dict:
        return {"consecutive": self.consecutive, "fired": self.fired}

    def from_dict(self, state: dict) -> None:
        if isinstance(state, dict):
            self.consecutive = int(state.get("consecutive") or 0)
            self.fired = bool(state.get("fired"))


def _watchdog_infra_components() -> list[dict]:
    """Components this box should poll, from the WORLD overlay
    `world/config/watchdog-infra-components.yaml`. Empty list on any failure.

    Domain-free by construction: core names no component and no endpoint. A
    deployment opts a component in by editing its own world config; a fresh
    world with no such file polls nothing and this probe is inert.

    Imported lazily so a world-resolution problem can degrade THIS probe
    rather than the whole watchdog — the other eleven probes have nothing to
    do with the world overlay and must keep running without it.

    INERT UNDER PYTEST unless MIND_WATCHDOG_INFRA_TEST is set. The role tests
    call check() on the whole reducer set against the REAL project root, so
    without this the suite would shell out to live probes, hit the network, and
    write the real world/infra-health.yaml — a unit test mutating production
    monitoring state. Same PYTEST_CURRENT_TEST chokepoint the daemon-spawn
    refusal uses (g-115-3329). It gates only the CONFIG, not the probe logic:
    a test exercising this probe sets .components directly and monkeypatches
    _infra_health_check, so the transition machinery stays fully covered.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
            "MIND_WATCHDOG_INFRA_TEST"):
        return []
    try:
        from _world_config import load_world_config
        cfg = load_world_config("watchdog-infra-components", {})
    except Exception:
        return []
    raw = cfg.get("components")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        comp = str(entry.get("component") or "").strip()
        if not comp:
            continue
        try:
            interval = int(entry.get("interval_minutes",
                                     InfraComponentProbe.DEFAULT_INTERVAL_MIN))
        except (TypeError, ValueError):
            interval = InfraComponentProbe.DEFAULT_INTERVAL_MIN
        out.append({"component": comp, "interval_minutes": max(1, interval)})
    return out


def _infra_health_check(root: Path, agent: str, component: str,
                        timeout: float = 60.0) -> dict:
    """Run one `infra-health check <component>` and return its parsed JSON.

    Calls infra-health.py with THIS interpreter rather than shelling through
    the .sh wrapper. The wrapper's whole body is `exec python3 infra-health.py
    "$@"` after sourcing _paths/_platform, so bash buys nothing here — and it
    costs the CreateProcess/System32 bash lottery (rb-225/rb-247), which on
    Windows can block forever on a wedged WSL launcher. A monitoring probe
    must not be able to hang the loop it monitors.

    Returns {} when the check could not be RUN (non-zero rc, timeout,
    unparseable output). That is deliberately distinct from a check that ran
    and reported failure: the caller reports the two differently, because
    "the prober is broken" and "the target is broken" need different humans.
    """
    try:
        r = subprocess.run(
            [sys.executable, str(root / "core" / "scripts" / "infra-health.py"),
             "check", component],
            cwd=str(root), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "MIND_AGENT": agent},
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if r.returncode != 0:
        return {}
    try:
        parsed = json.loads((r.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class InfraComponentProbe(Probe):
    """Polls opted-in infra-health components on a cadence, so a silent
    external failure is caught by a clock instead of by someone noticing.

    WHY THIS EXISTS. infra-health carries dozens of registered components and
    nothing ran `check` on a schedule — every entry was probed only when a
    human or a goal happened to ask, so components sat unread for weeks and
    the streak-notifier downstream had nothing fresh to reason about. This
    probe is the narrow fix: a short opt-in list, polled on the tick the loop
    already pays for. The broad question (should EVERYTHING be on a cadence,
    and what owns check-all) is deliberately out of scope and tracked
    separately — a poller that checks everything is a poller someone disables.

    THE THREE OUTCOMES ARE NOT TWO. A check can report the target is broken,
    report the target is fine, or fail to reach the target at all. That last
    one — infra-health's `no_target` — is a fact about the OBSERVER, not the
    target: most boxes in a fleet have no route to a given host, and treating
    unreachable as failure would alert continuously about healthy hardware
    while training everyone to ignore the probe. So no_target is silent, and
    it does NOT clear a prior failure either (losing sight of a broken thing
    is not the same as it being fixed).

    TRANSITION-BASED, like every other probe here: an event fires when the
    condition CHANGES, not on every tick, so a component down for a day
    produces one critical event rather than a hundred.

    Not in WORKER_SAFE_PROBES, on purpose. Nothing it reads is reducer-shaped,
    so a worker COULD run it — but two Bodies polling the same endpoint on the
    same cadence means two alerts for one fault, and a duplicate alert is
    worse than a late one. One poller per agent: the reducer.

    Cross-tick state: last_polled (component -> epoch seconds of last poll)
    and condition (component -> last reported condition, for transitions).
    """

    name = "infra-component"
    DEFAULT_INTERVAL_MIN = 30
    MAX_PER_TICK = 1        # bound the cost added to any single iteration close
    CHECK_TIMEOUT_S = 60.0

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.components = _watchdog_infra_components()
        self.last_polled: dict = {}   # component -> epoch seconds
        self.condition: dict = {}     # component -> "ok" | "failed" | "error"

    def check(self) -> list[Event]:
        if not self.components:
            return []

        now = time.time()
        due = []
        for spec in self.components:
            last = self.last_polled.get(spec["component"])
            if last is None:
                overdue = float("inf")          # never polled — poll now
            else:
                elapsed = now - float(last)
                # A negative elapsed means the wall clock moved backwards
                # (NTP step, VM resume). Re-poll rather than wait out a
                # bogus interval.
                if 0 <= elapsed < spec["interval_minutes"] * 60:
                    continue
                overdue = elapsed
            due.append((overdue, spec))

        if not due:
            return []
        due.sort(key=lambda d: d[0], reverse=True)   # most overdue first

        events: list[Event] = []
        for _, spec in due[:self.MAX_PER_TICK]:
            comp = spec["component"]
            self.last_polled[comp] = now
            result = _infra_health_check(
                self.ctx.project_root_path, self.ctx.agent_name, comp,
                timeout=self.CHECK_TIMEOUT_S,
            )
            prev = self.condition.get(comp)

            if not result:
                # The check could not be run at all — a misconfigured
                # component name, a missing probe script, a timeout. Report
                # once so it cannot sit silently broken forever, then stay
                # quiet: an unrunnable prober is not an incident, it is
                # maintenance.
                if prev != "error":
                    self.condition[comp] = "error"
                    events.append(Event(
                        probe=self.name, event="infra_check_unrunnable",
                        severity="info",
                        payload={"component": comp},
                        summary=(f"{self.name}: infra_check_unrunnable — "
                                 f"'{comp}' could not be checked (bad component "
                                 f"name, missing probe script, or timeout). "
                                 f"Nothing is watching it until this is fixed."),
                    ))
                continue

            status = str(result.get("status") or "")

            if status == "failed":
                if prev != "failed":
                    self.condition[comp] = "failed"
                    events.append(Event(
                        probe=self.name, event="infra_component_failed",
                        severity="critical",
                        payload={"component": comp,
                                 "error": result.get("error"),
                                 "detail": result.get("detail")},
                        summary=(f"{self.name}: infra_component_failed — "
                                 f"{comp}: {result.get('error') or result.get('detail') or 'no detail'}"),
                    ))
            elif status == "ok":
                if prev == "failed":
                    events.append(Event(
                        probe=self.name, event="infra_component_recovered",
                        severity="info",
                        payload={"component": comp, "detail": result.get("detail")},
                        summary=(f"{self.name}: infra_component_recovered — "
                                 f"{comp}: {result.get('detail') or 'ok'}"),
                    ))
                self.condition[comp] = "ok"
            # no_target and any unrecognised status: silent, and the prior
            # condition is left alone (see class docstring).

        return events

    def to_dict(self) -> dict:
        return {"last_polled": self.last_polled, "condition": self.condition}

    def from_dict(self, state: dict) -> None:
        if not state:
            return
        lp = state.get("last_polled")
        if isinstance(lp, dict):
            self.last_polled = {k: v for k, v in lp.items()
                                if isinstance(v, (int, float))}
        cond = state.get("condition")
        if isinstance(cond, dict):
            self.condition = {k: str(v) for k, v in cond.items()}


# Probes that read ONLY box-level state — daemon port, mirror, clock, memory,
# store freshness. None of them reads agent-state, running-session-id or
# runner-heartbeat, so they are correct on a worker Body exactly as written.
# Audited by name against the class bodies (): the other five each key
# on reducer-shaped state that a worker deliberately does not have.
# NOTE THE HYPHENS. Probe.name uses hyphens ("daemon-health"), not underscores.
# The first draft of this set wrote underscores and matched exactly ONE probe
# (freshness — the only name with no separator), so a worker silently registered
# 1 probe instead of 5 while the wiring looked correct. Caught only by printing
# the registered set and diffing it against the reducer's, which is why
# test_worker_safe_probe_names_all_exist asserts every name resolves to a real
# probe: a typo'd filter is indistinguishable from a working one at the call site.
WORKER_SAFE_PROBES = frozenset({
    "daemon-health", "clock-skew", "freshness", "mirror-wedge", "memory-headroom",
    # git-drift joins the worker set deliberately, and the incident record is the
    # argument: cc-07 forked at 342 ahead and cc-08 held 223 unpushed commits, and
    # neither box was the reducer. A worker Body has the same checkout, the same
    # host disk, and pushes its own carrier ref — so this probe is not merely
    # SAFE there, it is where the condition it detects actually accumulates
    # ().
    "git-drift",
})


class WorkerStallProbe(Probe):
    """Peer-side worker-Body stall detection ().

    THE ONLY PROBE HERE THAT WATCHES A BOX OTHER THAN ITS OWN, and that is the
    whole design. Four of this file's probes are structurally INERT in worker
    shape -- a worker box is `agent-state: IDLE` BY DESIGN, so `classify_stalled`
    returns None for ANY diary age, and RunningSidProbe / StopHookBlockProbe read
    a `running-session-id` a worker is forbidden to set. So even after the tick is
    wired into the worker loop, the stall detectors do not fire there. Worse, an
    in-loop tick dies WITH the loop, and the incident that motivated this probe
    (cc-08, ~2h stalled on a lost login) was process death: there was no loop left
    to tick. Detection of that class has to be out-of-process, which means a peer.

    This probe therefore runs on the REDUCER, inside the tick that already fires
    every iteration from iteration-close.sh -- no new wiring, no new cron.

    Heavy logic lives in worker_stall.py, lazy-imported inside check() so a
    syntax error there can never crash the tick (FreshnessProbe's pattern).

    Transition semantics: a body's verdict is remembered across ticks, so a
    persistent stall logs ONCE per episode rather than every tick, and emits a
    `worker_stall_cleared` info event when it recovers. Without that, a genuinely
    wedged worker would produce an event every iteration until someone noticed --
    which trains the reader to filter the log (guard-2418 class).

    Advisory only: it reports and never mutates. That is deliberate and is the
    point of the goal -- `stranded-claim-sweep` ALREADY computes this exact
    signal, but only after a 120-minute grace and only to silently release the
    claim, so the condition was observable-in-principle and reported nowhere.
    """
    name = "worker-stall"

    def __init__(self, ctx: WatchdogContext) -> None:
        super().__init__(ctx)
        self.prev: dict = {}  # {sid: verdict}

    def initialize(self) -> None:
        # Start from an empty baseline rather than capturing current-as-prev:
        # a stall that is ALREADY in progress at the first tick is exactly the
        # case worth reporting, and capture-as-prev would swallow it.
        self.prev = {}

    def check(self) -> list[Event]:
        try:
            from worker_stall import scan, is_alerting  # type: ignore
            from _paths import WORLD_DIR, agents_root  # type: ignore
        except Exception as e:
            sys.stderr.write(f"agent-watchdog: worker-stall import failed: {e}\n")
            return []
        try:
            report = scan(Path(agents_root()), Path(WORLD_DIR) / "aspirations.jsonl")
        except Exception as e:
            sys.stderr.write(f"agent-watchdog: worker-stall scan failed: {e}\n")
            return []

        events: list[Event] = []
        seen: dict = {}

        # INSTRUMENT-FAULT SIGNAL (guard-1893 caller half; guard-1977).
        # scan() now reports whether it could actually SEE the fleet. Without
        # this branch a blind scan emits zero events -- byte-identical to "all
        # bodies healthy" -- which is the exact silent-all-clear this probe
        # exists to end, reintroduced one layer up. Uses the same prev/seen
        # transition semantics as the verdicts below, so a persistent fault
        # logs ONCE per episode rather than every tick.
        enum_meta = report.get("enumeration") or {}
        # `all_carriers_unreadable` is load-bearing and was the gap ():
        # when every carrier fetch fails, the ROSTER listing still succeeds, so
        # `complete` is True, nothing raises out of the scan so `rows_dropped`
        # is 0, and the rows survive into `bodies` so `enumeration_lost_
        # everything` is False. Every term below read clean and no body alerted,
        # so this probe emitted ZERO events on a fleet it could not see at all.
        blind = (
            not enum_meta.get("complete", False)
            or report.get("enumeration_lost_everything")
            or (report.get("rows_dropped") or 0) > 0
            or report.get("all_carriers_unreadable")
        )
        # The third fallback is not padding. On the all-unreadable path `reason`
        # is None (the roster listing SUCCEEDED) and `first_drop_error` is None
        # (nothing raised), so the original two-term chain rendered this
        # diagnostic's cause as the literal string "None" -- a probe that
        # reports it cannot see, and cannot say why, is the next silent layer
        # one level up (guard-1977).
        blind_cause = (
            enum_meta.get("reason")
            or report.get("first_drop_error")
            or report.get("first_carrier_read_error")
            or "every carrier parsed to an unreadable verdict"
        )
        health = "blind" if blind else "ok"
        seen["__enumeration__"] = health
        if blind and self.prev.get("__enumeration__") != "blind":
            events.append(Event(
                probe=self.name,
                event="worker_stall_probe_blind",
                severity="warning",
                payload={
                    "enumeration": enum_meta,
                    "carriers_found": report.get("carriers_found"),
                    "rows_dropped": report.get("rows_dropped"),
                    "first_drop_error": report.get("first_drop_error"),
                    "claims_read_via": report.get("claims_read_via"),
                    "carrier_read_errors": report.get("carrier_read_errors"),
                    "first_carrier_read_error": report.get("first_carrier_read_error"),
                    "all_carriers_unreadable": report.get("all_carriers_unreadable"),
                },
                summary=(
                    f"worker-stall probe cannot bound the fleet: "
                    f"read_via={enum_meta.get('read_via')} "
                    f"complete={enum_meta.get('complete')} "
                    f"agents={enum_meta.get('agents_enumerated')} "
                    f"dropped={report.get('rows_dropped')} "
                    f"unreadable_carriers={report.get('all_carriers_unreadable')} "
                    f"carrier_read_errors={report.get('carrier_read_errors')} "
                    f"-- a zero-alert result here means UNKNOWN, not healthy "
                    f"({blind_cause})"
                ),
            ))
        elif not blind and self.prev.get("__enumeration__") == "blind":
            events.append(Event(
                probe=self.name,
                event="worker_stall_probe_blind_cleared",
                severity="info",
                payload={"enumeration": enum_meta},
                summary="worker-stall probe can bound the fleet again",
            ))

        for body in report.get("bodies") or []:
            sid = body.get("sid")
            verdict = body.get("verdict")
            seen[sid] = verdict
            was = self.prev.get(sid)
            if is_alerting(verdict) and not is_alerting(was or ""):
                events.append(Event(
                    probe=self.name,
                    event="worker_stall",
                    severity="critical",
                    payload={**body, "stale_minutes": report.get("stale_minutes"),
                             "degraded_read": report.get("degraded_read")},
                    summary=(
                        # TWO alerting verdicts since , and they need
                        # different sentences. This summary used to end "while
                        # holding {held_goal}" unconditionally -- for a Body
                        # that died BETWEEN units held_goal is None BY
                        # DEFINITION (holding no claim is the whole condition),
                        # so the single form renders "while holding None",
                        # which reads as a probe bug and hides the actionable
                        # fact: it never wrote a close.
                        f"WORKER STALL: {body.get('agent')} body {sid} on "
                        f"{body.get('host')} last ticked "
                        f"{body.get('carrier_age_minutes')}m ago while holding "
                        f"{body.get('held_goal')}"
                        if body.get("held_goal") is not None else
                        f"WORKER STALL (died between units): {body.get('agent')} "
                        f"body {sid} on {body.get('host')} last ticked "
                        f"{body.get('carrier_age_minutes')}m ago holding no claim, "
                        f"and never recorded a close "
                        f"(body_state={body.get('body_state')!r})"
                    ),
                ))
            elif is_alerting(was or "") and not is_alerting(verdict):
                events.append(Event(
                    probe=self.name,
                    event="worker_stall_cleared",
                    severity="info",
                    payload={**body},
                    summary=(f"worker stall cleared: {body.get('agent')} body {sid} "
                             f"-> {verdict}"),
                ))
        self.prev = seen
        return events

    def to_dict(self) -> dict:
        return {"prev": self.prev}

    def from_dict(self, state: dict) -> None:
        self.prev = dict(state.get("prev") or {})


def build_probes(ctx: WatchdogContext) -> list[Probe]:
    """Single registration point. Add new probes here.

    On a worker Body the set is FILTERED, not merely reordered — see
    `WORKER_SAFE_PROBES` and the audit in `is_worker_body`. Enabling the whole
    set on a worker would install five probes that are structurally incapable
    of firing there, which reads as coverage and is not (g-306-240).

    WorkerStallProbe is deliberately NOT in WORKER_SAFE_PROBES, and that is the
    seam where the two halves of g-306-240 meet. It is the PEER-side half: it
    watches OTHER boxes and must run on the reducer, because the class it exists
    to catch (process death, lost auth) kills the in-loop tick along with the
    loop. Registering it on a worker would have each worker watching itself with
    a probe whose entire premise is out-of-process observation — coverage in
    appearance only, which is the same defect the filter below prevents.
    """
    probes = [
        WorkerStallProbe(ctx),
        RunningSidProbe(ctx),
        HeartbeatProbe(ctx),
        ClaimHeartbeatProbe(ctx),
        StalledProbe(ctx),
        BackgroundJobProbe(ctx),
        StopHookBlockProbe(ctx),
        DaemonHealthProbe(ctx),
        ClockSkewProbe(ctx),
        FreshnessProbe(ctx),
        MirrorWedgeProbe(ctx),
        MemoryHeadroomProbe(ctx),
        GitDriftProbe(ctx),
        InfraComponentProbe(ctx),
        DependencyFunnelProbe(ctx),
    ]
    if ctx.body_role == "worker":
        return [p for p in probes if p.name in WORKER_SAFE_PROBES]
    return probes


def is_worker_body(env: Optional[dict] = None) -> bool:
    """True when this process is a worker Body rather than the reducer.

    Reads BODY_ROLE, which the PreToolUse bash hook injects on every Bash call
    (bash-agent-inject.py:500) and which six other scripts already consume — an
    established signal, not a new one invented here.

    WHY THE ROLE SPLIT EXISTS AT ALL. Until g-306-240, `agent-watchdog.py --tick`
    had exactly ONE caller in the tree — iteration-close.sh:2554 — and the worker
    loop deliberately skips iteration-close. So NO probe had ever executed on a
    worker box: not StalledProbe, not HeartbeatProbe, none. Same structural fact
    behind g-306-233 (workers never pulled) and g-306-235.

    WHY FIVE PROBES ARE EXCLUDED RATHER THAN FIXED — measured on a live worker
    (cc-08, 2026-08-06), not inferred:
      - A worker is `agent-state=IDLE` + `agent-mode=autonomous` BY DESIGN, and
        it writes NO `runner-heartbeat` and NO `running-session-id`; its liveness
        files are `sessions/<SID>/body-heartbeat` and the syncable
        `session/body-heartbeat-<SID>.json`.
      - So `classify_stalled` returns None on a worker at its FIRST guard
        (`agent_state != "RUNNING"`), and would return None at the second anyway
        (`runner-heartbeat` absent → hb_age is None). StalledProbe is doubly
        dead here; enabling it would add a permanent no-op.
      - RunningSidProbe, HeartbeatProbe, BackgroundJobProbe and StopHookBlockProbe
        read the same reducer-shaped files and either no-op or false-fire.

    WHY StalledProbe IS NOT SIMPLY TAUGHT THE WORKER SHAPE — the measurement that
    decided it. On a worker the execution-diary records ONE entry per GOAL, at
    claim time; there is no mid-unit progress write. So diary-staleness and
    unit-duration are the SAME quantity. Measured consecutive gaps on cc-08:
    34min, 56min, 92min, 28min, 15min — and the 92min one was a real stall while
    the others were healthy work. No threshold separates them, so a diary-based
    stall detector on a worker is a false-positive generator, and the goal's
    load-bearing negative (a healthy worker must not false-fire) is exactly what
    it would violate. Detecting a stalled worker needs a signal that advances
    DURING a unit; the diary is not one.
    """
    e = os.environ if env is None else env
    return (e.get("BODY_ROLE") or "").strip().lower() == "worker"


def run_once(ctx: WatchdogContext, log_path: Path) -> int:
    """Single snapshot — initializes each probe and emits a 'snapshot' event
    capturing current state. Useful for ad-hoc inspection or building a
    baseline before --loop. Returns number of probes inspected."""
    ctx.new_cycle()
    probes = build_probes(ctx)
    for probe in probes:
        try:
            probe.initialize()
            # Synthetic snapshot event: report current state without diff.
            if isinstance(probe, RunningSidProbe):
                snap = probe.prev_state
                event = Event(
                    probe=probe.name,
                    event="snapshot",
                    severity="info",
                    payload={
                        "current": {
                            "exists": snap.get("exists"),
                            "sid": snap.get("sid"),
                            "mtime": snap.get("mtime"),
                        }
                    },
                    include_processes=False,
                    summary=f"{probe.name}: snapshot (exists={snap.get('exists')} "
                            f"sid={(snap.get('sid') or '')[:8] or '-'})",
                )
                emit_event(ctx, log_path, event)
        except Exception as e:
            sys.stderr.write(
                f"agent-watchdog: {probe.name} initialize/snapshot failed: "
                f"{type(e).__name__}: {e}\n"
            )
    return len(probes)


def run_tick(ctx: WatchdogContext, log_path: Path, state_path: Path) -> int:
    """Single tick — load prev state, run each probe.check(), emit transitions,
    save new state. Designed for periodic invocation from iteration-close.sh.

    Replaces the daemon model (run_loop) with an iteration-cadence check that
    needs no detached process and works identically on Windows/macOS/Linux.

    State file schema: {probe_name: probe.to_dict()}. Atomic .tmp + replace so
    a torn write never corrupts a future tick. Missing or corrupt file → empty
    state (first-tick baseline, no events emitted because each probe.initialize
    captures current = prev).

    Returns the number of transition events emitted. Used only for the stderr
    summary line; the script's exit code is unaffected by transition count.
    """
    ctx.new_cycle()

    # Load prior state. Missing or corrupt → empty (treat as first tick).
    prior: dict = {}
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                prior = json.load(f) or {}
        except (json.JSONDecodeError, OSError) as e:
            sys.stderr.write(
                f"agent-watchdog: prev-state read failed ({type(e).__name__}: "
                f"{e}) — treating as first tick\n"
            )
            prior = {}

    probes = build_probes(ctx)

    # Seed each probe from prior state if available; otherwise call its
    # initialize() so it captures current state as baseline. First tick after
    # a fresh install (or after recovery clears the state file) emits no
    # events because prev == current after initialize.
    for probe in probes:
        try:
            saved = prior.get(probe.name)
            if saved is not None:
                probe.from_dict(saved)
            else:
                probe.initialize()
        except Exception as e:
            sys.stderr.write(
                f"agent-watchdog: {probe.name} seed raised "
                f"{type(e).__name__}: {e} — skipping this probe\n"
            )

    # Run each probe's check(); emit transitions. Per-probe fail-open — one
    # broken probe must not silence the others.
    transitions = 0
    for probe in probes:
        try:
            events = probe.check()
            for event in events:
                emit_event(ctx, log_path, event)
                transitions += 1
        except Exception as e:
            sys.stderr.write(
                f"agent-watchdog: {probe.name} check raised "
                f"{type(e).__name__}: {e}\n"
            )

    # Save new state atomically. .tmp + os.replace is cross-platform safe.
    new_state: dict = {}
    for probe in probes:
        try:
            new_state[probe.name] = probe.to_dict()
        except Exception as e:
            sys.stderr.write(
                f"agent-watchdog: {probe.name} to_dict raised "
                f"{type(e).__name__}: {e}\n"
            )
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, state_path)
    except OSError as e:
        sys.stderr.write(
            f"agent-watchdog: state save to {state_path} failed: {e}\n"
        )

    return transitions


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Per-agent session observability probes (periodic check)."
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--tick",
        action="store_true",
        help="Periodic tick (default): load prev state, run probes, emit "
             "transitions, save new state. Invoked from iteration-close.sh.",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Single snapshot without diff — ad-hoc inspection only.",
    )
    ap.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Override event-log path. Default: "
             "<project>/core/logs/watchdog-<agent>.jsonl",
    )
    ap.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Override tick-state path. Default: "
             "<agent>/session/watchdog-prev-state.json",
    )
    args = ap.parse_args()

    agent_name = os.environ.get("MIND_AGENT", "").strip()
    if not agent_name:
        sys.stderr.write(
            "agent-watchdog: MIND_AGENT is required — refusing to run.\n"
            "  Invoke via iteration-close.sh productivity-check, or set "
            "MIND_AGENT explicitly.\n"
        )
        return 2

    root = project_root()
    # Phase 2.5.D: agent dirs live under agents/ parent — routed through the
    # canonical _paths.agent_dir() helper so AGENTS_PARENT_DIR stays in sync
    # across all 5 declared sync sites + this file (CLAUDE.md "Agent-dir
    # Resolution"). Direct `root / "agents" / name` is the sync-drift
    # landmine fresh-eyes-code F2 surfaced 2026-05-19.
    agent_dir = _paths_agent_dir(agent_name)
    if not agent_dir.exists():
        sys.stderr.write(
            f"agent-watchdog: agent directory does not exist: {agent_dir}\n"
        )
        return 3

    log_path = args.log or (root / "core" / "logs" / f"watchdog-{agent_name}.jsonl")
    state_path = args.state or (agent_dir / "session" / "watchdog-prev-state.json")
    ctx = WatchdogContext(
        agent_name=agent_name,
        agent_dir=agent_dir,
        project_root_path=root,
        body_role="worker" if is_worker_body() else "reducer",
    )
    if ctx.body_role == "worker":
        # Say so on stderr. A filtered run must never be mistaken for a full one:
        # this tick is the FIRST watchdog coverage a worker box has ever had, and
        # silence about the filtering is how partial coverage gets read as total.
        sys.stderr.write(
            "agent-watchdog: BODY_ROLE=worker — running box-level probes only "
            "(%s). The five reducer-shaped probes are SKIPPED because a worker is "
            "IDLE+autonomous by design and writes no runner-heartbeat, so they "
            "cannot fire here; see is_worker_body(). Stall/auth-death of a worker "
            "is NOT covered by this tick.\n" % ", ".join(sorted(WORKER_SAFE_PROBES))
        )

    if args.once:
        n = run_once(ctx, log_path)
        sys.stderr.write(
            f"agent-watchdog: snapshotted {n} probes for agent={agent_name} → {log_path}\n"
        )
        return 0

    # Default mode: --tick. Periodic check from iteration-close.sh; no daemon,
    # no PID file, no detachment. State persists across calls via state_path.
    transitions = run_tick(ctx, log_path, state_path)
    if transitions > 0:
        sys.stderr.write(
            f"agent-watchdog: tick agent={agent_name} → "
            f"{transitions} transition(s) logged to {log_path}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
