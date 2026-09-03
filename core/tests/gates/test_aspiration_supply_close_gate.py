"""Closure gate for supply-gated aspirations ().

The creation gate (test_aspiration_supply_gate.py) decides whether a
self-generated aspiration may be FILED. This twin decides whether a record
carrying supply_evidence.needle may be CLOSED. Fixture: the coach asp-025
shape measured 2026-09-03 — one goal, needle present, no intent block,
archived through the plain all-goals-terminal path ~75 min after creation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "core" / "scripts"))

from gates import aspiration_supply as gate  # noqa: E402

NEEDLE = ("weekly matchup-specific start/sit recommendations backed by documented "
          "defensive weakness analysis")


def _asp_025():
    return {
        "id": "asp-025", "title": "Scouting database sources while the feed is blocked",
        "origin_signal": "all_blocked_gap", "status": "active", "scope": "sprint",
        "motivation": "Build the scouting database of sources for the coaching workflow.",
        "supply_evidence": {
            "gap": "No compiled list of scouting sources with documented defensive tendencies exists.",
            "needle": NEEDLE, "needle_by": "2026-09-14",
            "checked": ["asp-024", "intelligence/qb-evaluation-framework.md"],
        },
        "goals": [{"id": "g-025-01", "title": "Compile scouting database sources", "status": "completed"}],
    }


GOOD_STATEMENT = ("Every matchup now has start/sit recommendations for the week, each backed by the "
                  "documented defensive weakness analysis in the report the operator reads.")


@pytest.fixture
def world(tmp_path):
    w = tmp_path / "world"
    (w / "reports").mkdir(parents=True)
    (w / "reports" / "week1-start-sit.md").write_text("# Week 1 start/sit\n", encoding="utf-8")
    return w


def _eval(asp, world, **kw):
    kw.setdefault("world_dir", world)
    kw.setdefault("project_root", world.parent)
    return gate.evaluate_close(asp, **kw)


def test_plain_close_is_refused_naming_the_needle(world):
    r = _eval(_asp_025(), world)
    assert r["gated"] and r["would_block"]
    assert [f["check"] for f in r["failures"]] == ["needle_unaddressed"]
    assert NEEDLE in r["failures"][0]["detail"] and "2026-09-14" in r["failures"][0]["detail"]
    assert "--needle-satisfied" in r["remedy"] and "parent_aspiration:asp-025" in r["remedy"]
    assert "aspirations-retire.sh asp-025" in r["remedy"]
    assert r["checks"]["needle"] == NEEDLE and r["checks"]["mode"] == "needle"


def test_not_gated_without_a_needle(world):
    asp = _asp_025()
    del asp["supply_evidence"]
    r = _eval(asp, world)
    assert r == {"would_block": False, "gated": False,
                 "reason": "not gated (no supply_evidence.needle)",
                 "failures": [], "checks": {}, "remedy": None}
    asp = _asp_025()
    asp["supply_evidence"]["needle"] = "   "
    assert _eval(asp, world)["gated"] is False
    # config knob
    assert _eval(_asp_025(), world, config={"enabled": False})["gated"] is False


def test_needle_block_is_checked_on_content_not_presence(world):
    bad = {"statement": "done", "artifacts": ["g-025-01"]}
    r = _eval(_asp_025(), world, satisfaction=bad, existing_ids={"g-025-01"})
    checks = {f["check"] for f in r["failures"]}
    assert checks == {"needle_statement_short", "needle_statement_disjoint", "needle_artifact_unverified"}
    art = r["checks"]["artifacts"][0]
    assert art["ref"] == "g-025-01" and art["verified"] is False and "not an artifact" in art["note"]

    good = {"statement": GOOD_STATEMENT, "artifacts": ["reports/week1-start-sit.md"]}
    r = _eval(_asp_025(), world, satisfaction=good)
    assert r["gated"] and not r["would_block"], r["failures"]
    assert r["checks"]["artifacts"][0]["verified"] is True
    assert len(r["checks"]["shared_tokens"]) >= 2
    assert r["remedy"] is None


def test_a_missing_artifact_path_does_not_resolve(world):
    block = {"statement": GOOD_STATEMENT, "artifacts": ["reports/does-not-exist.md"]}
    r = _eval(_asp_025(), world, satisfaction=block)
    assert [f["check"] for f in r["failures"]] == ["needle_artifact_unverified"]
    assert "does-not-exist.md" in r["failures"][0]["detail"]


def test_intent_mode_needs_the_rationale_to_address_the_needle(world):
    on_motivation_only = {"evidence_goal_ids": ["g-025-01"], "superseded_goal_ids": [],
                          "rationale": "The scouting database of sources for the coaching workflow "
                                       "is compiled and validated across every provider we track."}
    r = _eval(_asp_025(), world, satisfaction=on_motivation_only, mode="intent")
    assert [f["check"] for f in r["failures"]] == ["needle_statement_disjoint"]
    on_needle = dict(on_motivation_only, rationale=GOOD_STATEMENT)
    r = _eval(_asp_025(), world, satisfaction=on_needle, mode="intent")
    assert not r["would_block"] and "artifacts" not in r["checks"]  # no artifact demanded here


def test_thresholds_are_config(world):
    good = {"statement": GOOD_STATEMENT, "artifacts": ["reports/week1-start-sit.md"]}
    r = _eval(_asp_025(), world, satisfaction=good, config={"close_min_shared_tokens": 99})
    assert [f["check"] for f in r["failures"]] == ["needle_statement_disjoint"]
    r = _eval(_asp_025(), world, satisfaction=good, config={"close_min_statement_chars": 10_000})
    assert [f["check"] for f in r["failures"]] == ["needle_statement_short"]


def test_override_is_ledgered_as_a_close_record(world):
    r = _eval(_asp_025(), world, override_close="operator re-filed the needle under asp-031", agent_name="alpha")
    assert r["gated"] and not r["would_block"] and r["override_applied"]
    rows = [json.loads(l) for l in (world / gate.LEDGER_NAME).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["gate"] == gate.GATE_ID_CLOSE and rows[0]["kind"] == "close"
    assert rows[0]["asp_id"] == "asp-025" and rows[0]["failures"] == ["needle_unaddressed"]
    assert rows[0]["agent"] == "alpha" and rows[0]["needle"] == NEEDLE
    # a pass writes nothing
    good = {"statement": GOOD_STATEMENT, "artifacts": ["reports/week1-start-sit.md"]}
    _eval(_asp_025(), world, satisfaction=good, override_close="unused")
    assert len((world / gate.LEDGER_NAME).read_text(encoding="utf-8").splitlines()) == 1
