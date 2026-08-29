"""PR 6 — /v1/aspirations/query daemon endpoint tests.

The endpoint mirrors `aspirations.py query` cross-queue: reads world AND agent
aspirations, returns flat list of {goal_id, asp_id, source, title, status}
across all matching goals. AND semantics across filters.

The conftest seeds aspirations with empty goals arrays — we overwrite them
in a fixture per test, then exercise the filters.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest


def _get(port: int, path: str, query: dict, *, agent: str = "alpha") -> tuple[int, str]:
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    req = urllib.request.Request(url)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


@pytest.fixture
def seeded_with_goals(running_daemon):
    """Overwrite the conftest aspirations with versions that have goals.

    World asp-001 → 2 goals (1 pending, 1 completed).
    Agent asp-100 → 3 goals (2 pending, 1 blocked; one with tags=['urgent']).
    """
    project_root, port = running_daemon
    world_asp = project_root / "world" / "aspirations.jsonl"
    agent_asp = project_root / "agents" / "alpha" / "aspirations.jsonl"

    world_record = {
        "id": "asp-001",
        "title": "World aspiration",
        "status": "active",
        "priority": "MEDIUM",
        "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "Build encoding pipeline",
             "status": "pending", "category": "framework-architecture",
             "tags": ["routine"], "recurring": True,
             "verified": "true",
             # widget_sentinel appears in NO title anywhere in this fixture —
             # that is the point: it is only reachable by description_contains
             # (). guard-2353 — the row must lie on the far side of
             # the boundary being moved or the suite gives the change no evidence.
             "description": "Covers the widget_sentinel path end to end."},
            # STRAY PROJECTION-ONLY KEYS ON A RAW RECORD (). These two
            # are attached at projection time and are NOT goal fields — but real
            # records in the live store carry stray literal copies anyway (both
            # appear in the endpoint's own 135-name valid-field list). That is the
            # production condition, and without it here the projection-only
            # refusal test passed while the defect was live: with no record
            # carrying the key, `field_key_seen` stayed False and the refusal
            # fired for the wrong reason. Seeded on the COMPLETED goal because
            # that is what leaked in production — `--goal-field asp_id asp-368`
            # returned exactly one row and it was a completed goal, which reads
            # as "this aspiration has one goal" rather than as an error.
            # guard-2353: a fixture row must lie on the FAR SIDE of the boundary
            # being moved, or the suite gives the change no evidence.
            {"id": "g-001-02", "title": "Audit token usage",
             "status": "completed", "category": "observability",
             "tags": [], "recurring": False,
             "asp_id": "asp-001", "source": "world"},
        ],
        "progress": {"completed_goals": 1, "total_goals": 2},
    }
    agent_record = {
        "id": "asp-100",
        "title": "Agent aspiration",
        "status": "active",
        "priority": "HIGH",
        "archived": False,
        "goals": [
            # EXPLICIT NULL description. `goal.get("description", "")` returns
            # None here (the two-arg default fires only on an ABSENT key), so a
            # naive implementation raises AttributeError on .lower() — a 500 on
            # a shape the live store carries in bulk.
            {"id": "g-100-01", "title": "Encode session insights",
             "status": "pending", "category": "framework-architecture",
             "tags": ["urgent", "encoding"], "recurring": True,
             "description": None},
            # ABSENT description key — the other null shape, and the common one.
            {"id": "g-100-02", "title": "Refactor encoder",
             "status": "pending", "category": "framework-architecture",
             "tags": []},
            {"id": "g-100-03", "title": "Investigate flaky test",
             "status": "blocked", "category": "infra",
             "tags": ["urgent"],
             "description": "Retry logic interacts with widget_sentinel under load."},
        ],
        "progress": {"completed_goals": 0, "total_goals": 3},
    }
    world_asp.write_text(json.dumps(world_record) + "\n", encoding="utf-8")
    agent_asp.write_text(json.dumps(agent_record) + "\n", encoding="utf-8")
    return project_root, port


# ---------------------------------------------------------------------------
# Filter: goal_status
# ---------------------------------------------------------------------------

def test_query_by_goal_status_single(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query", {"goal_status": "pending"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-001-01", "g-100-01", "g-100-02"]


def test_query_by_goal_status_comma_list(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_status": "pending,blocked"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-001-01", "g-100-01", "g-100-02", "g-100-03"]


def test_query_includes_source_field(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query", {"goal_status": "completed"})
    data = json.loads(body)
    assert len(data) == 1
    assert data[0]["source"] == "world"
    assert data[0]["asp_id"] == "asp-001"


def test_query_invalid_status_rejected(seeded_with_goals):
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query", {"goal_status": "nonsense"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        body = e.read().decode("utf-8")
        err = json.loads(body)
        assert err["error"] == "invalid_goal_status"
        # Message format must list valid statuses so the user can self-correct.
        assert "nonsense" in err["detail"]
        assert "pending" in err["detail"]
    else:
        raise AssertionError("expected 400 for invalid goal_status")


# ---------------------------------------------------------------------------
# Filter: goal_field (paired name/value)
# ---------------------------------------------------------------------------

def test_query_by_goal_field_scalar(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "category",
                    "goal_field_value": "framework-architecture"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-001-01", "g-100-01", "g-100-02"]


def test_query_by_goal_field_list_contains(seeded_with_goals):
    """List-valued fields use 'contains' semantics — cmd_query line 935."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "tags", "goal_field_value": "urgent"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-100-01", "g-100-03"]


