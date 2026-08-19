"""Tests for the two-axis hypothesis-pipeline health report ().

The properties pinned here are the ones that carry real risk, not the arithmetic:

1. VACUOUS ZERO. An unreadable corpus must report `unmeasurable`, never 0%. For THIS
   metric a 0 is not a neutral failure value — it is the exact shape of a total
   capitalization collapse, so a silent zero would read as the most alarming possible
   result of a successful run (rb-245 / guard-1641).

2. LIVE-WINS PRECEDENCE. 337 ids exist in both pipeline.jsonl and pipeline-archive.jsonl
   on the live corpus. Without a fixed precedence the headline depends on which file was
   listed first, which is not a measurement. The choice is deliberate and is pinned so a
   refactor that reorders STORE_FILES fails loudly instead of shifting the number.

3. DEDUP BY ID. guard-3523: a naive line-union double-counts every overlapping record,
   which inflates the denominator and drags the rate toward the archive's composition.

4. THE UNDATED SUB-POPULATION IS ALWAYS REPORTED. This is the anti-guard-3524 measure
   and the reason the report exists in this shape: on the live corpus 22.1% of terminal
   records carry no outcome_date and capitalize 33.7 points worse than the dated cohort,
   so any dated trend read alone is biased. If a future edit drops this from the payload
   the report becomes the thing it was built to prevent.

5. A MISSING STORE FILE IS NAMED. Losing one file silently drops the denominator, which
   presents as a capitalization shift that never happened.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "hypothesis-capitalization.py"
_spec = importlib.util.spec_from_file_location("hypothesis_capitalization", _MOD_PATH)
hc = importlib.util.module_from_spec(_spec)
sys.modules["hypothesis_capitalization"] = hc
_spec.loader.exec_module(hc)


def _write(path: Path, records) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _rec(rid, outcome, outcome_date=None):
    r = {"id": rid, "outcome": outcome}
    if outcome_date:
        r["outcome_date"] = outcome_date
    return r


# --------------------------------------------------------------------------
# 1. Vacuous zero
# --------------------------------------------------------------------------

def test_empty_corpus_is_unmeasurable_not_zero(tmp_path, capsys):
    rc = hc.main(["--world-dir", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["verdict"] == "unmeasurable"
    assert "capitalization" not in out, "a failed read must not emit a rate at all"


def test_unmeasurable_says_undefined_not_zero(tmp_path, capsys):
    hc.main(["--world-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert "UNDEFINED, not 0" in captured.out
    assert "UNMEASURABLE" in captured.err, "the failure must reach stderr, not only JSON"


def test_load_corpus_reports_both_files_missing(tmp_path):
    records, diag = hc.load_corpus(tmp_path)
    assert records == {}
    assert sorted(diag["missing"]) == sorted(hc.STORE_FILES)


# --------------------------------------------------------------------------
# 2. Live-wins precedence  (MUTATION PROOF: fails if STORE_FILES is reordered)
# --------------------------------------------------------------------------

def test_live_wins_when_the_two_files_disagree(tmp_path):
    _write(tmp_path / "pipeline-archive.jsonl", [_rec("h-1", "UNRESOLVABLE")])
    _write(tmp_path / "pipeline.jsonl", [_rec("h-1", "CONFIRMED")])

    records, _ = hc.load_corpus(tmp_path)
    assert records["h-1"]["outcome"] == "CONFIRMED"

    result = hc.compute(records)
    assert result["capitalization"]["learned"] == 1
    assert result["capitalization"]["lost"] == 0


def test_store_files_order_is_archive_then_live():
    """The precedence above is produced by ORDER, so pin the order itself — a reorder
    would silently flip every disagreeing record rather than failing a rate assertion."""
    assert hc.STORE_FILES == ("pipeline-archive.jsonl", "pipeline.jsonl")


# --------------------------------------------------------------------------
# 3. Dedup by id
# --------------------------------------------------------------------------

def test_overlapping_ids_are_not_double_counted(tmp_path):
    shared = [_rec("h-%d" % i, "CONFIRMED") for i in range(5)]
    _write(tmp_path / "pipeline-archive.jsonl", shared)
    _write(tmp_path / "pipeline.jsonl", shared)

    records, diag = hc.load_corpus(tmp_path)
    assert len(records) == 5, "union must dedup by id"
    assert sum(f["rows"] for f in diag["files"]) == 10, "per-file rows stay raw counts"
    assert hc.compute(records)["corpus_total"] == 5


# --------------------------------------------------------------------------
# 4. The undated sub-population is always reported
# --------------------------------------------------------------------------

def test_undated_subpopulation_is_always_present(tmp_path):
    _write(tmp_path / "pipeline.jsonl", [
        _rec("d-1", "CONFIRMED", "2026-08-01"),
        _rec("u-1", "EXPIRED"),
    ])
    records, _ = hc.load_corpus(tmp_path)
    sub = hc.compute(records)["subpopulations"]

    assert sub["dated"]["n"] == 1
    assert sub["undated"]["n"] == 1
    assert sub["undated_share_pct"] == 50.0


def test_undated_records_are_excluded_from_by_month_but_not_the_headline(tmp_path):
    """The exact bias the caveat exists to expose: a dated trend of 100% over a
    population that actually capitalizes at 50%."""
    _write(tmp_path / "pipeline.jsonl", [
        _rec("d-1", "CONFIRMED", "2026-08-01"),
        _rec("u-1", "EXPIRED"),
    ])
    result = hc.compute(hc.load_corpus(tmp_path)[0])

    assert result["by_month"]["2026-08"]["capitalization_pct"] == 100.0
    assert result["capitalization"]["capitalization_pct"] == 50.0


def test_every_month_bucket_carries_its_denominator(tmp_path):
    """guard-3542 — a rate trended without its n cannot distinguish improvement from a
    collapsing sample."""
    _write(tmp_path / "pipeline.jsonl", [
        _rec("a", "CONFIRMED", "2026-07-02"),
        _rec("b", "EXPIRED", "2026-07-09"),
        _rec("c", "CORRECTED", "2026-08-03"),
    ])
    by_month = hc.compute(hc.load_corpus(tmp_path)[0])["by_month"]

    assert set(by_month) == {"2026-07", "2026-08"}
    for bucket in by_month.values():
        assert bucket["n"] == bucket["learned"] + bucket["lost"]


# --------------------------------------------------------------------------
# 5. A missing store file is named
# --------------------------------------------------------------------------

def test_missing_one_file_is_named_and_still_measures(tmp_path, capsys):
    _write(tmp_path / "pipeline.jsonl", [_rec("h-1", "CONFIRMED", "2026-08-01")])

    rc = hc.main(["--world-dir", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert "pipeline-archive.jsonl" in json.loads(captured.out)["diagnostics"]["missing"]
    assert "WARNING" in captured.err and "denominator is incomplete" in captured.err


# --------------------------------------------------------------------------
# Axis separation + hygiene
# --------------------------------------------------------------------------

def test_the_two_axes_have_different_denominators(tmp_path):
    """The whole premise: accuracy is blind to EXPIRED/UNRESOLVABLE, capitalization is
    not. If a refactor ever makes these denominators equal, the report has collapsed
    back into the single instrument it exists to supplement."""
    _write(tmp_path / "pipeline.jsonl", [
        _rec("a", "CONFIRMED"), _rec("b", "CORRECTED"),
        _rec("c", "EXPIRED"), _rec("d", "UNRESOLVABLE"), _rec("e", None),
    ])
    result = hc.compute(hc.load_corpus(tmp_path)[0])

    assert result["accuracy"]["verdicted"] == 2
    assert result["capitalization"]["terminal"] == 4
    assert result["capitalization"]["capitalization_pct"] == 50.0


def test_null_outcome_is_in_neither_axis(tmp_path):
    """An unresolved hypothesis has not terminated, so it belongs in no rate — counting
    it as lost would make every in-flight hypothesis look like a failure."""
    _write(tmp_path / "pipeline.jsonl", [_rec("a", "CONFIRMED"), _rec("n", None)])
    result = hc.compute(hc.load_corpus(tmp_path)[0])

    assert result["corpus_total"] == 2
    assert result["capitalization"]["terminal"] == 1
    assert result["capitalization"]["capitalization_pct"] == 100.0


def test_malformed_lines_are_counted_not_fatal(tmp_path):
    (tmp_path / "pipeline.jsonl").write_text(
        json.dumps(_rec("a", "CONFIRMED")) + "\n{not json\n\n"
        + json.dumps(["not", "a", "dict"]) + "\n"
        + json.dumps({"no_id": True}) + "\n", encoding="utf-8")

    records, diag = hc.load_corpus(tmp_path)
    assert list(records) == ["a"]
    assert diag["malformed_lines"] == 1


def test_pct_is_none_not_zero_on_an_empty_denominator():
    """Same hazard as the vacuous zero, one layer down."""
    assert hc._pct(0, 0) is None
    assert hc._pct(1, 2) == 50.0


def test_module_writes_nothing_to_audit_baselines():
    """The refusal is the deliverable — this report deliberately does NOT ratchet.
    Pinning it stops a future edit from wiring in the inverted baseline the module
    docstring rejects on three independent grounds."""
    source = _MOD_PATH.read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]  # strip the docstring, which discusses it at length
    assert "audit-baselines" not in code
    assert "locked_modify_yaml" not in code
