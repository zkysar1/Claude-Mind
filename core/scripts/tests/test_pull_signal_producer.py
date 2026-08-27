"""test_pull_signal_producer.py —  item (1), PRODUCER half (2026-08-23).

The consumer (`goal-selector.py apply_pull_boost`) shipped 2026-08-17 with 20
green pins and was INERT for six days, because a boost that requires a
`pull_signal` dict does nothing when nothing writes one. These tests pin the
writer, and — more importantly — the JOIN between the two.

WHY THE JOIN TESTS ARE THE POINT (guard-3221, guard-4065). Both sides of this
wire were individually correct the whole time and still did nothing together.
guard-3221 mandates a test that calls the REAL producer and asserts the marker
appears, WITH a negative control asserting it does not appear when the upstream
value is absent — otherwise the assertion passes against any non-empty string.
guard-4065 is the same defect from the other end: a value that is validated,
required and range-checked on the producer side, then read from a DIFFERENT
object by the consumer, so every observation that appears to be about it is
worthless. So the tests below do not stop at "decide() returned SET": they take
decide()'s ACTUAL output string, round-trip it through the store's own
`parse_value`, and feed the result to the REAL `apply_pull_boost`.

THE DEFECT THAT MADE THE FEATURE UNWRITABLE, pinned here so it cannot return:
`pull_signal` was absent from `_goal_fields.py::GOAL_KNOWN_FIELDS`, so every
write was refused by the goal-field-allowlist gate. That allowlist was derived
2026-08-18 from a census of keys OBSERVED on live goals — and a read-only field
whose writer has not shipped yet is structurally invisible to such a census.

Pure-function tests plus source-level wiring greps: no daemon, no world writes,
no subprocess, safe under any STORAGE_BACKEND.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent   # core/scripts -> core -> repo root
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import pull_signal_producer as prod          # noqa: E402
from _goal_fields import is_known             # noqa: E402

gs = _load("goal_selector_for_producer", "goal-selector.py")
asp = _load("aspirations_for_producer", "aspirations.py")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


NOW = datetime(2026, 8, 23, 12, 0, 0)
MAX_AGE = 24.0
CONSUMER_CFG = {"enabled": True, "boost": 4.0, "max_age_hours": MAX_AGE}


def _goal(pull_signal=..., gid="g-306-284"):
    g = {"id": gid, "status": "pending", "recurring": True}
    if pull_signal is not ...:
        g["pull_signal"] = pull_signal
    return g


def _sig(hours_ago):
    return {
        "set_at": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S"),
        "by": "alpha/cc-08",
        "reason": "carrier ref abc1234, 3 framework file(s)",
    }


def _decide(goal, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("max_age_hours", MAX_AGE)
    kw.setdefault("reason", "carrier ref abc1234, 3 framework file(s)")
    kw.setdefault("by", "alpha/cc-08")
    return prod.decide(goal, **kw)


# --- 1. the allowlist defect that made every write impossible ---------------

def test_pull_signal_is_a_registered_goal_field():
    """Without this the producer is refused by the goal-field-allowlist gate.

    This is not a schema nicety: it is the single fact that kept the shipped
    consumer inert. Measured 2026-08-23 before the fix, an
    `aspirations-update-goal.sh <id> pull_signal '{...}'` returned
    {"error": "unknown_goal_field", "gate": "goal-field-allowlist"}.
    """
    assert is_known("pull_signal") is True


def test_allowlist_still_refuses_an_unregistered_name():
    """Negative control: the gate is not simply accepting everything now."""
    assert is_known("pull_signal_typo") is False
    assert is_known("pull_boost") is False   # the CONFIG block's name, not a goal field


# --- 2. decide(): the pure branches -----------------------------------------

def test_absent_signal_is_stamped():
    verdict, value = _decide(_goal())
    assert verdict == prod.VERDICT_SET
    assert json.loads(value)["reason"] == "carrier ref abc1234, 3 framework file(s)"


def test_explicit_null_signal_is_stamped():
    """The CLEAR writes null, so the next legitimate pull must still fire."""
    verdict, _ = _decide(_goal(pull_signal=None))
    assert verdict == prod.VERDICT_SET


def test_live_signal_is_not_restamped():
    """Idempotence (rb-662). Both lanes fire on a cadence; re-writing a signal
    the consumer already honours changes no ranking and only adds contention on
    a shared world-store field."""
    verdict, value = _decide(_goal(pull_signal=_sig(hours_ago=0.5)))
    assert verdict == prod.VERDICT_SKIP_LIVE
    assert value is None


def test_aged_out_signal_is_restamped():
    """Past max_age the consumer has stopped honouring it, so an outstanding
    dependency SHOULD pull again."""
    verdict, _ = _decide(_goal(pull_signal=_sig(hours_ago=MAX_AGE + 0.1)))
    assert verdict == prod.VERDICT_SET


def test_signal_just_inside_the_window_is_still_live():
    verdict, _ = _decide(_goal(pull_signal=_sig(hours_ago=MAX_AGE - 0.1)))
    assert verdict == prod.VERDICT_SKIP_LIVE


def test_small_clock_skew_is_treated_as_live():
    """The signal is written on the producer's box and read on the consumer's.
    A producer seconds ahead of the reader must not be re-stamped every cycle."""
    verdict, _ = _decide(_goal(pull_signal=_sig(hours_ago=-0.5)))
    assert verdict == prod.VERDICT_SKIP_LIVE


def test_far_future_signal_is_bogus_and_gets_restamped():
    """Beyond the skew tolerance a stamp is not credible; re-stamp rather than
    honour it, mirroring the consumer, which refuses to boost on it."""
    verdict, _ = _decide(_goal(pull_signal=_sig(hours_ago=-5.0)))
    assert verdict == prod.VERDICT_SET


def test_malformed_signals_do_not_raise_and_are_restamped():
    for bad in ("a string", 42, [], {}, {"set_at": None}, {"set_at": "not-a-date"}):
        verdict, _ = _decide(_goal(pull_signal=bad))
        assert verdict == prod.VERDICT_SET, f"{bad!r} should fail open to SET"


def test_missing_goal_is_its_own_verdict():
    verdict, value = _decide(None)
    assert verdict == prod.VERDICT_SKIP_NO_GOAL
    assert value is None


def test_clear_emits_null_never_a_key_removal():
    """Measured against coordination_merge._merge_goal: a clear by REMOVING the
    key is RESURRECTED by the cross-box merge even when the clearer is strictly
    newer. The literal 'null' is the whole safety property."""
    verdict, value = _decide(_goal(pull_signal=_sig(0.5)), clear=True)
    assert verdict == prod.VERDICT_CLEARED
    assert value == "null"


def test_clear_fires_even_on_a_live_signal():
    """The idempotence guard must not swallow an explicit clear."""
    verdict, _ = _decide(_goal(pull_signal=_sig(hours_ago=0.01)), clear=True)
    assert verdict == prod.VERDICT_CLEARED


def test_reason_is_collapsed_and_bounded():
    verdict, value = _decide(_goal(), reason="  a\n\n  b   c  " + "x" * 400)
    payload = json.loads(value)
    assert payload["reason"].startswith("a b c")
    assert len(payload["reason"]) <= 200


# --- 3. THE JOIN: producer output -> store parse -> REAL consumer ------------

def _entry(pull_signal, score=5.0):
    return {"goal_id": "g-306-284", "aspiration_id": "asp-306", "score": score,
            "recurring": True, "breakdown": {}, "raw": {"priority": 2},
            "pull_signal": pull_signal}


def test_producer_output_survives_the_store_parse_as_a_DICT():
    """guard-4065: the consumer requires isinstance(sig, dict). The producer
    hands `aspirations-update-goal.sh` a STRING, which the store turns back into
    a value via parse_value. If that round-trip yielded a string, the consumer's
    isinstance check would fail and the boost would never fire — producer and
    consumer individually correct, jointly inert."""
    _, value = _decide(_goal())
    parsed = asp.parse_value(value)
    assert isinstance(parsed, dict), f"round-trip produced {type(parsed).__name__}"
    assert isinstance(parsed.get("set_at"), str)


def test_the_clear_round_trips_to_None_not_the_string_null():
    assert asp.parse_value("null") is None


def test_END_TO_END_producer_output_makes_the_real_consumer_boost():
    """THE test this whole change exists to make pass.

    Real producer decision -> real store parse -> real apply_pull_boost.
    Asserts the score rises by exactly the configured boost.
    """
    _, value = _decide(_goal())
    parsed = asp.parse_value(value)
    scored = [_entry(parsed, score=5.0)]
    gs.apply_pull_boost(scored, CONSUMER_CFG)
    assert scored[0]["score"] == 9.0
    assert scored[0]["breakdown"]["pull_boost"] == 4.0
    assert "pull_signal_age_hours" in scored[0]["raw"]


def test_END_TO_END_negative_control_no_producer_run_means_no_boost():
    """guard-3221's mandated control: without the producer's marker the SAME
    consumer call must leave the score untouched. Without this the positive test
    above would pass against any non-empty value."""
    scored = [_entry(None, score=5.0)]
    gs.apply_pull_boost(scored, CONSUMER_CFG)
    assert scored[0]["score"] == 5.0
    assert "pull_boost" not in scored[0]["breakdown"]


def test_END_TO_END_the_clear_removes_the_boost():
    """Producer SET -> consumer boosts; producer CLEAR -> consumer does not."""
    _, set_value = _decide(_goal())
    boosted = [_entry(asp.parse_value(set_value), score=5.0)]
    gs.apply_pull_boost(boosted, CONSUMER_CFG)
    assert boosted[0]["score"] == 9.0

    _, clear_value = _decide(_goal(pull_signal=_sig(0.5)), clear=True)
    cleared = [_entry(asp.parse_value(clear_value), score=5.0)]
    gs.apply_pull_boost(cleared, CONSUMER_CFG)
    assert cleared[0]["score"] == 5.0


def test_producer_and_consumer_agree_on_liveness():
    """The producer's SKIP-live boundary must be exactly the consumer's boost
    boundary. If they drift, the producer either skips writes the consumer would
    have ignored, or re-writes signals it already honours."""
    for hours_ago in (0.0, 1.0, 12.0, MAX_AGE - 0.01, MAX_AGE + 0.01, 48.0, -0.5, -5.0):
        sig = _sig(hours_ago)
        producer_says_live = prod.is_live(sig, NOW, MAX_AGE)
        scored = [_entry(sig, score=5.0)]
        # apply_pull_boost uses datetime.now(); NOW is fixed, so compare against
        # a signal expressed relative to the real clock instead.
        sig_real = {**sig, "set_at": (datetime.now() - timedelta(hours=hours_ago))
                    .strftime("%Y-%m-%dT%H:%M:%S")}
        scored = [_entry(sig_real, score=5.0)]
        gs.apply_pull_boost(scored, CONSUMER_CFG)
        consumer_boosted = scored[0]["score"] > 5.0
        assert producer_says_live == consumer_boosted, (
            f"disagreement at {hours_ago}h: producer live={producer_says_live}, "
            f"consumer boosted={consumer_boosted}")


# --- 4. wiring: the coupling is a cross-file dependency, so pin it ----------

def test_worker_lane_is_wired_into_the_carrier_push_success_path():
    """guard-3221 'pin the coupling'. The producer is invoked by name from
    another file; nothing but a test notices when that call is removed."""
    src = (CORE_SCRIPTS / "iteration-push.sh").read_text(encoding="utf-8")
    assert "pull-signal-set.sh" in src
    assert "--if-carrier-content" in src


def test_reducer_lane_is_wired_into_worker_ref_consume_check():
    src = (CORE_SCRIPTS / "worker-ref-consume.sh").read_text(encoding="utf-8")
    assert "pull-signal-set.sh" in src
    assert 'DO_CHECK" = 1 ] && [ "$pull_tip_count" -gt 0' in src


def test_clear_is_wired_into_recurring_close():
    src = (CORE_SCRIPTS / "recurring-close.sh").read_text(encoding="utf-8")
    assert "has_pull_signal" in src
    assert '"pull_signal", "null"' in src


def test_clear_in_recurring_close_is_gated_so_it_cannot_stamp_every_goal():
    """recurring-close runs on EVERY recurring goal. An ungated clear would put
    `pull_signal: null` on every recurring record in the store, destroying the
    'absence is the no-op path' property the consumer's no-regression argument
    rests on."""
    src = (CORE_SCRIPTS / "recurring-close.sh").read_text(encoding="utf-8")
    assert "if has_pull_signal:" in src


def test_consumer_goal_id_has_exactly_one_home():
    """The id lives in config, not hardcoded in either producer."""
    import yaml
    cfg = yaml.safe_load((PROJECT_ROOT / "core/config/aspirations.yaml")
                         .read_text(encoding="utf-8"))["pull_boost"]
    assert cfg["carrier_consumer_goal"] == "g-306-284"
    # Neither producer may PASS the id — they must fall through to the config
    # default. Note the predicate tests the --goal ARGUMENT, not the bare
    # string: worker-ref-consume.sh legitimately CITES  in a comment
    # about the sync-merge drain, and a bare substring test would conflate
    # "mentions the goal" with "hardcodes the config value".
    import re
    for f in ("iteration-push.sh", "worker-ref-consume.sh"):
        src = (CORE_SCRIPTS / f).read_text(encoding="utf-8")
        assert not re.search(r'--goal\s+"?g-\d+', src), \
            f"{f} passes a literal --goal; the consumer id must come from config"
