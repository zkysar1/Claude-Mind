#!/usr/bin/env python3
"""test_precheck_hypothesis_health_rne_gate.py — cmd_hypothesis_health() must
honour an explicit future `resolves_no_earlier_than` (g-115-5301).

THE DEFECT THIS PINS. cmd_hypothesis_health built `resolvable_active` purely
from horizon + formed_date + the cognitive-horizons re_probe windows. It never
read `resolves_no_earlier_than`, so a record carrying an explicit FUTURE gate
was counted resolvable the moment its horizon window elapsed — even though the
pipeline contract forbids resolving it yet.

WHY IT MATTERS MORE THAN AN OFF-BY-ONE. `flowing` = fresh_discovered +
resolvable_active is compared against hypothesis_pipeline_low_water_mark to
raise `stalled_pipeline`. Counting time-gated records INFLATES flowing, so the
check reports healthy while the genuinely-actionable population sits below the
mark. That is a false NEGATIVE on a health check — the failure mode that reads
as coverage (guard-1760 family).

BOTH FIELD SHAPES ARE PINNED, DELIBERATELY. `resolves_no_earlier_than` appears
in live records as date-only ("2026-07-20") AND as full datetime
("2026-07-20T00:00:00", the sq-009 template shape). g-115-2508 exists precisely
because the SIBLING fix in goal-selector initially handled only one shape and
the gate silently no-opped for every template-filed record. A single-shape test
here would reproduce that hole, so each shape gets its own case.

SIBLINGS, not duplicates: g-115-2507 / g-115-2508 time-gated the SAME field in
goal-selector.py. Same field, different consumer — precheck-eval was never
touched by those.
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

_FIXED_NOW = datetime(2026, 6, 13, 12, 0, 0)
CONFIG = {"hypothesis_pipeline_low_water_mark": 1}

_FUTURE_DATE = "2026-06-20"                 # date-only shape, 7d after now
_FUTURE_DATETIME = "2026-06-20T00:00:00"    # datetime shape, same instant
_PAST_DATE = "2026-06-01"
_PAST_DATETIME = "2026-06-01T00:00:00"


class _Args:
    pass


def _write_horizons(meta_dir, short_win=12, long_win=24, fresh_days=7):
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "cognitive-horizons.yaml").write_text(
        "horizons:\n"
        "  micro:\n    re_probe_window_hours: 0\n"
        "  session:\n    re_probe_window_hours: 0\n"
        f"  short:\n    re_probe_window_hours: {short_win}\n"
        f"  long:\n    re_probe_window_hours: {long_win}\n"
        "pipeline_windows:\n"
        f"  fresh_discovered_window_days: {fresh_days}\n",
        encoding="utf-8",
    )
    return meta_dir


def _patch(monkeypatch, tmp_path, active):
    def _fake_query(q, *a, **k):
        if q == "--counts":
            return {}
        if q == "--stage discovered":
            return []
        if q == "--stage active":
            return active
        return None
    monkeypatch.setattr(pe, "_pipeline_query", _fake_query)
    monkeypatch.setattr(pe, "_now", lambda: _FIXED_NOW)
    _write_horizons(tmp_path)
    monkeypatch.setattr(pe, "META_DIR", tmp_path)


def _run(monkeypatch, tmp_path, active):
    _patch(monkeypatch, tmp_path, active)
    return pe.cmd_hypothesis_health(_Args(), CONFIG, None)


# ── the gate fires, in BOTH field shapes ───────────────────────────────────
# `session` horizon is used deliberately: the pre-fix code appended it to
# resolvable_active UNCONDITIONALLY, with no date arithmetic at all. So these
# two cases also prove the new guard runs BEFORE the horizon branch rather
# than merely alongside it.

def test_future_rne_date_only_is_time_gated(tmp_path, monkeypatch):
    r = _run(monkeypatch, tmp_path, [
        {"horizon": "session", "formed_date": "2026-06-01T00:00:00",
         "resolves_no_earlier_than": _FUTURE_DATE},
    ])
    assert r["resolvable_active"] == 0
    assert r["time_gated_active"] == 1


def test_future_rne_datetime_is_time_gated(tmp_path, monkeypatch):
    # The  shape. A parser that only accepts date-only silently
    # no-ops here and this case goes red.
    r = _run(monkeypatch, tmp_path, [
        {"horizon": "session", "formed_date": "2026-06-01T00:00:00",
         "resolves_no_earlier_than": _FUTURE_DATETIME},
    ])
    assert r["resolvable_active"] == 0
    assert r["time_gated_active"] == 1


def test_future_rne_gates_an_elapsed_short_horizon(tmp_path, monkeypatch):
    # The goal's stated VERIFY: elapsed horizon window + future RNE -> gated.
    formed = (_FIXED_NOW - timedelta(hours=48)).isoformat()
    r = _run(monkeypatch, tmp_path, [
        {"horizon": "short", "formed_date": formed,
         "resolves_no_earlier_than": _FUTURE_DATETIME},
    ])
    assert r["resolvable_active"] == 0
    assert r["time_gated_active"] == 1


# ── the gate does NOT over-fire ────────────────────────────────────────────

def test_past_rne_does_not_gate(tmp_path, monkeypatch):
    for shape in (_PAST_DATE, _PAST_DATETIME):
        r = _run(monkeypatch, tmp_path, [
            {"horizon": "session", "formed_date": "2026-06-01T00:00:00",
             "resolves_no_earlier_than": shape},
        ])
        assert r["resolvable_active"] == 1, f"past RNE {shape!r} wrongly gated"
        assert r["time_gated_active"] == 0


def test_absent_rne_preserves_prior_behaviour(tmp_path, monkeypatch):
    r = _run(monkeypatch, tmp_path, [
        {"horizon": "session", "formed_date": "2026-06-01T00:00:00"},
        {"horizon": "short", "formed_date": (_FIXED_NOW - timedelta(hours=1)).isoformat()},
    ])
    assert r["resolvable_active"] == 1   # session resolvable, short still gated
    assert r["time_gated_active"] == 1


def test_unparseable_rne_falls_through_to_horizon(tmp_path, monkeypatch):
    # Fail-OPEN on a malformed value: an unreadable gate must not silently
    # suppress a record from the actionable population. _parse_iso returns
    # None, so classification falls through to the horizon branch unchanged.
    r = _run(monkeypatch, tmp_path, [
        {"horizon": "session", "formed_date": "2026-06-01T00:00:00",
         "resolves_no_earlier_than": "not-a-date"},
    ])
    assert r["resolvable_active"] == 1
    assert r["time_gated_active"] == 0


# ── the consequence the goal was filed about ───────────────────────────────

def test_flowing_count_and_stalled_flag_reflect_the_gate(tmp_path, monkeypatch):
    # Two records, both with elapsed horizons; one carries a future RNE.
    # Pre-fix both counted -> flowing=2 >= lwm=1 -> "healthy".
    # Post-fix only one counts -> flowing=1. Raise the mark to 2 to show the
    # stall is now VISIBLE where it was previously masked.
    active = [
        {"horizon": "session", "formed_date": "2026-06-01T00:00:00"},
        {"horizon": "session", "formed_date": "2026-06-01T00:00:00",
         "resolves_no_earlier_than": _FUTURE_DATETIME},
    ]
    r = _run(monkeypatch, tmp_path, active)
    assert r["flowing_count"] == 1
    assert r["resolvable_active"] == 1
    assert r["time_gated_active"] == 1
    assert r["flags"] == []              # lwm=1, flowing=1 -> not stalled

    r2 = pe.cmd_hypothesis_health(
        _Args(), {"hypothesis_pipeline_low_water_mark": 2}, None)
    assert r2["flowing_count"] == 1
    assert r2["flags"] == ["stalled_pipeline"]
