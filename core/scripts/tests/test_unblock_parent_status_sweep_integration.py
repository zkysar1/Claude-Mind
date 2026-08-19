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
  2a. partial_write_is_repaired: Unblock carrying the sweep phrase but a
     NON-terminal status (the state a failed second write leaves) -> applied=1,
     status flips to skipped. Self-heals; g-115-5097 inverted this lane, which
     previously asserted applied=0 and so pinned the defect.
  2b. fully_swept_not_reprocessed: Unblock with the sweep phrase AND a terminal
     status -> applied=0, untouched. Carries the no-double-mutate invariant.
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
                          unblock_outcome_note: str | None = None,
                          parent_extra: dict | None = None,
                          unblock_extra: dict | None = None
                          ) -> tuple[Path, Path]:
    """Build tempdir world + agent dir with one Unblock + one parent goal.

    parent_status: status to set on the parent (g-700-69). Use a TERMINAL_STATES
    member (skipped/completed/superseded/archived) for the happy path; use
    "pending" or "in-progress" for the not-eligible path.

    unblock_outcome_note: pre-seed outcome_note on the Unblock. For the
    idempotency test, set this to the canonical sweep phrase so
    _is_already_swept returns True.

    parent_extra / unblock_extra: field overrides merged LAST onto the base
    goal dicts (g-115-2536 — lets the rb-3887 provenance fixture reshape the
    Unblock's parent-link fields without a parallel builder).
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
    if parent_extra:
        parent.update(parent_extra)
    if unblock_extra:
        unblock.update(unblock_extra)

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


def test_partial_write_is_repaired_not_sealed():
    """ — END-TO-END self-heal, and this test's ASSERTIONS ARE INVERTED
    from what they were before that goal.

    It previously asserted applied==0 and "status must remain pending" for a
    note-bearing goal — i.e. it pinned the DEFECT as correct behaviour, and did
    so convincingly, because "a re-run must not double-mutate" is a real and
    good invariant. The fixture just was not an already-swept goal. It is a
    goal mid-way through a FAILED sweep: _mark_skipped writes outcome_note and
    status as two non-atomic daemon calls, so note-without-terminal-status is
    precisely the state left behind when write 2 fails. Treating it as "already
    swept" is what made the sweep's own partial success seal the goal against
    its own repair, permanently and silently.

    The correct behaviour is to finish the job. Sibling unit-level pin:
    test_partial_write_is_not_already_swept in test_unblock_parent_status_sweep.
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
            assert result["applied"] == 1, (
                f"a partial write (note, no terminal status) must be RETRIED and "
                f"completed, not skipped as already-swept; got "
                f"applied={result['applied']}")

            unblock = _read_goal(world, "g-700-73")
            assert unblock is not None
            assert unblock["status"] == "skipped", (
                f"the stranded Unblock must reach a terminal status on the next "
                f"run; got {unblock['status']!r}")
            assert unblock["outcome_note"].startswith(
                "parent resolved without action needed"), (
                f"the sweep phrase must survive the repair; got "
                f"{unblock['outcome_note']!r}")


def test_fully_swept_goal_is_not_reprocessed():
    """The no-double-mutate invariant the inverted test above used to carry —
    re-pinned against a goal that is genuinely swept (note AND terminal status)
    rather than one that is merely note-bearing.

    Note this passes for two independent reasons, and that is deliberate
    belt-and-braces: main() excludes non-pending/in-progress goals before
    _is_already_swept is ever consulted, AND the guard itself now requires a
    terminal status. Either alone would hold; pinning the observable outcome
    means a future refactor of either layer still has to keep it true.
    """
    pre_seeded_note = ("parent resolved without action needed "
                       "(parent_id=g-700-69, parent.status=skipped)")
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_with_pair(
            Path(tmpd),
            parent_status="skipped",
            unblock_outcome_note=pre_seeded_note,
            unblock_extra={"status": "skipped"})
        with DaemonFixture(world):
            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, f"sweep rc={rc}; stderr={err!r}"
            result = json.loads(out)
            assert result["applied"] == 0, (
                f"a fully-swept Unblock must not re-apply; got "
                f"applied={result['applied']}")
            assert len(result["candidates"]) == 0, (
                f"a fully-swept Unblock must not surface as candidate; got "
                f"{result['candidates']}")

            unblock = _read_goal(world, "g-700-73")
            assert unblock is not None
            assert unblock["status"] == "skipped"
            assert unblock["outcome_note"] == pre_seeded_note, (
                f"a fully-swept Unblock's outcome_note must not change; got "
                f"{unblock['outcome_note']!r}")


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


