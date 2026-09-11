"""The deadline gate's `suggested` must be a CEILING resolver, not the defer one.

g-115-9516. `gates/deadline_date.py` sourced its suggestion from
`defer_date.extract`, which answers a different question — "when may this goal
RESUME?" — and carries two policies that are correct for a FLOOR and inverted
for a CEILING:

  1. DUE-BY SUPPRESSION (g-115-1783): a date governed by due-by language is
     discarded so a due date never becomes a `deferred_until` floor. The
     deadline gate fires ONLY on deadline cues, so it was querying the resolver
     on precisely the text the resolver exists to discard.
  2. STRICTLY-FUTURE: dates parse at 00:00 and only strictly-future matches
     survive, so a deadline falling TODAY was always None.

Both null the suggestion exactly where the deadline is most urgent. Measured
2026-09-09 on the live alert lane: 3 of 5 refused owner directives carried
`suggested=null` and the 2 that resolved were both tomorrow-dated.

These tests pin the ceiling behaviour AND pin that the floor resolver was not
"fixed" by breaking it — that separation is the whole point of the repair.
"""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "gates"))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G = _load("deadline_date_gate_mod", "gates/deadline_date.py")
D = _load("defer_date_mod", "gates/defer_date.py")

NOW = datetime(2026, 9, 9, 17, 55, 0)


# --- inversion 2: a deadline TODAY is the case the gate exists for -----------

def test_same_day_deadline_yields_a_suggestion():
    assert G._suggest_ceiling("response due September 9, 2026 at 2:00pm PDT",
                              now=NOW) == "2026-09-09T00:00:00"


def test_same_day_iso_deadline_yields_a_suggestion():
    assert G._suggest_ceiling("response due by 2026-09-09 noon ET",
                              now=NOW) == "2026-09-09T00:00:00"


# --- inversion 1: due-by phrasing is the MOST explicit deadline, not the least

def test_due_by_governed_date_yields_a_suggestion():
    assert G._suggest_ceiling("submit by September 12 2026",
                              now=NOW) == "2026-09-12T00:00:00"


def test_trailing_deadline_word_yields_a_suggestion():
    assert G._suggest_ceiling("September 12 2026 deadline",
                              now=NOW) == "2026-09-12T00:00:00"


# --- earliest wins; already-past dates are not ceilings ----------------------

def test_earliest_future_date_wins():
    text = "kickoff September 20 2026, but the response is due September 11 2026"
    assert G._suggest_ceiling(text, now=NOW) == "2026-09-11T00:00:00"


def test_past_dates_are_not_offered_as_a_ceiling():
    assert G._suggest_ceiling("thread opened 2026-08-01, see August 3 2026",
                              now=NOW) is None


# --- POSITIVE CONTROLS ------------------------------------------------------
# Without these, a resolver that returned "today" for every input would pass
# every test above and make the suggestion worthless.

def test_dateless_text_yields_no_suggestion():
    assert G._suggest_ceiling("response due at noon ET, no absolute date here",
                              now=NOW) is None


def test_empty_text_yields_no_suggestion():
    assert G._suggest_ceiling("", now=NOW) is None


def test_suggestion_does_not_widen_what_the_gate_FIRES_on():
    """`_suggest_ceiling` is suggestion-only: it must never make the gate block
    a record it would otherwise pass. `find_cue` remains the sole trigger."""
    payload = {"title": "Investigate: closes the goal after verify",
               "description": "ordinary prose mentioning 2026-09-12 in passing"}
    r = G.evaluate(payload, now=NOW)
    assert r["would_block"] is False
    assert r["cue"] is None


def test_a_present_ceiling_field_still_short_circuits():
    payload = {"title": "Directive: X",
               "description": "response due September 9, 2026 at 2:00pm PDT",
               "resolves_by": "2026-09-09T21:00:00"}
    r = G.evaluate(payload, now=NOW)
    assert r["would_block"] is False
    assert "deadline field present" in r["reason"]


# --- THE DISCRIMINATOR ------------------------------------------------------
# The ceiling repair lives in deadline_date.py. defer_date.py's floor semantics
# must be untouched, or  (a goal frozen until its own deadline)
# regresses. Fixing the ceiling by relaxing the floor would pass every test
# above and silently reintroduce that incident.

def test_defer_resolver_still_suppresses_due_by_dates():
    assert D.extract("submit by September 12 2026", now=NOW)["matched"] is False
    assert D.extract("September 12 2026 deadline", now=NOW)["matched"] is False


def test_defer_resolver_still_resolves_a_start_after_date():
    got = D.extract("not before September 12 2026", now=NOW)
    assert got["matched"] is True
    assert got["deferred_until"] == "2026-09-12T00:00:00"


def test_gate_refusal_now_carries_the_suggestion_for_a_same_day_deadline():
    """End-to-end: the refusal a caller reads must name a usable ceiling."""
    payload = {"title": "Directive: request for statement of work",
               "description": "response due September 9, 2026 at 2:00pm PDT"}
    r = G.evaluate(payload, now=NOW)
    assert r["would_block"] is True
    assert r["suggested"] == "2026-09-09T00:00:00"
    assert "suggest resolves_by=2026-09-09T00:00:00" in r["reason"]
