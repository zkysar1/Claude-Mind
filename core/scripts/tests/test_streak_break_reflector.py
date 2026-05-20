"""test_streak_break_reflector.py — convert streak-break signals to Investigate goals.

Covers LifingPolls plan item 1 (2026-05-08).

Lanes:
  1. No signals → no-op (return code 0, no goals filed)
  2. Unprocessed signal → Investigate filed on parent aspiration, signal marked processed
  3. Recent matching Investigate exists → dedup, mark processed without filing
  4. Already-processed signal → skipped on re-run (idempotent)

Daemon-fixture migration (g-115-887, Cat B of g-115-874):
  Lanes 2 and 3 fire streak-break-reflector.py as a subprocess; the script
  uses _rt internally, which resolves the daemon port from
  PROJECT_ROOT/mind_api/state/daemon.port — meaning Investigate filings land in
  the REAL world/aspirations.jsonl, not the test's temp world. The
  DaemonFixture context manager spins up an in-process daemon rooted at a
  per-test project_root and sets RT_DIR so the subprocess's _rt calls hit
  the test daemon. Lanes 1 and 4 don't make _rt calls, so they keep the
  bare-subprocess shape.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REFLECTOR = CORE_SCRIPTS / "streak-break-reflector.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world_and_agent(tmp: Path, agent_name: str = "alpha"):
    """Seed a minimal world + agent dir with test aspirations."""
    world = tmp / "world"
    world.mkdir()
    asp_world = {
        "id": "asp-100",
        "title": "Test world asp",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-08T12:00:00",
        "goals": [{
            "id": "g-100-01",
            "title": "Recurring goal",
            "description": "Test",
            "status": "pending",
            "priority": "MEDIUM",
            "recurring": True,
            "interval_hours": 4,
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive",
            "achievedCount": 1,
            "participants": ["agent"],
        }],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp_world, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / agent_name
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    # Empty agent aspirations.jsonl
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    return world, agent_dir


def _write_signals(agent_dir: Path, signals: list[dict]):
    log_path = agent_dir / "session" / "streak-breaks.jsonl"
    with open(log_path, "w", encoding="utf-8") as f:
        for s in signals:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")


def _read_signals(agent_dir: Path) -> list[dict]:
    log_path = agent_dir / "session" / "streak-breaks.jsonl"
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _read_aspirations(world: Path, agent_dir: Path) -> list[dict]:
    out = []
    for path in [world / "aspirations.jsonl",
                 agent_dir / "aspirations.jsonl"]:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _run_reflector(world: Path, agent_dir: Path, agent: str = "alpha",
                   dry_run: bool = False):
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = agent
    env["MIND_AGENT_DIR"] = str(agent_dir)
    args = [sys.executable, str(REFLECTOR), "--agent", agent]
    if dry_run:
        args.append("--dry-run")
    proc = subprocess.run(args, capture_output=True, text=True,
                          timeout=15, env=env)
    return proc.returncode, proc.stdout, proc.stderr


# ---- Tests ----------------------------------------------------------------


def test_no_signals_noop():
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        rc, out, err = _run_reflector(world, agent_dir)
        assert rc == 0, err
        # No goals filed
        asps = _read_aspirations(world, agent_dir)
        for asp in asps:
            for g in asp.get("goals", []):
                assert "streak-break" not in g.get("title", "").lower()


def test_signal_files_investigate():
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world) as df:
            signal = {
                "timestamp": "2026-05-08T12:00:00",
                "goal_id": "g-100-01",
                "aspiration_id": "asp-100",
                "expected_interval_hours": 4,
                "actual_elapsed_hours": 12.5,
                "lateness_ratio": 3.13,
                "processed": False,
            }
            _write_signals(agent_dir, [signal])

            rc, out, err = _run_reflector(world, agent_dir)
            assert rc == 0, err

            # Investigate goal landed on asp-100
            asps = _read_aspirations(world, agent_dir)
            asp_100 = next(a for a in asps if a["id"] == "asp-100")
            new_goals = [g for g in asp_100["goals"]
                         if "streak-break" in g.get("title", "").lower()]
            assert len(new_goals) == 1
            assert "g-100-01" in new_goals[0]["title"]
            assert "12.5" in new_goals[0]["title"]

            # Signal marked processed
            signals = _read_signals(agent_dir)
            assert signals[0]["processed"] is True
            assert "filed_at" in signals[0]


def test_dedup_skips_when_recent_investigate_exists():
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world) as df:
            signal = {
                "timestamp": "2026-05-08T12:00:00",
                "goal_id": "g-100-01",
                "aspiration_id": "asp-100",
                "expected_interval_hours": 4,
                "actual_elapsed_hours": 12.5,
                "lateness_ratio": 3.13,
                "processed": False,
            }
            _write_signals(agent_dir, [signal])

            # First run files
            rc, _, err = _run_reflector(world, agent_dir)
            assert rc == 0, err
            asps_first = _read_aspirations(world, agent_dir)
            asp_100 = next(a for a in asps_first if a["id"] == "asp-100")
            first_count = len([g for g in asp_100["goals"]
                               if "streak-break" in g.get("title", "").lower()])
            assert first_count == 1

            # Add a fresh signal — second run should DEDUP
            signal2 = dict(signal, processed=False,
                           timestamp="2026-05-08T13:00:00",
                           actual_elapsed_hours=14.0)
            _write_signals(agent_dir, _read_signals(agent_dir) + [signal2])
            rc, out, err = _run_reflector(world, agent_dir)
            assert rc == 0, err

            asps_second = _read_aspirations(world, agent_dir)
            asp_100 = next(a for a in asps_second if a["id"] == "asp-100")
            second_count = len([g for g in asp_100["goals"]
                                if "streak-break" in g.get("title", "").lower()])
            assert second_count == 1, (
                f"Dedup failed — second run created another Investigate "
                f"(now {second_count})"
            )

            signals = _read_signals(agent_dir)
            # The new signal is processed but marked dedup_skipped
            new_sig = signals[1]
            assert new_sig["processed"] is True
            assert new_sig.get("dedup_skipped") is True


def test_already_processed_signal_skipped():
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        signal = {
            "timestamp": "2026-05-08T12:00:00",
            "goal_id": "g-100-01",
            "aspiration_id": "asp-100",
            "expected_interval_hours": 4,
            "actual_elapsed_hours": 12.5,
            "processed": True,  # already processed
            "filed_at": "2026-05-08T11:55:00",
        }
        _write_signals(agent_dir, [signal])

        rc, out, err = _run_reflector(world, agent_dir)
        assert rc == 0, err

        # No new goals filed
        asps = _read_aspirations(world, agent_dir)
        for asp in asps:
            for g in asp.get("goals", []):
                assert "streak-break" not in g.get("title", "").lower()


# ---- Auto-resolve sweep tests () ---------------------------------
#
# These tests exercise _auto_resolve_recovered_canaries: an open
# `investigate:streak-break:<gid>` canary is closed when the source recurring
# goal's lastAchievedAt has advanced past the canary's created_at AND within
# STREAK_MULT × interval_hours of it. The fix targets canary pile-up from
# marginal session-gap noise (, rb-1057).
#
# Pattern: seed asp-100 with (a) the recurring source goal at a specific
# lastAchievedAt and (b) a manually-injected Investigate canary at a known
# created_at. Run reflector. Check whether the canary's status flipped to
# "completed".


def _inject_canary(world: Path, asp_id: str, canary_gid: str,
                   source_gid: str, created_at: str):
    """Add an Investigate canary directly to the world aspiration's goals.

    Bypasses _rt.aspirations_add_goal so we control created_at + status
    deterministically. The canary's origin_signal exactly matches the
    pattern _auto_resolve_recovered_canaries looks for.
    """
    asp_path = world / "aspirations.jsonl"
    asps = []
    for line in asp_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        asps.append(json.loads(line))
    target = next(a for a in asps if a["id"] == asp_id)
    target["goals"].append({
        "id": canary_gid,
        "title": f"Investigate: {source_gid} streak-break - 9h vs 4h expected",
        "description": "auto-resolve test fixture",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [],
                         "preconditions": []},
        "origin_signal": f"investigate:streak-break:{source_gid}",
        "created_at": created_at,
        "participants": ["agent"],
    })
    with open(asp_path, "w", encoding="utf-8") as f:
        for a in asps:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")


def _set_source_last_achieved(world: Path, asp_id: str, gid: str,
                              last_achieved: str):
    """Update a recurring source goal's lastAchievedAt directly on disk."""
    asp_path = world / "aspirations.jsonl"
    asps = []
    for line in asp_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        asps.append(json.loads(line))
    target = next(a for a in asps if a["id"] == asp_id)
    src = next(g for g in target["goals"] if g["id"] == gid)
    src["lastAchievedAt"] = last_achieved
    with open(asp_path, "w", encoding="utf-8") as f:
        for a in asps:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")


