"""GET /v1/experience/read must COMPOSE filters with selectors ().

THE DEFECT. `read()` was a first-match-wins if/return chain, so a combination
silently took the earliest branch and dropped every later one. Measured on
alpha/DESKTOP-O91DLK2 2026-09-05 against the live store, with controls:

    --recent 5                            ->   5 records   (correct alone)
    --type goal_execution                 -> 849 records   (correct alone)
    --type goal_execution --recent 5      -> 849 records   <- --recent DROPPED
    --type goal_execution --recent 30     -> 849 records   <- same bytes exactly
    --summary                             -> 1026 lines, non-JSON
    --type ... --recent 30 --summary      -> 1026 lines, IDENTICAL byte count

No error, no warning, rc=0 throughout. The module docstring called these params
"mutually exclusive", but nothing enforced it and aspirations-consolidate Step
2.9 was already combining three of them on every consolidation on every box.

WHY IT SURVIVED SO LONG, and why these tests are shaped the way they are: each
flag is CORRECT IN ISOLATION. Any suite that exercises them one at a time passes
while the combination is broken. So the regression tests here assert on the
COMBINATION first, and the single-flag cases appear only as explicit CONTROLS
proving the fix did not regress them (the goal's own outcome 4).

The subtle half is ORDER, not just presence: filter-then-limit and
limit-then-filter both "use" both flags, and both return <= N records, so a test
that only checks `len(result) <= N` passes against the wrong one.
test_filters_before_limiting is the one that separates them.

Fixture note: `running_daemon` binds a thread-local daemon in a tmp project
root, so `_seed` writes a tmp `experience.jsonl` and never the live store —
same pattern as test_experience_read_goal_derivation.py.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


def _get(port: int, query: dict, *, agent: str = "alpha") -> tuple[int, str]:
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}/v1/experience/read?{qs}"
    req = urllib.request.Request(url)
    req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


def _rec(rec_id, *, typ="goal_execution", category="test",
         created="2026-08-01T00:00:00", retrieval_count=0):
    return {
        "id": rec_id,
        "type": typ,
        "category": category,
        "summary": f"summary for {rec_id}",
        "goal_id": None,
        "content_path": f"alpha/experience/{rec_id}.md",
        "created": created,
        "retrieval_stats": {"retrieval_count": retrieval_count},
        "tree_nodes_related": [],
    }


def _seed(project_root, records):
    """Write the tmp-world live store the fixture's daemon reads."""
    p = project_root / "agents" / "alpha" / "experience.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _ids(body):
    return [r["id"] for r in json.loads(body)]


# ── THE REGRESSION: the combination ──────────────────────────────────────────

def test_type_and_recent_combined_limits_to_n(running_daemon):
    """THE defect. Before the fix this returned every goal_execution record."""
    project_root, port = running_daemon
    _seed(project_root, [
        _rec(f"exp-g-1-{i}", typ="goal_execution", created=f"2026-08-{i:02d}T00:00:00")
        for i in range(1, 21)
    ])
    code, body = _get(port, {"type": "goal_execution", "recent": "5"})
    assert code == 200
    got = _ids(body)
    assert len(got) == 5, (
        f"--type + --recent 5 returned {len(got)} records, not 5 — the selector "
        "was dropped, which is the g-115-5730 defect exactly"
    )


def test_filters_before_limiting(running_daemon):
    """ORDER, not just presence — the half a `len <= N` assertion cannot catch.

    The 10 globally-newest records are all `reflection`. A LIMIT-then-FILTER
    implementation takes those 10 first, then filters to goal_execution, and
    returns ZERO — while still having "used" both flags and still satisfying
    `len(result) <= 5`. Only filter-then-limit returns the 5 requested.
    """
    project_root, port = running_daemon
    _seed(project_root, [
        _rec(f"exp-old-{i:02d}", typ="goal_execution", created=f"2026-08-{i:02d}T00:00:00")
        for i in range(1, 11)
    ] + [
        _rec(f"exp-new-{i}", typ="reflection", created=f"2026-08-{i:02d}T00:00:00")
        for i in range(20, 30)
    ])
    code, body = _get(port, {"type": "goal_execution", "recent": "5"})
    assert code == 200
    got = _ids(body)
    assert len(got) == 5, (
        f"expected 5 goal_execution records, got {len(got)} — a limit-then-filter "
        "implementation returns 0 here because the newest 10 are all 'reflection'"
    )
    assert all(g.startswith("exp-old-") for g in got), got
    # and they must be the 5 NEWEST of that type, not an arbitrary 5
    assert got == ["exp-old-10", "exp-old-09", "exp-old-08",
                   "exp-old-07", "exp-old-06"], got


