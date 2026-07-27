"""bulk-retire-dead-entries: retire bar must honor times_inferred_helpful ().

The retire bar previously checked (times_helpful + times_cited) == 0 and IGNORED
times_inferred_helpful -- the automatic retrieval-application backstop the rest of
the utilization system already counts (utility_ratio = (th + 0.5*tih)/rc). That
mass-retired heavily-retrieved-but-only-inferred-helpful entries (e.g. the live
rb-200, tih=8) as false-positive dead while the cohort tally approached the bar.
The fix adds times_inferred_helpful to the zero-check. These tests pin that an
inferred-attested entry is exempt while a genuinely-unattested one stays a
candidate, and that the pre-existing explicit/cited/retrieval-threshold behavior
is preserved.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bre = _load("bulk_retire_dead_entries_t", "bulk-retire-dead-entries.py")

TODAY = date(2026, 6, 21)
OLD = "2026-04-01T00:00:00"  # ~81 days before TODAY (> 60-day floor)


def _rec(rc=250, th=0, tc=0, tih=0, created=OLD, status="active",
         retirement_date=None):
    util = {
        "retrieval_count": rc,
        "times_helpful": th,
        "times_cited": tc,
        "times_inferred_helpful": tih,
    }
    rec = {"id": "rb-x", "status": status, "created": created,
           "utilization": util}
    if retirement_date:
        rec["retirement_date"] = retirement_date
    return rec


def _cand(rec):
    return bre._is_candidate(rec, min_retrievals=200, min_age_days=60,
                             today=TODAY)


def test_inferred_helpful_exempts_from_retirement():
    # rb-200-class: high retrieval, zero explicit/cited, tih=8 -> NOT a candidate.
    assert _cand(_rec(rc=250, th=0, tc=0, tih=8)) is False


def test_zero_all_signals_remains_candidate():
    # rb-227-class: high retrieval, zero on ALL helpful signals -> candidate.
    assert _cand(_rec(rc=250, th=0, tc=0, tih=0)) is True


def test_explicit_helpful_still_exempts():
    # regression: times_helpful > 0 still exempts (pre-fix behavior preserved).
    assert _cand(_rec(rc=250, th=2, tc=0, tih=0)) is False


def test_cited_still_exempts():
    # regression: times_cited > 0 still exempts.
    assert _cand(_rec(rc=250, th=0, tc=1, tih=0)) is False


def test_below_retrieval_threshold_not_candidate():
    # under the retrieval bar -> not a candidate regardless of helpful signals.
    assert _cand(_rec(rc=150, th=0, tc=0, tih=0)) is False


def test_inferred_one_is_enough_to_exempt():
    # boundary: a single inferred-helpful hit is enough to spare the entry.
    assert _cand(_rec(rc=999, th=0, tc=0, tih=1)) is False
