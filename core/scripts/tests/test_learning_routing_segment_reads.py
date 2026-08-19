"""learning-routing-audit must load the reasoning-bank and guardrail stores as
their ORDERED SEGMENT SET, through the backend (g-358-05).

WHY THIS FILE IS DIFFERENT FROM THE OTHER SEGMENT-READER PINS. The two readers
converted before this one (`mind_api/src/world/reasoning_bank.py` and
`core/scripts/retrieve.py`) are read-only: a short read there degrades retrieval
quality. THIS module's output feeds a WRITER THAT FIRES AUTOMATICALLY —
`learning-routing-repair.py --apply`, run by `tree.py::_post_remove_sweep_dangling`
after every tree-node removal — and that writer NULLS whatever the audit calls
dangling. So a record the loader fails to see does not merely go unreported: every
reference pointing AT it reads as dangling, and the repair destroys those
references.

That is not hypothetical. g-115-5646 reached exactly this state by a different
route (a depth-1 glob rather than a missing segment) and nulled **17,466 fields
over 13 days, 16,541 of them — 94.7% — valid.** These tests pin the direction of
every failure mode so a segment cannot reproduce it:

  UNREADABLE / UNRESOLVABLE  ->  fall back to the legacy file, NEVER to []
  BACKEND UNAVAILABLE        ->  direct read, NEVER a short corpus
  SEGMENT PRESENT            ->  read it, in chronological order

THE LOAD-BEARING ASSERTION IS THAT THE CORPUS IS NEVER SHORTER THAN THE LEGACY
FILE. A zero or short corpus is the silent-failure signature; it reads as "the
store is clean" and it is what makes the downstream writer destructive.

Mutation-verified: `test_mutation_control_a_legacy_only_loader_misses_the_segment`
reproduces the pre-change read inline and asserts it WOULD miss, so these pins are
known able to fail.
"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

AUDIT_PY = SCRIPTS / "learning-routing-audit.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load(AUDIT_PY, "_lr_segment_audit")

KINDS = [("reasoning-bank", "rb"), ("guardrails", "guard")]


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _rec(rid, status="active"):
    return {"id": rid, "status": status}


def _names(paths):
    return [p.name for p in paths]


# ---------------------------------------------------------------------------
# Byte-compat: today's world, where no writer has emitted a segment.
# ---------------------------------------------------------------------------

def test_byte_compat_when_only_the_legacy_file_exists(tmp_path):
    for kind, pfx in KINDS:
        legacy = tmp_path / f"{kind}.jsonl"
        _write(legacy, [_rec(f"{pfx}-1"), _rec(f"{pfx}-2")])
        assert _names(audit._segment_paths(legacy, kind)) == [f"{kind}.jsonl"]
        assert (audit._read_store_jsonl(legacy, kind, active_only=True)
                == audit._read_jsonl(legacy, active_only=True))


def test_the_live_tree_yields_a_nonzero_corpus():
    """The pin that would have caught a ship. A zero here is the exact input
    that makes the downstream repair destructive, and it reads as a clean store."""
    rb = audit.load_reasoning_bank()
    guard = audit.load_guardrails()
    if not audit.RB_JSONL.exists() and not audit.GUARDRAILS_JSONL.exists():
        import pytest
        pytest.skip("empty checkout — no content stores to read")
    assert len(rb) > 0, "reasoning bank loaded 0 active records — the destructive signature"
    assert len(guard) > 0, "guardrails loaded 0 active records — the destructive signature"


def test_loaders_are_never_shorter_than_the_legacy_file_alone():
    """The invariant stated as a direction rather than a count: whatever the
    segment machinery does, it may only ADD records. Holds for any future
    segment population, and a count assertion would not."""
    for loader, path in ((audit.load_reasoning_bank, audit.RB_JSONL),
                         (audit.load_guardrails, audit.GUARDRAILS_JSONL)):
        if not path.exists():
            continue
        assert len(loader()) >= len(audit._read_jsonl(path, active_only=True))


# ---------------------------------------------------------------------------
# The invariant: every segment, chronological, legacy first.
# ---------------------------------------------------------------------------

def test_segments_are_read_after_legacy_in_chronological_order(tmp_path):
    for kind, pfx in KINDS:
        legacy = tmp_path / f"{kind}.jsonl"
        _write(legacy, [_rec(f"{pfx}-legacy")])
        _write(tmp_path / f"{kind}-2026-09-02.jsonl", [_rec(f"{pfx}-sep02")])
        _write(tmp_path / f"{kind}-2026-08-31.jsonl", [_rec(f"{pfx}-aug31")])

        assert _names(audit._segment_paths(legacy, kind)) == [
            f"{kind}.jsonl", f"{kind}-2026-08-31.jsonl", f"{kind}-2026-09-02.jsonl"]
        got = [r["id"] for r in audit._read_store_jsonl(legacy, kind)]
        assert got == [f"{pfx}-legacy", f"{pfx}-aug31", f"{pfx}-sep02"]


def test_active_only_filters_across_segments_too(tmp_path):
    """load_guardrails passes active_only=True deliberately — an RB entry linked
    to a RETIRED guardrail is stale knowledge and MUST surface as dangling. If
    the filter stopped at the legacy file, a retired guardrail sitting in a
    segment would read as active and MASK real drift, which is the opposite
    error from the destructive one and just as wrong."""
    legacy = tmp_path / "guardrails.jsonl"
    _write(legacy, [_rec("guard-1")])
    _write(tmp_path / "guardrails-2026-08-31.jsonl",
           [_rec("guard-2"), _rec("guard-3", status="retired")])
    ids = [r["id"] for r in audit._read_store_jsonl(legacy, "guardrails", active_only=True)]
    assert ids == ["guard-1", "guard-2"]


def test_legacy_appears_exactly_once(tmp_path):
    """A duplicated legacy file would double every record — and in an id-keyed
    audit a duplicate id is not merely noise."""
    legacy = tmp_path / "reasoning-bank.jsonl"
    _write(legacy, [_rec("rb-1")])
    _write(tmp_path / "reasoning-bank-2026-08-31.jsonl", [_rec("rb-2")])
    assert _names(audit._segment_paths(legacy, "reasoning-bank")).count(
        "reasoning-bank.jsonl") == 1
    ids = [r["id"] for r in audit._read_store_jsonl(legacy, "reasoning-bank")]
    assert ids == ["rb-1", "rb-2"]


def test_archive_and_sidecar_are_excluded(tmp_path):
    """The archive holds RETIRED records. Folding it in would resurrect them as
    part of the corpus and mask the very drift load_guardrails exists to find."""
    legacy = tmp_path / "guardrails.jsonl"
    _write(legacy, [_rec("guard-live")])
    _write(tmp_path / "guardrails-archive.jsonl", [_rec("guard-retired")])
    _write(tmp_path / "guardrails-counters.jsonl", [{"id": "guard-live", "n": 1}])
    assert _names(audit._segment_paths(legacy, "guardrails")) == ["guardrails.jsonl"]


def test_kinds_do_not_bleed(tmp_path):
    rb = tmp_path / "reasoning-bank.jsonl"
    guard = tmp_path / "guardrails.jsonl"
    _write(rb, [_rec("rb-1")])
    _write(guard, [_rec("guard-1")])
    _write(tmp_path / "reasoning-bank-2026-08-31.jsonl", [_rec("rb-2")])
    _write(tmp_path / "guardrails-2026-08-31.jsonl", [_rec("guard-2")])
    assert [r["id"] for r in audit._read_store_jsonl(rb, "reasoning-bank")] == ["rb-1", "rb-2"]
    assert [r["id"] for r in audit._read_store_jsonl(guard, "guardrails")] == ["guard-1", "guard-2"]


# ---------------------------------------------------------------------------
# Every failure mode must fall toward the LEGACY read, never toward [].
# ---------------------------------------------------------------------------

def test_seam_returning_empty_still_yields_the_legacy_records(tmp_path, monkeypatch):
    """`store_paths` returns [] on an unresolvable base and omits the legacy file
    when it can see neither a local copy nor a backend listing. Here an empty
    corpus is not a quiet degradation — it makes every reference dangle and the
    automatic repair NULL them."""
    import _utilization_store
    legacy = tmp_path / "reasoning-bank.jsonl"
    _write(legacy, [_rec("rb-1")])
    monkeypatch.setattr(_utilization_store, "store_paths", lambda kind, base: [])
    assert audit._segment_paths(legacy, "reasoning-bank") == [legacy]
    assert [r["id"] for r in audit._read_store_jsonl(legacy, "reasoning-bank")] == ["rb-1"]


def test_seam_raising_still_yields_the_legacy_records(tmp_path, monkeypatch):
    import _utilization_store
    legacy = tmp_path / "guardrails.jsonl"
    _write(legacy, [_rec("guard-1")])

    def _boom(kind, base):
        raise RuntimeError("seam unavailable")

    monkeypatch.setattr(_utilization_store, "store_paths", _boom)
    assert audit._segment_paths(legacy, "guardrails") == [legacy]
    assert [r["id"] for r in audit._read_store_jsonl(legacy, "guardrails")] == ["guard-1"]


def test_backend_failure_degrades_to_the_direct_read(tmp_path, monkeypatch):
    """A backend hiccup must not shorten the corpus. `ensure_local` raising is
    the case where a naive implementation drops the path and reports fewer
    records — the destructive direction."""
    import storage_backend
    legacy = tmp_path / "reasoning-bank.jsonl"
    _write(legacy, [_rec("rb-1")])
    _write(tmp_path / "reasoning-bank-2026-08-31.jsonl", [_rec("rb-2")])

    class _Broken:
        def ensure_local(self, p):
            raise RuntimeError("backend down")

    monkeypatch.setattr(storage_backend, "get_backend", lambda: _Broken())
    ids = [r["id"] for r in audit._read_store_jsonl(legacy, "reasoning-bank")]
    assert ids == ["rb-1", "rb-2"], "a backend fault shortened the corpus"


def test_every_path_is_materialised_through_the_backend(tmp_path, monkeypatch):
    """Including the LEGACY file. Under own-cloud the local tree is a
    read-through cache, so an unmaterialised object simply does not exist
    locally and `_read_jsonl`'s `path.exists()` skips it in silence."""
    import storage_backend
    legacy = tmp_path / "guardrails.jsonl"
    _write(legacy, [_rec("guard-1")])
    _write(tmp_path / "guardrails-2026-08-31.jsonl", [_rec("guard-2")])
    seen = []

    class _Spy:
        def ensure_local(self, p):
            seen.append(Path(p).name)
            return p

    monkeypatch.setattr(storage_backend, "get_backend", lambda: _Spy())
    audit._read_store_jsonl(legacy, "guardrails")
    assert seen == ["guardrails.jsonl", "guardrails-2026-08-31.jsonl"]


