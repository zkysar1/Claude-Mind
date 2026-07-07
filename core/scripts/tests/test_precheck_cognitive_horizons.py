#!/usr/bin/env python3
"""test_precheck_cognitive_horizons.py — precheck-eval.py cognitive-horizons SSOT
contract (BRD Gap 19 / g-306-02).

Pins the single-source consumption of meta/cognitive-horizons.yaml:

  - _load_cognitive_horizons() reads the yaml and returns the horizon windows.
    Fail-loud, no hardcoded fallback (guard-424 + precheck-eval constraint #4):
    RuntimeError if META_DIR is unresolved, FileNotFoundError if the yaml is
    absent.
  - cmd_hypothesis_health() CONSUMES those windows (short/long re_probe hours +
    the fresh-discovered day window) instead of re-hardcoding the
    timedelta(hours=12|24) / <=7 literals it used before consolidation. The
    consumption tests vary the fixture yaml's windows and assert the
    resolvable/fresh classification moves with them — proving the value flows
    from the yaml at call time, not from a constant.

META_DIR, _pipeline_query, and _now are module globals imported/defined in
precheck-eval.py; the tests monkeypatch them so the reader targets a controlled
fixture yaml and a deterministic clock rather than live state. This file is the
READER proof for rb-335 (writer-without-reader): the SSOT yaml is consumed, not
merely written.
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

# Fixed clock so time-gating is deterministic regardless of wall-clock.
_FIXED_NOW = datetime(2026, 6, 13, 12, 0, 0)

CONFIG = {"hypothesis_pipeline_low_water_mark": 1}


class _Args:
    pass


def _write_horizons(meta_dir, short_win=12, long_win=24, fresh_days=7,
                    micro_win=0, session_win=0):
    """Write a minimal cognitive-horizons.yaml fixture into meta_dir."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "cognitive-horizons.yaml").write_text(
        "horizons:\n"
        "  micro:\n"
        f"    re_probe_window_hours: {micro_win}\n"
        "  session:\n"
        f"    re_probe_window_hours: {session_win}\n"
        "  short:\n"
        f"    re_probe_window_hours: {short_win}\n"
        "  long:\n"
        f"    re_probe_window_hours: {long_win}\n"
        "pipeline_windows:\n"
        f"  fresh_discovered_window_days: {fresh_days}\n",
        encoding="utf-8",
    )
    return meta_dir


def _patch_pipeline(monkeypatch, discovered, active):
    """Stub _pipeline_query (no shell-out) + pin the clock to _FIXED_NOW."""
    def _fake_query(q, *a, **k):
        if q == "--counts":
            return {}
        if q == "--stage discovered":
            return discovered
        if q == "--stage active":
            return active
        return None
    monkeypatch.setattr(pe, "_pipeline_query", _fake_query)
    monkeypatch.setattr(pe, "_now", lambda: _FIXED_NOW)


# ── _load_cognitive_horizons() ─────────────────────────────────────────────

def test_load_returns_horizon_windows(tmp_path, monkeypatch):
    _write_horizons(tmp_path)
    monkeypatch.setattr(pe, "META_DIR", tmp_path)
    ch = pe._load_cognitive_horizons()
    assert ch["horizons"]["short"]["re_probe_window_hours"] == 12
    assert ch["horizons"]["long"]["re_probe_window_hours"] == 24
    assert ch["horizons"]["micro"]["re_probe_window_hours"] == 0
    assert ch["horizons"]["session"]["re_probe_window_hours"] == 0
    assert ch["pipeline_windows"]["fresh_discovered_window_days"] == 7


