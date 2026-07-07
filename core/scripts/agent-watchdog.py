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
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
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




def build_probes(ctx: WatchdogContext) -> list[Probe]:
    """Single registration point. Add new probes here."""
    return [
        RunningSidProbe(ctx),
        HeartbeatProbe(ctx),
        StalledProbe(ctx),
        BackgroundJobProbe(ctx),
        StopHookBlockProbe(ctx),
        DaemonHealthProbe(ctx),
        FreshnessProbe(ctx),
    ]


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
