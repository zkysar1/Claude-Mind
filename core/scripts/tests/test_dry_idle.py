"""Unit tests for core/scripts/_dry_idle.py (4-b, dry-idle Layer 2).

Pure-logic tests -- no daemon, no WM, no I/O beyond the fail-open config read
(so these run identically with or without a live daemon; STORAGE_BACKEND is
irrelevant here, but the suite still pins it via conftest per guard-955).

The two goal-mandated criteria:
  - criterion 2: curve table EXACT [120,240,480,960,1920,3840,7200(capped)]
  - criterion 5: dry-idle and quiescence are MUTUALLY EXCLUSIVE
"""
import sys
from pathlib import Path

# _dry_idle lives one level up in core/scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _dry_idle  # noqa: E402

# The exact expected curve for streaks 1..8 under the Layer-1 config
# (base_seconds=120, multiplier=2.0, max_seconds=7200). streak 7 is the first
# capped value (120*2**6 = 7680 -> 7200); 8 stays capped.
EXPECTED_CURVE = {
    1: 120, 2: 240, 3: 480, 4: 960,
    5: 1920, 6: 3840, 7: 7200, 8: 7200,
}


# ---------------------------------------------------------------------------
# criterion 2 -- curve table EXACT
# ---------------------------------------------------------------------------

def test_curve_table_exact_defaults():
    """dry_sleep_seconds must match the spec curve exactly under DEFAULTS."""
    for streak, expected in EXPECTED_CURVE.items():
        got = _dry_idle.dry_sleep_seconds(streak, _dry_idle.DEFAULTS)
        assert got == expected, f"streak {streak}: got {got}, want {expected}"


def test_curve_table_exact_from_live_config():
    """The COMMITTED aspirations.yaml dry_idle_backoff block must itself
    produce the spec curve -- catches a future config retune that silently
    breaks the contract (ties the test to the real config, not just DEFAULTS)."""
    cfg = _dry_idle.load_config()
    for streak, expected in EXPECTED_CURVE.items():
        got = _dry_idle.dry_sleep_seconds(streak, cfg)
        assert got == expected, (
            f"live-config streak {streak}: got {got}, want {expected} "
            f"(base={cfg['base_seconds']}, mult={cfg['multiplier']}, max={cfg['max_seconds']})"
        )


def test_curve_caps_beyond_streak_7():
    """Everything past the cap point stays pinned at max_seconds."""
    for streak in (8, 9, 12, 50, 100):
        assert _dry_idle.dry_sleep_seconds(streak, _dry_idle.DEFAULTS) == 7200


def test_curve_clamps_streak_below_one():
    """streak < 1 clamps to 1 -> base_seconds (never a zero/negative sleep)."""
    assert _dry_idle.dry_sleep_seconds(0, _dry_idle.DEFAULTS) == 120
    assert _dry_idle.dry_sleep_seconds(-5, _dry_idle.DEFAULTS) == 120


def test_at_cap_boundary():
    """at_cap flips exactly at streak 7 (the first flattened value)."""
    assert _dry_idle.at_cap(6, _dry_idle.DEFAULTS) is False
    assert _dry_idle.at_cap(7, _dry_idle.DEFAULTS) is True
    assert _dry_idle.at_cap(20, _dry_idle.DEFAULTS) is True


# ---------------------------------------------------------------------------
# criterion 5 -- mutual exclusion with quiescence
# ---------------------------------------------------------------------------

def test_quiescence_approved_is_never_dry():
    """The mutual-exclusion guarantee: an APPROVED quiescence cycle is NOT
    dry, even at zero executable goals -- so a caller cannot fire both the
    quiescence sleep AND the dry sleep on one cycle."""
    assert _dry_idle.is_dry_state(0, "approved") is False
    assert _dry_idle.is_dry_state(5, "approved") is False


def test_dry_requires_zero_executable_and_denied_or_na():
    """Dry fires only when there is nothing to run AND quiescence could not
    approve (denied, or not-applicable)."""
    assert _dry_idle.is_dry_state(0, "denied") is True
    assert _dry_idle.is_dry_state(0, "na") is True


def test_executable_work_is_never_dry():
    """Any executable goal means the loop should execute, not sleep."""
    assert _dry_idle.is_dry_state(1, "denied") is False
    assert _dry_idle.is_dry_state(5, "na") is False
    assert _dry_idle.is_dry_state(1, "approved") is False


