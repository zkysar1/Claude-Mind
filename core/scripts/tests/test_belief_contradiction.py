"""Unit tests for the Theory-of-Mind contradiction detector ().

Covers `_belief_contradiction` — classify, the N-consecutive streak gate (the
`no false-trigger on first observation` invariant), revise (lower/supersede),
the `process_all` composer, and the `main()` CLI plumbing. Uses generic
placeholder names (self-x / partner-a / partner-b) so the file is domain-free.
"""
import io
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import _belief_contradiction as BC  # noqa: E402


def _belief(about, domain, conf=0.6, belief="b", last="t0"):
    return {"about": about, "belief": belief, "confidence": conf,
            "last_observed": last, "domain": domain}


# --- classify -----------------------------------------------------------------

def test_classify_match_when_observed_equals_held_domain():
    beliefs = [_belief("partner-a", "framework-architecture")]
    assert BC.classify(beliefs, "partner-a", "framework-architecture") == "match"


def test_classify_contradiction_when_observed_differs():
    beliefs = [_belief("partner-a", "framework-architecture")]
    assert BC.classify(beliefs, "partner-a", "client-runtime") == "contradiction"


def test_classify_skip_when_no_domain_belief():
    # Free-form belief (domain=None) is not contradiction-checkable.
    beliefs = [_belief("partner-a", None)]
    assert BC.classify(beliefs, "partner-a", "client-runtime") == "skip"


def test_classify_skip_when_belief_below_threshold():
    beliefs = [_belief("partner-a", "framework-architecture", conf=0.3)]
    assert BC.classify(beliefs, "partner-a", "client-runtime", threshold=0.5) == "skip"


def test_classify_skip_when_no_observation():
    beliefs = [_belief("partner-a", "framework-architecture")]
    assert BC.classify(beliefs, "partner-a", None) == "skip"
    assert BC.classify(beliefs, "partner-a", "") == "skip"


def test_classify_skip_when_no_belief_about_partner():
    beliefs = [_belief("partner-b", "framework-architecture")]
    assert BC.classify(beliefs, "partner-a", "client-runtime") == "skip"


@pytest.mark.parametrize("junk", [None, "notalist", 42, {"k": "v"}])
def test_classify_tolerates_malformed_beliefs(junk):
    assert BC.classify(junk, "partner-a", "x") == "skip"


# --- 6: lane-granular comparison for verbose current_focus -----------
# Since 5 current_focus is the verbose 'asp-NNN: title' string and
# fresh-eyes-review 2.6c stamps belief.domain from it VERBATIM. Comparison must
# be at LANE ('asp-NNN') granularity: a goal-title change WITHIN a lane is NOT a
# contradiction, while a genuine cross-lane move still is. Colon-free short
# tokens (the rest of this file) reduce to themselves, so prior behavior holds.

def test_focus_lane_reduces_verbose_focus_to_asp_prefix():
    # split on the FIRST ':' only — titles themselves may contain ':'.
    assert BC._focus_lane("asp-115: Idea: reconcile current_focus") == "asp-115"
    assert BC._focus_lane("asp-309: deploy game system") == "asp-309"


def test_focus_lane_identity_on_bare_lane_and_short_token():
    assert BC._focus_lane("asp-115") == "asp-115"
    assert BC._focus_lane("framework-architecture") == "framework-architecture"


def test_focus_lane_empty_on_none_or_blank():
    assert BC._focus_lane(None) == ""
    assert BC._focus_lane("") == ""


def test_classify_match_same_lane_different_goal_title():
    # THE 6 fix: belief snapshot 'asp-115: <title-A>', now observed on
    # 'asp-115: <title-B>' (partner advanced to the next goal IN THE SAME lane).
    # Pre-fix this returned "contradiction" (verbose strings differ); must be "match".
    beliefs = [_belief("partner-a", "asp-115: Idea: reconcile current_focus")]
    assert BC.classify(beliefs, "partner-a", "asp-115: Sweep alert inbox") == "match"


def test_classify_contradiction_across_lanes_verbose():
    # A genuine cross-lane move still contradicts.
    beliefs = [_belief("partner-a", "asp-115: Idea: reconcile current_focus")]
    assert BC.classify(beliefs, "partner-a", "asp-309: deploy game system") == "contradiction"


