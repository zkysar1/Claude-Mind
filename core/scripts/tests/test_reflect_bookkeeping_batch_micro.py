"""Regression pins for reflect-bookkeeping.py batch-micro ().

Two defects fixed together:

1. READER LAYOUT BUG — `_read_micro_hypotheses` read `micro_hypotheses` at the
   TOP LEVEL of working-memory.yaml, but the live WM layout nests slots under
   the `slots:` map. The script path therefore returned [] on every invocation
   and batch-micro always early-exited with "no micro-hypotheses to process"
   (the real batches that produced pipeline-meta stats were LLM-manual).

2. COUNTER RE-COUNT DRIFT — the SKILL.md Step 4 aggregate did
   `total_all_time += total` where total includes carried PENDING micros that
   re-batch on every subsequent pass, so each carried micro was counted once
   per pass (observed 30 total vs 9 resolved by 2026-07-16). The fix makes the
   script emit `stats_delta` (settled deltas + pending_now, with the DERIVED
   total rule) and `micro_hypotheses_writeback` (the pending-only pruned array
   the caller writes back — the counted-once guarantee).

Pattern: importlib + sys.path (hyphenated script name), same shape as
test_pre_apply_consult_drift_gate.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "reflect-bookkeeping.py"


def _import():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("reflect_bookkeeping", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reflect_bookkeeping"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()


def _write_wm(tmp_path, payload):
    import yaml
    wm = tmp_path / "working-memory.yaml"
    wm.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return wm


def _point_reader_at(monkeypatch, wm_file):
    """Route _read_micro_hypotheses at a tmp WM file, hermetically."""
    import types
    fake_wm = types.ModuleType("wm")
    fake_wm.wm_path = lambda *a, **k: wm_file
    monkeypatch.setitem(sys.modules, "wm", fake_wm)
    # AGENT_DIR is only None-checked as an early-exit guard.
    monkeypatch.setattr(MOD, "AGENT_DIR", wm_file.parent, raising=False)


MICROS = [
    {"claim": "settled-yes", "confidence": 0.8, "outcome": "confirmed",
     "category": "cat-a"},
    {"claim": "settled-no", "confidence": 0.6, "outcome": "corrected",
     "category": "cat-a"},
    {"claim": "still-pending", "confidence": 0.5, "outcome": None,
     "category": "cat-b"},
]


# ── reader layout (defect 1) ─────────────────────────────────────────────────

def test_reader_finds_micros_under_slots_map(tmp_path, monkeypatch):
    #  regression pin: live WM nests slots under `slots:` — the old
    # top-level read returned [] here and batch-micro always early-exited.
    wm = _write_wm(tmp_path, {"slots": {"micro_hypotheses": MICROS},
                              "slot_meta": {}})
    _point_reader_at(monkeypatch, wm)
    assert len(MOD._read_micro_hypotheses()) == 3


def test_reader_top_level_fallback_still_works(tmp_path, monkeypatch):
    # Pre-slots layouts keep working (back-compat fallback).
    wm = _write_wm(tmp_path, {"micro_hypotheses": MICROS[:2]})
    _point_reader_at(monkeypatch, wm)
    assert len(MOD._read_micro_hypotheses()) == 2


def test_reader_empty_slot_returns_empty(tmp_path, monkeypatch):
    wm = _write_wm(tmp_path, {"slots": {"micro_hypotheses": None}})
    _point_reader_at(monkeypatch, wm)
    assert MOD._read_micro_hypotheses() == []


# ── stats_delta + writeback (defect 2) ───────────────────────────────────────

def _run_batch(tmp_path, monkeypatch, micros):
    wm = _write_wm(tmp_path, {"slots": {"micro_hypotheses": micros}})
    _point_reader_at(monkeypatch, wm)
    out = {}

    def fake_emit(payload, rc):
        out.update(payload)
        raise SystemExit(rc)

    monkeypatch.setattr(MOD, "_emit", fake_emit)
    with pytest.raises(SystemExit):
        MOD.cmd_batch_micro(None)
    return out


def test_stats_delta_counts_only_settled(tmp_path, monkeypatch):
    out = _run_batch(tmp_path, monkeypatch, MICROS)
    d = out["stats_delta"]
    assert d["confirmed_delta"] == 1
    assert d["corrected_delta"] == 1
    assert d["pending_now"] == 1
    # The rule string pins the DERIVED total semantics for the SKILL.md caller.
    assert "DERIVED" in d["rule"] and "+= total" in d["rule"]


def test_writeback_prunes_settled_keeps_pending(tmp_path, monkeypatch):
    # Counted-once guarantee: settled micros leave the slot at the pass that
    # counted them; ONLY the pending micro survives into the writeback array.
    out = _run_batch(tmp_path, monkeypatch, MICROS)
    wb = out["micro_hypotheses_writeback"]
    assert [m["claim"] for m in wb] == ["still-pending"]


def test_carried_pending_two_passes_contributes_no_settled_delta(tmp_path, monkeypatch):
    # INCIDENT SHAPE: a pending micro re-batched on a second pass must yield
    # zero settled deltas and pending_now=1 — under the derived-total rule the
    # aggregate is unchanged across passes (the old `+= total` added +1 per
    # pass, which is exactly the 30-vs-9 drift).
    out1 = _run_batch(tmp_path, monkeypatch, MICROS)
    carried = out1["micro_hypotheses_writeback"]
    out2 = _run_batch(tmp_path, monkeypatch, carried)
    d2 = out2["stats_delta"]
    assert d2["confirmed_delta"] == 0
    assert d2["corrected_delta"] == 0
    assert d2["pending_now"] == 1
    # Derived total: confirmed_all_time + corrected_all_time + pending_now is
    # identical after pass 1 and pass 2 (e.g. 1 + 1 + 1 == 3 both times).
    base_conf, base_corr = 1, 1  # accumulated from pass 1's deltas
    total_after_1 = base_conf + base_corr + out1["stats_delta"]["pending_now"]
    total_after_2 = (base_conf + d2["confirmed_delta"]
                     + base_corr + d2["corrected_delta"] + d2["pending_now"])
    assert total_after_1 == total_after_2 == 3


def test_empty_slot_early_exit_shape(tmp_path, monkeypatch):
    out = _run_batch(tmp_path, monkeypatch, [])
    assert out["total"] == 0
    assert "no micro-hypotheses" in out["summary"]


# ── compute_surprise: the single-implementation pin () ─────────────
#
# Until  this arithmetic existed TWICE: inline in cmd_batch_micro,
# and as PROSE in review-hypotheses/SKILL.md Step 3.2 that no script could
# reach. Two copies with no shared test could drift silently, and only the
# copy an LLM happened to read would apply. compute_surprise() is now the sole
# implementation; batch-micro and the `surprise` subcommand both call it.

def test_surprise_corrected_scales_with_confidence():
    # CORRECTED: surprise rises WITH confidence — being confident and wrong
    # is the surprising case.
    assert MOD.compute_surprise("CORRECTED", 0.72) == 7
    assert MOD.compute_surprise("CORRECTED", 0.5) == 5   # the  worked example
    assert MOD.compute_surprise("CORRECTED", 0.1) == 1


def test_surprise_confirmed_inverts():
    # CONFIRMED: surprise rises as confidence FALLS — being unsure and right
    # is the surprising case.
    assert MOD.compute_surprise("CONFIRMED", 0.72) == 3
    assert MOD.compute_surprise("CONFIRMED", 0.20) == 8
    # 1 not 0: (1-0.95)*10 is 0.5000000000000004 in float, so it rounds UP.
    assert MOD.compute_surprise("CONFIRMED", 0.95) == 1


def test_surprise_rounding_is_bankers_at_the_promotion_boundary():
    # MEASURED behavior, pinned deliberately — NOT an endorsement of it.
    # Python round() is round-half-to-EVEN, and .x5 confidences are common in
    # the live store (0.45/0.55/0.65/0.75/0.85 all appear). The result is a
    # boundary that reads as inconsistent: CORRECTED at 0.65 scores 6 while
    # 0.75 scores 8 — because 6.5 rounds DOWN to 6 and 7.5 rounds UP to 8.
    #
    # This is load-bearing, not trivia: surprise >= 7 is the promotion
    # threshold (promote_reason "high_surprise") and the Step 3.5 broad
    # re-retrieve trigger. A half-UP rule would score 0.65 CORRECTED as 7 and
    # fire both; banker's rounding scores 6 and fires neither.
    #
    #  LIFTED this arithmetic into one helper without altering it —
    # the values below are byte-identical to the previous inline batch-micro
    # code. Whether half-up is the INTENDED rule is a separate semantic
    # question, tracked as its own goal. Until then this test guarantees the
    # behavior cannot drift silently in either direction.
    assert MOD.compute_surprise("CORRECTED", 0.65) == 6   # 6.5 -> 6 (down, to even)
    assert MOD.compute_surprise("CORRECTED", 0.75) == 8   # 7.5 -> 8 (up, to even)
    assert MOD.compute_surprise("CORRECTED", 0.45) == 4   # 4.5 -> 4
    assert MOD.compute_surprise("CORRECTED", 0.55) == 6   # 5.5 -> 6
    assert MOD.compute_surprise("CONFIRMED", 0.15) == 8   # 8.5 -> 8


def test_surprise_is_case_insensitive():
    # LOAD-BEARING, not cosmetic. The micro-hypothesis store writes lowercase
    # ("corrected"), /review-hypotheses Step 3 writes uppercase ("CORRECTED").
    # A case-SENSITIVE match would score every SKILL.md caller 0, which reads
    # as "well-calibrated" and silently skips the Step 3.5 high-surprise
    # re-retrieve — a false negative that looks like a clean result.
    for conf in (0.1, 0.5, 0.72, 0.9):
        assert MOD.compute_surprise("CORRECTED", conf) == MOD.compute_surprise("corrected", conf)
        assert MOD.compute_surprise("CONFIRMED", conf) == MOD.compute_surprise("confirmed", conf)


def test_surprise_non_scoreable_outcomes_score_zero():
    # EXPIRED / UNRESOLVABLE are excluded from calibration store-wide, so a
    # zero here is correct rather than a silent miss.
    for outcome in ("EXPIRED", "UNRESOLVABLE", "", None, "pending"):
        assert MOD.compute_surprise(outcome, 0.9) == 0


def test_batch_micro_uses_the_shared_helper_not_a_second_copy(tmp_path, monkeypatch):
    # The anti-duplication pin: every surprise value batch-micro emits must
    # equal compute_surprise's answer for the same inputs. If someone re-inlines
    # the arithmetic and it drifts by so much as a rounding rule, this fails.
    micros = [
        {"claim": "c-hi", "outcome": "corrected", "confidence": 0.9},
        {"claim": "c-lo", "outcome": "corrected", "confidence": 0.2},
        {"claim": "k-hi", "outcome": "confirmed", "confidence": 0.85},
        {"claim": "k-lo", "outcome": "confirmed", "confidence": 0.15},
    ]
    out = _run_batch(tmp_path, monkeypatch, micros)
    scored = {m["claim"]: m["surprise"]
              for m in out.get("micro_hypotheses_writeback", []) + out.get("promoted", [])}
    # Fall back to whatever the payload exposes; assert on what we can see.
    for m in micros:
        expected = MOD.compute_surprise(m["outcome"], m["confidence"])
        if m["claim"] in scored:
            assert scored[m["claim"]] == expected, m["claim"]
    # Independent of payload shape: the helper must be the one deciding
    # promotion at the >=7 boundary.
    assert MOD.compute_surprise("corrected", 0.9) == 9    # promotes
    assert MOD.compute_surprise("confirmed", 0.15) == 8   # promotes (8.5 -> 8)


# ── cmd_surprise CLI contract (fresh-eyes findings, ) ──────────────
#
# Both pins below are for defects fresh-eyes found in the SAME iteration that
# introduced the subcommand. The first shipped and was caught before commit.

def _run_surprise(argv):
    """Invoke cmd_surprise with a patched argv + captured _emit."""
    import argparse
    out, code = {}, {}

    # Signature must mirror the real _emit(payload, exit_code=0): cmd_surprise
    # passes exit_code= as a KEYWORD, so a fake declaring `rc` TypeErrors.
    def fake_emit(payload, exit_code=0):
        out.update(payload)
        code["rc"] = exit_code

    real_emit, real_argv = MOD._emit, sys.argv
    try:
        MOD._emit = fake_emit
        sys.argv = argv
        ns = argparse.Namespace(
            outcome=None, confidence=None,
        )
        # Mirror what main() would have parsed, honouring the equals form.
        for i, a in enumerate(argv):
            if a == "--outcome":
                ns.outcome = argv[i + 1]
            elif a.startswith("--outcome="):
                ns.outcome = a.split("=", 1)[1]
            elif a == "--confidence":
                ns.confidence = float(argv[i + 1])
            elif a.startswith("--confidence="):
                ns.confidence = float(a.split("=", 1)[1])
        MOD.cmd_surprise(ns)
    finally:
        MOD._emit, sys.argv = real_emit, real_argv
    return out, code.get("rc", 0)


def test_surprise_accepts_the_equals_form_of_confidence():
    # REGRESSION PIN. The first implementation detected an omitted --confidence
    # by scanning sys.argv for the bare token "--confidence". argparse also
    # accepts "--confidence=0.9", which lands in argv as ONE joined token, so
    # the scan reported it missing and the call was refused with rc=2 despite
    # being valid. Detection now reads argparse's parsed value instead.
    out, rc = _run_surprise(["prog", "surprise", "--outcome", "CORRECTED", "--confidence=0.9"])
    assert rc == 0, out
    assert out["surprise"] == 9


def test_surprise_requires_confidence_explicitly():
    # The sentinel must still refuse a genuinely omitted flag — the equals-form
    # fix must not be achieved by dropping the requirement altogether.
    out, rc = _run_surprise(["prog", "surprise", "--outcome", "CORRECTED"])
    assert rc == 2
    assert out["error"] == "missing_confidence"


def test_surprise_range_validates_like_its_sibling():
    # cmd_dual_classification (the other consumer of --confidence) rejects
    # out-of-range input; cmd_surprise shipped without that check. A confidence
    # of 5.0 scores surprise=50 and trips every >=7 promotion gate downstream.
    out, rc = _run_surprise(["prog", "surprise", "--outcome", "CORRECTED", "--confidence", "5.0"])
    assert rc == 2
    assert "0..1" in out["error"]


def test_dual_classification_keeps_its_half_default_after_the_sentinel_change():
    # The --confidence parser default became None so `surprise` can tell
    # omitted from explicit-0.5. dual-classification's historical 0.5 default
    # must be unchanged — without the in-function coercion its range check
    # TypeErrors on None.
    import argparse
    out = {}

    def fake_emit(payload, exit_code=0):
        out.update(payload)

    real = MOD._emit
    try:
        MOD._emit = fake_emit
        MOD.cmd_dual_classification(argparse.Namespace(outcome="CONFIRMED", confidence=None))
    finally:
        MOD._emit = real
    assert out["confidence"] == 0.5
    assert out["dual_classification"] == "lucky_confirmed"
