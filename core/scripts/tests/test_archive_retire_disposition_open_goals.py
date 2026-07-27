"""test_archive_retire_disposition_open_goals.py —  regression test.

Pins the RETIRED-ARCHIVE-BOUNDARY disposition invariant added in g-115-2860:
when an aspiration is retired or swept to the archive, its OPEN (non-recurring,
non-terminal) goals are flipped to status="skipped" BEFORE the aspiration is
appended to aspirations-archive.jsonl — so they do not strand as
invisible-pending (no read path or the goal selector scans the archive).

── The bug this test pins ───────────────────────────────────────────────────
The COMPLETED-archive path had a recovery guard (_find_unfinished_goals →
reset to active, keep live). The RETIRED path had NO equivalent guard, so a
retired aspiration's open goals rode into the archive still pending/blocked and
could never be reached, completed, or surfaced — the stray-goal class
g-115-2860 was filed to fix. The fix adds _disposition_open_goals_on_retire()
at BOTH retired boundaries (retire endpoint + archive_sweep's to_archive path).

A pure-helper unit test would NOT have caught the ORIGINAL bug (a missing call
site); this integration test seeds the exact retired-with-open-goals shape and
asserts the archived goals are terminal — and, critically, that
completed-with-open-goals is still RECOVERED (kept live), NOT dispositioned, so
the fix stays scoped to the retired path only.

Hermetic: in-process DaemonFixture (no daemon_integration marker) — runs in the
daemon-safe -m "not daemon_integration" subset.

g-115-2882 extends the invariant to the two remaining `_append_jsonl(
archive_path, asp)` sites: complete(force=true) — a REAL strand (the unfinished
guard is skipped, no supersession) now dispositioned; and complete_intent —
proven NON-stranding (its `remaining_unfinished` validator + superseded-in-
terminal guarantee), so it deliberately runs NO disposition. The two new tests
pin both.

Refs: g-115-2860, g-115-2882, mind_api/src/endpoints/aspirations_write.py
(_disposition_open_goals_on_retire + retire + archive_sweep + complete(force)).
Run: py -3 -m pytest core/scripts/tests/test_archive_retire_disposition_open_goals.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # shared in-process daemon ()

CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
import _rt  # canonical Python -> daemon client


# ── Seed helpers ─────────────────────────────────────────────────────────────

def _goal(gid: str, status: str) -> dict:
    return {
        "id": gid,
        "title": f"Goal {gid}",
        "description": "disposition invariant probe",
        "status": status,
        "priority": "MEDIUM",
        "recurring": False,
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }


def _seed_world(tmp: Path, *, asp_status: str, goals: list[dict]) -> Path:
    world = tmp / "world"
    world.mkdir(exist_ok=True)
    asp = {
        "id": "asp-900",
        "title": "Test asp",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": asp_status,
        "created": "2026-07-22T00:00:00",
        "goals": goals,
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _read_archive_goal(world: Path, goal_id: str) -> dict:
    text = (world / "aspirations-archive.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    raise KeyError(f"{goal_id} not in archive")


def _read_live_goal(world: Path, goal_id: str) -> dict | None:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


def _live_asp(world: Path, asp_id: str) -> dict | None:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        if asp.get("id") == asp_id:
            return asp
    return None


def _retire(asp_id: str = "asp-900") -> None:
    _rt.rt_call("POST", "/v1/aspirations/retire",
                query=f"asp_id={asp_id}&source=world")


def _archive_sweep() -> None:
    _rt.rt_call("POST", "/v1/aspirations/archive-sweep", query="source=world")


def _complete_force(asp_id: str = "asp-900") -> None:
    _rt.rt_call("POST", "/v1/aspirations/complete",
                query=f"asp_id={asp_id}&source=world&force=true")


def _complete_intent(intent_block: dict, asp_id: str = "asp-900") -> None:
    _rt.rt_call("POST", "/v1/aspirations/complete-intent",
                query=f"asp_id={asp_id}&source=world",
                body=json.dumps(intent_block))


def _seed_intent_config(project_root: Path) -> None:
    """complete_intent reads project_root/core/config/aspirations.yaml for its
    intent_satisfaction config (min_evidence_by_scope). The DaemonFixture repo
    lacks it — seed a minimal SYNTHETIC config (project=1) so the intent path
    reaches the archive code under test, decoupled from real-config tuning."""
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        "intent_satisfaction:\n"
        "  min_evidence_by_scope:\n"
        "    sprint: 1\n"
        "    project: 1\n"
        "    initiative: 1\n",
        encoding="utf-8")


# ── Tests ────────────────────────────────────────────────────────────────────

def test_retire_dispositions_open_goals_to_skipped():
    """The core  invariant on the RETIRE endpoint: open (pending +
    blocked) goals become status="skipped" in the archive with the auditable
    stranding marker; an already-completed goal in the same aspiration is
    UNTOUCHED (idempotent on terminal goals)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), asp_status="active", goals=[
            _goal("g-900-01", "pending"),
            _goal("g-900-02", "blocked"),
            _goal("g-900-03", "completed"),
        ])
        with DaemonFixture(world):
            _retire()

        # The two OPEN goals are now terminal-in-archive, not invisible-pending:
        for gid in ("g-900-01", "g-900-02"):
            g = _read_archive_goal(world, gid)
            assert g["status"] == "skipped", (
                f"{gid} must be dispositioned to 'skipped' at the retired-archive "
                f"boundary, got {g['status']!r} (non-terminal here is the "
                "invisible-pending stranding bug g-115-2860 fixes)")
            assert g.get("stranded_on_retire") is True, (
                f"{gid} must carry the stranded_on_retire audit marker")
            assert "g-115-2860" in (g.get("disposition_reason") or ""), (
                f"{gid} must record a disposition_reason citing g-115-2860")
            assert g.get("skipped_at"), f"{gid} must be stamped skipped_at"

        # The already-terminal goal is left exactly as it was:
        done = _read_archive_goal(world, "g-900-03")
        assert done["status"] == "completed", (
            "an already-completed goal must NOT be re-dispositioned")

        # The aspiration left the live queue (retired → archived):
        assert _live_asp(world, "asp-900") is None, (
            "retired aspiration must be popped from the live queue")


