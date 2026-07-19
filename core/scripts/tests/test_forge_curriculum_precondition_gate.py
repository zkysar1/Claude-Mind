"""test_forge_curriculum_precondition_gate.py — regression for 4.

The add_goal pipeline must GUARANTEE that every /forge-skill goal carries the
`pc-curriculum-forge` structured precondition, so the goal-selector's
per-executor precondition check (predicate.command_succeeds inherits the
selecting agent's MIND_AGENT) gates the TARGET agent — closing the filing-site
gap where the four forge-filing sites (aspirations-evolve Step 9,
aspirations-spark Phase 6.5, respond, reflect-on-outcome) curriculum-checked only
the FILING agent, never the routed TARGET (g-315-383 was routed to echo at cur-01,
which cannot forge).

Contracts pinned here (all via the real wired Phase D.5 mutator in
mind_api/src/endpoints/aspirations_write.py::_run_add_goal_pipeline):
  1. A forge goal detected by skill="/forge-skill" gets pc-curriculum-forge.
  2. A forge goal detected by the "Forge skill:" title prefix gets it.
  3. A forge goal detected by the "idea:forge-ready-" origin_signal gets it.
  4. A forge goal that ALREADY carries the precondition is NOT double-attached.
  5. A non-forge goal does NOT get the precondition.

Pattern: DaemonFixture + direct HTTP POST (bash-free, hits the patched endpoint
via the fixture's in-process daemon). Mirrors test_add_goal_filed_by_agent_stamp.py.
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


def _make_world(tmp: Path) -> Path:
    """Tempdir world with asp-200 holding one existing goal."""
    world = tmp / "world"
    world.mkdir()
    seed = {
        "id": "g-200-01",
        "title": "Seed goal",
        "description": "Pre-existing goal",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    asp = {
        "id": "asp-200",
        "title": "forge-curriculum precondition gate regression",
        "motivation": "Test forge-curriculum precondition guarantee",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-07-17T00:00:00",
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
    return world


def _add_goal(port: int, body: dict, agent: str = "alpha") -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/add-goal"
           "?asp_id=asp-200&source=world")
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


def _find_goal_by_title(world: Path, title: str) -> dict | None:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("title") == title:
                return g
    return None


def _forge_pcs(goal: dict) -> list:
    """All structured preconditions gating allow_forge_skill on a goal."""
    pcs = (goal.get("verification") or {}).get("preconditions") or []
    out = []
    for p in pcs:
        if isinstance(p, dict) and (
            p.get("id") == "pc-curriculum-forge"
            or "allow_forge_skill" in str(p.get("command") or "")
        ):
            out.append(p)
    return out


def test_forge_goal_by_skill_gets_precondition():
    """skill='/forge-skill' → pc-curriculum-forge auto-attached."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Build the widget forge (skill-detected)",
                "description": "Forge goal detected purely by skill field",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "skill": "/forge-skill",
                "verification": {"outcomes": ["x"], "checks": [],
                                 "preconditions": []},
                "origin_signal": "user_directive",
                "participants": ["agent"],
                "intended_agent": "echo",
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(world, "Build the widget forge (skill-detected)")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            pcs = _forge_pcs(g)
            assert len(pcs) == 1, (
                "forge goal (skill=/forge-skill) must carry exactly one "
                f"pc-curriculum-forge precondition; got {pcs!r}")
            assert pcs[0]["type"] == "command_succeeds"
            assert "allow_forge_skill" in pcs[0]["command"]


def test_forge_goal_by_title_prefix_gets_precondition():
    """title 'Forge skill: ...' (no skill field) → precondition auto-attached."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Forge skill: widget-maker",
                "description": "Forge goal detected purely by title prefix",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "verification": {"outcomes": ["x"], "checks": [],
                                 "preconditions": []},
                "origin_signal": "user_directive",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(world, "Forge skill: widget-maker")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert len(_forge_pcs(g)) == 1, (
                "forge goal (title 'Forge skill:') must carry the "
                "pc-curriculum-forge precondition")


def test_forge_goal_by_origin_signal_gets_precondition():
    """origin_signal 'idea:forge-ready-...' → precondition auto-attached."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Make a skill from a recurring procedure",
                "description": "Forge goal detected purely by origin_signal",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "verification": {"outcomes": ["x"], "checks": [],
                                 "preconditions": []},
                "origin_signal": "idea:forge-ready-gap-100",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(world, "Make a skill from a recurring procedure")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert len(_forge_pcs(g)) == 1, (
                "forge goal (origin_signal 'idea:forge-ready-') must carry the "
                "pc-curriculum-forge precondition")


def test_existing_forge_precondition_not_duplicated():
    """A caller-supplied pc-curriculum-forge is respected, not double-attached."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Forge skill: already-gated",
                "description": "Forge goal that already carries the precondition",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "skill": "/forge-skill",
                "verification": {
                    "outcomes": ["x"], "checks": [],
                    "preconditions": [{
                        "id": "pc-curriculum-forge",
                        "type": "command_succeeds",
                        "command": ("bash core/scripts/curriculum-contract-check.sh "
                                    "--action allow_forge_skill"),
                        "timeout_seconds": 30,
                        "description": "caller-supplied",
                    }],
                },
                "origin_signal": "idea:forge-ready-gap-101",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(world, "Forge skill: already-gated")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            pcs = _forge_pcs(g)
            assert len(pcs) == 1, (
                "a caller-supplied pc-curriculum-forge must NOT be duplicated; "
                f"got {len(pcs)} matching preconditions: {pcs!r}")
            assert pcs[0]["description"] == "caller-supplied", (
                "the caller's original precondition must be preserved verbatim")


def test_non_forge_goal_no_precondition():
    """An ordinary goal does NOT get a forge-curriculum precondition."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = {
                "title": "Fix the retry backoff in deploy.sh",
                "description": "Ordinary non-forge goal",
                "priority": "MEDIUM",
                "status": "pending",
                "blocked_by": [],
                "verification": {"outcomes": ["x"], "checks": [],
                                 "preconditions": []},
                "origin_signal": "user_directive",
                "participants": ["agent"],
            }
            status, out = _add_goal(df.port, body)
            assert status == 200, f"add-goal status={status}; body={out!r}"
            g = _find_goal_by_title(world, "Fix the retry backoff in deploy.sh")
            assert g is not None, f"goal not found on disk; resp={out!r}"
            assert _forge_pcs(g) == [], (
                "non-forge goal must NOT receive a pc-curriculum-forge "
                f"precondition; got {_forge_pcs(g)!r}")


if __name__ == "__main__":
    test_forge_goal_by_skill_gets_precondition()
    test_forge_goal_by_title_prefix_gets_precondition()
    test_forge_goal_by_origin_signal_gets_precondition()
    test_existing_forge_precondition_not_duplicated()
    test_non_forge_goal_no_precondition()
    print("ok")