def test_provenance_guard_fires_through_main():
    """rb-3887 guard-FIRING branch through main() ().

    Fixture: discovered_by-only parent link (origin_signal carries no goal-id,
    title has no "for <g-id>" form) on an Unblock created AFTER its parent's
    completed_at, parent terminal. That is the provenance shape — the
    completed parent's audit DISCOVERED this work; the Unblock never waited
    on it. The guard must veto the sweep: candidates empty, applied=0, the
    details[] entry cites rb-3887, and the goal is untouched on disk
    (the g-115-2530/2531 auto-skip FP this guard exists to prevent).
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_with_pair(
            Path(tmpd),
            parent_status="completed",
            parent_extra={"completed_at": "2026-05-01T00:00:00"},
            unblock_extra={
                # No goal-id in origin_signal -> priority-1 no match
                "origin_signal": "unblock:stranded-forged-body",
                # No "for g-NNN-NN" in title -> priority-2 no match
                "title": "Unblock: commit+push stranded SKILL.md body "
                         "(registered but absent from git)",
                # Priority-3 provenance link
                "discovered_by": "g-700-69",
                # Created AFTER parent completion -> guard must fire
                "created_at": "2026-05-02T00:00:00",
            })
        with DaemonFixture(world):
            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, f"sweep rc={rc}; stderr={err!r}; stdout={out!r}"
            result = json.loads(out)
            assert result["applied"] == 0, (
                f"provenance-linked Unblock must NOT be swept; "
                f"got applied={result['applied']}")
            assert len(result["candidates"]) == 0, (
                f"guard-vetoed Unblock must not surface as candidate; "
                f"got {result['candidates']}")
            fired = [d for d in result.get("details", [])
                     if d.get("goal_id") == "g-700-73"
                     and "rb-3887" in (d.get("reason") or "")]
            assert fired, (
                f"details[] must record the rb-3887 guard veto for g-700-73; "
                f"details={result.get('details')}")

            # On-disk: goal untouched (status pending, no outcome_note)
            unblock = _read_goal(world, "g-700-73")
            assert unblock is not None
            assert unblock["status"] == "pending", (
                f"guard-vetoed Unblock must stay pending; got {unblock['status']!r}")
            assert not unblock.get("outcome_note"), (
                f"guard-vetoed Unblock must have no outcome_note; "
                f"got {unblock.get('outcome_note')!r}")


def test_untimestamped_unblock_reports_age_uncomputable_not_below_threshold():
    """An Unblock with NO parseable timestamp gets its own verdict, not a threshold one.

    g-115-4093 (from g-115-4084). The age gate used to read

        if age_h is None or age_h < args.max_age_hours:
            ... "reason": f"age {age_h} below threshold {args.max_age_hours}h"

    which rendered the literal string "age None below threshold 0.0h" — not even
    well-formed, let alone true. A reader scanning `reason` sees a threshold word
    and moves on, while the goal is structurally incapable of ever aging into
    eligibility because it has no timestamp at all.

    This is the POSITIVE CONTROL for that fix. The live queue currently yields
    zero uncomputable rows (created_at was backfilled in g-115-4084), so a live
    run cannot distinguish "the branch works" from "the branch is unreachable" —
    exactly the flattering zero the parent goal was about. This test constructs
    the case so the branch is proven reachable.

    Skipping stays correct (guard-420: no arithmetic on a null). Only the NAME
    was wrong. guard-2024.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # created_at=None is falsy, and no defer_reason_set_at / started exist,
        # so ref_ts resolves to None and _age_hours returns None.
        world, agent_dir = _make_world_with_pair(
            tmp, parent_status="skipped", unblock_extra={"created_at": None})
        with DaemonFixture(world):
            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, f"sweep failed rc={rc}: {err}"
            result = json.loads(out)

            row = next((d for d in result.get("details", [])
                        if d.get("goal_id") == "g-700-73"), None)
            assert row is not None, (
                f"the untimestamped Unblock must appear in details; "
                f"scanned={result.get('scanned')} eligible={result.get('eligible')} "
                f"applied={result.get('applied')} details={result.get('details')} "
                f"candidates={result.get('candidates')}")

            assert str(row.get("reason", "")).startswith("age_uncomputable"), (
                f"an undefined age must get its OWN verdict, not a threshold "
                f"one; got reason={row.get('reason')!r}")
            assert "below threshold" not in str(row.get("reason", "")), (
                f"reason must not claim a threshold comparison it never made; "
                f"got {row.get('reason')!r}")
            assert row.get("age_hours") is None, (
                f"age_hours must stay null, not be coerced; "
                f"got {row.get('age_hours')!r}")
            assert row.get("action") == "skipped"

            # Behavior unchanged: still skipped, nothing applied, goal untouched.
            assert result["applied"] == 0
            unblock = _read_goal(world, "g-700-73")
            assert unblock is not None and unblock["status"] == "pending", (
                "an uncomputable-age Unblock must be left alone, exactly as before")
