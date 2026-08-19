""" — hyp_capture WM slot registration (worker->reducer hypothesis-evidence bridge).

SCOPE, and why it is narrower than test_exp_capture_registration.py, which is in
turn narrower than test_spark_capture_bridge.py.

The TRANSPORT (body-merge fork -> capture -> generalize-down, plus its
content-hash dedup) is slot-AGNOSTIC: `_dedup_append` keys off ARRAY_SLOTS
membership, so it treats hyp_capture exactly as it treats the other two, and
test_spark_capture_bridge.py already proves that machinery end to end. The
dedup-distinctness case is likewise already re-derived once, for exp_capture,
against an entry shape hyp_capture shares (a required, varying goal_id). Copying
either here would add a proof that cannot fail independently of the file it was
copied from -- the vacuity this suite's own siblings warn about.

What is genuinely NEW for hyp_capture, and therefore all this file pins, is the
REGISTRATION at each hand-mirrored sync site plus the config cap. Those CAN fail
independently: each is a separate hand-edit in a separate file, and the daemon
copy is the one that runs in production.

NOT COVERED HERE, deliberately, so a green run is not mistaken for more than it
is: the no-double-resolution guard (the goal's second outcome) and the
/review-hypotheses wiring that consumes this slot do not exist yet. Registration
without a consumer is an inert slot -- see g-306-227 / guard-1943 for how long
that shape can hide behind green tests.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent          # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402

SLOT = "hyp_capture"
SIBLINGS = ("spark_capture", "exp_capture")


def _daemon_constant(name: str) -> set:
    """AST-read a constant from the daemon endpoint without importing it (the
    module pulls in server-side deps this test does not need)."""
    src = (PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "wm_write.py").read_text(
        encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in the daemon mirror")


# ---------------------------------------------------------------------------
# 1. MEMBERSHIP at every hand-mirrored sync site
# ---------------------------------------------------------------------------

def test_membership_present_on_both_sides():
    """wm-append.sh / wm-reset.sh are DAEMON-ONLY, so the daemon copy is the LIVE
    path -- a CLI-only edit would leave the bridge inert in production while every
    CLI-level assertion in this file still passed (guard-2323)."""
    for const in ("ARRAY_SLOTS", "RESET_SURVIVING_SLOTS"):
        assert SLOT in getattr(wm, const), f"wm.py {const} lost {SLOT} (g-306-200)"
        assert SLOT in _daemon_constant(const), (
            f"daemon wm_write.py {const} lost {SLOT} -- the LIVE wm-append/"
            f"wm-reset path would diverge from the CLI (g-306-200)")


def test_registered_alongside_both_siblings_not_instead_of_either():
    """Anti-vacuity (guard-1220), and it has more to catch than the exp_capture
    version did: every assertion above passes if someone RENAMES spark_capture OR
    exp_capture to hyp_capture rather than ADDING it. The bridge would read as
    'registered' while one of the other two lanes went silently dead. All three
    must be present in all four constants."""
    for const in ("ARRAY_SLOTS", "RESET_SURVIVING_SLOTS"):
        expected = {SLOT, *SIBLINGS}
        assert expected <= getattr(wm, const), (
            f"wm.py {const} must carry ALL THREE capture slots -- hyp_capture is "
            f"an addition, not a replacement (g-306-200)")
        assert expected <= _daemon_constant(const), (
            f"daemon {const} must carry ALL THREE capture slots (g-306-200)")


# ---------------------------------------------------------------------------
# 2. SURVIVAL property that RESET_SURVIVING_SLOTS membership buys
# ---------------------------------------------------------------------------

def test_reset_preserves_hyp_capture():
    """body-merge delivers a worker's captured evidence into the reducer WM at
    consolidate Step -1; wm-reset runs at Step 5 of the SAME consolidation, and
    /review-hypotheses consumes the evidence later still. Without the exemption the
    payload is wiped ~5 steps after arriving.

    The consequence is worse here than for the sibling slots, which is why this
    property is pinned for hyp_capture specifically rather than inherited: a lost
    narrative is a visibly missing artifact, but lost EVIDENCE keyed to a
    hypothesis_id lets that hypothesis resolve WITHOUT it while every signal still
    reports success -- a silently under-informed resolution."""
    assert SLOT in wm.RESET_SURVIVING_SLOTS
    assert SLOT in _daemon_constant("RESET_SURVIVING_SLOTS")


# ---------------------------------------------------------------------------
# 3. CONFIG cap -- a reset-surviving slot with no cap grows without bound
# ---------------------------------------------------------------------------

def test_array_limit_is_configured_and_tightest_of_the_three():
    """The slot is reset-surviving, so a window in which no reducer runs
    /review-hypotheses would otherwise grow it forever. The cap is deliberately at
    or below BOTH siblings': an entry is written only when execution surfaces
    evidence bearing on a real pipeline hypothesis, which is rarer than
    exp_capture's one-per-executed-goal."""
    import yaml
    cfg = yaml.safe_load(
        (PROJECT_ROOT / "core" / "config" / "memory-pipeline.yaml").read_text(
            encoding="utf-8"))
    limits = (cfg.get("working_memory_pruning") or {}).get("array_limits") or {}
    assert isinstance(limits.get(SLOT), int) and limits[SLOT] > 0, (
        "hyp_capture has no array_limits entry -- a reset-surviving slot with no "
        "cap grows the WM without bound (g-306-200)")
    for sib in SIBLINGS:
        if sib in limits:
            assert limits[SLOT] <= limits[sib], (
                f"hyp_capture's cap must not exceed {sib}'s -- it is the rarest "
                f"and most targeted of the three capture lanes (g-306-200)")


def test_cap_is_pinned_by_value_not_derived_from_itself():
    """Guards the vacuity that bit this session's earlier unit: a bound test whose
    expected value is read from the same constant it is checking can never fail.
    The goal specifies 10, so assert 10 literally and accept the maintenance cost
    -- that cost IS the detector."""
    import yaml
    cfg = yaml.safe_load(
        (PROJECT_ROOT / "core" / "config" / "memory-pipeline.yaml").read_text(
            encoding="utf-8"))
    limits = (cfg.get("working_memory_pruning") or {}).get("array_limits") or {}
    assert limits.get(SLOT) == 10, (
        "g-306-200 specifies a hyp_capture cap of 10; changing it is a deliberate "
        "act that should update this pin too")