def test_process_all_sustained_cross_lane_triggers_despite_title_change():
    # The streak-keying half of the fix: a partner genuinely working a DIFFERENT
    # lane across two checks, but on two DIFFERENT goal-titles within that lane,
    # must still accumulate to a revision. Pre-fix the raw verbose observed_domain
    # differed between checks and reset the streak to 1, so a real sustained
    # cross-lane divergence never triggered. Lane normalization keeps the streak.
    held = [_belief("partner-a", "asp-115: framework work", conf=0.6)]
    # check 1: partner observed in asp-309 (title A) -> contradiction, streak 1
    ts1 = _team_state(held, partner_a="asp-309: deploy game system")
    r1 = BC.process_all(ts1, "self-x", {}, "t1", n_required=2)
    assert r1["revised_beliefs"] is None
    assert r1["new_streaks"]["partner-a"]["count"] == 1
    assert r1["new_streaks"]["partner-a"]["observed_domain"] == "asp-309"  # lane, not verbose
    # check 2: still asp-309 but a DIFFERENT goal-title -> streak kept (same lane) -> revise
    ts2 = _team_state(held, partner_a="asp-309: wire CI pipeline")
    r2 = BC.process_all(ts2, "self-x", r1["new_streaks"], "t2", n_required=2, mode="lower")
    assert r2["revised_beliefs"] is not None
    assert r2["revised_beliefs"][0]["confidence"] == pytest.approx(0.3)
    assert r2["events"][0]["should_revise"] is True


# --- next_streak: the no-false-trigger-on-first-observation gate ---------------

def test_first_contradiction_does_not_revise():
    # THE core invariant ( outcome 3): one divergent observation never
    # triggers — count reaches 1, should_revise stays False.
    streak, should = BC.next_streak(None, "contradiction", "client-runtime", n_required=2)
    assert should is False
    assert streak == {"observed_domain": "client-runtime", "count": 1}


def test_second_consecutive_contradiction_triggers():
    prev = {"observed_domain": "client-runtime", "count": 1}
    streak, should = BC.next_streak(prev, "contradiction", "client-runtime", n_required=2)
    assert should is True
    assert streak is None  # resets on trigger (the belief is being revised)


def test_match_clears_streak():
    prev = {"observed_domain": "client-runtime", "count": 1}
    streak, should = BC.next_streak(prev, "match", "framework-architecture")
    assert should is False and streak is None


def test_skip_clears_streak():
    prev = {"observed_domain": "client-runtime", "count": 1}
    streak, should = BC.next_streak(prev, "skip", None)
    assert should is False and streak is None


def test_changed_observed_domain_resets_streak_to_one():
    # An oscillating divergence (different observed domain) does NOT accumulate —
    # only a SUSTAINED contradiction on the SAME domain reaches the threshold.
    prev = {"observed_domain": "client-runtime", "count": 1}
    streak, should = BC.next_streak(prev, "contradiction", "analysis", n_required=2)
    assert should is False
    assert streak == {"observed_domain": "analysis", "count": 1}


def test_higher_n_required_needs_more_observations():
    # n_required=3: the 2nd consecutive observation (count 1 -> 2) does NOT yet trigger...
    s, should = BC.next_streak({"observed_domain": "y", "count": 1}, "contradiction", "y", n_required=3)
    assert should is False and s["count"] == 2
    # ...but the 3rd (count 2 -> 3) does.
    _, should3 = BC.next_streak({"observed_domain": "y", "count": 2}, "contradiction", "y", n_required=3)
    assert should3 is True
    # Contrast: with n_required=2 the same 2nd observation already triggers.
    _, should2 = BC.next_streak({"observed_domain": "y", "count": 1}, "contradiction", "y", n_required=2)
    assert should2 is True


def test_n_required_floored_to_two_blocks_first_observation_trigger():
    # Outcome 3 is STRUCTURAL, not just the default: even an adversarial env
    # override of N to 1 (or 0, or negative) must NOT revise on a FIRST
    # observation — next_streak floors n_required to 2. ( adversarial
    # review, 2026-06-18; the BELIEF_CONTRADICTION_N footgun.)
    for bad_n in (1, 0, -3):
        streak, should = BC.next_streak(None, "contradiction", "x", n_required=bad_n)
        assert should is False, "n_required=%r false-triggered on first observation" % bad_n
        assert streak == {"observed_domain": "x", "count": 1}
    # ...but the SECOND consecutive observation still triggers at the floored N=2.
    _, should2 = BC.next_streak({"observed_domain": "x", "count": 1},
                                "contradiction", "x", n_required=1)
    assert should2 is True


# --- revise -------------------------------------------------------------------

def test_revise_lower_halves_confidence_and_records_surprise():
    beliefs = [_belief("partner-a", "framework-architecture", conf=0.6)]
    out = BC.revise(beliefs, "partner-a", "client-runtime", "2026-06-18T05:00:00",
                    mode="lower", factor=0.5)
    e = out[0]
    assert e["confidence"] == pytest.approx(0.3)       # halved
    assert e["domain"] == "framework-architecture"      # held domain kept (just less sure)
    assert e["prior_confidence"] == pytest.approx(0.6)  # surprise recorded
    assert e["prior_domain"] == "framework-architecture"
    assert e["revised_at"] == "2026-06-18T05:00:00"


def test_revise_supersede_flips_domain_to_observed():
    beliefs = [_belief("partner-a", "framework-architecture", conf=0.7)]
    out = BC.revise(beliefs, "partner-a", "client-runtime", "2026-06-18T05:00:00",
                    mode="supersede")
    e = out[0]
    assert e["domain"] == "client-runtime"              # flipped to observed reality
    assert e["prior_domain"] == "framework-architecture"
    assert e["confidence"] == pytest.approx(0.5)        # calibrated reset
    assert "superseded" in e["belief"]


