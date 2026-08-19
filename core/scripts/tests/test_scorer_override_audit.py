"""Unit tests for scorer-override-audit.py — Scorer Sovereignty Layer C ().

Fully hermetic: synthetic agent diaries under tmp_path via the --agents-root
seam (audit(root=...)), so the test never touches live agent diaries. Covers
the two hit conditions (>3 per agent, any force-override), the time window, the
cross-agent glob routing, and the by-code grouping the metric-of-success needs.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load():
    # Hyphenated module name (matches aspirations-rejection-audit.py) -> importlib.
    spec = importlib.util.spec_from_file_location(
        "scorer_override_audit", SCRIPT_DIR / "scorer-override-audit.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _line(agent, claimed, top, code, ts):
    return json.dumps(
        {
            "entry_type": "scorer_override",
            "content": f"scorer-override: claimed {claimed} over scorer top {top} (deviation={code})",
            "goal_id": claimed,
            "timestamp": ts,
        }
    )


def _write(root, agent, lines):
    d = root / agent / "session"
    d.mkdir(parents=True, exist_ok=True)
    (d / "execution-diary.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _iso(offset_h=0):
    return (datetime.now() - timedelta(hours=offset_h)).strftime("%Y-%m-%dT%H:%M:%S")


def _iso_s(offset_h=0, plus_s=0):
    """Timestamp offset_h hours ago, shifted forward by plus_s seconds — for
    retry-collapse tests that need sub-window spacing (g-115-6163)."""
    return (datetime.now() - timedelta(hours=offset_h) + timedelta(seconds=plus_s)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def test_clean_at_threshold_is_not_a_hit(tmp_path):
    # Exactly 3 -> NOT over (threshold is STRICTLY >3).
    _write(tmp_path, "alpha", [_line("alpha", f"g-{i}", "g-t", "precondition-fail", _iso(1)) for i in range(3)])
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["per_agent"]["alpha"]["total"] == 3
    assert r["hits"] is False
    assert MOD.build_investigate_goal(r) is None


def test_over_threshold_is_a_hit(tmp_path):
    _write(tmp_path, "bravo", [_line("bravo", f"g-{i}", "g-t", "self-abstention", _iso(1)) for i in range(4)])
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["hits"] is True
    assert r["agents_over_threshold"] == {"bravo": 4}
    g = MOD.build_investigate_goal(r)
    assert g is not None
    assert g["participants"] == ["agent"]
    assert g["origin_signal"] == "scorer-override-audit-hit"
    assert "bravo" in g["description"]


def test_force_override_is_a_hit_at_count_one(tmp_path):
    _write(tmp_path, "echo", [_line("echo", "g-1", "g-t", "force-override", _iso(1))])
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["per_agent"]["echo"]["total"] == 1  # only ONE override
    assert r["hits"] is True  # ...but force-override is a hit at count 1
    assert len(r["force_override_rows"]) == 1
    assert MOD.build_investigate_goal(r) is not None


def test_time_window_excludes_stale_entries(tmp_path):
    # 5 force-overrides but all 48h old -> outside the 24h window -> no hit.
    _write(tmp_path, "zeta", [_line("zeta", f"g-{i}", "g-t", "force-override", _iso(48)) for i in range(5)])
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["total_overrides"] == 0
    assert r["hits"] is False


def test_cross_agent_glob_via_agents_root(tmp_path):
    # Two agents -> proves the */session/execution-diary.jsonl glob + root routing.
    _write(tmp_path, "alpha", [_line("alpha", "g-1", "g-t", "self-abstention", _iso(1))])
    _write(tmp_path, "bravo", [_line("bravo", f"g-{i}", "g-t", "meta-tiebreaker", _iso(1)) for i in range(4)])
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert set(r["per_agent"]) == {"alpha", "bravo"}
    assert r["agents_over_threshold"] == {"bravo": 4}


def test_by_code_grouping_populates_the_success_metric(tmp_path):
    _write(
        tmp_path,
        "alpha",
        [
            _line("alpha", "g-1", "g-t", "self-abstention", _iso(1)),
            _line("alpha", "g-2", "g-t", "precondition-fail", _iso(1)),
            _line("alpha", "g-3", "g-t", "precondition-fail", _iso(1)),
        ],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["per_agent"]["alpha"]["by_code"] == {"self-abstention": 1, "precondition-fail": 2}
    assert r["hits"] is False  # 3 sanctioned, not over threshold


def test_malformed_and_non_override_lines_ignored(tmp_path):
    _write(
        tmp_path,
        "alpha",
        [
            '{"entry_type": "phase_start", "content": "phase-4", "timestamp": "' + _iso(1) + '"}',
            "{not valid json",
            "",
            _line("alpha", "g-1", "g-t", "self-abstention", _iso(1)),
        ],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["per_agent"]["alpha"]["total"] == 1  # only the real override counted


# ── : code-class-aware attribution (deviation codes are NOT fungible) ──


def test_by_class_grouping(tmp_path):
    # One deviation per class (distinct tops -> nothing stuck) proves the
    # WEIGHT_SIGNAL / LANE_DISCIPLINE / RUNNABILITY classifier.
    _write(
        tmp_path,
        "bravo",
        [
            _line("bravo", "g-1", "g-a", "cross-agent", _iso(1)),
            _line("bravo", "g-2", "g-b", "self-abstention", _iso(1)),
            _line("bravo", "g-3", "g-c", "precondition-fail", _iso(1)),
            _line("bravo", "g-4", "g-d", "force-override", _iso(1)),
        ],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["per_agent"]["bravo"]["by_class"] == {
        "lane_discipline": 2,
        "runnability": 1,
        "weight_signal": 1,
    }
    assert r["stuck_tops"] == {}  # 4 distinct tops -> nothing stuck


def test_stuck_at_top_recommends_routing_not_weights(tmp_path):
    # VERIFY case 1: 4 precondition-fail all over ONE scorer_top -> stuck-at-top
    # hit with a routing recommendation, NOT 'tune weights' (the  shape).
    _write(
        tmp_path,
        "bravo",
        [_line("bravo", f"g-{i}", "g-001-341", "precondition-fail", _iso(1)) for i in range(4)],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["hits"] is True
    assert r["stuck_tops"]["g-001-341"]["count"] == 4
    classes = MOD.recommendation_classes(r)
    assert "routing" in classes
    assert "weight" not in classes  # RUNNABILITY is never a weights signal
    g = MOD.build_investigate_goal(r)
    assert "STUCK-AT-TOP" in g["description"]
    assert "g-001-341" in g["description"]


def test_concentrated_lane_deviations_are_stuck_routing(tmp_path):
    # The  shape: 13 cross-agent all over ONE top -> stuck-at-top routing,
    # NOT weights, even though the code-class is lane-discipline.
    # Timestamps spread 10 min apart (): identical-tuple rows are 13
    # DISTINCT re-claim decisions here, so each must sit outside the 300s retry
    # window — same-timestamp rows would now correctly collapse to ONE decision.
    _write(
        tmp_path,
        "bravo",
        [_line("bravo", "g-x", "g-001-339", "cross-agent", _iso_s(4, i * 600)) for i in range(13)],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["stuck_tops"]["g-001-339"]["count"] == 13
    classes = MOD.recommendation_classes(r)
    assert "routing" in classes
    assert "weight" not in classes


def test_pure_lane_discipline_over_threshold_no_weight_rec(tmp_path):
    # VERIFY case 2: lane-discipline deviations SPREAD across distinct tops (no
    # stuck) -> lane recommendation, and NEVER a weights recommendation.
    _write(
        tmp_path,
        "bravo",
        [
            _line("bravo", f"g-{i}", f"g-top{i}", "cross-agent" if i % 2 == 0 else "self-abstention", _iso(1))
            for i in range(4)
        ],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["hits"] is True  # over threshold (4 > 3)
    assert r["stuck_tops"] == {}  # spread across 4 tops -> nothing stuck
    classes = MOD.recommendation_classes(r)
    assert "lane" in classes
    assert "weight" not in classes  # by-design lane discipline is NEVER a weights signal
    g = MOD.build_investigate_goal(r)
    assert "LANE-DISCIPLINE" in g["description"]
    assert "Scorer Sovereignty" in g["description"]


def test_force_override_recommends_weights(tmp_path):
    # VERIFY case 3: force-override hits at count 1 with the enum/weights rec —
    # the ONLY class that recommends weight tuning.
    _write(tmp_path, "echo", [_line("echo", "g-1", "g-t", "force-override", _iso(1))])
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["hits"] is True
    classes = MOD.recommendation_classes(r)
    assert "weight" in classes
    g = MOD.build_investigate_goal(r)
    assert "WEIGHT/ENUM SIGNAL" in g["description"]
    assert "enum/weights gap" in g["title"]


# ── : claim RETRIES collapse into decisions before any counting ──


def test_retries_collapse_to_one_decision(tmp_path):
    # The measured alpha cluster: ONE claim decision retried 4x over 65s emits 4
    # identical-tuple rows. Counted raw, alpha is 1 short of over-threshold and
    # the top gains 4 recurrences; collapsed, it is ONE decision.
    _write(
        tmp_path,
        "alpha",
        [_line("alpha", "g-115-6083", "g-335-1215", "partner-claim", _iso_s(1, s)) for s in (0, 20, 45, 65)],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["raw_rows"] == 4
    assert r["retries_collapsed"] == 3
    assert r["per_agent"]["alpha"]["total"] == 1
    assert r["total_overrides"] == 1


def test_collapsed_retries_annotate_evidence_rows(tmp_path):
    # A hit whose decision was retried carries the collapse count into the
    # Investigate goal evidence — the transparency half of .
    _write(
        tmp_path,
        "echo",
        [_line("echo", "g-1", "g-t", "force-override", _iso_s(1, s)) for s in (0, 30)],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["hits"] is True  # force-override hits at decision count 1
    assert len(r["force_override_rows"]) == 1  # 2 rows -> 1 decision
    assert r["force_override_rows"][0]["retries"] == 1
    g = MOD.build_investigate_goal(r)
    assert "[+1 retries collapsed]" in g["description"]


def test_retry_collapse_prevents_false_stuck_top(tmp_path):
    # 4 retries of ONE decision would cross the >3 stuck-top threshold by
    # themselves — the "manufactures false stuck_tops" half of the defect.
    _write(
        tmp_path,
        "alpha",
        [_line("alpha", "g-115-6083", "g-335-1215", "partner-claim", _iso_s(1, s)) for s in (0, 20, 45, 65)],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["stuck_tops"] == {}
    assert r["hits"] is False


def test_same_tuple_beyond_window_is_a_new_decision(tmp_path):
    # Anchored window: a same-tuple row >300s after the cluster ANCHOR is a new
    # decision. Rows at 0s / 250s / 500s -> 250s folds into the 0s anchor, 500s
    # is beyond it and anchors a second decision (chained semantics would give 1).
    _write(
        tmp_path,
        "alpha",
        [_line("alpha", "g-1", "g-t", "partner-claim", _iso_s(1, s)) for s in (0, 250, 500)],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["per_agent"]["alpha"]["total"] == 2
    assert r["retries_collapsed"] == 1


def test_distinct_claims_within_window_never_collapse(tmp_path):
    # 4 DIFFERENT goals claimed seconds apart share agent/top/code but are
    # distinct decisions — over-threshold must still fire on genuine volume.
    _write(
        tmp_path,
        "bravo",
        [_line("bravo", f"g-{i}", "g-t", "self-abstention", _iso_s(1, i * 10)) for i in range(4)],
    )
    r = MOD.audit(since_hours=24, root=tmp_path)
    assert r["retries_collapsed"] == 0
    assert r["agents_over_threshold"] == {"bravo": 4}


# ──  / : _parse_ts offset-aware hardening regression guard ──


def test_parse_ts_normalizes_offset_aware():
    # An offset-aware stamp (e.g. a +00:00 suffix) must parse to a NAIVE datetime,
    # else audit()'s comparison against the naive datetime.now() cutoff raises
    # "can't compare offset-naive and offset-aware datetimes" and takes down the
    # whole audit. This is the exact bug that shipped when zeta's fix was stranded.
    naive = MOD._parse_ts("2026-07-24T13:00:00")
    assert naive.tzinfo is None
    # trailing Z is tolerated (stripped), result stays naive and equal
    assert MOD._parse_ts("2026-07-24T13:00:00Z") == naive
    # +00:00 offset -> same wall time, normalized to naive
    aware = MOD._parse_ts("2026-07-24T13:00:00+00:00")
    assert aware.tzinfo is None
    assert aware == naive
    # non-UTC offset -> converted to UTC wall time, naive (13:00 UTC == 18:00+05:00)
    assert MOD._parse_ts("2026-07-24T18:00:00+05:00") == naive
    # malformed -> None (drop the row, never crash)
    assert MOD._parse_ts("not-a-date") is None


def test_audit_survives_offset_aware_diary_timestamp(tmp_path):
    # Integration path: a diary line carrying an offset-aware timestamp must NOT
    # crash audit(). Build the stamp by hand so it keeps its +00:00 suffix (the
    # _iso() helper emits naive only, which is why this class went uncaught).
    aware_ts = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
    _write(tmp_path, "alpha", [_line("alpha", "g-1", "g-t", "self-abstention", aware_ts)])
    r = MOD.audit(since_hours=24, root=tmp_path)  # must not raise
    assert r["per_agent"]["alpha"]["total"] == 1  # aware-stamped override still counted


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
