"""Tests for compounding-events.py ( / design  Section 13).

Acceptance criteria (design Section 13.3 + 13: anti-inflation and guard
compliance are the acceptance criteria, NOT afterthoughts):
  - C1 self-citation exclusion
  - C1 temporal-order rejection
  - distinct-goal de-dup (Section 8 rule 4)
  - density math (Section 9)
  - value-density / no raw-count headline leakage (guard-841)
  - guard-809 regression: aggregation NEVER emits a prune/archive/delete action
"""
import importlib.util
import json
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parents[1]

# Hyphenated module name -> load by file path (standard pattern; the file is
# core/scripts/compounding-events.py which is not a valid bare import name).
_spec = importlib.util.spec_from_file_location(
    "compounding_events", CORE_SCRIPTS / "compounding-events.py")
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)


# --- helpers ---------------------------------------------------------------

def _lb_event(entry_id="rb-100", goal="g-200-01", src="g-100-01",
              retrieved_at="2026-06-27T10:00:00",
              artifact_write_time="2026-06-27T11:00:00",
              confidence="high", category=None):
    """A well-formed LOAD-BEARING event (passes all gates by default)."""
    e = {
        "entry_id": entry_id,
        "entry_kind": "reasoning_bank",
        "retrieved_by_goal_id": goal,
        "retrieved_at": retrieved_at,
        "was_load_bearing": True,
        "confidence": confidence,
        "artifact_produced": "commit",
        "artifact_ref": "abc123",
        "artifact_write_time": artifact_write_time,
        "source_goal_of_entry": src,
    }
    if category is not None:
        e["category"] = category
    return e


def _non_lb_event(entry_id="rb-100", goal="g-300-01", src="g-100-01", category=None):
    """A well-formed NON-load-bearing (denominator) retrieval event."""
    e = {
        "entry_id": entry_id,
        "entry_kind": "reasoning_bank",
        "retrieved_by_goal_id": goal,
        "retrieved_at": "2026-06-27T10:00:00",
        "was_load_bearing": False,
        "source_goal_of_entry": src,
    }
    if category is not None:
        e["category"] = category
    return e


# --- validation: required fields + kind ------------------------------------

@pytest.mark.parametrize("missing", ["entry_id", "entry_kind",
                                     "retrieved_by_goal_id", "retrieved_at"])
def test_required_field_missing_rejected(missing):
    e = _lb_event()
    del e[missing]
    ok, err, _ = ce.validate_event(e)
    assert ok is False
    assert missing in err


def test_invalid_entry_kind_rejected():
    e = _lb_event()
    e["entry_kind"] = "belief"  # not a tracked store
    ok, err, _ = ce.validate_event(e)
    assert ok is False
    assert "entry_kind" in err


def test_was_load_bearing_must_be_bool():
    e = _lb_event()
    e["was_load_bearing"] = "true"  # string, not bool
    ok, err, _ = ce.validate_event(e)
    assert ok is False
    assert "was_load_bearing" in err


# --- C1 self-citation ------------------------------------------------------

def test_c1_self_citation_load_bearing_excluded():
    # retrieved_by_goal_id == source_goal_of_entry -> an entry cannot compound
    # by being written.
    e = _lb_event(goal="g-555-01", src="g-555-01")
    ok, err, _ = ce.validate_event(e)
    assert ok is False
    assert "self-citation" in err.lower()


def test_c1_self_citation_excluded_even_for_non_load_bearing():
    # A self-retrieval must not count toward the denominator either.
    e = _non_lb_event(goal="g-555-01", src="g-555-01")
    ok, err, _ = ce.validate_event(e)
    assert ok is False
    assert "self-citation" in err.lower()


# --- C1 temporal order -----------------------------------------------------

def test_temporal_order_rejected_when_retrieved_after_artifact():
    e = _lb_event(retrieved_at="2026-06-27T12:00:00",
                  artifact_write_time="2026-06-27T11:00:00")  # retrieved AFTER artifact
    ok, err, _ = ce.validate_event(e)
    assert ok is False
    assert "temporal" in err.lower()


