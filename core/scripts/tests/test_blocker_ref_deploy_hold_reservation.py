""" — a deploy hold is a LEASE, not an open-ended claim.

`deploy-hold:<target>` blocker_refs used to be declared once and owned by
nobody on a cadence: no owner, no bounded window, honored only by whoever
happened to probe. The reservation contract refuses an unbounded declaration
at the write path instead of hoping a later audit catches it.

These tests pin FOUR properties:
  1. Every deny branch emits its REFUSAL PAYLOAD, not merely a False predicate
     (guard-3803: a gate's fail-open handler also covers its own deny-message
     construction, so a bug while COMPOSING a refusal silently converts that
     refusal into an approval; a predicate-only test passes while the gate
     approves). Every assertion below reads the message text.
  2. The grandfather clause holds for the REAL pre-contract refs (guard-2400:
     a validator added after records exist makes every subsequent write to
     those records fail, forever, for every writer — including the writes that
     would clear them). The two fixtures are the exact refs measured live on
     2026-08-26, not invented shapes.
  3. The audited long-hold override relaxes the WINDOW and nothing else — an
     owner and an explicit expiry are still required, because those are what
     make a long hold accountable rather than merely long.
  4. Non-deploy-hold refs are untouched — the gate is keyed on the external_id
     prefix, so widening the vocabulary must not disturb any existing writer.

Pure-function tests against gates.blocker_ref: no daemon, no world writes, no
subprocess. Safe under any STORAGE_BACKEND.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gates.blocker_ref import (  # noqa: E402
    DEPLOY_HOLD_CONTRACT_EFFECTIVE_FROM,
    DEPLOY_HOLD_MAX_HOURS,
    DEPLOY_HOLD_PREFIX,
    validate,
)

# A fixed "now" after the contract cutoff, so nothing here depends on the
# wall clock: a test that silently starts grandfathering everything the day
# the cutoff passes would pass forever while testing nothing.
NOW = datetime(2026, 9, 1, 12, 0, 0)


def _ref(**over):
    """A deploy-hold ref declared AFTER the cutoff (i.e. governed)."""
    base = {
        "type": "infrastructure",
        "external_id": "deploy-hold:Some-Repo:2026-09-01",
        "created_at": "2026-09-01T12:00:00",
        "expires_at": "2026-09-02T12:00:00",   # 24h — inside the window
        "owner": "alpha",
        "why": "staging soak in flight; clears when the soak reports green",
    }
    base.update(over)
    return base


# --- the happy path, first: the gate must not refuse a valid reservation ----

def test_valid_reservation_passes_and_preserves_owner():
    ok, out = validate(_ref(), now=NOW)
    assert ok is True, out
    # `owner` must survive into the OUTPUT — deploy-hold-check.sh reads it off
    # the stored ref to name who to ask on a HELD verdict. A promoted key that
    # validate() drops is indistinguishable from one that was never supplied.
    assert out["owner"] == "alpha"
    assert out["external_id"].startswith(DEPLOY_HOLD_PREFIX)


def test_window_boundary_is_inclusive_at_exactly_max_hours():
    """Exactly 48h is INSIDE the window. Pinned because an off-by-one here
    refuses the most common deliberate choice — the round number a human
    reaches for when told the limit is 48h."""
    ok, out = validate(
        _ref(created_at="2026-09-01T00:00:00",
             expires_at="2026-09-03T00:00:00"),   # exactly 48h
        now=NOW,
    )
    assert ok is True, out


# --- deny branches: assert on the PAYLOAD, never the predicate alone --------

def test_missing_owner_is_refused_and_the_message_says_owner():
    ok, err = validate(_ref(owner=None), now=NOW)
    assert ok is False
    assert isinstance(err, str) and err.strip(), "deny branch emitted no payload"
    assert "owner" in err
    assert "deploy-holds.md" in err


def test_blank_owner_is_refused_like_a_missing_one():
    ok, err = validate(_ref(owner="   "), now=NOW)
    assert ok is False
    assert "owner" in err


def test_missing_explicit_expiry_is_refused_rather_than_ttl_filled():
    """The per-type TTL would silently manufacture an expiry, which is exactly
    the open-ended declaration being refused. The check must read the RAW
    input, so an auto-filled value cannot satisfy the demand for a deliberate
    one."""
    payload = _ref()
    payload.pop("expires_at")
    ok, err = validate(payload, now=NOW)
    assert ok is False
    assert "expires_at" in err
    assert "TTL" in err or "default" in err


def test_over_window_is_refused_and_the_message_names_both_numbers():
    ok, err = validate(
        _ref(created_at="2026-09-01T00:00:00",
             expires_at="2026-09-13T00:00:00"),   # 288h
        now=NOW,
    )
    assert ok is False
    assert "288" in err, err
    assert str(DEPLOY_HOLD_MAX_HOURS) in err
    # The refusal must name the way out, or it reads as a prohibition.
    assert "override" in err.lower()


def test_unparseable_expiry_is_refused_not_assumed_valid():
    ok, err = validate(_ref(expires_at="whenever the soak finishes"), now=NOW)
    assert ok is False
    assert "expires_at" in err


def test_expiry_before_creation_is_refused():
    """A hold that expires before it begins gates nothing while READING as
    active — the worst of both states."""
    ok, err = validate(
        _ref(created_at="2026-09-02T00:00:00",
             expires_at="2026-09-01T00:00:00"),
        now=NOW,
    )
    assert ok is False
    assert "created_at" in err or "before it begins" in err


# --- the audited override: relaxes the WINDOW and nothing else -------------

def test_override_allows_the_long_hold():
    ok, out = validate(
        _ref(created_at="2026-09-01T00:00:00",
             expires_at="2026-09-13T00:00:00"),   # 288h
        now=NOW, allow_long_hold=True,
    )
    assert ok is True, out


@pytest.mark.parametrize("mutation,expect_in_msg", [
    ({"owner": None}, "owner"),
    ({"expires_at": "not-a-date"}, "expires_at"),
])
def test_override_does_not_waive_owner_or_a_readable_expiry(mutation, expect_in_msg):
    """The override is for LONG, not for UNACCOUNTABLE. Stated as its own test
    because 'override' naturally reads as 'skip the deploy-hold checks', and
    that reading would let the override reintroduce the exact open-ended hold
    the contract removes."""
    ok, err = validate(_ref(**mutation), now=NOW, allow_long_hold=True)
    assert ok is False
    assert expect_in_msg in err


# --- guard-2400: the REAL pre-contract population must stay writable -------

@pytest.mark.parametrize("goal_id,ext_id,created,expires", [
    # Measured live in the world queue on 2026-08-26, before this gate shipped.
    ("g-326-148", "deploy-hold:Ayoai-Roblox-Integration",
     "2026-08-21T20:51:29", "2026-08-26T20:51:29"),          # 120h, no owner
    ("g-326-330", "deploy-hold:Ayoai-Environment-Server:2026-08-16",
     "2026-08-16T18:26:00", "2026-08-29T02:30:00"),          # 296h, no owner
])
def test_pre_contract_refs_are_grandfathered(goal_id, ext_id, created, expires):
    """Both of these violate the contract on TWO counts (no owner, far over
    the window). Without the cutoff they become unwritable by every writer on
    the day this gate ships — and g-326-330 is status=blocked, so the wedge
    would have blocked its own unblocking. The breakage would surface only
    when someone tried to write, as a rejection naming a field they did not
    send (guard-2400)."""
    ok, out = validate(
        {"type": "infrastructure", "external_id": ext_id,
         "created_at": created, "expires_at": expires,
         "why": "pre-contract hold"},
        now=NOW,
    )
    assert ok is True, "wedged a live pre-contract ref (" + goal_id + "): " + str(out)


def test_the_cutoff_actually_discriminates():
    """Positive control for the grandfather test above. Without this, a bug
    that grandfathered EVERYTHING would leave both cases green while the gate
    enforced nothing at all — the failure mode that looks exactly like
    success."""
    before = "2026-08-25T23:59:59"   # < DEPLOY_HOLD_CONTRACT_EFFECTIVE_FROM
    after = "2026-08-26T00:00:01"    # >= cutoff
    common = {"type": "infrastructure",
              "external_id": "deploy-hold:Repo",
              "expires_at": "2026-09-30T00:00:00",   # wildly over window
              "why": "x"}
    ok_before, _ = validate(dict(common, created_at=before), now=NOW)
    ok_after, err_after = validate(dict(common, created_at=after), now=NOW)
    assert ok_before is True, "cutoff failed to grandfather a pre-contract ref"
    assert ok_after is False, "cutoff grandfathered a POST-contract ref"
    assert isinstance(err_after, str) and err_after.strip()


def test_cutoff_constant_is_parseable():
    """The grandfather branch silently disables itself if this constant stops
    parsing — an unparseable cutoff makes effective_dt None, the branch never
    fires, and every legacy ref wedges. Cheap to pin, invisible when broken."""
    assert datetime.fromisoformat(DEPLOY_HOLD_CONTRACT_EFFECTIVE_FROM)


# --- regression: non-deploy-hold refs are untouched ------------------------

def test_ordinary_ref_is_unaffected_by_the_deploy_hold_gate():
    ok, out = validate(
        {"type": "infrastructure", "external_id": "efs-mount-down",
         "why": "mount target unreachable"},
        now=NOW,
    )
    assert ok is True, out
    # No owner, no explicit expiry — and that stays perfectly legal off-prefix.
    assert "owner" not in out
    assert out["expires_at"]          # TTL-derived, as before


def test_owner_is_accepted_on_an_ordinary_ref_without_being_required():
    """Widening the vocabulary must not make `owner` mandatory everywhere, and
    must not make it REFUSED anywhere — validate() rejects unknown keys, so a
    half-landed promotion would break any writer that supplies it."""
    ok, out = validate(
        {"type": "resource", "external_id": "gpu-pool-exhausted",
         "owner": "bravo"},
        now=NOW,
    )
    assert ok is True, out
    assert out["owner"] == "bravo"
