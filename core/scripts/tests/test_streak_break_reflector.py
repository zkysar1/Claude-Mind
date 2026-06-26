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


def _make_world_and_agent_with_agent_asp(tmp: Path, agent_name: str = "alpha"):
    """Seed minimal world + agent dir with the recurring goal in the AGENT queue.

    Mirror of _make_world_and_agent but the test aspiration lives in
    agent_dir/aspirations.jsonl, not world. Used by the agent-queue path
    tests for g-115-980 (source-aware filing).
    """
    world = tmp / "world"
    world.mkdir()
    (world / "aspirations.jsonl").write_text("", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / agent_name
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    asp_agent = {
        "id": "asp-200",
        "title": "Test agent asp",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-08T12:00:00",
        "goals": [{
            "id": "g-200-01",
            "title": "Recurring agent-queue goal",
            "description": "Test",
            "status": "pending",
            "priority": "MEDIUM",
            "recurring": True,
            "interval_hours": 4,
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [],
                             "preconditions": []},
            "origin_signal": "user_directive",
            "achievedCount": 1,
            "participants": ["agent"],
        }],
    }
    (agent_dir / "aspirations.jsonl").write_text(
        json.dumps(asp_agent, ensure_ascii=False) + "\n", encoding="utf-8")
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


# ---- Agent-queue path tests () -----------------------------------
#
#  fixed source-misrouting in _file_investigate: _rt.aspirations_add_goal
# defaults to source="world", so signals for agent-queue goals (e.g., 
# on asp-001 in the agent queue) silently failed with aspiration_not_found
# in world. The fix returns (asp_id, source) from _aspiration_for_goal and
# threads source through _file_investigate -> _rt.aspirations_add_goal.


def _seed_agent_asp_at_daemon_path(daemon_agent_dir: Path):
    """Write the asp-200 agent aspiration into the daemon's agent_dir.

    The daemon resolves agent_dir from its project_root (pr/agents/<agent>),
    NOT from MIND_AGENT_DIR. For agent-queue tests both the subprocess
    reflector and the daemon must read/write the same agent aspirations
    file, so seed at the daemon's path AND pass that same path as the
    subprocess's MIND_AGENT_DIR via _run_reflector(agent_dir=...).
    """
    daemon_agent_dir.mkdir(parents=True, exist_ok=True)
    (daemon_agent_dir / "session").mkdir(parents=True, exist_ok=True)
    asp_agent = {
        "id": "asp-200",
        "title": "Test agent asp",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-08T12:00:00",
        "goals": [{
            "id": "g-200-01",
            "title": "Recurring agent-queue goal",
            "description": "Test",
            "status": "pending",
            "priority": "MEDIUM",
            "recurring": True,
            "interval_hours": 4,
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [],
                             "preconditions": []},
            "origin_signal": "user_directive",
            "achievedCount": 1,
            "participants": ["agent"],
        }],
    }
    (daemon_agent_dir / "aspirations.jsonl").write_text(
        json.dumps(asp_agent, ensure_ascii=False) + "\n", encoding="utf-8")
    (daemon_agent_dir / "aspirations-archive.jsonl").write_text(
        "", encoding="utf-8")


