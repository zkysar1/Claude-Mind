"""Tests for the deadline-date gate (gates/deadline_date.py).

Origin: user directive 2026-09-04 (g-115-8906) — "you get an error if you
add something without a date."

These call `evaluate()` DIRECTLY and never issue a production write.
guard-1006: probing a write-path gate with the real write command means that
if the gate does NOT block, the throwaway payload LANDS as live state.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gates.deadline_date import evaluate, find_cue, CEILING_FIELDS, FLOOR_FIELDS  # noqa: E402


NOW = datetime(2026, 9, 4, 12, 0, 0)


# --- The incident itself -------------------------------------------------
# These two strings are the verbatim text of the records whose missing dates
# produced the directive. If either stops blocking, the gate no longer covers
# the case it was built for.

def test_incident_title_blocks():
    r = evaluate({"title": "Directive: Re: [Omni] Army software bid closes "
                           "4pm ET today - go or skip?"}, now=NOW)
    assert r["would_block"] is True
    assert r["cue"] == "closes"


def test_incident_body_blocks():
    r = evaluate({"title": "Screened opportunity",
                  "description": "Quotes are due at 4:00pm Eastern today."},
                 now=NOW)
    assert r["would_block"] is True
    assert r["cue"] == "due"


# --- Detector is not the resolver ----------------------------------------

def test_deadline_without_parseable_date_still_blocks():
    """The high-risk shape: commits to a deadline, names no absolute date.

    `defer_date.extract` returns matched=False here. A gate built on the
    resolver would pass this — which is exactly the incident.
    """
    from gates.defer_date import extract
    text = "bid closes 4pm ET today"
    assert extract(text, now=NOW)["matched"] is False, \
        "premise changed: extract now resolves this; re-check the gate design"
    r = evaluate({"title": text}, now=NOW)
    assert r["would_block"] is True
    assert r["suggested"] is None


def test_parseable_date_is_offered_as_a_suggestion():
    r = evaluate({"title": "Submit brief",
                  "description": "submission deadline September 12 2026"}, now=NOW)
    assert r["would_block"] is True
    assert r["suggested"] == "2026-09-12T00:00:00"
    assert "resolves_by=" in r["reason"]


# --- Ceilings vs floors (guard-2073 / guard-2458) ------------------------

def test_each_ceiling_field_discharges():
    for f in CEILING_FIELDS:
        r = evaluate({"title": "bid closes 4pm ET today", f: "2026-09-04"}, now=NOW)
        assert r["would_block"] is False, f
        assert f in r["reason"]


def test_floor_alone_does_not_discharge():
    """A floor raises no alarm, so a floor-only record is as invisible as none."""
    for f in FLOOR_FIELDS:
        r = evaluate({"title": "bid closes 4pm ET today", f: "2026-09-04"}, now=NOW)
        assert r["would_block"] is True, f
        assert "FLOOR" in r["reason"]


# --- False-positive shapes measured on the live corpus -------------------
# Every string below fired under the first (proximity-window) design and was
# hand-judged false. They are pinned so a future widening cannot silently
# reintroduce the 21.2%-of-corpus firing rate.

def test_framework_close_vocabulary_does_not_fire():
    for text in [
        "iteration-close.sh closes the goal after verify",
        "Idea: recurring-close.sh should forward --override-domain-suite",
        "Investigate: UNRESOLVABLE conflates three causes",   # 'EXPIRED' status word
        "the sweep ends with diverged_skipped",
        "grew 68% in 25h past its own fold-before threshold",
        "Fold checker-input-assumption-defects.md before deciding",
        "failure due to a race condition",                     # causal 'due to'
        "Idea: VALID_USER_LEG_SCOPES has no domain-extension path for submissions",
    ]:
        assert find_cue(text) is None, text
        assert evaluate({"title": text}, now=NOW)["would_block"] is False, text


def test_hyphenated_compound_is_a_token_not_a_commitment():
    for text in ["a due-today marker", "the deadline-today token", "WAKE-DUE TIME"]:
        assert find_cue(text) is None, text


def test_bare_iso_date_in_prose_does_not_fire():
    """ISO dates are ambient in framework prose; only a cue may bind one."""
    assert find_cue("measured 2026-08-11 on cc-04, 2026-08-12 on cc-05") is None


# --- Structural behaviour ------------------------------------------------

def test_recurring_goal_is_exempt():
    r = evaluate({"title": "quotes due today", "recurring": True}, now=NOW)
    assert r["would_block"] is False
    assert "recurring" in r["reason"]


def test_interval_hours_also_exempt():
    r = evaluate({"title": "quotes due today", "interval_hours": 24}, now=NOW)
    assert r["would_block"] is False


def test_override_passes_and_reports_itself():
    r = evaluate({"title": "bid closes 4pm ET today"},
                 override_deadline="tracked in the vendor portal, not here",
                 now=NOW)
    assert r["would_block"] is False
    assert r["override_applied"] == "tracked in the vendor portal, not here"


def test_override_is_audited_to_the_ledger(tmp_path):
    evaluate({"title": "bid closes 4pm ET today"},
             override_deadline="why", agent_name="alpha",
             world_dir=tmp_path, now=NOW)
    led = tmp_path / "deadline-date-overrides.jsonl"
    assert led.exists()
    body = led.read_text(encoding="utf-8")
    assert "why" in body and "alpha" in body


def test_gate_fails_open_on_internal_error():
    """guard-142 — a gate must never fail CLOSED on its own dependency error."""
    class Exploding(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")
    r = evaluate(Exploding(), now=NOW)
    assert r["would_block"] is False
    assert "failing open" in r["reason"]


def test_empty_payload_is_silent():
    assert evaluate({}, now=NOW)["would_block"] is False


def test_reverse_order_construction():
    r = evaluate({"title": "prep", "description": "there is a 4pm deadline"}, now=NOW)
    assert r["would_block"] is True
    assert r["cue"] == "deadline-rev"
