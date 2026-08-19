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
    """Both write-path implementations carry the  anchor-persist (guard-742).

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


# ---------------------------------------------------------------------------
# : deliberate-raise anchor re-base (the FLOOR consumer's half)
#
# original_interval_hours feeds TWO consumers with opposite requirements. The
# tests above pin the CAP side (immutable anchor -> bounded auto-extension).
# These pin the FLOOR side: contract floor = original*contract_floor_ratio goes
# stale-LOW when a cadence is deliberately RAISED, so a goal widened 24h->168h
# kept floor = 24*0.33 = 7.92h and a deep-outcome streak could walk a weekly
# cadence back toward ~8h.
#
# The discriminator must not weaken the cap: an auto-extension is bounded BY
# CONSTRUCTION at original*cap_ratio, so only a write STRICTLY ABOVE that bound
# re-bases. The at-cap test below is the one that would go red if the 
# ratchet were reintroduced through this branch.
# ---------------------------------------------------------------------------


def test_deliberate_raise_rebases_anchor():
    """A raise ABOVE original*cap_ratio is provably manual -> re-base the anchor.

    g-100-02 is anchored at 4h. 24h > 4*3.0, so no auto-extension could have
    produced it; the anchor must move to 24 so the contract floor becomes
    24*0.33 = 7.92h instead of the stale 1.32h.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-02", "interval_hours", 24, "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-02")
            assert g is not None and g.get("interval_hours") == 24
            assert g.get("original_interval_hours") == 24, (
                "a raise above original*cap_ratio is unreachable by auto-extension and "
                "must re-base the anchor so the contract floor tracks the deliberate "
                f"cadence; got {g.get('original_interval_hours')!r}")


def test_auto_extension_at_cap_does_not_rebase():
    """A raise to EXACTLY original*cap_ratio is reachable by auto-extension -> no re-base.

    This is the g-001-36 guard. If the strict `>` were ever relaxed to `>=`, the
    anchor would follow each capped auto-extension (4 -> 12 -> 36 -> ...) and the
    cap would ratchet unbounded again.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-02", "interval_hours", 12, "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-02")
            assert g is not None and g.get("interval_hours") == 12
            assert g.get("original_interval_hours") == 4, (
                "12h is exactly 3.0x the 4h anchor and therefore producible by a capped "
                "auto-extension; the anchor must stay immutable or the g-001-36 ratchet "
                f"reopens; got {g.get('original_interval_hours')!r}")


def test_rebase_runs_twice_without_ratcheting():
    """Run the write path TWICE (guard-3116): re-basing must not become a ratchet.

    Write 1 is a deliberate raise (4 -> 24) and DOES re-base. Write 2 raises again
    to 30h, which is inside the NEW anchor's cap (24*3.0 = 72) and so is
    indistinguishable from an auto-extension — it must NOT re-base. Asserting only
    the first write would leave a compounding anchor undetected, which is exactly
    the failure shape a single-write fixture cannot see.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            s1, o1 = _update_goal(df.port, "g-100-02", "interval_hours", 24, "delta")
            assert s1 == 200, f"first update status={s1}; body={o1!r}"
            assert _find_goal(world, "g-100-02").get("original_interval_hours") == 24

            s2, o2 = _update_goal(df.port, "g-100-02", "interval_hours", 30, "delta")
            assert s2 == 200, f"second update status={s2}; body={o2!r}"
            g = _find_goal(world, "g-100-02")
            assert g.get("interval_hours") == 30
            assert g.get("original_interval_hours") == 24, (
                "30h is within the re-based anchor's cap (24*3.0=72) so it is "
                "auto-extension-shaped and must leave the anchor alone; a second "
                f"re-base here is a compounding ratchet; got "
                f"{g.get('original_interval_hours')!r}")


def test_absent_anchor_path_unchanged_by_rebase_branch():
    """The  write-once branch still wins when the anchor is ABSENT.

    The re-base branch is an `elif`, so a goal with no anchor must still take the
    original persist-PRE-update-cadence path even when the new value would clear
    the cap bound. g-100-01 has no anchor and interval 4; writing 99 must anchor
    to 4 (the pre-update cadence), NOT to 99.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "interval_hours", 99, "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g.get("original_interval_hours") == 4, (
                "absent-anchor goals must keep taking the g-115-2049 pre-update-cadence "
                f"path; got {g.get('original_interval_hours')!r}")


def test_cli_daemon_rebase_parity():
    """Both write paths carry the  re-base (guard-742 byte-parallel).

    The daemon is the LIVE batch/manual apply path under daemon-only architecture,
    so a CLI-only re-base is the half that never runs.
    """
    cli = CLI_FILE.read_text(encoding="utf-8")
    daemon = DAEMON_FILE.read_text(encoding="utf-8")
    for label, src in (("CLI", cli), ("daemon", daemon)):
        assert "g-115-6104" in src, f"{label} lost the g-115-6104 re-base marker"
        assert "_is_deliberate_raise(_anchor, _new_interval)" in src, (
            f"{label} lost the deliberate-raise call")
        assert "from _cadence_anchor import is_deliberate_raise" in src, (
            f"{label} lost the shared-policy import — a local reimplementation is a "
            "second place for cap_ratio to drift")


def test_shared_policy_is_single_definition():
    """cap_ratio is read from the SAME config block cargo-cult-detector reads.

    If the two ever diverged, a write the detector COULD have produced would be
    classified deliberate and re-base the anchor — the ratchet re-entering
    through the new branch.
    """
    import _cadence_anchor
    ccd = _load_ccd()
    assert _cadence_anchor.cargo_cult_cap_ratio() == ccd._load_detector_config()["cap_ratio"], (
        "the re-base discriminator and the cap it reasons about must read one value")
    # Fail-safe default agrees with the detector's own default.
    assert _cadence_anchor.DEFAULT_CAP_RATIO == 3.0


if __name__ == "__main__":
    test_interval_update_persists_anchor_when_absent()
    test_interval_update_preserves_existing_anchor()
    test_fresh_goal_first_interval_no_spurious_anchor()
    test_cli_daemon_anchor_parity()
    test_propose_exempt_returns_none()
    test_persisted_anchor_bounds_the_cap()
    test_deliberate_raise_rebases_anchor()
    test_auto_extension_at_cap_does_not_rebase()
    test_rebase_runs_twice_without_ratcheting()
    test_absent_anchor_path_unchanged_by_rebase_branch()
    test_cli_daemon_rebase_parity()
    test_shared_policy_is_single_definition()
    print("ok")
