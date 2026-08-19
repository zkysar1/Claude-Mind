"""Pins for the lane-pin `review_by` lifecycle ().

THE LOAD-BEARING TEST IS `test_a_lapsed_review_by_does_not_weaken_the_pin_by_one_goal`.
Everything else here is plumbing; that one is the reason the feature has this
shape. A `review_by` that expired to a VOID would silently hand an agent back a
work surface the user deliberately took away, at the exact moment nobody was
looking. So the claim gate must be byte-identical either side of the date, and
this file proves it rather than trusting the comment that says so.

The second-order pin is `test_a_header_whose_name_contains_an_underscore_is_read`.
The first implementation normalized header cells through `_strip_markdown`, which
strips `_` as emphasis -- so `review_by` became `reviewby`, matched nothing, and
the column read EMPTY while `granted` and `expires` (no underscores) parsed
correctly next to it. That is the defect class the whole goal is about: a field
that parses to nothing, with no error, and an audit that reads clean because the
field exists.
"""

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS / "gates"))

import lane_pin as lp  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "lane_pin_review", _SCRIPTS / "lane_pin_review.py")
lpr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lpr)

IN_LANE = "run worlds: live sessions; box-local infra; own store hygiene"
OUT_LANE = "ALL CODE work: server lua, framework scripts, workflows, analyzers"


def registry(review_by="2026-11-04", granted="2026-08-06",
             header="| id | agent | in-lane | out-of-lane | granted | source | expires | review_by |"):
    return (
        "## Standing Lane Pins\n\n"
        + header + "\n"
        "|----|-------|---------|-------------|---------|--------|---------|-----------|\n"
        "| pin-001 | foxtrot | " + IN_LANE + " | " + OUT_LANE + " | " + granted
        + " | user directive | user directive only | " + review_by + " |\n"
        "\n## Next Section\n"
    )


def _pin(**kw):
    return lp.parse_pins(registry(**kw))[0]


# ---------------------------------------------------------------------------
# THE INVARIANT: a lapsed review date changes NOTHING about enforcement.
# ---------------------------------------------------------------------------

def test_a_lapsed_review_by_does_not_weaken_the_pin_by_one_goal():
    """The whole reason this is a review date and not a timeout."""
    text = registry(review_by="2020-01-01")          # long past due
    goal = {"title": "Fix the analyzer workflow", "category": "framework"}
    verdict = lp.evaluate("foxtrot", goal, registry_text=text)
    assert verdict["would_block"] is True
    assert verdict["verdict"] == "out-of-lane"


def test_enforcement_is_identical_either_side_of_the_review_date():
    """Byte-compare the gate's own verdict across a due and an overdue pin."""
    goal = {"title": "Fix the analyzer workflow", "category": "framework"}
    fresh = lp.evaluate("foxtrot", goal, registry_text=registry(review_by="2099-01-01"))
    stale = lp.evaluate("foxtrot", goal, registry_text=registry(review_by="2020-01-01"))
    assert fresh == stale


def test_an_in_lane_goal_is_still_allowed_when_review_is_overdue():
    """The other direction: lapsing must not make the gate MORE aggressive."""
    goal = {"title": "Run worlds live sessions on the host", "category": "ops"}
    assert lp.evaluate("foxtrot", goal,
                       registry_text=registry(review_by="2020-01-01"))["would_block"] is False


def test_evaluate_never_calls_review_status():
    """Structural proof of the separation, not a behavioural sample."""
    called = []
    original = lp.review_status
    lp.review_status = lambda *a, **k: called.append(1)
    try:
        lp.evaluate("foxtrot", {"title": "framework scripts change"},
                    registry_text=registry(review_by="2020-01-01"))
    finally:
        lp.review_status = original
    assert called == []


# ---------------------------------------------------------------------------
# The column is READ -- the half that was dead for `expires`.
# ---------------------------------------------------------------------------

def test_review_by_is_parsed_off_the_table():
    assert _pin()["review_by"] == "2026-11-04"


