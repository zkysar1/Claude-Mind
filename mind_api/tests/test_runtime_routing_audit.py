"""Integration test for post-decompose-routing-audit daemon wire-in.

Covers the Phase D.5 wire-in at
mind_api/src/endpoints/aspirations_write.py _file_routing_audit_investigate().
The audit module itself has 8 unit tests on audit() in
core/scripts/tests/test_post_decompose_routing_audit.py; this file exercises
the daemon-side glue: response surface + asp-115 mutation + dedup.

Reference: g-115-1110 (Idea), g-115-1085 (the wire-in landing).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _post(port: int, path: str, query: dict, body: bytes,
          *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query)
    url = (f"http://127.0.0.1:{port}{path}?{qs}"
           if qs else f"http://127.0.0.1:{port}{path}")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip()]


def _seed_routing_audit_fixture(project_root: Path):
    """Seed asp-115 + alpha/echo Self.md + team-state.yaml so the audit can
    score a mismatch.

    Tokens chosen so a goal about "solver verticals gameplay reasoning" scores
    much higher against echo than alpha — gap ≥ 3 triggers a file decision.

    Also mirrors the audit module file into the test's project_root so the
    daemon's `_file_routing_audit_investigate` can load it via
    `spec_from_file_location(<test-project_root>/core/scripts/...)`. `_agents`
    is already in sys.modules from `_clear_agents_cache`, so only the audit
    module file is needed.
    """
    import shutil
    world = project_root / "world"

    # Mirror the audit module into the test project_root.
    repo_root = Path(__file__).resolve().parents[2]
    src_audit = repo_root / "core" / "scripts" / "post-decompose-routing-audit.py"
    dst_scripts = project_root / "core" / "scripts"
    dst_scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_audit, dst_scripts / "post-decompose-routing-audit.py")

    # Append asp-115 to world aspirations.jsonl (conftest seeded only asp-001).
    asp_115 = {
        "id": "asp-115",
        "title": "Maintain Mind System Health (Recurring)",
        "status": "active",
        "priority": "MEDIUM",
        "archived": False,
        "goals": [],
        "progress": {"completed_goals": 0, "total_goals": 0},
    }
    aspirations_path = world / "aspirations.jsonl"
    existing = aspirations_path.read_text(encoding="utf-8")
    aspirations_path.write_text(
        existing + json.dumps(asp_115) + "\n",
        encoding="utf-8",
    )

    # Alpha Self.md — daemon/backend tokens (no overlap with goal tokens).
    (project_root / "agents" / "alpha" / "self.md").write_text(
        "---\nname: alpha\nrole: server-backend\n---\n\n"
        "## What I Do\n\n"
        "Daemon infrastructure backend endpoints database lifecycle.\n"
        "Server deployment configuration networking authentication.\n"
        "Httpd request handling persistence concurrency thread safety.\n",
        encoding="utf-8",
    )

    # Echo Self.md — solver/ARC tokens (overlap with goal tokens).
    (project_root / "agents" / "echo" / "self.md").write_text(
        "---\nname: echo\nrole: arc-vertical-owner\n---\n\n"
        "## What I Do\n\n"
        "Solver verticals gameplay reasoning hooks recordings sessions.\n"
        "ARC integration agent-server bridge skill-acquisition training.\n"
        "Reasoning trajectories environment-server framework-routed.\n",
        encoding="utf-8",
    )

    # Stub Self.md for the other four agents so scoring is well-defined.
    for other in ("bravo", "charlie", "delta", "zeta"):
        (project_root / "agents" / other / "self.md").write_text(
            "---\nname: " + other + "\n---\n\n"
            "## What I Do\n\nUnrelated unique scope distinct domain coverage.\n",
            encoding="utf-8",
        )

    # team-state.yaml so _agents.get_active_agents reads the test's agent set
    # rather than falling back to discovery (which would still work, but
    # team-state is the SSOT path the production code prefers).
    (world / "team-state.yaml").write_text(
        "last_updated: '2026-05-22T00:00:00'\n"
        "agent_status:\n"
        "  alpha: {last_active: '2026-05-22T00:00:00', current_focus: ''}\n"
        "  bravo: {last_active: '2026-05-22T00:00:00', current_focus: ''}\n"
        "  charlie: {last_active: '2026-05-22T00:00:00', current_focus: ''}\n"
        "  delta: {last_active: '2026-05-22T00:00:00', current_focus: ''}\n"
        "  echo: {last_active: '2026-05-22T00:00:00', current_focus: ''}\n"
        "  zeta: {last_active: '2026-05-22T00:00:00', current_focus: ''}\n",
        encoding="utf-8",
    )


def _clear_agents_cache():
    """_agents.py caches active_agents at module level; tests need a fresh read
    against each tmp_path's team-state.yaml."""
    import sys as _sys
    from pathlib import Path as _Path
    scripts_dir = _Path(__file__).resolve().parents[2] / "core" / "scripts"
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    import _agents as agents_mod  # type: ignore
    agents_mod.clear_cache()


# ---------------------------------------------------------------------------
# Assertion 1+2+3: mismatch fires, response carries id, asp-115 has Investigate.
# ---------------------------------------------------------------------------

