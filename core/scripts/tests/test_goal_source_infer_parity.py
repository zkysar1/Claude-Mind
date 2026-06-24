"""test_goal_source_infer_parity.py -- regression for 9.

The origin-signal gate (gates/origin_signal.py: is_valid / ALLOWED_PREFIXES)
MUST accept every signal that _goal_source.infer() recognizes as a legitimate
goal source. When the two drift, a goal carrying an infer-blessed signal passes
source-inference but is BLOCKED by the gate, forcing the caller to
--override-signal. That is exactly the override-rate inflation
gate-retirement-eval flagged TIGHTEN (origin-signal-gate, 51.6% over its
window): seven infer-recognized prefixes (user-directed:, user_directed:,
recurring:, monitor:, investigation:, apply:, brief:) had drifted out of
ALLOWED_PREFIXES.

The invariant pinned here is the SUPERSET of the cycle-detector subset already
pinned by test_goal_source_cycle_detector_prefixes.py:

    infer(sig) is not None  ==>  is_valid(sig) is True

(The reverse is intentionally NOT required: a gate-accepted signal may have no
infer() mapping -- e.g. program-change-proposal: -- which leaves
goal_source null but does NOT inflate the override rate. That is a separate,
lesser attribution gap, out of scope for this lock.)

Fixture maintenance: INFER_RECOGNIZED_SIGNALS lists one representative concrete
signal per branch of _goal_source.infer(). When infer() gains a new recognized
prefix, add a sample here AND to ALLOWED_PREFIXES -- this test fails loudly if
the gate side is forgotten.
"""
from __future__ import annotations

from _goal_source import infer
from gates.origin_signal import ALLOWED_PREFIXES, is_valid

# One representative concrete signal per infer()-recognized prefix / bare tag.
# Mirror of _goal_source.infer() branches (core/scripts/_goal_source.py).
INFER_RECOGNIZED_SIGNALS = [
    # -> user
    "user_directive",
    "pending_question:pq-001",
    "user-directed:rb-314-followup",
    "user_directed:some-slug",
    # -> recurring-cycle
    "recurring_cadence:daily",
    "recurring:run-game-session",
    # -> cycle-detector
    "failing_test:test_foo",
    "resolved_hypothesis:hyp-1",
    "low_confidence_node:node-x",
    "drift_detected:tree",
    "monitor:proc-1778000000",
    "alert-email:s3-key-abc",
    "routing-mismatch:g-115-1358",
    "routing-either-resolve:g-001-17",
    "insight_trigger:msg-20260611-120000-zeta-1900",
    # -> agent-self
    "idle_fallback",
    "decomposition:parent-1",
    "parent_aspiration:asp-115",
    "unblock:g-115-1357",
    "investigate:g-002-17",
    "investigation:fresh-eyes-code-finding",
    "idea:tighten-gate",
    "maintain:inline-fix",
    "apply:per-source-normalization",
    "brief:cross-cutting-audit",
    "board_post:msg-1",
]

# The seven prefixes this goal added to ALLOWED_PREFIXES.
G1439_ADDED_PREFIXES = (
    "user-directed:",
    "user_directed:",
    "recurring:",
    "monitor:",
    "investigation:",
    "apply:",
    "brief:",
)


def test_fixture_is_current():
    """Guard against a stale fixture: every sample must still be infer-blessed.
    If this fails, infer() changed and the list above needs updating."""
    for sig in INFER_RECOGNIZED_SIGNALS:
        assert infer(sig) is not None, (
            f"stale fixture: infer({sig!r}) is None -- update "
            f"INFER_RECOGNIZED_SIGNALS to match _goal_source.infer()")


def test_infer_recognized_implies_gate_valid():
    """The core invariant: infer() -> non-None source ==> is_valid() True.

    A failure here means the gate blocks a signal that the source-of-truth
    inference treats as a real goal source -- the drift class that produced the
    g-115-1439 override-rate inflation."""
    for sig in INFER_RECOGNIZED_SIGNALS:
        src = infer(sig)
        assert is_valid(sig), (
            f"DRIFT: infer({sig!r})={src!r} but the gate rejects it -- "
            f"ALLOWED_PREFIXES is missing the prefix (g-115-1439 regression)")


def test_g1439_additions_present_in_allowed_prefixes():
    """The seven prefixes added by 9 are present in the gate whitelist
    (pins the specific fix, not just the general invariant)."""
    for pfx in G1439_ADDED_PREFIXES:
        assert pfx in ALLOWED_PREFIXES, (
            f"{pfx} missing from ALLOWED_PREFIXES (g-115-1439 regressed)")


def test_g1439_additions_are_infer_blessed():
    """Each added prefix maps to a real source via infer() -- proving the
    additions restore a parity that already held on the infer side (we widened
    the gate to match infer, not invented new signal kinds)."""
    for pfx in G1439_ADDED_PREFIXES:
        sample = pfx + "concrete-suffix"
        assert infer(sample) is not None, (
            f"{pfx} added to the gate but infer() does not recognize it -- "
            f"would create REVERSE drift (gate-accepts / source-null)")
