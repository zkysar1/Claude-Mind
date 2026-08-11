"""test_agent_watchdog_memory.py — MemoryHeadroomProbe (agent-watchdog.py, ).

The incident this probe exists for: 2026-08-02T14:41:13 on LAPTOP-3IOFCNEO
(uname -r 6.6.87.2-microsoft-standard-WSL2), a Claude Code process that had run
6.5 days held 6.06 GiB anon-rss on a 7.7 GiB box — 88.6% of all resident process
memory in the kernel's own OOM victim table. A global OOM fired and the kernel
selected it BECAUSE it was the largest; systemd then marked foxtrot-tmux.service
failed (OOMPolicy=stop, Restart=no) and restarted nothing. The agent stayed dead
~76 minutes until a human reopened it.

Every in-process defense (ScheduleWakeup deadman pair, SessionStart
recovery-gate, stop-hook BLOCK) runs INSIDE the process the OOM killer removes,
so none of them can act. This probe is the only class of defense that can: it
fires while the process is still alive.

Tests:
  1. Threshold parsing — default, valid override, and every malformed override
     falling back to the default rather than crashing or disabling the probe.
  2. Fires critical at/above threshold; quiet below it.
  3. Transition dedup — a sustained over-threshold reading emits ONCE, not on
     every tick (this probe runs every iteration-close).
  4. Hysteresis — clears only once well back under, so a reading parked at the
     boundary cannot flap a critical event every tick.
  5. Largest-process selection — with several Claude processes the probe reports
     the biggest, which is the one the OOM killer would actually choose.
  6. Fails OPEN off Linux / on unreadable /proc (returns no events rather than a
     false reading) — the framework runs on Windows boxes too.
  7. State round-trips through to_dict/from_dict (tick mode persists across
     separate invocations).
  8. MemoryHeadroomProbe is registered in build_probes().
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load_watchdog():
    # agent-watchdog.py is hyphenated — load via importlib for its symbols.
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_mem", CORE_SCRIPTS / "agent-watchdog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load_watchdog()


class _Ctx:
    """Minimal stand-in — MemoryHeadroomProbe reads no context state."""


def _probe(monkeypatch, *, total_kb, procs):
    """A probe wired to synthetic memory readings."""
    monkeypatch.setattr(WD, "_mem_total_kb", lambda: total_kb)
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: procs)
    return WD.MemoryHeadroomProbe(_Ctx())


GIB = 1048576  # kB in a GiB
BOX = 8 * GIB  # an 8 GiB box


# ── 1. threshold parsing ─────────────────────────────────────────────────────

def test_threshold_defaults_to_60_percent(monkeypatch):
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    assert WD._mem_headroom_threshold() == pytest.approx(0.60)


def test_threshold_honours_valid_override(monkeypatch):
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", "75")
    assert WD._mem_headroom_threshold() == pytest.approx(0.75)


@pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-5", "101", "1e999", "60%"])
def test_malformed_override_falls_back_to_default(monkeypatch, raw):
    """A bad value must not crash and must not silently disable the probe by
    yielding a threshold nothing can reach."""
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", raw)
    assert WD._mem_headroom_threshold() == pytest.approx(0.60)


# ── 2. fires above threshold, quiet below ────────────────────────────────────

def test_fires_critical_at_incident_proportions(monkeypatch):
    """The measured incident shape: ~79% of MemTotal in one process."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=BOX, procs=[(1478029, "claude.exe", int(6.3 * GIB))])
    events = p.check()
    assert len(events) == 1
    ev = events[0]
    assert ev.severity == "critical"
    assert ev.event == "memory_pressure"
    assert ev.probe == "memory-headroom"
    assert ev.payload["pid"] == 1478029
    assert ev.payload["pct_of_memtotal"] == pytest.approx(78.8, abs=0.2)
    assert ev.include_processes is True


def test_quiet_below_threshold(monkeypatch):
    """A healthy long-running session must not emit — otherwise the critical
    channel is noise and the real one gets ignored."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=BOX, procs=[(999, "claude", int(0.6 * GIB))])
    assert p.check() == []


def test_boundary_is_inclusive(monkeypatch):
    """Exactly at threshold counts as pressure (>=, not >)."""
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", "50")
    p = _probe(monkeypatch, total_kb=BOX, procs=[(1, "claude", BOX // 2)])
    assert len(p.check()) == 1


# ── 3. transition dedup ──────────────────────────────────────────────────────

def test_sustained_pressure_emits_once(monkeypatch):
    """Runs every iteration-close; a sustained condition must not re-fire."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=BOX, procs=[(1, "claude", int(7.0 * GIB))])
    assert len(p.check()) == 1
    assert p.check() == []
    assert p.check() == []


