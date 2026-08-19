"""Tests for scar-tissue-check.py ( — the subtractive gradient's cadence).

The module filename is kebab-case per the repo naming convention, so it is loaded
via importlib rather than a plain import.

These pin the properties that make the instrument trustworthy rather than merely
runnable. Two of them are regressions from defects found in this file's own first
run, which is the reason they are worth stating explicitly:

  * ``metrics`` must be present in BOTH branches of measure_file_surface. The
    ledger branch originally returned append_and_delta's shape verbatim, which
    nests the counts under ``row`` — so half A printed an empty metrics line while
    still printing a verdict. A measurement of nothing that looks like a
    measurement is the worst failure shape an instrument can have.
  * the aspirations.yaml criterion keys must actually reach the predicate. They
    were declared in config before the loader read them, which advertises a
    tuning knob that does nothing.
"""
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "scar_tissue_check", str(_SCRIPTS / "scar-tissue-check.py"))
stc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stc)


# ─────────────────────────── helpers ───────────────────────────

def _entry(eid, *, status="active", helpful=0, cited=0, inferred=0,
           retrievals=0, created="2020-01-01", title="t"):
    return {"id": eid, "status": status, "title": title, "created": created,
            "utilization": {"times_helpful": helpful, "times_cited": cited,
                            "times_inferred_helpful": inferred,
                            "retrieval_count": retrievals}}


def _write_store(tmp_path, name, entries):
    p = tmp_path / name
    p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return p


class _Args:
    """Minimal stand-in for the argparse namespace `run` consumes."""
    def __init__(self, **kw):
        self.ledger = kw.get("ledger")
        self.min_retrievals = kw.get("min_retrievals", 100)
        self.min_age_days = kw.get("min_age_days", 30)
        self.slate_cap = kw.get("slate_cap", 25)


# ─────────────────────────── never-helpful predicate ───────────────────────────

def test_never_helpful_requires_all_three_channels_zero():
    assert stc._never_helpful(_entry("a")) is True


@pytest.mark.parametrize("kw", [{"helpful": 1}, {"cited": 1}, {"inferred": 1}])
def test_any_attestation_channel_disqualifies_never_helpful(kw):
    """An entry attested by ANY channel is not 'never helpful'.

    Counting only times_helpful would overstate the dead population — the
    inferred-helpful backstop is a real attestation the utilization system
    already honours (g-115-1605).
    """
    assert stc._never_helpful(_entry("a", **kw)) is False


# ─────────────────────────── corpus stats ───────────────────────────

def test_missing_store_reports_absence_not_zero(tmp_path):
    """rb-245: a zero-count against a store that is not there is not a zero."""
    s = stc.corpus_stats(tmp_path / "nope.jsonl", "guardrails", date(2026, 8, 1),
                         100, 30, 25)
    assert s["present"] is False
    assert s["total"] is None and s["active"] is None and s["retired"] is None
    assert s["never_helpful"] is None
    assert s["slate"] == [] and s["slate_total"] == 0


def test_retire_ratio_is_none_when_nothing_retired(tmp_path):
    """An undefined ratio must not be rendered as a number — the absence of any
    retirement IS the finding, and a fake denominator would hide it."""
    p = _write_store(tmp_path, "g.jsonl", [_entry("g1"), _entry("g2")])
    s = stc.corpus_stats(p, "guardrails", date(2026, 8, 1), 100, 30, 25)
    assert s["retired"] == 0
    assert s["retire_ratio"] is None


def test_retire_ratio_computed_when_retirements_exist(tmp_path):
    p = _write_store(tmp_path, "g.jsonl", [
        _entry("g1"), _entry("g2"), _entry("g3"), _entry("g4"),
        _entry("g5", status="retired")])
    s = stc.corpus_stats(p, "guardrails", date(2026, 8, 1), 100, 30, 25)
    assert s["active"] == 4 and s["retired"] == 1
    assert s["retire_ratio"] == 4.0


def test_never_helpful_counts_only_active_entries(tmp_path):
    """A retired entry is already subtracted — counting it would double-count the
    very work the cadence exists to credit."""
    p = _write_store(tmp_path, "g.jsonl", [
        _entry("g1"), _entry("g2", status="retired")])
    s = stc.corpus_stats(p, "guardrails", date(2026, 8, 1), 100, 30, 25)
    assert s["active"] == 1
    assert s["never_helpful"] == 1


