"""Pins for the reflection_quality_log producer ().

THE LOAD-BEARING TEST IS `test_a_producer_for_reflection_quality_log_exists_at_all`.
Every other test here checks that the producer behaves; that one checks that it
EXISTS, which is the defect being fixed. `aspirations-execute/SKILL.md` described
this write for months, two downstream consumers were built against it, a
verification-checklist item was written for it -- and no code performed it. The
log sat at `[]` and `reflection_effectiveness_by_type` read total=0 on all three
types while 2,651 non-null `source_reflection_id` values accumulated on rb and
guardrail records.

WHY THE CHECKLIST NEVER CAUGHT IT, which is the reusable half: item 31 of
`core/config/verification-checklist.md` is prefixed "Runtime: IF any reasoning
bank entry was created AND later retrieved as helpful". With no producer the
IF-guard could never be satisfied, so the check SELF-SKIPPED rather than failed.
A conditional verification item cannot detect the absence of the thing its own
condition depends on. Hence an unconditional existence pin here.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "utilization_feedback", _SCRIPTS / "utilization-feedback.py")
uf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uf)


def _strategy(tmp_path, log=None, extra=None):
    """A reflection-strategy.yaml shaped like the live one."""
    data = {
        "reflection_quality_log": [] if log is None else log,
        "reflection_effectiveness_by_type": {
            "execution": {"effective": 0, "rate": 0.0, "total": 0},
            "hypothesis": {"effective": 0, "rate": 0.0, "total": 0},
            "spark": {"effective": 0, "rate": 0.0, "total": 0},
        },
        "some_other_key": {"must": "survive"},
    }
    if extra:
        data.update(extra)
    (tmp_path / "reflection-strategy.yaml").write_text(
        yaml.safe_dump(data), encoding="utf-8")
    return tmp_path / "reflection-strategy.yaml"


def _read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The defect: there was no producer.
# ---------------------------------------------------------------------------

def test_a_producer_for_reflection_quality_log_exists_at_all():
    """The whole goal. Do not weaken this to a behaviour check -- the failure
    was ABSENCE, and only an existence assertion catches absence."""
    assert hasattr(uf, "log_reflection_quality")
    assert callable(uf.log_reflection_quality)


def test_the_helpful_path_can_read_source_reflection_id():
    """The producer needs the field to key on; a reader that cannot see it
    would leave the log empty just as effectively as no writer at all."""
    assert hasattr(uf, "_source_reflection_id")
    assert callable(uf._source_reflection_id)


# ---------------------------------------------------------------------------
# The write.
# ---------------------------------------------------------------------------

def test_entries_are_appended_with_the_documented_three_field_schema(tmp_path, monkeypatch):
    """core/config/meta.yaml documents {reflection_id, downstream_goal, helpful}
    and verification-checklist item 31 pins 'no other schema'."""
    p = _strategy(tmp_path)
    monkeypatch.setattr(uf, "META_DIR", tmp_path)
    n = uf.log_reflection_quality(
        [{"reflection_id": "ref-a", "downstream_goal": "g-1-01", "helpful": True}])
    assert n == 1
    log = _read(p)["reflection_quality_log"]
    assert log == [{"reflection_id": "ref-a", "downstream_goal": "g-1-01", "helpful": True}]
    assert set(log[0]) == {"reflection_id", "downstream_goal", "helpful"}


def test_appending_preserves_every_other_key_untouched(tmp_path, monkeypatch):
    """A whole-file YAML rewrite is the obvious way to get this wrong."""
    p = _strategy(tmp_path)
    before = _read(p)
    monkeypatch.setattr(uf, "META_DIR", tmp_path)
    uf.log_reflection_quality(
        [{"reflection_id": "ref-a", "downstream_goal": "g-1-01", "helpful": True}])
    after = _read(p)
    assert set(before) == set(after)
    for k in before:
        if k != "reflection_quality_log":
            assert before[k] == after[k], f"{k} was mutated"


def test_entries_accumulate_across_calls(tmp_path, monkeypatch):
    """The value is the FORWARD signal building up over time; a writer that
    replaced instead of appending would keep the log permanently length-1 and
    the total>=3 depth gate would still never open."""
    p = _strategy(tmp_path)
    monkeypatch.setattr(uf, "META_DIR", tmp_path)
    for i in range(3):
        uf.log_reflection_quality(
            [{"reflection_id": f"ref-{i}", "downstream_goal": "g-1-01", "helpful": True}])
    assert len(_read(p)["reflection_quality_log"]) == 3


def test_a_batch_is_written_in_one_call(tmp_path, monkeypatch):
    p = _strategy(tmp_path)
    monkeypatch.setattr(uf, "META_DIR", tmp_path)
    n = uf.log_reflection_quality([
        {"reflection_id": "ref-a", "downstream_goal": "g-1-01", "helpful": True},
        {"reflection_id": "ref-b", "downstream_goal": "g-1-01", "helpful": True},
    ])
    assert n == 2
    assert len(_read(p)["reflection_quality_log"]) == 2


def test_the_log_is_capped_and_keeps_the_NEWEST_entries(tmp_path, monkeypatch):
    """Unbounded growth on a hot path (every non-routine goal) would eventually
    make the file unreadable. Keep the tail: recent signal is the useful one."""
    cap = uf._REFLECTION_QUALITY_LOG_CAP
    seed = [{"reflection_id": f"old-{i}", "downstream_goal": "g", "helpful": True}
            for i in range(cap)]
    p = _strategy(tmp_path, log=seed)
    monkeypatch.setattr(uf, "META_DIR", tmp_path)
    uf.log_reflection_quality(
        [{"reflection_id": "newest", "downstream_goal": "g", "helpful": True}])
    log = _read(p)["reflection_quality_log"]
    assert len(log) == cap
    assert log[-1]["reflection_id"] == "newest"
    assert log[0]["reflection_id"] == "old-1", "oldest entry should be the one dropped"


# ---------------------------------------------------------------------------
# Fail-soft: this feeds a soft meta-learning signal, never a gate.
# ---------------------------------------------------------------------------

def test_an_empty_batch_writes_nothing_and_does_not_raise(tmp_path, monkeypatch):
    p = _strategy(tmp_path)
    monkeypatch.setattr(uf, "META_DIR", tmp_path)
    assert uf.log_reflection_quality([]) == 0
    assert _read(p)["reflection_quality_log"] == []


def test_a_missing_strategy_file_is_a_noop_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(uf, "META_DIR", tmp_path)   # no file written
    assert uf.log_reflection_quality(
        [{"reflection_id": "ref-a", "downstream_goal": "g", "helpful": True}]) == 0


def test_an_unwritable_location_degrades_to_zero_rather_than_raising(tmp_path, monkeypatch):
    """Measured during implementation: writing outside a configured root is
    refused by the path guard. The producer must absorb that -- utilization
    feedback for the whole goal must not fail because a soft signal could not
    be recorded."""
    _strategy(tmp_path)
    monkeypatch.setattr(uf, "META_DIR", tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("not under any configured root")

    import _fileops
    monkeypatch.setattr(_fileops, "locked_modify_yaml", _boom)
    assert uf.log_reflection_quality(
        [{"reflection_id": "ref-a", "downstream_goal": "g", "helpful": True}]) == 0


def test_a_null_META_DIR_is_a_noop(monkeypatch):
    monkeypatch.setattr(uf, "META_DIR", None)
    assert uf.log_reflection_quality(
        [{"reflection_id": "ref-a", "downstream_goal": "g", "helpful": True}]) == 0


# ---------------------------------------------------------------------------
# The reader.
# ---------------------------------------------------------------------------

def _store(tmp_path, name, records):
    p = tmp_path / name
    import json
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


@pytest.mark.parametrize("fname,itype", [
    ("reasoning-bank.jsonl", "reasoning_bank"),
    ("guardrails.jsonl", "guardrail"),
])
def test_source_reflection_id_is_read_from_both_stores(tmp_path, monkeypatch, fname, itype):
    _store(tmp_path, fname, [{"id": "x-1", "source_reflection_id": "ref-42"}])
    monkeypatch.setattr(uf, "WORLD_DIR", tmp_path)
    assert uf._source_reflection_id("x-1", itype) == "ref-42"


def test_a_record_without_the_field_yields_None(tmp_path, monkeypatch):
    _store(tmp_path, "reasoning-bank.jsonl", [{"id": "x-1"}])
    monkeypatch.setattr(uf, "WORLD_DIR", tmp_path)
    assert uf._source_reflection_id("x-1", "reasoning_bank") is None


def test_an_empty_or_whitespace_field_yields_None_not_a_blank_key(tmp_path, monkeypatch):
    """A blank reflection_id would create log rows that can never be joined to
    a reflection -- worse than no row, because it inflates the total the
    depth gate reads."""
    _store(tmp_path, "reasoning-bank.jsonl",
           [{"id": "x-1", "source_reflection_id": "   "},
            {"id": "x-2", "source_reflection_id": ""}])
    monkeypatch.setattr(uf, "WORLD_DIR", tmp_path)
    assert uf._source_reflection_id("x-1", "reasoning_bank") is None
    assert uf._source_reflection_id("x-2", "reasoning_bank") is None


def test_a_non_string_field_yields_None(tmp_path, monkeypatch):
    _store(tmp_path, "reasoning-bank.jsonl", [{"id": "x-1", "source_reflection_id": 42}])
    monkeypatch.setattr(uf, "WORLD_DIR", tmp_path)
    assert uf._source_reflection_id("x-1", "reasoning_bank") is None


def test_an_unknown_item_type_yields_None(tmp_path, monkeypatch):
    monkeypatch.setattr(uf, "WORLD_DIR", tmp_path)
    assert uf._source_reflection_id("x-1", "experience") is None


def test_a_malformed_line_does_not_stop_the_scan(tmp_path, monkeypatch):
    p = tmp_path / "reasoning-bank.jsonl"
    p.write_text('{"id": "bad"\nnot json\n{"id": "x-1", "source_reflection_id": "ref-9"}\n',
                 encoding="utf-8")
    monkeypatch.setattr(uf, "WORLD_DIR", tmp_path)
    assert uf._source_reflection_id("x-1", "reasoning_bank") == "ref-9"


def test_a_missing_store_file_yields_None(tmp_path, monkeypatch):
    monkeypatch.setattr(uf, "WORLD_DIR", tmp_path)
    assert uf._source_reflection_id("x-1", "reasoning_bank") is None
