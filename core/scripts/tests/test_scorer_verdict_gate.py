"""test_scorer_verdict_gate.py — Scorer Sovereignty Layer B ().

Covers the claim chokepoint gate's pure decision core (`evaluate`) across every
branch, the closed deviation enum, the freshness boundary, the
`write_scorer_verdict` sidecar writer, and the 2026-07-20T22:24 counterfactual
replay (a claim of g-115-2798 while the scorer top was g-315-390 must be
refused). The gate's load-bearing safety property is FAIL-OPEN: a missing,
malformed, or stale verdict allows without validation so a broken selector
never wedges claiming.

g-375-133 adds one allowance and pins it from both sides: a worker's claim of a
row its own select-walk KEPT, over a top that walk DROPPED, passes with no code
(logged as an override), and without every piece of that evidence the deny
stands exactly as before.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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

def _census(top="g-1", dropped=True, rows=("g-2", "g-3"), age_s=60, now=NOW):
    """A select-walk census (worker_execute.worker_view + write_select_census)."""
    return {"top": 10, "scorer_top": top, "scorer_top_dropped": dropped,
            "rows": [{"goal_id": g, "verdict": "eligible"} for g in rows],
            "ts": (now - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S")}


def _branch_cases():
    """(label, verdict, claimed, code, census) for every branch `_classify` can
    reach from a direct call. `path_resolution_failed` is main()-only — no
    verdict is ever built on that path — and is covered by the completeness test
    below."""
    return [
        ("no_verdict",             None,                                      "g-2", "", None),
        ("malformed_verdict",      {"ts": NOW.strftime("%Y-%m-%dT%H:%M:%S")}, "g-2", "", None),
        ("stale_verdict",          _verdict("g-1", ts=NOW - timedelta(minutes=11)), "g-2", "", None),
        ("top_pick_match",         _verdict("g-1"),                           "g-1", "", None),
        ("unsanctioned_deviation", _verdict("g-1"),                           "g-2", "", None),
        ("sanctioned_deviation",   _verdict("g-1"),         "g-2", "self-abstention", None),
        ("walk_dropped_top",       _verdict("g-1"),                     "g-2", "", _census()),
    ]


def test_classify_emits_expected_branch_label():
    """Every branch of the decision core returns its own stable label — the
    telemetry discriminator guard-502 requires (no two branches conflated)."""
    for want, verdict, claimed, code, census in _branch_cases():
        got = svg._classify(verdict, claimed, code, NOW, census=census, census_now=NOW)[0]
        assert got == want, f"expected {want}, got {got}"


def test_branch_labels_are_unique():
    """guard-502: a shared label would silently merge two branches in the
    firing log, which is the failure this instrumentation exists to prevent."""
    labels = [c[0] for c in _branch_cases()]
    assert len(labels) == len(set(labels))


def test_evaluate_is_a_faithful_facade_over_classify():
    """`evaluate` keeps its 3-tuple contract and must never disagree with
    `_classify` — the branch logic lives in exactly one place."""
    for _want, verdict, claimed, code, census in _branch_cases():
        path, rc, msg, ev = svg._classify(verdict, claimed, code, NOW,
                                          census=census, census_now=NOW)
        assert svg.evaluate(verdict, claimed, code, NOW,
                            census=census, census_now=NOW) == (rc, msg, ev)
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
    assert d["walk_dropped_top"] == "override"       # the walk's census is the bypass
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
        svg._log_gate_firing("walk_dropped_top", "alpha", "g-2", "g-1", "self-abstention")
    finally:
        gate_log.log = orig
    assert seen[0] == ("fail_open", None)
    assert seen[1] == ("override", "self-abstention")
    assert seen[2] == ("override", "self-abstention")


# ── the walk's census as evidence () ──────────────────────────
#
# Measured 2026-10-03..05 on the zc worker Bodies: the scorer's top pick was a
# row the worker's select-walk had dropped (a structural no_claim), so the
# walk's first kept row drew this gate's deny, and the Body chased the top it
# could never take. The census select-walk writes is the evidence that lets the
# gate tell that claim apart from a real unsanctioned divergence.

def test_a_kept_row_over_a_top_the_walk_dropped_passes_with_no_code():
    path, rc, msg, ev = svg._classify(_verdict("g-1"), "g-2", "", NOW,
                                      census=_census(), census_now=NOW)
    assert (path, rc, msg) == ("walk_dropped_top", 0, "")
    assert ev == {"claimed": "g-2", "scorer_top": "g-1", "code": "self-abstention"}
    assert ev["code"] in svg.VALID_DEVIATION_CODES, "the logged code is one the audit knows"


@pytest.mark.parametrize("census, why", [
    (None, "no census"),
    ("not-a-mapping", "a census that is not a mapping"),
    (_census(top="g-9"), "the census judged a different top"),
    (_census(dropped=False), "the walk kept the top"),
    (dict(_census(), scorer_top_dropped="yes"), "dropped must be the boolean true"),
    (_census(rows=("g-3",)), "the claimed goal is not a kept row"),
    (dict(_census(), rows="g-2"), "rows that are not a list"),
    (_census(age_s=11 * 60), "a census older than the verdict window"),
    (dict(_census(), ts="not-a-timestamp"), "a census with no readable ts"),
])
def test_without_every_piece_of_walk_evidence_the_deny_stands(census, why):
    path, rc, msg, ev = svg._classify(_verdict("g-1"), "g-2", "", NOW,
                                      census=census, census_now=NOW)
    assert (path, rc, ev) == ("unsanctioned_deviation", 2, None), why
    assert "g-1" in msg and "g-2" in msg


def test_the_census_ages_on_its_own_utc_clock():
    """The census is stamped in naive UTC; `census_now` is what it is aged against,
    so a box whose local clock is not UTC cannot age it wrongly. At the window's
    edge it is still fresh, one second past it is not."""
    edge = _census(age_s=10 * 60)
    assert svg._classify(_verdict("g-1"), "g-2", "", NOW, census=edge,
                         census_now=NOW)[0] == "walk_dropped_top"
    later = NOW + timedelta(seconds=1)
    assert svg._classify(_verdict("g-1"), "g-2", "", NOW, census=edge,
                         census_now=later)[0] == "unsanctioned_deviation"


def test_a_census_with_no_clock_to_age_it_by_is_no_evidence():
    """A caller that passes a census but no `census_now` gets the deny: the census is
    never aged on `now`, the verdict's local clock, in its place."""
    path, rc, _, ev = svg._classify(_verdict("g-1"), "g-2", "", NOW, census=_census())
    assert (path, rc, ev) == ("unsanctioned_deviation", 2, None)