def test_load_missing_file_raises(tmp_path, monkeypatch):
    # META_DIR resolves but the yaml is absent -> fail loud, no fallback.
    monkeypatch.setattr(pe, "META_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        pe._load_cognitive_horizons()


def test_load_meta_dir_none_raises(monkeypatch):
    monkeypatch.setattr(pe, "META_DIR", None)
    with pytest.raises(RuntimeError):
        pe._load_cognitive_horizons()


# ── cmd_hypothesis_health() consumes the yaml windows ──────────────────────

def test_short_window_consumed_from_yaml(tmp_path, monkeypatch):
    # A short-horizon active hypo formed 13h before the fixed now.
    formed = (_FIXED_NOW - timedelta(hours=13)).isoformat()
    _patch_pipeline(monkeypatch, discovered=[],
                    active=[{"horizon": "short", "formed_date": formed}])
    monkeypatch.setattr(pe, "META_DIR", tmp_path)

    # short_win=12 -> formed+12h (11:00) <= now (12:00) -> RESOLVABLE.
    _write_horizons(tmp_path, short_win=12)
    r = pe.cmd_hypothesis_health(_Args(), CONFIG, None)
    assert r["resolvable_active"] == 1

    # short_win=24 -> formed+24h (23:00) > now -> TIME-GATED. Same hypo, same
    # clock; only the yaml window changed -> classification flips. Proves the
    # window is read from the yaml, not a hardcoded 12.
    _write_horizons(tmp_path, short_win=24)
    r = pe.cmd_hypothesis_health(_Args(), CONFIG, None)
    assert r["resolvable_active"] == 0


def test_long_window_consumed_from_yaml(tmp_path, monkeypatch):
    formed = (_FIXED_NOW - timedelta(hours=20)).isoformat()
    _patch_pipeline(monkeypatch, discovered=[],
                    active=[{"horizon": "long", "formed_date": formed}])
    monkeypatch.setattr(pe, "META_DIR", tmp_path)

    _write_horizons(tmp_path, long_win=24)
    r = pe.cmd_hypothesis_health(_Args(), CONFIG, None)
    assert r["resolvable_active"] == 0  # 20h < 24h gate

    _write_horizons(tmp_path, long_win=12)
    r = pe.cmd_hypothesis_health(_Args(), CONFIG, None)
    assert r["resolvable_active"] == 1  # 20h >= 12h gate


def test_fresh_window_consumed_from_yaml(tmp_path, monkeypatch):
    # A discovered hypo formed 8 days before the fixed now.
    formed = (_FIXED_NOW - timedelta(days=8)).isoformat()
    _patch_pipeline(monkeypatch, discovered=[{"formed_date": formed}], active=[])
    monkeypatch.setattr(pe, "META_DIR", tmp_path)

    _write_horizons(tmp_path, fresh_days=7)
    r = pe.cmd_hypothesis_health(_Args(), CONFIG, None)
    assert r["fresh_discovered"] == 0  # 8d > 7d window

    _write_horizons(tmp_path, fresh_days=14)
    r = pe.cmd_hypothesis_health(_Args(), CONFIG, None)
    assert r["fresh_discovered"] == 1  # 8d <= 14d window


def test_session_and_micro_always_resolvable(tmp_path, monkeypatch):
    # session/micro horizons resolve immediately (re_probe=0) regardless of the
    # short/long windows.
    active = [{"horizon": "session", "formed_date": _FIXED_NOW.isoformat()},
              {"horizon": "micro", "formed_date": _FIXED_NOW.isoformat()}]
    _patch_pipeline(monkeypatch, discovered=[], active=active)
    _write_horizons(tmp_path)
    monkeypatch.setattr(pe, "META_DIR", tmp_path)
    r = pe.cmd_hypothesis_health(_Args(), CONFIG, None)
    assert r["resolvable_active"] == 2


def test_hypothesis_health_missing_yaml_raises(tmp_path, monkeypatch):
    # The SSOT consumer fails loud when the yaml is absent — the whole point of
    #  is no hardcoded fallback (guard-424 + constraint #4).
    _patch_pipeline(monkeypatch, discovered=[],
                    active=[{"horizon": "short", "formed_date": _FIXED_NOW.isoformat()}])
    monkeypatch.setattr(pe, "META_DIR", tmp_path)  # empty dir, no yaml
    with pytest.raises(FileNotFoundError):
        pe.cmd_hypothesis_health(_Args(), CONFIG, None)


# ── init-seed parity (echo-2744 audit / ) ──────────────────────────

def test_init_meta_seeds_cognitive_horizons():
    """The consumer above fails loud when meta/cognitive-horizons.yaml is
    absent, so init-meta.sh MUST seed it — else fresh-box init FileNotFoundErrors
    (the ~3wk fleet-wide FNF since 2026-06-13 was this exact missing seed).

    This is the init-seed-COVERAGE assertion the fixture tests above cannot
    make: a self-written fixture proves the CONSUMER reads the yaml, NOT that a
    fresh clone HAS it (rb init-seed-parity). The two are separate assertions —
    the whole point of echo's finding msg-20260703-093826-echo-2744.
    """
    core = SCRIPT_DIR.parent  # core/
    # 1. The git-tracked seed SOURCE exists.
    src = core / "config" / "cognitive-horizons.yaml"
    assert src.exists(), f"seed source missing: {src}"
    # 2. init-meta.sh actually copies it into meta/ (cp from $CONFIG to $META).
    body = (SCRIPT_DIR / "init-meta.sh").read_text(encoding="utf-8")
    assert 'cp "$CONFIG/cognitive-horizons.yaml" "$META/cognitive-horizons.yaml"' in body, (
        "init-meta.sh does not seed cognitive-horizons.yaml — fresh-box init "
        "would FileNotFoundError in precheck-eval hypothesis-health"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
