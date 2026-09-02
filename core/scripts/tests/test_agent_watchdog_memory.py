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
  9. Box-local alert state (2026-09-02 email flood): dedup survives separate
     invocations on one box, a re-alert window while the box stays over,
     recovery clears it, the mirrored per-agent tick state never overrides
     it, and every state-file failure fails OPEN.
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


def _probe(monkeypatch, *, total_kb, procs, avail_kb="healthy"):
    """A probe wired to synthetic memory readings.

    `avail_kb` defaults to a comfortably healthy 50% of MemTotal so the
    pre-existing single-process tests below exercise ONLY the signal they were
    written for. Pass an explicit value to drive the host_available trigger.
    """
    if avail_kb == "healthy":
        avail_kb = None if not total_kb else int(total_kb * 0.5)
    monkeypatch.setattr(WD, "_mem_total_kb", lambda: total_kb)
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: procs)
    monkeypatch.setattr(WD, "_mem_available_kb", lambda: avail_kb)
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
    saved = p.to_dict()
    assert saved["over"] is True and isinstance(saved["last_alert_ts"], float)

    revived = WD.MemoryHeadroomProbe(_Ctx())
    revived.from_dict({"over": True})  # an older writer's shape: no stamp
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


# ─────────────────────────────────────────────────────────────────────────────
# 9. host_available trigger — the 2026-08-22 zakbox1 OOM (multi-container host)
#
# The single-process signal is structurally blind there: each container
# enumerates only its OWN pid namespace while /proc/meminfo reports the whole
# host, so N containers each at ~9% of MemTotal sum to a fatal load with every
# individual reading far under the 60% floor.
# ─────────────────────────────────────────────────────────────────────────────

HOST = 64 * GIB  # the zakbox1 shape


def test_avail_floor_defaults_to_10_percent(monkeypatch):
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    assert WD._mem_avail_floor() == pytest.approx(0.10)


def test_avail_floor_honours_valid_override(monkeypatch):
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", "25")
    assert WD._mem_avail_floor() == pytest.approx(0.25)


@pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-5", "101", "10%"])
def test_malformed_avail_override_falls_back(monkeypatch, raw):
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raw)
    assert WD._mem_avail_floor() == pytest.approx(0.10)


def test_fires_when_host_starved_though_every_process_is_small(monkeypatch):
    """THE REGRESSION TEST for the zakbox1 OOM. This container sees its own
    6 GiB agent on a 64 GiB host — 9.4%, nowhere near the 60% floor — while
    the host has 3 GiB left because ~10 sibling containers hold the rest."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=HOST,
               procs=[(279635, "claude.exe", 6 * GIB)],
               avail_kb=3 * GIB)
    events = p.check()
    assert len(events) == 1, "host starvation must fire even with a small local process"
    ev = events[0]
    assert ev.severity == "critical"
    assert ev.payload["triggers"] == ["host_available"]
    assert ev.payload["pct_of_memtotal"] == pytest.approx(9.4, abs=0.2)
    assert ev.payload["pct_available"] == pytest.approx(4.7, abs=0.2)
    assert "host has" in ev.summary


def test_fires_when_no_claude_process_is_visible_at_all(monkeypatch):
    """PID-namespace blindness in its purest form: the probe can see no agent
    process whatsoever, and the host is still starving. The pre-fix code
    returned [] on an empty proc list and could never report this."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=HOST, procs=[], avail_kb=1 * GIB)
    events = p.check()
    assert len(events) == 1
    assert events[0].payload["triggers"] == ["host_available"]
    assert events[0].payload["claude_process_count"] == 0


def test_healthy_host_with_small_processes_stays_quiet(monkeypatch):
    """The other half of the contract — this must not become a noise source."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=HOST,
               procs=[(1, "claude", 6 * GIB), (2, "claude", 5 * GIB)],
               avail_kb=30 * GIB)
    assert p.check() == []


def test_payload_carries_aggregate_not_just_max(monkeypatch):
    """`max()` is what went blind; the aggregate is the number a reader needs
    to see that N small processes are collectively the problem."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=HOST,
               procs=[(1, "claude", 6 * GIB), (2, "claude", 5 * GIB),
                      (3, "claude.exe", 4 * GIB)],
               avail_kb=2 * GIB)
    ev = p.check()[0]
    assert ev.payload["rss_kb"] == 6 * GIB            # max, unchanged
    assert ev.payload["claude_rss_total_kb"] == 15 * GIB  # sum, new
    assert ev.payload["claude_rss_total_gib"] == pytest.approx(15.0, abs=0.01)


