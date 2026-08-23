"""GET /v1/experience/read?goal= must find records whose goal_id field is null
but whose ID embeds the goal id (g-115-7072).

WHY THE FIELD ALONE IS NOT ENOUGH — and it is not a writer bug. g-115-7028
already fixed the writer regex AND repaired 862 records in
agents/alpha/experience.jsonl at 04:13 on 2026-08-21. By 06:00 the next writes
had reverted 26 of them to null: the store is append-heavy and fleet-synced, so
a peer holding a pre-repair copy silently un-does the repair on merge
(guard-3209 — a locked write plus a clean re-read proves the write LANDED, not
that it SURVIVES). Measured asymmetry that proves the mechanism:

    experience.jsonl         (many writers) 948 records -> 28 invisible
    experience-archive.jsonl (few writers)  691 records ->  0 invisible

Same repair, same commit; only the contended file eroded. So a field-only
predicate re-breaks on its own and re-running the backfill is a treadmill.
Deriving from the record ID is immune — the id is the record's identity, no
merge rewrites it, and every writer forms it as exp-{goal_id}[-{slug}].

WHAT IT PROTECTS. guard-2939's anti-overwrite pre-check asks "does this goal
already have an experience?" before writing `exp-<goal-id>` and
`agents/<agent>/experience/exp-<goal-id>.md`. A false [] tells the caller both
names are free when they are taken, and the Write silently overwrites a real
record while reporting success.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

import pytest


def _get(port: int, query: dict, *, agent: str = "alpha") -> tuple[int, str]:
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}/v1/experience/read?{qs}"
    req = urllib.request.Request(url)
    req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


def _rec(rec_id, *, goal_id=None, source_id=None, category="test"):
    r = {"id": rec_id, "type": "goal_execution", "category": category,
         "summary": f"summary for {rec_id}",
         "content_path": f"alpha/experience/{rec_id}.md",
         "created": "2026-08-21T04:00:00",
         "retrieval_stats": {"retrieval_count": 0}}
    if goal_id is not None:
        r["goal_id"] = goal_id
    if source_id is not None:
        r["source_id"] = source_id
    return r


def _seed(project_root, records):
    p = project_root / "agents" / "alpha" / "experience.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _ids(body):
    return sorted(r["id"] for r in json.loads(body))


# ── the fix ──────────────────────────────────────────────────────────────────

def test_null_goal_id_with_derivable_bare_id_is_found(running_daemon):
    """THE regression. `exp-` with goal_id null was invisible; this is
    the exact shape of all 28 currently-broken records, and of the two written
    on 2026-08-21 by a daemon still holding pre-fix code."""
    project_root, port = running_daemon
    _seed(project_root, [_rec("exp-g-306-184", goal_id=None, source_id="g-306-184")])

    code, body = _get(port, {"goal": "g-306-184"})
    assert code == 200
    assert _ids(body) == ["exp-g-306-184"], (
        "record whose ID names the goal was not returned — the --goal read is "
        "still field-only, so guard-2939's pre-check cannot fire")


def test_null_goal_id_with_slugged_id_is_found(running_daemon):
    """The other live shape: exp-{goal}-{slug}."""
    project_root, port = running_daemon
    _seed(project_root, [_rec("exp-g-115-5627-coverage-bounds-2026-08-21",
                              goal_id=None, source_id="g-115-5627")])
    code, body = _get(port, {"goal": "g-115-5627"})
    assert code == 200
    assert len(json.loads(body)) == 1


def test_populated_goal_id_still_matches(running_daemon):
    """ANTI-VACUITY. 829 of 948 live records match on the field alone; a change
    that broke them would be far worse than the bug being fixed."""
    project_root, port = running_daemon
    _seed(project_root, [_rec("exp-whatever-slug-only", goal_id="g-001-01")])
    code, body = _get(port, {"goal": "g-001-01"})
    assert code == 200
    assert _ids(body) == ["exp-whatever-slug-only"]


# ── it must not over-match ───────────────────────────────────────────────────

def test_other_goals_are_not_returned(running_daemon):
    """The derivation is exact, so a query must not sweep in neighbours."""
    project_root, port = running_daemon
    _seed(project_root, [
        _rec("exp-g-306-184", goal_id=None, source_id="g-306-184"),
        _rec("exp-g-306-185", goal_id=None, source_id="g-306-185"),
        _rec("exp-g-306-18", goal_id=None, source_id="g-306-18"),
    ])
    code, body = _get(port, {"goal": "g-306-184"})
    assert code == 200
    assert _ids(body) == ["exp-g-306-184"], (
        "prefix bleed: a goal query returned a neighbouring goal's record")


def test_slug_only_id_stays_invisible(running_daemon):
    """`exp-owncloud-s3-collision-truncation-2026-07-09` embeds no goal id. It
    is the one record in the live store that legitimately has no derivable
    linkage, and g-115-7028 ruled it SHOULD stay null. Returning it under some
    goal query would be a false positive, not a fix."""
    project_root, port = running_daemon
    _seed(project_root, [_rec("exp-owncloud-s3-collision-truncation-2026-07-09",
                              goal_id=None, source_id="g-999-99")])
    code, body = _get(port, {"goal": "g-999-99"})
    assert code == 200
    assert json.loads(body) == [], (
        "matched on source_id — source_id is the goal id only for "
        "goal_execution records and carries hypothesis ids elsewhere, so "
        "matching it returns foreign records under a goal query")


def test_unrelated_record_is_not_returned(running_daemon):
    project_root, port = running_daemon
    _seed(project_root, [_rec("exp-g-111-11", goal_id="g-111-11")])
    code, body = _get(port, {"goal": "g-222-22"})
    assert code == 200
    assert json.loads(body) == []


# ── one derivation helper, not three ─────────────────────────────────────────

def test_reader_reuses_the_writers_helper_object():
    """Not a copy — the SAME function. experience_write.py's comment requires
    its regex stay literally identical to core/scripts/experience.py's; a third
    copy in the reader would make that promise harder to keep and could drift
    so the reader and writer disagree about which ids name a goal."""
    from mind_api.src.endpoints import experience as read_mod
    from mind_api.src.endpoints import experience_write as write_mod
    assert read_mod._derive_goal_id_from_id is write_mod._derive_goal_id_from_id


def test_the_two_regex_copies_are_still_identical():
    """Pins the invariant experience_write.py's comment asserts. If this fails,
    the writer and the core-side helper have diverged again — which is exactly
    how the bare-id shape went underivable before g-115-7028."""
    import re
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent.parent
    pat = re.compile(r"^GOAL_ID_IN_EXP_ID_RE = re\.compile\((.+)\)$", re.M)
    found = {}
    for rel in ("core/scripts/experience.py",
                "mind_api/src/endpoints/experience_write.py"):
        m = pat.search((repo / rel).read_text(encoding="utf-8"))
        assert m, f"{rel}: GOAL_ID_IN_EXP_ID_RE not found"
        found[rel] = m.group(1)
    assert len(set(found.values())) == 1, f"copies diverged: {found}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
