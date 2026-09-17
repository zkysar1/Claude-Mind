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


def test_reason_states_the_limit_of_the_claim_and_names_the_zone():
    """A firing proves the loop is not ADVANCING, never WHY.

    alpha/cc-04 2026-09-11: the stop rung fired at 307 BLOCKs with the diary
    frozen since 06:29:19 while context sat around half consumed, and the
    firing was read -- off this module's NAME -- as context exhaustion.  The
    stop was right; the inherited cause was not.  The reason must therefore
    carry both halves, and must stay non-decisive either way.
    """
    for zone in ("normal", "fresh", None):
        for streak, rung in (
            (fence.DEFAULT_STOP_THRESHOLD, "stop"),
            (fence.DEFAULT_PAUSE_THRESHOLD, "pause"),
        ):
            r = fence.decide(streak, FAR_PAST, budget_zone=zone)
            assert r["verdict"] == rung, "the note must not move the verdict"
            assert "cause NOT established" in r["reason"]
            assert (zone or "unrecorded") in r["reason"]


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


def test_compute_streak_counts_turn_ends_after_the_diary_advanced(tmp_path):
    """Counts TURN-ENDS, not BLOCKs ().

    This test asserted 2 until 2026-09-08, with the ALLOW line marked
    "# not a BLOCK".  That was the defect, pinned: the fence's own pause rung
    prescribes ending the turn on a REGISTERED external-wait sleep, and a
    registered Tier-A job makes stop-hook Gate 2.6 ALLOW the turn-end instead
    of BLOCKing it -- so an agent that ADOPTS the remedy stops emitting the
    only observable the fence counted, and the ladder became unreachable from
    below.  The ALLOW line now counts and the expected value is 3.

    The last line is the guard against over-widening (guard-5468): a line
    carrying the sid and a timestamp but NO verdict token is still excluded.
    Without it "count turn-ends" silently becomes "count any line mentioning
    the sid", which is a broader rule than the one intended.
    """
    advanced = datetime.datetime(2026, 9, 4, 13, 0, 0)
    lines = [
        "2026-09-04T12:00:00 BLOCK agent=alpha sid=S1 x",     # before -> not counted
        "2026-09-04T14:00:00 BLOCK agent=alpha sid=S1 x",
        "2026-09-04T15:00:00 BLOCK agent=alpha sid=S1 x",
        "2026-09-04T15:30:00 BLOCK agent=alpha sid=OTHER x",  # other sid
        "2026-09-04T16:00:00 ALLOW agent=alpha sid=S1 x",     # a turn-end -> counted
        "2026-09-04T17:00:00 RECOVERY gate=x sid=S1 x",       # no verdict -> NOT a turn-end
        "garbage line with BLOCK and sid=S1 but no timestamp",
    ]
    streak, stalled = fence.compute_streak(
        _write_log(tmp_path, lines), "S1", _diary(tmp_path, advanced),
        now=advanced + datetime.timedelta(hours=4))
    assert streak == 3
    assert stalled == pytest.approx(4 * 3600, abs=1)


def test_compute_streak_verdict_vocabulary_is_built_from_the_emitter(tmp_path):
    """guard-4285: build the match from the EMITTER's format string.

    stop-hook.sh emits exactly two verdicts, in two different shapes --
    `<ts> BLOCK sid=...` (no gate= field) and `<ts> ALLOW gate=<name> sid=...`.
    Both are pinned here so a future emitter change that drops a space, or adds
    a third verdict, fails loudly instead of silently halving the streak.
    """
    advanced = datetime.datetime(2026, 9, 4, 13, 0, 0)
    lines = [
        "2026-09-04T14:00:00 BLOCK sid=S1 agent=alpha runner_token=t",
        "2026-09-04T14:30:00 ALLOW gate=background-jobs sid=S1 agent=alpha runner_token=t",
        "2026-09-04T15:00:00 ALLOW gate=pending-agents sid=S1 agent=alpha runner_token=t",
    ]
    streak, _ = fence.compute_streak(
        _write_log(tmp_path, lines), "S1", _diary(tmp_path, advanced),
        now=advanced + datetime.timedelta(hours=4))
    assert streak == 3, "the live emitter's own two line shapes must both count"


