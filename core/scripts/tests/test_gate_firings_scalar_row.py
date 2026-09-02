"""One bare JSON scalar in the gate-firings store must not break any consumer.

MEASURED 2026-08-30: `meta/gate-firings-2026-08-19.jsonl` line 1 is the single
character `7`. A bare int is VALID JSON, so `json.loads` does not raise and the
`except` clause every one of these loaders already had never fired; the very
next `rec.get("ts")` then died with `AttributeError`. That one row had taken
BOTH gate-telemetry tools down since 2026-08-19 — `gate-retirement-eval.py`
(the prescriptive evaluator) and `gate-stats.py` (the descriptive dashboard) —
each crashing on every invocation, i.e. eleven days of blind gate governance.

Why this test exists rather than three inline guards: the identical guard WAS
added to `override-ledger-consume.py`'s twin loader on 2026-08-29 and was not
swept to the other two consumers, because nothing encoded the invariant. An
unpinned fix in one of N copies is how the other N-1 stay broken (guard-1710).
So this file asserts the property for every consumer that walks the store, and
a new consumer should be added here rather than trusted to remember.

The write side is tested too, and it is the one that actually stops recurrence:
`gate-firings-flush.py::_parse_lossy` counted a line as "torn" only when it
FAILED to parse, so a torn append landing on a digit boundary produced a valid
scalar that the flush then appended into the shared store. Read-side guards are
defence in depth (guard-1512); the writer is the stop.

Every case carries a POSITIVE CONTROL — a well-formed record that must still
come back (guard-4166). Without it a loader that returned nothing at all would
pass, which is the failure this whole file is about.

Run:
    STORAGE_BACKEND=local python -m pytest \
        core/scripts/tests/test_gate_firings_scalar_row.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

GOOD = (
    '{"schema_version": 1, "ts": "2026-08-18T23:31:26", '
    '"gate_id": "hot-path-size-gate", "decision": "block", "agent": "alpha"}'
)

# Every JSON scalar shape a torn append can leave behind. `7` is the one
# actually measured in the live store; the others parse just as cleanly and
# would crash a `.get()` identically, so the guard is asserted against all of
# them rather than against the single row that happened to be found.
SCALARS = ["7", "0", "-1", '"a string"', "true", "null", "[1, 2]"]


def _load(name: str):
    """Load a hyphen-named script as an importable module object."""
    path = SCRIPT_DIR / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _since():
    return datetime(2026, 1, 1)


# --------------------------------------------------------------------------
# READ SIDE
# --------------------------------------------------------------------------

@pytest.mark.parametrize("scalar", SCALARS)
def test_gate_stats_loader_skips_a_bare_scalar(tmp_path, scalar):
    mod = _load("gate-stats.py")
    store = tmp_path / "gate-firings.jsonl"
    store.write_text(f"{scalar}\n{GOOD}\n", encoding="utf-8")

    recs = list(mod._load_records(store, _since()))

    assert len(recs) == 1, f"scalar {scalar!r} was not skipped: {recs!r}"
    assert recs[0]["gate_id"] == "hot-path-size-gate"  # positive control


@pytest.mark.parametrize("scalar", SCALARS)
def test_gate_retirement_eval_loader_skips_a_bare_scalar(tmp_path, scalar, monkeypatch):
    mod = _load("gate-retirement-eval.py")
    # _load_firings resolves the store through firings_paths(FIRINGS_JSONL.parent),
    # so pointing that constant at tmp_path redirects the whole walk.
    monkeypatch.setattr(mod, "FIRINGS_JSONL", tmp_path / "gate-firings.jsonl")
    (tmp_path / "gate-firings.jsonl").write_text(f"{scalar}\n{GOOD}\n", encoding="utf-8")

    recs = list(mod._load_firings(_since()))

    assert len(recs) == 1, f"scalar {scalar!r} was not skipped: {recs!r}"
    assert recs[0]["gate_id"] == "hot-path-size-gate"  # positive control


def test_the_measured_row_does_not_crash_either_reader(tmp_path, monkeypatch):
    """The literal live shape: a bare `7` as line 1, real records after it.

    Both readers crashed on exactly this file for eleven days, so it is worth
    one test that reproduces it verbatim rather than only the parametrised form.
    """
    body = "7\n" + GOOD + "\n" + GOOD.replace("23:31:26", "23:31:29") + "\n"

    stats = _load("gate-stats.py")
    store = tmp_path / "gate-firings-2026-08-19.jsonl"
    store.write_text(body, encoding="utf-8")
    assert len(list(stats._load_records(store, _since()))) == 2

    ev = _load("gate-retirement-eval.py")
    monkeypatch.setattr(ev, "FIRINGS_JSONL", tmp_path / "gate-firings.jsonl")
    assert len(list(ev._load_firings(_since()))) == 2


def test_a_future_dated_record_is_still_filtered_by_since(tmp_path):
    """The guard must skip non-records WITHOUT weakening the `since` window —
    a loader that started yielding everything would also pass the tests above.
    """
    mod = _load("gate-stats.py")
    store = tmp_path / "gate-firings.jsonl"
    store.write_text(f"7\n{GOOD}\n", encoding="utf-8")

    later = datetime(2026, 8, 18, 23, 31, 26) + timedelta(seconds=1)
    assert list(mod._load_records(store, later)) == []


# --------------------------------------------------------------------------
# WRITE SIDE — the stop that keeps the scalar out of the shared store
# --------------------------------------------------------------------------

@pytest.mark.parametrize("scalar", SCALARS)
def test_flush_counts_a_scalar_as_torn_and_does_not_carry_it(tmp_path, scalar):
    mod = _load("gate-firings-flush.py")
    spool = tmp_path / "gate-firings.spool.jsonl"
    spool.write_text(f"{scalar}\n{GOOD}\n", encoding="utf-8")

    records, torn = mod._parse_lossy(spool)

    assert torn == 1, f"scalar {scalar!r} was not counted torn"
    assert len(records) == 1, f"scalar {scalar!r} leaked into records: {records!r}"
    assert records[0]["gate_id"] == "hot-path-size-gate"  # positive control
    assert all(isinstance(r, dict) for r in records)


def test_flush_still_counts_genuinely_unparseable_lines_as_torn(tmp_path):
    """The scalar branch must ADD to the torn count, not replace the parse-error
    branch it sits beside — an interrupted append usually yields invalid JSON.
    """
    mod = _load("gate-firings-flush.py")
    spool = tmp_path / "gate-firings.spool.jsonl"
    spool.write_text('{"ts": "2026-08-18T23:3\n7\n' + GOOD + "\n", encoding="utf-8")

    records, torn = mod._parse_lossy(spool)

    assert torn == 2, f"expected both the truncated line and the scalar: torn={torn}"
    assert len(records) == 1
    assert records[0]["gate_id"] == "hot-path-size-gate"  # positive control
