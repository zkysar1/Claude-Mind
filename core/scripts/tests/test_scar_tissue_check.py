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
