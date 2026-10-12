"""test_conftest_pin_intended_agent_roster.py —  (b) regression (omni-388), mind_api twin.

Twin of core/scripts/tests/test_conftest_pin_intended_agent_roster.py — read
its docstring for the full rationale (the goal-selector resolves the roster
through ``aspirations._valid_intended_agents()``, a seam the gates gate-pin
does NOT reach; omni-388's fix pins it in-process in BOTH conftests, and this
is the test that fails without the pin). The two test packages load
independently and neither imports the other's conftest, so the copies MUST stay
identical — a pin guarded in only one tree leaves the other exposed (g-115-5651).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SCRIPTS = REPO_ROOT / "core" / "scripts"
if str(CORE_SCRIPTS) not in sys.path:  # module-level: guard against per-test growth
    sys.path.insert(0, str(CORE_SCRIPTS))

import aspirations  # noqa: E402  (same import the conftest pin patches)

# MUST equal the tuple in BOTH conftests' _pin_intended_agent_roster.
FIXTURE_ROSTER = ("alpha", "bravo", "charlie", "delta", "echo",
                  "foxtrot", "omni", "zeta")


def test_autouse_pin_reaches_the_selector_seam() -> None:
    """The conftest's autouse pin has replaced aspirations's roster root seam.

    The pin is a lambda (see _pin_intended_agent_roster in BOTH conftests); the
    original seam is a module-level `def` named _valid_intended_agents. So the
    attribute's __name__ is '<lambda>' if and only if the autouse pin is in
    effect — deployment-independent, so this fails without the pin on ANY box,
    even one whose live roster happens to equal the fixture set (where a
    value-only check would pass vacuously). THIS is the 'fails without the pin'
    regression g-358-248 (b) asks for.
    """
    assert aspirations._valid_intended_agents.__name__ == "<lambda>", (
        "aspirations._valid_intended_agents is the LIVE deployment roster "
        f"(__name__={aspirations._valid_intended_agents.__name__!r}), not the "
        "conftest's autouse pin — the goal-selector path is exposed "
        "(guard-1038's cross-deployment form). The pin landed only in the "
        "gates.intended_agent_vocab seam and missed the selector root seam "
        "(omni-388 / g-358-248 (b)).")


def test_pin_carries_full_roster_plus_either() -> None:
    """The pinned set carries every fixture name AND the 'either' sentinel."""
    got = aspirations._valid_intended_agents()
    want = set(FIXTURE_ROSTER) | {"either"}
    assert got == want, (
        f"pinned roster {sorted(got)} != fixture+either {sorted(want)}; "
        "the autouse pin is not carrying the full roster the daemon-POSTing "
        "tests route through (omni-388 / g-358-248 (b)).")