def test_category_and_recent_compose(running_daemon):
    """The sibling filter — the fix must not be special-cased to `type`."""
    project_root, port = running_daemon
    _seed(project_root, [
        _rec(f"exp-a-{i:02d}", category="alpha-cat", created=f"2026-08-{i:02d}T00:00:00")
        for i in range(1, 11)
    ] + [
        _rec(f"exp-b-{i}", category="other-cat", created=f"2026-08-{i:02d}T00:00:00")
        for i in range(11, 21)
    ])
    code, body = _get(port, {"category": "alpha-cat", "recent": "3"})
    assert code == 200
    assert _ids(body) == ["exp-a-10", "exp-a-09", "exp-a-08"], _ids(body)


def test_type_and_category_and_recent_all_three_compose(running_daemon):
    """Two filters AND together, then the selector limits."""
    project_root, port = running_daemon
    _seed(project_root, [
        _rec("exp-hit-1", typ="goal_execution", category="c1", created="2026-08-09T00:00:00"),
        _rec("exp-hit-2", typ="goal_execution", category="c1", created="2026-08-08T00:00:00"),
        _rec("exp-hit-3", typ="goal_execution", category="c1", created="2026-08-07T00:00:00"),
        _rec("exp-wrongtype", typ="reflection", category="c1", created="2026-08-30T00:00:00"),
        _rec("exp-wrongcat", typ="goal_execution", category="c2", created="2026-08-29T00:00:00"),
    ])
    code, body = _get(port, {"type": "goal_execution", "category": "c1", "recent": "2"})
    assert code == 200
    assert _ids(body) == ["exp-hit-1", "exp-hit-2"], _ids(body)


def test_summary_respects_type_and_recent(running_daemon):
    """The literal Step 2.9 invocation. Still plain text (by design) — but now
    scoped to the requested window instead of rendering the whole store."""
    project_root, port = running_daemon
    _seed(project_root, [
        _rec(f"exp-g-{i:02d}", typ="goal_execution", created=f"2026-08-{i:02d}T00:00:00")
        for i in range(1, 16)
    ] + [
        _rec("exp-reflect", typ="reflection", created="2026-08-31T00:00:00"),
    ])
    code, body = _get(port, {"type": "goal_execution", "recent": "4", "summary": "1"})
    assert code == 200
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert len(lines) == 4, f"summary rendered {len(lines)} lines, expected 4"
    assert "exp-reflect" not in body, "summary ignored the type filter"


# ── CONTROLS (goal outcome 4): single-flag behaviour must be unchanged ────────

def test_CONTROL_recent_alone_unchanged(running_daemon):
    project_root, port = running_daemon
    _seed(project_root, [
        _rec(f"exp-x-{i:02d}", typ="goal_execution" if i % 2 else "reflection",
             created=f"2026-08-{i:02d}T00:00:00")
        for i in range(1, 21)
    ])
    code, body = _get(port, {"recent": "5"})
    assert code == 200
    recs = json.loads(body)
    assert len(recs) == 5
    # across ALL types — --recent alone must not have acquired a type filter
    assert len({r["type"] for r in recs}) > 1, (
        "--recent alone returned a single type; the fix leaked a filter into it")


def test_CONTROL_type_alone_unchanged(running_daemon):
    """--type alone still returns EVERY matching record, unlimited."""
    project_root, port = running_daemon
    _seed(project_root, [
        _rec(f"exp-t-{i:02d}", typ="goal_execution", created=f"2026-08-{i:02d}T00:00:00")
        for i in range(1, 21)
    ] + [_rec("exp-other", typ="reflection")])
    code, body = _get(port, {"type": "goal_execution"})
    assert code == 200
    got = _ids(body)
    assert len(got) == 20, f"--type alone returned {len(got)}, expected all 20"
    assert "exp-other" not in got


def test_CONTROL_summary_alone_is_plain_text_over_whole_store(running_daemon):
    """--summary alone was ALWAYS plain text. That is not the combination
    defect and must not be 'fixed' into JSON — a real consumer reads these
    lines."""
    project_root, port = running_daemon
    _seed(project_root, [_rec(f"exp-s-{i}") for i in range(1, 8)])
    code, body = _get(port, {"summary": "1"})
    assert code == 200
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert len(lines) == 7, f"expected 7 summary lines, got {len(lines)}"
    try:
        json.loads(body)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("--summary alone became JSON; that breaks its callers")


def test_CONTROL_most_retrieved_alone_unchanged(running_daemon):
    project_root, port = running_daemon
    _seed(project_root, [
        _rec("exp-hot", retrieval_count=99),
        _rec("exp-warm", retrieval_count=5),
        _rec("exp-cold", retrieval_count=0),
    ])
    code, body = _get(port, {"most_retrieved": "2"})
    assert code == 200
    assert _ids(body) == ["exp-hot", "exp-warm"], _ids(body)


def test_CONTROL_no_filter_still_errors(running_daemon):
    """The missing-filter error must survive the restructure — it is the tell
    guard-5505 tells you to read when checking whether a daemon is current."""
    project_root, port = running_daemon
    _seed(project_root, [_rec("exp-any")])
    try:
        code, _body = _get(port, {})
    except urllib.error.HTTPError as e:
        assert e.code >= 400
        return
    assert code >= 400, f"no-filter query returned {code}, expected an error"
