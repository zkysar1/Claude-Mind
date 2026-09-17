"""learning-routing-repair --apply refuses when a WORLD ref-target store read
degraded with no local copy (g-358-147).

reasoning_bank.source_hypothesis and experience.hypothesis_id resolve against the
pipeline id set, guardrails.related_patterns against pattern signatures, and
reasoning_bank.preventive_guardrail against guardrails. Before g-358-147 those
sets came from reads that degraded in silence: a failed backend or materialize
fell back to a local path that was not there, returned [], and recorded nothing,
so every valid ref into the store read as dangling and --apply would null it.

These tests run the REAL loaders and the real repair main. Only the backend is
faulted, and only the WRITE is observed (rb-10505: stubbing the degraded read
itself would certify the defect this file exists to catch).
"""
import importlib.util
import sys
from pathlib import Path

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
    """ensure_local raises `exc` for the paths in `failing`; identity otherwise."""

    def __init__(self, failing=(), exc=None):
        self.failing = {str(p) for p in failing}
        self.exc = exc

    def ensure_local(self, path):
        if str(path) in self.failing:
            raise self.exc
        return Path(path)


def _patch_backend(monkeypatch, backend=None, raises=None):
    import storage_backend

    if raises is not None:
        def _unavailable():
            raise raises
        monkeypatch.setattr(storage_backend, "get_backend", _unavailable)
    else:
        monkeypatch.setattr(storage_backend, "get_backend", lambda: backend)


# --- the loaders record a degraded read, and only a degraded one --------------

def test_materialize_failure_with_no_local_copy_is_recorded(monkeypatch, tmp_path):
    audit = _load(AUDIT_PY, "lra_world_fb_materialize")
    pipe = tmp_path / "pipeline.jsonl"  # not on local disk
    monkeypatch.setattr(audit, "PIPELINE_JSONL", pipe)
    _patch_backend(monkeypatch, _Backend([pipe], RuntimeError("store unreachable")))
    assert audit.load_pipeline() == []
    assert audit.AUTHORITATIVE_READ_FALLBACKS == [str(pipe)]


def test_unavailable_backend_with_no_local_copy_is_recorded(monkeypatch, tmp_path):
    audit = _load(AUDIT_PY, "lra_world_fb_nobackend")
    sigs = tmp_path / "pattern-signatures.jsonl"
    monkeypatch.setattr(audit, "SIGNATURES_JSONL", sigs)
    _patch_backend(monkeypatch, raises=RuntimeError("backend import failed"))
    assert audit.load_pattern_signatures() == []
    assert audit.AUTHORITATIVE_READ_FALLBACKS == [str(sigs)]


def test_object_absent_from_store_is_not_degraded(monkeypatch, tmp_path):
    # FileNotFoundError from ensure_local: the store holds no such object, so the
    # empty local read is the superset, exactly as in _read_agent_jsonl_fresh.
    audit = _load(AUDIT_PY, "lra_world_fb_absent")
    pipe = tmp_path / "pipeline.jsonl"
    monkeypatch.setattr(audit, "PIPELINE_JSONL", pipe)
    _patch_backend(monkeypatch, _Backend([pipe], FileNotFoundError("no such object")))
    assert audit.load_pipeline() == []
    assert audit.AUTHORITATIVE_READ_FALLBACKS == []


def test_failure_with_a_local_copy_reads_it_and_records_the_fallback(monkeypatch, tmp_path):
    # : a PRESENT copy may be stale, so a failed freshness check over it
    # is still a degraded read. The local records are served, and the read is recorded.
    audit = _load(AUDIT_PY, "lra_world_fb_localcopy")
    pipe = tmp_path / "pipeline.jsonl"
    pipe.write_text('{"id": "2026-01-01_kept"}\n', encoding="utf-8")
    monkeypatch.setattr(audit, "PIPELINE_JSONL", pipe)
    _patch_backend(monkeypatch, _Backend([pipe], RuntimeError("store unreachable")))
    assert [r["id"] for r in audit.load_pipeline()] == ["2026-01-01_kept"]
    assert audit.AUTHORITATIVE_READ_FALLBACKS == [str(pipe)]


# --- the repair refuses on it, through the real loaders ------------------------