def test_unparseable_count_is_not_dry():
    """A non-integer executable_count fails conservatively to NOT dry."""
    assert _dry_idle.is_dry_state(None, "denied") is False
    assert _dry_idle.is_dry_state("x", "na") is False


# ---------------------------------------------------------------------------
# streak increment / reset
# ---------------------------------------------------------------------------

def test_streak_increments_on_dry():
    assert _dry_idle.advance_streak(0, True, _dry_idle.DEFAULTS) == 1
    assert _dry_idle.advance_streak(3, True, _dry_idle.DEFAULTS) == 4


def test_streak_resets_on_executable_by_default():
    assert _dry_idle.advance_streak(6, False, _dry_idle.DEFAULTS) == 0


def test_streak_holds_when_reset_disabled():
    cfg = dict(_dry_idle.DEFAULTS, reset_on_executable=False)
    assert _dry_idle.advance_streak(6, False, cfg) == 6


# ---------------------------------------------------------------------------
# next_dry_signals -- pinned 5-key shape + accumulation
# ---------------------------------------------------------------------------

REQUIRED_KEYS = {"streak", "last_dry_at", "sleep_total_s",
                 "session_start_at", "cap_cycles"}


def test_next_dry_signals_shape_from_none():
    out = _dry_idle.next_dry_signals(None, True, "2026-07-13T16:00:00")
    assert set(out) == REQUIRED_KEYS
    assert out["streak"] == 1
    assert out["last_dry_at"] == "2026-07-13T16:00:00"
    assert out["session_start_at"] == "2026-07-13T16:00:00"
    assert out["sleep_total_s"] == 120  # first dry cycle sleeps base_seconds


def test_next_dry_signals_accumulates_sleep():
    prev = {"streak": 2, "last_dry_at": "2026-07-13T15:00:00",
            "sleep_total_s": 360, "session_start_at": "2026-07-13T13:00:00",
            "cap_cycles": 0}
    out = _dry_idle.next_dry_signals(prev, True, "2026-07-13T16:00:00")
    assert out["streak"] == 3
    assert out["sleep_total_s"] == 360 + 480  # + dry_sleep_seconds(3)
    assert out["session_start_at"] == "2026-07-13T13:00:00"  # preserved
    assert out["cap_cycles"] == 0  # streak 3 is below cap


def test_next_dry_signals_cap_cycles_increment():
    prev = {"streak": 7, "last_dry_at": "2026-07-13T15:00:00",
            "sleep_total_s": 7200, "session_start_at": "2026-07-13T13:00:00",
            "cap_cycles": 1}
    out = _dry_idle.next_dry_signals(prev, True, "2026-07-13T16:00:00")
    assert out["streak"] == 8
    assert out["cap_cycles"] == 2  # another at-cap cycle
    assert out["sleep_total_s"] == 7200 + 7200


def test_next_dry_signals_resets_streak_on_executable():
    # prev carries cap_cycles=3 (was at cap) to prove BOTH streak AND cap_cycles
    # reset when an executable cycle breaks the dry run.
    prev = {"streak": 5, "last_dry_at": "2026-07-13T15:00:00",
            "sleep_total_s": 3720, "session_start_at": "2026-07-13T13:00:00",
            "cap_cycles": 3}
    out = _dry_idle.next_dry_signals(prev, False, "2026-07-13T16:00:00")
    assert out["streak"] == 0
    assert out["cap_cycles"] == 0  # dry streak broke -> consecutive-at-cap counter resets
    assert out["sleep_total_s"] == 3720  # no new sleep added on a non-dry cycle
    assert out["last_dry_at"] == "2026-07-13T15:00:00"  # preserved, not bumped


# ---------------------------------------------------------------------------
# config load fail-open
# ---------------------------------------------------------------------------

def test_load_config_failopen_to_defaults():
    cfg = _dry_idle.load_config(config_path="/nonexistent/aspirations.yaml")
    assert cfg == _dry_idle.DEFAULTS


def test_load_config_matches_layer1_values():
    """The live config must carry the Layer-1 spec values (ties module to config)."""
    cfg = _dry_idle.load_config()
    assert cfg["base_seconds"] == 120
    assert cfg["multiplier"] == 2.0
    assert cfg["max_seconds"] == 7200
    assert cfg["reset_on_executable"] is True