def test_temporal_order_rejected_when_equal():
    e = _lb_event(retrieved_at="2026-06-27T11:00:00",
                  artifact_write_time="2026-06-27T11:00:00")  # equal, not strictly before
    ok, err, _ = ce.validate_event(e)
    assert ok is False


def test_temporal_order_accepted_when_retrieved_before_artifact():
    e = _lb_event(retrieved_at="2026-06-27T10:00:00",
                  artifact_write_time="2026-06-27T11:00:00")
    ok, err, norm = ce.validate_event(e)
    assert ok is True, err
    assert norm["experiment_version"] == "compounding-v1"


def test_temporal_unparseable_timestamps_rejected_for_load_bearing():
    e = _lb_event(artifact_write_time="not-a-timestamp")
    ok, err, _ = ce.validate_event(e)
    assert ok is False
    assert "temporal" in err.lower()


# --- C2 confidence + artifact gate -----------------------------------------

@pytest.mark.parametrize("conf", ["low", "", None, "speculative"])
def test_c2_confidence_gate_rejects_below_medium(conf):
    e = _lb_event(confidence=conf)
    if conf is None:
        del e["confidence"]
    ok, err, _ = ce.validate_event(e)
    assert ok is False


def test_load_bearing_requires_artifact():
    e = _lb_event()
    del e["artifact_produced"]
    ok, err, _ = ce.validate_event(e)
    assert ok is False
    assert "artifact" in err.lower()


def test_load_bearing_requires_source_goal():
    # C1 guard cannot be enforced without source_goal_of_entry.
    e = _lb_event()
    del e["source_goal_of_entry"]
    ok, err, _ = ce.validate_event(e)
    assert ok is False


def test_non_load_bearing_minimal_accepted():
    # A denominator event needs only the four core fields; no artifact/conf/temporal.
    e = _non_lb_event()
    ok, err, norm = ce.validate_event(e)
    assert ok is True, err
    assert norm["was_load_bearing"] is False


# --- distinct-goal de-dup (Section 8 rule 4) -------------------------------

def test_distinct_goal_dedup_one_chatty_goal_counts_once():
    # The SAME entry cited as load-bearing by the SAME goal 5 times == ONE
    # compounding goal, not five. compounding_reach is distinct-goal.
    events = [_lb_event(entry_id="rb-100", goal="g-200-01") for _ in range(5)]
    agg = ce.aggregate_entry("rb-100", events=events)
    assert agg["compounding_reach"] == 1
    assert agg["distinct_goals_load_bearing"] == 1


# --- density math (Section 9) ----------------------------------------------

def test_load_bearing_rate_math():
    # 2 distinct goals load-bearing, 2 more distinct goals retrieved-but-not.
    events = [
        _lb_event(entry_id="rb-1", goal="g-1-01"),
        _lb_event(entry_id="rb-1", goal="g-2-01"),
        _non_lb_event(entry_id="rb-1", goal="g-3-01"),
        _non_lb_event(entry_id="rb-1", goal="g-4-01"),
    ]
    agg = ce.aggregate_entry("rb-1", events=events)
    assert agg["distinct_goals_retrieved"] == 4
    assert agg["distinct_goals_load_bearing"] == 2
    assert agg["load_bearing_rate"] == 0.5
    assert agg["compounding_reach"] == 2


def test_rate_zero_when_never_load_bearing():
    events = [_non_lb_event(entry_id="rb-9", goal="g-1-01")]
    agg = ce.aggregate_entry("rb-9", events=events)
    assert agg["load_bearing_rate"] == 0.0
    assert agg["compounding_reach"] == 0
    assert agg["distinct_goals_retrieved"] == 1


