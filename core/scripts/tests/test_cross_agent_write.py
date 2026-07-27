"""test_cross_agent_write.py — regression for  (option A per bravo
decision msg-20260709-021804-bravo-118).

Pins the cross-agent write-back MECHANISM + the option-A enforcement HELPER:

  1. MECHANISM (the g-115-1848 flag-flip prerequisite): an agent-source write
     carrying `X-Mind-Agent: <owner>` lands in <owner>'s aspirations.jsonl and
     NOT in the actor's — proving the daemon routes source=agent by the header
     (mind_api/src/server.py:180 -> resolver.resolve -> ctx.paths.agent ->
     endpoints/aspirations.py _resolve_paths). This is what makes the fragile
     env-prefix path safe to rely on and is the precondition for ever flipping
     cross_agent_surfacing.enabled=true.

  2. HELPER e2e (core/scripts/cross-agent-write.sh): invoked with the OWNER as a
     data argument while the ambient MIND_AGENT is the ACTOR, the helper's write
     still lands in the OWNER's queue — i.e. the helper applies the prefix so the
     orchestrator can no longer forget it.

  3. HELPER enforcement: an identity/liveness-exempt script (board-post.sh) is
     REFUSED (exit 2) so it can never be accidentally swapped to the owner.

Seeds TWO tmp agent queues (owner=bravo, actor=alpha) so "landed in the right
queue" is a positive AND a negative assertion.

Pattern: DaemonFixture (in-process daemon over a temp world) + a manually-seeded
second agent dir + direct HTTP POST for (1); subprocess through the real helper
for (2)/(3). Mirrors test_add_goal_filed_by_agent_stamp.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402

HELPER = CORE_SCRIPTS / "cross-agent-write.sh"


def _seed_agent_queue(agent_dir: Path, asp_id: str = "asp-001") -> None:
    """Give an agent dir an empty-but-valid aspirations.jsonl holding one
    aspiration with one seed goal, plus the archive file the endpoints expect."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "session").mkdir(exist_ok=True)
    seed_goal = {
        "id": f"{asp_id.replace('asp', 'g')}-01",
        "title": f"Seed goal in {agent_dir.name}",
        "description": "pre-existing",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    asp = {
        "id": asp_id,
        "title": f"{agent_dir.name} queue",
        "motivation": "cross-agent write regression",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-07-09T00:00:00",
        "goals": [seed_goal],
    }
    (agent_dir / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")


def _seed_second_agent(project_root: Path, world: Path, name: str) -> Path:
    """DaemonFixture.make_project_root seeds only ONE agent. Add a second agent
    dir with its own local-paths.conf (pointing at the SAME shared world+meta)
    so the daemon's resolver.resolve(<name>) can resolve it."""
    agent_dir = project_root / "agents" / name
    _seed_agent_queue(agent_dir)
    meta = project_root / "meta"
    (agent_dir / "local-paths.conf").write_text(
        f"WORLD_PATH={world.as_posix()}\nMETA_PATH={meta.as_posix()}\n",
        encoding="utf-8",
    )
    return agent_dir


def _goals_in(agent_dir: Path) -> list[dict]:
    text = (agent_dir / "aspirations.jsonl").read_text(encoding="utf-8")
    out: list[dict] = []
    for line in text.splitlines():
        if line.strip():
            out.extend(json.loads(line).get("goals", []))
    return out


def _add_goal(port: int, title: str, agent: str) -> tuple[int, str]:
    """POST a single goal to <owner>'s asp-001 via source=agent + X-Mind-Agent."""
    url = (f"http://127.0.0.1:{port}/v1/aspirations/add-goal"
           "?asp_id=asp-001&source=agent")
    body = {
        "title": title,
        "description": "cross-agent routed write",
        "priority": "MEDIUM",
        "status": "pending",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
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


def test_agent_source_write_routes_to_header_owner_queue():
    """MECHANISM: source=agent + X-Mind-Agent:bravo lands in bravo's queue,
    NOT the actor alpha's queue. (g-115-1848 flag-flip prerequisite.)"""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        (world / "aspirations.jsonl").write_text("", encoding="utf-8")
        (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

        # DaemonFixture seeds actor 'alpha'; give alpha a real queue too so the
        # negative assertion is meaningful, and seed owner 'bravo'.
        with DaemonFixture(world, agent="alpha") as df:
            _seed_agent_queue(df.project_root / "agents" / "alpha")
            bravo_dir = _seed_second_agent(df.project_root, world, "bravo")
            alpha_dir = df.project_root / "agents" / "alpha"

            status, out = _add_goal(df.port, "Routed to bravo", agent="bravo")
            assert status == 200, f"add-goal status={status}; body={out!r}"

            bravo_titles = [g.get("title") for g in _goals_in(bravo_dir)]
            alpha_titles = [g.get("title") for g in _goals_in(alpha_dir)]
            assert "Routed to bravo" in bravo_titles, (
                "agent-source write with X-Mind-Agent:bravo must land in "
                f"bravo's queue; bravo goals={bravo_titles!r}")
            assert "Routed to bravo" not in alpha_titles, (
                "write must NOT leak into the actor alpha's queue; "
                f"alpha goals={alpha_titles!r}")


def test_helper_applies_owner_prefix_over_ambient_actor():
    """HELPER e2e: cross-agent-write.sh bravo <write> — while ambient
    MIND_AGENT=alpha — routes the write to bravo's queue."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        (world / "aspirations.jsonl").write_text("", encoding="utf-8")
        (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

        with DaemonFixture(world, agent="alpha") as df:
            _seed_agent_queue(df.project_root / "agents" / "alpha")
            bravo_dir = _seed_second_agent(df.project_root, world, "bravo")
            alpha_dir = df.project_root / "agents" / "alpha"

            # Ambient identity is the ACTOR alpha; the helper gets OWNER bravo as
            # a data arg. RT_DIR points the shell wrapper at the test daemon.
            env = dict(os.environ)
            env["MIND_AGENT"] = "alpha"
            env["RT_DIR"] = str(df.runtime_dir)
            proc = subprocess.run(
                [BASH, str(HELPER), "bravo", "aspirations-update-goal.sh",
                 "--source", "agent", "g-001-01", "priority", "HIGH"],
                capture_output=True, text=True, env=env, timeout=60,
                cwd=str(PROJECT_ROOT),
            )
            assert proc.returncode == 0, (
                f"helper rc={proc.returncode}; stderr={proc.stderr!r}")

            bravo_goal = next((g for g in _goals_in(bravo_dir)
                               if g.get("id") == "g-001-01"), None)
            alpha_goal = next((g for g in _goals_in(alpha_dir)
                               if g.get("id") == "g-001-01"), None)
            assert bravo_goal is not None and bravo_goal.get("priority") == "HIGH", (
                "helper must route the update to bravo's g-001-01; "
                f"bravo g-001-01={bravo_goal!r}")
            # The actor's own  must be UNCHANGED (still MEDIUM).
            assert alpha_goal is not None and alpha_goal.get("priority") == "MEDIUM", (
                "helper must NOT touch the actor alpha's g-001-01; "
                f"alpha g-001-01={alpha_goal!r}")


def test_helper_refuses_exempt_identity_script():
    """ENFORCEMENT: an identity/liveness-exempt script is refused (exit 2) so it
    can never be swapped to the owner's identity."""
    proc = subprocess.run(
        [BASH, str(HELPER), "bravo", "board-post.sh",
         "--channel", "coordination", "--type", "status"],
        capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT),
    )
    assert proc.returncode == 2, (
        f"exempt script must be refused with exit 2; rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
    assert "identity/liveness" in proc.stderr, (
        f"refusal must explain why; stderr={proc.stderr!r}")


def test_helper_self_execution_is_passthrough():
    """Owner '' or '-' is a pure passthrough (normal, non-cross-agent path)."""
    # Usage error path is the cheapest passthrough proof: '-' owner + a bogus
    # script yields the no-such-script error (exit 2), proving '-' does NOT
    # prefix (it reached the script-existence check under the caller's identity).
    proc = subprocess.run(
        [BASH, str(HELPER), "-", "definitely-not-a-real-script.sh"],
        capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT),
    )
    assert proc.returncode == 2 and "no such script" in proc.stderr, (
        f"self/passthrough path wrong; rc={proc.returncode} "
        f"stderr={proc.stderr!r}")


if __name__ == "__main__":
    test_agent_source_write_routes_to_header_owner_queue()
    test_helper_applies_owner_prefix_over_ambient_actor()
    test_helper_refuses_exempt_identity_script()
    test_helper_self_execution_is_passthrough()
    print("ok")
