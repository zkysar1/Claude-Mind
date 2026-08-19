"""The starvation Unblock must not claim more than it checked ().

THREE REPORTING DEFECTS, all pinned here. None of them changes which goals the
sweep fires on — FW-1 substantive demotion works as designed and is deliberately
untouched. What changes is what the filed Unblock TELLS its reader.

1. THE UNIVERSAL CLAIM. The body used to read "...its structured gates currently
   pass — so nothing is parking it deliberately and nothing has errored." Four
   checks, turned by that "so" into a claim about every possible cause. The
   detector never consults the scorer (`grep goal_selector|substantive|demotion`
   over the script returns nothing), and goal-selector.py's
   apply_substantive_demotion caps a recurring goal's score beneath the best
   substantive candidate — which IS deliberate parking, sitting outside all four
   checks. A reader was sent hunting a defect that was not there.

2. THE RATIO SCALE. The body reports elapsed/cadence. The scorer's exemption is
   compared against goal-selector.py:3409's (elapsed - interval)/interval, which
   is exactly one less. So a "5.33x" headline reads as past a documented
   exemption while the scorer sees 4.33 and demotes anyway. Measured live
   2026-08-10 on g-115-2155 at exactly those numbers.

   The fix emits the scorer's number and names NO value for the bar, because
   there isn't one: goal-selector.py:4015 overdue_exemption_level takes THREE
   knobs and returns a graded fraction — the pure-ratio arm divides by 5.0, but
   a monitor-class goal (interval <= 6h) is fully exempt at 1x excess. Pinning a
   single constant here would reproduce this goal's own defect one layer down,
   so no test asserts one.

3. THE UNMEASURED BASIS. basis_reason="interval" covered two different states —
   no usable samples (the basis is the DECLARED cadence, unmeasured) and samples
   that the declared cadence already covers. Only the first is the cry-wolf class
   the basis gate exists to prevent, and the body presented both identically.
   Measured live the same day: 8 of 10 starved rows had NO measured basis.

Both new fields are ADDITIVE. basis_reason keeps its existing values because six
assertions pin `== "interval"` exactly (test_streak_break_canary_basis L78/85/
110/119, test_recurring_starvation_check L89/156), and a reporting fix has no
business breaking them.
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location(
    "recurring_starvation_check",
    str(SCRIPT_DIR / "recurring-starvation-check.py"),
)
rsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsc)


def _ago(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).replace(
        microsecond=0).isoformat()


def _goal(**over) -> dict:
    g = {
        "id": "g-999-01",
        "title": "Recurring: synthetic sweep",
        "recurring": True,
        "status": "pending",
        "interval_hours": 6,
        "lastAchievedAt": _ago(20),
    }
    g.update(over)
    return g


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    monkeypatch.setattr(rsc, "_read_active", lambda source: [])


def _install(monkeypatch, goals):
    monkeypatch.setattr(
        rsc, "_read_active",
        lambda source: ([{"id": "asp-999", "goals": list(goals)}]
                        if source == "world" else []))


def _capture_unblock(monkeypatch, row):
    """Render a real Unblock body through the real code path, filing nothing.

    _rt.aspirations_add_goal is the ONLY mutating call in _file_unblock, so
    stubbing exactly it exercises everything above and writes nothing. Learned
    the hard way: a name-heuristic reach for a "payload builder" finds
    _file_unblock, which FILES.
    """
    seen = {}

    def _stub(asp_id, payload, *a, **k):
        seen["payload"] = payload
        return {"id": "g-DRYRUN-000"}

    monkeypatch.setattr(rsc._rt, "aspirations_add_goal", _stub)
    monkeypatch.setattr(rsc, "_existing_origin_signals", lambda: set())
    rsc._file_unblock(row)
    assert "payload" in seen, "no payload captured — a guard short-circuited"
    return seen["payload"]


# ── Defect 2: the scorer's scale ──────────────────────────────────────────

def test_selector_excess_ratio_is_exactly_one_less_than_declared(monkeypatch):
    """The scorer's exemption is compared against THIS number, not the headline."""
    _install(monkeypatch, [_goal()])
    starved, _ = rsc.scan(3.0, breaks={})
    row = starved[0]
    assert row["selector_excess_ratio"] == pytest.approx(
        row["declared_ratio"] - 1.0, abs=0.011), (
        "selector_excess_ratio must equal declared_ratio - 1: goal-selector.py:3409 "
        "computes (elapsed - interval)/interval and the 5.0 exemption is compared "
        "against that. If this drifts, the Unblock sends its reader to the wrong "
        "number again."
    )