def test_both_triggers_can_fire_together(monkeypatch):
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=BOX,
               procs=[(1, "claude", int(BOX * 0.7))], avail_kb=int(BOX * 0.05))
    ev = p.check()[0]
    assert ev.payload["triggers"] == ["largest_process", "host_available"]


def test_host_trigger_dedups_like_the_process_trigger(monkeypatch):
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=HOST, procs=[], avail_kb=1 * GIB)
    assert len(p.check()) == 1
    assert p.check() == []


def test_clears_only_when_both_signals_recover(monkeypatch):
    """A recovered process reading must not clear the event while the HOST is
    still starving — that would report all-clear into an ongoing incident."""
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", "60")
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    p = _probe(monkeypatch, total_kb=BOX,
               procs=[(1, "claude", int(BOX * 0.70))], avail_kb=int(BOX * 0.05))
    assert len(p.check()) == 1

    # Process shrinks well under, host still starved → still no all-clear.
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: [(1, "claude", int(BOX * 0.20))])
    assert p.check() == []
    assert p.over is True

    # Host recovers too → now it clears.
    monkeypatch.setattr(WD, "_mem_available_kb", lambda: int(BOX * 0.50))
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "memory_pressure_cleared"
    assert p.over is False


def test_unreadable_available_cannot_veto_recovery(monkeypatch):
    """MemAvailable absent (older kernel) must not wedge the probe permanently
    over-threshold once the process signal has recovered."""
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", "60")
    p = _probe(monkeypatch, total_kb=BOX,
               procs=[(1, "claude", int(BOX * 0.70))], avail_kb=None)
    assert len(p.check()) == 1
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: [(1, "claude", int(BOX * 0.20))])
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "memory_pressure_cleared"


def test_mem_available_parses_meminfo(monkeypatch, tmp_path):
    """The parser reads MemAvailable, not MemFree — they differ by reclaimable
    cache and MemFree would fire constantly on a healthy box."""
    meminfo = ("MemTotal:       65792316 kB\n"
               "MemFree:          812344 kB\n"
               "MemAvailable:   12345678 kB\n")
    monkeypatch.setattr(WD, "read_text_safe", lambda p: meminfo)
    assert WD._mem_available_kb() == 12345678
    assert WD._mem_total_kb() == 65792316


def test_mem_available_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(WD, "read_text_safe", lambda p: "MemTotal: 100 kB\n")
    assert WD._mem_available_kb() is None


# ─────────────────────────────────────────────────────────────────────────────
# 10. swap_exhausted trigger — the signal that was missing entirely on
# 2026-08-22. Swap fills BEFORE MemAvailable moves, so this is the earliest
# reliable warning in the chain that ends in a global OOM.
# ─────────────────────────────────────────────────────────────────────────────

def _swap_probe(monkeypatch, *, total_kb, procs, avail_kb, swap_total, swap_free):
    monkeypatch.setattr(WD, "_mem_total_kb", lambda: total_kb)
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: procs)
    monkeypatch.setattr(WD, "_mem_available_kb", lambda: avail_kb)
    monkeypatch.setattr(WD, "_swap_kb", lambda: (swap_total, swap_free))
    monkeypatch.setattr(WD, "_notify_critical_memory",
                        lambda s, m: {"sent": True, "stub": True})
    return WD.MemoryHeadroomProbe(_Ctx())


def test_swap_floor_defaults_to_25_percent(monkeypatch):
    monkeypatch.delenv("AGENT_WATCHDOG_SWAP_FREE_PCT", raising=False)
    assert WD._swap_floor() == pytest.approx(0.25)


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "101"])
def test_malformed_swap_override_falls_back(monkeypatch, raw):
    monkeypatch.setenv("AGENT_WATCHDOG_SWAP_FREE_PCT", raw)
    assert WD._swap_floor() == pytest.approx(0.25)


