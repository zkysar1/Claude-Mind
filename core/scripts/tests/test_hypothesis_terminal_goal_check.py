#!/usr/bin/env python3
"""Tests for hypothesis-terminal-goal-check.py ().

Structured against the goal's five verification outcomes, plus the two defects
found by MEASUREMENT while building it (which is why they have tests at all —
neither was in the goal's criteria):

  P1  ownership: `claimed_by` outranks `intended_agent`. The first version read
      only `intended_agent` and would have routed THIS agent to close a live
      partner's work — measured on g-115-5328 / g-318-87, both
      `intended_agent: either` (reads as "mine") with `claimed_by: alpha`.
  P2  residual scope: the count-only heuristic MISSED its own motivating case
      (g-115-3668 has exactly ONE verification outcome; its residual is prose).

`_classify` is pure, so the replay in outcome #2 runs against the two named
historical instances' real shape without needing their pre-close queue state —
they are both `skipped` now, so a live sweep can no longer surface them and
only a replay can satisfy that criterion.
"""

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "hypothesis_terminal_goal_check",
    SCRIPTS / "hypothesis-terminal-goal-check.py")
htgc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(htgc)

NOW = dt.datetime(2026, 8, 10, 9, 0, 0)


def _goal(**kw):
    g = {
        "id": "g-000-1",
        "status": "pending",
        "hypothesis_id": None,
        "intended_agent": "either",
        "claimed_by": None,
        "priority": "MEDIUM",
        "title": "t",
        "description": "",
        "verification": {"outcomes": ["only one"]},
        "_source": "world",
        "_aspiration_id": "asp-000",
    }
    g.update(kw)
    return g


def _rec(stage, **kw):
    r = {"stage": stage, "outcome": "CONFIRMED", "outcome_date": "2026-08-01",
         "reflected": True, "resolved_by": "alpha"}
    r.update(kw)
    return r


# ---------------------------------------------------------------- outcome #1
def test_terminal_hypothesis_is_surfaced_with_outcome_fields():
    """#1: a detective check reports open goals whose hypothesis is terminal,
    surfacing outcome / outcome_date / reflected."""
    for stage in ("resolved", "archived"):
        idx = {"h1": _rec(stage, outcome="CORRECTED", outcome_date="2026-08-05",
                          reflected=False, resolved_by="zeta")}
        e = htgc._classify(_goal(hypothesis_id="h1"), idx, NOW, "echo")
        assert e is not None, stage
        assert e["verdict"] == "hypothesis_terminal"
        assert e["hypothesis_stage"] == stage
        # The three fields the goal names explicitly — a hit without them is
        # not actionable, since UNREFLECTED terminal work may still owe
        # reflection.
        assert e["outcome"] == "CORRECTED"
        assert e["outcome_date"] == "2026-08-05"
        assert e["reflected"] is False
        assert e["resolved_by"] == "zeta"
        assert e["days_since_outcome"] == 5.4


def test_archived_is_included_not_just_resolved():
    """Records migrate resolved -> archived as they age, so a resolved-only
    test is a survivorship filter that misses the OLDEST — i.e. stalest — half.
    g-115-1983's hypothesis is archived, so resolved-only would miss the very
    instance that motivated this script."""
    assert "archived" in htgc.TERMINAL_STAGES
    assert "resolved" in htgc.TERMINAL_STAGES


# ---------------------------------------------------------------- outcome #2
def test_replay_against_the_two_named_historical_instances():
    """#2: replay against  and  surfaces BOTH.

    Real ids and real hypothesis ids, read from the live queue 2026-08-10.
    Both goals are `skipped` today, so this reconstructs their pre-close state
    (`pending`) — which is the only way the criterion is satisfiable now.
    """
    cases = [
        ("g-318-57", "2026-07-18_g31821-postunblock-cadence-holds", "resolved"),
        ("g-115-1983", "2026-07-11_foxtrot-tombstone-revival", "archived"),
    ]
    for gid, hyp, stage in cases:
        idx = {hyp: _rec(stage)}
        e = htgc._classify(
            _goal(id=gid, hypothesis_id=hyp, status="pending"), idx, NOW, "echo")
        assert e is not None, "%s must be surfaced" % gid
        assert e["verdict"] == "hypothesis_terminal"
        assert e["goal_id"] == gid
        assert e["hypothesis_id"] == hyp