def test_a_code_the_caller_passes_still_decides():
    """The census sanctions only the claim that names NO code. A valid code takes
    the ordinary sanctioned branch with the caller's own code; an unknown code is
    still refused, census or not, so the deny keeps teaching the enum."""
    path, rc, _, ev = svg._classify(_verdict("g-1"), "g-2", "partner-claim", NOW,
                                    census=_census(), census_now=NOW)
    assert (path, rc, ev["code"]) == ("sanctioned_deviation", 0, "partner-claim")
    path, rc, msg, _ = svg._classify(_verdict("g-1"), "g-2", "nope", NOW,
                                     census=_census(), census_now=NOW)
    assert (path, rc) == ("unsanctioned_deviation", 2) and "nope" in msg


def test_the_census_changes_no_other_branch():
    census = _census()
    assert svg._classify(_verdict("g-1"), "g-1", "", NOW, census=census,
                         census_now=NOW)[0] == "top_pick_match"
    stale = _verdict("g-1", ts=NOW - timedelta(minutes=11))
    assert svg._classify(stale, "g-2", "", NOW, census=census,
                         census_now=NOW)[0] == "stale_verdict"
    assert svg._classify(None, "g-2", "", NOW, census=census,
                         census_now=NOW)[0] == "no_verdict"


def test_the_census_file_name_is_the_walk_writer_s():
    """The gate keeps its own copy of the name (it does not import worker_execute
    on the claim path); this pins the two equal."""
    import worker_execute as we
    assert svg.SELECT_CENSUS_FILENAME == we.SELECT_CENSUS_FILENAME