def test_fires_on_swap_exhaustion_while_ram_reads_healthy(monkeypatch):
    """THE 2026-08-22 zakbox1 READING, verbatim: MemAvailable 47% (healthy by
    every RAM measure) while SwapFree is 5.8% of SwapTotal. Neither existing
    trigger fires; this one must, because that box was already thrashing."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    monkeypatch.delenv("AGENT_WATCHDOG_SWAP_FREE_PCT", raising=False)
    p = _swap_probe(monkeypatch, total_kb=65579460,
                    procs=[(279635, "claude.exe", 5149116)],
                    avail_kb=30822764,          # 47% available — looks fine
                    swap_total=8388604, swap_free=483908)   # 5.8% free
    events = p.check()
    assert len(events) == 1
    ev = events[0]
    assert ev.payload["triggers"] == ["swap_exhausted"]
    assert ev.payload["pct_swap_free"] == pytest.approx(5.8, abs=0.2)
    assert ev.payload["pct_available"] == pytest.approx(47.0, abs=0.5)
    assert "paging to disk" in ev.summary
    assert ev.payload["notified"]["sent"] is True


def test_swap_disabled_is_not_pressure(monkeypatch):
    """SwapTotal 0 is a configuration, not an exhausted swap. Dividing by it
    would either crash or report 0% free forever."""
    monkeypatch.delenv("AGENT_WATCHDOG_SWAP_FREE_PCT", raising=False)
    p = _swap_probe(monkeypatch, total_kb=BOX, procs=[(1, "claude", GIB)],
                    avail_kb=BOX // 2, swap_total=0, swap_free=0)
    assert p.check() == []


def test_healthy_swap_stays_quiet(monkeypatch):
    monkeypatch.delenv("AGENT_WATCHDOG_SWAP_FREE_PCT", raising=False)
    p = _swap_probe(monkeypatch, total_kb=BOX, procs=[(1, "claude", GIB)],
                    avail_kb=BOX // 2, swap_total=8 * GIB, swap_free=7 * GIB)
    assert p.check() == []


def test_unreadable_swap_does_not_block_other_triggers(monkeypatch):
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_AVAIL_PCT", raising=False)
    p = _swap_probe(monkeypatch, total_kb=HOST, procs=[], avail_kb=1 * GIB,
                    swap_total=None, swap_free=None)
    ev = p.check()[0]
    assert ev.payload["triggers"] == ["host_available"]
    assert ev.payload["pct_swap_free"] is None


def test_swap_cannot_veto_recovery_when_unreadable(monkeypatch):
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", "60")
    p = _swap_probe(monkeypatch, total_kb=BOX, procs=[(1, "claude", int(BOX * .7))],
                    avail_kb=BOX // 2, swap_total=None, swap_free=None)
    assert len(p.check()) == 1
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: [(1, "claude", int(BOX * .2))])
    assert p.check()[0].event == "memory_pressure_cleared"


def test_swap_still_low_blocks_all_clear(monkeypatch):
    """A recovered RAM reading must not report all-clear while swap is still
    exhausted — the box is still thrashing."""
    monkeypatch.setenv("AGENT_WATCHDOG_MEM_PCT", "60")
    monkeypatch.delenv("AGENT_WATCHDOG_SWAP_FREE_PCT", raising=False)
    p = _swap_probe(monkeypatch, total_kb=BOX, procs=[(1, "claude", int(BOX * .7))],
                    avail_kb=BOX // 2, swap_total=8 * GIB, swap_free=int(0.05 * 8 * GIB))
    assert len(p.check()) == 1
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: [(1, "claude", int(BOX * .2))])
    assert p.check() == []          # swap still 5% free
    assert p.over is True
    monkeypatch.setattr(WD, "_swap_kb", lambda: (8 * GIB, 7 * GIB))
    assert p.check()[0].event == "memory_pressure_cleared"


def test_swap_parses_meminfo(monkeypatch):
    meminfo = ("MemTotal:       65579460 kB\n"
               "MemAvailable:   30822764 kB\n"
               "SwapTotal:       8388604 kB\n"
               "SwapFree:         483908 kB\n")
    monkeypatch.setattr(WD, "read_text_safe", lambda p: meminfo)
    assert WD._swap_kb() == (8388604, 483908)


def test_swap_returns_none_pair_when_meminfo_absent(monkeypatch):
    monkeypatch.setattr(WD, "read_text_safe", lambda p: None)
    assert WD._swap_kb() == (None, None)


# ── notification is FAIL-OPEN ────────────────────────────────────────────────

def test_check_survives_a_notifier_that_raises(monkeypatch):
    """A watchdog that dies because mail is down is worse than the condition
    it was reporting. The tick must complete and the event must still be
    emitted, with the delivery failure recorded in the payload."""
    monkeypatch.delenv("AGENT_WATCHDOG_SWAP_FREE_PCT", raising=False)
    monkeypatch.setattr(WD, "_mem_total_kb", lambda: BOX)
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: [(1, "claude", GIB)])
    monkeypatch.setattr(WD, "_mem_available_kb", lambda: BOX // 2)
    monkeypatch.setattr(WD, "_swap_kb", lambda: (8 * GIB, int(0.01 * 8 * GIB)))
    p = WD.MemoryHeadroomProbe(_Ctx())

    def _boom(subject, message):
        raise RuntimeError("smtp exploded")
    monkeypatch.setattr(WD, "_notify_critical_memory", _boom)

    events = p.check()          # must NOT propagate
    assert len(events) == 1
    assert events[0].severity == "critical"
    assert events[0].payload["notified"]["sent"] is False
    assert "RuntimeError" in events[0].payload["notified"]["reason"]


def test_real_notifier_swallows_a_broken_subprocess(monkeypatch):
    """The inner layer's own fail-open contract, tested against the REAL
    function (the previous version of this test patched the function out and
    then called it, so it only ever exercised its own stub)."""
    import subprocess as _sp

    def _explode(*a, **k):
        raise OSError("no bash")
    monkeypatch.setattr(_sp, "run", _explode)
    got = WD._notify_critical_memory("s", "m")
    assert got["sent"] is False
    assert "OSError" in got["reason"]


def test_notify_reports_nonzero_rc_without_raising(monkeypatch):
    """rc 3/4/5/6 from notify-user.sh are outcomes (suppressed, duplicate, no
    transport, transport failed) — recorded, never raised."""
    import subprocess as _sp

    class _R:
        returncode = 5
        stdout = "no transport configured"
        stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R())
    got = WD._notify_critical_memory("s", "m")
    assert got["sent"] is False and got["rc"] == 5


# ── 9. box-local alert state — the email flood (2026-09-02) ──────────────────
#
# The transition dedup above is in-process and persisted PER AGENT under
# agents/<agent>/session/, a directory mirrored across boxes. An agent ticking
# on two hosts alternated that file between over=True and over=False, so the
# starved host re-alerted on nearly every tick: 1,696 decision-needed emails in
# four days. The alert state now lives box-locally in core/logs/ and is keyed
# by the box alone, with a re-alert window while the box stays over.

class _RootCtx:
    """A context that knows its project root — the tick-mode shape."""

    def __init__(self, root):
        self.project_root_path = root


def _root_probe(monkeypatch, root, *, total_kb, procs, avail_kb="healthy"):
    """A probe whose box file lives under `root` — via the env override, which
    is the only way a test may get the file written at all (see the probe's
    PYTEST_CURRENT_TEST refusal)."""
    if avail_kb == "healthy":
        avail_kb = None if not total_kb else int(total_kb * 0.5)
    monkeypatch.setattr(WD, "_mem_total_kb", lambda: total_kb)
    monkeypatch.setattr(WD, "_claude_rss_kb", lambda: procs)
    monkeypatch.setattr(WD, "_mem_available_kb", lambda: avail_kb)
    monkeypatch.setenv(WD.MemoryHeadroomProbe.BOX_STATE_DIR_ENV,
                       str(Path(root) / "core" / "logs"))
    return WD.MemoryHeadroomProbe(_RootCtx(root))


def _record_notifies(monkeypatch):
    sent = []

    def _rec(subject, message):
        sent.append(subject)
        return {"sent": True, "rc": 0}
    monkeypatch.setattr(WD, "_notify_critical_memory", _rec)
    return sent


def _state_path(root):
    return Path(root) / "core" / "logs" / WD.MemoryHeadroomProbe.BOX_STATE_NAME


def _write_state(root, **fields):
    import json
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(fields), encoding="utf-8")


def _read_state(root):
    import json
    return json.loads(_state_path(root).read_text(encoding="utf-8"))


PRESSURE = dict(total_kb=BOX, procs=[(1478029, "claude.exe", int(6.3 * GIB))])
HEALTHY = dict(total_kb=BOX, procs=[(1478029, "claude.exe", int(0.6 * GIB))])


def test_realert_window_defaults_to_six_hours(monkeypatch):
    monkeypatch.delenv(WD.MemoryHeadroomProbe.REALERT_ENV, raising=False)
    assert WD.MemoryHeadroomProbe._realert_seconds() == pytest.approx(6 * 3600)
    monkeypatch.setenv(WD.MemoryHeadroomProbe.REALERT_ENV, "120")
    assert WD.MemoryHeadroomProbe._realert_seconds() == pytest.approx(120)


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "6h"])
def test_malformed_realert_window_falls_back(monkeypatch, raw):
    monkeypatch.setenv(WD.MemoryHeadroomProbe.REALERT_ENV, raw)
    assert WD.MemoryHeadroomProbe._realert_seconds() == pytest.approx(6 * 3600)


def test_box_state_dedups_across_separate_invocations(monkeypatch, tmp_path):
    """Two ticks from two different processes (two probe instances) on one box:
    the first alerts and writes the box file; the second reads it and stays
    silent. This is the exact shape the per-agent file could not hold."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    monkeypatch.delenv(WD.MemoryHeadroomProbe.REALERT_ENV, raising=False)
    sent = _record_notifies(monkeypatch)

    first = _root_probe(monkeypatch, tmp_path, **PRESSURE)
    events = first.check()
    assert len(events) == 1 and events[0].event == "memory_pressure"
    assert events[0].payload["realert"] is False
    assert len(sent) == 1 and events[0].payload["notified"]["sent"] is True
    st = _read_state(tmp_path)
    assert st["over"] is True and isinstance(st["last_alert_ts"], float)
    assert st["triggers"] == events[0].payload["triggers"]

    second = _root_probe(monkeypatch, tmp_path, **PRESSURE)
    assert second.over is True  # loaded from the box file, not from memory
    assert second.check() == []
    assert len(sent) == 1


