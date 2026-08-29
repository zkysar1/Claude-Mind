"""test_pending_phase_6_spark_sentinel.py —  regression test.

Pins the sentinel-WM-slot transport that decouples Phase 6 spark dispatch
from recurring-close.sh's stdout. When recurring-close.sh's wall-clock
exceeds the Bash 2-minute timeout, the call backgrounds; the harness fires
the stop hook before bg completes; the LLM re-enters /aspirations loop
never seeing the stdout outcome-aware imperative (g-115-977). Result:
Phase 6 spark silently bypassed on deep recurring closes (observed 2/2:
g-115-760 bfzr7dvyk + g-115-754 bo42a8rld).

Fix (Hypothesis 2 from g-115-1159 investigation): recurring-close.sh
writes pending_phase_6_spark = {goal_id, outcome, source, summary,
expires_at: now+60min} to wm.yaml at the end of Block C/D classification.
aspirations/SKILL.md Phase -0.5c.2 consumes the sentinel on next-iteration
entry — fires Skill(aspirations-spark) when outcome=deep and not expired;
clears silently otherwise.

The bg-race scenario this test covers:
  - recurring-close.sh runs to completion BUT in background
  - LLM never sees the stdout imperative
  - The wm.pending_phase_6_spark sentinel is still on disk
  - Next iteration's Phase -0.5c.2 picks it up, fires Phase 6 spark

Verification strategy: invoke recurring-close.sh's sentinel-write code path
(or a faithful reproduction) and assert (a) the wm slot is set with the
expected shape, (b) outcome is the post-flip value, (c) expires_at is in
the future, (d) re-running with outcome=routine writes a routine sentinel.

Cross-refs:
  - g-115-1174 (this fix — Apply)
  - g-115-1159 (Investigate origin — Phase 6 silent-skip root cause)
  - g-115-977 (prior fix — outcome-aware terminal imperative on stdout)
  - rb-428 (sentinel-lifecycle pattern reused here)
  - core/scripts/recurring-close.sh (sentinel write at bottom)
  - .claude/skills/aspirations/SKILL.md Phase -0.5c.2 (consumer)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"
ITERATION_CLOSE_SH = CORE_SCRIPTS / "iteration-close.sh"

BASH_PATH = shutil.which("bash") or "bash"

# Load the kebab-named spark-fire-dedup.py to unit-test its pure functions
# ( — fast-stdout-path double-fire dedup). importlib is the
# established pattern for hyphenated core/scripts modules (cf.
# test_applies_to_required.py, test_aspirations_compact_completed.py).
import importlib.util as _ilu  # noqa: E402

_DEDUP_SPEC = _ilu.spec_from_file_location(
    "spark_fire_dedup", CORE_SCRIPTS / "spark-fire-dedup.py"
)
spark_fire_dedup = _ilu.module_from_spec(_DEDUP_SPEC)
_DEDUP_SPEC.loader.exec_module(spark_fire_dedup)


def _build_sentinel_payload(goal_id: str, outcome: str, source: str, summary: str) -> dict:
    """Reproduces the sentinel-payload construction from recurring-close.sh.

    This mirrors the inline `py -3` block that builds pending_phase_6_spark.
    Keeping the construction in one place per the test would defeat the test;
    we deliberately reproduce the field-set here so the test catches schema
    drift between the script and what the consumer expects.
    """
    now = datetime.now()
    expires_at = (now + timedelta(minutes=60)).isoformat(timespec="seconds")
    set_at = now.isoformat(timespec="seconds")  # : consumption-based dedup key
    return {
        "goal_id":    goal_id,
        "outcome":    outcome,
        "source":     source,
        "summary":    summary,
        "expires_at": expires_at,
        "set_at":     set_at,
    }


def test_sentinel_payload_shape_has_required_fields():
    """The sentinel MUST carry goal_id, outcome, source, summary, expires_at, set_at.

    The consumer (aspirations/SKILL.md Phase -0.5c.2) reads each of these
    fields. Dropping any one silently breaks the dispatch. set_at is the
    g-115-1404 consumption-based dedup key — without it the consumer's
    --sentinel-set-at falls back to the time-window heuristic that
    false-fired across bg-timeout wall-clock (rb-1674).
    """
    p = _build_sentinel_payload("g-XYZ-99", "deep", "world", "test close")
    for field in ("goal_id", "outcome", "source", "summary", "expires_at", "set_at"):
        assert field in p, f"sentinel missing required field: {field}"


def test_sentinel_outcome_deep_round_trips():
    """outcome=deep must survive payload construction unchanged.

    The consumer's `IF signal.outcome == "deep"` branch is the entire reason
    the sentinel exists. Any case-folding or rewrite breaks dispatch.
    """
    p = _build_sentinel_payload("g-XYZ-99", "deep", "world", "summary")
    assert p["outcome"] == "deep"


def test_sentinel_outcome_routine_round_trips():
    """outcome=routine must survive payload construction unchanged.

    Routine sentinels are cleared silently by the consumer (Phase 6 skip-rule).
    The string must match exactly so the consumer's `ELSE` branch fires.
    """
    p = _build_sentinel_payload("g-XYZ-99", "routine", "agent", "summary")
    assert p["outcome"] == "routine"


def test_sentinel_expires_at_is_future_iso():
    """expires_at must be a valid ISO timestamp ~60 minutes in the future.

    The consumer compares now_iso > expires_at to decide whether to clear
    silently (stale) vs dispatch. A malformed or past timestamp would mean
    every freshly-written sentinel gets cleared without firing Phase 6.
    """
    p = _build_sentinel_payload("g-XYZ-99", "deep", "world", "")
    expires_at = datetime.fromisoformat(p["expires_at"])
    now = datetime.now()
    delta = expires_at - now
    # Allow a small tolerance for test execution time
    assert delta > timedelta(minutes=55), \
        f"expires_at should be ~60min future; got delta={delta}"
    assert delta < timedelta(minutes=65), \
        f"expires_at should be ~60min future; got delta={delta}"


def test_recurring_close_writes_sentinel_block_present():
    """The recurring-close.sh script must contain the sentinel-write block.

    Guards against accidental deletion of the wm-set call. The pattern
    matched here is the literal slot name + the wm-set wrapper invocation.
    """
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    assert "pending_phase_6_spark" in src, \
        "recurring-close.sh missing pending_phase_6_spark sentinel write"
    assert "wm-set.sh" in src, \
        "recurring-close.sh missing wm-set.sh wrapper invocation"
    # The block must invoke wm-set with the sentinel slot name as argv
    assert "wm-set.sh\" pending_phase_6_spark" in src \
        or "wm-set.sh pending_phase_6_spark" in src, \
        "recurring-close.sh wm-set.sh call must target pending_phase_6_spark"


def test_recurring_close_writes_set_at():
    """recurring-close.sh must write set_at into the sentinel payload
    (g-115-1404). Without it the consumer cannot do consumption-based dedup
    and silently falls back to the bg-timeout-fragile time window (rb-1674)."""
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    assert "set_at" in src, \
        "recurring-close.sh missing set_at in pending_phase_6_spark payload (g-115-1404)"


# ── : the NON-recurring sentinel WRITE lives in do_state_update ──
# The  backstop wrote the pending_phase_6_spark sentinel from
# do_verify. But do_verify treats --outcome as OPTIONAL (usage line ~33),
# while do_state_update REQUIRES it (exit 2 when absent). A deep non-recurring
# verify that omitted --outcome left OUTCOME empty → the deep-guard was false →
# the sentinel was silently not written → Phase 6 spark skipped (observed
# ). The fix MOVED the write to do_state_update, where --outcome is
# guaranteed present. These pins guard the move against a silent revert.

def _slice_iteration_close_function(func_name: str) -> str:
    """Return the source text of one do_* function body in iteration-close.sh.

    Slices between this function's `do_<name>() {` marker and the next
    `do_*() {` definition. Robust to bash `${...}`/`$(...)`/JSON-dict braces
    (which a naive brace-counter would trip on) because it uses the ordered
    function-definition markers, not brace depth.
    """
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8")
    import re
    defs = [(m.start(), m.group(1))
            for m in re.finditer(r"^(do_[a-z_]+)\(\) \{", src, re.MULTILINE)]
    assert defs, "no do_*() functions found in iteration-close.sh"
    for idx, (pos, name) in enumerate(defs):
        if name == func_name:
            end = defs[idx + 1][0] if idx + 1 < len(defs) else len(src)
            return src[pos:end]
    raise AssertionError(f"function {func_name}() not found in iteration-close.sh")


def test_sentinel_write_is_in_do_state_update():
    """The pending_phase_6_spark WM write must live in do_state_update ().

    do_state_update REQUIRES --outcome, so OUTCOME is guaranteed non-empty and
    the deep-guard cannot be silently falsified by an omitted flag.
    """
    body = _slice_iteration_close_function("do_state_update")
    assert "wm-set.sh pending_phase_6_spark" in body \
        or 'wm-set.sh" pending_phase_6_spark' in body, \
        "do_state_update missing pending_phase_6_spark sentinel write (g-115-2848 regressed)"


def test_sentinel_write_not_in_do_verify():
    """do_verify must NOT write the pending_phase_6_spark sentinel ().

    do_verify keeps ONLY the stdout imperative — the WRITE moved out because
    --outcome is optional there and an omission defeated the g-115-2416 backstop.
    """
    body = _slice_iteration_close_function("do_verify")
    assert "wm-set.sh pending_phase_6_spark" not in body \
        and 'wm-set.sh" pending_phase_6_spark' not in body, \
        "do_verify still writes the sentinel — g-115-2848 move incomplete (--outcome " \
        "is optional in verify, so the write is unreliable there)"


def test_do_verify_keeps_phase6_stdout_imperative():
    """do_verify must still emit the in-turn Phase-6 stdout imperative ().

    Moving the WRITE must not delete the fast-path prompt that fires the spark
    in-turn when --outcome IS present on the verify call (the common path).
    """
    body = _slice_iteration_close_function("do_verify")
    assert "Phase 6 spark REQUIRED" in body, \
        "do_verify lost the Phase-6 stdout imperative (g-115-2416)"


def test_state_update_sentinel_guarded_on_recurring():
    """The do_state_update sentinel write must be guarded so recurring goals do
    NOT get a double sentinel (g-115-2848).

    recurring-close.sh writes its own POST-FLIP sentinel at end-of-script and
    subprocess-calls iteration-close.sh --phase state-update. Without the
    !recurring guard, a recurring deep close would write the sentinel twice.
    """
    body = _slice_iteration_close_function("do_state_update")
    # The write block must reference the recurring probe and the deep-outcome guard.
    assert "_su_is_recurring" in body, \
        "do_state_update sentinel write missing recurring guard (double-write risk)"
    assert '"$OUTCOME" == "deep"' in body, \
        "do_state_update sentinel write missing deep-outcome guard"


def test_aspirations_skill_md_has_consumer_block():
    """aspirations/SKILL.md Phase -0.5c.2 must consume pending_phase_6_spark.

    Without the consumer, the sentinel write is a no-op. This pin catches
    regression where one side of the producer/consumer pair gets edited
    without the other.
    """
    skill_md = (
        SCRIPT_DIR.parent.parent.parent
        / ".claude" / "skills" / "aspirations" / "SKILL.md"
    )
    assert skill_md.exists(), f"aspirations/SKILL.md not found at {skill_md}"
    body = skill_md.read_text(encoding="utf-8")
    # Phase -0.5c.2 must read the sentinel
    assert "pending_phase_6_spark" in body, \
        "aspirations/SKILL.md missing pending_phase_6_spark consumer"
    # Must dispatch to aspirations-spark on deep
    assert "aspirations-spark" in body, \
        "aspirations/SKILL.md consumer must dispatch /aspirations-spark on deep"


def test_sentinel_payload_is_valid_json():
    """The payload must round-trip through json.dumps + json.loads."""
    p = _build_sentinel_payload("g-115-1174", "deep", "world", "test")
    s = json.dumps(p)
    parsed = json.loads(s)
    assert parsed == p


# ── : consumer-side dedup for the fast-stdout-path double-fire ──
#
# recurring-close.sh writes the sentinel AND emits a stdout imperative; on the
# FAST path the LLM fires Skill(aspirations-spark) in-turn (fire #1) and the
# sentinel then fires it AGAIN next iteration (fire #2). spark-fire-dedup.py
# records each firing in wm.spark_fired_session; Phase -0.5c.2 `check`s it and
# skips the redundant re-fire. The fail-safe contract is load-bearing: a bad
# record must degrade to "fire", NEVER suppress a legitimate spark.

_NOW = datetime(2026, 1, 1, 12, 0, 0)


def test_recently_fired_within_window_true():
    """A goal recorded 2 min ago is 'recently fired' → skip the re-fire."""
    fired = {"g-XYZ-1": (_NOW - timedelta(minutes=2)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.recently_fired(fired, "g-XYZ-1", _NOW, window_minutes=5) is True


def test_recently_fired_outside_window_false():
    """A goal recorded 6 min ago is outside the 5-min window → fire."""
    fired = {"g-XYZ-1": (_NOW - timedelta(minutes=6)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.recently_fired(fired, "g-XYZ-1", _NOW, window_minutes=5) is False


def test_recently_fired_absent_false():
    """A goal not in the map was never fired → fire (fail-safe default)."""
    assert spark_fire_dedup.recently_fired({}, "g-XYZ-1", _NOW) is False


def test_recently_fired_malformed_ts_false():
    """An unparseable timestamp must NOT suppress the spark → fire."""
    fired = {"g-XYZ-1": "not-a-timestamp"}
    assert spark_fire_dedup.recently_fired(fired, "g-XYZ-1", _NOW) is False


def test_recently_fired_future_ts_false():
    """A future timestamp (clock skew) is not 'recent' → fire, don't suppress."""
    fired = {"g-XYZ-1": (_NOW + timedelta(minutes=3)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.recently_fired(fired, "g-XYZ-1", _NOW) is False


def test_recently_fired_non_dict_false():
    """A non-dict map (corrupt wm slot) must degrade to fire, not raise."""
    assert spark_fire_dedup.recently_fired(None, "g-XYZ-1", _NOW) is False
    assert spark_fire_dedup.recently_fired("garbage", "g-XYZ-1", _NOW) is False


def test_prune_and_record_adds_goal():
    """record stamps the goal_id at `now`."""
    out = spark_fire_dedup.prune_and_record({}, "g-XYZ-1", _NOW)
    assert out["g-XYZ-1"] == _NOW.isoformat(timespec="seconds")


def test_prune_and_record_prunes_old_entries():
    """record drops entries older than the prune window (default 90 min,
    widened from 10 by g-115-1404 so a fired entry survives the sentinel's
    ~60-min lifetime for the set_at comparison to find it)."""
    fired = {
        "old": (_NOW - timedelta(minutes=95)).isoformat(timespec="seconds"),
        "fresh": (_NOW - timedelta(minutes=3)).isoformat(timespec="seconds"),
    }
    out = spark_fire_dedup.prune_and_record(fired, "g-XYZ-1", _NOW)
    assert "old" not in out
    assert "fresh" in out
    assert "g-XYZ-1" in out


def test_prune_and_record_does_not_mutate_input():
    """record returns a new dict; the input map is untouched."""
    fired = {"a": (_NOW - timedelta(minutes=1)).isoformat(timespec="seconds")}
    snapshot = dict(fired)
    spark_fire_dedup.prune_and_record(fired, "b", _NOW)
    assert fired == snapshot


def test_record_then_check_round_trip_semantics():
    """End-to-end pure-function semantics: after record, a check within the
    window reports skip-eligible (True); once the window elapses, fire (False).
    Passes window_minutes=5 explicitly so the test pins the window-boundary
    mechanism independent of DEFAULT_WINDOW_MIN (g-115-1404 widened the default
    5->60)."""
    fired = spark_fire_dedup.prune_and_record({}, "g-XYZ-9", _NOW)
    assert spark_fire_dedup.recently_fired(fired, "g-XYZ-9", _NOW + timedelta(minutes=2), window_minutes=5) is True
    assert spark_fire_dedup.recently_fired(fired, "g-XYZ-9", _NOW + timedelta(minutes=7), window_minutes=5) is False


# --  / rb-1674 +  / rb-2615: consumption-window dedup -------
#
# The 5-min time window false-fired across the bg-timeout wall-clock between
# record (aspirations-spark Step 0.5) and check (next loop entry): spark
# increments + a >5min loop batch elapsed the window, so check returned "fire"
# for an already-fired deep spark, double-firing it. The consumption path
# brackets the recorded fire time against THIS sentinel's set_at with a window
# [set_at - MAX_BG_CLOSE_DURATION_MIN, set_at + TTL]:
#   - fired inside the window (EITHER side of set_at) = this close's spark -> skip.
#     The NORMAL path fires AFTER set_at; the PROACTIVE path () fires off
#     recurring-close.sh's stdout imperative up to ~MAX_BG_CLOSE_DURATION_MIN
#     BEFORE the bg close writes set_at (incident : 6m11s early) -- the
#     strict `>= set_at` test mis-read that as a prior close and false-fired.
#   - fired well before the lower bound (recurring intervals are hours) or no
#     entry -> a genuine previous close -> fire.
# _NOW doubles as the sentinel set_at in these tests.


def test_fired_in_consumption_window_fired_after_set_at_true():
    """A spark fired AFTER set_at (within the lookahead) is this close's
    re-fire -> skip."""
    fired = {"g-XYZ-1": (_NOW + timedelta(minutes=4)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", _NOW) is True


def test_fired_in_consumption_window_fired_exactly_at_set_at_true():
    """fired_at == set_at is inside the window -> skip (boundary case)."""
    fired = {"g-XYZ-1": _NOW.isoformat(timespec="seconds")}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", _NOW) is True


def test_fired_in_consumption_window_proactive_before_set_at_true():
    """ REGRESSION: a PROACTIVE fire recorded within
    MAX_BG_CLOSE_DURATION_MIN before set_at is this close's consumption -> skip.
    Pre-fix (strict fired_at >= set_at) this false-fired (incident g-115-399:
    fire 6m11s before set_at)."""
    fired = {"g-XYZ-1": (_NOW - timedelta(minutes=6)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", _NOW) is True


def test_fired_in_consumption_window_prior_close_before_window_false():
    """A spark from a genuine PREVIOUS close (well before the lower bound --
    recurring intervals are hours) -> fire, not suppress. 20 min exceeds the
    15-min lookback (g-115-2988), so it is outside the window."""
    fired = {"g-XYZ-1": (_NOW - timedelta(minutes=20)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", _NOW) is False


def test_fired_in_consumption_window_slow_resume_gap_within_widened_bound_true():
    """ REGRESSION: a NON-recurring in-turn Phase-6 spark can fire well
    before the do_state_update sentinel's set_at under a slow post-compaction
    resume (observed 10m08s: fire 23:03:24 vs set_at 23:13:32, g-115-2984). The
    old 10-min lower bound false-fired by 8s; the widened 15-min bound
    (MAX_BG_CLOSE_DURATION_MIN) counts a 10m30s-early fire as THIS close's
    consumption -> skip. Pins the widen: this fire is False under 10, True under 15."""
    fired = {"g-XYZ-1": (_NOW - timedelta(minutes=10, seconds=30)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", _NOW) is True


def test_fired_in_consumption_window_at_lower_bound_exactly_true():
    """: the lower bound is INCLUSIVE (`lo <= ts`), so a fire at
    exactly MAX_BG_CLOSE_DURATION_MIN before set_at still counts as this close's
    consumption -> skip. Sibling tests bracket this at 10m30s (True) and 20m
    (False) but neither pins the boundary itself, so an accidental `lo < ts`
    would pass the whole suite. Bound is DERIVED from the module constant
    (guard-1648): widening MAX_BG_CLOSE_DURATION_MIN moves this case with it
    rather than silently un-pinning the boundary."""
    lb = spark_fire_dedup.MAX_BG_CLOSE_DURATION_MIN
    fired = {"g-XYZ-1": (_NOW - timedelta(minutes=lb)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", _NOW) is True


def test_fired_in_consumption_window_one_second_past_lower_bound_false():
    """The exclusive side of the same boundary: one second beyond the lookback
    is a genuine PRIOR close -> fire. Paired with the test above, this pins the
    bound as a real edge — without it, a window widened to infinity would also
    satisfy the inclusive case."""
    lb = spark_fire_dedup.MAX_BG_CLOSE_DURATION_MIN
    fired = {"g-XYZ-1": (_NOW - timedelta(minutes=lb, seconds=1)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", _NOW) is False


def test_fired_in_consumption_window_far_future_beyond_ttl_false():
    """A fire far beyond set_at + TTL (clock skew / a stale entry past the
    sentinel's ~60-min life) is not this close's consumption -> fire. Pins the
    upper bound the window added over the old unbounded `>= set_at` test."""
    fired = {"g-XYZ-1": (_NOW + timedelta(minutes=90)).isoformat(timespec="seconds")}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", _NOW) is False


def test_fired_in_consumption_window_absent_goal_false():
    """No entry for the goal -> never fired for this close -> fire (fail-safe)."""
    assert spark_fire_dedup.fired_in_consumption_window({}, "g-XYZ-1", _NOW) is False


def test_fired_in_consumption_window_none_set_at_false():
    """A None set_at (no sentinel timestamp) -> fire; caller falls back to window."""
    fired = {"g-XYZ-1": _NOW.isoformat(timespec="seconds")}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", None) is False


def test_fired_in_consumption_window_malformed_fired_ts_false():
    """An unparseable fired timestamp must NOT suppress the spark -> fire."""
    fired = {"g-XYZ-1": "not-a-timestamp"}
    assert spark_fire_dedup.fired_in_consumption_window(fired, "g-XYZ-1", _NOW) is False


def test_fired_in_consumption_window_non_dict_false():
    """A non-dict map (corrupt wm slot) must degrade to fire, not raise."""
    assert spark_fire_dedup.fired_in_consumption_window(None, "g-XYZ-1", _NOW) is False
    assert spark_fire_dedup.fired_in_consumption_window("garbage", "g-XYZ-1", _NOW) is False


def test_sentinel_set_at_precedes_expires_at_by_window():
    """set_at must be ~60min before expires_at -- the consumer derives the
    sentinel's lifetime from this gap, and the prune window (90min) must exceed
    it so the fired entry survives for the set_at comparison."""
    p = _build_sentinel_payload("g-XYZ-99", "deep", "world", "")
    set_at = datetime.fromisoformat(p["set_at"])
    expires_at = datetime.fromisoformat(p["expires_at"])
    gap = expires_at - set_at
    assert timedelta(minutes=59) < gap < timedelta(minutes=61), \
        f"expires_at should be ~60min after set_at; got gap={gap}"


def test_consumer_phase_minus_0_5c_2_has_dedup_check():
    """aspirations/SKILL.md Phase -0.5c.2 deep branch must call the dedup
    `check` before firing — the fix for the fast-stdout double-fire."""
    skill_md = (
        SCRIPT_DIR.parent.parent.parent
        / ".claude" / "skills" / "aspirations" / "SKILL.md"
    )
    body = skill_md.read_text(encoding="utf-8")
    assert "spark-fire-dedup.py check" in body, \
        "Phase -0.5c.2 must call spark-fire-dedup.py check before firing (g-115-1203)"
    assert "dedup-skip" in body, \
        "Phase -0.5c.2 must have a dedup-skip branch that clears without re-firing"


def test_consumer_passes_sentinel_set_at():
    """aspirations/SKILL.md Phase -0.5c.2 must pass --sentinel-set-at to the
    dedup check so dedup is consumption-based (skip iff fired_at lands in the
    window BRACKETING set_at, [set_at - 15min, set_at + 60min]) rather than the
    time-window heuristic that false-fired (g-115-1404). NOT the strict
    `fired_at >= set_at` test — that was retired by g-306-80/rb-2615 because on
    the non-recurring path the in-turn Phase-6 spark fires BEFORE
    do_state_update writes set_at, making fired_at < set_at the NORMAL shape
    there. (g-115-3264 asserted the retired rule as current and concluded the
    dedup was structurally dead on that path; whether it read this phrasing is
    not recorded — the observation is only that both stated the same retired
    test.)"""
    skill_md = (
        SCRIPT_DIR.parent.parent.parent
        / ".claude" / "skills" / "aspirations" / "SKILL.md"
    )
    body = skill_md.read_text(encoding="utf-8")
    assert "--sentinel-set-at" in body, \
        "Phase -0.5c.2 must pass --sentinel-set-at to spark-fire-dedup.py check (g-115-1404)"


def test_spark_skill_records_firing():
    """aspirations-spark/SKILL.md must record the firing (Step 0.5) so the
    sentinel consumer can dedup the fast-path double-fire."""
    spark_md = (
        SCRIPT_DIR.parent.parent.parent
        / ".claude" / "skills" / "aspirations-spark" / "SKILL.md"
    )
    body = spark_md.read_text(encoding="utf-8")
    assert "spark-fire-dedup.py record" in body, \
        "aspirations-spark must record its firing via spark-fire-dedup.py record (g-115-1203)"


# ── CLI stdin->stdout contract (the pipeline the SKILL.md blocks rely on) ──
#
# The helper is PURE: it does NO wm I/O of its own (a python->bash subprocess
# to wm-*.sh hangs on this platform — rb-225/rb-247). The wm read/write live
# in the bash pipeline: `wm-read | spark-fire-dedup.py {record|check} | wm-set`.
# These tests pin the stdin->stdout contract that pipeline depends on. They run
# python->python (sys.executable), which does NOT hang.

DEDUP_PY = CORE_SCRIPTS / "spark-fire-dedup.py"


def _run_dedup_cli(args_list, stdin_text):
    """Invoke the spark-fire-dedup CLI with stdin_text; return (rc, stdout)."""
    r = subprocess.run(
        [sys.executable, str(DEDUP_PY)] + args_list,
        input=stdin_text, capture_output=True, text=True, timeout=30,
    )
    return r.returncode, r.stdout.strip()


def test_cli_record_adds_to_stdin_map_and_preserves_recent():
    """record reads the current map from stdin and emits the new map with the
    goal_id stamped, preserving other recent entries. NOTE: the CLI prunes
    against the REAL datetime.now() (not the _NOW fixture), so the 'other'
    entry must be recent relative to real now — else it is correctly pruned."""
    other_ts = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    rc, out = _run_dedup_cli(["record", "g-XYZ-1"], json.dumps({"g-OTHER": other_ts}))
    assert rc == 0
    parsed = json.loads(out)
    assert "g-XYZ-1" in parsed
    assert "g-OTHER" in parsed


def test_cli_record_handles_null_stdin():
    """record treats 'null' stdin (an unset wm slot prints 'null') as empty."""
    rc, out = _run_dedup_cli(["record", "g-XYZ-1"], "null")
    assert rc == 0
    assert list(json.loads(out).keys()) == ["g-XYZ-1"]


def test_cli_record_then_check_skips_live():
    """End-to-end CLI: feed record's emitted map into check → skip (exit 1).
    This is the wm-independent analogue of the live pipeline round-trip and is
    the regression guard for the original subprocess-hang bug (the live wm
    round-trip silently failed because the helper shelled out to bash)."""
    rc1, recorded = _run_dedup_cli(["record", "g-XYZ-9"], "null")
    assert rc1 == 0
    rc2, out2 = _run_dedup_cli(["check", "g-XYZ-9"], recorded)
    assert out2 == "skip"
    assert rc2 == 1


def test_cli_check_fire_for_unrecorded_goal():
    """check reports fire (exit 0) for a goal absent from the stdin map."""
    rc, out = _run_dedup_cli(["check", "g-NEVER"], "null")
    assert out == "fire"
    assert rc == 0


def test_cli_check_fire_on_garbage_stdin():
    """check fails open to 'fire' on malformed stdin — never suppress a spark."""
    rc, out = _run_dedup_cli(["check", "g-XYZ-1"], "{not valid json")
    assert out == "fire"
    assert rc == 0


# -- CLI --sentinel-set-at: the consumption-based dedup path () -----


def test_cli_check_skip_when_fired_at_or_after_set_at():
    """check with --sentinel-set-at skips (exit 1) when the recorded fire is
    AT/AFTER set_at — a spark fired in response to THIS close (g-115-1404)."""
    set_at = datetime.now() - timedelta(minutes=2)
    fired_at = (set_at + timedelta(minutes=1)).isoformat(timespec="seconds")
    stdin = json.dumps({"g-XYZ-9": fired_at})
    rc, out = _run_dedup_cli(
        ["check", "g-XYZ-9", "--sentinel-set-at", set_at.isoformat(timespec="seconds")],
        stdin,
    )
    assert out == "skip"
    assert rc == 1


def test_cli_check_fire_when_fired_well_before_set_at():
    """check with --sentinel-set-at fires (exit 0) when the recorded fire
    PREDATES set_at by MORE than MAX_BG_CLOSE_DURATION_MIN — a spark from a
    genuine previous close, not this one (recurring intervals are hours). 20 min
    is outside the 15-min lookback window (g-115-2988), so it still fires (rb-1674 / g-306-80)."""
    set_at = datetime.now()
    fired_at = (set_at - timedelta(minutes=20)).isoformat(timespec="seconds")
    stdin = json.dumps({"g-XYZ-9": fired_at})
    rc, out = _run_dedup_cli(
        ["check", "g-XYZ-9", "--sentinel-set-at", set_at.isoformat(timespec="seconds")],
        stdin,
    )
    assert out == "fire"
    assert rc == 0


def test_cli_check_skip_proactive_fire_before_set_at():
    """ REGRESSION (CLI): a PROACTIVE fire recorded ~6 min BEFORE set_at
    (the LLM fired off recurring-close.sh's stdout imperative while the bg close
    was still writing set_at) is inside the MAX_BG_CLOSE_DURATION_MIN lookback
    -> skip (exit 1). Pre-fix the strict `>= set_at` test false-fired it next
    iteration (incident g-115-399: fire 01:55:14 vs set_at 02:01:25)."""
    set_at = datetime.now()
    fired_at = (set_at - timedelta(minutes=6)).isoformat(timespec="seconds")
    stdin = json.dumps({"g-XYZ-9": fired_at})
    rc, out = _run_dedup_cli(
        ["check", "g-XYZ-9", "--sentinel-set-at", set_at.isoformat(timespec="seconds")],
        stdin,
    )
    assert out == "skip"
    assert rc == 1


def test_cli_check_set_at_absent_goal_fires():
    """check with --sentinel-set-at fires for a goal absent from the map."""
    set_at = datetime.now().isoformat(timespec="seconds")
    rc, out = _run_dedup_cli(["check", "g-NEVER", "--sentinel-set-at", set_at], "null")
    assert out == "fire"
    assert rc == 0


def test_cli_check_malformed_set_at_falls_back_to_window():
    """A malformed --sentinel-set-at parses to None, so check falls back to the
    time-window heuristic. A fire recorded just now is within the (60-min
    default) window → skip. Pins the fallback path's wiring (g-115-1404)."""
    fired_at = datetime.now().isoformat(timespec="seconds")
    stdin = json.dumps({"g-XYZ-9": fired_at})
    rc, out = _run_dedup_cli(
        ["check", "g-XYZ-9", "--sentinel-set-at", "not-a-timestamp"],
        stdin,
    )
    assert out == "skip"
    assert rc == 1


# ══════════════════════════════════════════════════════════════════════════
#  /  — write-site elimination of the dedup window.
#
# The read-side lookback (MAX_BG_CLOSE_DURATION_MIN) was outgrown FOUR times
# because on the NON-recurring path it was bounding the LLM's Phase-6 work,
# which is unbounded by design. The fix stops writing the sentinel at all when
# an in-turn spark is already recorded, so no timing window is consulted.
#
# The four field measurements below are INCIDENT DATA (real gaps, real dates),
# used as regression fixtures. They are deliberately hardcoded — guard-1220
# forbids restating the OTHER COMPONENT's expectation, not pinning observed
# incident values.
# ══════════════════════════════════════════════════════════════════════════

# (goal_id, fired_at, set_at, gap_label) — every one returned a FALSE `fire`.
FIELD_MEASUREMENTS = [
    ("g-115-3338", "2026-07-26T16:27:36", "2026-07-26T16:42:45", "15m09s (zeta)"),
    ("g-115-3576", "2026-07-28T01:20:58", "2026-07-28T01:36:18", "15m20s (bravo)"),
    ("g-115-3632", "2026-07-28T08:58:40", "2026-07-28T09:13:47", "15m07s (zeta)"),
    ("g-335-392",  "2026-07-28T17:21:26", "2026-07-28T17:37:57", "16m31s (alpha)"),
    ("g-115-3597", "2026-07-28T02:53:45", "2026-07-28T03:17:45", "24m00s (g-115-3609)"),
]


def _writer_producer_literal() -> str:
    """Read the `producer` literal the WRITER (iteration-close.sh) actually
    emits into the sentinel payload.

    guard-1220: for a bug spanning a producer and a consumer, the expected value
    MUST be read from the other component at runtime — never restated by the
    test. If this test hardcoded "nonrecurring-state-update" it would be
    self-consistent with the reader alone and could NOT fail on the exact
    boundary mismatch it exists to catch (the reader's constant drifting away
    from the writer's literal). Same lesson as rb-5562: when a pre-commit
    boundary forbids a shared constant, the coupling has to be a test that reads
    the other side as text.
    """
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'"producer":\s*"([^"]+)"', src)
    assert m, "iteration-close.sh no longer emits a `producer` field in the sentinel payload"
    return m.group(1)


def test_producer_literal_matches_between_writer_and_reader():
    """BOUNDARY TEST (guard-1220). The writer's emitted literal and the reader's
    NONRECURRING_PRODUCER constant are duplicated across a shell/python
    boundary that admits no shared constant. Derive the expectation from the
    writer, compare to the reader — so a drift on EITHER side goes red."""
    assert _writer_producer_literal() == spark_fire_dedup.NONRECURRING_PRODUCER


def test_write_site_consults_spark_fired_session_before_writing():
    """The structural fix: do_state_update asks whether the spark already fired
    BEFORE writing the sentinel. Without this call the window is back."""
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8", errors="replace")
    assert "spark-fire-dedup.py\" fired" in src or "spark-fire-dedup.py fired" in src, \
        "do_state_update no longer invokes the `fired` write-side gate"
    assert "spark_fired_session" in src, \
        "do_state_update no longer reads the spark_fired_session map"
    assert "skip-write" in src, "do_state_update no longer honors the skip-write verdict"


def test_write_site_gate_precedes_the_wm_set():
    """Ordering matters: the gate must be consulted BEFORE the wm-set that
    writes the sentinel, or it cannot suppress anything."""
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8", errors="replace")
    gate_at = src.index("spark-fire-dedup.py")
    set_at_idx = src.index("wm-set.sh\" pending_phase_6_spark")
    assert gate_at < set_at_idx, "write-side gate must precede the sentinel wm-set"


def test_consumer_passes_producer_flag():
    """The read-side backstop only engages if the consumer forwards `producer`."""
    skill_md = (CORE_SCRIPTS.parent.parent / ".claude" / "skills" / "aspirations" / "SKILL.md")
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    assert "--producer" in text, "Phase -0.5c.2 no longer forwards --producer to the dedup check"


# ---------------------------------------------------------------- already_fired

def test_already_fired_present_true():
    assert spark_fire_dedup.already_fired_this_close({"g-A": "2026-07-28T18:18:01"}, "g-A") is True


def test_already_fired_absent_false():
    assert spark_fire_dedup.already_fired_this_close({"g-A": "2026-07-28T18:18:01"}, "g-B") is False


def test_already_fired_malformed_ts_false():
    """Fail-open direction is INVERTED vs the read-side helpers: False here means
    WRITE the sentinel, which is the safe answer on bad data."""
    assert spark_fire_dedup.already_fired_this_close({"g-A": "garbage"}, "g-A") is False


def test_already_fired_non_dict_false():
    assert spark_fire_dedup.already_fired_this_close(None, "g-A") is False
    assert spark_fire_dedup.already_fired_this_close("nope", "g-A") is False


def test_already_fired_ignores_elapsed_time_entirely():
    """THE POINT OF THE FIX. A fire 10 hours before set_at still suppresses the
    write, because a non-recurring goal closes exactly once — there is no prior
    close to mis-match. Any timestamp comparison reintroduces the uncalibratable
    bound. If someone adds one back, this test goes red."""
    ancient = (datetime.now() - timedelta(hours=10)).isoformat(timespec="seconds")
    assert spark_fire_dedup.already_fired_this_close({"g-A": ancient}, "g-A") is True


# ------------------------------------------------------------------ gap_seconds

def test_gap_seconds_measures_uncensored_gap():
    gap = spark_fire_dedup.gap_seconds(
        {"g-A": "2026-07-28T18:18:01"}, "g-A",
        datetime.fromisoformat("2026-07-28T18:25:21"),
    )
    assert gap == 440.0  # foxtrot's own correctly-deduped close — the uncensored class


def test_gap_seconds_negative_when_fired_after_set_at():
    gap = spark_fire_dedup.gap_seconds(
        {"g-A": "2026-07-28T18:30:00"}, "g-A",
        datetime.fromisoformat("2026-07-28T18:25:00"),
    )
    assert gap == -300.0


def test_gap_seconds_none_when_absent_or_unparseable():
    now = datetime.now()
    assert spark_fire_dedup.gap_seconds({}, "g-A", now) is None
    assert spark_fire_dedup.gap_seconds({"g-A": "junk"}, "g-A", now) is None
    assert spark_fire_dedup.gap_seconds({"g-A": now.isoformat()}, "g-A", None) is None


# --------------------------------------------- read-side backstop, both orderings

def test_field_measurements_all_deduped_under_nonrecurring_producer():
    """All FOUR real-world false-fires now SKIP. Each was a genuine in-turn spark
    that the bounded lookback failed to recognize."""
    for goal_id, fired_at, set_at, label in FIELD_MEASUREMENTS:
        assert spark_fire_dedup.fired_in_consumption_window(
            {goal_id: fired_at}, goal_id, datetime.fromisoformat(set_at),
            lookback_minutes=spark_fire_dedup.UNBOUNDED_LOOKBACK,
        ) is True, f"{label} should dedup under the unbounded lower bound"


def test_field_measurements_all_false_fired_under_old_bounded_lookback():
    """Pins the DEFECT itself: with the bounded lookback every one returns False
    (fire). If this ever goes green, the regression fixture has drifted and the
    test above proves nothing."""
    for goal_id, fired_at, set_at, label in FIELD_MEASUREMENTS:
        assert spark_fire_dedup.fired_in_consumption_window(
            {goal_id: fired_at}, goal_id, datetime.fromisoformat(set_at),
            lookback_minutes=spark_fire_dedup.MAX_BG_CLOSE_DURATION_MIN,
        ) is False, f"{label} should have false-fired under the old bound"


def test_unbounded_lookback_still_respects_upper_ttl_bound():
    """Dropping the LOWER bound must not drop the upper one — a far-future stamp
    (clock skew / stale entry beyond the sentinel's life) must still fire."""
    assert spark_fire_dedup.fired_in_consumption_window(
        {"g-A": "2026-07-28T23:59:00"}, "g-A",
        datetime.fromisoformat("2026-07-28T18:00:00"),
        lookback_minutes=spark_fire_dedup.UNBOUNDED_LOOKBACK,
    ) is False


def test_recurring_path_keeps_bounded_lookback_prior_close_still_fires():
    """The recurring path must be UNTOUCHED: a genuine prior close (hours back,
    recurring intervals are hours) must still FIRE. This is what the lower bound
    is legitimately for, and why it is kept for that producer."""
    assert spark_fire_dedup.fired_in_consumption_window(
        {"g-R": "2026-07-28T02:00:00"}, "g-R",
        datetime.fromisoformat("2026-07-28T18:00:00"),
    ) is False


def test_recurring_path_proactive_fire_still_skips():
    """...while the proactive bg-timeout fire it was built for still SKIPs."""
    assert spark_fire_dedup.fired_in_consumption_window(
        {"g-R": "2026-07-28T17:55:00"}, "g-R",
        datetime.fromisoformat("2026-07-28T18:00:00"),
    ) is True


# ------------------------------------------------------------------------- CLI

def test_cli_fired_skip_write_and_reports_gap():
    rc, out = _run_dedup_cli(
        ["fired", "g-A", "--set-at", "2026-07-28T18:25:21"],
        json.dumps({"g-A": "2026-07-28T18:18:01"}),
    )
    assert out.split("\t")[0] == "skip-write"
    assert out.split("\t")[1] == "440"
    assert rc == 1


def test_cli_fired_write_when_no_entry():
    rc, out = _run_dedup_cli(["fired", "g-B", "--set-at", "2026-07-28T18:25:21"],
                             json.dumps({"g-A": "2026-07-28T18:18:01"}))
    assert out == "write"
    assert rc == 0


def test_cli_fired_write_on_garbage_stdin():
    """Fail-open: unparseable map → write the sentinel (never lose a spark)."""
    rc, out = _run_dedup_cli(["fired", "g-A"], "{{{not json")
    assert out == "write"
    assert rc == 0


def test_cli_check_producer_flag_drops_lower_bound():
    """End-to-end through the CLI: the same input flips fire→skip purely on the
    presence of the producer flag."""
    stdin = json.dumps({"g-115-3338": "2026-07-26T16:27:36"})
    _, without = _run_dedup_cli(
        ["check", "g-115-3338", "--sentinel-set-at", "2026-07-26T16:42:45"], stdin)
    _, with_prod = _run_dedup_cli(
        ["check", "g-115-3338", "--sentinel-set-at", "2026-07-26T16:42:45",
         "--producer", spark_fire_dedup.NONRECURRING_PRODUCER], stdin)
    assert without == "fire"
    assert with_prod == "skip"


def test_cli_check_unknown_producer_keeps_bounded_behavior():
    """Backward-compat: an empty or unrecognized producer (recurring-close.sh,
    or any pre-g-115-3351 sentinel) must keep the bounded lookback."""
    stdin = json.dumps({"g-115-3338": "2026-07-26T16:27:36"})
    for prod in ("", "recurring-close", "something-else"):
        _, out = _run_dedup_cli(
            ["check", "g-115-3338", "--sentinel-set-at", "2026-07-26T16:42:45",
             "--producer", prod], stdin)
        assert out == "fire", f"producer={prod!r} must keep the bounded behavior"


# ---------------------------------------------------------------------------
# : an ABSENT spark_fired_session record is not evidence the spark
# never fired. The record is written by aspirations-spark Step 0.5 — one bash
# line at the top of a long LLM-executed skill (compliance measured 3 of 4 on
# the observed session) — so "Phase 6 never ran" and "Phase 6 ran and did not
# record it" look identical at `check` and demand OPPOSITE actions. No window
# can separate them: with no key present, every bound returns 'fire'. The
# execution diary is an INDEPENDENT signal with a DIFFERENT (script) writer,
# keyed by goal_id, and decides it.
#
# Measured incident (echo, cc-03, 2026-08-05): sentinel for  consumed
# at 05:31 with spark_fired_session holding only  /  /
# ; the diary carried ` | phase-6-spark` phase_start AND
# phase_end at 04:53, ~28 min before the sentinel's 05:21:32 set_at. One full
# redundant Phase-6 re-fire, which would have double-counted times_asked across
# all 20 active spark questions into the yield_rate denominators that drive the
# retire/promote review.
# ---------------------------------------------------------------------------

# The exact spark_fired_session contents measured at the incident — no key for
# the goal under test, which is the whole point.
ECHO_MAP = json.dumps({
    "g-250-293": "2026-08-05T02:18:31",
    "g-335-710": "2026-08-05T02:47:34",
    "g-335-713": "2026-08-05T03:37:11",
})
ECHO_SET_AT = "2026-08-05T05:21:32"
ECHO_GOAL = "g-335-717"


def _diary(tmp_path, rows, name="execution-diary.jsonl"):
    """Write JSONL rows to a temp diary and return its path as a string."""
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(p)


def _spark_row(goal_id, ts, kind="phase_start", phase="phase-6-spark"):
    return {"entry_type": kind, "phase": phase, "timestamp": ts,
            "content": f"{kind} {phase}", "goal_id": goal_id}


def test_diary_fired_at_finds_the_marker():
    """The pure core takes LINES, never a path, so it is testable with no
    filesystem — the same contract every other helper in the module keeps."""
    lines = [json.dumps(_spark_row(ECHO_GOAL, "2026-08-05T04:53:00"))]
    got = spark_fire_dedup.diary_fired_at(lines, ECHO_GOAL)
    assert got == datetime(2026, 8, 5, 4, 53, 0)


def test_diary_fired_at_returns_the_LATEST_of_several():
    """A recurring goal accumulates markers across closes; only the most recent
    can be THIS close's, and the consumption window judges it from there."""
    lines = [
        json.dumps(_spark_row(ECHO_GOAL, "2026-08-04T01:00:00")),
        json.dumps(_spark_row(ECHO_GOAL, "2026-08-05T04:53:00")),
        json.dumps(_spark_row(ECHO_GOAL, "2026-08-05T04:53:40", kind="phase_end")),
    ]
    assert spark_fire_dedup.diary_fired_at(lines, ECHO_GOAL) == datetime(2026, 8, 5, 4, 53, 40)


def test_diary_fired_at_ignores_another_goals_marker():
    lines = [json.dumps(_spark_row("g-OTHER-999", "2026-08-05T04:53:00"))]
    assert spark_fire_dedup.diary_fired_at(lines, ECHO_GOAL) is None


def test_diary_fired_at_ignores_another_phase_for_the_same_goal():
    """phase-4-execute rows for the same goal are abundant; only a spark row
    is evidence a spark fired."""
    lines = [json.dumps(_spark_row(ECHO_GOAL, "2026-08-05T04:53:00",
                                   phase="phase-4-execute"))]
    assert spark_fire_dedup.diary_fired_at(lines, ECHO_GOAL) is None


def test_diary_fired_at_ignores_a_row_that_only_MENTIONS_the_phase_in_its_content():
    """The `phase` FIELD decides, not the presence of the string anywhere in the
    row. This is the case the cheap substring prefilter structurally cannot
    catch — diary rows carry a `content` field, and the incident's own spark
    rows had content "phase_start phase-6-spark", so a row of a DIFFERENT phase
    whose content mentions the spark phase passes the prefilter and reaches the
    explicit field check.

    Written after mutation testing: deleting `row.get("phase") != phase` flipped
    NOTHING, because the sibling test above uses a phase-4-execute row that the
    prefilter rejects before the field check is ever reached. That test pins the
    PREFILTER; this one pins the FILTER."""
    lines = [json.dumps({
        "entry_type": "phase_end",
        "phase": "phase-4-execute",
        "timestamp": "2026-08-05T04:53:00",
        "content": "phase_end phase-4-execute — deferred phase-6-spark to next turn",
        "goal_id": ECHO_GOAL,
    })]
    assert spark_fire_dedup.diary_fired_at(lines, ECHO_GOAL) is None


def test_diary_fired_at_survives_garbage_and_returns_none():
    """Fail-open at every layer: unparseable lines, non-dict rows, a missing
    timestamp and a non-string entry are all skipped, and nothing usable
    returns None -> no corroboration -> 'fire'."""
    lines = [
        "not json at all — phase-6-spark " + ECHO_GOAL,
        json.dumps(["phase-6-spark", ECHO_GOAL]),
        json.dumps({"phase": "phase-6-spark", "goal_id": ECHO_GOAL}),
        json.dumps({"phase": "phase-6-spark", "goal_id": ECHO_GOAL, "timestamp": "not-a-time"}),
        None,
        "",
    ]
    assert spark_fire_dedup.diary_fired_at(lines, ECHO_GOAL) is None
    assert spark_fire_dedup.diary_fired_at([], ECHO_GOAL) is None
    assert spark_fire_dedup.diary_fired_at(None, ECHO_GOAL) is None


def test_cli_absent_record_without_diary_still_fires_UNCHANGED(tmp_path):
    """Backward compatibility AND the positive control for the next test: with
    no --diary-file the incident reproduces exactly as measured. If this ever
    returns 'skip', the next test proves nothing — it would be passing on
    behavior that was already there."""
    rc, out = _run_dedup_cli(
        ["check", ECHO_GOAL, "--sentinel-set-at", ECHO_SET_AT,
         "--producer", spark_fire_dedup.NONRECURRING_PRODUCER], ECHO_MAP)
    assert out == "fire"
    assert rc == 0


def test_cli_absent_record_IS_corroborated_by_the_diary(tmp_path):
    """The fix, on the incident's own numbers: same map, same set_at, same
    producer — only the diary is added, and the verdict flips."""
    diary = _diary(tmp_path, [
        _spark_row(ECHO_GOAL, "2026-08-05T04:10:00", phase="phase-4-execute"),
        _spark_row(ECHO_GOAL, "2026-08-05T04:53:00"),
        _spark_row(ECHO_GOAL, "2026-08-05T04:53:40", kind="phase_end"),
    ])
    rc, out = _run_dedup_cli(
        ["check", ECHO_GOAL, "--sentinel-set-at", ECHO_SET_AT,
         "--producer", spark_fire_dedup.NONRECURRING_PRODUCER,
         "--diary-file", diary], ECHO_MAP)
    assert out == "skip", "an absent record with a diary marker must dedup (g-115-4201)"
    assert rc == 1


def test_cli_diary_with_no_marker_for_this_goal_still_fires(tmp_path):
    """FAIL-OPEN PRESERVED, and this is the assertion that keeps the fix from
    becoming a suppressor: a diary that exists and is readable but carries no
    spark row for THIS goal must not dedup. Absent record AND absent marker
    still fires — unchanged from before this feature."""
    diary = _diary(tmp_path, [
        _spark_row("g-OTHER-999", "2026-08-05T04:53:00"),
        _spark_row(ECHO_GOAL, "2026-08-05T04:53:00", phase="phase-4-execute"),
    ])
    rc, out = _run_dedup_cli(
        ["check", ECHO_GOAL, "--sentinel-set-at", ECHO_SET_AT,
         "--producer", spark_fire_dedup.NONRECURRING_PRODUCER,
         "--diary-file", diary], ECHO_MAP)
    assert out == "fire"
    assert rc == 0


def test_cli_unreadable_diary_path_still_fires(tmp_path):
    """A missing or unreadable diary is a plumbing fault, and a plumbing fault
    must never suppress a spark."""
    rc, out = _run_dedup_cli(
        ["check", ECHO_GOAL, "--sentinel-set-at", ECHO_SET_AT,
         "--producer", spark_fire_dedup.NONRECURRING_PRODUCER,
         "--diary-file", str(tmp_path / "does-not-exist.jsonl")], ECHO_MAP)
    assert out == "fire"
    assert rc == 0


def test_cli_a_PRESENT_record_stays_authoritative_over_the_diary(tmp_path):
    """The diary is consulted ONLY when the record is absent, and it earns no
    privilege the record lacks.

    THE TIMESTAMPS ARE CHOSEN TO DISCRIMINATE, and an earlier draft of this test
    did not: it put BOTH the stale record and the diary marker outside the
    window, so 'fire' came out either way and the assertion passed identically
    whether the absent-record guard existed or had been deleted (caught by
    mutating the guard to `if True:` — all 83 tests stayed green). Here the
    diary marker is INSIDE the bounded window (set_at - 15 min = 05:06:32) while
    the record is a day old and outside it, so the two signals disagree: correct
    code follows the RECORD and fires; a version that let the diary override
    would skip."""
    diary = _diary(tmp_path, [_spark_row(ECHO_GOAL, "2026-08-05T05:10:00")])
    stale_record = json.dumps({ECHO_GOAL: "2026-08-04T01:00:00"})
    rc, out = _run_dedup_cli(
        ["check", ECHO_GOAL, "--sentinel-set-at", ECHO_SET_AT,
         "--diary-file", diary], stale_record)
    assert out == "fire", "a present record must decide, even when the diary would match"
    assert rc == 0


def test_cli_diary_marker_from_a_PREVIOUS_close_does_not_dedup(tmp_path):
    """A substituted diary timestamp runs through the SAME consumption window a
    real record does, so a marker from a genuine earlier close of a RECURRING
    goal (bounded lookback — no producer field) falls outside it and still
    fires. This is what stops the corroboration becoming a blanket 'ever fired'
    test."""
    diary = _diary(tmp_path, [_spark_row(ECHO_GOAL, "2026-08-04T01:00:00")])
    rc, out = _run_dedup_cli(
        ["check", ECHO_GOAL, "--sentinel-set-at", ECHO_SET_AT,
         "--diary-file", diary], ECHO_MAP)
    assert out == "fire"
    assert rc == 0


def test_omni_19_minute_gap_dedups_under_the_current_producer(tmp_path):
    """REGRESSION PIN for the half of  that  ALREADY FIXED,
    on omni's own measured numbers (g-029-84, ZDS prod, 2026-07-30): fire
    recorded 00:35:29, sentinel set_at 00:54:30 — a 19m01s gap, past the
    15-minute lower bound, which returned 'fire' at the time.

    It dedups now because the NON-recurring producer drops the lower bound
    entirely. Pinned so that fix cannot silently regress, and pinned WITH its
    control below: the same input without the producer field still fires, which
    is correct — recurring-close.sh's lookback models a bounded script runtime,
    and a pre-g-115-3351 sentinel (which is the shape omni actually observed)
    carries no producer at all."""
    omni_map = json.dumps({"g-001-216": "2026-07-29T23:07:53",
                           "g-029-84": "2026-07-30T00:35:29"})
    _, with_producer = _run_dedup_cli(
        ["check", "g-029-84", "--sentinel-set-at", "2026-07-30T00:54:30",
         "--producer", spark_fire_dedup.NONRECURRING_PRODUCER], omni_map)
    _, without_producer = _run_dedup_cli(
        ["check", "g-029-84", "--sentinel-set-at", "2026-07-30T00:54:30"], omni_map)
    assert with_producer == "skip", "g-115-3351's unbounded lookback must still hold"
    assert without_producer == "fire", "the bounded recurring path must be unchanged"


def test_phase_0_5c_2_passes_diary_file_to_the_check():
    """WIRING (guard-3448 /  class: a gate is only as broad as its
    entry points). The corroboration is inert unless the one live consumer
    actually passes the flag, and a passing unit test says nothing about that."""
    body = (Path(__file__).resolve().parents[3]
            / ".claude" / "skills" / "aspirations" / "SKILL.md").read_text(encoding="utf-8")
    idx = body.find("spark-fire-dedup.py check")
    assert idx != -1, "Phase -0.5c.2 must call spark-fire-dedup.py check"
    invocation = body[idx:idx + 400]
    assert "--diary-file" in invocation, \
        "Phase -0.5c.2 must pass --diary-file so an ABSENT record can be corroborated (g-115-4201)"
    assert "execution-diary.jsonl" in invocation, \
        "the --diary-file argument must name the execution diary"


if __name__ == "__main__":
    sys.exit(0 if not subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]) else 1)
