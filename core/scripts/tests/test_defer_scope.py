"""Tests for the shared defer-scope vocabulary + its coverage consumer ().

Pins the four properties the goal's verification names, plus the two that make
the vocabulary a SHARED decision rather than a fourth fork:

  * TOTAL function — every input maps to a declared token or to the ONE
    sentinel; nothing is passed through and nothing coerces to a default.
  * ONE sentinel, identical in every lane.
  * lane `user-leg` is lane P's set BY IMPORT, so it cannot drift without
    test_allowlist_parity_batch3::test_2b_user_leg_scopes_equal going red too.
  * every lane subset is drawn from the shared superset (no lane-private token).
  * the consumer CONSUMES the exclusion count — it reports keyable/unkeyable
    per lane and surfaces the observed TEXT, not merely a count.
  * the consumer is REPORT-ONLY: it must not write a scope onto any goal.

The vocabulary module is imported directly (gates/ on sys.path); the consumer
is exercised as a SUBPROCESS rather than imported, because its hyphenated name
blocks a plain import and because report-only-ness is only meaningful when the
real CLI path runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
COVERAGE = SCRIPTS / "defer-scope-coverage.py"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "gates"))

import defer_scope as DS  # noqa: E402


# ── the vocabulary is ONE decision, not four ────────────────────────────────

def test_user_leg_lane_is_lane_p_set_by_import_not_a_copy():
    from user_leg_scope import VALID_USER_LEG_SCOPES
    assert DS.LANE_SCOPES["user-leg"] == frozenset(VALID_USER_LEG_SCOPES)


def test_every_lane_token_is_drawn_from_the_shared_superset():
    for lane, tokens in DS.LANE_SCOPES.items():
        extra = tokens - DS.DEFER_SCOPES
        assert not extra, f"lane {lane} declares private token(s) {sorted(extra)}"


def test_lanes_overlap_which_is_the_reason_for_one_vocabulary():
    """If the lanes shared nothing, four enums would have been the right call."""
    cred = DS.LANE_SCOPES["credential"]
    userleg = DS.LANE_SCOPES["user-leg"]
    assert cred & userleg, "no overlap — re-justify the shared-set decision"


def test_four_lanes_are_declared():
    assert DS.lanes() == ["credential", "grant", "precondition", "user-leg"]


def test_importable_as_a_package_module_not_only_with_gates_on_syspath():
    """The daemon imports `gates` as a PACKAGE, where only core/scripts is on
    sys.path — so a bare `from user_leg_scope import ...` raises
    ModuleNotFoundError there while working perfectly in every CLI consumer.

    Runs in a SUBPROCESS deliberately. This test module already inserts
    `gates/` at import time, so an in-process check would find the sibling
    either way and pass vacuously — a pin that cannot fail is not a pin.
    """
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import gates.defer_scope as m; "
         "print(len(m.DEFER_SCOPES))" % str(SCRIPTS)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout.strip()) == len(DS.DEFER_SCOPES)


# ── classify is TOTAL, with ONE sentinel ────────────────────────────────────

def test_classify_returns_a_declared_token_or_the_sentinel_for_any_input():
    inputs = ["", None, "   ", "s3:DeleteObjectVersion is denied",
              "totally unrelated prose about badgers", "\x00\x01",
              "deployment-approval", 12345]
    for lane in DS.lanes():
        for val in inputs:
            out = DS.classify(lane, val if isinstance(val, str) or val is None else str(val))
            assert out == DS.SENTINEL or out in DS.LANE_SCOPES[lane], (lane, val, out)


def test_unknown_lane_is_sentinel_not_an_exception():
    assert DS.classify("no-such-lane", "s3:DeleteObjectVersion") == DS.SENTINEL


def test_sentinel_is_one_name_shared_by_every_lane():
    assert DS.SENTINEL == "UNKNOWN"
    assert DS.SENTINEL not in DS.DEFER_SCOPES, "the sentinel must not be a declared token"


def test_unrecognised_input_never_coerces_to_a_declared_token():
    """The failure this guards: silently picking the most common token."""
    assert DS.classify("credential", "nothing recognisable here at all") == DS.SENTINEL


# ── the IAM-action insight the originating goal recorded ────────────────────

def test_credential_lane_keys_on_an_iam_action_not_only_an_env_var():
    """: the joinable key was `s3:DeleteObjectVersion`, not a secret name."""
    assert DS.classify("credential", "human_blocked: IAM grant of s3:DeleteObjectVersion") \
        == "iam-permission"


def test_credential_lane_still_keys_a_plain_env_var_defer():
    assert DS.classify("credential", "env-read.sh has OPERATOR_URL returned false") == "env-var"


# ── step 5: surface the VALUE, not a count ──────────────────────────────────

def test_undeclared_returns_none_when_the_text_is_recognised():
    assert DS.undeclared("credential", "denied: s3:GetLifecycleConfiguration") is None


def test_undeclared_surfaces_the_observed_text_and_the_allowed_set():
    u = DS.undeclared("credential", "some prose no pattern matches")
    assert u is not None
    assert u["verdict"] == DS.SENTINEL
    assert "some prose" in u["observed"]
    assert u["allowed"] == sorted(DS.LANE_SCOPES["credential"])


def test_undeclared_excerpt_is_bounded():
    u = DS.undeclared("credential", "x" * 5000, excerpt=40)
    assert len(u["observed"]) == 40


# ── the consumer actually consumes ──────────────────────────────────────────

def _fixture_queue(tmp_path):
    rows = [{
        "id": "asp-999", "status": "active",
        "goals": [
            {"id": "g-999-01", "status": "blocked",
             "defer_reason": "human_blocked: IAM grant of s3:DeleteObjectVersion denied"},
            {"id": "g-999-02", "status": "blocked",
             "defer_reason": "human_blocked: prose that matches nothing whatsoever"},
            {"id": "g-999-03", "status": "completed",
             "defer_reason": "human_blocked: terminal goals must not be counted"},
            {"id": "g-999-04", "status": "pending",
             "participants": ["agent", "user"], "title": "Decide: something"},
        ],
    }]
    p = tmp_path / "aspirations.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _run(qpath, *extra):
    return subprocess.run(
        [sys.executable, str(COVERAGE), "--queue", str(qpath), "--output", "json", *extra],
        capture_output=True, text=True)


def test_consumer_splits_the_exclusion_count_into_keyable_and_unkeyable(tmp_path):
    proc = _run(_fixture_queue(tmp_path))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    cred = out["lanes"]["credential"]
    assert cred["keyable"] == 1 and cred["by_token"].get("iam-permission") == 1


def test_a_defer_naming_no_lane_cue_is_unrouted_not_defaulted_into_a_lane(tmp_path):
    """Routing by keyword uses the prose whose un-keyability is the subject, so a
    text-unrecognizable defer cannot be lane-assigned. Defaulting it into
    `precondition` inflated that lane and deflated the others."""
    out = json.loads(_run(_fixture_queue(tmp_path)).stdout)
    assert out["lanes"]["unrouted"]["unkeyable"] == 1
    assert out["lanes"]["credential"]["unkeyable"] == 0
    assert out["lanes"]["precondition"]["total"] == 0


def test_unrouted_is_reported_never_silently_dropped(tmp_path):
    """A dropped bucket shrinks the denominator, which reads as better coverage."""
    out = json.loads(_run(_fixture_queue(tmp_path)).stdout)
    assert "unrouted" in out["lanes"]
    assert out["total_excluded"] >= out["lanes"]["unrouted"]["total"] > 0
    assert "input_caveat" in out["lanes"]["unrouted"]


def test_consumer_excludes_terminal_goals_from_the_population(tmp_path):
    out = json.loads(_run(_fixture_queue(tmp_path)).stdout)
    ids = [s["goal_id"] for lane in out["lanes"].values()
           for s in lane["unkeyable_samples"]]
    assert "g-999-03" not in ids, "a completed goal must not be counted as excluded"


def test_consumer_surfaces_the_text_behind_each_unkeyable(tmp_path):
    out = json.loads(_run(_fixture_queue(tmp_path)).stdout)
    samples = out["lanes"]["unrouted"]["unkeyable_samples"]
    assert samples and "matches nothing" in samples[0]["observed"]


def test_consumer_exit_on_unkeyable_is_opt_in(tmp_path):
    q = _fixture_queue(tmp_path)
    assert _run(q).returncode == 0
    assert _run(q, "--exit-on-unkeyable").returncode == 1


def test_consumer_carries_the_input_caveat_for_the_declaration_lanes(tmp_path):
    """A bare 0-keyable on a lane whose input is a TITLE must not read as a
    pattern defect — the caveat is what stops the next reader widening regexes."""
    out = json.loads(_run(_fixture_queue(tmp_path)).stdout)
    assert "input_caveat" in out["lanes"]["user-leg"]
    assert "g-115-3856" in out["lanes"]["user-leg"]["input_caveat"]


def test_an_unreachable_lane_reports_as_unexamined_not_as_clean(tmp_path):
    """A zero has two causes and they are not interchangeable.

    `_lane_of` returns only user-leg/credential/precondition/unrouted, so NOTHING
    can land in `grant` — it appears in the report solely because the lane set is
    built from the full declared vocabulary. Rendering that as "(no excluded
    population)" makes an unexamined lane read as a clean one, which is the
    guard-1760 failure this consumer otherwise guards against, arriving in its
    subtlest form: a lane dropped outright invites "where is grant?", while a lane
    showing 0 beside a reassuring note answers the question before it is asked.
    """
    q = _fixture_queue(tmp_path)
    out = json.loads(_run(q).stdout)
    caveat = out["lanes"]["grant"]["input_caveat"]
    assert caveat.startswith("NOT MEASURED"), caveat
    assert "not evidence of a clean lane" in caveat
    text = subprocess.run(
        [sys.executable, str(COVERAGE), "--queue", str(q)],
        capture_output=True, text=True).stdout
    assert "UNEXAMINED" in text
    # Negative control: a lane that IS measured and happens to be empty must NOT
    # borrow the unexamined wording, or the distinction collapses again.
    assert "measured, no excluded population" in text


def test_consumer_is_report_only_and_does_not_mutate_the_queue(tmp_path):
    q = _fixture_queue(tmp_path)
    before = q.read_bytes()
    _run(q, "--exit-on-unkeyable")
    assert q.read_bytes() == before, "the consumer wrote to the queue — it must not"


def test_consumer_reports_report_only_in_its_payload(tmp_path):
    out = json.loads(_run(_fixture_queue(tmp_path)).stdout)
    assert out["report_only"] is True


def test_consumer_exits_2_on_an_unreadable_queue(tmp_path):
    proc = _run(tmp_path / "does-not-exist.jsonl")
    assert proc.returncode == 2


# ── lane-agnostic classify: "recognizable scope, unknown lane" ──────────────

def test_classify_any_recognises_a_scope_without_a_lane():
    assert DS.classify_any("human_blocked: fleet-quiesce-window") == "human-window"


def test_classify_any_is_still_total():
    for val in ("", None, "badgers", "\x00"):
        out = DS.classify_any(val)
        assert out == DS.SENTINEL or out in DS.DEFER_SCOPES


def test_unrouted_samples_report_whether_the_scope_itself_is_recognisable(tmp_path):
    """'recognisable scope, unknown lane' and 'recognisable as nothing' need
    different remedies; a bare unrouted count cannot tell them apart."""
    rows = [{
        "id": "asp-998", "status": "active",
        "goals": [
            {"id": "g-998-01", "status": "blocked",
             "defer_reason": "human_blocked: fleet-quiesce-window until the user says go"},
            {"id": "g-998-02", "status": "blocked",
             "defer_reason": "human_blocked: entirely opaque prose about badgers"},
        ],
    }]
    q = tmp_path / "aspirations.jsonl"
    q.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = json.loads(_run(q, "--show", "0").stdout)
    un = out["lanes"]["unrouted"]
    assert un["unkeyable"] == 2
    assert un["scope_recognizable"] == 1
    toks = {s["goal_id"]: s.get("lane_agnostic_token") for s in un["unkeyable_samples"]}
    assert toks["g-998-01"] == "human-window"
    assert toks["g-998-02"] == DS.SENTINEL


def test_policy_prohibition_is_declarable_but_never_recognised_from_free_text():
    """The token is legitimate to DECLARE and impossible to RECOGNISE.

    Measured over all 41 non-terminal defers carrying a defer_reason: the
    free-text pattern won on 4 and was wrong on all 4 — zero true positives.
    Every hit was a prohibition phrase inside a note constraining HOW to do the
    work, not the reason the goal was blocked. Both halves are pinned here
    because they are one decision: dropping the pattern while keeping the token
    is the point, and a future author restoring the pattern must fail a test,
    not merely contradict a comment.
    """
    assert "policy-prohibition" in DS.DEFER_SCOPES
    assert "policy-prohibition" in DS.LANE_SCOPES["credential"]
    assert not any(tok == "policy-prohibition" for tok, _ in DS._PATTERNS)
    for text in ("SOURCE-write is prohibited on this repo",
                 "a sweep must not kill the legitimate bridges",
                 "this is forbidden by guard-1234"):
        assert DS.classify_any(text) != "policy-prohibition", text


def test_the_four_prohibition_false_positives_no_longer_mis_key():
    """Regression pin for the measured corpus shapes, in their real form: the
    blocking reason is in the PREFIX and the prohibition phrase is in a
    constraint note further in. Mutation-proof — each of these classified as
    policy-prohibition before the pattern was removed."""
    cases = [
        "blocked_on_dependency: cc-03 does not resolve from this box (ssh: Could "
        "not resolve hostname cc-03). NOTE: a sweep must not kill this box's own "
        "bridges, which are legitimate.",
        "human_blocked: requires a SECOND physical box running as a forked body; "
        "a shared box MUST NOT be used to fake it.",
    ]
    for text in cases:
        assert DS.classify_any(text) != "policy-prohibition", text
    # The second case carries its true scope in the head and must still reach it.
    assert DS.classify_any(cases[1]) == "hardware-resource"
