"""test_scorer_verdict_gate.py — Scorer Sovereignty Layer B ().

Covers the claim chokepoint gate's pure decision core (`evaluate`) across every
branch, the closed deviation enum, the freshness boundary, the
`write_scorer_verdict` sidecar writer, and the 2026-07-20T22:24 counterfactual
replay (a claim of g-115-2798 while the scorer top was g-315-390 must be
refused). The gate's load-bearing safety property is FAIL-OPEN: a missing,
malformed, or stale verdict allows without validation so a broken selector
never wedges claiming.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


svg = _load("scorer_verdict_gate", "scorer-verdict-gate.py")
gs = _load("goal_selector_svg", "goal-selector.py")

NOW = datetime(2026, 7, 21, 10, 0, 0)


def _verdict(top, ts=None, top5=None):
    return {
        "top_goal_id": top,
        "top_score": 9.9,
        "ts": (ts or NOW).strftime("%Y-%m-%dT%H:%M:%S"),
        "top_5": top5 or [{"goal_id": top, "score": 9.9}],
    }


# ── evaluate() branch matrix ──────────────────────────────────────────

def test_claim_top_no_flag_allows():
    """Happy path: claiming the scorer's top pick needs no deviation flag."""
    rc, msg, ev = svg.evaluate(_verdict("g-1"), "g-1", "", NOW)
    assert rc == 0 and msg == "" and ev is None


def test_deviate_valid_code_allows_and_returns_event():
    """A sanctioned deviation with a valid code allows AND returns the override
    event to log for the Layer C audit."""
    rc, msg, ev = svg.evaluate(_verdict("g-1"), "g-2", "self-abstention", NOW)
    assert rc == 0 and msg == ""
    assert ev == {"claimed": "g-2", "scorer_top": "g-1", "code": "self-abstention"}


def test_deviate_no_code_denies():
    """Diverging from the top pick with NO code is refused (exit 2)."""
    rc, msg, ev = svg.evaluate(_verdict("g-1"), "g-2", "", NOW)
    assert rc == 2 and ev is None
    assert "scorer-sovereignty" in msg and "g-1" in msg and "g-2" in msg


def test_deviate_unknown_code_denies():
    """An out-of-enum code is refused (closed enum — no free-text escape)."""
    rc, msg, ev = svg.evaluate(_verdict("g-1"), "g-2", "because-i-said-so", NOW)
    assert rc == 2 and ev is None
    assert "because-i-said-so" in msg


def test_stale_verdict_fail_open():
    """A verdict older than the freshness window allows without validation."""
    stale = _verdict("g-1", ts=NOW - timedelta(minutes=11))
    rc, msg, ev = svg.evaluate(stale, "g-2", "", NOW)
    assert rc == 0 and msg == "" and ev is None


def test_missing_verdict_fail_open():
    """No verdict (None) allows — a broken selector must never wedge claiming."""
    assert svg.evaluate(None, "g-2", "", NOW) == (0, "", None)


def test_malformed_verdict_fail_open():
    """A verdict with no usable top_goal_id allows (fail-open)."""
    ts = NOW.strftime("%Y-%m-%dT%H:%M:%S")
    assert svg.evaluate({"ts": ts}, "g-2", "", NOW) == (0, "", None)
    assert svg.evaluate({"top_goal_id": "", "ts": ts}, "g-2", "", NOW) == (0, "", None)
    assert svg.evaluate("not-a-dict", "g-2", "", NOW) == (0, "", None)


def test_unparseable_ts_fail_open():
    """A verdict with a garbage timestamp is treated as stale -> allow."""
    v = {"top_goal_id": "g-1", "ts": "not-a-timestamp"}
    assert svg.evaluate(v, "g-2", "", NOW) == (0, "", None)