def test_signal_with_explicit_source_routes_to_agent_queue():
    """Signal entry carrying source='agent' files Investigate in agent queue."""
    with tempfile.TemporaryDirectory() as tmpd:
        # Build only the world; the agent_dir lives inside the daemon's PR
        world = Path(tmpd) / "world"
        world.mkdir()
        (world / "aspirations.jsonl").write_text("", encoding="utf-8")
        (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

        with DaemonFixture(world) as df:
            daemon_agent_dir = df.project_root / "agents" / df.agent
            _seed_agent_asp_at_daemon_path(daemon_agent_dir)
            signal = {
                "timestamp": "2026-05-08T12:00:00",
                "goal_id": "g-200-01",
                "aspiration_id": "asp-200",
                "source": "agent",
                "expected_interval_hours": 4,
                "actual_elapsed_hours": 12.5,
                "lateness_ratio": 3.13,
                "processed": False,
            }
            _write_signals(daemon_agent_dir, [signal])

            rc, out, err = _run_reflector(world, daemon_agent_dir)
            assert rc == 0, err

            # Investigate landed in the AGENT queue
            agent_path = daemon_agent_dir / "aspirations.jsonl"
            agent_asps = [json.loads(line) for line in
                          agent_path.read_text(encoding="utf-8").splitlines()
                          if line.strip()]
            asp_200 = next(a for a in agent_asps if a["id"] == "asp-200")
            streak_goals = [g for g in asp_200["goals"]
                            if "streak-break" in g.get("title", "").lower()]
            assert len(streak_goals) == 1, (
                f"Expected 1 streak-break Investigate in agent queue, "
                f"got {len(streak_goals)}. stdout={out!r} stderr={err!r}")
            assert "g-200-01" in streak_goals[0]["title"]

            # World queue stays empty (no misrouting)
            world_text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
            assert "streak-break" not in world_text.lower(), (
                f"World queue should not contain streak-break goals; "
                f"reflector misrouted to world. World content: {world_text!r}")

            signals = _read_signals(daemon_agent_dir)
            assert signals[0]["processed"] is True
            assert "filed_at" in signals[0]
            assert "filing_error" not in signals[0], (
                f"Filing should have succeeded; got error: "
                f"{signals[0].get('filing_error')}")


def test_legacy_signal_falls_back_to_lookup_for_agent_queue():
    """Legacy signal lacking source field — _aspiration_for_goal lookup
    finds the goal in the agent queue and threads source through."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        (world / "aspirations.jsonl").write_text("", encoding="utf-8")
        (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

        with DaemonFixture(world) as df:
            daemon_agent_dir = df.project_root / "agents" / df.agent
            _seed_agent_asp_at_daemon_path(daemon_agent_dir)
            signal = {
                "timestamp": "2026-05-08T12:00:00",
                "goal_id": "g-200-01",
                # No aspiration_id, no source — forces _aspiration_for_goal
                "expected_interval_hours": 4,
                "actual_elapsed_hours": 12.5,
                "processed": False,
            }
            _write_signals(daemon_agent_dir, [signal])

            rc, out, err = _run_reflector(world, daemon_agent_dir)
            assert rc == 0, err

            agent_path = daemon_agent_dir / "aspirations.jsonl"
            agent_asps = [json.loads(line) for line in
                          agent_path.read_text(encoding="utf-8").splitlines()
                          if line.strip()]
            asp_200 = next(a for a in agent_asps if a["id"] == "asp-200")
            streak_goals = [g for g in asp_200["goals"]
                            if "streak-break" in g.get("title", "").lower()]
            assert len(streak_goals) == 1, (
                f"Legacy signal lookup should route to agent queue; "
                f"found {len(streak_goals)} streak-break goals. "
                f"stdout={out!r} stderr={err!r}")

            world_text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
            assert "streak-break" not in world_text.lower()

            signals = _read_signals(daemon_agent_dir)
            assert signals[0]["processed"] is True
            assert "filing_error" not in signals[0]


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


# ---- Option A filing gate: session-gap canary suppression (9) -----


def _write_diary(agent_dir: Path, timestamps: list[str]):
    """Write execution-diary.jsonl entries (each carries a timestamp).

    _has_session_gap only reads the `timestamp` field; other fields are
    decorative. Used to simulate agent activity / inactivity across a
    streak-break window.
    """
    diary = agent_dir / "session" / "execution-diary.jsonl"
    with open(diary, "w", encoding="utf-8") as f:
        for ts in timestamps:
            f.write(json.dumps({"timestamp": ts, "event": "tick"},
                               ensure_ascii=False) + "\n")


def test_session_gap_suppresses_filing():
    """A streak-break whose elapsed window is explained by agent inactivity
    is SUPPRESSED at filing time, not filed (Option A gate, g-115-1319).

    Window = [2026-05-07T23:30, 2026-05-08T12:00] (12.5h). The diary has
    entries only OUTSIDE that window, so there is no in-window activity and
    the 12.5h span exceeds the 2h default threshold → session gap → suppress.
    DaemonFixture isolates any errant filing into the test world (a live
    daemon is otherwise reachable).
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world):
            _write_diary(agent_dir, [
                "2026-05-06T08:00:00",
                "2026-05-09T08:00:00",
            ])
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

            # No Investigate canary filed
            asps = _read_aspirations(world, agent_dir)
            asp_100 = next(a for a in asps if a["id"] == "asp-100")
            new_goals = [g for g in asp_100["goals"]
                         if "streak-break" in g.get("title", "").lower()]
            assert len(new_goals) == 0, (
                "session-gap break must NOT file a canary")

            # Signal marked suppressed + processed (won't re-fire)
            signals = _read_signals(agent_dir)
            assert signals[0]["processed"] is True
            assert signals[0].get("session_gap_suppressed") is True
            assert "session-gap suppressed" in out
            assert "1 session-gap-suppressed" in out


