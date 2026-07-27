"""Tests for aspirations-rejection-audit.py silent-death detection ().

The Layer-C detective was extended to flag the resurrection-death gap (rb-4345):
the deadman net fires, the resurrected turn goes quiet without re-arming, and the
loop dies a second time. The detectable signature is an IDLE gap between
consecutive deadman arms that ALSO contains a structured API-error event — the
2026-07-19 cc-04 shape. These tests pin the new detector AND guard the
pre-existing bad-slash / rejection detection against regression.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

_AUDIT_PY = os.path.join(_SCRIPTS, "aspirations-rejection-audit.py")
_spec = importlib.util.spec_from_file_location("aspirations_rejection_audit", _AUDIT_PY)
aud = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aud)

from pathlib import Path

# Fixed timestamps modelling the real cc-04 incident: last arm 23:38, silence,
# next arm 07:32 after zombie-recovery (~7.9h idle gap).
T_ARM1 = "2026-07-19T23:38:59Z"
T_ERR = "2026-07-20T00:01:00Z"   # structured API error inside the gap
T_ARM2 = "2026-07-20T07:32:18Z"
CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)  # everything after Jan 2026


def _text_line(ts, text, role="assistant"):
    return json.dumps({"timestamp": ts,
                       "message": {"role": role, "content": [{"type": "text", "text": text}]}})


def _arm_line(ts):
    return json.dumps({"timestamp": ts, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "ScheduleWakeup",
         "input": {"prompt": "<<autonomous-loop-dynamic>>", "delaySeconds": 600}}]}})


def _bad_arm_line(ts):
    return json.dumps({"timestamp": ts, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "ScheduleWakeup", "input": {"prompt": "/aspirations loop"}}]}})


def _api_error_line(ts, status=529):
    # Carries the STRUCTURED flag Claude Code sets on genuine transport errors.
    return json.dumps({"timestamp": ts, "isApiErrorMessage": True, "apiErrorStatus": status,
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": f"API Error: {status} Overloaded."}]}})


def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ── silent-death gap detection ────────────────────────────────────────────────

def test_high_confidence_gap_flagged(tmp_path):
    # Idle 7.9h arm gap WITH a structured API error inside → the incident shape.
    p = _write(tmp_path, "t.jsonl", [
        _arm_line(T_ARM1),
        _api_error_line(T_ERR),
        _arm_line(T_ARM2),
    ])
    r = aud._scan_transcript(p, CUTOFF)
    assert r["resurrection_risk"] is True
    assert r["deadman_arms"] == 2
    assert len(r["resurrection_gaps"]) == 1
    g = r["resurrection_gaps"][0]
    assert g["high_confidence"] is True
    assert g["api_errors_in_gap"] == 1
    assert 7 < g["gap_hours"] < 9


def test_idle_gap_without_api_error_not_high_confidence(tmp_path):
    # Same idle gap, NO API error → a legitimate /stop idle looks like this;
    # recorded as a candidate gap but NOT risk-flagged.
    p = _write(tmp_path, "t.jsonl", [_arm_line(T_ARM1), _arm_line(T_ARM2)])
    r = aud._scan_transcript(p, CUTOFF)
    assert r["resurrection_risk"] is False
    assert len(r["resurrection_gaps"]) == 1
    assert r["resurrection_gaps"][0]["high_confidence"] is False


def test_sub_hour_gap_not_flagged_even_with_error(tmp_path):
    # A 40-min gap is under the 1h floor — a normal long iteration, not a death,
    # even if a transient API error occurred inside it.
    p = _write(tmp_path, "t.jsonl", [
        _arm_line("2026-07-20T10:00:00Z"),
        _api_error_line("2026-07-20T10:20:00Z"),
        _arm_line("2026-07-20T10:40:00Z"),
    ])
    r = aud._scan_transcript(p, CUTOFF)
    assert r["resurrection_gaps"] == []
    assert r["resurrection_risk"] is False


def test_high_churn_gap_still_flagged(tmp_path):
    # REGRESSION GUARD for the density bug that hid the real incident: a
    # storm-death is HIGH-churn (the 2026-07-19 gap held 577 failed-retry/hook
    # lines), not idle. A long gap PACKED with events + an API error MUST still
    # flag — event count must not exclude it.
    lines = [_arm_line(T_ARM1)]
    for i in range(200):  # ~200 churn lines spread across the 7.9h gap
        lines.append(_text_line(f"2026-07-20T{i // 60:02d}:{i % 60:02d}:30Z", "retry failed"))
    lines.append(_api_error_line(T_ERR))
    lines.append(_arm_line(T_ARM2))
    r = aud._scan_transcript(_write(tmp_path, "t.jsonl", lines), CUTOFF)
    assert r["resurrection_risk"] is True
    assert r["resurrection_gaps"][0]["high_confidence"] is True
    assert r["resurrection_gaps"][0]["events_in_gap"] >= 200


def test_short_gap_not_flagged(tmp_path):
    p = _write(tmp_path, "t.jsonl", [
        _arm_line("2026-07-20T10:00:00Z"),
        _api_error_line("2026-07-20T10:02:00Z"),
        _arm_line("2026-07-20T10:05:00Z"),  # 5-min gap < 1800s
    ])
    r = aud._scan_transcript(p, CUTOFF)
    assert r["resurrection_gaps"] == []
    assert r["resurrection_risk"] is False


def test_content_mention_without_structured_flag_is_not_an_error(tmp_path):
    # REGRESSION GUARD for the defect this rewrite fixed: a turn that merely
    # DISCUSSES "529 Overloaded" (no isApiErrorMessage flag) inside an idle gap
    # must NOT be treated as an API error → gap stays low-confidence.
    p = _write(tmp_path, "t.jsonl", [
        _arm_line(T_ARM1),
        _text_line(T_ERR, "analyzing the 529 Overloaded / ECONNRESET incident"),
        _arm_line(T_ARM2),
    ])
    r = aud._scan_transcript(p, CUTOFF)
    assert r["api_errors"] == []
    assert r["resurrection_risk"] is False
    assert r["resurrection_gaps"][0]["high_confidence"] is False


# ── deadman arm vs bad-slash discrimination (regression) ──────────────────────

def test_deadman_sentinel_counted_as_arm_not_bad(tmp_path):
    p = _write(tmp_path, "t.jsonl", [_arm_line("2026-07-20T10:00:00Z")])
    r = aud._scan_transcript(p, CUTOFF)
    assert r["deadman_arms"] == 1
    assert r["bad_schedule_wakeups"] == []


def test_bad_slash_still_detected(tmp_path):
    p = _write(tmp_path, "t.jsonl", [_bad_arm_line("2026-07-20T10:00:00Z")])
    r = aud._scan_transcript(p, CUTOFF)
    assert len(r["bad_schedule_wakeups"]) == 1
    assert r["deadman_arms"] == 0


def test_rejection_message_still_detected(tmp_path):
    p = _write(tmp_path, "t.jsonl", [
        json.dumps({"timestamp": "2026-07-20T10:00:00Z",
                    "message": {"role": "user",
                                "content": "This skill can only be invoked by Claude"}}),
    ])
    r = aud._scan_transcript(p, CUTOFF)
    assert len(r["rejection_messages"]) == 1


def test_cutoff_excludes_old_events(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    p = _write(tmp_path, "t.jsonl", [_api_error_line(old), _arm_line(old)])
    recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    r = aud._scan_transcript(p, recent_cutoff)
    assert r["deadman_arms"] == 0
    assert r["api_errors"] == []


# ── build_report end-to-end ───────────────────────────────────────────────────

def test_build_report_surfaces_resurrection_risk(tmp_path):
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, "sid-alpha-1.jsonl", [
        _arm_line(T_ARM1), _api_error_line(T_ERR), _arm_line(T_ARM2)])
    # A clean runner transcript with tight arm cadence — must NOT surface.
    _write(tdir, "sid-alpha-2.jsonl", [
        _arm_line("2026-07-20T09:00:00Z"), _arm_line("2026-07-20T09:05:00Z")])
    report = aud._build_report(tdir, tmp_path, since_hours=24 * 365)
    assert report["totals"]["total_resurrection_risk_sids"] == 1
    assert report["totals"]["agents_with_resurrection_risk"] == 1
    hits = (report["totals"]["total_bad_schedule_wakeups"]
            + report["totals"]["total_rejections"]
            + report["totals"]["total_resurrection_risk_sids"])
    assert hits == 1


def test_build_report_clean_is_empty(tmp_path):
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, "sid-clean.jsonl", [
        _arm_line("2026-07-20T09:00:00Z"), _arm_line("2026-07-20T09:05:00Z")])
    report = aud._build_report(tdir, tmp_path, since_hours=24 * 365)
    assert report["per_agent"] == {}
    assert report["totals"]["total_resurrection_risk_sids"] == 0


# ── health-ledger cross-reference filter () ─────────────────────────

def _health_line(ts, iteration=1, agent="alpha"):
    # Mirrors the health-ledger schema appended by iteration-close.sh: `ts` is a
    # naive UTC ISO timestamp (CLAUDE.md TZ convention).
    return json.dumps({"ts": ts, "agent": agent, "iteration": iteration,
                       "session_id": "s", "signals": {}, "composite": 0.6})


def _bind_agent(project_root, sid, agent):
    # Legacy .active-agent-<SID> binding (the fallback _load_agent_map reads when
    # no Phase-2.6 sessions/<SID>/binding.yaml exists).
    (Path(project_root) / f".active-agent-{sid}").write_text(agent, encoding="utf-8")


def _write_health(project_root, agent, date, lines):
    hd = Path(project_root) / "agents" / agent / "health"
    hd.mkdir(parents=True, exist_ok=True)
    (hd / f"{date}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_health_ledger_ts_unknown_or_absent_agent_returns_empty(tmp_path):
    # Conservative: no agent / no health dir → [] → no filtering, risk stands.
    assert aud._health_ledger_ts(tmp_path, "(unknown)", CUTOFF) == []
    assert aud._health_ledger_ts(tmp_path, "", CUTOFF) == []
    assert aud._health_ledger_ts(tmp_path, "ghost", CUTOFF) == []


def test_health_ledger_ts_parses_naive_utc_and_filters_cutoff(tmp_path):
    _write_health(tmp_path, "alpha", "2026-07-20", [
        _health_line("2026-07-20T00:01:00", 1),    # naive UTC (ledger convention)
        _health_line("2026-07-20T00:05:00Z", 2),   # Z-suffixed also accepted
        _health_line("2025-01-01T00:00:00", 3),    # before cutoff → excluded
        json.dumps({"agent": "alpha", "no_ts": True}),  # missing ts → skipped
        "not json at all",                          # unparseable → skipped
    ])
    ts = aud._health_ledger_ts(tmp_path, "alpha", CUTOFF)
    assert len(ts) == 2
    assert all(t.tzinfo is not None for t in ts)   # all tz-aware UTC
    assert ts == sorted(ts)                          # returned sorted


def test_health_ledger_liveness_downgrades_high_confidence(tmp_path):
    # High-confidence gap (arm, API error, arm) BUT the agent's health-ledger has
    # an entry INSIDE the gap → the loop was demonstrably iterating across the
    # "silent" window → downgrade (transcript-completeness artifact, not a death).
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, "sidx.jsonl", [
        _arm_line(T_ARM1), _api_error_line(T_ERR), _arm_line(T_ARM2)])
    _bind_agent(tmp_path, "sidx", "alpha")
    _write_health(tmp_path, "alpha", "2026-07-20", [
        _health_line("2026-07-20T03:00:00", 100)])   # inside 23:38→07:32 gap
    report = aud._build_report(tdir, tmp_path, since_hours=24 * 365)
    assert report["totals"]["total_resurrection_risk_sids"] == 0   # downgraded
    assert report["totals"]["total_health_ledger_filtered"] == 1
    assert report["per_agent"] == {}   # sole hit downgraded → transcript skipped


def test_health_ledger_empty_gap_preserves_real_incident(tmp_path):
    # The real 2026-07-19 shape: health-ledger entries BEFORE and AFTER the gap
    # but NONE inside (dead loop → no iteration-close append). Must stay flagged.
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, "sidy.jsonl", [
        _arm_line(T_ARM1), _api_error_line(T_ERR), _arm_line(T_ARM2)])
    _bind_agent(tmp_path, "sidy", "alpha")
    _write_health(tmp_path, "alpha", "2026-07-19", [
        _health_line("2026-07-19T23:38:27", 241)])   # just before the gap
    _write_health(tmp_path, "alpha", "2026-07-20", [
        _health_line("2026-07-20T15:04:51", 242)])   # after the gap
    report = aud._build_report(tdir, tmp_path, since_hours=24 * 365)
    assert report["totals"]["total_resurrection_risk_sids"] == 1   # preserved
    assert report["totals"]["total_health_ledger_filtered"] == 0
    g = [x for x in report["per_agent"]["alpha"]["resurrection_gaps"]
         if x["high_confidence"]][0]
    assert g["health_ledger_entries_in_gap"] == 0
    assert g["health_ledger_liveness_in_gap"] is False


def test_health_ledger_unknown_agent_preserves_risk(tmp_path):
    # No binding → agent "(unknown)" → no health dir → filter is a no-op →
    # a real high-confidence gap is NOT wrongly suppressed for an unmapped SID.
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, "sid-unmapped.jsonl", [
        _arm_line(T_ARM1), _api_error_line(T_ERR), _arm_line(T_ARM2)])
    report = aud._build_report(tdir, tmp_path, since_hours=24 * 365)
    assert report["totals"]["total_resurrection_risk_sids"] == 1
    assert report["totals"]["total_health_ledger_filtered"] == 0