def test_still_over_is_silent_inside_the_window(monkeypatch, tmp_path):
    import time
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    monkeypatch.delenv(WD.MemoryHeadroomProbe.REALERT_ENV, raising=False)
    sent = _record_notifies(monkeypatch)
    _write_state(tmp_path, over=True, last_alert_ts=time.time() - 60, triggers=["largest_process"])
    p = _root_probe(monkeypatch, tmp_path, **PRESSURE)
    assert p.check() == []
    assert sent == []


def test_still_over_realerts_once_the_window_has_passed(monkeypatch, tmp_path):
    """A box that has been starved for longer than the window gets ONE reminder,
    the stamp moves forward, and the next tick is silent again."""
    import time
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    monkeypatch.delenv(WD.MemoryHeadroomProbe.REALERT_ENV, raising=False)
    sent = _record_notifies(monkeypatch)
    old = time.time() - 7 * 3600
    _write_state(tmp_path, over=True, last_alert_ts=old, triggers=["largest_process"])

    p = _root_probe(monkeypatch, tmp_path, **PRESSURE)
    events = p.check()
    assert len(events) == 1 and events[0].payload["realert"] is True
    assert len(sent) == 1
    assert _read_state(tmp_path)["last_alert_ts"] > old + 6 * 3600

    again = _root_probe(monkeypatch, tmp_path, **PRESSURE)
    assert again.check() == [] and len(sent) == 1


