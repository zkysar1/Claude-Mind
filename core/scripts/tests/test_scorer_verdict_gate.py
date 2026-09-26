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
    assert len(svg.VALID_DEVIATION_CODES) == 11
    assert "always-run-lane" in svg.VALID_DEVIATION_CODES  # 
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


# ── gate telemetry: branch labels + registry pairing () ──────

def _branch_cases():
    """(label, verdict, claimed, code) for every branch `_classify` can reach
    from a direct call. `path_resolution_failed` is main()-only — no verdict is
    ever built on that path — and is covered by the completeness test below."""
    return [
        ("no_verdict",             None,                                      "g-2", ""),
        ("malformed_verdict",      {"ts": NOW.strftime("%Y-%m-%dT%H:%M:%S")}, "g-2", ""),
        ("stale_verdict",          _verdict("g-1", ts=NOW - timedelta(minutes=11)), "g-2", ""),
        ("top_pick_match",         _verdict("g-1"),                           "g-1", ""),
        ("unsanctioned_deviation", _verdict("g-1"),                           "g-2", ""),
        ("sanctioned_deviation",   _verdict("g-1"),         "g-2", "self-abstention"),
    ]


def test_classify_emits_expected_branch_label():
    """Every branch of the decision core returns its own stable label — the
    telemetry discriminator guard-502 requires (no two branches conflated)."""
    for want, verdict, claimed, code in _branch_cases():
        got = svg._classify(verdict, claimed, code, NOW)[0]
        assert got == want, f"expected {want}, got {got}"


def test_branch_labels_are_unique():
    """guard-502: a shared label would silently merge two branches in the
    firing log, which is the failure this instrumentation exists to prevent."""
    labels = [c[0] for c in _branch_cases()]
    assert len(labels) == len(set(labels))


def test_evaluate_is_a_faithful_facade_over_classify():
    """`evaluate` keeps its 3-tuple contract and must never disagree with
    `_classify` — the branch logic lives in exactly one place."""
    for _want, verdict, claimed, code in _branch_cases():
        path, rc, msg, ev = svg._classify(verdict, claimed, code, NOW)
        assert svg.evaluate(verdict, claimed, code, NOW) == (rc, msg, ev)
        assert path in svg.DECISION_BY_PATH


def test_decision_by_path_covers_every_branch_and_uses_valid_decisions():
    """Every label maps to a decision, and every decision is one _gate_log
    accepts — an invalid value is silently coerced to fail_open, which would
    make `block` and `pass` indistinguishable in the log."""
    import importlib
    gate_log = importlib.import_module("_gate_log")
    reachable = {c[0] for c in _branch_cases()} | {"path_resolution_failed"}
    assert reachable == set(svg.DECISION_BY_PATH), (
        "DECISION_BY_PATH must cover exactly the reachable branch labels")
    for path, decision in svg.DECISION_BY_PATH.items():
        assert decision in gate_log._VALID_DECISIONS, f"{path} -> {decision}"


def test_decisions_match_caller_control_flow_effect():
    """guard-1743: the decision names the branch's effect AT THE CALLER
    (aspirations-claim.sh aborts on rc 2 and proceeds otherwise), not local
    intent. Deny -> block; sanctioned bypass -> override; validated allow ->
    pass; unvalidated allow -> fail_open."""
    d = svg.DECISION_BY_PATH
    assert d["unsanctioned_deviation"] == "block"    # caller exits 2
    assert d["sanctioned_deviation"] == "override"   # named bypass flag used
    assert d["top_pick_match"] == "pass"             # validated, claim proceeds
    for path in ("no_verdict", "malformed_verdict", "stale_verdict",
                 "path_resolution_failed"):
        assert d[path] == "fail_open", path


def test_gate_id_is_registered_in_gates_yaml():
    """_gate_log's contract: gate_id MUST match an `id` in core/config/gates.yaml
    or the retirement evaluator and gate-stats cannot see this gate's firings."""
    import yaml
    registry = yaml.safe_load(
        (CORE_SCRIPTS.parent / "config" / "gates.yaml").read_text(encoding="utf-8"))
    entries = [g for g in registry["gates"] if g.get("id") == svg.GATE_ID]
    assert len(entries) == 1, f"{svg.GATE_ID} not registered exactly once"
    entry = entries[0]
    assert entry["instrumented"] is True
    assert entry["script"] == "core/scripts/scorer-verdict-gate.py"


def test_log_gate_firing_never_raises():
    """Telemetry is best-effort and must NEVER affect the claim — including on
    a garbage decision path, a None agent, and an unknown label."""
    svg._log_gate_firing("no_verdict", "alpha", "g-1", "g-1", "")
    svg._log_gate_firing("not-a-real-path", None, None, None, None)
    svg._log_gate_firing(None, "", "", "", "")


def test_identifying_fields_travel_in_extra_not_payload(monkeypatch):
    """`_gate_log` stores `extra` VERBATIM but reduces `payload` to a
    `payload_hash`. The identifying fields — above all `scorer_top`, the goal
    the selector ranked first and this Body did not take — must therefore go in
    `extra`, or they reach no consumer. Caught in review 2026-09-06 after the
    first implementation put them in `payload`; pinned here because the failure
    is SILENT (rows still appear, they just answer nothing)."""
    import importlib
    gate_log = importlib.import_module("_gate_log")
    seen = {}

    def _capture(gate_id, decision, **kw):
        seen["gate_id"] = gate_id
        seen["decision"] = decision
        seen.update(kw)

    monkeypatch.setattr(gate_log, "log", _capture)
    svg._log_gate_firing("sanctioned_deviation", "alpha", "g-2", "g-1",
                         "self-abstention")

    assert seen["gate_id"] == svg.GATE_ID
    assert seen["decision"] == "override"
    assert "payload" not in seen, "identifying fields must not be hashed away"
    extra = seen["extra"]
    assert extra["scorer_top"] == "g-1"      # the skipped goal — the point
    assert extra["claimed"] == "g-2"
    assert extra["deviation"] == "self-abstention"
    assert extra["decision_path"] == "sanctioned_deviation"
    assert seen["override_reason"] == "self-abstention"


def test_override_reason_only_set_on_sanctioned_branch():
    """`override_reason` names the bypass actually used. A stray --deviation
    string on a non-override branch would inflate the override count and
    corrupt the FP ratio this instrumentation exists to make measurable."""
    import importlib
    gate_log = importlib.import_module("_gate_log")
    seen = []
    orig = gate_log.log
    try:
        gate_log.log = lambda gid, dec, **kw: seen.append((dec, kw.get("override_reason")))
        # a deviation code present on a branch that is NOT the sanctioned one
        svg._log_gate_firing("stale_verdict", "alpha", "g-2", "g-1", "self-abstention")
        svg._log_gate_firing("sanctioned_deviation", "alpha", "g-2", "g-1", "self-abstention")
    finally:
        gate_log.log = orig
    assert seen[0] == ("fail_open", None)
    assert seen[1] == ("override", "self-abstention")