def test_all_enum_codes_allow_on_divergence():
    """Every code in the closed enum is accepted on a divergence."""
    assert len(svg.VALID_DEVIATION_CODES) == 10
    for code in svg.VALID_DEVIATION_CODES:
        rc, _, ev = svg.evaluate(_verdict("g-1"), "g-2", code, NOW)
        assert rc == 0, code
        assert ev["code"] == code


def test_boundary_freshness_exactly_10min_gate_active():
    """At exactly the freshness edge (age == 10min) the verdict is still fresh
    (comparison is strictly-greater-than), so a no-code divergence still denies
    rather than fail-opening."""
    edge = _verdict("g-1", ts=NOW - timedelta(minutes=10))
    rc, _, _ = svg.evaluate(edge, "g-2", "", NOW)
    assert rc == 2


# ── 2026-07-20T22:24 counterfactual replay ────────────────────────────

def test_counterfactual_20260720_2224_denies():
    """Replay: the scorer top was ; a claim of  without a
    code MUST be refused. This is the concrete divergence the gate exists to
    prevent."""
    verdict = _verdict("g-315-390", top5=[
        {"goal_id": "g-315-390", "score": 12.1},
        {"goal_id": "g-115-2798", "score": 8.4},
    ])
    rc, msg, ev = svg.evaluate(verdict, "g-115-2798", "", NOW)
    assert rc == 2 and ev is None
    assert "g-315-390" in msg and "g-115-2798" in msg


# ── write_scorer_verdict sidecar writer (goal-selector.py) ─────────────

def test_write_scorer_verdict_schema(tmp_path):
    """write_scorer_verdict writes the sidecar with the exact schema the gate
    reads (top_goal_id / top_score / ts / top_5)."""
    scored = [
        {"goal_id": "g-1", "score": 9.87654},
        {"goal_id": "g-2", "score": 5.4},
        {"goal_id": "g-3", "score": 3.2},
    ]
    gs.write_scorer_verdict(scored, tmp_path)
    target = tmp_path / "session" / "scorer-verdict.json"
    assert target.exists()
    data = json.loads(target.read_text())
    assert data["top_goal_id"] == "g-1"
    assert data["top_score"] == 9.8765  # rounded to 4 places
    assert [e["goal_id"] for e in data["top_5"]] == ["g-1", "g-2", "g-3"]
    datetime.strptime(data["ts"], "%Y-%m-%dT%H:%M:%S")  # gate-parseable


def test_write_scorer_verdict_caps_top5(tmp_path):
    """top_5 holds at most 5 entries even when more goals are scored."""
    scored = [{"goal_id": f"g-{i}", "score": float(20 - i)} for i in range(8)]
    gs.write_scorer_verdict(scored, tmp_path)
    data = json.loads((tmp_path / "session" / "scorer-verdict.json").read_text())
    assert len(data["top_5"]) == 5
    assert data["top_goal_id"] == "g-0"


def test_write_scorer_verdict_empty_scored_noop(tmp_path):
    """Empty scored list writes nothing (no verdict to record)."""
    gs.write_scorer_verdict([], tmp_path)
    assert not (tmp_path / "session" / "scorer-verdict.json").exists()


def test_write_scorer_verdict_none_agent_dir_noop():
    """None agent_dir is a no-op and never raises."""
    gs.write_scorer_verdict([{"goal_id": "g-1", "score": 1.0}], None)


def test_verdict_roundtrip_writer_to_gate(tmp_path):
    """End-to-end: what the writer emits, the gate reads and gates on."""
    gs.write_scorer_verdict(
        [{"goal_id": "g-1", "score": 9.9}, {"goal_id": "g-2", "score": 1.0}],
        tmp_path)
    verdict = json.loads((tmp_path / "session" / "scorer-verdict.json").read_text())
    # Fresh verdict (ts = writer's now) — claiming a non-top goal w/o a code denies.
    assert svg.evaluate(verdict, "g-2", "", datetime.now())[0] == 2
    # Claiming the written top pick allows.
    assert svg.evaluate(verdict, "g-1", "", datetime.now())[0] == 0