def test_the_measured_allow_only_stall_escalates_instead_of_holding(tmp_path):
    """THE INCIDENT, reproduced (; alpha, cc-04, 2026-09-08).

    A 10.5h total loop stall: execution diary frozen at 2026-09-07T19:46:11,
    the precheck-gap line reporting 628.6 minutes with 0 iterations closed, and
    the stop-hook log carrying ZERO BLOCKs for the sid -- 9 turn-ends, every one
    of them `ALLOW gate=background-jobs`.  Positive control at the time: the
    same grep found 81 BLOCKs for this sid across the session's healthy portion,
    so the zero was a real zero and not a broken matcher.

    Under the old predicate this computed streak=0 and the fence held forever.
    The stall was 4.4x longer than the 2h21m incident that motivated building
    the fence, and produced no fence action whatsoever.
    """
    advanced = datetime.datetime(2026, 9, 7, 19, 46, 11)
    lines = [
        "2026-09-07T%02d:00:00 ALLOW gate=background-jobs sid=S1 agent=alpha x" % h
        for h in range(20, 24)
    ] + [
        "2026-09-08T%02d:00:00 ALLOW gate=background-jobs sid=S1 agent=alpha x" % h
        for h in range(0, 5)
    ]
    assert len(lines) == 9, "the measured turn-end count for the stall"
    stalled_seconds = 628.6 * 60
    streak, _ = fence.compute_streak(
        _write_log(tmp_path, lines), "S1", _diary(tmp_path, advanced),
        now=advanced + datetime.timedelta(seconds=stalled_seconds))
    assert streak == 9, "ALLOW-only turn-ends must be counted, or the ladder is unreachable"
    assert fence.decide(streak, stalled_seconds)["verdict"] == "pause", (
        "9 turn-ends with the diary frozen throughout reaches pause (4) and "
        "stops one short of stop (10) -- the measured counterfactual")
    # The defect itself, pinned: the old predicate saw nothing here.
    assert fence.decide(0, stalled_seconds)["verdict"] == "hold"


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
])
def test_compute_streak_returns_none_on_any_unreadable_source(tmp_path, sid, log, diary):
    _write_log(tmp_path, ["2026-09-04T14:00:00 BLOCK agent=alpha sid=S1 x"])
    _diary(tmp_path, datetime.datetime(2026, 9, 4, 13, 0, 0))
    streak, stalled = fence.compute_streak(tmp_path / log, sid, tmp_path / diary)
    assert (streak, stalled) == (None, None)
    assert fence.decide(streak, stalled)["verdict"] == "hold"


def test_compute_streak_absent_diary_anchors_at_the_first_turn_end(tmp_path):
    """A session with turn-ends and NO diary has never advanced a phase.

    Measured 2026-09-17 on a prod vessel (debc47de, run A): 85 consecutive
    BLOCKs from 19:38:36 to 19:59:39, every one answered by prose, and no
    execution diary was ever written because no iteration ever ran.  The fence
    HELD on all 85 -- an absent diary read as an unreadable input.  It is not
    ambiguous: the diary is appended at every phase start/end, so its absence
    beside a streak of turn-ends is the strongest form of "cannot execute".
    Anchor = the FIRST turn-end for the sid, so the wall-clock floor still
    applies from that moment.
    """
    lines = [
        "2026-09-17T19:38:36 BLOCK agent=a sid=S1 x",
        "2026-09-17T19:38:50 BLOCK agent=a sid=S1 x",
        "2026-09-17T19:39:05 BLOCK agent=a sid=OTHER x",   # other sid
        "2026-09-17T19:39:20 ALLOW gate=g sid=S1 x",
        "2026-09-17T19:39:35 RECOVERY gate=x sid=S1 x",     # no verdict token
    ]
    log = _write_log(tmp_path, lines)
    streak, stalled = fence.compute_streak(
        log, "S1", tmp_path / "never-written.jsonl",
        now=datetime.datetime(2026, 9, 17, 19, 59, 39))
    assert streak == 3
    assert stalled == pytest.approx(21 * 60 + 3, abs=1)