def test_routing_audit_fires_on_mismatch_and_response_surfaces_id(running_daemon):
    project_root, port = running_daemon
    _seed_routing_audit_fixture(project_root)
    _clear_agents_cache()

    goal = {
        "title": "Apply: solver verticals gameplay reasoning recordings",
        "description": (
            "Build a hook for the solver vertical that processes gameplay "
            "recordings via the ARC integration bridge. Touches environment-"
            "server reasoning trajectories and skill-acquisition signals."
        ),
        "intended_agent": "alpha",
        "origin_signal": "user_directive",
        "category": "framework-architecture",
        "work_class": "framework",
    }
    body = json.dumps(goal).encode("utf-8")
    status, resp_text = _post(
        port, "/v1/aspirations/add-goal",
        {"asp_id": "asp-115", "source": "world"}, body,
    )

    assert status == 200, f"add-goal failed: {status} {resp_text}"
    resp = json.loads(resp_text)

    # Assertion 1: response_body has non-null routing_audit_investigate_id.
    investigate_id = resp.get("routing_audit_investigate_id")
    assert investigate_id, (
        f"Expected routing_audit_investigate_id in response, got: {resp}"
    )

    # Assertion 2: asp-115 in world queue has the new Investigate goal.
    items = _read_jsonl(project_root / "world" / "aspirations.jsonl")
    asp_115 = next((a for a in items if a["id"] == "asp-115"), None)
    assert asp_115 is not None, "asp-115 should exist post-add"
    investigate = next(
        (g for g in asp_115["goals"] if g.get("id") == investigate_id),
        None,
    )
    assert investigate is not None, (
        f"Investigate {investigate_id} not found in asp-115 goals"
    )
    assert investigate["title"].startswith("Investigate: routing-mismatch"), (
        f"Unexpected Investigate title: {investigate.get('title')}"
    )

    # Assertion 3: origin_signal carries routing-mismatch:<main_goal_id>.
    main_goal_id = resp["goal_id"]
    expected_origin = f"routing-mismatch:{main_goal_id}"
    assert investigate["origin_signal"] == expected_origin, (
        f"Expected origin_signal={expected_origin}, "
        f"got={investigate.get('origin_signal')}"
    )

    # Sanity: the Investigate must be in asp-115 only (idempotency surface).
    investigates = [
        g for g in asp_115["goals"]
        if g.get("origin_signal") == expected_origin
    ]
    assert len(investigates) == 1, (
        f"Expected exactly 1 Investigate with origin {expected_origin}, "
        f"got {len(investigates)}"
    )


# ---------------------------------------------------------------------------
# Assertion 4: dedup — calling _file_routing_audit_investigate twice with the
# same goal dict doesn't file two Investigates.
#
# Tested in-process: daemon POST always allocates a fresh main goal_id, so the
# origin_signal differs per POST. The dedup the audit actually defends against
# is two helper calls on the SAME goal dict (e.g., a retry inside the
# pipeline, or a manual replay). Direct in-process invocation exercises that.
# ---------------------------------------------------------------------------

def test_routing_audit_idempotent_on_same_goal(running_daemon):
    project_root, port = running_daemon
    _seed_routing_audit_fixture(project_root)
    _clear_agents_cache()

    # First POST: file the Investigate normally.
    goal = {
        "title": "Apply: solver verticals gameplay reasoning",
        "description": (
            "Build solver hook gameplay recordings via ARC integration bridge "
            "environment-server reasoning trajectories."
        ),
        "intended_agent": "alpha",
        "origin_signal": "user_directive",
        "category": "framework-architecture",
        "work_class": "framework",
    }
    body = json.dumps(goal).encode("utf-8")
    status, resp_text = _post(
        port, "/v1/aspirations/add-goal",
        {"asp_id": "asp-115", "source": "world"}, body,
    )
    assert status == 200, resp_text
    resp = json.loads(resp_text)
    main_goal_id = resp["goal_id"]
    investigate_id = resp.get("routing_audit_investigate_id")
    assert investigate_id, f"Initial audit should file Investigate: {resp}"

    # Now invoke _file_routing_audit_investigate directly with the same
    # persisted goal — dedup should refuse the second filing.
    import sys as _sys
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))
    from mind_api.src.endpoints import aspirations_write as aw  # type: ignore

    # Build a ctx stub that matches what add_goal sees.
    items = _read_jsonl(project_root / "world" / "aspirations.jsonl")
    asp_115 = next(a for a in items if a["id"] == "asp-115")
    persisted_goal = next(
        g for g in asp_115["goals"] if g.get("id") == main_goal_id
    )

    class _PathsStub:
        def __init__(self, root: Path):
            self.project_root = root
            self.world = root / "world"
            self.meta = root / "meta"
            self.agent = root / "agents" / "alpha"

    class _CtxStub:
        def __init__(self, root: Path):
            self.paths = _PathsStub(root)
            self.query = {}
            self.body = b""
            self.headers = {"X-Mind-Agent": "alpha"}

    ctx = _CtxStub(project_root)
    second_id = aw._file_routing_audit_investigate(ctx, persisted_goal)

    # Assertion 4: dedup returned None (no second Investigate filed).
    assert second_id is None, (
        f"Expected dedup to refuse second filing, got id={second_id}"
    )

    # Verify on disk: exactly one Investigate with the audit origin_signal.
    items_after = _read_jsonl(project_root / "world" / "aspirations.jsonl")
    asp_after = next(a for a in items_after if a["id"] == "asp-115")
    expected_origin = f"routing-mismatch:{main_goal_id}"
    investigates = [
        g for g in asp_after["goals"]
        if g.get("origin_signal") == expected_origin
    ]
    assert len(investigates) == 1, (
        f"Expected 1 Investigate with origin {expected_origin}, "
        f"got {len(investigates)}"
    )
