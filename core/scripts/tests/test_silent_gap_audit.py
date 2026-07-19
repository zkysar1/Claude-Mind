"""test_silent_gap_audit.py — silent-gap-audit.py regression ().

Exercises the PURE logic of the silent-gap / orphaned-asset audit: the two
load-bearing suppression gates (rb-245 zero-count verification + dedup-against-
open-goals) plus each detector's classification. No daemon, no network — the
daemon read (_read_goals) and the filesystem globs are the only impure parts and
are not exercised here; everything below takes its inputs by injection.

Pattern: importlib-load the module, call the pure functions with synthetic data.
Real asserts (a bool-returning test passes vacuously under pytest). tempfile (NOT
/tmp — guard-759) for the one helper that reads a file.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import datetime as dt
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

MOD_PATH = CORE_SCRIPTS / "silent-gap-audit.py"
spec = importlib.util.spec_from_file_location("silent_gap_audit", MOD_PATH)
sga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sga)


# ---- is_covered (dedup primary-token match) ----

def test_is_covered_primary_token_match():
    corpus = [("g-1", "investigate ohs-trend staleness in eval"), ("g-2", "unrelated work")]
    covered, gid = sga.is_covered(["ohs-trend", "ohs"], corpus)
    assert covered is True
    assert gid == "g-1"


def test_is_covered_no_match():
    corpus = [("g-1", "something else entirely"), ("g-2", "another goal")]
    covered, gid = sga.is_covered(["history-shadow-telemetry"], corpus)
    assert covered is False
    assert gid is None


def test_is_covered_empty_tokens():
    covered, gid = sga.is_covered([], [("g-1", "anything")])
    assert covered is False
    assert gid is None


def test_is_covered_token_lowercased():
    # is_covered consumes the ALREADY-lowercased corpus that open_goal_corpus
    # produces (lowercase once — single source of truth); it lowercases only the
    # incoming token. Verify a mixed-case token still matches the lower corpus.
    corpus = [("g-9", "refresh deadline_urgency scoring input")]
    covered, gid = sga.is_covered(["DEADLINE_Urgency"], corpus)
    assert covered is True and gid == "g-9"


# ---- open_goal_corpus (filters terminal, concatenates fields) ----

def test_open_goal_corpus_filters_terminal_and_builds_text():
    goals = [
        {"id": "g-1", "status": "pending", "title": "Fix A", "description": "do thing",
         "origin_signal": "investigate:x"},
        {"id": "g-2", "status": "completed", "title": "Done B", "description": "nope"},
        {"id": "g-3", "status": "in-progress", "title": "Wire C", "description": "", "origin_signal": ""},
    ]
    corpus = sga.open_goal_corpus(goals)
    ids = {gid for gid, _ in corpus}
    assert ids == {"g-1", "g-3"}  # completed g-2 excluded
    text_g1 = dict(corpus)["g-1"]
    assert "fix a" in text_g1 and "do thing" in text_g1 and "investigate:x" in text_g1


# ---- run_pipeline (rb-245 gate, then dedup) ----

def _gap(detector="written-never-read", target="foo.jsonl", rb=True, tokens=None, sev="medium"):
    return {
        "detector": detector, "target": target, "summary": f"{target} summary",
        "evidence": {}, "severity": sev, "dedup_tokens": tokens or [target.split(".")[0]],
        "rb245_passed": rb, "rb245_note": "note",
    }


def test_run_pipeline_rb245_suppressed():
    gaps = [_gap(rb=False, target="bar.jsonl")]
    new, supp_rb, supp_dd = sga.run_pipeline(gaps, corpus=[])
    assert new == []
    assert len(supp_rb) == 1 and supp_rb[0]["target"] == "bar.jsonl"
    assert supp_dd == []


def test_run_pipeline_dedup_suppressed():
    gaps = [_gap(rb=True, target="baz.jsonl", tokens=["baz"])]
    corpus = [("g-7", "already tracking baz cleanup")]
    new, supp_rb, supp_dd = sga.run_pipeline(gaps, corpus)
    assert new == []
    assert supp_rb == []
    assert len(supp_dd) == 1 and supp_dd[0]["covering_goal_id"] == "g-7"


def test_run_pipeline_emits_new_with_suggested():
    gaps = [_gap(rb=True, target="qux.jsonl", tokens=["qux"], sev="medium")]
    new, supp_rb, supp_dd = sga.run_pipeline(gaps, corpus=[("g-1", "unrelated")])
    assert len(new) == 1
    s = new[0]["suggested"]
    assert s["category"] == "framework-architecture"
    assert s["priority"] == "MEDIUM"
    assert s["origin_signal"].startswith("investigate:silent-gap-")
    assert "qux" in s["origin_signal"]


def test_run_pipeline_low_severity_maps_low_priority():
    gaps = [_gap(rb=True, target="lo.jsonl", tokens=["lo-unique-tok"], sev="low")]
    new, _, _ = sga.run_pipeline(gaps, corpus=[])
    assert new[0]["suggested"]["priority"] == "LOW"


# ---- _store_reader_patterns (rb-245 multi-pattern gate for detector a) ----

def test_store_reader_patterns_variants():
    pats = sga._store_reader_patterns("history-shadow-telemetry.jsonl")
    assert "history-shadow-telemetry.jsonl" in pats
    assert "history-shadow-telemetry" in pats
    assert "history_shadow_telemetry" in pats  # snake variant


def test_store_reader_patterns_drops_short_tokens():
    pats = sga._store_reader_patterns("ab.yaml")  # stem "ab" too short
    assert all(len(p) >= 4 for p in pats)


# ---- detect_zero_input (coverage + field-validity rb-245 gate) ----

def test_detect_zero_input_low_coverage_flags():
    specs = (("test mech", ["resolves_by"], "world", 0.10, "note"),)
    world = [{"resolves_by": "2026-11-02"}] + [{"x": 1} for _ in range(99)]  # 1/100 = 1% < 10%
    gaps = sga.detect_zero_input(world, [], specs=specs)
    assert len(gaps) == 1
    assert gaps[0]["rb245_passed"] is True  # field present in >=1 record -> validated
    assert gaps[0]["evidence"]["carriers"] == 1


def test_detect_zero_input_field_unknown_suppressed_rb245():
    # field appears in ZERO records -> ambiguous (misspelled vs real) -> rb-245 suppress
    specs = (("typo mech", ["resolvez_by"], "world", 0.10, "note"),)
    world = [{"resolves_by": "x"} for _ in range(10)]
    gaps = sga.detect_zero_input(world, [], specs=specs)
    assert len(gaps) == 1
    assert gaps[0]["rb245_passed"] is False
    assert "0 records" in gaps[0]["rb245_note"]


def test_detect_zero_input_above_threshold_skips():
    specs = (("ok mech", ["deadline"], "world", 0.10, "note"),)
    world = [{"deadline": "d"} for _ in range(50)] + [{"x": 1} for _ in range(50)]  # 50%
    gaps = sga.detect_zero_input(world, [], specs=specs)
    assert gaps == []


# ---- detect_never_invoked (per-window/situational rb-245 suppression) ----

def test_detect_never_invoked_situational_suppressed():
    gaps = sga.detect_never_invoked(skill_names=["run-processor"], inv_counts={})
    assert len(gaps) == 1
    assert gaps[0]["rb245_passed"] is False
    assert gaps[0]["evidence"]["situational"] is True


def test_detect_never_invoked_nonsituational_still_rb245_suppressed():
    # even a non-situational 0-invocation skill is suppressed: per-window log != lifetime
    gaps = sga.detect_never_invoked(skill_names=["widget-forge"], inv_counts={})
    assert len(gaps) == 1
    assert gaps[0]["rb245_passed"] is False
    assert "per-window" in gaps[0]["rb245_note"]


def test_detect_never_invoked_invoked_skill_skipped():
    gaps = sga.detect_never_invoked(skill_names=["widget-forge"], inv_counts={"widget-forge": 3})
    assert gaps == []


# ---- _last_jsonl_timestamp (rb-245: timestamp from CONTENT not mtime) ----

def test_last_jsonl_timestamp_from_content():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "telem.jsonl"
        p.write_text(
            json.dumps({"timestamp": "2026-05-20T23:10:00", "v": 1}) + "\n" +
            json.dumps({"timestamp": "2026-06-01T08:00:00", "v": 2}) + "\n",
            encoding="utf-8")
        ts = sga._last_jsonl_timestamp(p)
        assert ts == dt.datetime(2026, 6, 1, 8, 0, 0)  # LAST row's timestamp


def test_last_jsonl_timestamp_none_when_no_ts_field():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "no_ts.jsonl"
        p.write_text(json.dumps({"v": 1}) + "\n", encoding="utf-8")
        assert sga._last_jsonl_timestamp(p) is None


# ---- detect_telemetry_stale (staleness from content ts; uncheckable -> suppress) ----

def test_detect_telemetry_stale_flags_old(monkeypatch=None):
    with tempfile.TemporaryDirectory() as td:
        orig = sga.WORLD_DIR
        sga.WORLD_DIR = td
        try:
            p = Path(td) / "ohs.jsonl"
            p.write_text(json.dumps({"timestamp": "2026-05-01T00:00:00"}) + "\n", encoding="utf-8")
            now = dt.datetime(2026, 6, 1, 0, 0, 0)  # 31 days later
            specs = (("ohs.jsonl", 14, "OHS signal"),)
            gaps = sga.detect_telemetry_stale(now=now, specs=specs)
            assert len(gaps) == 1
            assert gaps[0]["rb245_passed"] is True
            assert gaps[0]["evidence"]["age_days"] >= 30
        finally:
            sga.WORLD_DIR = orig


def test_detect_telemetry_stale_fresh_skips():
    with tempfile.TemporaryDirectory() as td:
        orig = sga.WORLD_DIR
        sga.WORLD_DIR = td
        try:
            p = Path(td) / "ohs.jsonl"
            p.write_text(json.dumps({"timestamp": "2026-05-30T00:00:00"}) + "\n", encoding="utf-8")
            now = dt.datetime(2026, 6, 1, 0, 0, 0)  # 2 days -> fresh
            specs = (("ohs.jsonl", 14, "OHS signal"),)
            assert sga.detect_telemetry_stale(now=now, specs=specs) == []
        finally:
            sga.WORLD_DIR = orig


def test_detect_telemetry_stale_no_content_ts_suppressed():
    with tempfile.TemporaryDirectory() as td:
        orig = sga.WORLD_DIR
        sga.WORLD_DIR = td
        try:
            p = Path(td) / "ohs.jsonl"
            p.write_text(json.dumps({"v": 1}) + "\n", encoding="utf-8")  # no ts field
            now = dt.datetime(2026, 6, 1, 0, 0, 0)
            specs = (("ohs.jsonl", 14, "OHS signal"),)
            gaps = sga.detect_telemetry_stale(now=now, specs=specs)
            assert len(gaps) == 1
            assert gaps[0]["rb245_passed"] is False  # rb-245: can't assert staleness without content ts
        finally:
            sga.WORLD_DIR = orig


# ---- recent_completed_corpus (6: time-windowed completed-goal dedup) ----

_NOW = dt.datetime(2026, 7, 15, 12, 0, 0)  # fixed reference for deterministic windowing


def _completed(gid, days_ago, title="t", desc="d", origin="o", field="completed_at"):
    ts = (_NOW - dt.timedelta(days=days_ago)).replace(microsecond=0).isoformat()
    g = {"id": gid, "status": "completed", "title": title, "description": desc,
         "origin_signal": origin}
    g[field] = ts if field == "completed_at" else ts[:10]
    return g


def test_recent_completed_corpus_includes_within_window():
    goals = [_completed("g-1", 2, origin="investigate:deadline_urgency")]
    corpus = sga.recent_completed_corpus(goals, 14, now=_NOW)
    assert corpus == [("g-1", "t d investigate:deadline_urgency")]  # blob shape = open_goal_corpus


def test_recent_completed_corpus_excludes_older_completion():
    # 25d > 14d window -> NOT in corpus, so a gap matching ONLY this old
    # completion still fires (legitimate regression — the audit's tail purpose).
    goals = [_completed("g-old", 25, origin="investigate:deadline_urgency")]
    assert sga.recent_completed_corpus(goals, 14, now=_NOW) == []


def test_recent_completed_corpus_boundary_inclusive():
    # exactly window_days old -> included (ts == cutoff is not < cutoff)
    goals = [_completed("g-edge", 14)]
    assert [gid for gid, _ in sga.recent_completed_corpus(goals, 14, now=_NOW)] == ["g-edge"]


def test_recent_completed_corpus_skips_non_completed():
    ts = _NOW.isoformat()
    goals = [
        {"id": "p", "status": "pending", "title": "x", "completed_at": ts},
        {"id": "ip", "status": "in-progress", "title": "y", "completed_at": ts},
        {"id": "sk", "status": "skipped", "title": "z", "completed_at": ts},
    ]
    assert sga.recent_completed_corpus(goals, 14, now=_NOW) == []


def test_recent_completed_corpus_skips_no_timestamp():
    # completed but no completed_at/completed_date -> cannot window -> SKIP
    # (regression-safe: the gap fires rather than being suppressed forever).
    goals = [{"id": "g-nots", "status": "completed", "title": "t",
              "description": "d", "origin_signal": "o"}]
    assert sga.recent_completed_corpus(goals, 14, now=_NOW) == []


def test_recent_completed_corpus_completed_date_fallback():
    # only completed_date (date-only, no completed_at) within window -> included
    goals = [_completed("g-date", 3, field="completed_date")]
    assert [gid for gid, _ in sga.recent_completed_corpus(goals, 14, now=_NOW)] == ["g-date"]


def test_completed_corpus_suppresses_refire_via_is_covered():
    # THE fix (6): a gap whose primary token matches a RECENTLY-completed
    # investigation is COVERED even when NO open goal exists — the exact re-fire the
    # open-only dedup missed (3/3 deep-investigated twice).
    completed = [_completed(
        "g-2233", 1, title="Investigate: silent-gap zero-input deadline_urgency",
        origin="investigate:silent-gap-zero-input-goal-selector-deadline_urgency")]
    corpus = sga.recent_completed_corpus(completed, 14, now=_NOW)
    covered, gid = sga.is_covered(["deadline_urgency"], corpus)
    assert covered is True and gid == "g-2233"


def test_completed_corpus_old_refire_still_fires_via_is_covered():
    # Symmetric guard: the SAME gap matched only by an OLD completion is NOT
    # covered -> the audit fires it (regression detection preserved).
    completed = [_completed("g-old", 30, title="Investigate: deadline_urgency",
                            origin="investigate:deadline_urgency")]
    corpus = sga.recent_completed_corpus(completed, 14, now=_NOW)
    covered, gid = sga.is_covered(["deadline_urgency"], corpus)
    assert covered is False and gid is None


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except Exception as e:  # noqa: BLE001 — aggregator reports, doesn't raise
            failures.append(f"[FAIL] {t.__name__}: {type(e).__name__}: {e}")
    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)}/{len(tests)} test(s) failed")
        return 1
    print(f"All {len(tests)} silent-gap-audit cases verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