def test_archive_sweep_dispositions_retired_open_goals():
    """archive_sweep's RETIRED path (status=retired reaching to_archive) also
    dispositions open goals — the second boundary the fix wired."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), asp_status="retired", goals=[
            _goal("g-900-01", "pending"),
            _goal("g-900-04", "in-progress"),
        ])
        with DaemonFixture(world):
            _archive_sweep()

        for gid in ("g-900-01", "g-900-04"):
            g = _read_archive_goal(world, gid)
            assert g["status"] == "skipped", (
                f"{gid} in a swept RETIRED aspiration must be 'skipped', got "
                f"{g['status']!r}")
            assert g.get("stranded_on_retire") is True


def test_archive_sweep_completed_open_goals_recovered_not_dispositioned():
    """NEGATIVE guard: the fix must stay scoped to the RETIRED path. A COMPLETED
    aspiration carrying an open goal is RECOVERED (reset to active, kept LIVE),
    NOT archived and NOT dispositioned — the pre-existing completed-path recovery
    guard still owns that case. This pins that _disposition_open_goals_on_retire
    is never reached for completed-with-open-goals."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), asp_status="completed", goals=[
            _goal("g-900-05", "pending"),
        ])
        with DaemonFixture(world):
            _archive_sweep()

        # Recovered to the live queue as active; goal stays pending (selectable):
        asp = _live_asp(world, "asp-900")
        assert asp is not None, (
            "completed-with-open-goals must be RECOVERED to live, not archived")
        assert asp["status"] == "active", (
            f"recovery guard must reset status active, got {asp['status']!r}")
        g = _read_live_goal(world, "g-900-05")
        assert g is not None and g["status"] == "pending", (
            "the open goal must stay pending (recovered), NOT dispositioned to "
            "skipped — disposition is retired-path-only")


