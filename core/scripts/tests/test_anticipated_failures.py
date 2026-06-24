"""test_anticipated_failures.py — anticipated-failures store engine ( Phase B).

Covers the Phase 3.96 anticipatory-reflection store (core/scripts/anticipated-failures.py):
validation, add (+dedup-on-goal_id), read, update-outcome, and the argparse CLI
round-trip. Hermetic by construction — the engine exposes a `path=None` parameter
on every library function, so tests pass an explicit tmp_path store and never touch
the real agent dir. The CLI tests monkeypatch `store_path` + `sys.stdin`.

Design spec: world/conventions/anticipated-failures.md (section 2 = the store schema).
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load_engine():
    """Load the hyphenated engine module via importlib spec (name has a dash)."""
    spec = importlib.util.spec_from_file_location(
        "anticipated_failures", CORE_SCRIPTS / "anticipated-failures.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["anticipated_failures"] = mod
    spec.loader.exec_module(mod)
    return mod


af = _load_engine()


@pytest.fixture
def store(tmp_path):
    """A tmp store file (parent exists; file created on first write)."""
    return tmp_path / "anticipated-failures.jsonl"


def _valid_record(goal_id="g-test-01", n_modes=1):
    modes = [
        {
            "id": "af-%d" % i,
            "mode": "regression-%d" % i,
            "why": "core-loop edit",
            "signal": "pytest test_%d fails" % i,
        }
        for i in range(1, n_modes + 1)
    ]
    return {
        "goal_id": goal_id,
        "aspiration_id": "asp-test",
        "category": "framework-architecture",
        "estimated_depth": "deep",
        "anticipated": modes,
    }


# --------------------------------------------------------------------------- #
# validate_record
# --------------------------------------------------------------------------- #


def test_validate_accepts_valid_record():
    assert af.validate_record(_valid_record()) == []


def test_validate_accepts_max_modes():
    assert af.validate_record(_valid_record(n_modes=af.MAX_ANTICIPATED)) == []


def test_validate_rejects_non_dict():
    errs = af.validate_record(["not", "a", "dict"])
    assert errs and "object" in errs[0]


def test_validate_rejects_missing_goal_id():
    rec = _valid_record()
    del rec["goal_id"]
    errs = af.validate_record(rec)
    assert any("goal_id" in e for e in errs)


def test_validate_rejects_empty_anticipated():
    rec = _valid_record()
    rec["anticipated"] = []
    errs = af.validate_record(rec)
    assert any("non-empty" in e for e in errs)


def test_validate_rejects_too_many_modes():
    rec = _valid_record(n_modes=af.MAX_ANTICIPATED + 1)
    errs = af.validate_record(rec)
    assert any("at most" in e for e in errs)


def test_validate_rejects_mode_missing_required_field():
    rec = _valid_record()
    del rec["anticipated"][0]["signal"]
    errs = af.validate_record(rec)
    assert any("signal" in e for e in errs)


def test_validate_mitigation_is_optional():
    rec = _valid_record()
    rec["anticipated"][0]["mitigation"] = "add a guard test"
    assert af.validate_record(rec) == []
    # And absence of mitigation is still valid (the default record omits it).
    assert "mitigation" not in _valid_record()["anticipated"][0]


# --------------------------------------------------------------------------- #
# add_entry
# --------------------------------------------------------------------------- #


def test_add_entry_appends_and_defaults(store):
    rec = af.add_entry(_valid_record("g-add-01"), path=store)
    assert rec["outcome"] is None
    assert "anticipated_at" in rec
    on_disk = af.read_entry("g-add-01", path=store)
    assert on_disk is not None
    assert on_disk["goal_id"] == "g-add-01"
    assert on_disk["outcome"] is None


def test_add_entry_preserves_provided_anticipated_at(store):
    rec = _valid_record("g-add-02")
    rec["anticipated_at"] = "2026-06-14T09:00:00"
    out = af.add_entry(rec, path=store)
    assert out["anticipated_at"] == "2026-06-14T09:00:00"


def test_add_entry_dedups_on_goal_id(store):
    af.add_entry(_valid_record("g-dup-01"), path=store)
    with pytest.raises(ValueError, match="already exists"):
        af.add_entry(_valid_record("g-dup-01"), path=store)
    # Only one record persisted.
    items = af._fileops.read_jsonl_with_recovery(store)
    assert len([r for r in items if r.get("goal_id") == "g-dup-01"]) == 1


def test_add_entry_rejects_invalid(store):
    bad = _valid_record("g-bad-01")
    del bad["anticipated"]
    with pytest.raises(ValueError):
        af.add_entry(bad, path=store)
    # Nothing written.
    assert af.read_entry("g-bad-01", path=store) is None


def test_add_entry_two_distinct_goals_coexist(store):
    af.add_entry(_valid_record("g-co-01"), path=store)
    af.add_entry(_valid_record("g-co-02"), path=store)
    assert af.read_entry("g-co-01", path=store)["goal_id"] == "g-co-01"
    assert af.read_entry("g-co-02", path=store)["goal_id"] == "g-co-02"


# --------------------------------------------------------------------------- #
# read_entry
# --------------------------------------------------------------------------- #


def test_read_entry_missing_file_returns_none(store):
    assert not store.exists()
    assert af.read_entry("g-none", path=store) is None


def test_read_entry_absent_goal_returns_none(store):
    af.add_entry(_valid_record("g-present"), path=store)
    assert af.read_entry("g-absent", path=store) is None


# --------------------------------------------------------------------------- #
# update_outcome
# --------------------------------------------------------------------------- #


def test_update_outcome_sets_outcome(store):
    af.add_entry(_valid_record("g-up-01"), path=store)
    outcome = {
        "executed_at": "2026-06-14T10:00:00",
        "errors_observed": [],
        "hits": [],
        "misses": ["af-1"],
        "surprises": [],
        "anticipation_score": 1.0,
        "clean_success": True,
    }
    rec = af.update_outcome("g-up-01", outcome, path=store)
    assert rec["outcome"]["clean_success"] is True
    # Persisted.
    assert af.read_entry("g-up-01", path=store)["outcome"]["anticipation_score"] == 1.0


def test_update_outcome_missing_goal_raises_keyerror(store):
    af.add_entry(_valid_record("g-up-02"), path=store)
    with pytest.raises(KeyError):
        af.update_outcome("g-nope", {"executed_at": "x"}, path=store)


def test_update_outcome_rejects_non_dict(store):
    af.add_entry(_valid_record("g-up-03"), path=store)
    with pytest.raises(ValueError, match="object"):
        af.update_outcome("g-up-03", ["not", "a", "dict"], path=store)


# --------------------------------------------------------------------------- #
# CLI (main) round-trip
# --------------------------------------------------------------------------- #


def _run_cli(monkeypatch, capsys, store_file, argv, stdin_str=None):
    monkeypatch.setattr(af, "store_path", lambda: store_file)
    if stdin_str is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_str))
    rc = af.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_cli_add_read_update_roundtrip(store, monkeypatch, capsys):
    # add
    rc, out, _ = _run_cli(
        monkeypatch, capsys, store, ["add"],
        stdin_str=json.dumps(_valid_record("g-cli-01")),
    )
    assert rc == 0
    added = json.loads(out)
    assert added["goal_id"] == "g-cli-01" and added["outcome"] is None

    # read present
    rc, out, _ = _run_cli(monkeypatch, capsys, store, ["read", "g-cli-01"])
    assert rc == 0
    assert json.loads(out)["goal_id"] == "g-cli-01"

    # update
    outcome = {"executed_at": "2026-06-14T11:00:00", "errors_observed": [],
               "hits": [], "misses": [], "surprises": [],
               "anticipation_score": 1.0, "clean_success": True}
    rc, out, _ = _run_cli(
        monkeypatch, capsys, store, ["update", "g-cli-01"],
        stdin_str=json.dumps(outcome),
    )
    assert rc == 0
    assert json.loads(out)["outcome"]["clean_success"] is True

    # read reflects the outcome
    rc, out, _ = _run_cli(monkeypatch, capsys, store, ["read", "g-cli-01"])
    assert json.loads(out)["outcome"]["anticipation_score"] == 1.0


def test_cli_read_absent_prints_null(store, monkeypatch, capsys):
    rc, out, _ = _run_cli(monkeypatch, capsys, store, ["read", "g-missing"])
    assert rc == 0
    assert out.strip() == "null"


def test_cli_add_duplicate_exits_1(store, monkeypatch, capsys):
    payload = json.dumps(_valid_record("g-cli-dup"))
    rc, _, _ = _run_cli(monkeypatch, capsys, store, ["add"], stdin_str=payload)
    assert rc == 0
    rc, _, err = _run_cli(monkeypatch, capsys, store, ["add"], stdin_str=payload)
    assert rc == 1
    assert "invalid_input" in err


def test_cli_add_invalid_exits_1(store, monkeypatch, capsys):
    rc, _, err = _run_cli(
        monkeypatch, capsys, store, ["add"],
        stdin_str=json.dumps({"anticipated": []}),  # missing goal_id + empty modes
    )
    assert rc == 1
    assert "invalid_input" in err


def test_cli_update_absent_exits_2(store, monkeypatch, capsys):
    af.add_entry(_valid_record("g-cli-present"), path=store)
    rc, _, err = _run_cli(
        monkeypatch, capsys, store, ["update", "g-cli-missing"],
        stdin_str=json.dumps({"executed_at": "x"}),
    )
    assert rc == 2
    assert "not_found" in err
