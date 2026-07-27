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