def _run_main(tmp_path, monkeypatch, census, claimed="g-2", top="g-1", code=""):
    """main() with an explicit verdict and census and the two loggers captured, so
    nothing reaches a real diary or gate firing store."""
    vf = tmp_path / "scorer-verdict.json"
    vf.write_text(json.dumps(_verdict(top, ts=datetime.now())), encoding="utf-8")
    argv = ["--agent", "alpha", "--goal-id", claimed, "--verdict-file", str(vf)]
    if census is not None:
        cf = tmp_path / "select-census.json"
        cf.write_text(json.dumps(census), encoding="utf-8")
        argv += ["--census-file", str(cf)]
    if code:
        argv += ["--deviation", code]
    firings, overrides = [], []
    monkeypatch.setattr(svg, "_log_gate_firing", lambda *a: firings.append(a))
    monkeypatch.setattr(svg, "_log_override", lambda ev, agent: overrides.append(ev))
    return svg.main(argv), firings, overrides


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_main_reads_the_census_and_logs_the_walk_branch_as_an_override(tmp_path, monkeypatch):
    rc, firings, overrides = _run_main(tmp_path, monkeypatch, _census(now=_utc_now()))
    assert rc == 0
    assert firings == [("walk_dropped_top", "alpha", "g-2", "g-1", "self-abstention")]
    assert overrides == [{"claimed": "g-2", "scorer_top": "g-1", "code": "self-abstention"}]


def test_main_with_no_census_refuses_exactly_as_before(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MIND_SID", raising=False)
    rc, firings, overrides = _run_main(tmp_path, monkeypatch, None)
    assert rc == 2 and overrides == []
    assert firings == [("unsanctioned_deviation", "alpha", "g-2", "g-1", "")]
    assert "g-1" in capsys.readouterr().err


def test_an_unreadable_or_absent_census_is_no_evidence(tmp_path, monkeypatch):
    cf = tmp_path / "broken.json"
    cf.write_text("{not json", encoding="utf-8")
    assert svg._load_census(str(cf), "alpha") is None
    assert svg._load_census(str(tmp_path / "absent.json"), "alpha") is None
    monkeypatch.delenv("MIND_SID", raising=False)
    assert svg._load_census("", "alpha") is None


def _brief(i, **extra):
    """A row in the selector's brief shape, as select-walk receives it."""
    row = {"goal_id": f"g-900-{i:02d}", "source": "world", "title": f"T{i}",
           "score": 10.0 - i / 100, "skill": None, "executable_by_role": None,
           "recurring": False, "routed_to_me": False}
    row.update(extra)
    return row


@pytest.mark.parametrize("top_skill, want_rc, want_path", [
    ("/reflect", 0, "walk_dropped_top"),        # the walk dropped the top
    (None, 2, "unsanctioned_deviation"),        # positive control: the walk kept it
])
def test_round_trip_through_the_real_walk_census(tmp_path, monkeypatch,
                                                 top_skill, want_rc, want_path):
    """The producer and the reader composed: worker_view's census, written by
    write_select_census, read by this gate's main(). A worker claims row 1 with no
    code. Only the walk that DROPPED row 0 sanctions that claim; the same claim
    after a walk that KEPT row 0 is refused as before."""
    import worker_execute as we
    ranked = [_brief(0, skill=top_skill), _brief(1), _brief(2)]
    _, census = we.worker_view(ranked, 10)
    sess = tmp_path / "sessions" / "sid-133"
    sess.mkdir(parents=True)
    we.write_select_census(census, sess)
    vf = tmp_path / "scorer-verdict.json"
    vf.write_text(json.dumps(_verdict("g-900-00", ts=datetime.now())), encoding="utf-8")
    firings = []
    monkeypatch.setattr(svg, "_log_gate_firing", lambda *a: firings.append(a))
    monkeypatch.setattr(svg, "_log_override", lambda ev, agent: None)
    rc = svg.main(["--agent", "alpha", "--goal-id", "g-900-01", "--verdict-file", str(vf),
                   "--census-file", str(sess / we.SELECT_CENSUS_FILENAME)])
    assert (rc, firings[0][0]) == (want_rc, want_path)