def test_a_header_whose_name_contains_an_underscore_is_read():
    """The measured regression: `_strip_markdown` ate the `_` in `review_by`,
    so the column normalized to `reviewby` and silently read empty."""
    assert lp._norm_header("review_by") == "review_by"
    assert lp._norm_header("Review By") == "review_by"
    assert lp._norm_header("`review_by`") == "review_by"
    assert _pin()["review_by"] != ""


def test_columns_are_read_by_NAME_not_by_position():
    """Reordering the metadata columns must not make a field read its
    neighbour's text -- the fragility that let `expires` die at cells[6]."""
    swapped = "| id | agent | in-lane | out-of-lane | review_by | source | expires | granted |"
    text = registry(header=swapped).replace(
        "| 2026-08-06 | user directive | user directive only | 2026-11-04 |",
        "| 2026-11-04 | user directive | user directive only | 2026-08-06 |")
    pin = lp.parse_pins(text)[0]
    assert pin["review_by"] == "2026-11-04"
    assert pin["granted"] == "2026-08-06"


def test_expires_still_parses_after_the_move_off_positional_reads():
    assert _pin()["expires"] == "user directive only"


# ---------------------------------------------------------------------------
# Fail-open: never manufacture a prompt nobody can act on.
# ---------------------------------------------------------------------------

def test_before_the_date_there_is_no_prompt():
    assert lp.review_status(_pin(), "2026-08-11") is None


def test_on_the_date_itself_there_is_no_prompt():
    assert lp.review_status(_pin(review_by="2026-08-11"), "2026-08-11") is None


def test_past_the_date_the_prompt_says_still_enforced():
    r = lp.review_status(_pin(), "2026-12-01")
    assert r["days_overdue"] == 27
    assert r["days_since_grant"] == 117
    assert "STILL ENFORCED" in r["message"]
    assert "confirm or retire" in r["message"].lower()
    assert "pin-001" in r["message"]


def test_a_pin_with_no_review_by_never_prompts():
    text = registry(header="| id | agent | in-lane | out-of-lane | granted | source | expires |")
    pin = lp.parse_pins(text)[0]
    assert pin["review_by"] == ""
    assert lp.review_status(pin, "2099-01-01") is None


def test_an_unparseable_review_by_never_prompts():
    assert lp.review_status(_pin(review_by="whenever the user says so"), "2099-01-01") is None


def test_a_missing_grant_date_still_prompts_and_says_so():
    r = lp.review_status(_pin(granted="not recorded"), "2026-12-01")
    assert r["days_since_grant"] is None
    assert "grant date not recorded" in r["message"]


def test_review_status_never_raises_on_junk():
    for junk in (None, "", 7, [], {}, {"review_by": None}, {"review_by": ["x"]}):
        assert lp.review_status(junk, "2099-01-01") is None


# ---------------------------------------------------------------------------
# The startup surface.
# ---------------------------------------------------------------------------

def test_the_surface_is_silent_for_an_agent_with_no_pin():
    assert lpr.collect("alpha", registry(review_by="2020-01-01"), lp, "2099-01-01") == []


def test_the_surface_is_silent_before_the_date():
    assert lpr.collect("foxtrot", registry(), lp, "2026-08-11") == []


def test_the_surface_reports_the_pinned_agent_past_the_date():
    rows = lpr.collect("foxtrot", registry(), lp, "2026-12-01")
    assert len(rows) == 1 and rows[0]["pin_id"] == "pin-001"


def test_the_surface_repeats_rather_than_latching():
    """It must keep saying so until a human answers -- there is deliberately no
    acknowledgement file to suppress it."""
    for today in ("2026-12-01", "2027-06-01", "2030-01-01"):
        assert len(lpr.collect("foxtrot", registry(), lp, today)) == 1


def test_the_banner_says_enforced_and_names_only_the_user_as_resolver():
    body = lpr.render(lpr.collect("foxtrot", registry(), lp, "2026-12-01"))
    assert "STILL ENFORCED" in body
    assert "NOT an expiry" in body
    assert "ONLY THE USER" in body
    for forbidden in ("auto-retire", "automatically retired", "has expired"):
        assert forbidden not in body.lower().replace("-", "-")