# ---------------------------------------------------------------- outcome #3
def test_live_hypothesis_is_not_surfaced():
    """#3: a goal whose hypothesis is still discovered/active/measurement-pending
    is NOT surfaced. This is the quiet case and the bulk of the corpus — 119 of
    150 open world goals carrying a hypothesis, measured 2026-08-10."""
    for stage in ("discovered", "active", "measurement-pending"):
        idx = {"h1": _rec(stage)}
        assert htgc._classify(_goal(hypothesis_id="h1"), idx, NOW, "echo") is None, stage


def test_goals_without_a_hypothesis_and_closed_goals_are_ignored():
    idx = {"h1": _rec("resolved")}
    assert htgc._classify(_goal(hypothesis_id=None), idx, NOW, "echo") is None
    assert htgc._classify(_goal(hypothesis_id="  "), idx, NOW, "echo") is None
    for st in ("completed", "skipped", "expired", "blocked"):
        assert htgc._classify(
            _goal(hypothesis_id="h1", status=st), idx, NOW, "echo") is None, st


def test_in_progress_is_open_and_therefore_scanned():
    """`in-progress` is an OPEN status: a claimed goal on a resolved hypothesis
    is the case that blocks the goal from selection entirely, so excluding it
    would drop the highest-cost members of the population."""
    idx = {"h1": _rec("resolved")}
    e = htgc._classify(
        _goal(hypothesis_id="h1", status="in-progress"), idx, NOW, "echo")
    assert e is not None and e["status"] == "in-progress"


# ---------------------------------------------------------------- outcome #4
def test_another_agents_goal_is_routed_to_board_post_never_closed():
    """#4: lane routing respected — another agent's goal is surfaced for a
    board post, never auto-closed (guard-1007)."""
    idx = {"h1": _rec("resolved")}
    e = htgc._classify(
        _goal(hypothesis_id="h1", intended_agent="foxtrot"), idx, NOW, "echo")
    assert e["lane"] == "other"
    assert e["action"] == "board-post"


def test_either_and_unset_and_self_are_mine():
    idx = {"h1": _rec("resolved")}
    for intended in ("either", "", None, "echo"):
        e = htgc._classify(
            _goal(hypothesis_id="h1", intended_agent=intended), idx, NOW, "echo")
        assert e["lane"] == "mine", intended
        assert e["action"] == "review-and-close"


def test_P1_claimed_by_outranks_intended_agent():
    """P1 — the defect measured while building this.

    `intended_agent: either` reads as "mine, close it"; `claimed_by: alpha`
    says a partner is executing it RIGHT NOW. Ownership is the claim, not the
    routing preference. Measured live: 27 of 31 hits were in exactly this
    shape, so an intended-only predicate would have pointed this agent at a
    live partner's whole working set.
    """
    idx = {"h1": _rec("resolved")}
    e = htgc._classify(
        _goal(hypothesis_id="h1", intended_agent="either", claimed_by="alpha"),
        idx, NOW, "echo")
    assert e["lane"] == "claimed-by-other"
    assert e["action"] == "board-post", "must never be review-and-close"
    assert e["claimed_by"] == "alpha"


def test_P1_my_own_claim_stays_mine():
    """The other direction: a claim held by THIS agent must not be pushed to a
    board post — that would route the agent to notify itself."""
    idx = {"h1": _rec("resolved")}
    e = htgc._classify(
        _goal(hypothesis_id="h1", claimed_by="echo"), idx, NOW, "echo")
    assert e["lane"] == "mine" and e["action"] == "review-and-close"


