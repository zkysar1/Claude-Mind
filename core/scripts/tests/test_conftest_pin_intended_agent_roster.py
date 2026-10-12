"""test_conftest_pin_intended_agent_roster.py —  (b) regression (omni-388).

The goal-selector (and the daemon-POSTing goal writes across this tree) resolve
the intended-agent roster through ``aspirations._valid_intended_agents()`` ->
``_agents.get_active_agents()``. The gates gate-pin in this conftest
(``_pin_intended_agent_roster``) covers only ``gates.intended_agent_vocab.
_resolve_roster`` — a DIFFERENT seam — so the selector path stayed exposed to
the LIVE deployment roster: green on a dev-fleet roster that carries the
fixture names, 400s / routes-wrong on a deployment whose roster does not
(guard-1038's cross-deployment form; omni-388 measured 14 idle-reallocation
reds it flips). omni-388's fix adds an in-process pin of
``aspirations._valid_intended_agents`` beside the gate pin in BOTH conftests.

THIS IS THE REGRESSION THAT FAILS WITHOUT THE PIN (g-358-248 (b): 'with a test
that fails without the pin'). It asserts the autouse pin is in effect and that
it carries the FULL fixture roster plus the 'either' sentinel — an
identity-AND-value check, not just a value check. The identity check is what
makes the test pin-dependent and non-vacuous: on a deployment whose live roster
happens to equal the fixture set, a value-only assertion would still pass with
the pin removed, and the exposure would be invisible again.

Twin: mind_api/tests/test_conftest_pin_intended_agent_roster.py. The two test
packages load independently and neither imports the other's conftest, so a pin
(parity) guarded by a test in only one tree leaves the other exposed — the
exact shape g-115-5651 warns about. Keep the copies in lockstep.
"""
from __future__ import annotations

import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "core" / "scripts"
if str(CORE_SCRIPTS) not in sys.path:
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
