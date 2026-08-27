"""test_prose_verification_drift_daemon_parity.py — regression for .

The prose-verification-drift gate (a goal whose description carries the
"Verification outcomes:" / "Verification checks:" prose headers but has an
empty/absent structured verification.checks) must be rejected on the DAEMON
write paths, not just the CLI. The g-115-1440 investigation found the daemon
_validate_goal subset omitted the check entirely, so daemon-path adds/edits
bypassed it — a realized false-negative (g-315-119 completed, g-316-08 pending).

gates.prose_verification is the single-source module imported by both the CLI
(aspirations.py::validate_goal) and the daemon (aspirations_write.py), per
guard-547 (validation that must hold on both paths lives in a shared module,
not duplicated).

Contracts pinned here:
  1. add_goal REJECTS a prose-only goal (markers, empty checks) with 400.
  2. add_goal ACCEPTS markers + a non-empty structured checks list (pass).
  3. add_goal ACCEPTS a goal with no markers (noop).
  4. update_goal REJECTS a description edit that injects the markers onto a
     goal whose verification.checks is empty (post-add prose injection).
  5. gates.prose_verification.evaluate returns the right verdict for
     block / pass / noop (direct unit).
  6. CLI aspirations.validate_goal still RAISES on prose-only drift
     (delegation regression — the refactor preserved the contract).

Pattern: DaemonFixture + direct HTTP POST (hermetic in-process daemon; NOT
daemon_integration-marked) for (1)-(4); direct import for (5)/(6). Mirrors
test_add_goal_filed_by_agent_stamp.py.
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402
import aspirations  # noqa: E402
from gates.prose_verification import evaluate as prose_evaluate  # noqa: E402

# A description that trips the gate when verification.checks is empty.
_PROSE_DESC = (
    "Do the thing end to end. Verification outcomes: the thing works; "
    "the other thing also works. Verification checks: run the suite."
)


def _make_world(tmp: Path) -> tuple[Path, Path]:
    """Tempdir world with asp-100 holding one existing goal 
    (no prose markers, empty checks — the update-injection target)."""
    world = tmp / "world"
    world.mkdir()

    seed = {
        "id": "g-100-01",
        "title": "Seed goal",
        "description": "Pre-existing goal with no verification prose.",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    asp = {
        "id": "asp-100",
        "title": "prose-verification-drift daemon parity",
        "motivation": "Test daemon-path prose-verification-drift rejection",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-01T00:00:00",
        "goals": [seed],
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
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    n = 0
    for line in text.splitlines():
        if line.strip():
            n += len(json.loads(line).get("goals", []))
    return n


# --- (1) add_goal rejects prose-only ----------------------------------------
def test_add_goal_rejects_prose_only():
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            before = _goal_count(world)
            body = {
                "title": "Prose-only goal",
                "description": _PROSE_DESC,
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "verification": {"outcomes": [], "checks": [],
                                 "preconditions": []},
                "origin_signal": "user_directive",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body)
            assert status == 400, (
                f"prose-only goal must be rejected on the daemon add path; "
                f"got status={status} body={out!r}")
            assert "verification" in out.lower() or "prose" in out.lower(), (
                f"400 body should explain the prose-drift; got {out!r}")
            # And it must NOT have landed on disk.
            assert _goal_count(world) == before, (
                "rejected prose-only goal must not persist")


# --- (2) add_goal accepts markers + structured checks (pass) -----------------
def test_add_goal_accepts_markers_with_checks():
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Markers with structured checks",
                "description": _PROSE_DESC,
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                # A REAL predicate type, not a placeholder. This fixture used
                # {"type": "test"} until  added gates.check_schema to
                # the same ADD path, which refuses a type predicate.py cannot
                # dispatch — so the placeholder started failing an assertion
                # that has nothing to do with it. The assertion below is
                # unchanged; only the fixture moved to a shape a real filing
                # could actually have (guard-920). goal_completed_after is used
                # because it needs no filesystem, so it behaves identically in
                # this test's temporary world.
                "verification": {"outcomes": ["works"],
                                 "checks": [{"type": "goal_completed_after",
                                             "goal_id": "g-100-01",
                                             "after_ref": "iso:2030-01-01T00:00:00"}],
                                 "preconditions": []},
                "origin_signal": "user_directive",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, (
                f"markers + non-empty checks must pass; got {status} {out!r}")


# --- (3) add_goal accepts no markers (noop) ----------------------------------
def test_add_goal_accepts_no_markers():
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Plain goal",
                "description": "Do the thing. No verification prose headers.",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "verification": {"outcomes": [], "checks": [],
                                 "preconditions": []},
                "origin_signal": "user_directive",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, (
                f"no-marker goal must pass even with empty checks; "
                f"got {status} {out!r}")


# --- (4) update_goal rejects description prose injection ---------------------
def test_update_goal_description_rejects_prose_injection():
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            #  has empty checks; injecting the markers must be rejected.
            status, out = _update_goal(df.port, "g-100-01", "description",
                                       _PROSE_DESC)
            assert status == 400, (
                f"description edit injecting prose markers (no structured "
                f"checks) must be rejected; got status={status} body={out!r}")
            # The on-disk description must be unchanged.
            text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
            asp = json.loads(text.splitlines()[0])
            g = asp["goals"][0]
            assert "Verification outcomes:" not in (g.get("description") or ""), (
                "rejected description edit must not persist")


# --- (5) shared gate direct unit ---------------------------------------------
def test_shared_gate_evaluate_verdicts():
    block = prose_evaluate({"id": "g-1-1", "description": _PROSE_DESC,
                            "verification": {}})
    assert block["would_block"] and block["decision"] == "block"

    passed = prose_evaluate({"id": "g-1-2", "description": _PROSE_DESC,
                             "verification": {"checks": [{"type": "test"}]}})
    assert (not passed["would_block"]) and passed["decision"] == "pass"

    noop = prose_evaluate({"id": "g-1-3", "description": "no markers here",
                           "verification": {}})
    assert (not noop["would_block"]) and noop["decision"] == "noop"


# --- (6) CLI delegation regression -------------------------------------------
def test_cli_validate_goal_raises_on_prose_drift():
    goal = {
        "id": "g-100-09",
        "title": "cli prose drift",
        "status": "pending",
        "description": _PROSE_DESC,
        "verification": {"outcomes": [], "checks": []},
    }
    raised = False
    try:
        aspirations.validate_goal(goal)
    except ValueError as e:
        raised = True
        assert "verification" in str(e).lower(), f"wrong error: {e}"
    assert raised, "CLI validate_goal must still raise on prose-only drift"

    # And a goal with structured checks must NOT raise.
    ok_goal = {**goal, "id": "g-100-10",
               "verification": {"outcomes": ["x"],
                                "checks": [{"type": "test"}]}}
    aspirations.validate_goal(ok_goal)


# --- (7) acceptance/success SYNONYM headers () ----------------------
# NOTE: this file contains the marker strings as literals, which is safe — the
# gate scans goal DESCRIPTIONS, never source files, so there is no self-match
# surface here (cf. guard-1668, which applies to scanners that read their own
# source).
_SYNONYM_DESC = "Widen the thing.\n\nAcceptance: the widened gate blocks the drifted case."


def test_synonym_marker_blocks_empty_verification():
    """A synonym header with NOTHING structured is the drift this goal targets."""
    v = prose_evaluate({"id": "g-2-1", "description": _SYNONYM_DESC,
                        "verification": {}})
    assert v["would_block"] and v["decision"] == "block", v
    assert "Acceptance:" in v["markers_seen"], v["markers_seen"]


def test_synonym_marker_passes_with_outcomes_only():
    """SAME prose, populated outcomes and NO checks -> pass.

    Measured on the live corpus (2026-08-25): 3 of the 7 goals the synonym set
    newly admits (g-368-01, g-368-02, g-306-126) carry outcomes and no checks.
    Demanding checks for synonym headers would block those legitimate backlog
    goals, so outcomes OR checks satisfies the synonym class.
    """
    v = prose_evaluate({"id": "g-2-2", "description": _SYNONYM_DESC,
                        "verification": {"outcomes": ["the gate blocks it"]}})
    assert (not v["would_block"]) and v["decision"] == "pass", v


def test_original_markers_still_require_checks():
    """Backward compat: outcomes alone must NOT satisfy an ORIGINAL marker.

    Widening the synonym class must not silently relax the pre-existing
    contract goal-schemas.md promises (guard-3351 — narrowing a gate is the
    quiet direction, so it gets an explicit test rather than an assumption).
    """
    v = prose_evaluate({"id": "g-2-3", "description": _PROSE_DESC,
                        "verification": {"outcomes": ["x"]}})
    assert v["would_block"] and v["decision"] == "block", v


def test_marker_quoted_in_code_region_does_not_fire():
    """A description that merely QUOTES a marker must stay a no-op."""
    fenced = {"id": "g-2-4",
              "description": "example:\n```\nAcceptance: quoted\n```\n",
              "verification": {}}
    assert prose_evaluate(fenced)["decision"] == "noop", prose_evaluate(fenced)

    inline = {"id": "g-2-5",
              "description": "the `Acceptance:` header is what we match",
              "verification": {}}
    assert prose_evaluate(inline)["decision"] == "noop", prose_evaluate(inline)

    # Positive control for the strip: the SAME marker outside a code region
    # must still fire, or the test above would pass on a gate that never fires.
    live = {"id": "g-2-6", "description": "Acceptance: real header",
            "verification": {}}
    assert prose_evaluate(live)["decision"] == "block", prose_evaluate(live)


def test_bare_success_is_deliberately_not_a_marker():
    """`Success:` was measured as a false positive and must stay excluded.

    g-115-7522 quotes a load-test log line (`Total: 120, Success: 120 (200)`).
    Bare high-frequency English words do not survive pasted tool output.
    """
    v = prose_evaluate({"id": "g-2-7",
                        "description": "Total: 120, Success: 120 (200), Failed: 0",
                        "verification": {}})
    assert v["decision"] == "noop", v


def test_marker_set_is_wider_than_the_original_two():
    """The union the gate matches on must be strictly wider than the original 2."""
    from gates.prose_verification import (  # noqa: E402
        ALL_PROSE_MARKERS, PROSE_ACCEPTANCE_SYNONYM_MARKERS,
        PROSE_VERIFICATION_MARKERS,
    )
    # HARDCODED, not derived (guard-1628): the marker set IS the thing under
    # protection here, so a test that only asserts len(ALL_PROSE_MARKERS) > 2
    # would still pass if someone replaced all seven synonyms with junk -- it
    # moves with the mutation and can never fail. Ask the guard's question: if
    # this constant were the thing that broke, would this check still pass?
    # With the expected strings written out, no. Editing the set must now be a
    # deliberate act that updates this list too.
    assert PROSE_VERIFICATION_MARKERS == (
        "Verification outcomes:", "Verification checks:",
    ), "the original pair must not move -- goal-schemas.md promises compatibility"
    assert set(PROSE_ACCEPTANCE_SYNONYM_MARKERS) == {
        "Acceptance criteria:", "Acceptance:", "Success criteria:", "Done when:",
        "Definition of done:", "Completion criteria:", "Exit criteria:",
    }, "synonym set changed -- update this expectation deliberately, and re-measure"
    assert set(PROSE_VERIFICATION_MARKERS) < set(ALL_PROSE_MARKERS)
    assert "Success:" not in ALL_PROSE_MARKERS, "bare Success: is a measured FP"
    assert set(PROSE_ACCEPTANCE_SYNONYM_MARKERS).isdisjoint(
        PROSE_VERIFICATION_MARKERS), "the two classes must not overlap"


if __name__ == "__main__":
    test_add_goal_rejects_prose_only()
    test_add_goal_accepts_markers_with_checks()
    test_add_goal_accepts_no_markers()
    test_update_goal_description_rejects_prose_injection()
    test_shared_gate_evaluate_verdicts()
    test_cli_validate_goal_raises_on_prose_drift()
    test_synonym_marker_blocks_empty_verification()
    test_synonym_marker_passes_with_outcomes_only()
    test_original_markers_still_require_checks()
    test_marker_quoted_in_code_region_does_not_fire()
    test_bare_success_is_deliberately_not_a_marker()
    test_marker_set_is_wider_than_the_original_two()
    print("ok")