def test_corrupt_line_does_not_blind_the_measurement(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps(_entry("g1")) + "\n{ not json\n"
                 + json.dumps(_entry("g2")) + "\n", encoding="utf-8")
    s = stc.corpus_stats(p, "guardrails", date(2026, 8, 1), 100, 30, 25)
    assert s["total"] == 2


# ─────────────────────────── the bounded slate ───────────────────────────

def _dead_entries(n, base_rc=500):
    """Entries that satisfy the dead-entry criterion: old, heavily retrieved,
    zero attestation on every channel."""
    return [_entry(f"d{i}", retrievals=base_rc + i, created="2020-01-01")
            for i in range(n)]


def test_slate_is_capped_and_truncation_is_flagged(tmp_path):
    p = _write_store(tmp_path, "rb.jsonl", _dead_entries(10))
    s = stc.corpus_stats(p, "reasoning-bank", date(2026, 8, 1), 100, 30, 3)
    assert s["slate_total"] == 10
    assert len(s["slate"]) == 3
    assert s["slate_truncated"] is True


def test_untruncated_slate_is_not_flagged(tmp_path):
    p = _write_store(tmp_path, "rb.jsonl", _dead_entries(2))
    s = stc.corpus_stats(p, "reasoning-bank", date(2026, 8, 1), 100, 30, 25)
    assert s["slate_total"] == 2 and s["slate_truncated"] is False


def test_slate_orders_highest_retrieval_first(tmp_path):
    """A bounded proposal should spend its budget where carrying cost is greatest."""
    p = _write_store(tmp_path, "rb.jsonl", _dead_entries(5))
    s = stc.corpus_stats(p, "reasoning-bank", date(2026, 8, 1), 100, 30, 25)
    rcs = [e["retrieval_count"] for e in s["slate"]]
    assert rcs == sorted(rcs, reverse=True)


def test_attested_entry_is_never_proposed(tmp_path):
    """The whole point of the criterion: volume alone is not deadness."""
    p = _write_store(tmp_path, "rb.jsonl",
                     [_entry("live", retrievals=9999, helpful=1, created="2020-01-01")])
    s = stc.corpus_stats(p, "reasoning-bank", date(2026, 8, 1), 100, 30, 25)
    assert s["slate_total"] == 0


def test_min_retrievals_widens_the_slate(tmp_path):
    """The criterion must be live, not decorative."""
    p = _write_store(tmp_path, "rb.jsonl",
                     [_entry("x", retrievals=60, created="2020-01-01")])
    strict = stc.corpus_stats(p, "reasoning-bank", date(2026, 8, 1), 100, 30, 25)
    loose = stc.corpus_stats(p, "reasoning-bank", date(2026, 8, 1), 50, 30, 25)
    assert strict["slate_total"] == 0
    assert loose["slate_total"] == 1


# ─────────────────────────── half A shape (regression) ───────────────────────────

def test_metrics_present_without_ledger():
    out = stc.measure_file_surface(None)
    assert out["verdict"] == "no-ledger"
    assert out["metrics"] and "rules" in out["metrics"]


def test_metrics_present_with_ledger(tmp_path):
    """REGRESSION: append_and_delta nests counts under `row`. Returning its shape
    verbatim left `metrics` empty, so half A printed a verdict over a blank line —
    a successful-looking measurement of nothing."""
    out = stc.measure_file_surface(tmp_path / "ledger.jsonl")
    assert out["metrics"] and "rules" in out["metrics"]
    assert out["verdict"] == "baseline"


