"""test_depends_on_consistency_daemon_parity.py — regression for .

`depends_on` and `blocked_by` are not synonyms. `blocked_by` is what SEQUENCES a
goal — goal-selector.py reads it and only it (zero occurrences of `depends_on` in
that file). `depends_on` is the output-passing annotation of goal-schemas.md
"Output-Passing Dependencies", a list of {goal_id, expects} dicts, and the
convention requires every one of those goal_ids to appear in `blocked_by` too.

A goal that violates the invariant carries an output-passing annotation with no
sequencing behind it: it LOOKS sequenced and is not, and nothing warns.
aspirations.py::validate_goal has enforced the rule since the field existed, but
the daemon `_validate_goal` subset omitted it, and under no-python-cli-fallback
the daemon IS the live write path — the same orphaning that produced the
prose-verification false-negatives (g-115-1440). Measured 2026-08-20 over 2771
live goal records: 6 non-empty depends_on carriers, exactly 1 conforming.

gates.depends_on_consistency is the single-source module imported by both the CLI
and the daemon, per guard-547.

Contracts pinned here:
  1. add_goal REJECTS depends_on whose goal_id is absent from blocked_by, and
     the goal does not land on disk.
  2. add_goal REJECTS bare-string depends_on (the shape that signals the field
     was reached for as a sequencing field).
  3. add_goal ACCEPTS a conforming {goal_id, expects} + matching blocked_by.
  4. add_goal ACCEPTS absent / empty depends_on (noop, both spellings).
  5. update_goal does NOT retroactively wedge a status change on a goal that
     already violates the invariant. This is the ADD-sites-only guarantee, and
     it is the reason the gate is not wired into _validate_goal — five live
     records violate it today.
  6. evaluate() returns the right verdict for block / pass / noop (direct unit).
  7. CLI aspirations.validate_goal still RAISES (delegation regression — the
     refactor from the inline copy preserved the contract).
  8. The refuted alternative stays refuted: the selector's `done_ids` is a SET,
     so unioning depends_on into its predicate raises TypeError on a dict-shaped
     entry rather than suppressing anything.

Pattern: DaemonFixture + direct HTTP POST (hermetic in-process daemon; NOT
daemon_integration-marked) for (1)-(5); direct import for (6)-(8). Mirrors
test_prose_verification_drift_daemon_parity.py.
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402
import aspirations  # noqa: E402
from gates.depends_on_consistency import evaluate as depends_on_evaluate  # noqa: E402


def _make_world(tmp: Path) -> tuple[Path, Path]:
    """Tempdir world with asp-100 holding  (a clean predecessor) and
    g-100-02, which ALREADY VIOLATES the invariant — the contract-5 target."""
    world = tmp / "world"
    world.mkdir()

    predecessor = {
        "id": "g-100-01",
        "title": "Predecessor goal",
        "description": "The goal whose output a dependent needs.",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    # Seeded directly into the file, bypassing the add path — exactly how the
    # five live violators got there (they predate the gate).
    legacy_violator = {
        "id": "g-100-02",
        "title": "Legacy goal carrying depends_on with no blocked_by",
        "description": "Pre-existing violator; must stay mutable.",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "depends_on": [{"goal_id": "g-100-01", "expects": "the output"}],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    asp = {
        "id": "asp-100",
        "title": "depends_on/blocked_by consistency daemon parity",
        "motivation": "Test daemon-path depends_on consistency rejection",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-01T00:00:00",
        "goals": [predecessor, legacy_violator],
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


def _add_goal(port: int, body: dict, agent: str = "alpha") -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/add-goal"
           "?asp_id=asp-100&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _update_goal(port: int, goal_id: str, field: str, value,
                 agent: str = "alpha") -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/update-goal"
           f"?id={goal_id}&field={field}&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(value).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _goal_count(world: Path) -> int:
    n = 0
    for line in (world / "aspirations.jsonl").read_text(
            encoding="utf-8").splitlines():
        if line.strip():
            n += len(json.loads(line).get("goals", []))
    return n


def _base_body(**over) -> dict:
    body = {
        "title": "Dependent goal",
        "description": "Needs the predecessor's output.",
        "priority": "MEDIUM",
        "status": "pending",
        "blocked_by": [],
        "verification": {"outcomes": [], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    body.update(over)
    return body


# --- (1) add_goal rejects depends_on not backed by blocked_by ---------------
def test_add_goal_rejects_depends_on_without_blocked_by():
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            before = _goal_count(world)
            body = _base_body(
                blocked_by=[],
                depends_on=[{"goal_id": "g-100-01", "expects": "the output"}],
            )
            status, out = _add_goal(df.port, body)
            assert status == 400, (
                "depends_on with no matching blocked_by must be rejected on the "
                f"daemon add path; got status={status} body={out!r}")
            assert "blocked_by" in out, (
                f"400 body must name blocked_by so the filer can fix it; got {out!r}")
            assert _goal_count(world) == before, (
                "rejected goal must not persist")


# --- (2) add_goal rejects the bare-string shape -----------------------------
def test_add_goal_rejects_bare_string_depends_on():
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            before = _goal_count(world)
            # 4 of the 6 live carriers use this shape. It cannot serve the
            # output-passing purpose (no `expects`) and does not sequence
            # anything either — the exact trap the gate exists to name.
            body = _base_body(blocked_by=["g-100-01"],
                              depends_on=["g-100-01"])
            status, out = _add_goal(df.port, body)
            assert status == 400, (
                "bare-string depends_on must be rejected even when blocked_by "
                f"names the same id; got status={status} body={out!r}")
            assert _goal_count(world) == before


# --- (3) add_goal accepts a conforming record -------------------------------
def test_add_goal_accepts_conforming_depends_on():
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            before = _goal_count(world)
            body = _base_body(
                blocked_by=["g-100-01"],
                depends_on=[{"goal_id": "g-100-01", "expects": "the output"}],
            )
            status, out = _add_goal(df.port, body)
            assert status == 200, (
                f"conforming depends_on must be accepted; got {status} {out!r}")
            assert _goal_count(world) == before + 1


# --- (4) absent / empty depends_on are noops --------------------------------
@pytest.mark.parametrize("over", [{}, {"depends_on": []}])
def test_add_goal_accepts_absent_or_empty_depends_on(over):
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            before = _goal_count(world)
            status, out = _add_goal(df.port, _base_body(**over))
            assert status == 200, (
                f"depends_on {over!r} must be a noop; got {status} {out!r}")
            assert _goal_count(world) == before + 1


# --- (5) THE ADD-SITES-ONLY GUARANTEE ---------------------------------------
def test_update_goal_does_not_wedge_a_legacy_violator():
    """A pre-existing violator must stay mutable.

    This is the contract that decided the wiring. update_goal validates its
    in-lock candidate through _validate_goal; had the gate been placed there,
    every status change — claim, in-progress, complete — on the five live
    violating records would start returning 400. A filing-time gate that
    freezes existing work is worse than the drift it prevents.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, out = _update_goal(df.port, "g-100-02", "status",
                                       "in-progress")
            assert status == 200, (
                "a legacy depends_on violator must remain mutable; the gate is "
                f"wired at ADD sites only. got {status} {out!r}")