def test_selector_excess_ratio_never_negative(monkeypatch):
    """A goal barely past its interval must not report a negative excess."""
    _install(monkeypatch, [_goal(interval_hours=6, lastAchievedAt=_ago(19))])
    starved, _ = rsc.scan(3.0, breaks={})
    assert starved[0]["selector_excess_ratio"] >= 0.0


# ── Defect 3: measured vs assumed basis ───────────────────────────────────

def test_basis_measured_false_when_no_samples(monkeypatch):
    _install(monkeypatch, [_goal()])
    starved, _ = rsc.scan(3.0, breaks={})
    assert starved[0]["basis_measured"] is False
    # The enum stays put — six other assertions depend on it.
    assert starved[0]["basis_reason"] == "interval"


def test_basis_measured_true_when_p50_available(monkeypatch):
    _install(monkeypatch, [_goal(lastAchievedAt=_ago(200))])
    starved, _ = rsc.scan(3.0, breaks={"g-999-01": [30.0, 30.0, 30.0]})
    row = starved[0]
    assert row["basis_measured"] is True
    assert row["basis_reason"] == "recent_actual_p50"
    assert row["basis_hours"] == pytest.approx(30.0)


def test_body_flags_an_unmeasured_basis(monkeypatch):
    _install(monkeypatch, [_goal()])
    starved, _ = rsc.scan(3.0, breaks={})
    body = _capture_unblock(monkeypatch, starved[0])["description"]
    assert "UNMEASURED" in body, (
        "an unmeasured basis must say so — otherwise a declared interval is "
        "presented as though it were a demonstrated cadence, which is the "
        "cry-wolf class the basis gate exists to prevent"
    )


def test_body_does_not_flag_a_measured_basis(monkeypatch):
    """The complement — without it, the flag could be unconditional and still pass."""
    _install(monkeypatch, [_goal(lastAchievedAt=_ago(200))])
    starved, _ = rsc.scan(3.0, breaks={"g-999-01": [30.0, 30.0, 30.0]})
    body = _capture_unblock(monkeypatch, starved[0])["description"]
    assert "UNMEASURED" not in body


# ── Defect 1: the universal claim ─────────────────────────────────────────

def test_body_drops_the_universal_parking_claim(monkeypatch):
    _install(monkeypatch, [_goal()])
    body = _capture_unblock(monkeypatch, rsc.scan(3.0, breaks={})[0][0])["description"]
    assert "nothing is parking it deliberately" not in body, (
        "four checks cannot support a claim about every possible cause; the "
        "scorer's substantive demotion is deliberate parking outside all four"
    )


def test_body_names_the_scorer_as_an_unchecked_cause(monkeypatch):
    """Dropping the claim is half the fix — the reader needs the lead it lost."""
    _install(monkeypatch, [_goal()])
    body = _capture_unblock(monkeypatch, rsc.scan(3.0, breaks={})[0][0])["description"]
    assert "scorer" in body.lower()
    assert "selector's overdue exemption" in body


# ── Backward compatibility ────────────────────────────────────────────────

def test_hand_built_row_without_new_fields_still_renders(monkeypatch):
    """Rows are built by callers and tests too — a missing field must degrade
    to the PRE-fix text, never to a wrong claim."""
    legacy = {
        "goal_id": "g-999-02", "aspiration_id": "asp-999", "source": "world",
        "title": "legacy row", "age_hours": 100.0, "anchor_field": "lastAchievedAt",
        "interval_hours": 16, "basis_hours": 16.0, "basis_reason": "interval",
        "ratio": 6.25, "declared_ratio": 6.25, "intended_agent": "either",
    }
    body = _capture_unblock(monkeypatch, legacy)["description"]
    assert "UNMEASURED" not in body          # absent field => no unproven claim
    assert "On the scorer's own scale" not in body   # no ratio to report
    assert "nothing is parking it deliberately" not in body  # still softened