def test_ledger_accumulates_and_diffs(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    first = stc.measure_file_surface(ledger)
    second = stc.measure_file_surface(ledger)
    assert first["verdict"] == "baseline"
    assert second["had_previous"] is True
    assert second["metrics"] == first["metrics"]
    assert len(ledger.read_text(encoding="utf-8").strip().splitlines()) == 2


# ─────────────────────────── signal gating ───────────────────────────

def test_flat_surface_with_empty_slate_is_not_signal():
    """A clean bill of health stays quiet — an instrument that posts on every fire
    trains its readers to skip it."""
    result = {"file_surface": {"verdict": "flat"},
              "stores": [{"slate_total": 0}, {"slate_total": 0}]}
    assert stc.has_signal(result) is False


@pytest.mark.parametrize("verdict", ["growing", "mixed"])
def test_growth_is_signal(verdict):
    result = {"file_surface": {"verdict": verdict},
              "stores": [{"slate_total": 0}]}
    assert stc.has_signal(result) is True


def test_nonempty_slate_is_signal_even_when_surface_flat():
    result = {"file_surface": {"verdict": "flat"},
              "stores": [{"slate_total": 0}, {"slate_total": 4}]}
    assert stc.has_signal(result) is True


def test_shrinking_surface_is_not_signal():
    """Subtraction is the goal, not an alarm (learning-philosophy rule 5)."""
    result = {"file_surface": {"verdict": "shrinking"},
              "stores": [{"slate_total": 0}]}
    assert stc.has_signal(result) is False


# ─────────────────────────── proposal-only guarantee ───────────────────────────

def test_no_apply_flag_is_registered():
    """'Never auto-retire' is STRUCTURAL, not a flag someone can pass.

    Asserts on the argparse REGISTRATION, not a bare substring: the file
    legitimately contains the text '--apply' twice — once in the docstring
    stating that no such path exists, and once in the render() line telling the
    agent which OTHER tool to run. A naive `"--apply" not in src` matches its own
    documentation and fails on a correct file, which is the count-vs-match trap
    in miniature: prose asserting an absence is not evidence about code.
    """
    src = (_SCRIPTS / "scar-tissue-check.py").read_text(encoding="utf-8")
    assert 'add_argument("--apply"' not in src
    assert "add_argument('--apply'" not in src


def test_no_mutation_helper_is_imported():
    """The retirement machinery must be unreachable from this module."""
    src = (_SCRIPTS / "scar-tissue-check.py").read_text(encoding="utf-8")
    assert "locked_modify_jsonl" not in src
    assert "_apply_retirement" not in src


def test_argparse_surface_has_no_mutating_flag():
    """Behavioural twin of the source check: parse the real CLI and confirm no
    option even accepts an apply-like action."""
    with pytest.raises(SystemExit) as exc:
        stc.main(["--apply"])
    assert exc.value.code != 0


def test_render_states_proposal_only(tmp_path):
    result = {"file_surface": stc.measure_file_surface(None),
              "stores": [stc.corpus_stats(_write_store(tmp_path, "rb.jsonl",
                                                       _dead_entries(2)),
                                          "reasoning-bank", date(2026, 8, 1),
                                          100, 30, 25)]}
    text = stc.render(result)
    assert "PROPOSAL ONLY" in text
    assert "cannot retire anything" in text


def test_render_reports_missing_store_as_missing(tmp_path):
    result = {"file_surface": stc.measure_file_surface(None),
              "stores": [stc.corpus_stats(tmp_path / "gone.jsonl", "guardrails",
                                          date(2026, 8, 1), 100, 30, 25)]}
    text = stc.render(result)
    assert "STORE NOT FOUND" in text
    assert "not a zero measurement" in text


# ─────────────────────────── config precedence ───────────────────────────

def test_config_block_supplies_the_criterion():
    """REGRESSION: the criterion keys were declared in aspirations.yaml before the
    loader read them, so lowering min_retrievals silently did nothing."""
    cfg = stc._load_cadence_config()
    for key in ("goal_cadence", "wm_slot", "min_retrievals", "min_age_days",
                "slate_cap"):
        assert key in cfg
    assert isinstance(cfg["min_retrievals"], int)


def test_live_config_block_is_parseable_and_wired():
    """The shipped aspirations.yaml block must actually load — a typo here would
    silently fall back to defaults on every fire."""
    import yaml
    cfg_path = _SCRIPTS.parent / "config" / "aspirations.yaml"
    block = (yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
             or {}).get("scar_tissue_check")
    assert block is not None, "scar_tissue_check block missing from aspirations.yaml"
    assert block["wm_slot"] == "last_scar_tissue_check"
    assert int(block["goal_cadence"]) > 0


# ─────────────────────────── cadence gate guards ───────────────────────────

def test_first_fire_is_normalized_to_one_cadence(monkeypatch):
    """: an unset slot must read as 'due now', not 'overdue by thousands'."""
    monkeypatch.setattr(stc, "_count_completed_goals", lambda: 99999)
    monkeypatch.setattr(stc, "_wm_read", lambda slot: None)
    fire, current, cfg, last = stc._cadence_gate()
    assert fire is True and last is None


def test_zero_guard_noops_without_restamping(monkeypatch):
    """guard-1091: _count_completed_goals returns 0 on four distinct failure paths.
    Re-baselining on that sentinel would persist a transient failure as the basis
    and then spuriously fire."""
    stamped = []
    monkeypatch.setattr(stc, "_count_completed_goals", lambda: 0)
    monkeypatch.setattr(stc, "_wm_read",
                        lambda slot: {"goals_count_at_last_fire": 500})
    monkeypatch.setattr(stc, "_wm_set", lambda s, v: stamped.append((s, v)))
    fire, current, cfg, last = stc._cadence_gate()
    assert fire is False
    assert stamped == [], "must NOT re-stamp on a failed measurement"


def test_real_backward_movement_rebaselines(monkeypatch):
    """A genuine basis correction (current > 0) SHOULD re-baseline — the zero-guard
    must not swallow this case too."""
    stamped = []
    monkeypatch.setattr(stc, "_count_completed_goals", lambda: 300)
    monkeypatch.setattr(stc, "_wm_read",
                        lambda slot: {"goals_count_at_last_fire": 500})
    monkeypatch.setattr(stc, "_wm_set", lambda s, v: stamped.append((s, v)))
    fire, current, cfg, last = stc._cadence_gate()
    assert fire is False
    assert len(stamped) == 1
    assert stamped[0][1]["goals_count_at_last_fire"] == 300
    assert stamped[0][1]["rebaselined_from"] == 500


def test_cadence_fires_only_after_interval(monkeypatch):
    monkeypatch.setattr(stc, "_wm_read",
                        lambda slot: {"goals_count_at_last_fire": 1000})
    monkeypatch.setattr(stc, "_wm_set", lambda s, v: None)

    monkeypatch.setattr(stc, "_count_completed_goals", lambda: 1001)
    assert stc._cadence_gate()[0] is False

    cadence = stc._load_cadence_config()["goal_cadence"]
    monkeypatch.setattr(stc, "_count_completed_goals", lambda: 1000 + cadence)
    assert stc._cadence_gate()[0] is True


# ─────────────────────── subset-pair detector () ───────────────────────
#
# These pin the properties that make the subset slate trustworthy rather than
# merely present. The detector's whole value is that a zero from it MEANS
# something, so most of these assert on what it must NOT report.

def _g(gid, rule, *, source="s1", created="2026-01-01", amended=None,
       active=0, status="active"):
    """A guardrail-shaped row. Deliberately separate from _entry: guardrails key
    on source+created and carry `rule`, and reusing the title/created helper
    would silently exercise a shape the detector never sees in production."""
    r = {"id": gid, "status": status, "rule": rule, "source": source,
         "created": created,
         "utilization": {"times_active": active, "times_helpful": 0,
                         "times_cited": 0, "times_inferred_helpful": 0,
                         "retrieval_count": 0}}
    if amended:
        r["amended_fields"] = amended
    return r


def _pairs(rows, cap=25):
    return stc.subset_pairs(rows, ("source", "created"), "rule", cap)


def test_strict_prefix_is_detected_with_the_shorter_member_as_subset():
    out = _pairs([_g("a", "AAA"), _g("b", "AAABBB")])
    assert out["pairs_total"] == 1
    p = out["pairs"][0]
    assert p["subset_id"] == "a" and p["superset_id"] == "b"
    assert p["subset_chars"] == 3 and p["superset_chars"] == 6


def test_direction_does_not_depend_on_input_order():
    """The forking mechanism produces the stale copy at either id, so the probe
    must not infer direction from position (the goal's 18 live pairs run 15 one
    way and 3 the other)."""
    out = _pairs([_g("b", "AAABBB"), _g("a", "AAA")])
    assert out["pairs"][0]["subset_id"] == "a"


def test_identical_text_is_an_exact_duplicate_not_a_prefix_pair():
    """str.startswith is True in BOTH directions for equal strings, so without an
    explicit equality branch an exact duplicate would be reported twice as a
    prefix pair — and the remedy for a duplicate differs from that for a fork."""
    out = _pairs([_g("a", "SAME"), _g("b", "SAME")])
    assert out["pairs_total"] == 0
    assert out["exact_total"] == 1
    assert out["exact_duplicates"][0]["ids"] == ["a", "b"]


def test_same_text_in_a_different_group_is_not_a_pair():
    """Grouping is load-bearing, not decoration: two rails may legitimately share
    a prefix (a house phrasing) without being a fork of each other."""
    assert _pairs([_g("a", "AAA", source="s1"),
                   _g("b", "AAABBB", source="s2")])["pairs_total"] == 0
    assert _pairs([_g("a", "AAA", created="2026-01-01"),
                   _g("b", "AAABBB", created="2026-01-02")])["pairs_total"] == 0


def test_rows_missing_a_grouping_field_are_excluded_and_counted():
    """The manufactured-pair hazard: grouping on an absent field collapses every
    such row into ONE bucket, so two unrelated rails would be compared and could
    report a pair that shares nothing. Excluding them is correct — reporting the
    exclusion is what keeps the resulting number honest (guard-1760)."""
    out = _pairs([_g("a", "AAA", source=None), _g("b", "AAABBB", source=None),
                  _g("c", "CCC", created=None), _g("d", "")])
    assert out["pairs_total"] == 0
    assert out["ungroupable"] == 4


def test_zero_pairs_still_reports_its_denominators():
    """The goal's explicit check: an empty slate must be distinguishable from a
    probe that never ran. A bare 0 with no group counts cannot do that."""
    out = _pairs([_g("a", "AAA"), _g("b", "ZZZ")])
    assert out["pairs_total"] == 0 and out["exact_total"] == 0
    assert out["groups"] == 1 and out["multi_member_groups"] == 1
    assert out["group_fields"] == ["source", "created"]
    assert out["text_field"] == "rule"


def test_blind_spot_is_carried_in_the_result_not_only_in_a_comment():
    out = _pairs([_g("a", "AAA")])
    assert "byte-prefix" in out["blind_spot"]
    assert out["blind_spot"] == stc.SUBSET_BLIND_SPOT


def test_amended_flags_are_surfaced_for_both_members():
    """amended_fields is the usual discriminator for which member is stale — in
    all four twins named by the goal the subset has none and the superset carries
    a `rule` stamp. Surfacing it saves the reader re-opening both records; the
    probe still refuses to name a stale member, because it is not true by
    construction."""
    out = _pairs([_g("a", "AAA"),
                  _g("b", "AAABBB", amended={"rule": "2026-08-01T00:00:00"})])
    p = out["pairs"][0]
    assert p["subset_amended"] is False and p["superset_amended"] is True


def test_pairs_are_ordered_widest_gap_first_and_capped():
    rows = [_g("s1", "A", source="g1"), _g("l1", "A" * 500, source="g1"),
            _g("s2", "B", source="g2"), _g("l2", "B" * 100, source="g2")]
    out = _pairs(rows, cap=1)
    assert out["pairs_total"] == 2
    assert len(out["pairs"]) == 1
    assert out["pairs_truncated"] is True
    assert out["pairs"][0]["superset_id"] == "l1"


def test_untruncated_pair_list_is_not_flagged():
    out = _pairs([_g("a", "AAA"), _g("b", "AAABBB")], cap=25)
    assert out["pairs_truncated"] is False


def test_retired_entries_are_outside_the_scan(tmp_path):
    """A retired rail is already dispositioned; counting it would re-propose work
    that is done and inflate the slate every run thereafter."""
    p = _write_store(tmp_path, "g.jsonl", [
        _g("a", "AAA", status="retired"), _g("b", "AAABBB")])
    s = stc.corpus_stats(p, "guardrails", date(2026, 8, 1), 100, 30, 25)
    assert s["subset_pairs"]["pairs_total"] == 0


def test_corpus_stats_wires_the_scan_for_a_spec_backed_store(tmp_path):
    p = _write_store(tmp_path, "g.jsonl", [_g("a", "AAA"), _g("b", "AAABBB")])
    s = stc.corpus_stats(p, "guardrails", date(2026, 8, 1), 100, 30, 25)
    assert s["subset_pairs"]["pairs_total"] == 1


def test_store_without_a_spec_reports_none_not_an_empty_scan(tmp_path):
    """None and {} are different claims: 'not run here' vs 'run and found none'."""
    p = _write_store(tmp_path, "x.jsonl", [_g("a", "AAA")])
    s = stc.corpus_stats(p, "some-other-store", date(2026, 8, 1), 100, 30, 25)
    assert s["subset_pairs"] is None


def test_missing_store_carries_the_subset_key_too(tmp_path):
    """Both branches of corpus_stats must return the same keys — a shape that
    differs by branch is how half A once printed a verdict over empty metrics."""
    s = stc.corpus_stats(tmp_path / "nope.jsonl", "guardrails",
                         date(2026, 8, 1), 100, 30, 25)
    assert "subset_pairs" in s and s["subset_pairs"] is None


def test_reasoning_bank_spec_keys_on_the_populated_fields():
    """MEASURED, not stylistic: `source` is present on 74/7086 reasoning-bank rows
    and `source_goal` on 7086/7086. Keying RB on `source` for symmetry with
    guardrails would exclude 99% of the corpus and report a clean zero — the exact
    false-negative this detector exists to prevent (guard-1902)."""
    assert stc.SUBSET_SPEC["reasoning-bank"]["group_fields"] == ("source_goal", "created")
    assert stc.SUBSET_SPEC["reasoning-bank"]["text_field"] == "content"
    assert stc.SUBSET_SPEC["guardrails"]["group_fields"] == ("source", "created")
    assert stc.SUBSET_SPEC["guardrails"]["text_field"] == "rule"


def test_a_subset_pair_alone_is_signal():
    """Neither existing signal channel can see this class: the file surface is
    unchanged by a forked rail, and both members are heavily used so neither
    meets the dead-entry criterion."""
    result = {"file_surface": {"verdict": "flat"},
              "stores": [{"slate_total": 0,
                          "subset_pairs": {"pairs_total": 1, "exact_total": 0}}]}
    assert stc.has_signal(result) is True


def test_an_exact_duplicate_alone_is_signal():
    result = {"file_surface": {"verdict": "flat"},
              "stores": [{"slate_total": 0,
                          "subset_pairs": {"pairs_total": 0, "exact_total": 2}}]}
    assert stc.has_signal(result) is True


def test_clean_subset_scan_does_not_manufacture_signal():
    result = {"file_surface": {"verdict": "flat"},
              "stores": [{"slate_total": 0,
                          "subset_pairs": {"pairs_total": 0, "exact_total": 0}}]}
    assert stc.has_signal(result) is False


def test_has_signal_tolerates_a_store_with_no_subset_key():
    """The pre-existing callers in this file construct store dicts without the
    new key; has_signal must not raise on them."""
    result = {"file_surface": {"verdict": "flat"}, "stores": [{"slate_total": 0}]}
    assert stc.has_signal(result) is False


def test_render_prints_the_numbers_and_the_blind_spot(tmp_path):
    p = _write_store(tmp_path, "g.jsonl", [
        _g("a", "AAA"), _g("b", "AAABBB", amended={"rule": "x"})])
    s = stc.corpus_stats(p, "guardrails", date(2026, 8, 1), 100, 30, 25)
    text = stc.render({"file_surface": {"verdict": "flat", "metrics": {}},
                       "stores": [s]})
    assert "subset-pair scan: 1 prefix pair(s)" in text
    assert "a ⊂ b" in text
    assert "BLIND SPOT" in text


def test_render_says_not_run_rather_than_zero_for_an_unspecced_store(tmp_path):
    p = _write_store(tmp_path, "x.jsonl", [_g("a", "AAA")])
    s = stc.corpus_stats(p, "some-other-store", date(2026, 8, 1), 100, 30, 25)
    text = stc.render({"file_surface": {"verdict": "flat", "metrics": {}},
                       "stores": [s]})
    assert "NOT RUN for this store" in text
    assert "not a zero, an absence" in text