# ── 4. hysteresis ────────────────────────────────────────────────────────────

def test_does_not_flap_just_under_threshold(monkeypatch):
    """Dropping a hair under threshold must NOT clear — otherwise a reading
    hovering at the boundary emits critical/cleared on alternating ticks."""
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", "60")
    p = _probe(monkeypatch, total_kb=BOX, procs=[(1, "claude", int(BOX * 0.61))])
    assert len(p.check()) == 1
    # 0.58 is under 0.60 but above the 0.54 clear-band — still no event.
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: [(1, "claude", int(BOX * 0.58))])
    assert p.check() == []
    assert p.over is True


def test_clears_once_well_under(monkeypatch):
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", "60")
    p = _probe(monkeypatch, total_kb=BOX, procs=[(1, "claude", int(BOX * 0.70))])
    assert len(p.check()) == 1
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: [(1, "claude", int(BOX * 0.20))])
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "memory_pressure_cleared"
    assert events[0].severity == "info"
    assert p.over is False


# ── 5. largest-process selection ─────────────────────────────────────────────

def test_reports_largest_process(monkeypatch):
    """The OOM killer picks the biggest; the probe must measure the same one.
    Mirrors the real victim table, which held both a 'claude.exe' and a
    smaller sibling 'claude'."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=BOX, procs=[
        (3642604, "claude", int(0.64 * GIB)),
        (1478029, "claude.exe", int(6.06 * GIB)),
    ])
    events = p.check()
    assert len(events) == 1
    assert events[0].payload["pid"] == 1478029
    assert events[0].payload["comm"] == "claude.exe"
    assert events[0].payload["claude_process_count"] == 2


# ── 6. fails open ────────────────────────────────────────────────────────────

def test_fails_open_without_meminfo(monkeypatch):
    """Non-Linux box: no reading is better than a false reading."""
    p = _probe(monkeypatch, total_kb=None, procs=[(1, "claude", 999)])
    assert p.check() == []


def test_fails_open_with_no_claude_process(monkeypatch):
    p = _probe(monkeypatch, total_kb=BOX, procs=[])
    assert p.check() == []


def test_claude_rss_returns_empty_when_proc_absent(monkeypatch):
    """_claude_rss_kb is the Linux-only seam — it must degrade, not raise."""
    monkeypatch.setattr(WD.Path, "is_dir", lambda self: False)
    assert WD._claude_rss_kb() == []


# ── 7. state round-trip ──────────────────────────────────────────────────────

def test_state_round_trips(monkeypatch):
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", "60")
    p = _probe(monkeypatch, total_kb=BOX, procs=[(1, "claude", int(BOX * 0.9))])
    p.check()
    assert p.to_dict() == {"over": True}

    revived = WD.MemoryHeadroomProbe(_Ctx())
    revived.from_dict({"over": True})
    assert revived.over is True
    # A revived over-threshold probe must stay deduped across invocations.
    monkeypatch.setattr(WD, "_mem_total_kb", lambda: BOX)
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: [(1, "claude", int(BOX * 0.9))])
    assert revived.check() == []

    fresh = WD.MemoryHeadroomProbe(_Ctx())
    fresh.from_dict({})
    assert fresh.over is False


# ── 8. registration ──────────────────────────────────────────────────────────

def test_probe_is_registered(tmp_path):
    """An unregistered probe never runs — the defect class this file guards.

    build_probes() instantiates every sibling probe, so this needs a real
    WatchdogContext rather than the bare stub the unit tests above use.
    """
    ctx = WD.WatchdogContext(
        agent_name="foxtrot",
        agent_dir=tmp_path / "agents" / "foxtrot",
        project_root_path=tmp_path,
    )
    names = [p.name for p in WD.build_probes(ctx)]
    assert "memory-headroom" in names, (
        f"MemoryHeadroomProbe not registered in build_probes(): {names}"
    )