def test_missing_store_reads_empty_without_raising(tmp_path):
    legacy = tmp_path / "reasoning-bank.jsonl"
    assert audit._read_store_jsonl(legacy, "reasoning-bank") == []


# ---------------------------------------------------------------------------
# Call-site pin: the loaders must go through the segmented reader.
# ---------------------------------------------------------------------------

def test_both_loaders_read_through_the_segmented_reader():
    """Guards the WIRING. A correct helper that nothing calls is the shape
    g-306-227 shipped (a fixed writer with no caller, tests green throughout —
    guard-1943), and here the un-wired state is the destructive one."""
    import inspect
    for fn_name, kind in (("load_reasoning_bank", "reasoning-bank"),
                          ("load_guardrails", "guardrails")):
        src = inspect.getsource(getattr(audit, fn_name))
        assert "_read_store_jsonl(" in src, f"{fn_name} does not call _read_store_jsonl"
        assert f'"{kind}"' in src, f"{fn_name} does not name kind {kind}"
        assert "_read_jsonl(RB_JSONL" not in src and "_read_jsonl(GUARDRAILS_JSONL" not in src


def test_other_stores_are_deliberately_untouched():
    """pipeline / pattern-signatures / tree are NOT part of this split, and
    quietly routing them through the segment reader would assert a store shape
    that does not exist. Pinned so a future 'consistency' edit is deliberate."""
    import inspect
    for fn_name in ("load_pipeline", "load_pattern_signatures"):
        src = inspect.getsource(getattr(audit, fn_name))
        assert "_read_store_jsonl" not in src, f"{fn_name} was routed through the segment reader"


# ---------------------------------------------------------------------------
# MUTATION CONTROL.
# ---------------------------------------------------------------------------

def test_mutation_control_a_legacy_only_loader_misses_the_segment(tmp_path):
    """Reproduces the pre-change read inline and asserts it WOULD miss — and
    states the consequence, which is what separates this from a style pin: the
    missed id's inbound references all read as dangling, and the automatic
    repair NULLS them."""
    legacy = tmp_path / "guardrails.jsonl"
    _write(legacy, [_rec("guard-legacy")])
    _write(tmp_path / "guardrails-2026-08-31.jsonl", [_rec("guard-in-segment")])

    old_ids = {r["id"] for r in audit._read_jsonl(legacy, active_only=True)}
    new_ids = {r["id"] for r in audit._read_store_jsonl(legacy, "guardrails", active_only=True)}

    assert old_ids == {"guard-legacy"}
    assert new_ids == {"guard-legacy", "guard-in-segment"}
    # The consequence, spelled out: under the old read, anything referencing
    # guard-in-segment resolves against a set that does not contain it.
    assert "guard-in-segment" not in old_ids
    assert old_ids != new_ids, "control drifted — the segment must be invisible to the old read"