def _wire(monkeypatch, tmp_path, with_pipeline, backend):
    world = tmp_path / "world"
    world.mkdir()
    rb = world / "reasoning-bank.jsonl"
    rb.write_text(
        '{"id": "rb-1", "status": "active", "source_hypothesis": "2026-01-01_real-hyp"}\n'
        '{"id": "rb-2", "status": "active", "source_hypothesis": "2026-01-02_gone-hyp"}\n',
        encoding="utf-8")
    (world / "guardrails.jsonl").write_text("", encoding="utf-8")
    (world / "pattern-signatures.jsonl").write_text("", encoding="utf-8")
    pipe = world / "pipeline.jsonl"
    if with_pipeline:
        pipe.write_text('{"id": "2026-01-01_real-hyp"}\n', encoding="utf-8")

    repair = _load(REPAIR_PY, "lrr_world_fb_%s" % ("healthy" if with_pipeline else "cold"))
    a = repair.audit
    monkeypatch.setattr(a, "RB_JSONL", rb)
    monkeypatch.setattr(a, "GUARDRAILS_JSONL", world / "guardrails.jsonl")
    monkeypatch.setattr(a, "PIPELINE_JSONL", pipe)
    monkeypatch.setattr(a, "SIGNATURES_JSONL", world / "pattern-signatures.jsonl")
    # The per-agent experience corpus and the tree read this box's live dirs and
    # are not the stores under test, so they stay empty.
    monkeypatch.setattr(a, "load_all_experiences", lambda: [])
    monkeypatch.setattr(a, "load_tree_node_keys", lambda: set())
    _patch_backend(monkeypatch, backend)
    monkeypatch.setattr(repair, "_resolve_store_path", lambda store, rid: rb)
    calls = []

    def observe_write(path, refs):
        calls.append(sorted(d["record_id"] for d in refs))
        return list(refs), []
    monkeypatch.setattr(repair, "repair_file", observe_write)
    # --apply appends to WORLD_DIR/.history: keep it off the real world (guard-2102).
    monkeypatch.setattr(repair, "WORLD_DIR", tmp_path / "hist")
    monkeypatch.setattr(sys, "argv", ["learning-routing-repair.py", "--apply"])
    return repair, calls, rb, pipe


def test_healthy_world_read_still_applies(monkeypatch, tmp_path):
    # POSITIVE CONTROL: the same fixture reaches the write step, so the refusal
    # below cannot pass merely because nothing was ever going to be written.
    repair, calls, _rb, _pipe = _wire(monkeypatch, tmp_path, True, _Backend())
    assert repair.main() == 0
    assert calls == [["rb-2"]]  # only the genuinely dangling ref
    assert list((tmp_path / "hist" / ".history").glob("learning-routing-repair-*.jsonl"))


def test_apply_refuses_when_the_pipeline_read_degraded(monkeypatch, tmp_path, capsys):
    backend = _Backend([tmp_path / "world" / "pipeline.jsonl"], RuntimeError("store unreachable"))
    repair, calls, rb, pipe = _wire(monkeypatch, tmp_path, False, backend)
    before = rb.read_bytes()
    assert repair.main() == 3
    assert calls == []
    assert rb.read_bytes() == before
    assert not (tmp_path / "hist").exists()
    err = capsys.readouterr().err
    assert "REFUSED --apply" in err and str(pipe) in err


def test_apply_refuses_when_a_present_but_stale_pipeline_copy_failed_to_refresh(
        monkeypatch, tmp_path, capsys):
    #  (bravo's cc-05 probe): the local pipeline copy EXISTS but is stale
    # (it lacks rb-1's valid target) and ensure_local raises. Before the fix nothing
    # was recorded, so --apply nulled the valid rb-1 ref along with rb-2.
    backend = _Backend([tmp_path / "world" / "pipeline.jsonl"], RuntimeError("store unreachable"))
    repair, calls, rb, pipe = _wire(monkeypatch, tmp_path, False, backend)
    pipe.write_text("", encoding="utf-8")  # present, stale
    before = rb.read_bytes()
    assert repair.main() == 3
    assert calls == []
    assert rb.read_bytes() == before
    assert not (tmp_path / "hist").exists()
    assert "REFUSED --apply" in capsys.readouterr().err