def _goal_status(world: Path, asp_id: str, gid: str) -> str:
    asp_path = world / "aspirations.jsonl"
    for line in asp_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        if asp.get("id") != asp_id:
            continue
        for g in asp.get("goals", []):
            if g.get("id") == gid:
                return g.get("status", "")
    return ""


def test_auto_resolve_when_cadence_recovers_in_window():
    """Canary auto-resolved when source fires within STREAK_MULT*interval
    of canary's filed_at."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world):
            # Canary filed at T=12:00; interval=4h; STREAK_MULT=2.0 → 8h window.
            # Source recovered at T=14:00 (2h after canary) → INSIDE window.
            _inject_canary(world, "asp-100", "g-100-99", "g-100-01",
                           created_at="2026-05-18T12:00:00")
            _set_source_last_achieved(world, "asp-100", "g-100-01",
                                      last_achieved="2026-05-18T14:00:00")

            rc, out, err = _run_reflector(world, agent_dir)
            assert rc == 0, err
            assert "1 auto-resolved" in out, (
                f"summary missing auto-resolve count: out={out!r}")

            status = _goal_status(world, "asp-100", "g-100-99")
            assert status == "completed", (
                f"canary should be completed; got status={status!r}")


def test_auto_resolve_skips_when_recovery_too_late():
    """Canary stays open when source recovery exceeds STREAK_MULT*interval."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world):
            # interval=4h, STREAK_MULT=2.0 → 8h window. Recovery at 20h is
            # well outside the window — real drift, not session noise.
            _inject_canary(world, "asp-100", "g-100-98", "g-100-01",
                           created_at="2026-05-18T12:00:00")
            _set_source_last_achieved(world, "asp-100", "g-100-01",
                                      last_achieved="2026-05-19T08:00:00")

            rc, out, err = _run_reflector(world, agent_dir)
            assert rc == 0, err
            assert "0 auto-resolved" in out, (
                f"out-of-window recovery should NOT auto-resolve; out={out!r}")

            status = _goal_status(world, "asp-100", "g-100-98")
            assert status == "pending", (
                f"canary should stay pending; got status={status!r}")


