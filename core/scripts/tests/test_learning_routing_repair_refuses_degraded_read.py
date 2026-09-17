"""learning-routing-repair --apply refuses when a per-agent read fell back to the
local mirror (g-358-145).

A mirror can be SHORT of the store, and every experience missing from it makes a
valid experience_ref read as dangling, which --apply nulls. The refusal exits 3
so tree.py's post-remove sweep, which discards a clean run's stderr, prints it.
Each repair test wires the same fixture; the positive control proves that fixture
reaches the write step, so the refusal test cannot pass because nothing was ever
going to be written.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

AUDIT_PY = SCRIPTS / "learning-routing-audit.py"
REPAIR_PY = SCRIPTS / "learning-routing-repair.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Backend:
    def __init__(self, exc):
        self.exc = exc

    def read_authoritative_bytes(self, path):
        raise self.exc


# --- the audit records a degraded read, and only a degraded one --------------

def _read_with(monkeypatch, tmp_path, exc):
    import storage_backend

    audit = _load(AUDIT_PY, "lra_degraded_under_test")
    monkeypatch.setattr(storage_backend, "get_backend", lambda: _Backend(exc))
    p = tmp_path / "experience.jsonl"
    p.write_text('{"id": "exp-a"}\n', encoding="utf-8")
    records = audit._read_agent_jsonl_fresh(p)
    return audit, p, records


def test_failed_authoritative_read_is_recorded(monkeypatch, tmp_path):
    audit, p, records = _read_with(monkeypatch, tmp_path, RuntimeError("unreachable"))
    assert [r["id"] for r in records] == ["exp-a"]  # still reads the mirror
    assert audit.AUTHORITATIVE_READ_FALLBACKS == [str(p)]


def test_object_absent_from_store_is_not_degraded(monkeypatch, tmp_path):
    # This box's own unpushed file: the local copy is the superset.
    audit, _p, records = _read_with(monkeypatch, tmp_path, FileNotFoundError("absent"))
    assert [r["id"] for r in records] == ["exp-a"]
    assert audit.AUTHORITATIVE_READ_FALLBACKS == []


# --- the repair refuses to write on a degraded read ---------------------------

def _wire(monkeypatch, tmp_path, fallbacks, argv):
    repair = _load(REPAIR_PY, "lrr_degraded_under_test")
    a = repair.audit
    for loader in ("load_reasoning_bank", "load_guardrails", "load_pipeline",
                   "load_pattern_signatures", "load_all_experiences"):
        monkeypatch.setattr(a, loader, lambda: [])
    monkeypatch.setattr(a, "load_tree_node_keys", lambda: set())
    monkeypatch.setattr(a, "build_id_sets", lambda stores: {})
    dangling = [{"store": "reasoning_bank", "record_id": "rb-1",
                 "field": "experience_ref", "ref": "exp-only-in-the-store"}]
    monkeypatch.setattr(a, "audit_cross_refs", lambda s, i, t: (dangling, []))
    monkeypatch.setattr(a, "AUTHORITATIVE_READ_FALLBACKS", list(fallbacks))
    target = tmp_path / "reasoning-bank.jsonl"
    target.write_text('{"id": "rb-1"}\n', encoding="utf-8")
    monkeypatch.setattr(repair, "_resolve_store_path", lambda store, rid: target)
    calls = []

    def fake_repair_file(path, refs):
        calls.append((str(path), len(refs)))
        return list(refs), []
    monkeypatch.setattr(repair, "repair_file", fake_repair_file)
    # --apply appends to WORLD_DIR/.history: keep it off the real world store.
    monkeypatch.setattr(repair, "WORLD_DIR", tmp_path / "world")
    monkeypatch.setattr(sys, "argv", ["learning-routing-repair.py", *argv])
    return repair, calls


def test_apply_writes_when_every_read_was_authoritative(monkeypatch, tmp_path):
    repair, calls = _wire(monkeypatch, tmp_path, [], ["--apply"])  # POSITIVE CONTROL
    assert repair.main() == 0
    assert len(calls) == 1
    assert list((tmp_path / "world" / ".history").glob("learning-routing-repair-*.jsonl"))


def test_apply_refuses_when_a_read_fell_back(monkeypatch, tmp_path, capsys):
    repair, calls = _wire(monkeypatch, tmp_path, ["/agents/alpha/experience.jsonl"], ["--apply"])
    assert repair.main() == 3
    assert calls == []
    err = capsys.readouterr().err
    assert "REFUSED --apply" in err and "/agents/alpha/experience.jsonl" in err


def test_dry_run_warns_on_a_fallback(monkeypatch, tmp_path, capsys):
    repair, calls = _wire(monkeypatch, tmp_path, ["/agents/alpha/experience.jsonl"], [])
    repair.main()
    assert calls == []
    assert "WARNING" in capsys.readouterr().err