def test_retire_no_open_goals_is_noop():
    """A retire with only terminal goals dispositions nothing — the helper is a
    no-op when there are no open goals (idempotence / no spurious markers)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), asp_status="active", goals=[
            _goal("g-900-06", "completed"),
            _goal("g-900-07", "skipped"),
        ])
        with DaemonFixture(world):
            _retire()

        for gid, expect in (("g-900-06", "completed"), ("g-900-07", "skipped")):
            g = _read_archive_goal(world, gid)
            assert g["status"] == expect, (
                f"{gid} terminal status must be preserved, got {g['status']!r}")
            assert "stranded_on_retire" not in g, (
                f"{gid} must NOT gain the stranding marker (no open goals)")


# ── : the two remaining archive-append sites ───────────────────────

def test_complete_force_dispositions_open_goals():
    """: complete(force=true) skips the unfinished-goals guard and
    archives directly — its open (pending/blocked) non-recurring goals must be
    dispositioned to 'skipped' BEFORE the archive append, exactly like retire()
    and archive_sweep(), or they strand as invisible-pending. This is the REAL
    strand the g-115-2882 fix closes: the third `_append_jsonl(archive_path,
    asp)` site, previously uncovered."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), asp_status="active", goals=[
            _goal("g-900-01", "pending"),
            _goal("g-900-02", "blocked"),
            _goal("g-900-03", "completed"),
        ])
        with DaemonFixture(world):
            _complete_force()

        for gid in ("g-900-01", "g-900-02"):
            g = _read_archive_goal(world, gid)
            assert g["status"] == "skipped", (
                f"{gid} force-completed with an open status must be "
                f"dispositioned to 'skipped' in the archive, got {g['status']!r} "
                "(non-terminal here is the invisible-pending stranding bug "
                "g-115-2882 fixes for the force path)")
            assert g.get("stranded_on_retire") is True, (
                f"{gid} must carry the stranded_on_retire audit marker")
            assert g.get("skipped_at"), f"{gid} must be stamped skipped_at"

        done = _read_archive_goal(world, "g-900-03")
        assert done["status"] == "completed", (
            "an already-completed goal must NOT be re-dispositioned")

        assert _live_asp(world, "asp-900") is None, (
            "force-completed aspiration must be popped from the live queue")


def test_complete_intent_supersedes_open_goal_no_strand_no_reskip():
    """ companion (the NEGATIVE / no-op-backstop decision): the
    complete_intent archive site deliberately runs NO
    _disposition_open_goals_on_retire, because its validator's
    `remaining_unfinished` guard refuses completion unless every open
    non-recurring goal is in superseded_goal_ids, and supersession then flips
    each to status='superseded' (a terminal status). This pins that (a) the open
    goal does not strand — it archives terminal, and (b) it archives as
    'superseded', NOT re-flipped to 'skipped' (which a spurious disposition
    backstop would wrongly do — the exact mistake the no-op analysis avoided)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), asp_status="active", goals=[
            _goal("", "completed"),   # evidence
            _goal("", "pending"),      # open → must be superseded
        ])
        intent = {
            "evidence_goal_ids": ["g-900-10"],
            "superseded_goal_ids": ["g-900-11"],
            "rationale": (
                "Test intent is satisfied: the completed evidence goal meets "
                "the Test motivation; the remaining goal is superseded."),
        }
        with DaemonFixture(world) as df:
            _seed_intent_config(df.project_root)
            _complete_intent(intent)

        superseded = _read_archive_goal(world, "g-900-11")
        assert superseded["status"] == "superseded", (
            f"g-900-11 must archive as 'superseded' (its intended terminal "
            f"transition), got {superseded['status']!r} — not stranded pending")
        assert superseded.get("stranded_on_retire") is not True, (
            "a superseded goal must NOT carry the retire-disposition marker — "
            "complete_intent deliberately runs no disposition (validator + "
            "superseded-in-terminal already guarantee no strand)")

        ev = _read_archive_goal(world, "g-900-10")
        assert ev["status"] == "completed", "evidence goal must stay completed"

        assert _live_asp(world, "asp-900") is None, (
            "intent-completed aspiration must be popped from the live queue")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
