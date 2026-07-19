"""Integration tests for dry-idle Layer 3 (4-c).

Two halves:
  1. dry-idle-tick.py engine -- subprocess against an isolated MIND_AGENT_DIR
     (the test-only _paths.py override; same pattern as
     test_loop_state_counter_advance.py). Pins: streak advance + curve,
     approved-resets (mutual exclusion), interlude reset (criterion 3 /
     reset_on_executable), disabled/fail-open contract (safe direction =
     dry:false = legacy hot re-entry, never a wrong sleep).
  2. Wiring anchors -- the three surfaces Layer 3 edited are pseudocode/shell,
     not importable, so their contract is pinned by content greps (same
     posture as the verify-learning Section BCP check): the digest dry branch,
     the all-blocked B7 dry branch + DRY_SLEEP B7.2 prefix, and the
     interruptible-sleep.sh DRY_SLEEP registration gate WITHOUT informational
     demotion (criterion 4 / criterion 6 regression guards).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent.parent
TICK = REPO / "core" / "scripts" / "dry-idle-tick.py"
DIGEST = REPO / "core" / "config" / "aspirations-loop-digest.md"
ALL_BLOCKED = REPO / ".claude" / "skills" / "aspirations-all-blocked" / "SKILL.md"
ISLEEP = REPO / "core" / "scripts" / "interruptible-sleep.sh"


def _seed_wm(tmpdir: Path, goals_items=None, dry_idle=None) -> Path:
    session = tmpdir / "session"
    session.mkdir(parents=True, exist_ok=True)
    signals = {"routine_streak_global": 0}
    if dry_idle is not None:
        signals["dry_idle"] = dry_idle
    wm = {
        # Top-level canonical LIST (dicts with _item_ts) — the field
        # _interlude_happened reads (8). Matches production wm shape.
        "goals_completed_this_session": goals_items or [],
        "slots": {
            "loop_state": {
                "goals_completed": 5,
                # INT counter here (NOT the list) — mirrors production AND guards
                # the 8 regression: if _interlude_happened ever reverts to
                # reading THIS field, `for item in <int>` raises TypeError.
                "goals_completed_this_session": len(goals_items or []),
                "signals": signals,
            }
        },
        "slot_meta": {},
    }
    wm_path = session / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
    return wm_path


def _run_tick(tmpdir: Path, count="0", decision="na", extra_env=None):
    env = dict(os.environ)
    env["MIND_AGENT_DIR"] = str(tmpdir)
    env["MIND_AGENT"] = "test-agent-dry-tick"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(TICK),
         "--executable-count", count, "--quiescence-decision", decision],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, f"tick must fail-open exit 0: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _read_dry_idle(wm_path: Path):
    wm = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
    return wm["slots"]["loop_state"]["signals"].get("dry_idle")


# ---------------------------------------------------------------------------
# Engine: streak advance + curve + persistence
# ---------------------------------------------------------------------------

def test_dry_cycles_advance_streak_and_curve(tmp_path):
    wm_path = _seed_wm(tmp_path)
    expected = [(1, 120), (2, 240), (3, 480)]
    for streak, sleep_s in expected:
        out = _run_tick(tmp_path)
        assert out["dry"] is True
        assert out["streak"] == streak
        assert out["sleep_seconds"] == sleep_s
    persisted = _read_dry_idle(wm_path)
    assert persisted["streak"] == 3
    assert persisted["sleep_total_s"] == 120 + 240 + 480


def test_approved_is_never_dry_and_resets_streak(tmp_path):
    """Mutual exclusion (criterion 5 regression, quiescence path UNCHANGED):
    an approved cycle is not dry and resets the streak to 0."""
    wm_path = _seed_wm(tmp_path)
    _run_tick(tmp_path)  # streak 1
    out = _run_tick(tmp_path, decision="approved")
    assert out["dry"] is False
    assert out["streak"] == 0
    assert out["sleep_seconds"] == 0
    assert _read_dry_idle(wm_path)["streak"] == 0


def test_executable_work_is_never_dry(tmp_path):
    """Criterion 6: executable-work agents never dry-sleep."""
    _seed_wm(tmp_path)
    out = _run_tick(tmp_path, count="3", decision="denied")
    assert out["dry"] is False
    assert out["sleep_seconds"] == 0


def test_interlude_resets_streak(tmp_path):
    """Criterion 3 (reset_on_executable): a goal completed AFTER last_dry_at
    breaks the streak -- the next dry cycle restarts at streak 1 / 120s, not
    at the stale continuation."""
    dry_idle = {
        "streak": 4,
        "last_dry_at": "2026-01-01T00:00:00",
        "sleep_total_s": 1800,
        "session_start_at": "2026-01-01T00:00:00",
        "cap_cycles": 0,
    }
    items = [{"goal_id": "g-x", "_item_ts": "2026-01-02T00:00:00"}]  # after last_dry_at
    _seed_wm(tmp_path, goals_items=items, dry_idle=dry_idle)
    out = _run_tick(tmp_path)
    assert out["dry"] is True
    assert out["streak"] == 1, "interlude must reset the stale streak before advancing"
    assert out["sleep_seconds"] == 120


def test_no_interlude_continues_streak(tmp_path):
    """Completions OLDER than last_dry_at are not an interlude -- the streak
    continues (the g-001-213/222 noise class must not reset the curve)."""
    dry_idle = {
        "streak": 2,
        "last_dry_at": "2026-01-03T00:00:00",
        "sleep_total_s": 360,
        "session_start_at": "2026-01-01T00:00:00",
        "cap_cycles": 0,
    }
    items = [{"goal_id": "g-x", "_item_ts": "2026-01-02T00:00:00"}]  # BEFORE last_dry_at
    _seed_wm(tmp_path, goals_items=items, dry_idle=dry_idle)
    out = _run_tick(tmp_path)
    assert out["streak"] == 3
    assert out["sleep_seconds"] == 480


def test_missing_wm_fails_open_not_dry(tmp_path):
    """Fail-open contract: no WM file -> dry:false exit 0 (legacy hot
    re-entry is the safe failure direction, never a wrong sleep)."""
    out = _run_tick(tmp_path)  # no _seed_wm
    assert out["dry"] is False


def test_missing_loop_state_fails_open_not_dry(tmp_path):
    session = tmp_path / "session"
    session.mkdir(parents=True)
    (session / "working-memory.yaml").write_text(
        yaml.safe_dump({"slots": {}, "slot_meta": {}}), encoding="utf-8")
    out = _run_tick(tmp_path)
    assert out["dry"] is False


# ---------------------------------------------------------------------------
# Wiring anchors: digest + B-ladder + interruptible-sleep registration
# ---------------------------------------------------------------------------

def test_digest_dry_branch_anchors():
    """The loop digest's goal-is-None branch must route dry to the bg sleep
    terminal (RETURN), never the synchronous Skill re-entry."""
    text = DIGEST.read_text(encoding="utf-8")
    assert "dry-idle-tick.py" in text, "digest lost the dry tick call"
    assert "DRY_SLEEP=1 bash core/scripts/interruptible-sleep.sh" in text, \
        "digest lost the DRY_SLEEP sleep terminal"
    assert "g-115-2084-c" in text, "digest lost the dry-branch provenance anchor"


def test_all_blocked_b7_dry_anchors():
    """B7 must consult the tick for the dry curve and B7.2 must carry the
    DRY_SLEEP env alternative; the legacy schedule stays as the fail-open."""
    text = ALL_BLOCKED.read_text(encoding="utf-8")
    assert "dry-idle-tick.py" in text, "B7 lost the dry tick call"
    assert "dry_sleep_env" in text, "B7/B7.2 lost the DRY_SLEEP env plumbing"
    assert "BACKOFF_SCHEDULE = [300, 600, 1200, 1800]" in text, \
        "B7 lost the legacy fail-open schedule"


def test_interruptible_sleep_dry_registration_without_demotion():
    """DRY_SLEEP=1 must trigger the Tier-A bg-job registration (guard-967)
    but must NOT appear in the informational-demotion condition -- dry sleeps
    stay fully wake-sensitive (criterion 4); quiescence demotion unchanged
    (criterion 6)."""
    text = ISLEEP.read_text(encoding="utf-8")
    assert '[ "${DRY_SLEEP:-0}" = "1" ]' in text, \
        "registration gate lost the DRY_SLEEP condition"
    assert "dry-idle-sleep" in text, "dry job type/id naming missing"
    demotion_lines = [
        ln for ln in text.splitlines()
        if 'class" = "informational"' in ln
    ]
    assert demotion_lines, "informational demotion branch vanished entirely"
    for ln in demotion_lines:
        assert "DRY_SLEEP" not in ln, \
            "informational demotion must stay QUIESCENCE_SLEEP-only"
        assert "QUIESCENCE_SLEEP" in ln, \
            "informational demotion lost its quiescence key"