def test_recovery_clears_the_box_state_and_a_new_episode_alerts(monkeypatch, tmp_path):
    import time
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    monkeypatch.delenv(WD.MemoryHeadroomProbe.REALERT_ENV, raising=False)
    sent = _record_notifies(monkeypatch)
    _write_state(tmp_path, over=True, last_alert_ts=time.time() - 60, triggers=["largest_process"])

    healthy = _root_probe(monkeypatch, tmp_path, **HEALTHY)
    events = healthy.check()
    assert len(events) == 1 and events[0].event != "memory_pressure"
    assert _read_state(tmp_path)["over"] is False
    assert sent == []

    relapse = _root_probe(monkeypatch, tmp_path, **PRESSURE)
    events = relapse.check()
    assert len(events) == 1 and events[0].payload["realert"] is False
    assert len(sent) == 1


def test_mirrored_per_agent_state_never_overrides_the_box_file(monkeypatch, tmp_path):
    """The per-agent tick file (from_dict) is what other boxes overwrite. With a
    box file present it must be ignored in BOTH directions: a mirrored
    over=False cannot re-arm an alert the box already sent, and a mirrored
    over=True cannot suppress a box's first alert."""
    import time
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    monkeypatch.delenv(WD.MemoryHeadroomProbe.REALERT_ENV, raising=False)
    sent = _record_notifies(monkeypatch)

    _write_state(tmp_path, over=True, last_alert_ts=time.time() - 60, triggers=["largest_process"])
    p = _root_probe(monkeypatch, tmp_path, **PRESSURE)
    p.from_dict({"over": False})  # the healthy box's tick, mirrored here
    assert p.over is True and p.check() == [] and sent == []

    fresh_root = tmp_path / "other-box"
    q = _root_probe(monkeypatch, fresh_root, **PRESSURE)
    q.from_dict({"over": True})  # a starved box's tick, mirrored here
    assert q.over is False
    assert len(q.check()) == 1 and len(sent) == 1