# --- (6) direct unit: block / pass / noop -----------------------------------
def test_evaluate_verdicts():
    blocked = depends_on_evaluate({
        "id": "g-100-09",
        "blocked_by": [],
        "depends_on": [{"goal_id": "g-100-08", "expects": "out"}],
    })
    assert blocked["would_block"] is True
    assert blocked["decision"] == "block"
    assert "not_in_blocked_by" in blocked["violations"]

    wrong_shape = depends_on_evaluate({
        "id": "g-100-09",
        "blocked_by": ["g-100-08"],
        "depends_on": ["g-100-08"],
    })
    assert wrong_shape["would_block"] is True
    assert "wrong_shape" in wrong_shape["violations"]

    passing = depends_on_evaluate({
        "id": "g-100-09",
        "blocked_by": ["g-100-08"],
        "depends_on": [{"goal_id": "g-100-08", "expects": "out"}],
    })
    assert passing["would_block"] is False
    assert passing["decision"] == "pass"

    for goal in ({"id": "g-100-09"},
                 {"id": "g-100-09", "depends_on": []},
                 {"id": "g-100-09", "depends_on": None}):
        v = depends_on_evaluate(goal)
        assert v["decision"] == "noop", f"{goal!r} should be a noop, got {v}"
        assert v["would_block"] is False


def test_evaluate_tolerates_bare_string_blocked_by():
    """A legacy string-shaped blocked_by must be WRAPPED, not iterated.

    Iterating the string would make membership test per-character, so a
    single-character id would pass the gate by accident.
    """
    v = depends_on_evaluate({
        "id": "g-100-09",
        "blocked_by": "g-100-08",
        "depends_on": [{"goal_id": "g-100-08", "expects": "out"}],
    })
    assert v["decision"] == "pass", f"string blocked_by should wrap; got {v}"


def test_evaluate_rejects_non_list_depends_on():
    v = depends_on_evaluate({"id": "g-100-09", "depends_on": "g-100-08"})
    assert v["would_block"] is True
    assert "not_a_list" in v["violations"]


# --- (7) CLI delegation regression ------------------------------------------
def test_cli_validate_goal_still_raises():
    goal = {
        "id": "g-100-09",
        "title": "t",
        "status": "pending",
        "blocked_by": [],
        "depends_on": [{"goal_id": "g-100-08", "expects": "out"}],
    }
    with pytest.raises(ValueError, match="blocked_by"):
        aspirations.validate_goal(goal)


# --- (8) the refuted alternative stays refuted ------------------------------
def test_selector_done_ids_is_a_set_so_dict_deps_would_crash():
    """Pins WHY 'make the selector honour depends_on' was rejected.

    goal-selector.py's predicate is
        [bid for bid in _ensure_list(goal.get("blocked_by")) if bid not in done_ids]
    and done_ids is built as a SET comprehension. A {goal_id, expects} dict is
    unhashable, so unioning the two fields raises TypeError on the first
    dict-shaped carrier the selector scores — crashing the fleet's mandatory
    selection entry point rather than suppressing anything. If a future change
    makes done_ids a list, this test goes green for the wrong reason; read the
    assertion message before deleting it.
    """
    done_ids = {"g-100-08", "g-100-07"}
    dep_entry = {"goal_id": "g-100-08", "expects": "out"}
    with pytest.raises(TypeError, match="unhashable"):
        _ = dep_entry not in done_ids
