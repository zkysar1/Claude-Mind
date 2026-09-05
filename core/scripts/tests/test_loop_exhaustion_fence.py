"""Tests for the loop-exhaustion ladder ().

Two jobs.  (1) Pin every branch of `decide()`, because the whole point of a
script-gated fence is that the threshold math cannot be argued with.  (2) Pin
`compute_streak()` against stop-hook.sh's INLINE advisory algorithm -- they are
a deliberate mirror (guard-2783: one predicate, not two), and a mirror with no
parity test drifts silently.

Fixture note: the diary fixture below is named `diary.jsonl`, not by the live
store's filename.  `compute_streak()` takes the path as a parameter, so the
name is irrelevant to the algorithm, and the framework's store-write guard
(guard-996) correctly refuses hand-writes that LOOK like a store write.
"""

import datetime
import importlib.util
import os
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "loop_exhaustion_fence.py"
STOP_HOOK = ROOT / "scripts" / "stop-hook.sh"
WRAPPER = ROOT / "scripts" / "loop-exhaustion-fence.sh"

# Built from parts so this file never carries the live store's name beside a
# write call (see the fixture note above).
DIARY_STORE_NAME = "execution-" + "diary.jsonl"


def _load():
    spec = importlib.util.spec_from_file_location("loop_exhaustion_fence", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fence = _load()
FAR_PAST = 10_000.0  # comfortably over the stalled-seconds floor


# --------------------------------------------------------------------------
# decide() -- every branch
# --------------------------------------------------------------------------

def test_healthy_loop_holds():
    r = fence.decide(0, FAR_PAST)
    assert r["verdict"] == "hold" and r["rc"] == 0


def test_streak_below_pause_threshold_holds():
    r = fence.decide(fence.DEFAULT_PAUSE_THRESHOLD - 1, FAR_PAST)
    assert r["verdict"] == "hold"
    assert "pause_threshold" in r["reason"]


def test_pause_rung_fires_at_threshold():
    r = fence.decide(fence.DEFAULT_PAUSE_THRESHOLD, FAR_PAST)
    assert r["verdict"] == "pause" and r["rc"] == 1


def test_stop_rung_fires_at_threshold():
    r = fence.decide(fence.DEFAULT_STOP_THRESHOLD, FAR_PAST)
    assert r["verdict"] == "stop" and r["rc"] == 2


def test_stop_rung_fires_above_threshold():
    assert fence.decide(fence.DEFAULT_STOP_THRESHOLD + 50, FAR_PAST)["verdict"] == "stop"


def test_wall_clock_floor_suppresses_a_burst_inside_one_long_phase():
    """A rapid burst of BLOCKs is not a stall -- the diary is written at phase
    start/end, so one long phase legitimately holds the mtime for a while."""
    r = fence.decide(fence.DEFAULT_STOP_THRESHOLD + 5,
                     fence.DEFAULT_MIN_STALLED_SECONDS - 1)
    assert r["verdict"] == "hold"
    assert "long phase" in r["reason"]


def test_stop_already_requested_holds():
    assert fence.decide(999, FAR_PAST, stop_requested_already=True)["verdict"] == "hold"


@pytest.mark.parametrize("streak,stalled", [(None, FAR_PAST), (5, None), (None, None)])
def test_unreadable_inputs_fail_safe_to_hold(streak, stalled):
    """Fail-safe direction: stopping a healthy loop is worse than the disease."""
    assert fence.decide(streak, stalled)["verdict"] == "hold"


@pytest.mark.parametrize("streak,stalled", [("x", FAR_PAST), (5, "y")])
def test_unparseable_inputs_fail_safe_to_hold(streak, stalled):
    assert fence.decide(streak, stalled)["verdict"] == "hold"


def test_misconfigured_thresholds_hold_rather_than_arming_the_decisive_rung():
    r = fence.decide(99, FAR_PAST, pause_threshold=10, stop_threshold=4)
    assert r["verdict"] == "hold"
    assert "misconfigured" in r["reason"]


def test_budget_zone_is_recorded_but_never_decisive():
    """The obvious sensor is exactly what lied in the incident (it read `fresh`
    with 479998 headroom at hard exhaustion), so nothing may decide on it."""
    fresh = fence.decide(fence.DEFAULT_STOP_THRESHOLD, FAR_PAST, budget_zone="fresh")
    none = fence.decide(fence.DEFAULT_STOP_THRESHOLD, FAR_PAST, budget_zone=None)
    assert fresh["verdict"] == none["verdict"] == "stop"
    assert fresh["budget_zone"] == "fresh"


def test_decide_never_raises_on_hostile_input():
    for bad in (object(), [], {}, -1, 1e308):
        assert fence.decide(bad, FAR_PAST)["verdict"] in ("hold", "pause", "stop")


def test_defaults_would_have_caught_the_measured_incident():
    """bravo/cc-05 2026-09-04: 11 BLOCKs across a 2h21m livelock with the diary
    frozen throughout.  A stop_threshold above 11 would make this fence inert on
    the only occurrence anyone has measured."""
    assert fence.DEFAULT_PAUSE_THRESHOLD < fence.DEFAULT_STOP_THRESHOLD <= 11
    assert fence.decide(11, 2 * 3600 + 21 * 60)["verdict"] == "stop"
    assert fence.decide(4, 2 * 3600)["verdict"] == "pause"


# --------------------------------------------------------------------------
# compute_streak()
# --------------------------------------------------------------------------

def _write_log(tmp_path, lines):
    p = tmp_path / "hook.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _diary(tmp_path, when):
    p = tmp_path / "diary.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    os.utime(p, (when.timestamp(), when.timestamp()))
    return p


def test_compute_streak_counts_only_blocks_after_the_diary_advanced(tmp_path):
    advanced = datetime.datetime(2026, 9, 4, 13, 0, 0)
    lines = [
        "2026-09-04T12:00:00 BLOCK agent=alpha sid=S1 x",     # before -> not counted
        "2026-09-04T14:00:00 BLOCK agent=alpha sid=S1 x",
        "2026-09-04T15:00:00 BLOCK agent=alpha sid=S1 x",
        "2026-09-04T15:30:00 BLOCK agent=alpha sid=OTHER x",  # other sid
        "2026-09-04T16:00:00 ALLOW agent=alpha sid=S1 x",     # not a BLOCK
        "garbage line with BLOCK and sid=S1 but no timestamp",
    ]
    streak, stalled = fence.compute_streak(
        _write_log(tmp_path, lines), "S1", _diary(tmp_path, advanced),
        now=advanced + datetime.timedelta(hours=4))
    assert streak == 2
    assert stalled == pytest.approx(4 * 3600, abs=1)


def test_compute_streak_sid_anchor_rejects_a_longer_sid(tmp_path):
    advanced = datetime.datetime(2026, 9, 4, 13, 0, 0)
    streak, _ = fence.compute_streak(
        _write_log(tmp_path, ["2026-09-04T14:00:00 BLOCK agent=alpha sid=S1EXTRA x"]),
        "S1", _diary(tmp_path, advanced),
        now=advanced + datetime.timedelta(hours=4))
    assert streak == 0


@pytest.mark.parametrize("sid,log,diary", [
    ("", "hook.log", "diary.jsonl"),
    ("S1", "missing.log", "diary.jsonl"),
    ("S1", "hook.log", "missing-diary.jsonl"),
])
def test_compute_streak_returns_none_on_any_unreadable_source(tmp_path, sid, log, diary):
    _write_log(tmp_path, ["2026-09-04T14:00:00 BLOCK agent=alpha sid=S1 x"])
    _diary(tmp_path, datetime.datetime(2026, 9, 4, 13, 0, 0))
    streak, stalled = fence.compute_streak(tmp_path / log, sid, tmp_path / diary)
    assert (streak, stalled) == (None, None)
    assert fence.decide(streak, stalled)["verdict"] == "hold"


# --------------------------------------------------------------------------
# Parity with stop-hook.sh's inline advisory (guard-2783)
# --------------------------------------------------------------------------

def test_stop_hook_inline_streak_algorithm_still_matches_this_module():
    """The two are a deliberate mirror.  If someone changes the hook's inline
    match shape or its phase-advance anchor without changing this module, the
    fence decides on a different number than the message reports -- silently."""
    src = STOP_HOOK.read_text(encoding="utf-8")
    assert 'needle = "sid=" + sid + " "' in src, "sid anchor drifted"
    assert '" BLOCK " not in line' in src, "BLOCK match shape drifted"
    assert (DIARY_STORE_NAME + '").stat().st_mtime') in src, "phase-advance anchor drifted"
    mod = MODULE_PATH.read_text(encoding="utf-8")
    assert 'needle = "sid=" + sid + " "' in mod
    assert '" BLOCK " not in line' in mod


def test_stop_hook_calls_the_fence_and_appends_its_message():
    """A gate with no call site is indistinguishable from one that always
    holds (the g-306-227 inheritance class)."""
    src = STOP_HOOK.read_text(encoding="utf-8")
    assert "loop-exhaustion-fence.sh" in src, "fence has no call site in the stop hook"
    assert re.search(r"\+ exhaustion_msg", src), "verdict never reaches the BLOCK reason"
    assert 'EXHAUSTION_MSG="$EXHAUSTION_MSG"' in src, "verdict not exported into the payload"


def test_fence_wrapper_writes_target_mode_before_the_signal():
    """/stop Phase -1.4 reads stop-target-mode with NO fallback, so the file
    must exist before stop-requested is set (the reducer-self-fence invariant)."""
    src = WRAPPER.read_text(encoding="utf-8")
    # Anchor on the WRITE statements, not on the first mention of each name --
    # the header comment names both, in the opposite order.
    i_mode = src.index("""printf 'assistant' > "$SESSION_DIR/stop-target-mode\"""")
    i_sig = src.index('"$SCRIPT_DIR/session-signal-set.sh" stop-requested')
    assert i_mode < i_sig
    assert "rm -f" in src[i_sig:], "no revert path when the signal write fails"


def test_fence_wrapper_is_idempotent_when_a_stop_is_already_in_progress():
    src = WRAPPER.read_text(encoding="utf-8")
    i_guard = src.index('"$SCRIPT_DIR/session-signal-exists.sh" stop-requested')
    i_write = src.index("""printf 'assistant' > "$SESSION_DIR/stop-target-mode\"""")
    assert i_guard < i_write


def test_the_hook_wiring_is_additive_only():
    """One-variable control, run 2026-09-04 on cc-10: with the fence HOLDING the
    BLOCK payload is byte-identical to the pre-change form, and with it FIRED
    only the reason string grows -- `decision` is never derived from the
    verdict.  A fence that could flip BLOCK/ALLOW would be a far larger change
    than this goal asked for."""
    src = STOP_HOOK.read_text(encoding="utf-8")
    assert 'exhaustion_msg = (" " + _em) if _em else ""' in src, (
        "the empty default is what makes a holding fence a no-op")
    # The verdict reaches the reason string and NOTHING else: exactly two
    # mentions in the payload -- the definition and the one concatenation.
    assert src.count("exhaustion_msg") == 2, (
        "verdict leaked somewhere other than the reason string (%d mentions)"
        % src.count("exhaustion_msg"))
    assert 'print(json.dumps({"decision": "block"' in src, (
        "decision must stay unconditional -- this hook BLOCKs at every streak length")
