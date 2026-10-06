""" — GET /v1/retrieve?goal= scopes the experiences lane to that goal.

`retrieve.sh --category <cat> --goal <id>` returned the SAME experiences as the
call without `--goal`: the experiences lane matched on category alone, and a
recurring goal files each run under that run's topic, so its own history is
scattered across the one dimension the lane indexes on (measured: 9 records
under 8 categories, a category query reached 1-2 of them, while
`experience-read --goal` returned all 9). The loop calls the retrieval path at
goal start, never the CLI read, so the lesson on disk twice was re-derived from
scratch twice.

The endpoint now hands `load_experiences` the same predicate
`/v1/experience/read?goal=` uses: the record's goal_id FIELD, or the goal id
embedded in its `exp-<goal-id>[-slug]` id (g-115-7072 — the field alone erodes
on a fleet-synced store). The loader half is pinned in
core/scripts/tests/test_retrieve_experiences_goal_union.py; this file pins what
only the endpoint can: the REAL id derivation (including the prefix-collision
decoy a startswith() would sweep in), parity with the experience read endpoint,
the no-goal path, and the session manifest, which must name every experience the
response returned or `utilization-feedback --helpful` cannot credit it
(g-115-3855's invariant, applied to the new records).

Reachable-red (guard-1475): with `goal_match` not passed at the call site in
mind_api/src/endpoints/retrieve.py, the three goal-bearing tests FAIL.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

GOAL = "g-901-07"
QUERY_CAT = "alpha-cat"


def _get(port: int, path: str, query: dict, *, agent: str = "alpha"):
    url = f"http://127.0.0.1:{port}{path}?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url)
    req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200, f"HTTP {resp.status} from {path}"
        return json.loads(resp.read().decode("utf-8"))


def _retrieve(port, **q):
    return _get(port, "/v1/retrieve", q)


def _exp_ids(data):
    return [e["id"] for e in data["experiences"]]


def _rec(rec_id, category, created, *, goal_id=None, retrieval_count=0, **extra):
    r = {"id": rec_id, "type": "goal_execution", "category": category,
         "summary": f"summary for {rec_id}", "goal_id": goal_id,
         "content_path": f"alpha/experience/{rec_id}.md", "created": created,
         "retrieval_stats": {"retrieval_count": retrieval_count}}
    r.update(extra)
    return r


# The goal's own history: four runs, four categories, none = QUERY_CAT.
OWN_NEWEST_FIRST = [
    "exp-g-901-07",          # bare exp-<goal-id>: the id regex's end-anchored arm
    "exp-g-901-07-r3",       # goal_id field ERASED: only the id still names the goal
    "exp-g-901-07-r2",
    "exp-g-901-07-r1",
]
OWN = [
    _rec("exp-g-901-07-r1", "cat-one", "2026-07-01T10:00:00", goal_id=GOAL),
    _rec("exp-g-901-07-r2", "cat-two", "2026-07-02T10:00:00", goal_id=GOAL),
    _rec("exp-g-901-07-r3", "cat-three", "2026-07-03T10:00:00"),
    _rec("exp-g-901-07", "cat-four", "2026-07-04T10:00:00"),
]
NOT_OWN = [
    # A DIFFERENT goal whose id merely BEGINS with this goal's characters: a
    # startswith() would sweep it in, the id regex's (?:-|$) anchor must not.
    _rec("exp-g-901-077-sibling", "cat-five", "2026-07-05T10:00:00"),
    _rec("exp-g-902-01-other", "cat-six", "2026-07-06T10:00:00",
         goal_id="g-902-01"),
    # A slug-only id embeds no goal id and carries no goal_id value.
    _rec("exp-owncloud-collision-note", "cat-seven", "2026-07-07T10:00:00"),
]
ARCHIVED_OWN = _rec("exp-g-901-07-old", "cat-eight", "2026-06-01T10:00:00",
                    goal_id=GOAL, archived=True)
CATEGORY = [
    _rec("exp-cat-a", QUERY_CAT, "2026-07-08T10:00:00", goal_id="g-902-02",
         retrieval_count=5),
    _rec("exp-cat-b", QUERY_CAT, "2026-07-09T10:00:00", goal_id="g-902-03",
         retrieval_count=1),
]


def _seed(project_root):
    p = project_root / "agents" / "alpha" / "experience.jsonl"
    rows = OWN + NOT_OWN + [ARCHIVED_OWN] + CATEGORY
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_goal_param_surfaces_own_records_whatever_their_category(running_daemon):
    project_root, port = running_daemon
    _seed(project_root)

    data = _retrieve(port, category=QUERY_CAT, depth="deep", goal=GOAL,
                     read_only="1")

    # own records first, newest first; then the category selection in its own
    # order. The decoys (prefix-collision, other goal, slug-only) and the
    # archived own record are absent.
    assert _exp_ids(data) == OWN_NEWEST_FIRST + ["exp-cat-a", "exp-cat-b"]
    assert data["meta"]["items_returned"]["experiences"] == 6


def test_without_a_goal_the_experiences_lane_is_unchanged(running_daemon):
    """The no-goal path: category selection only, exactly as before."""
    project_root, port = running_daemon
    _seed(project_root)

    data = _retrieve(port, category=QUERY_CAT, depth="deep", read_only="1")

    assert _exp_ids(data) == ["exp-cat-a", "exp-cat-b"]


def test_union_matches_the_experience_read_goal_predicate(running_daemon):
    """One predicate, two readers: whatever `/v1/experience/read?goal=` finds in
    the live store (archived rows aside), a retrieval for that goal finds even
    when its category query matches nothing."""
    project_root, port = running_daemon
    _seed(project_root)

    read_ids = {r["id"] for r in _get(port, "/v1/experience/read",
                                      {"goal": GOAL})
                if not r.get("archived")}
    # ANTI-VACUITY: the equality below must not be satisfiable by two empty sets
    assert read_ids == set(OWN_NEWEST_FIRST)

    data = _retrieve(port, category="zzz-no-such-category", depth="deep",
                     goal=GOAL, read_only="1")
    assert set(_exp_ids(data)) == read_ids


def test_own_records_are_attestable_in_the_session_manifest(running_daemon):
    """Membership is whatever the loader returned (): an own record
    that is returned but absent from `supplementary_items` is counter-bumped
    and then impossible to credit with `utilization-feedback --helpful`."""
    project_root, port = running_daemon
    _seed(project_root)

    data = _retrieve(port, category=QUERY_CAT, depth="deep", goal=GOAL)

    manifest_path = (project_root / "agents" / "alpha" / "session"
                     / "retrieval-session.json")
    assert manifest_path.exists(), (
        "retrieve wrote no utilization manifest -- the endpoint's "
        "`effective_goal and not read_only and agent_dir` gate did not fire, "
        "so this test measured nothing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded = {i["id"] for i in manifest["supplementary_items"]
                if i.get("type") == "experience"}

    assert recorded == set(_exp_ids(data))
    assert set(OWN_NEWEST_FIRST) <= recorded