def test_category_density_with_total_entries():
    events = [
        _lb_event(entry_id="rb-1", goal="g-1-01", category="framework"),
        _lb_event(entry_id="rb-2", goal="g-2-01", category="framework"),
        _non_lb_event(entry_id="rb-3", goal="g-3-01", category="framework"),
    ]
    agg = ce.aggregate_category("framework", total_entries=10, events=events)
    assert agg["compounding_entries"] == 2          # rb-1, rb-2 compounded
    assert agg["load_bearing_distinct_goal_events"] == 2
    assert agg["density"] == 0.2                     # 2 / 10


def test_category_density_null_without_total_entries():
    events = [_lb_event(entry_id="rb-1", goal="g-1-01", category="framework")]
    agg = ce.aggregate_category("framework", events=events)
    assert agg["density"] is None
    assert "density_note" in agg  # honest: not event-derivable without the store count


# --- guard-841: value-density, no raw-count headline -----------------------

def test_guard_841_entry_headline_is_density_not_raw_count():
    # The per-entry headline is a RATE (0..1) + a distinct-goal reach, never a
    # raw event count. A chatty entry with many events must not inflate the
    # headline beyond its distinct-goal reach.
    events = [_lb_event(entry_id="rb-1", goal="g-1-01") for _ in range(50)]
    agg = ce.aggregate_entry("rb-1", events=events)
    assert 0.0 <= agg["load_bearing_rate"] <= 1.0
    assert agg["compounding_reach"] == 1            # 50 events, ONE distinct goal
    # No raw event-count key leaks into the per-entry headline.
    assert "event_count" not in agg
    assert "total_events" not in agg


def test_guard_841_summary_labels_raw_counts_descriptive():
    events = [_lb_event(entry_id="rb-1", goal="g-1-01")]
    s = ce.summary(events=events)
    # Raw counts exist for description but the note must steer to densities.
    assert "density" in s["note"].lower()
    assert "guard-841" in s["note"]


# --- guard-809: aggregation NEVER emits a prune/archive/delete action -------

_ACTION_TOKENS = ("prune", "delete", "archive", "merge", "remove", "retire")


def _assert_no_action(obj):
    blob = json.dumps(obj).lower()
    for tok in _ACTION_TOKENS:
        # The summary NOTE explicitly says it emits NO prune/archive action and
        # references guard-809; that descriptive disclaimer is allowed. Forbid
        # the tokens only as ACTION KEYS in the structured output.
        assert tok not in [str(k).lower() for k in obj.keys()], \
            "guard-809: aggregation output must not carry an action key %r" % tok


def test_guard_809_zero_compounding_emits_no_action():
    # A brand-new node with zero compounding (rate 0, reach 0) must produce a
    # pure ADVISORY report -- never a prune/archive/delete action key.
    events = [_non_lb_event(entry_id="rb-new", goal="g-1-01")]
    entry = ce.aggregate_entry("rb-new", events=events)
    assert entry["compounding_reach"] == 0
    _assert_no_action(entry)

    cat = ce.aggregate_category("framework", events=[])  # empty category
    _assert_no_action(cat)

    summ = ce.summary(events=events)
    _assert_no_action(summ)


# --- append round-trip (I/O; LocalBackend pinned hermetic by conftest) ------

def test_append_event_roundtrip(tmp_path):
    store = tmp_path / "compounding-events.jsonl"
    res = ce.append_event(_lb_event(entry_id="rb-rt", goal="g-9-01"), path=str(store))
    assert res["ok"] is True, res
    assert res["event"]["recorded_at"]                 # stamped
    assert res["event"]["schema_version"] == 1
    # Stored and re-readable; aggregation reflects it.
    loaded = ce.load_events(path=str(store))
    assert any(e["entry_id"] == "rb-rt" for e in loaded)
    agg = ce.aggregate_entry("rb-rt", path=str(store))
    assert agg["compounding_reach"] == 1


