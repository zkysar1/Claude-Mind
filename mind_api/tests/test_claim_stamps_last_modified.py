"""The claim/release writers must stamp `last_modified` in the SAME mutation ().

WHY THIS TEST EXISTS. `coordination_merge._merge_goal` picks its LWW base by
`last_modified` and moves the claim triple (`claimed_by` / `claimed_by_sid` /
`claimed_at`) as a UNIT. A claim write that does not advance `last_modified` is
therefore INVISIBLE to the merge: any peer snapshot carrying the pre-claim state
wins the LWW comparison and overwrites it, so a later release REVERTS to the old
claim (measured twice on g-373-38, 2026-09-13). guard-2872 already states the
general rule -- an amendment to a merge-protected record MUST write a recency
stamp in the same mutation -- and `update_goal` honours it; the claim path did
not.

THE FALSE-GREEN THIS TEST IS SHAPED TO AVOID (measured, echo/cc-03 2026-09-17).
The skew is UNIVERSAL, not intermittent: the claim writer failed to stamp EVERY
time, and the live claims that read "ok" were simply claimers who happened to
issue an unrelated `aspirations-update-goal` write seconds later, which stamped
`last_modified` as a side effect. So a check that samples real claims reports a
ratio of how often a follow-up write masked the defect, NOT how often it fired.
Both cases below therefore assert on a claim with NO follow-up write, against a
seeded `last_modified` far in the past -- the only shape that can distinguish
"the writer stamped" from "something else stamped afterwards".

Assertions read the PERSISTED store, not the response body: the defect's own
signature is that the in-turn read-back looks clean, so the standard
verify-the-write-landed discipline cannot catch it.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Deliberately far in the past: any value the claim writer stamps must exceed it,
# and a writer that stamps nothing leaves exactly this value behind -- which is
# what makes the failure legible rather than a near-miss on clock resolution.
SEEDED_LAST_MODIFIED = "2026-01-01T00:00:00"

# Claim POSTs MUST carry a sid -- the endpoint refuses sid-less world-goal claims
# (-b) and production always sends one (aspirations-claim.sh appends
# &sid=$MIND_SID). Omitting it would exercise a branch production never takes
# (guard-920).
CLAIMER_SID = "77777777-bbbb-cccc-dddd-777777777777"


def _post(port, path, query, *, agent="alpha"):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    req = urllib.request.Request(url, data=None, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_goal(world: Path, goal_id: str):
    """Read the goal back out of the PERSISTED store."""
    path = world / "aspirations.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        asp = json.loads(line)
        for goal in asp.get("goals", []):
            if goal.get("id") == goal_id:
                return goal
    raise AssertionError(f"goal {goal_id} not found in {path}")


def _seed(world: Path, *, claimed=False):
    goal = {
        "id": "g-001-01",
        "title": "Claimable goal",
        "status": "pending",
        "recurring": False,
        "last_modified": SEEDED_LAST_MODIFIED,
    }
    if claimed:
        goal["claimed_by"] = "alpha"
        goal["claimed_by_sid"] = CLAIMER_SID
        goal["claimed_at"] = "2026-09-01T12:00:00"
    asp = {
        "id": "asp-001", "title": "Test claim stamping", "status": "active",
        "priority": "LOW", "archived": False,
        "goals": [goal],
        "progress": {"completed_goals": 0, "total_goals": 1, "recurring_goals": 0},
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")


def test_claim_stamps_last_modified(running_daemon):
    """A claim must advance last_modified to at least claimed_at, in the same write.

    Pre-fix this FAILS with last_modified still at the seeded 2026-01-01 value
    while claimed_at is now -- the exact skew claim-integrity-check.sh reports.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    _seed(world)

    status, body = _post(port, "/v1/aspirations/claim",
                         {"id": "g-001-01", "agent": "alpha", "sid": CLAIMER_SID})
    assert status == 200, f"Expected 200, got {status}: {body}"

    goal = _read_goal(world, "g-001-01")
    claimed_at = goal.get("claimed_at")
    last_modified = goal.get("last_modified")
    assert claimed_at, "claim did not write claimed_at -- the fixture is wrong, not the writer"
    # The positive control: a writer that stamps nothing leaves the seed behind.
    assert last_modified != SEEDED_LAST_MODIFIED, (
        "claim left last_modified at its seeded value "
        f"({SEEDED_LAST_MODIFIED}) -- the claim triple is invisible to "
        "coordination_merge._merge_goal and a release will REVERT (g-115-9909)")
    # Both fields are second-resolution ISO strings from the same clock, so a
    # lexicographic compare is a chronological compare.
    assert last_modified >= claimed_at, (
        f"last_modified ({last_modified}) is older than claimed_at "
        f"({claimed_at}) -- claim-clock skew, guard-2872")


def test_release_stamps_last_modified(running_daemon):
    """A release must advance last_modified too -- it mutates the same merged unit.

    The release pops the claim triple. Under LWW that pop is just as invisible
    as the claim was if the record's recency stamp does not move with it.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    _seed(world, claimed=True)

    before = _read_goal(world, "g-001-01")["last_modified"]
    assert before == SEEDED_LAST_MODIFIED

    status, body = _post(port, "/v1/aspirations/release",
                         {"id": "g-001-01", "source": "world"})
    assert status == 200, f"Expected 200, got {status}: {body}"

    goal = _read_goal(world, "g-001-01")
    assert goal.get("claimed_by") is None, "release did not clear the claim"
    after = goal.get("last_modified")
    assert after != before, (
        "release left last_modified at its pre-release value -- the cleared "
        "claim is invisible to the merge, so a peer snapshot carrying the old "
        "claim will resurrect it (g-115-9909)")
    assert after > before, f"last_modified moved backwards: {before} -> {after}"