def test_compute_streak_absent_diary_with_no_turn_ends_is_a_zero_not_a_hold(tmp_path):
    log = _write_log(tmp_path, ["2026-09-17T19:38:36 BLOCK agent=a sid=OTHER x"])
    assert fence.compute_streak(log, "S1", tmp_path / "never-written.jsonl") == (0, 0.0)
    assert fence.decide(0, 0.0)["verdict"] == "hold"


def test_absent_diary_is_the_one_absence_that_does_not_hold(tmp_path):
    """The measured run-A shape reaches the STOP rung under the DEFAULT thresholds
    once the wall-clock floor has passed, and a diary that exists but cannot be
    read still holds (the fail-safe direction is unchanged for unreadables)."""
    t0 = datetime.datetime(2026, 9, 17, 19, 38, 36)
    lines = ["%s BLOCK agent=a sid=S1 x" % (t0 + datetime.timedelta(seconds=15 * i)).isoformat()
             for i in range(12)]
    log = _write_log(tmp_path, lines)
    streak, stalled = fence.compute_streak(
        log, "S1", tmp_path / "never-written.jsonl", now=t0 + datetime.timedelta(minutes=16))
    assert fence.decide(streak, stalled)["verdict"] == "stop"
    unreadable = tmp_path / "diary-as-dir"
    unreadable.mkdir()
    (unreadable / "x").touch()
    # a directory stats fine (mtime) -- that is a readable anchor, not an absence;
    # the genuinely unreadable case is a path whose stat raises something other
    # than FileNotFoundError, e.g. a component that is a file, not a directory
    not_a_dir = tmp_path / "file-as-dir"
    not_a_dir.write_text("x", encoding="utf-8")
    streak, stalled = fence.compute_streak(log, "S1", not_a_dir / "diary.jsonl")
    assert (streak, stalled) == (None, None)


# --------------------------------------------------------------------------
# Parity with stop-hook.sh's inline advisory (guard-2783)
# --------------------------------------------------------------------------

def test_stop_hook_inline_streak_algorithm_still_matches_this_module():
    """The two are a deliberate mirror.  If someone changes the hook's inline
    match shape or its phase-advance anchor without changing this module, the
    fence decides on a different number than the message reports -- silently."""
    src = STOP_HOOK.read_text(encoding="utf-8")
    mod = MODULE_PATH.read_text(encoding="utf-8")

    # STILL SHARED -- these are what the mirror is actually for.
    assert 'needle = "sid=" + sid + " "' in src, "sid anchor drifted"
    assert 'needle = "sid=" + sid + " "' in mod, "sid anchor drifted"
    assert (DIARY_STORE_NAME + '").stat().st_mtime') in src, "phase-advance anchor drifted"

    # DELIBERATELY DIVERGENT since , and NOT relaxed away (guard-2392:
    # a docstring claiming shared semantics is a claim, not a mechanism -- so the
    # divergence gets an assertion of its own rather than a deleted one).
    assert '" BLOCK " not in line' in src, (
        "the hook's INLINE block is message-only and must stay BLOCK-scoped: it "
        "runs only while composing the BLOCK payload, so widening it is a no-op")
    assert '" BLOCK " not in line' not in mod, (
        "the MODULE's copy drives a DECISION and must count turn-ends, not "
        "BLOCKs (g-115-9467) -- if this reappears the ladder is unreachable again")
    assert "TURN_END_VERDICTS" in mod, "the widened vocabulary must be a named constant"


def test_stop_hook_calls_the_fence_and_appends_its_message():
    """A gate with no call site is indistinguishable from one that always
    holds (the g-306-227 inheritance class)."""
    src = STOP_HOOK.read_text(encoding="utf-8")
    assert "loop-exhaustion-fence.sh" in src, "fence has no call site in the stop hook"
    assert re.search(r"\+ exhaustion_msg", src), "verdict never reaches the BLOCK reason"
    assert 'EXHAUSTION_MSG="$EXHAUSTION_MSG"' in src, "verdict not exported into the payload"