def test_P1_claim_outranks_even_a_matching_intended_agent():
    """intended_agent=echo (mine) + claimed_by=alpha (theirs) -> theirs."""
    idx = {"h1": _rec("resolved")}
    e = htgc._classify(
        _goal(hypothesis_id="h1", intended_agent="echo", claimed_by="alpha"),
        idx, NOW, "echo")
    assert e["action"] == "board-post"


def test_claim_age_reported_and_tolerant_of_absence():
    idx = {"h1": _rec("resolved")}
    e = htgc._classify(
        _goal(hypothesis_id="h1", claimed_by="alpha",
              claimed_at="2026-08-09T09:00:00"), idx, NOW, "echo")
    assert e["claim_age_hours"] == 24.0
    e2 = htgc._classify(
        _goal(hypothesis_id="h1", claimed_by="alpha", claimed_at=None),
        idx, NOW, "echo")
    assert e2["claim_age_hours"] is None


# ------------------------------------------------------------ dangling verdict
def test_dangling_hypothesis_is_its_own_verdict_and_never_a_close():
    """A `hypothesis_id` resolving to no record in ANY stage can never
    auto-clear — the reference itself is the defect, so closing is wrong."""
    e = htgc._classify(_goal(hypothesis_id="nope"), {}, NOW, "echo")
    assert e["verdict"] == "hypothesis_dangling"
    assert e["action"] == "repoint-or-remove-reference"
    assert e["hypothesis_stage"] is None
    assert e["days_since_outcome"] is None


def test_dangling_action_wins_over_lane_for_mine():
    e = htgc._classify(
        _goal(hypothesis_id="nope", intended_agent="either"), {}, NOW, "echo")
    assert e["action"] == "repoint-or-remove-reference"


# --------------------------------------------------- P2: residual-scope prompt
def test_P2_residual_prose_marker_catches_the_motivating_case():
    """P2 —  has exactly ONE verification outcome, so the obvious
    count-only heuristic misses it. Its residual is in DESCRIPTION PROSE.
    Text is g-115-3668's own, quoted from the live record."""
    g = _goal(
        hypothesis_id="h1",
        verification={"outcomes": ["Hypothesis ... is resolved CONFIRMED or CORRECTED"]},
        description=("If it resolves CORRECTED (the role IS allowed), that is the "
                     "more consequential outcome: it means the role carries grants "
                     "well beyond env-server duties, and the other two open register "
                     "rows should be re-read in that light rather than assumed narrow."))
    suspected, n, markers = htgc._residual_scope_suspected(g)
    assert n == 1, "the count signal alone would say 'no residual'"
    assert suspected is True
    assert markers, "prose markers must be what carries it"


def test_P2_downstream_consumer_shape_is_caught():
    """The first live run surfaced exactly two goals in this agent's own lane
    and BOTH carried a named downstream consumer — work the hypothesis reaching
    `resolved` does not discharge. Text is g-115-1969's own, and its
    `verification` is None, so the count signal contributes nothing here."""
    g = _goal(
        hypothesis_id="h1",
        verification=None,
        description=("Resolve when precheck-eval.sh accuracy by_confidence_band.high.total "
                     ">= 30. Consumer: hypothesis-calibration node (inversion section "
                     "retract-or-confirm) + calibration-gate ceiling tuning evidence."))
    suspected, n, markers = htgc._residual_scope_suspected(g)
    assert n == 0, "no verification outcomes at all — count signal is silent"
    assert suspected is True
    assert "consumer:" in markers


def test_P2_multiple_outcomes_still_suspected():
    g = _goal(verification={"outcomes": ["a", "b"]})
    suspected, n, markers = htgc._residual_scope_suspected(g)
    assert suspected is True and n == 2 and markers == []


def test_P2_plain_single_outcome_goal_is_not_suspected():
    """The flag must stay a signal, not fire on everything — measured 3.3% of
    open goals, 1 of 150 among open goals carrying a hypothesis."""
    suspected, n, markers = htgc._residual_scope_suspected(
        _goal(description="Do the thing. Verify it was done."))
    assert suspected is False and n == 1 and markers == []