def test_append_rejects_invalid_without_writing(tmp_path):
    store = tmp_path / "compounding-events.jsonl"
    res = ce.append_event(_lb_event(goal="g-555-01", src="g-555-01"), path=str(store))  # self-citation
    assert res["ok"] is False
    assert "self-citation" in res["error"].lower()
    # Nothing written.
    assert ce.load_events(path=str(store)) == []


# --- emit() iteration-close wiring (design Section 6) -----------------------

def _write_manifest(path, *, supp=None, trees=None,
                    timestamp="2026-06-27T10:00:00", retrieval_performed=True):
    """Write a synthetic retrieval-session.json for emit() tests."""
    m = {
        "timestamp": timestamp,
        "retrieval_performed": retrieval_performed,
        "supplementary_detail": supp or [],
        "tree_nodes_loaded": trees or [],
    }
    Path(path).write_text(json.dumps(m), encoding="utf-8")
    return str(path)


def test_emit_disabled_is_noop(tmp_path):
    # The dormant-ship guarantee (design Section 6 + 13): with the flag OFF,
    # emit() writes NOTHING. This is the property that lets  ship inert.
    store = tmp_path / "compounding-events.jsonl"
    manifest = _write_manifest(
        tmp_path / "retrieval-session.json",
        supp=[{"id": "rb-100", "type": "reasoning_bank"}])
    res = ce.emit("g-200-01", manifest_path=manifest, store=str(store), enabled=False)
    assert res["reason"] == "disabled"
    assert res["emitted_load_bearing"] == 0
    assert res["emitted_denominator"] == 0
    assert ce.load_events(path=str(store)) == []


def test_emit_denominators_only_without_citations(tmp_path):
    # Every retrieved entry with no citation is a non-load-bearing denominator
    # (needed for the Section 9 load_bearing_rate denominator).
    store = tmp_path / "compounding-events.jsonl"
    manifest = _write_manifest(
        tmp_path / "retrieval-session.json",
        supp=[{"id": "rb-100", "type": "reasoning_bank"},
              {"id": "guard-50", "type": "guardrail"}],
        trees=["system/foo"])
    res = ce.emit("g-200-01", manifest_path=manifest, store=str(store), enabled=True)
    assert res["emitted_denominator"] == 3   # 2 supp + 1 tree
    assert res["emitted_load_bearing"] == 0
    events = ce.load_events(path=str(store))
    assert len(events) == 3
    assert all(e["was_load_bearing"] is False for e in events)


def test_emit_pattern_signature_excluded_from_denominator(tmp_path):
    # guard-575: a retrieved pattern_signature must NOT become a compounding
    # event here (its outcome is recorded via reflect-on-outcome, not double-counted).
    store = tmp_path / "compounding-events.jsonl"
    manifest = _write_manifest(
        tmp_path / "retrieval-session.json",
        supp=[{"id": "rb-100", "type": "reasoning_bank"},
              {"id": "sig-003", "type": "pattern_signature"}])
    res = ce.emit("g-200-01", manifest_path=manifest, store=str(store), enabled=True)
    assert res["emitted_denominator"] == 1   # only rb-100; sig-003 excluded
    events = ce.load_events(path=str(store))
    assert all(e["entry_id"] != "sig-003" for e in events)


def test_emit_cited_entry_is_load_bearing(tmp_path):
    # An explicitly-cited entry (entry_id:source_goal) becomes a HIGH-confidence
    # load-bearing event; uncited retrieved entries stay denominators.
    store = tmp_path / "compounding-events.jsonl"
    manifest = _write_manifest(
        tmp_path / "retrieval-session.json",
        supp=[{"id": "rb-100", "type": "reasoning_bank"},
              {"id": "guard-50", "type": "guardrail"}],
        timestamp="2026-06-27T10:00:00")
    res = ce.emit("g-200-01", artifact_produced="commit", artifact_ref="abc123",
                  artifact_write_time="2026-06-27T11:00:00",
                  cited={"rb-100": "g-100-01"},
                  manifest_path=manifest, store=str(store), enabled=True)
    assert res["emitted_load_bearing"] == 1
    assert res["emitted_denominator"] == 1   # guard-50 uncited
    lb = [e for e in ce.load_events(path=str(store)) if e["was_load_bearing"]]
    assert len(lb) == 1
    assert lb[0]["entry_id"] == "rb-100"
    assert lb[0]["confidence"] == "high"
    assert lb[0]["source_goal_of_entry"] == "g-100-01"