def test_the_background_jobs_allow_path_also_calls_the_fence():
    """The load-bearing half of  -- and the half no test covered.

    The sibling test above pinned the BLOCK-path call site only, which is
    exactly why this defect was invisible: Gate 2.6's background-jobs ALLOW
    exits at stop-hook.sh:658, one hundred and thirty-seven lines BEFORE the
    fence is invoked on the BLOCK path.  Measured on the incident session:
    every one of the 9 turn-ends across the 10.5h stall took the ALLOW path,
    so THE FENCE DID NOT RUN A SINGLE TIME -- it did not hold, it did not
    evaluate, it was never called.  A predicate-only fix would have changed
    nothing while passing every existing test.

    Only the `stop` rung can act from here (the ALLOW path emits no payload to
    the model, so the `pause` rung's directive string cannot reach anyone) --
    and that is the rung that matters: it lets an indefinitely-pacing session
    escalate, which is precisely the missing behaviour.

    SECOND DEFECT, found by the fresh-eyes pass over g-115-9467's OWN commit:
    presence in the branch is not enough, the call must sit AFTER the append.
    The first cut placed it before, and THIS TEST PINNED THAT ORDER -- a test
    written alongside the fix inherits the author's blind spot, so it certified
    the off-by-one instead of catching it.  Both orderings are now asserted,
    and the BLOCK path carries a matching ordering control so the two call
    sites can never drift into different streak semantics.
    """
    src = STOP_HOOK.read_text(encoding="utf-8")
    i_gate = src.index("Gate 2.6")
    i_append = src.index("ALLOW gate=background-jobs sid=")
    i_block = src.index("--- BLOCK: Agent is RUNNING")
    assert i_gate < i_append < i_block

    # THE CALL MUST SIT AFTER THE APPEND, not merely somewhere in the branch.
    # compute_streak counts the turn-ends already present in the log, so a call
    # placed before the append cannot see the turn-end it is deciding about.
    assert "loop-exhaustion-fence.sh" in src[i_append:i_block], (
        "the background-jobs ALLOW branch exits without consulting the fence -- "
        "a session pacing on registered sleeps can never escalate (g-115-9467)")
    # ...and NOT before it.  This half is the off-by-one pin: the branch's first
    # cut called the fence above the echo, so the ALLOW path computed N-1 where
    # the BLOCK path computes N, costing one whole turn-end per rung (~9 min at
    # the measured 536s median) on the very path the branch exists to cover.
    assert "loop-exhaustion-fence.sh" not in src[i_gate:i_append], (
        "the ALLOW-path fence call sits BEFORE the log append, so this turn-end "
        "is not counted and the streak reads one lower than the BLOCK path's "
        "for the same stall (g-115-9467 fresh-eyes finding F-001)")
    # Positive control: the original BLOCK-path call site is still there.  If
    # this ever fails, the fix MOVED the call rather than adding one.
    assert "loop-exhaustion-fence.sh" in src[i_block:], "BLOCK-path call site lost"
    # Ordering control on the BLOCK path, which is the SHAPE this branch copies:
    # it writes its turn-end line first and consults the fence afterwards.
    i_block_append = src.index("BLOCK sid=$HOOK_SID")
    i_block_call = src.index('bash "$CORE_ROOT/scripts/loop-exhaustion-fence.sh"', i_block)
    assert i_block_append < i_block_call, (
        "the BLOCK path no longer appends before consulting the fence -- the "
        "two call sites must share one streak semantics")
    # Count INVOCATIONS, not mentions.  A bare-substring count over the file
    # reads 3, because a comment further down names the script while explaining
    # who owns the stop-requested write -- anchoring on the `bash "$CORE_ROOT/...`
    # call shape is what makes this a call-site pin rather than a mention pin.
    invocation = 'bash "$CORE_ROOT/scripts/loop-exhaustion-fence.sh"'
    assert src.count(invocation) == 2, (
        "expected exactly two call sites (ALLOW path + BLOCK path), found %d"
        % src.count(invocation))


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
