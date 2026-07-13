"""test_interval_anchor_persist.py -- regression for the unbounded interval-ratchet
fix (g-115-2049).

Bug shape: cargo-cult-detector's auto-extend cap is cap_ratio x original_interval_hours.
The per-goal path (cargo-cult-detector.update_interval_hours) writes the anchor itself,
but the BATCH-CALIBRATE and MANUAL apply paths reach interval_hours through the generic
update-goal chokepoint (CLI aspirations.py cmd_update_goal AND its daemon mirror
mind_api/src/endpoints/aspirations_write.py update_goal) and never persisted
original_interval_hours. So every later auto-extension read orig=None, treated the
already-extended value as "original", and the 3x cap ratcheted UNBOUNDED (g-001-36
root-cause, zeta 2026-07-12).

Fix: persist the cap anchor at the single write site every interval_hours path funnels
through -- the update-goal chokepoint -- to the PRE-update cadence when absent. The
DAEMON endpoint is the LIVE batch-apply path (daemon-only architecture), so the daemon
mirror is the load-bearing half; guard-742 byte-parallel discipline requires both sides.
Plus a review-ritual exemption (calibration_exempt) so deliberate-cadence goals are
never auto-extended into irrelevance.

Pattern: DaemonFixture + direct HTTP POST to the update-goal endpoint (bash-free,
exercises the LIVE daemon path) -- mirrors test_completed_by_stamp.py /
test_add_goal_blocked_since_stamp.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_FILE = PROJECT_ROOT / "core" / "scripts" / "aspirations.py"
DAEMON_FILE = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "aspirations_write.py"
CCD_FILE = PROJECT_ROOT / "core" / "scripts" / "cargo-cult-detector.py"

from _daemon_fixture import DaemonFixture  # noqa: E402


def _load_ccd():
    """Load cargo-cult-detector.py (hyphenated name -> importlib)."""
    spec = importlib.util.spec_from_file_location("cargo_cult_detector", CCD_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_world(tmp: Path) -> Path:
    """Tempdir world with recurring goals in three anchor states."""
    world = tmp / "world"
    world.mkdir()
    # : extended cadence, NO anchor yet (the batch-apply victim).
    g1 = {
        "id": "g-100-01", "title": "Recurring w/o anchor",
        "description": "interval extended via batch-apply, no original_interval_hours",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "recurring": True, "interval_hours": 4, "lastAchievedAt": "2026-07-12T00:00:00",
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "recurring", "participants": ["agent"],
    }
    # : already carries an anchor (must be preserved, never clobbered).
    g2 = {
        "id": "g-100-02", "title": "Recurring w/ anchor",
        "description": "already anchored at 4h, currently at 6h",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "recurring": True, "interval_hours": 6, "original_interval_hours": 4,
        "lastAchievedAt": "2026-07-12T00:00:00",
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "recurring", "participants": ["agent"],
    }
    # : plain goal with NO interval yet (fresh first-set — no spurious anchor).
    g3 = {
        "id": "g-100-03", "title": "No interval yet",
        "description": "first-ever interval set must not create a spurious anchor",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    asp = {
        "id": "asp-100", "title": "interval anchor persist regression",
        "motivation": "Test update-goal anchor persistence", "scope": "project",
        "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00", "goals": [g1, g2, g3],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "delta"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _update_goal(port: int, goal_id: str, field: str, value, agent: str) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/update-goal"
           f"?id={goal_id}&field={field}&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(value).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _find_goal(world: Path, goal_id: str) -> dict | None:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


def test_interval_update_persists_anchor_when_absent():
    """Batch/manual interval_hours write on a goal WITHOUT an anchor persists
    original_interval_hours = the PRE-update cadence (stops the unbounded ratchet)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "interval_hours", 6, "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("interval_hours") == 6, f"interval not written; {out!r}"
            assert g.get("original_interval_hours") == 4, (
                "anchor must be persisted to the PRE-update cadence (4h) on a batch/manual "
                f"interval write when absent; got {g.get('original_interval_hours')!r}")


def test_interval_update_preserves_existing_anchor():
    """An existing original_interval_hours is NOT clobbered by a later interval write."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-02", "interval_hours", 8, "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-02")
            assert g is not None and g.get("interval_hours") == 8
            assert g.get("original_interval_hours") == 4, (
                "pre-existing anchor (4h) must be preserved, never overwritten; "
                f"got {g.get('original_interval_hours')!r}")


def test_fresh_goal_first_interval_no_spurious_anchor():
    """A goal's FIRST-ever interval_hours set must NOT create a spurious anchor
    (pre-update value is None -> skip)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-03", "interval_hours", 4, "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-03")
            assert g is not None and g.get("interval_hours") == 4
            assert g.get("original_interval_hours") in (None, ""), (
                "no anchor should be created on a fresh goal's first interval set; "
                f"got {g.get('original_interval_hours')!r}")


def test_cli_daemon_anchor_parity():
    """Both write-path implementations carry the 9 anchor-persist (guard-742).

    A fix to only one side is half a fix. The daemon side is the LIVE batch-apply path,
    so losing it there silently reopens the unbounded ratchet even with the CLI patched.
    """
    cli = CLI_FILE.read_text(encoding="utf-8")
    daemon = DAEMON_FILE.read_text(encoding="utf-8")
    for label, src in (("CLI", cli), ("daemon", daemon)):
        assert "g-115-2049" in src, f"{label} lost the g-115-2049 anchor-persist marker"
        assert 'goal["original_interval_hours"] = _prev_interval_hours' in src, (
            f"{label} lost the anchor-persist assignment")
        assert 'field == "interval_hours"' in src, f"{label} lost the interval_hours guard"


def test_propose_exempt_returns_none():
    """calibration_exempt goals are never auto-extended (review-ritual exemption)."""
    ccd = _load_ccd()
    cfg = {"multiplier": 1.5, "cap_ratio": 3.0}
    goal = {"interval_hours": 4, "calibration_exempt": True}
    assert ccd._propose_new_interval(goal, cfg) is None, (
        "a calibration_exempt goal must never be proposed for extension")
    # sanity: the same goal WITHOUT the flag would propose an extension
    assert ccd._propose_new_interval({"interval_hours": 4}, cfg) is not None


def test_persisted_anchor_bounds_the_cap():
    """With original_interval_hours persisted, the proposal is capped at
    cap_ratio x TRUE original -- the ratchet is bounded. Without it, the fallback
    reads the already-extended value and the cap ratchets past it."""
    ccd = _load_ccd()
    cfg = {"multiplier": 1.5, "cap_ratio": 3.0}
    # Anchored at 4h, already at the 12h cap (3x4): no further extension proposed.
    assert ccd._propose_new_interval(
        {"interval_hours": 12, "original_interval_hours": 4}, cfg) is None, (
        "at cap (3x original) the anchor must block further extension")
    # No anchor: fallback treats 12h as 'original', proposing 18h -> the exact
    # ratchet the chokepoint fix prevents by always persisting a real anchor.
    assert ccd._propose_new_interval({"interval_hours": 12}, cfg) == 18.0, (
        "documents the unbounded-ratchet behavior when the anchor is absent")


if __name__ == "__main__":
    test_interval_update_persists_anchor_when_absent()
    test_interval_update_preserves_existing_anchor()
    test_fresh_goal_first_interval_no_spurious_anchor()
    test_cli_daemon_anchor_parity()
    test_propose_exempt_returns_none()
    test_persisted_anchor_bounds_the_cap()
    print("ok")
