"""test_add_aspiration_embedded_goal_stamps.py — regression for the
full-aspiration add() endpoint's embedded-goal stamping gap (g-115-6599).

Bug shape, and it is the MIRROR of test_add_goal_blocked_since_stamp.py: that
test pins a field add_goal() was missing and add() had. This pins the three
fields add() was missing and add_goal() had. aspirations_write.add()
(POST /v1/aspirations/add — a whole aspiration with embedded goals) ran a
per-goal loop that stamped ONLY blocked_since, so every goal born inside an
aspiration-creation call landed with NO created_at, NO alloc_nonce and NO
filed_by_agent — while any goal appended to that same aspiration later, through
add_goal(), got all three.

HOW IT WAS ISOLATED, because the id ordering is the whole proof: the unstamped
goals are always the LOWEST-numbered ones in their aspiration. asp-369's
g-369-03/04/08/09 lack all three fields; g-369-14 onward carry all three.
asp-326 has the identical shape across 126 goals — only g-326-01 is bare.

IT IS NOT A USER-DIRECTIVE DEFECT, which is how it was originally framed and
filed. goal_source correlated (13 of the first 14 instances were
goal_source=user) only because user-supplied plans arrive as a whole aspiration
while agent work appends goals one at a time. The counterexamples settle it in
both directions: g-326-01 is goal_source=agent-self and UNSTAMPED; g-369-14 is
goal_source=user and STAMPED. The write path discriminates, the source does not
— which also answers the "is there a second producer?" question that was left
open on the goal record: there is one producer, and it is add().

WHY created_at IS THE LOAD-BEARING ONE: it is the age input to
apply_starvation_boost, which fail-opens to NO boost when the timestamp is
missing, so an unstamped goal can never be starvation-rescued. Confirmed on a
live victim rather than argued — g-326-01 sat 1,878h (78 days) at the tail of a
working agent's queue, roughly 2.4x older than anything else in it. The second
consumer is user-facing: user-blocker-escalation-check renders an ageless goal
into the user's blocked-goals digest with its age shown as "unknown".

Pattern: DaemonFixture + direct HTTP POST to /v1/aspirations/add (bash-free,
hits the patched endpoint via the fixture's in-process daemon), matching the
sibling blocked_since test.
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

STAMPED_FIELDS = ("created_at", "alloc_nonce", "filed_by_agent")


def _make_world(tmp: Path) -> Path:
    """Empty tempdir world — this endpoint CREATES the aspiration."""
    world = tmp / "world"
    world.mkdir()
    (world / "aspirations.jsonl").write_text("", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _add_aspiration(port: int, body: dict) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}/v1/aspirations/add?source=world"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", "alpha")
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _goals_on_disk(world: Path) -> list:
    out = []
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip():
            out.extend(json.loads(line).get("goals", []))
    return out


def _payload(goals: list) -> dict:
    return {
        "title": "Embedded-goal stamping regression",
        "motivation": "Pin created_at/alloc_nonce/filed_by_agent parity",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "goals": goals,
    }


def _goal(title: str, **extra) -> dict:
    g = {
        "title": title,
        "description": "Embedded at aspiration-creation time",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    g.update(extra)
    return g


def test_embedded_goals_are_stamped_at_aspiration_creation():
    """Every goal embedded in an add() payload must carry all three fields."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, out = _add_aspiration(
                df.port, _payload([_goal("Phase A3"), _goal("Phase A4")]))
            assert status == 200, f"add status={status}; body={out!r}"
            goals = _goals_on_disk(world)
            assert len(goals) == 2, f"expected 2 embedded goals, got {goals!r}"
            for g in goals:
                for field in STAMPED_FIELDS:
                    assert g.get(field), (
                        f"embedded goal {g.get('id')!r} ({g.get('title')!r}) is "
                        f"missing {field} — the add() per-goal loop must stamp "
                        f"the same fields add_goal() does. Got: "
                        f"{ {k: g.get(k) for k in STAMPED_FIELDS} }")


def test_embedded_goal_alloc_nonces_are_distinct():
    """alloc_nonce is an identity stamp — two goals must not share one.

    A single uuid4() hoisted out of the loop would satisfy the presence test
    above while giving every embedded goal the SAME identity, which is worse
    than absence: coordination_merge keys goal identity on alloc_nonce, so two
    distinct goals would merge into one.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, out = _add_aspiration(
                df.port,
                _payload([_goal("Phase A3"), _goal("Phase A4"),
                          _goal("Phase C1")]))
            assert status == 200, f"add status={status}; body={out!r}"
            nonces = [g.get("alloc_nonce") for g in _goals_on_disk(world)]
            assert len(nonces) == 3, f"expected 3 goals, got {nonces!r}"
            assert len(set(nonces)) == 3, (
                f"alloc_nonce must be unique per goal, got {nonces!r} — a uuid4() "
                "hoisted out of the per-goal loop collapses goal identity")


def test_explicit_caller_values_are_preserved():
    """setdefault semantics: a caller-supplied value wins (parity with add_goal).

    A migrated or replayed plan may legitimately carry its own created_at, and
    a goal may be filed on behalf of another agent. Overwriting either would
    destroy real provenance, so the fix must not use unconditional assignment.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, out = _add_aspiration(df.port, _payload([
                _goal("Carries its own provenance",
                      created_at="2026-01-02T03:04:05",
                      filed_by_agent="echo"),
            ]))
            assert status == 200, f"add status={status}; body={out!r}"
            g = _goals_on_disk(world)[0]
            assert g.get("created_at") == "2026-01-02T03:04:05", (
                f"caller created_at must be preserved, got {g.get('created_at')!r}")
            assert g.get("filed_by_agent") == "echo", (
                f"caller filed_by_agent must be preserved, got "
                f"{g.get('filed_by_agent')!r}")


def test_blocked_since_still_stamped():
    """Negative-control on the pre-existing behaviour this loop already had.

    The fix extends the same loop; this pins that it did not disturb the one
    thing the loop was already doing.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, out = _add_aspiration(df.port, _payload([
                _goal("Independent"),
                _goal("Dependent", blocked_by=["g-001-01"]),
            ]))
            assert status == 200, f"add status={status}; body={out!r}"
            goals = {g.get("title"): g for g in _goals_on_disk(world)}
            assert goals["Dependent"].get("blocked_since"), (
                "blocked_since must still be stamped when blocked_by is present")
            assert not goals["Independent"].get("blocked_since"), (
                "blocked_since must stay unset when blocked_by is empty; got "
                f"{goals['Independent'].get('blocked_since')!r}")


if __name__ == "__main__":
    test_embedded_goals_are_stamped_at_aspiration_creation()
    test_embedded_goal_alloc_nonces_are_distinct()
    test_explicit_caller_values_are_preserved()
    test_blocked_since_still_stamped()
    print("ok")