def test_revise_preserves_other_partners():
    beliefs = [_belief("partner-a", "framework-architecture"),
               _belief("partner-b", "client-runtime")]
    out = BC.revise(beliefs, "partner-a", "analysis", "t1", mode="lower")
    by = {b["about"]: b for b in out}
    assert by["partner-b"]["domain"] == "client-runtime"       # untouched
    assert "revised_at" not in by["partner-b"]
    assert by["partner-a"]["revised_at"] == "t1"


def test_revise_noop_when_no_contradiction():
    beliefs = [_belief("partner-a", "framework-architecture")]
    out = BC.revise(beliefs, "partner-a", "framework-architecture", "t1")  # observed == held
    assert out == beliefs


# --- process_all: end-to-end multi-partner pass -------------------------------

def _team_state(self_beliefs, **partner_focus):
    status = {"self-x": {"beliefs": self_beliefs}}
    for p, focus in partner_focus.items():
        status[p.replace("_", "-")] = {"current_focus": focus}
    return {"agent_status": status}


def test_process_all_first_observation_no_revision():
    # partner-a believed on framework, observed on client-runtime ONCE -> no trigger.
    ts = _team_state([_belief("partner-a", "framework-architecture")],
                     partner_a="client-runtime")
    res = BC.process_all(ts, "self-x", {}, "t1", n_required=2)
    assert res["revised_beliefs"] is None                      # nothing revised yet
    assert res["new_streaks"]["partner-a"]["count"] == 1
    assert res["events"][0]["status"] == "contradiction"
    assert res["events"][0]["should_revise"] is False


def test_process_all_second_consecutive_triggers_revision():
    ts = _team_state([_belief("partner-a", "framework-architecture", conf=0.6)],
                     partner_a="client-runtime")
    prev = {"partner-a": {"observed_domain": "client-runtime", "count": 1}}
    res = BC.process_all(ts, "self-x", prev, "t2", n_required=2, mode="lower")
    assert res["revised_beliefs"] is not None                  # revision fired
    assert res["revised_beliefs"][0]["confidence"] == pytest.approx(0.3)
    assert "partner-a" not in res["new_streaks"]               # streak reset on trigger
    assert res["events"][0]["should_revise"] is True


def test_process_all_match_produces_no_event_streak():
    ts = _team_state([_belief("partner-a", "framework-architecture")],
                     partner_a="framework-architecture")
    res = BC.process_all(ts, "self-x", {}, "t1")
    assert res["revised_beliefs"] is None
    assert res["new_streaks"] == {}
    assert res["events"][0]["status"] == "match"


def test_process_all_skips_partners_without_domain_belief():
    ts = _team_state([_belief("partner-a", None)], partner_a="client-runtime")
    res = BC.process_all(ts, "self-x", {}, "t1")
    assert res["events"] == []                                 # skip emits no event
    assert res["revised_beliefs"] is None


def test_process_all_multiple_partners_independent_streaks():
    ts = _team_state(
        [_belief("partner-a", "framework-architecture", conf=0.6),
         _belief("partner-b", "client-runtime", conf=0.6)],
        partner_a="analysis", partner_b="client-runtime")  # a contradicts, b matches
    prev = {"partner-a": {"observed_domain": "analysis", "count": 1}}
    res = BC.process_all(ts, "self-x", prev, "t2", n_required=2)
    # partner-a triggers (2nd consecutive), partner-b matches (untouched)
    by = {b["about"]: b for b in res["revised_beliefs"]}
    assert by["partner-a"]["revised_at"] == "t2"
    assert "revised_at" not in by["partner-b"]


# --- main(): stdin/argv plumbing ----------------------------------------------

def _run_main(stdin_text, argv, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    rc = BC.main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def test_main_full_round_trip_triggers(monkeypatch, capsys):
    ts = _team_state([_belief("partner-a", "framework-architecture", conf=0.6)],
                     partner_a="client-runtime")
    prev = json.dumps({"partner-a": {"observed_domain": "client-runtime", "count": 1}})
    rc, parsed = _run_main(
        json.dumps(ts),
        ["--self", "self-x", "--prev-streaks", prev, "--now", "t2", "--n-required", "2"],
        monkeypatch, capsys)
    assert rc == 0
    assert parsed["revised_beliefs"][0]["confidence"] == pytest.approx(0.3)


def test_main_malformed_stdin_is_safe(monkeypatch, capsys):
    rc, parsed = _run_main(
        "{not json", ["--self", "self-x", "--now", "t1"], monkeypatch, capsys)
    assert rc == 0
    assert parsed["revised_beliefs"] is None and parsed["events"] == []


def test_main_null_stdin_is_safe(monkeypatch, capsys):
    rc, parsed = _run_main("null", ["--self", "self-x"], monkeypatch, capsys)
    assert rc == 0 and parsed["events"] == []
