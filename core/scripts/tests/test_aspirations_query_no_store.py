"""A query against ZERO readable stores must not look like a clean queue — .

THE DEFECT. `aspirations_query.query()` built its source list with two `.exists()`
checks and no else-branch. When neither store path resolved, `sources` stayed
empty, the result loop ran zero times, and the endpoint returned `[]` with HTTP
200 and no stderr — BYTE-IDENTICAL to a genuinely empty queue.

That is not a hypothetical. Measured on one box (foxtrot, WSL2): EVERY invocation
of aspirations-query.sh returned `[]` rc=0 — `--goal-status pending`,
`--goal-status completed`, `--title-contains ppe`, both `--source` values — while
`aspirations-read.sh` reported 25 aspirations / 5149 world goals IN THE SAME
MINUTE. The wrapper was not filtering to zero; it was answering zero
unconditionally, and every consumer reads that as an authoritative all-clear.

WHY AN ERROR AND NOT AN EMPTY LIST WITH A WARNING: the endpoint's entire output is
the JSON array, so a caller parsing it has nowhere to receive a warning, and every
existing consumer already treats `[]` as authoritative. rb-245 is the general form
— a zero whose two explanations ("nothing matched your filter" / "I could not find
the store at all") imply OPPOSITE actions is not a measurement.

The suite here is two-way on purpose (guard-1220): case 1 proves the missing-store
path now refuses, and case 2 proves a present store still answers 200 with real
rows. Case 1 alone would also pass against an endpoint that refused everything.
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402

ASP_ID = "asp-qnostore"
GOAL_ID = "g-qnostore-001"


def _world(tmp: Path, *, with_store: bool) -> Path:
    """A well-formed world dir that either HAS or LACKS aspirations.jsonl."""
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True, exist_ok=True)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text("nodes: {}\n", encoding="utf-8")
    if with_store:
        asp = {
            "id": ASP_ID, "title": "no-store regression aspiration", "status": "active",
            "goals": [{"id": GOAL_ID, "title": "present-store target",
                       "status": "pending", "priority": "MEDIUM"}],
        }
        (world / "aspirations.jsonl").write_text(json.dumps(asp) + "\n", encoding="utf-8")
    return world


def _get(port: int, params: dict):
    """Return (status, body). Never raises on 4xx — the 404 IS the assertion."""
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/aspirations/query?{q}",
        headers={"X-Mind-Agent": "alpha"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_missing_store_refuses_instead_of_returning_empty():
    """The pin. Before the fix this returned (200, '[]') — a silent false all-clear."""
    with tempfile.TemporaryDirectory(prefix="q-nostore-") as tmpd:
        world = _world(Path(tmpd), with_store=False)
        with DaemonFixture(world) as df:
            status, body = _get(df.port, {"goal_status": "pending"})

    assert status != 200, (
        "a query against a world with NO aspirations.jsonl returned HTTP 200 — "
        f"indistinguishable from a genuinely clean queue. body={body[:200]}")
    assert status == 404, f"expected 404 no_aspiration_store, got {status}: {body[:200]}"
    assert "no_aspiration_store" in body, body[:300]
    # The operator has to know WHERE it looked, or the error is unactionable —
    # this defect is a path-resolution failure, so the path is the whole diagnosis.
    assert "aspirations.jsonl" in body, (
        f"the error does not name the path it probed, so it cannot be acted on: {body[:300]}")


def test_missing_store_refuses_for_every_filter_shape():
    """The reported signature was EVERY query returning [], not one filter."""
    shapes = [
        {"goal_status": "pending"},
        {"goal_status": "completed"},
        {"title_contains": "ppe"},
        {"goal_field_name": "id", "goal_field_value": GOAL_ID},
    ]
    with tempfile.TemporaryDirectory(prefix="q-nostore-all-") as tmpd:
        world = _world(Path(tmpd), with_store=False)
        with DaemonFixture(world) as df:
            results = [(p, *_get(df.port, p)) for p in shapes]
    bad = [(p, s, b[:80]) for p, s, b in results if s != 404]
    assert not bad, f"these filter shapes did not refuse against a missing store: {bad}"


def test_present_store_still_answers_normally():
    """Two-way half (guard-1220). Case 1 alone passes against a total refusal."""
    with tempfile.TemporaryDirectory(prefix="q-store-") as tmpd:
        world = _world(Path(tmpd), with_store=True)
        with DaemonFixture(world) as df:
            status, body = _get(df.port, {"goal_status": "pending"})

    assert status == 200, f"a world WITH a store must answer 200, got {status}: {body[:200]}"
    data = json.loads(body)
    assert isinstance(data, list) and data, f"expected real rows, got {data!r}"
    assert any(r.get("goal_id") == GOAL_ID for r in data), (
        f"the seeded goal is missing from the result: {data}")


def test_empty_result_from_a_present_store_is_still_200():
    """The distinction the fix exists to draw, asserted directly.

    A present store that matches nothing MUST still be 200 + `[]` — that is a real
    measurement. Only an ABSENT store refuses. Without this, a fix that refused on
    any empty result would pass both tests above and destroy the legitimate case.
    """
    with tempfile.TemporaryDirectory(prefix="q-store-empty-") as tmpd:
        world = _world(Path(tmpd), with_store=True)
        with DaemonFixture(world) as df:
            status, body = _get(df.port, {"title_contains": "zzz-matches-nothing-zzz"})

    assert status == 200, f"a present store matching nothing must be 200, got {status}"
    assert json.loads(body) == [], f"expected an empty list, got {body[:200]}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
