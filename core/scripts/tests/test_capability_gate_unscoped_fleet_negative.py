"""Unscoped fleet-wide capability negative — ADVISORY (, guard-1412).

A defer_reason that universally quantifies a capability negative over the fleet
("no agent in the fleet can obtain a shell on X", "unreachable fleet-wide")
almost always generalises a SINGLE-BOX measurement, and freezes the goal for
every box including the ones that can do it.

Canonical incident 2026-08-17: a goal was re-deferred twelve minutes after being
cleared, on "no agent in the fleet can obtain a shell on <host>", while the
clearing agent held a working shell on exactly that host.

WHY THESE PINS. Two properties are easy to break and silent when broken:

  1. POLARITY (test_negation_may_live_in_the_quantifier). The first cut of this
     predicate required a NEGATIVE verb ("cannot obtain"). The real incident
     text says "no agent in the fleet CAN OBTAIN" -- the negation lives in the
     quantifier -- so it scored 0 on the exact case it was written for. Anyone
     re-tightening the verb list to reduce false positives will reintroduce
     this, and the corpus will not complain: it is a RECALL loss, invisible in
     a flagged-count check.
  2. ADVISORY-NESS (test_never_changes_would_block). The measured population is
     1 flag in 163 live defers. At that volume a false refusal costs more than
     the defect it prevents, so this must never reach would_block. A future
     edit promoting it to a refusal has to fail here.
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "gates"))

from capability import _match_unscoped_fleet_negative as match  # noqa: E402


# --- flags: universal quantifier + reachability claim + no box named ---------
@pytest.mark.parametrize("text", [
    "no agent in the fleet can obtain a shell on the inference host",
    "precondition_unmet: peer relay to zds-mind unreachable fleet-wide",
    "nobody can reach the relay; no route exists",
    "no box can ssh to the inference host",
    "no machine can connect to the endpoint",
])
def test_flags_unscoped_fleet_claims(text):
    assert match(text) is not None, f"should have flagged: {text!r}"


def test_negation_may_live_in_the_quantifier():
    """The canonical incident text. Uses the POSITIVE verb 'can obtain' with the
    negation carried by 'no agent in the fleet'. A negative-verb-only predicate
    double-counts the negation and misses this -- measured, not hypothetical."""
    assert match("no agent in the fleet can obtain a shell on the pod") is not None


# --- clears: a named box, or a claim that is not about machine reachability --
@pytest.mark.parametrize("text,why", [
    ("no agent in the fleet can obtain a shell on zakpod1",
     "host token names the box"),
    ("unreachable from cc-04; measured on cc-04 at 14:02",
     "explicit provenance"),
    ("no route to the relay, measured from LAPTOP-3IOFCNEO",
     "measured-from phrase"),
    ("human_blocked: compliance judgment, no one can decide but the owner",
     "human gate makes no machine claim -- the measured false positive"),
    ("waiting on the vendor to publish the SDK", "no fleet quantifier"),
    ("blocked on user approval for the deploy", "no reachability claim"),
    ("", "empty"),
    (None, "none"),
])
def test_clears_when_scoped_or_irrelevant(text, why):
    assert match(text) is None, f"should NOT have flagged ({why}): {text!r}"


def test_naming_any_box_is_the_escape_hatch():
    """The fix is always to ADD evidence, never to reword around the check:
    the same sentence flags bare and clears once a host is named."""
    bare = "no agent in the fleet can obtain a shell"
    assert match(bare) is not None
    assert match(bare + " on cc-05") is None


def test_never_changes_would_block():
    """ADVISORY. Promoting this to a refusal must fail this test.

    Asserts on the SPECIFIC advisory fields rather than a coarse rc (guard-1082),
    and pins that a text which flags is not itself sufficient to block."""
    from capability import evaluate

    res = evaluate("peer relay unreachable fleet-wide",
                   intended_participants="agent")
    assert res["unscoped_fleet_negative_detected"] is True
    assert res["unscoped_fleet_negative_claim"] is not None
    # intended_participants=agent cannot be blocked by the capability match,
    # so any block here would have to come from the new advisory.
    assert res["would_block"] is False, (
        "the unscoped-fleet advisory must never drive would_block -- "
        "measured population is 1 in 163 live defers, too rare to justify "
        "refusing a write (guard-1562)"
    )


def test_payload_fields_always_present():
    """Both keys must exist even when nothing is detected, so a consumer can
    read them unconditionally instead of .get()-ing around their absence."""
    from capability import evaluate

    res = evaluate("waiting on the vendor", intended_participants="agent")
    assert res["unscoped_fleet_negative_detected"] is False
    assert res["unscoped_fleet_negative_claim"] is None
