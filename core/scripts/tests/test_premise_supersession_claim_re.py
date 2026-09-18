"""Regression tests for premise_supersession_check.CLAIM_RE ().

CLAIM_RE had NO test coverage before this file, and it had no TIME UNITS — so
the check was structurally blind on the class of goal whose premises decay
fastest. Both cargo-cult-detector escalation templates state their premises in
HOURS, as does every recurring-starvation Unblock. Measured on g-001-845's own
description, the matcher found 2 claims and missed the four that WERE the
premise ('0.45h', '0.3h', '0.33h', '1.0h').

The negative controls below are the point of this file. They are NOT invented
shapes: both are strings the widening newly admitted when bare `d` was included,
pulled from live non-terminal goal descriptions.

  '456d'     — a segment of an SES messageId UUID (...-68cc-456d-9cc7-...)
  '658385d'  — a git short sha ('Lodestar-Web-App @ 658385d')

Both are duration-SHAPED, so a regex-shape check over the newly-admitted set
reports them clean; only reading their surrounding context separates them. That
is why `d` is excluded and `h` is not: `d` is a HEX DIGIT and therefore occurs
inside UUIDs and shas, while `h` cannot appear in either. The exclusion is safe
by construction, not merely by measurement — but it is measured too.

Hermetic: imports the module and exercises the compiled pattern. Reads no store.
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CORE_SCRIPTS))

from premise_supersession_check import CLAIM_RE  # noqa: E402


def _claims(text: str) -> list[str]:
    return [" ".join(m.group("claim").split()) for m in CLAIM_RE.finditer(text)]


def test_duration_claims_in_hours_are_enumerated():
    """The  case: hour premises were invisible and are the whole argument."""
    text = "interval is 0.45h, next contraction 0.3h, below the floor of 0.33h (0.33x original 1.0h)"
    found = {c.lower() for c in _claims(text)}
    for expected in ("0.45h", "0.3h", "0.33h", "1.0h"):
        assert expected in found, f"{expected!r} not enumerated; got {sorted(found)}"


def test_day_and_week_durations_are_enumerated():
    text = "the window was 14 days, re-probed after 7 days, cadence 2 weeks, timeout 24h"
    found = {c.lower() for c in _claims(text)}
    assert {"14 days", "7 days", "2 weeks", "24h"} <= found, sorted(found)


def test_preexisting_claim_shapes_still_match():
    """Regression floor — the widening must not disturb what already worked."""
    text = "447 of 491 rows, 37%, 58,322 B, 2049 records, 1,212 goals"
    found = {c.lower() for c in _claims(text)}
    assert "447 of 491" in found, sorted(found)
    assert "37%" in found, sorted(found)
    assert "58,322 b" in found, sorted(found)
    assert "2049 records" in found, sorted(found)


def test_uuid_segment_is_not_read_as_a_duration():
    """ADVERSARIAL CONTROL, from the live corpus (), not invented.

    Bare `d` would match '456d' here. It is a hex segment of an SES messageId.
    """
    text = ("the send carries an SES messageId "
            "010f01a0a4c6e74a-21b77906-68cc-456d-9cc7-205354f2a1b8-000000@us-east-2.amazonses.com")
    assert not any(c.lower().endswith("d") for c in _claims(text)), _claims(text)


def test_git_short_sha_is_not_read_as_a_duration():
    """ADVERSARIAL CONTROL, from the live corpus (), not invented.

    Bare `d` would match '658385d', a git short sha, as 658,385 days.
    """
    text = "measured, Lodestar-Web-App @ 658385d, the description says wire an EventBridge cadence"
    assert not any(c.lower().endswith("d") for c in _claims(text)), _claims(text)


def test_bare_minute_forms_stay_out():
    """A bare `\\d+\\s*m` matches ordinary prose; `min` was never admitted either."""
    assert _claims("it took 5m and then 12 min more") == []


def test_day_suffix_stays_out_even_when_it_looks_like_a_duration():
    """`30d` reads as a plain duration and is still excluded — the exclusion is
    about what `d` CAN collide with, not about whether a given instance is real."""
    assert _claims("the window was 30d") == []