def test_P2_malformed_verification_shapes_do_not_raise():
    for ver in (None, [], "text", {"outcomes": None}, {"outcomes": "notalist"}):
        suspected, n, markers = htgc._residual_scope_suspected(_goal(verification=ver))
        assert isinstance(suspected, bool) and isinstance(n, int)


def test_P2_flag_is_surfaced_on_the_entry():
    idx = {"h1": _rec("resolved")}
    e = htgc._classify(
        _goal(hypothesis_id="h1", description="if it resolves CORRECTED, re-read in that light"),
        idx, NOW, "echo")
    assert e["residual_scope_suspected"] is True
    assert "if it resolves" in e["residual_markers"]


# ---------------------------------------------------------------- outcome #5
def test_read_helpers_fail_open_and_report_rather_than_raise(monkeypatch):
    """#5: an unreadable pipeline or goal source yields FEWER hits, never an
    exception. The `ok=False` half is what keeps this honest against guard-383:
    a zero from a failed read must be reportable as degraded, not mistaken for
    a clean queue."""
    def boom(*a, **k):
        raise RuntimeError("daemon down")
    monkeypatch.setattr(htgc._rt, "rt_call", boom)
    recs, ok = htgc._read_pipeline_stage("resolved")
    assert recs == [] and ok is False

    monkeypatch.setattr(htgc._rt, "aspirations_read", boom)
    goals, ok2 = htgc._read_goals("world")
    assert goals == [] and ok2 is False


def test_undecodable_body_is_caught_not_propagated_as_systemexit(monkeypatch):
    """tolerant_decode_aggregate exits(1) on a malformed body (guard-383).
    This sweep must degrade rather than die, so the SystemExit is converted to
    a reported miss — otherwise criterion #5 fails on exactly the corrupt-body
    case it is written for."""
    monkeypatch.setattr(htgc._rt, "rt_call", lambda *a, **k: "{not json")
    recs, ok = htgc._read_pipeline_stage("resolved")
    assert recs == [] and ok is False

    monkeypatch.setattr(htgc._rt, "aspirations_read", lambda *a, **k: "{not json")
    goals, ok2 = htgc._read_goals("world")
    assert goals == [] and ok2 is False


def test_empty_body_is_a_valid_state_not_a_failure(monkeypatch):
    """An empty stage is a real state — reporting it as degraded would make
    `degraded` fire constantly and train the reader to ignore it."""
    monkeypatch.setattr(htgc._rt, "rt_call", lambda *a, **k: "")
    recs, ok = htgc._read_pipeline_stage("resolved")
    assert recs == [] and ok is True


def test_main_degrades_to_zero_hits_and_SAYS_SO_when_every_read_fails(monkeypatch, capsys):
    """#5 at the BINARY level, not just the helper level (guard-920 — test the
    production shape). Both readers fail; main() must exit 0 with zero hits AND
    `degraded: true`.

    The `degraded` half is the whole point: a zero produced by a failed read is
    otherwise byte-identical to a clean queue, which is the silent-empty-aggregate
    lie guard-383 forbids. Asserting only "it didn't crash" would pass on a
    sweep that reports a confident false all-clear forever.
    """
    def boom(*a, **k):
        raise RuntimeError("daemon down")
    monkeypatch.setattr(htgc._rt, "rt_call", boom)
    monkeypatch.setattr(htgc._rt, "aspirations_read", boom)
    monkeypatch.setattr(sys, "argv", ["x", "--output", "json"])

    assert htgc.main() == 0, "must not raise and must not exit non-zero"
    out = json.loads(capsys.readouterr().out)
    assert out["terminal_count"] == 0
    assert out["scanned"] == 0
    assert out["degraded"] is True
    assert set(out["pipeline_read_failed"]) == set(
        htgc.TERMINAL_STAGES + htgc.LIVE_STAGES)
    assert set(out["goal_read_failed"]) == {"world", "agent"}


