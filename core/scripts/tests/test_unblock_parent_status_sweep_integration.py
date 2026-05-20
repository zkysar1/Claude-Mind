"""test_unblock_parent_status_sweep_integration.py - .

Integration test for unblock-parent-status-sweep.py --apply mutation path.
Complements test_unblock_parent_status_sweep.py (which exercises the helper
functions parse_parent_id, is_unblock_goal, is_already_swept, TERMINAL_STATES
at the unit level only). This file builds a real tempdir world+agent queue,
runs the script under DaemonFixture (script uses _rt.aspirations_read),
and asserts mutations land on disk.

The gap this closes (per g-115-699): g-250-76 shipped 12 unit tests covering
helpers, but the full --apply path -- scan active queues -> identify
Unblock+parent-terminal pair -> run aspirations.py update-goal twice ->
verify status=skipped + outcome_note set -- was not regression-pinned. A
silent regression in _mark_skipped (e.g., update sequence swapped, env
var missing, daemon write path drift after the 2026-05-14 cutover) would
not be caught by the unit tests.

Lanes:
  1. happy_path: Unblock + parent in terminal state (skipped) -> applied=1,
     status flips to skipped, outcome_note starts with "parent resolved
     without action needed".
  2. idempotency_already_swept: Unblock with outcome_note already starting
     with sweep phrase -> applied=0, status preserved.
  3. parent_pending_not_applied: Unblock + parent.status=pending ->
     candidates=0, applied=0, Unblock untouched.

Pattern: mirrors test_auto_contract.py + test_inactivity_detector.py:
DaemonFixture spins an in-process daemon pointing at tempdir world; subprocess
invocation receives MIND_WORLD + MIND_AGENT + MIND_AGENT_DIR env so the
script's _mark_skipped subprocess call (sys.executable + aspirations.py
update-goal) reaches the same temp world.

Run: py -3 -m pytest core/scripts/tests/test_unblock_parent_status_sweep_integration.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
SWEEP = CORE_SCRIPTS / "unblock-parent-status-sweep.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world_with_pair(tmp: Path, *, parent_status: str = "skipped",
                          unblock_outcome_note: str | None = None
                          ) -> tuple[Path, Path]:
    """Build tempdir world + agent dir with one Unblock + one parent goal.

    parent_status: status to set on the parent (g-700-69). Use a TERMINAL_STATES
    member (skipped/completed/superseded/archived) for the happy path; use
    "pending" or "in-progress" for the not-eligible path.

    unblock_outcome_note: pre-seed outcome_note on the Unblock. For the
    idempotency test, set this to the canonical sweep phrase so
    _is_already_swept returns True.
    """
    world = tmp / "world"
    world.mkdir()

    parent = {
        "id": "g-700-69",
        "title": "Apply: do thing",
        "description": "Parent goal",
        "status": parent_status,
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    if parent_status in ("skipped", "completed"):
        parent["outcome_note"] = "parent resolved at test time"

    unblock = {
        "id": "g-700-73",
        "title": "Unblock: behavior for g-700-69",
        "description": "Layer D Unblock filed by capability-gate",
        "status": "pending",
        "priority": "HIGH",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "unblock:g-700-69",
        "participants": ["agent"],
        # Aged so age_h >= 0 (default --max-age-hours=0)
        "created_at": "2026-05-01T00:00:00",
    }
    if unblock_outcome_note is not None:
        unblock["outcome_note"] = unblock_outcome_note

    asp = {
        "id": "asp-700",
        "title": "Unblock sweep integration test",
        "motivation": "Test mutation path",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-01T00:00:00",
        "goals": [parent, unblock],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _run_sweep(world: Path, agent_dir: Path, *, apply: bool = True):
    """Invoke the sweep script. Apply mode triggers mutations."""
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    args = [sys.executable, str(SWEEP),
            "--output", "json",
            "--metrics-log", ""]
    if apply:
        args.append("--apply")
    proc = subprocess.run(args, capture_output=True, text=True,
                          timeout=20, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _read_goal(world: Path, goal_id: str) -> dict | None:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


# ---- Tests ----------------------------------------------------------------


def test_happy_path_parent_terminal_unblock_marked_skipped():
    """Canonical Layer D shape: Unblock + parent.status=skipped -> applied=1.

    After the sweep:
      - applied counter == 1
      - candidates length == 1
      - Unblock goal status == "skipped" on disk
      - Unblock goal outcome_note starts with sweep canonical phrase
      - outcome_note references parent_id and parent.status (audit trail)
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_with_pair(Path(tmpd),
                                                  parent_status="skipped")
        with DaemonFixture(world):
            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, f"sweep rc={rc}; stderr={err!r}; stdout={out!r}"
            result = json.loads(out)
            assert result["applied"] == 1, (
                f"expected applied=1 (Unblock+parent-terminal pair), got {result['applied']}; "
                f"candidates={result['candidates']}")
            assert len(result["candidates"]) == 1
            cand = result["candidates"][0]
            assert cand["goal_id"] == "g-700-73"
            assert cand["parent_id"] == "g-700-69"
            assert cand["parent_status"] == "skipped"

            # Verify on-disk mutations
            unblock = _read_goal(world, "g-700-73")
            assert unblock is not None, "Unblock goal disappeared after sweep"
            assert unblock["status"] == "skipped", (
                f"expected status=skipped, got {unblock['status']!r}")
            note = unblock.get("outcome_note") or ""
            assert note.startswith("parent resolved without action needed"), (
                f"outcome_note must start with canonical sweep phrase; got {note!r}")
            assert "g-700-69" in note, (
                f"outcome_note must cite parent_id g-700-69 for audit; got {note!r}")
            assert "skipped" in note, (
                f"outcome_note must cite parent.status=skipped for audit; got {note!r}")