def test_auto_resolve_skips_when_no_recovery():
    """Canary stays open when source has NOT fired since canary was filed."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world):
            # Source's lastAchievedAt is BEFORE the canary was filed —
            # no recovery yet.
            _inject_canary(world, "asp-100", "g-100-97", "g-100-01",
                           created_at="2026-05-18T12:00:00")
            _set_source_last_achieved(world, "asp-100", "g-100-01",
                                      last_achieved="2026-05-18T10:00:00")

            rc, out, err = _run_reflector(world, agent_dir)
            assert rc == 0, err
            assert "0 auto-resolved" in out, (
                f"no-recovery case must not auto-resolve; out={out!r}")

            status = _goal_status(world, "asp-100", "g-100-97")
            assert status == "pending", (
                f"canary should stay pending; got status={status!r}")


def test_auto_resolve_dry_run_does_not_close():
    """--dry-run reports would-resolve but does not change goal status."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world):
            _inject_canary(world, "asp-100", "g-100-96", "g-100-01",
                           created_at="2026-05-18T12:00:00")
            _set_source_last_achieved(world, "asp-100", "g-100-01",
                                      last_achieved="2026-05-18T14:00:00")

            rc, out, err = _run_reflector(world, agent_dir, dry_run=True)
            assert rc == 0, err
            assert "1 auto-resolved" in out  # counted in summary
            assert "[dry-run] would auto-resolve" in out, (
                f"dry-run banner missing from out={out!r}")

            status = _goal_status(world, "asp-100", "g-100-96")
            assert status == "pending", (
                f"dry-run must NOT mutate; got status={status!r}")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