def test_main_reports_not_degraded_on_a_healthy_read(monkeypatch, capsys):
    """The other direction — `degraded` must not be stuck on, or it says
    nothing. A flag that always fires is not a flag."""
    monkeypatch.setattr(htgc._rt, "rt_call", lambda *a, **k: '{"records": []}')
    monkeypatch.setattr(htgc._rt, "aspirations_read",
                        lambda *a, **k: '{"aspirations": []}')
    monkeypatch.setattr(sys, "argv", ["x", "--output", "json"])
    assert htgc.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["degraded"] is False
    assert out["pipeline_read_failed"] == [] and out["goal_read_failed"] == []


def test_stage_conflict_is_surfaced_when_an_id_sits_in_two_stages():
    """Fresh-eyes finding F1. Terminal stages are indexed first, so a record in
    two stages resolves to the terminal one — the safe direction (fails toward
    surfacing, cost = one read; live-first would fail toward silence, which is
    the whole defect). But collapsing it SILENTLY makes a data anomaly look
    like a clean lookup, so the collapse is reported.

    Not hypothetical: 3 ids sat in two stages on 2026-08-10, one in
    `archived` + `discovered`."""
    idx = {"h1": _rec("archived")}
    conflicts = {"h1": ["archived", "discovered"]}
    e = htgc._classify(_goal(hypothesis_id="h1"), idx, NOW, "echo", conflicts)
    assert e["stage_conflict"] == ["archived", "discovered"]
    assert e["verdict"] == "hypothesis_terminal", "terminal must still win"


def test_stage_conflict_is_none_for_a_clean_id_and_when_arg_omitted():
    """The flag must stay null on the common path, or it says nothing. The
    omitted-arg case also pins backward compatibility for callers predating
    the parameter."""
    idx = {"h1": _rec("resolved")}
    e = htgc._classify(_goal(hypothesis_id="h1"), idx, NOW, "echo", {"other": ["a", "b"]})
    assert e["stage_conflict"] is None
    e2 = htgc._classify(_goal(hypothesis_id="h1"), idx, NOW, "echo")
    assert e2["stage_conflict"] is None


def test_main_declares_its_scan_bound_even_on_a_clean_run(monkeypatch, capsys):
    """Fresh-eyes finding F3 (guard-2529): `agent` resolves to the BOUND agent's
    queue only, so a zero is "clean for world + this agent", never "clean
    fleet-wide". A sweep that filters its input before counting must declare
    what it excluded — on the CLEAN run especially, since that is the run whose
    zero gets quoted."""
    monkeypatch.setattr(htgc._rt, "rt_call", lambda *a, **k: '{"records": []}')
    monkeypatch.setattr(htgc._rt, "aspirations_read",
                        lambda *a, **k: '{"aspirations": []}')
    monkeypatch.setenv("MIND_AGENT", "echo")
    monkeypatch.setattr(sys, "argv", ["x", "--output", "json"])
    assert htgc.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["terminal_count"] == 0, "this is the clean run"
    assert out["sources_scanned"] == ["world", "agent:echo"]
    assert "NOT scanned" in out["scan_bound"]
    assert out["stage_conflicts"] == {}


def test_classify_never_raises_on_malformed_records():
    """A garbage pipeline record must not take down the sweep (fail-open)."""
    for bad in ({}, {"stage": None}, {"stage": "resolved", "outcome_date": "not-a-date"}):
        e = htgc._classify(_goal(hypothesis_id="h1"), {"h1": bad}, NOW, "echo")
        if bad.get("stage") == "resolved":
            assert e is not None and e["days_since_outcome"] is None
        else:
            assert e is None or e["verdict"] == "hypothesis_terminal"


def test_bad_outcome_date_degrades_to_none_not_an_exception():
    idx = {"h1": _rec("resolved", outcome_date="2026-13-99")}
    e = htgc._classify(_goal(hypothesis_id="h1"), idx, NOW, "echo")
    assert e["days_since_outcome"] is None