def test_box_state_path_resolution(monkeypatch, tmp_path):
    """Production: <project root>/core/logs/<name>. Under pytest with no
    override: NO file — the probe tests run on the real project root, and a
    stamped over=True there would silence a live box's next alert. The env
    override wins everywhere."""
    env = WD.MemoryHeadroomProbe.BOX_STATE_DIR_ENV
    name = WD.MemoryHeadroomProbe.BOX_STATE_NAME
    monkeypatch.delenv(env, raising=False)
    assert WD.MemoryHeadroomProbe(_RootCtx(tmp_path))._box_state_path() is None
    assert WD.MemoryHeadroomProbe(_Ctx())._box_state_path() is None

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert (WD.MemoryHeadroomProbe(_RootCtx(tmp_path))._box_state_path()
            == tmp_path / "core" / "logs" / name)
    assert WD.MemoryHeadroomProbe(_Ctx())._box_state_path() is None

    monkeypatch.setenv(env, str(tmp_path / "elsewhere"))
    assert (WD.MemoryHeadroomProbe(_Ctx())._box_state_path()
            == tmp_path / "elsewhere" / name)


def test_context_without_a_root_keeps_the_in_process_behaviour(monkeypatch):
    """No box file (stub context, no override): the per-agent state is
    honoured and nothing is written anywhere."""
    monkeypatch.delenv(WD.MemoryHeadroomProbe.BOX_STATE_DIR_ENV, raising=False)
    p = _probe(monkeypatch, **PRESSURE)
    assert p._box_state_path() is None
    p.from_dict({"over": True})
    assert p.over is True


def test_unwritable_state_dir_fails_open(monkeypatch, tmp_path):
    """core/logs occupied by a FILE: mkdir raises, the alert still fires, and
    nothing propagates. A broken state file must never silence the probe."""
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    sent = _record_notifies(monkeypatch)
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "logs").write_text("not a directory", encoding="utf-8")
    p = _root_probe(monkeypatch, tmp_path, **PRESSURE)
    events = p.check()
    assert len(events) == 1 and len(sent) == 1


def test_corrupt_state_file_fails_open_to_not_over(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_WATCHDOG_MEM_PCT", raising=False)
    sent = _record_notifies(monkeypatch)
    p = _state_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    probe = _root_probe(monkeypatch, tmp_path, **PRESSURE)
    assert probe.over is False
    assert len(probe.check()) == 1 and len(sent) == 1
    assert _read_state(tmp_path)["over"] is True  # rewritten cleanly
