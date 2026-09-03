"""The starvation Unblock names SINGLE-AGENT ROUTING first ().

WHAT WAS WRONG, and why a re-ordering is a real fix rather than cosmetics.
The description's LAST sentence used to read "If the goal is routed to a
specific agent, confirm that agent is live." Measured 2026-09-02, that check
answers YES in the case that actually occurs: g-353-04 stopped for 144h and
g-115-8602 for 9h while their owner (bravo) was alive and working the whole
time, both sitting at ranks 1097/1180 on its queue with recurring_urgency
already AT the urgency_max clamp — so no further waiting could ever raise
them — while every peer was excluded by block_reason=routed_to_agent. A reader
following the instruction confirmed liveness, ruled routing out, and went
looking for a defect that was not there. The detector then filed two HIGH
Unblock goals that no Body could act on either.

The liveness question is not merely low-yield, it is ANTI-correlated: a DORMANT
owner opens the selector's idle-reallocation escape, so some other Body picks
the goal up and it never reaches this detector. A recurring goal that is still
starving is therefore weak evidence its owner is AWAKE.

Both the promotion and the removal are pinned. Sweeping the old sentence in the
same change is the point (guard-1710) — leaving it in place would put a
retracted conclusion and its replacement in one paragraph.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location(
    "recurring_starvation_routing_hypothesis",
    str(SCRIPT_DIR / "recurring-starvation-check.py"),
)
rsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsc)


def _row(intended_agent="either", **over):
    """One starved row in the shape scan() emits (mirrors the dedup-key
    fixture, including `anchor` — guard-920: a fixture missing it exercises
    the None-anchor fallback while claiming production shape)."""
    row = {
        "goal_id": "g-353-04", "aspiration_id": "asp-001", "source": "world",
        "title": "Recurring: synthetic sweep", "age_hours": 150.0,
        "anchor_field": "lastAchievedAt", "anchor": "2026-08-01T00:00:00",
        "interval_hours": 24, "basis_hours": 30.0, "basis_reason": "interval",
        "ratio": 5.0, "declared_ratio": 6.25,
    }
    if intended_agent is not None:
        row["intended_agent"] = intended_agent
    row.update(over)
    return row


def _payload(monkeypatch, row):
    captured = {}
    monkeypatch.setattr(
        rsc._rt, "aspirations_add_goal",
        lambda asp_id, payload, source=None: (
            captured.update(payload) or {"id": "g-115-9001"}))
    rsc._file_unblock(row)
    return captured


def _pos(text, needle):
    i = text.find(needle)
    assert i >= 0, "missing %r in:\n%s" % (needle, text)
    return i


def test_routed_goal_names_routing_before_every_other_hypothesis(monkeypatch):
    body = _payload(monkeypatch, _row("bravo"))["description"]
    assert "intended_agent=bravo" in body
    assert "block_reason=routed_to_agent" in body
    # ORDERING IS THE FIX. Assert position, not presence: the retracted version
    # also "mentioned routing" — at the very end, after three other leads.
    routing = _pos(body, "SINGLE-AGENT ROUTING")
    for later in ("No blocker, no defer, no claim",
                  "Ask why it is not being selected"):
        assert routing < _pos(body, later), (
            "routing must precede %r; it is the most likely cause" % later)


def test_it_asks_about_the_owners_RANKING_not_the_owners_LIVENESS(monkeypatch):
    """The precise correction. "Is the owner alive?" returns YES in the case
    that actually happens, so it is worse than no instruction at all.
    """
    body = _payload(monkeypatch, _row("bravo"))["description"]
    assert "RANKING" in body and "NOT bravo's liveness" in body
    assert "confirm that agent is live" not in body, (
        "the retracted check must not survive anywhere in the artifact "
        "(guard-1710)")
    assert "1097/1180" in body, "the measurement that retracts it must travel"


def test_either_routed_goal_records_a_measured_negative(monkeypatch):
    """Not silence. A reader has to be able to tell "routing was checked and is
    not the cause" from "routing was never considered" — the two look identical
    when the paragraph is simply absent, and the second is what this whole
    change is about.
    """
    body = _payload(monkeypatch, _row("either"))["description"]
    assert "Routing is NOT the cause" in body
    assert "intended_agent=either" in body
    assert "SINGLE-AGENT ROUTING" not in body


def test_absent_field_reports_a_gap_never_an_all_clear(monkeypatch):
    """Rows are hand-built by callers and tests, so the field can be missing.
    Degrading to "not routed" would manufacture a confident negative out of an
    absence — the exact class guard-2298 names.
    """
    body = _payload(monkeypatch, _row(intended_agent=None))["description"]
    assert "Routing was NOT measured" in body
    assert "Routing is NOT the cause" not in body
    assert "SINGLE-AGENT ROUTING" not in body


def test_dedup_key_is_unaffected_by_the_description_change(monkeypatch):
    """The key is built from goal_id/source/anchor, never the body. Pinned
    because a description edit that silently re-keyed the Unblock would make
    the sweep re-file every run (rb-3879).
    """
    row = _row("bravo")
    payload = _payload(monkeypatch, row)
    assert payload["origin_signal"] == rsc._origin_signal(
        row["goal_id"], row["source"], anchor=row.get("anchor"))
