"""Pin the NINE reason codes of `_cross_box_body_liveness` ().

The five end-to-end cases in `core/scripts/tests/test_claim_cross_box_body_holder.py`
pin the WRAPPER's boolean — which claims are refused and which are permitted. They
say nothing about the reason strings, and the reason strings ARE the deliverable of
g-306-328: they are what `meta/gate-firings.jsonl` keys on, and the next decision on
this predicate is made by reading that distribution.

The distinction under test is the one `guard-2223` exists for. `False` and `None`
BOTH permit the claim today, so no behavioural test can separate them — only these
assertions can:

    False  the store SAID this Body is not on this goal   (grounded)
    None   the store was never reached, or has no row     (NOT evidence of dormancy)

If a future edit collapses `None` back into `False`, every one of these fails and
the telemetry stops being able to answer the question it was built for.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mind_api.src.endpoints import aspirations_write

AGENT = "alpha"
SID = "d1aec55b-0000-0000-0000-000000000000"
GOAL = "g-306-323"
STALE_MIN = 30.0


def _ctx(tmp_path: Path):
    """Minimal ctx: the helper touches only `paths.project_root` and `paths.world`."""
    paths = types.SimpleNamespace(
        project_root=Path.cwd(),
        world=tmp_path / "world",
        meta=tmp_path / "meta",
        agent_name=AGENT,
    )
    return types.SimpleNamespace(paths=paths)


def _stamp(minutes_ago: float = 0.0) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S")


@pytest.fixture
def shard(monkeypatch):
    """Replace `_team_state.read_shard_authoritative` with a settable stub.

    The helper does `from _team_state import read_shard_authoritative` INSIDE its
    try-block after inserting core/scripts on sys.path, so the name is resolved at
    CALL time off the module object — patching the attribute is sufficient and no
    real shard/backend is touched.
    """
    sys.path.insert(0, str(Path.cwd() / "core" / "scripts"))
    import _team_state  # noqa: E402

    box = {"value": None, "raises": None}

    def _fake(world, agent_name):
        if box["raises"] is not None:
            raise box["raises"]
        return box["value"]

    monkeypatch.setattr(_team_state, "read_shard_authoritative", _fake)
    return box


def _call(tmp_path, *, goal=GOAL, agent=AGENT, sid=SID):
    return aspirations_write._cross_box_body_liveness(
        _ctx(tmp_path), agent, sid, goal, STALE_MIN)


# ── the two GROUNDED verdicts: the store actually answered ───────────────────

def test_live_row_refuses(tmp_path, shard):
    shard["value"] = {"in_flight_bodies": {
        SID: {"goal_id": GOAL, "claimed_at": _stamp(1)}}}
    assert _call(tmp_path) == (True, "live")


def test_row_naming_another_goal_is_grounded_dormant(tmp_path, shard):
    shard["value"] = {"in_flight_bodies": {
        SID: {"goal_id": "g-999-99", "claimed_at": _stamp(1)}}}
    verdict, reason = _call(tmp_path)
    assert (verdict, reason) == (False, "row_other_goal")


def test_stale_row_is_grounded_dormant(tmp_path, shard):
    shard["value"] = {"in_flight_bodies": {
        SID: {"goal_id": GOAL, "claimed_at": _stamp(STALE_MIN * 10)}}}
    verdict, reason = _call(tmp_path)
    assert (verdict, reason) == (False, "stale")


# ── the UNANSWERED family: absent evidence, NOT dormancy ────────────────────
# Each of these permits the claim today (wrapper maps None -> False). The point
# of the assertions is that they must never be reported AS dormancy.

@pytest.mark.parametrize("kwargs,expected", [
    ({"goal": ""},  "no_ids"),
    ({"agent": ""}, "no_ids"),
    ({"sid": ""},   "no_ids"),
])
def test_missing_ids_are_unanswered(tmp_path, shard, kwargs, expected):
    shard["value"] = {"in_flight_bodies": {}}
    assert _call(tmp_path, **kwargs) == (None, expected)


@pytest.mark.parametrize("row,expected", [
    (None,                              "shard_unreadable"),
    ("not-a-dict",                      "shard_unreadable"),
    ({},                                "no_bodies_map"),
    ({"in_flight_bodies": None},        "no_bodies_map"),
    ({"in_flight_bodies": ["list"]},    "no_bodies_map"),
    ({"in_flight_bodies": {}},          "no_row_for_sid"),
    ({"in_flight_bodies": {"other-sid": {"goal_id": GOAL}}}, "no_row_for_sid"),
])
def test_store_shape_failures_are_unanswered(tmp_path, shard, row, expected):
    shard["value"] = row
    verdict, reason = _call(tmp_path)
    assert verdict is None, (
        f"{expected!r} must be UNANSWERED, never a dormancy verdict "
        f"(guard-2223); got verdict={verdict!r}")
    assert reason == expected


def test_no_row_for_sid_is_the_structural_case(tmp_path, shard):
    """The single most important assertion in this file.

    `in_flight_bodies` is written FAIL-OPEN (coordination.md:1035: a failed
    body-row write logs a WARN and does NOT fail the claim; with no MIND_SID no
    row is attempted at all). So an absent row is what a BROKEN WRITER and a
    DORMANT BODY look like identically from here — and it is precisely the state
    the 2026-08-19 duplicate-claim collision was decided on.
    """
    shard["value"] = {"in_flight_bodies": {"someone-else": {"goal_id": GOAL}}}
    verdict, reason = _call(tmp_path)
    assert verdict is None
    assert reason == "no_row_for_sid"
    assert verdict is not False, (
        "collapsing no_row_for_sid to False re-creates the g-306-328 defect")


@pytest.mark.parametrize("claimed_at,expected", [
    (None,          "no_claimed_at"),
    ("",            "no_claimed_at"),
    ("not-a-date",  "unparseable_claimed_at"),
    ("2026-13-45T99:99:99", "unparseable_claimed_at"),
])
def test_unusable_timestamp_is_unanswered(tmp_path, shard, claimed_at, expected):
    shard["value"] = {"in_flight_bodies": {
        SID: {"goal_id": GOAL, "claimed_at": claimed_at}}}
    assert _call(tmp_path) == (None, expected)


def test_probe_exception_is_unanswered_and_names_the_type(tmp_path, shard):
    """A raise must NOT arrive as a dormancy verdict, and the reason must carry
    the exception type — the wrapper routes exactly this case to
    `decision="fail_open"`, which gate-retirement-eval investigates on."""
    shard["raises"] = RuntimeError("s3 timeout")
    verdict, reason = _call(tmp_path)
    assert verdict is None
    assert reason.startswith("probe_error:RuntimeError"), reason
    assert "s3 timeout" in reason


# ── the wrapper contract: behaviour is UNCHANGED (blast radius zero) ─────────

@pytest.mark.parametrize("row,expected_bool", [
    ({"in_flight_bodies": {SID: {"goal_id": GOAL, "claimed_at": _stamp(1)}}}, True),
    ({"in_flight_bodies": {SID: {"goal_id": "g-9-9", "claimed_at": _stamp(1)}}}, False),
    ({"in_flight_bodies": {}}, False),
    (None, False),
])
def test_wrapper_still_returns_a_plain_bool(tmp_path, shard, row, expected_bool,
                                            monkeypatch):
    """`None` must collapse to False at the wrapper, so no claim that succeeds
    today is newly refused. Telemetry is stubbed out — this asserts the decision,
    not the logging."""
    monkeypatch.setattr(aspirations_write, "_log_cross_box_body_liveness",
                        lambda *a, **k: None)
    shard["value"] = row
    got = aspirations_write._cross_box_body_is_live(
        _ctx(tmp_path), AGENT, SID, GOAL, STALE_MIN)
    assert got is expected_bool


# ── integration path: does a firing actually REACH gate-firings.jsonl? ───────

@pytest.mark.parametrize("row,exp_decision,exp_class", [
    ({"in_flight_bodies": {SID: {"goal_id": GOAL, "claimed_at": _stamp(1)}}},
     "block", "live"),
    ({"in_flight_bodies": {SID: {"goal_id": "g-9-9", "claimed_at": _stamp(1)}}},
     "pass", "dormant"),
    ({"in_flight_bodies": {}}, "pass", "unanswered"),
])
def test_firing_reaches_the_store(project_root, shard, row, exp_decision, exp_class,
                                  monkeypatch):
    """The wrapper's telemetry is the DELIVERABLE of , and the unit tests
    above stub it out — so without this, "all outcomes are logged" is an untested
    claim about the one thing the next decision reads.

    Exercises trigger (a claim contest) -> predicate -> `_gate_log.log` -> a row on
    disk, against the conftest project fixture's meta dir so nothing touches the
    live store.

    USE `project_root`, NOT a bare `tmp_path`. `_gate_log.log` appends via
    `locked_append_jsonl`, which REFUSES a path that is not under a configured
    root — and `log()` is documented best-effort and NEVER raises, so that refusal
    is swallowed and the firing simply does not appear. A bare-tmp version of this
    test failed 4/4 while the code was provably correct (same call in a plain
    subprocess wrote the row). The failure mode is the one this whole goal is
    about: absence of evidence read as evidence of absence.
    """
    import json
    # `_gate_log.log` is a DELIBERATE no-op under pytest (: it returns
    # early whenever PYTEST_CURRENT_TEST is set) so tests cannot pollute the real
    # firings store. `GATE_LOG_ALLOW_PYTEST` is the documented escape hatch, and
    # an integration test of the logging path is exactly what it is for. Without
    # it this test fails against provably-correct code — the suppression and a
    # broken writer are byte-identical from the assertion's side.
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    meta = project_root / "meta"
    ctx = _ctx(project_root)
    ctx.paths.meta = meta
    shard["value"] = row

    aspirations_write._cross_box_body_is_live(ctx, AGENT, SID, GOAL, STALE_MIN)

    rows = []
    for p in meta.rglob("*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    mine = [r for r in rows if r.get("gate_id") == "cross-box-body-liveness"]
    assert mine, (
        "no cross-box-body-liveness firing reached the store — the telemetry "
        f"half of g-306-328 is inert. Files seen: {[p.name for p in meta.rglob('*.jsonl')]}")
    rec = mine[-1]
    assert rec["decision"] == exp_decision, rec
    assert (rec.get("extra") or {}).get("verdict_class") == exp_class, rec
    assert (rec.get("extra") or {}).get("holder_sid") == SID, rec


def test_probe_error_populates_gate_error_not_just_extra(project_root, shard,
                                                         monkeypatch):
    """`fail_open` is the one decision gate-retirement-eval investigates on, so the
    raised branch must be distinguishable in the FIELD the evaluator reads."""
    import json
    # `_gate_log.log` is a DELIBERATE no-op under pytest (: it returns
    # early whenever PYTEST_CURRENT_TEST is set) so tests cannot pollute the real
    # firings store. `GATE_LOG_ALLOW_PYTEST` is the documented escape hatch, and
    # an integration test of the logging path is exactly what it is for. Without
    # it this test fails against provably-correct code — the suppression and a
    # broken writer are byte-identical from the assertion's side.
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    meta = project_root / "meta"
    ctx = _ctx(project_root)
    ctx.paths.meta = meta
    shard["raises"] = RuntimeError("boom")

    aspirations_write._cross_box_body_is_live(ctx, AGENT, SID, GOAL, STALE_MIN)

    rows = [json.loads(l) for p in meta.rglob("*.jsonl")
            for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    mine = [r for r in rows if r.get("gate_id") == "cross-box-body-liveness"]
    assert mine, "raised branch logged nothing at all"
    rec = mine[-1]
    assert rec["decision"] == "fail_open", rec
    assert rec.get("gate_error"), (
        "gate_error must carry the exception for the one decision value the "
        f"retirement evaluator investigates on; got {rec}")
    assert "boom" in str(rec["gate_error"])