def test_query_goal_field_name_only_rejected(seeded_with_goals):
    """Half-pair → 400. The CLI's argparse nargs=2 enforces this; the daemon
    must too, otherwise the wrapper's PASSTHROUGH could silently send a
    value-less filter."""
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query", {"goal_field_name": "category"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "invalid_goal_field"
    else:
        raise AssertionError("expected 400 for half-paired goal_field")


# ---------------------------------------------------------------------------
# Filter: title_contains (case-insensitive substring)
# ---------------------------------------------------------------------------

def test_query_title_contains_case_insensitive(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query", {"title_contains": "ENCOD"})
    data = json.loads(body)
    ids = sorted(r["goal_id"] for r in data)
    assert ids == ["g-001-01", "g-100-01", "g-100-02"]


# ---------------------------------------------------------------------------
# AND semantics across filters
# ---------------------------------------------------------------------------

def test_query_and_semantics(seeded_with_goals):
    """Multiple filters narrow the result set — all must match."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_status": "pending",
                    "goal_field_name": "category",
                    "goal_field_value": "framework-architecture",
                    "title_contains": "session"})
    data = json.loads(body)
    ids = [r["goal_id"] for r in data]
    assert ids == ["g-100-01"]


# ---------------------------------------------------------------------------
# Missing-filter 400
# ---------------------------------------------------------------------------

def test_query_no_filter_400(seeded_with_goals):
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query", {})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "missing_filter"
    else:
        raise AssertionError("expected 400 when no filter provided")


# ---------------------------------------------------------------------------
# Empty result returns []
# ---------------------------------------------------------------------------

def test_query_empty_result(seeded_with_goals):
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"title_contains": "nonexistent-substring"})
    assert json.loads(body) == []


# ---------------------------------------------------------------------------
# Vacuous goal_field filters are refused, not answered ()
#
# Before this, an unknown/misspelled/output-only field name returned `[]` with
# HTTP 200 — byte-identical to a genuine empty AND sharing its exit code, so a
# caller had no discriminator at all. Three false zeros in one investigation
# (2026-08-10), then a fourth by a different agent in a different lane the next
# day, all against goals that existed.
# ---------------------------------------------------------------------------

def test_query_unknown_goal_field_rejected(seeded_with_goals):
    """A field name no goal record carries is unanswerable, not empty."""
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query",
             {"goal_field_name": "zzz_not_a_field", "goal_field_value": "x"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "unknown_goal_field"
        # Must name the offending key AND list real ones, so the caller can
        # self-correct without reading the source (mirrors invalid_goal_status).
        # The full valid-key list is asserted, not a sample: the omitted key is
        # exactly the one the caller was reaching for (guard-1941).
        assert "zzz_not_a_field" in err["detail"]
        for valid in ("id", "title", "status", "category"):
            assert valid in err["detail"], f"valid key {valid!r} missing from detail"
    else:
        raise AssertionError("expected 400 for a field name no record carries")


def test_query_unknown_field_with_none_value_does_not_match_everything(seeded_with_goals):
    """The over-broad half, and the reason the refusal is not gated on `not results`.

    `_goal_matches` compares str(goal.get(field)) != value, so an ABSENT key
    stringifies to "None" and the literal value "None" matched EVERY goal. That
    returned the whole store with HTTP 200 — a wrong answer that looks like a
    thorough one. A result-size-gated refusal would sail straight past it.
    """
    _, port = seeded_with_goals
    try:
        _get(port, "/v1/aspirations/query",
             {"goal_field_name": "zzz_not_a_field", "goal_field_value": "None"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read().decode("utf-8"))["error"] == "unknown_goal_field"
    else:
        raise AssertionError(
            "expected 400 — an absent key with value 'None' must not match every goal")


def test_query_goal_id_alias_resolves_to_id(seeded_with_goals):
    """`goal_id` is what every projection EMITS, so callers key on it ().

    Matching reads the raw record, where the key is `id`. Measured twice by two
    agents a day apart: `--goal-field goal_id <id>` returned 0 rows for a goal
    that exists.
    """
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "goal_id", "goal_field_value": "g-001-01"})
    data = json.loads(body)
    assert [r["goal_id"] for r in data] == ["g-001-01"]

    # The alias must agree with the un-aliased spelling, not merely be non-empty.
    _, body_id = _get(port, "/v1/aspirations/query",
                      {"goal_field_name": "id", "goal_field_value": "g-001-01"})
    assert json.loads(body_id) == data


def test_query_valid_field_with_no_match_still_returns_empty(seeded_with_goals):
    """OVER-REFUSAL GUARD — the failure mode this change could introduce.

    `category` is carried by every seeded goal, so a value that matches none of
    them is a genuine empty result and MUST stay HTTP 200 + []. If this ever
    starts raising, the refusal has widened from 'key absent everywhere' to
    'nothing matched' — trading a silent false zero for a loud false failure,
    which is not an improvement.
    """
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "category",
                    "goal_field_value": "no-such-category"})
    assert json.loads(body) == []


@pytest.mark.parametrize("key,value", [("asp_id", "asp-001"), ("source", "world")])
def test_query_projection_only_key_refused_even_when_a_record_carries_it(
        seeded_with_goals, key, value):
    """DISCRIMINATING (): refused EVEN THOUGH  carries the key.

    This is the whole defect. The refusal used to be nested inside
    `if field_key is not None and not field_key_seen:` — reachable only when NO
    goal record carried the name. Stray literal `asp_id`/`source` fields exist on
    real records, so in production `field_key_seen` went True, the refusal was
    skipped, and the endpoint returned the stray rows with HTTP 200: a PLAUSIBLE
    NON-ZERO that trips none of the count-hazard reflexes a zero would. Measured
    `--goal-field asp_id asp-115` → 4 rows against a truth of 1477.

    The fixture now seeds those stray keys, so against the pre-fix code this test
    FAILS with 200 + one row instead of erroring — which is what makes it evidence
    rather than decoration. The value asserted is not "some rows" but "no rows at
    any count": a projection-only key can never be filtered on, so the only
    correct row count is *no answer at all*.
    """
    _, port = seeded_with_goals
    try:
        status, body = _get(port, "/v1/aspirations/query",
                            {"goal_field_name": key, "goal_field_value": value})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        detail = json.loads(e.read().decode("utf-8"))["detail"]
        assert "projection" in detail
        # Name the supported path, or the caller has an error and no next step.
        assert "aspirations-read.sh" in detail
    else:
        raise AssertionError(
            f"expected 400 for projection-only key {key!r}; got {status} with "
            f"body {body!r} — the stray-field path is live again"
        )


def test_stray_projection_key_is_really_on_the_raw_record(seeded_with_goals):
    """POSITIVE CONTROL for the test above — proves the fixture is discriminating.

    If g-001-02 does not actually carry the stray key, the refusal test above
    passes for the OLD wrong reason (key unseen) rather than the new right one
    (key seen and refused anyway), and the suite silently stops covering the
    defect the moment someone tidies the fixture.

    READS THE STORE FILE, NOT THE ENDPOINT — and that is the whole point. Asking
    the endpoint with `full=true` would assert nothing: `full` mode ATTACHES
    `asp_id` and `source` during projection, so those keys come back present
    whether or not the raw record holds them. That is precisely the trap that
    produced this defect's original misdiagnosis, which cited a `--full` read as
    proof the fields were "in the RAW record" and concluded the wrong defect
    class. The store file is the only surface where the distinction is visible.
    """
    project_root, _ = seeded_with_goals
    raw = (project_root / "world" / "aspirations.jsonl").read_text(encoding="utf-8")
    record = json.loads(raw.strip().splitlines()[0])
    goal = next(g for g in record["goals"] if g["id"] == "g-001-02")
    assert goal.get("asp_id") == "asp-001", (
        "fixture no longer carries a stray asp_id — the refusal test above is "
        "now vacuous (it would pass via the key-unseen path)"
    )
    assert goal.get("source") == "world"


def test_query_unknown_field_against_goalless_store_is_empty_not_400(running_daemon):
    """ZERO-GOALS GUARD — a store with no goal records cannot validate a field name.

    Uses `running_daemon` rather than `seeded_with_goals` precisely because the
    conftest seeds aspirations whose `goals` arrays are EMPTY. That store exists
    (so the 404 no_aspiration_store branch does not fire) while carrying zero goal
    records — the one state in which EVERY field name is unseen. Without the
    guard the refusal fires on all of them, so a fresh world answers every
    filtered query with a 400 and "no goals yet" is reported as a caller error.

    The empty array is the honest answer here, and it is the same answer the
    endpoint gave before the refusal existed — this pin is what keeps the loud
    half of g-115-5752 from widening into the quiet half's territory.
    """
    _, port = running_daemon
    status, body = _get(port, "/v1/aspirations/query",
                        {"goal_field_name": "zzz_not_a_field",
                         "goal_field_value": "x"})
    assert status == 200
    assert json.loads(body) == []


# ---------------------------------------------------------------------------
# Filter: BOOLEAN goal fields ()
#
# guard-2353: a boundary move gets no evidence from a pre-existing suite unless
# some fixture row lies on the FAR SIDE of the old boundary. None did — the word
# "recurring" appeared zero times in this file — so the fixture above was
# extended and these rows ARE the discriminating set: every assertion marked
# DISCRIMINATING below returned [] under `str(actual) != value`.
# ---------------------------------------------------------------------------

def test_query_bool_field_json_spelling_matches(seeded_with_goals):
    """DISCRIMINATING: `true` is the JSON/YAML spelling the store holds on disk,
    and it was the one input that could never match (g-115-3965)."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "recurring", "goal_field_value": "true"})
    assert sorted(r["goal_id"] for r in json.loads(body)) == ["g-001-01", "g-100-01"]


def test_query_bool_field_spellings_agree(seeded_with_goals):
    """true / True / 1 must return the SAME non-zero set. Only `True` worked before."""
    _, port = seeded_with_goals
    seen = []
    for spelling in ("true", "True", "1"):
        _, body = _get(port, "/v1/aspirations/query",
                       {"goal_field_name": "recurring", "goal_field_value": spelling})
        seen.append(sorted(r["goal_id"] for r in json.loads(body)))
    assert seen[0] == ["g-001-01", "g-100-01"], seen
    assert seen[0] == seen[1] == seen[2], seen


def test_query_bool_field_false_matches_false_only(seeded_with_goals):
    """`false` must select the False-valued goal and EXCLUDE the True ones —
    the truthiness trap: not everything, not nothing."""
    _, port = seeded_with_goals
    for spelling in ("false", "False", "0"):
        _, body = _get(port, "/v1/aspirations/query",
                       {"goal_field_name": "recurring", "goal_field_value": spelling})
        ids = sorted(r["goal_id"] for r in json.loads(body))
        assert ids == ["g-001-02"], (spelling, ids)


def test_query_bool_field_absent_is_not_false(seeded_with_goals):
    """A goal LACKING the field is not thereby False — absence stays unmatched."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "recurring", "goal_field_value": "false"})
    ids = {r["goal_id"] for r in json.loads(body)}
    assert "g-100-02" not in ids and "g-100-03" not in ids, ids


def test_query_bool_field_nonsense_value_matches_nothing(seeded_with_goals):
    """An unrecognised spelling must match NOTHING, never everything."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "recurring", "goal_field_value": "banana"})
    assert json.loads(body) == []


def test_query_string_field_holding_true_is_not_coerced(seeded_with_goals):
    """RECALL CONTROL (guard-958).  carries verified="true" as a STRING.
    It must match "true" exactly and NOT the other boolean spellings — the bool
    normalization is scoped by the ACTUAL field's type, not applied to input."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"goal_field_name": "verified", "goal_field_value": "true"})
    assert sorted(r["goal_id"] for r in json.loads(body)) == ["g-001-01"]
    for spelling in ("True", "1", "yes"):
        _, body = _get(port, "/v1/aspirations/query",
                       {"goal_field_name": "verified", "goal_field_value": spelling})
        assert json.loads(body) == [], spelling


# ---------------------------------------------------------------------------
# Filter: description_contains ()
#
# The duplication GATE that refuses a filing already reads descriptions; the
# probe an agent runs BEFORE filing could only read titles. That asymmetry cost
# 5 duplicate filings by 4 agents in 7 days — every sibling was worded
# differently, so every title search came back clean and every filing looked
# novel. These pin the probe to the gate's own width.
# ---------------------------------------------------------------------------

def test_query_description_contains_finds_what_title_search_cannot(seeded_with_goals):
    """The core defect: a token present ONLY in descriptions.

    The title search is the positive control — it must return [] for the same
    token, or this test would pass even if the filter secretly read titles.
    """
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"description_contains": "widget_sentinel"})
    ids = sorted(r["goal_id"] for r in json.loads(body))
    assert ids == ["g-001-01", "g-100-03"]

    _, title_body = _get(port, "/v1/aspirations/query",
                         {"title_contains": "widget_sentinel"})
    assert json.loads(title_body) == [], (
        "positive control failed: the token leaked into a title, so the "
        "description filter is not what produced the rows above"
    )


def test_query_description_contains_is_case_insensitive(seeded_with_goals):
    """Mirror --title-contains semantics exactly — one shape to learn."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"description_contains": "WIDGET_SENTINEL"})
    ids = sorted(r["goal_id"] for r in json.loads(body))
    assert ids == ["g-001-01", "g-100-03"]


def test_query_description_contains_composes_with_goal_status(seeded_with_goals):
    """Composition is explicitly required: naive flag composition in this script
    is already broken elsewhere (g-115-5128), so the new flag must be pinned
    against a second filter, not just alone."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query",
                   {"description_contains": "widget_sentinel",
                    "goal_status": "pending"})
    ids = [r["goal_id"] for r in json.loads(body)]
    assert ids == ["g-001-01"], "AND semantics must drop the blocked g-100-03"


def test_query_description_contains_survives_null_and_absent_descriptions(seeded_with_goals):
    """Most goals carry no description; a large minority carry an explicit null.

    Both shapes are in the fixture (g-100-01 null, g-100-02 absent). A miss must
    return [] with HTTP 200 — never a 500 from .lower() on None.
    """
    _, port = seeded_with_goals
    status, body = _get(port, "/v1/aspirations/query",
                        {"description_contains": "no-goal-carries-this-string"})
    assert status == 200
    assert json.loads(body) == []


def test_query_description_contains_alone_satisfies_the_filter_requirement(seeded_with_goals):
    """It must count as a filter — otherwise it 400s as missing_filter and the
    flag is unusable on its own, which is the primary dedup-probe shape."""
    _, port = seeded_with_goals
    status, body = _get(port, "/v1/aspirations/query",
                        {"description_contains": "widget_sentinel"})
    assert status == 200
    assert len(json.loads(body)) == 2


def test_query_title_contains_unchanged_by_the_new_filter(seeded_with_goals):
    """Regression pin: --title-contains behaviour is unchanged (goal's own
    verification criterion)."""
    _, port = seeded_with_goals
    _, body = _get(port, "/v1/aspirations/query", {"title_contains": "ENCOD"})
    ids = sorted(r["goal_id"] for r in json.loads(body))
    assert ids == ["g-001-01", "g-100-01", "g-100-02"]