def test_emit_self_citation_cited_is_skipped(tmp_path):
    # cited source == the closing goal -> self-citation -> add() rejects ->
    # counted as skipped, NOT load-bearing, and nothing for that entry is stored.
    store = tmp_path / "compounding-events.jsonl"
    manifest = _write_manifest(
        tmp_path / "retrieval-session.json",
        supp=[{"id": "rb-100", "type": "reasoning_bank"}])
    res = ce.emit("g-200-01", artifact_ref="abc123",
                  artifact_write_time="2026-06-27T11:00:00",
                  cited={"rb-100": "g-200-01"},   # source == closing goal
                  manifest_path=manifest, store=str(store), enabled=True)
    assert res["emitted_load_bearing"] == 0
    assert res["skipped"] == 1
    assert ce.load_events(path=str(store)) == []


def test_emit_cited_temporal_violation_skipped(tmp_path):
    # cited but artifact_write_time precedes retrieved_at -> temporal gate
    # rejects -> skipped (the emit path surfaces add()'s conservative refusal).
    store = tmp_path / "compounding-events.jsonl"
    manifest = _write_manifest(
        tmp_path / "retrieval-session.json",
        supp=[{"id": "rb-100", "type": "reasoning_bank"}],
        timestamp="2026-06-27T12:00:00")   # retrieved AFTER the artifact below
    res = ce.emit("g-200-01", artifact_ref="abc123",
                  artifact_write_time="2026-06-27T11:00:00",
                  cited={"rb-100": "g-100-01"},
                  manifest_path=manifest, store=str(store), enabled=True)
    assert res["emitted_load_bearing"] == 0
    assert res["skipped"] == 1
    assert ce.load_events(path=str(store)) == []


def test_emit_missing_manifest_returns_zero(tmp_path):
    # No manifest on disk -> no retrieved entries -> zero events, no crash.
    store = tmp_path / "compounding-events.jsonl"
    res = ce.emit("g-200-01",
                  manifest_path=str(tmp_path / "does-not-exist.json"),
                  store=str(store), enabled=True)
    assert res["emitted_load_bearing"] == 0
    assert res["emitted_denominator"] == 0
    assert res["skipped"] == 0
    assert ce.load_events(path=str(store)) == []


def test_emit_never_raises_on_bad_manifest(tmp_path):
    # A malformed manifest must fail-open (emit is called fail-open from
    # iteration-close) -- read_manifest_entries tolerates it -> [] -> zero.
    store = tmp_path / "compounding-events.jsonl"
    bad = tmp_path / "retrieval-session.json"
    bad.write_text("{not json", encoding="utf-8")
    res = ce.emit("g-200-01", manifest_path=str(bad), store=str(store), enabled=True)
    assert res["emitted_load_bearing"] == 0
    assert res["emitted_denominator"] == 0


# --- is_enabled() feature flag (dormant-ship gate) --------------------------

def test_is_enabled_env_override_true(monkeypatch):
    monkeypatch.setenv("COMPOUNDING_METRIC_ENABLED", "1")
    assert ce.is_enabled() is True


def test_is_enabled_env_override_false(monkeypatch):
    monkeypatch.setenv("COMPOUNDING_METRIC_ENABLED", "off")
    assert ce.is_enabled() is False


def test_is_enabled_default_off_from_config(monkeypatch):
    # No env override -> reads aspirations.yaml, which ships enabled:false (the
    # dormant default). Pins the ship-OFF guarantee for .
    monkeypatch.delenv("COMPOUNDING_METRIC_ENABLED", raising=False)
    assert ce.is_enabled() is False
