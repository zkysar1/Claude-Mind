"""test_aspirations_query_full_mode.py -  regression test.

Pins the --full goal-by-id full-record read mode added to the
/v1/aspirations/query endpoint (mind_api/src/endpoints/aspirations_query.py)
plus the --full flag in core/scripts/aspirations-query.sh.

g-115-1304 (zeta g-115-1281 follow-up, rb-1416): before this change there was
no goal-by-id full-record read in one call. The query endpoint indexed BY
goal-id but projected only 5 fields {goal_id, asp_id, source, title, status};
the full record was only reachable via an aspiration-scoped read + .goals[]
traverse. The fix adds full=true to the query endpoint, which returns the raw
goal dict merged with {asp_id, source} metadata.

Contract pinned here:
  1. full=true returns the FULL canonical record (description, verification,
     priority, intended_agent) + asp_id + source, in one goal-by-id call.
  2. WITHOUT full, the response is the EXACT default projection (6 fields as of
     g-115-1614, which added "category") - byte-for-byte unchanged otherwise (no
     regression). This is the discriminating axis.
  3. full=1 / full=yes / full=TRUE are accepted truthy spellings; full=false /
     absent fall through to the default projection.

Live-daemon safe (guard-672): uses the in-process DaemonFixture against a temp
world (ephemeral OS port, temp mind_api/state/) - never touches the live daemon
or world directory. Run this file standalone, not the full suite, while a live
daemon serves agents.
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402

GOAL_ID = "g-qfull-001"
ASP_ID = "asp-qfull"

# Fields the default projection MUST drop and --full MUST surface.
FULL_ONLY_FIELDS = {"description", "verification", "priority", "intended_agent"}
#  added "category" to the default projection (npc-composition-sweep
# needs it to key cat::prefix clusters instead of degenerating to bare-prefix).
DEFAULT_PROJECTION_KEYS = {"goal_id", "asp_id", "source", "title", "status", "category"}


def _seed_world(tmp: Path) -> Path:
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True, exist_ok=True)
    # Minimal well-formed tree so any incidental tree read does not error.
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        "nodes: {}\n", encoding="utf-8")
    asp = {
        "id": ASP_ID,
        "title": "query full-mode regression aspiration",
        "status": "active",
        "goals": [
            {
                "id": GOAL_ID,
                "title": "full-mode target goal",
                "status": "pending",
                "priority": "MEDIUM",
                "description": "RICH-DESCRIPTION-MARKER body the 5-field projection drops.",
                "verification": {"outcomes": ["marker-outcome"], "checks": []},
                "intended_agent": "alpha",
                "category": "framework-architecture",
            },
            {
                "id": "g-qfull-002",
                "title": "second goal (non-target, must not match id filter)",
                "status": "completed",
            },
        ],
    }
    # aspirations.jsonl is JSONL: one aspiration record per line.
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp) + "\n", encoding="utf-8")
    return world


def _http_get(port: int, path: str, agent: str = "alpha"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Mind-Agent": agent},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8")


def _query(port, full_param=None):
    """GET the goal-by-id query, optionally with full=<full_param>."""
    params = {"goal_field_name": "id", "goal_field_value": GOAL_ID}
    if full_param is not None:
        params["full"] = full_param
    q = urllib.parse.urlencode(params)
    status, body = _http_get(port, f"/v1/aspirations/query?{q}")
    assert status == 200, f"expected HTTP 200, got {status}: {body[:200]}"
    data = json.loads(body)
    assert isinstance(data, list) and len(data) == 1, (
        f"expected exactly 1 result for goal-by-id, got: {data}")
    return data[0]


def test_full_mode_returns_full_record():
    with tempfile.TemporaryDirectory(prefix="q-full-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            rec = _query(df.port, "true")
    missing = FULL_ONLY_FIELDS - set(rec.keys())
    assert not missing, (
        f"--full dropped full-record fields {missing}; keys={sorted(rec.keys())}")
    assert rec["description"].startswith("RICH-DESCRIPTION-MARKER"), rec.get("description")
    assert rec["verification"]["outcomes"] == ["marker-outcome"], rec.get("verification")
    assert rec["priority"] == "MEDIUM", rec.get("priority")
    assert rec["intended_agent"] == "alpha", rec.get("intended_agent")
    # Metadata still merged in.
    assert rec["asp_id"] == ASP_ID and rec["source"] == "world", rec
    # Raw goal dict preserves the goal's own "id" key (NOT renamed to goal_id).
    assert rec["id"] == GOAL_ID, rec


def test_default_projection_unchanged():
    with tempfile.TemporaryDirectory(prefix="q-default-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            rec = _query(df.port, None)  # no full param at all
    assert set(rec.keys()) == DEFAULT_PROJECTION_KEYS, (
        f"default projection drifted: {sorted(rec.keys())} "
        f"!= {sorted(DEFAULT_PROJECTION_KEYS)}")
    assert rec == {
        "goal_id": GOAL_ID,
        "asp_id": ASP_ID,
        "source": "world",
        "title": "full-mode target goal",
        "status": "pending",
        "category": "framework-architecture",
    }, rec


def test_full_truthy_and_false_spellings():
    with tempfile.TemporaryDirectory(prefix="q-truthy-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            for spelling in ("1", "yes", "TRUE"):
                rec = _query(df.port, spelling)
                assert "description" in rec, (
                    f"full={spelling} should return the full record, got {sorted(rec.keys())}")
            # Explicit falsey -> default projection (no full-only fields).
            rec_false = _query(df.port, "false")
            assert set(rec_false.keys()) == DEFAULT_PROJECTION_KEYS, (
                f"full=false should fall through to default projection, got {sorted(rec_false.keys())}")


if __name__ == "__main__":
    test_full_mode_returns_full_record()
    test_default_projection_unchanged()
    test_full_truthy_and_false_spellings()
    print("PASS: g-115-1304 --full goal-by-id full-record read mode")