def test_idempotency_already_swept_skipped():
    """Idempotency: Unblock with outcome_note already starting with sweep
    phrase -> applied=0, status preserved.

    This pins _is_already_swept: a re-run of the sweep does NOT double-mutate
    a previously-swept Unblock. The Unblock stays pending (no second
    status flip) and outcome_note is not appended/replaced.
    """
    pre_seeded_note = ("parent resolved without action needed "
                       "(parent_id=g-700-69, parent.status=skipped)")
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_with_pair(
            Path(tmpd),
            parent_status="skipped",
            unblock_outcome_note=pre_seeded_note)
        with DaemonFixture(world):
            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, f"sweep rc={rc}; stderr={err!r}"
            result = json.loads(out)
            assert result["applied"] == 0, (
                f"already-swept Unblock must not re-apply; got applied={result['applied']}")
            assert len(result["candidates"]) == 0, (
                f"already-swept Unblock must not surface as candidate; got {result['candidates']}")

            # Verify on-disk state is untouched
            unblock = _read_goal(world, "g-700-73")
            assert unblock is not None
            assert unblock["status"] == "pending", (
                f"already-swept Unblock status must remain pending; got {unblock['status']!r}")
            assert unblock["outcome_note"] == pre_seeded_note, (
                f"already-swept Unblock outcome_note must not change; got {unblock['outcome_note']!r}")


def test_parent_pending_not_applied():
    """Negative path: parent.status not in TERMINAL_STATES -> applied=0.

    Pins the TERMINAL_STATES gate (skipped/completed/superseded/archived).
    A pending parent means the Unblock's premise is still live; the sweep
    must not skip it.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_with_pair(Path(tmpd),
                                                  parent_status="pending")
        with DaemonFixture(world):
            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, f"sweep rc={rc}; stderr={err!r}"
            result = json.loads(out)
            assert result["applied"] == 0, (
                f"parent.status=pending must NOT trigger mark-skipped; "
                f"got applied={result['applied']}")
            assert len(result["candidates"]) == 0

            # Unblock stays pending; no outcome_note added
            unblock = _read_goal(world, "g-700-73")
            assert unblock is not None
            assert unblock["status"] == "pending"
            assert "outcome_note" not in unblock or not unblock["outcome_note"]