# ---- Pass-1 selector-contention suppression (3) ------------------
#
# Supersedes the prior `test_continuous_activity_still_files`. 9
# treated "continuous activity, cadence missed" as real drift and filed.
# The 3 investigation (99 world canaries, median 4.77x interval,
# 98 closed transient/FP) showed that for best-effort recurring cadence
# (rb-257) an active-loop miss of a NON-HIGH goal is the selector reasonably
# prioritizing other work — not drift. The gate now suppresses NON-HIGH
# continuous-activity breaks while preserving HIGH (cadence matters there).


def _set_goal_priority(world: Path, asp_id: str, gid: str, priority: str):
    """Patch a goal's priority directly on disk (test helper)."""
    asp_path = world / "aspirations.jsonl"
    asps = []
    for line in asp_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            asps.append(json.loads(line))
    target = next(a for a in asps if a["id"] == asp_id)
    g = next(x for x in target["goals"] if x["id"] == gid)
    g["priority"] = priority
    with open(asp_path, "w", encoding="utf-8") as f:
        for a in asps:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")


def test_continuous_activity_nonhigh_contention_suppressed():
    """A NON-HIGH recurring goal whose elapsed window had the agent
    continuously active (no session gap) is SUPPRESSED as selector contention,
    not filed (g-115-1643). Same 12.5h window with a tick every 30 min (no gap
    >= 2h); goal g-100-01 is MEDIUM → contention suppress. The diary has
    in-window activity (positive evidence), so this is NOT the session-gap path.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world):
            base = datetime.fromisoformat("2026-05-07T23:00:00")
            ticks = [(base + timedelta(minutes=30 * i)).isoformat()
                     for i in range(30)]  # 23:00 .. 13:30 next day, 30m apart
            _write_diary(agent_dir, ticks)
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

            # No canary filed — suppressed as selector contention
            asps = _read_aspirations(world, agent_dir)
            asp_100 = next(a for a in asps if a["id"] == "asp-100")
            new_goals = [g for g in asp_100["goals"]
                         if "streak-break" in g.get("title", "").lower()]
            assert len(new_goals) == 0, (
                "MEDIUM continuous-activity break must be contention-suppressed")

            signals = _read_signals(agent_dir)
            assert signals[0]["processed"] is True
            assert signals[0].get("contention_suppressed") is True
            assert signals[0].get("session_gap_suppressed") is not True
            assert "1 contention-suppressed" in out, (
                f"summary missing contention count: out={out!r}")


def test_continuous_activity_high_priority_still_files():
    """A HIGH-priority recurring goal with continuous activity STILL files
    (g-115-1643) — the gate preserves the discriminating signal for HIGH goals,
    where cadence matters and the selector's overdue boost should have prevented
    the miss. Same continuous-activity window as the MEDIUM suppression test,
    but g-100-01 is patched to HIGH → files.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world):
            _set_goal_priority(world, "asp-100", "g-100-01", "HIGH")
            base = datetime.fromisoformat("2026-05-07T23:00:00")
            ticks = [(base + timedelta(minutes=30 * i)).isoformat()
                     for i in range(30)]
            _write_diary(agent_dir, ticks)
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

            asps = _read_aspirations(world, agent_dir)
            asp_100 = next(a for a in asps if a["id"] == "asp-100")
            new_goals = [g for g in asp_100["goals"]
                         if "streak-break" in g.get("title", "").lower()]
            assert len(new_goals) == 1, (
                "HIGH-priority continuous-activity break must still file")

            signals = _read_signals(agent_dir)
            assert signals[0]["processed"] is True
            assert signals[0].get("contention_suppressed") is not True


def test_missing_diary_continuous_activity_unknown_still_files():
    """A MEDIUM break with NO execution-diary files normally — a missing diary
    means activity is UNKNOWN, not 'agent active', so contention suppression
    must NOT fire (g-115-1643 positive-evidence requirement). This pins the
    fix's conservatism and guards the pre-existing 'file' contract that the
    other no-diary tests rely on.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        with DaemonFixture(world):
            # Intentionally write NO execution-diary.jsonl.
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

            asps = _read_aspirations(world, agent_dir)
            asp_100 = next(a for a in asps if a["id"] == "asp-100")
            new_goals = [g for g in asp_100["goals"]
                         if "streak-break" in g.get("title", "").lower()]
            assert len(new_goals) == 1, (
                "no-diary break must file (activity unknown, not suppressed)")
            signals = _read_signals(agent_dir)
            assert signals[0].get("contention_suppressed") is not True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
