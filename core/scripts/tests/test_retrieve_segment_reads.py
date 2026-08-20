"""test_retrieve_segment_reads.py — retrieve.py reads the reasoning-bank and
guardrail stores as their ORDERED SEGMENT SET, not as a single legacy file
(g-358-05, unit 5).

WHY THIS FILE EXISTS. `retrieve.py` is the retrieval scorer behind every
`/prime` and `retrieve.sh` call, on BOTH the CLI and the daemon path, so it is
the highest-traffic reader of the two content stores that g-358-05 is splitting
into date-shaped segments. Until this unit it read `read_jsonl(RB_PATH)` /
`read_jsonl(GUARD_PATH)` directly, so the first segment a writer emitted would
have been invisible to it — and an invisible guardrail segment does not read as
an error, it reads as "those guardrails do not apply". That is the worst
available failure direction and it is silent, which is why the reader lands
before the writer.

WHAT IS PINNED, and why it is the invariant rather than the current output: not
"the resolver returns one file" (true today, and a hardcoded single-file
resolver would satisfy it) but "every segment of the store is read, in
chronological order, from the base the CALLER named". That holds for any
segment population, including ones no writer has produced yet.

`test_base_comes_from_the_caller_path_not_world_dir` pins the daemon path: the
retrieve endpoint monkeypatches `_r.RB_PATH` / `_r.GUARD_PATH` to the PER-REQUEST
world (endpoints/retrieve.py:295-296), so a resolver deriving its base from the
module-level WORLD_DIR would silently serve the DAEMON's world on every request.

MUTATION-VERIFIED, four mutations, each measured rather than predicted:

| # | mutation | caught by |
|---|---|---|
| M1 | both call sites reverted to `read_jsonl(<LEGACY>)` | 1 — `test_both_loaders_read_through_the_segmented_reader` ALONE |
| M2 | base derived from module WORLD_DIR, not `path.parent` | 9 |
| M3 | legacy-path pin removed from the resolver | 2 — both fail-safe tests, exactly |
| M4 | `read_jsonl` captured at def time (default arg) | 1 — the module-global pin ALONE |

Two of those corrected an expectation written before the run, and both
corrections are the reason the table is here rather than a sentence:

* This docstring first claimed a WORLD_DIR-derived resolver "would keep passing
  every other test in this file". It fails NINE — the tmp-dir fixtures all break
  once the base stops following the caller. The daemon pin is still the only one
  that names the failure, but it is not the only one that catches it.
* M4 was first written as `_rj = read_jsonl` inside the function BODY and all 13
  tests passed — because a body-local assignment still resolves the global at
  CALL time, so that mutant was not the defect it looked like. Only a default
  argument captures at def time. A mutation that does not mutate reads as a
  missing test; check what the mutant actually changed before believing it.

M1 catching exactly one test is by design and worth reading twice: the helper
keeps working when nothing calls it, which is the shape g-306-227 shipped (a
fixed writer with no caller, tests green throughout — guard-1943). The wiring
pin is the only thing standing between this unit and that outcome.

Pure stdlib + PyYAML. Self-contained: never touches the live world directory.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# retrieve.py imports `from _paths import ...` at module load — _paths reads
# MIND_WORLD env. Set it to a temp dir BEFORE the import so the module binds to
# scratch paths, not the live world. Capture-restore pattern ().
#
# STORAGE_BACKEND is pinned local for the same window and for a reason specific
# to this file: `_utilization_store.store_paths` enumerates BACKEND-FIRST, so
# under own-cloud its listing would return names from the REAL world prefix and
# leak them into assertions about a tmp directory. guard-955.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_ORIG_BACKEND = os.environ.get("STORAGE_BACKEND")

_TMPDIR = tempfile.mkdtemp(prefix="retrieve-segment-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)
os.environ["STORAGE_BACKEND"] = "local"

import importlib.util  # noqa: E402

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_seg_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

# Restore env so downstream tests inherit clean conftest defaults. STORAGE_BACKEND
# stays local for the whole session if it was already unset — the pin below in
# each test body is what actually matters, this is only tidiness.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT
if _ORIG_BACKEND is not None:
    os.environ["STORAGE_BACKEND"] = _ORIG_BACKEND

KINDS = [("reasoning-bank", "rb"), ("guardrails", "guard")]


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _rec(rid):
    return {"id": rid, "status": "active", "category": "test"}


def _names(paths):
    return [p.name for p in paths]


# ---------------------------------------------------------------------------
# Byte-compat: today's world, where no writer has emitted a segment.
# ---------------------------------------------------------------------------

def test_byte_compat_when_only_the_legacy_file_exists(tmp_path):
    """The whole change must be a no-op until a writer emits segments."""
    for kind, pfx in KINDS:
        legacy = tmp_path / f"{kind}.jsonl"
        recs = [_rec(f"{pfx}-1"), _rec(f"{pfx}-2")]
        _write_jsonl(legacy, recs)

        assert _names(_retrieve._store_paths(legacy, kind)) == [f"{kind}.jsonl"]
        assert _retrieve._read_store(legacy, kind) == _retrieve.read_jsonl(legacy)
        assert _retrieve._read_store(legacy, kind) == recs


def test_missing_legacy_file_reads_empty_without_raising(tmp_path):
    """A store that does not exist yet is empty, not an exception. The legacy
    path is still PINNED into the resolver output — read_jsonl returns [] for a
    missing file, so pinning costs nothing and keeps the shape uniform."""
    for kind, _ in KINDS:
        legacy = tmp_path / f"{kind}.jsonl"
        assert _names(_retrieve._store_paths(legacy, kind)) == [f"{kind}.jsonl"]
        assert _retrieve._read_store(legacy, kind) == []


# ---------------------------------------------------------------------------
# The invariant: every segment, chronological, legacy first.
# ---------------------------------------------------------------------------

def test_segments_are_read_after_legacy_in_chronological_order(tmp_path):
    for kind, pfx in KINDS:
        legacy = tmp_path / f"{kind}.jsonl"
        _write_jsonl(legacy, [_rec(f"{pfx}-legacy")])
        # Written out of order on purpose — ordering must come from the DATE in
        # the name, never from directory or creation order.
        _write_jsonl(tmp_path / f"{kind}-2026-09-02.jsonl", [_rec(f"{pfx}-sep02")])
        _write_jsonl(tmp_path / f"{kind}-2026-08-31.jsonl", [_rec(f"{pfx}-aug31")])
        _write_jsonl(tmp_path / f"{kind}-2026-09-01.jsonl", [_rec(f"{pfx}-sep01")])

        assert _names(_retrieve._store_paths(legacy, kind)) == [
            f"{kind}.jsonl",
            f"{kind}-2026-08-31.jsonl",
            f"{kind}-2026-09-01.jsonl",
            f"{kind}-2026-09-02.jsonl",
        ]
        assert [r["id"] for r in _retrieve._read_store(legacy, kind)] == [
            f"{pfx}-legacy", f"{pfx}-aug31", f"{pfx}-sep01", f"{pfx}-sep02",
        ]


def test_legacy_path_appears_exactly_once(tmp_path):
    """The resolver PINS the legacy path and the seam also enumerates it when it
    can see it. Both happening must not double-read the store — a duplicated
    legacy file would silently double every counter and every record."""
    for kind, pfx in KINDS:
        legacy = tmp_path / f"{kind}.jsonl"
        _write_jsonl(legacy, [_rec(f"{pfx}-1")])
        _write_jsonl(tmp_path / f"{kind}-2026-08-31.jsonl", [_rec(f"{pfx}-2")])

        paths = _retrieve._store_paths(legacy, kind)
        assert _names(paths).count(f"{kind}.jsonl") == 1
        ids = [r["id"] for r in _retrieve._read_store(legacy, kind)]
        assert ids == sorted(set(ids), key=ids.index), f"duplicate reads: {ids}"


def test_archive_and_counter_sidecar_are_excluded(tmp_path):
    """Neither is date-shaped, and folding either in would be a correctness bug
    rather than noise: the archive holds RETIRED records (they would reappear as
    active) and the sidecar holds counters, not records."""
    for kind, pfx in KINDS:
        legacy = tmp_path / f"{kind}.jsonl"
        _write_jsonl(legacy, [_rec(f"{pfx}-live")])
        _write_jsonl(tmp_path / f"{kind}-archive.jsonl", [_rec(f"{pfx}-retired")])
        _write_jsonl(tmp_path / f"{kind}-counters.jsonl", [{"id": f"{pfx}-live", "n": 1}])
        # A near-miss that is prefix-shaped but not a date.
        _write_jsonl(tmp_path / f"{kind}-scratch.jsonl", [_rec(f"{pfx}-scratch")])

        names = _names(_retrieve._store_paths(legacy, kind))
        assert names == [f"{kind}.jsonl"], names
        assert [r["id"] for r in _retrieve._read_store(legacy, kind)] == [f"{pfx}-live"]


def test_kinds_do_not_bleed_into_each_other(tmp_path):
    """`guardrails-*` must never be read as part of the reasoning bank. Both
    stems live in the same directory, so a loose glob would merge two corpora."""
    rb_legacy = tmp_path / "reasoning-bank.jsonl"
    guard_legacy = tmp_path / "guardrails.jsonl"
    _write_jsonl(rb_legacy, [_rec("rb-1")])
    _write_jsonl(guard_legacy, [_rec("guard-1")])
    _write_jsonl(tmp_path / "reasoning-bank-2026-08-31.jsonl", [_rec("rb-2")])
    _write_jsonl(tmp_path / "guardrails-2026-08-31.jsonl", [_rec("guard-2")])

    rb_ids = [r["id"] for r in _retrieve._read_store(rb_legacy, "reasoning-bank")]
    guard_ids = [r["id"] for r in _retrieve._read_store(guard_legacy, "guardrails")]
    assert rb_ids == ["rb-1", "rb-2"]
    assert guard_ids == ["guard-1", "guard-2"]


# ---------------------------------------------------------------------------
# The daemon pin. This is the one a WORLD_DIR-derived resolver fails alone.
# ---------------------------------------------------------------------------

def test_base_comes_from_the_caller_path_not_world_dir(tmp_path):
    """The daemon patches `_r.RB_PATH` per request; the base must follow it.

    Two worlds, each with its own segment. Reading world B must return B's
    segment and NOT A's — a resolver keyed on the module-level WORLD_DIR would
    return A's on every daemon request while looking correct everywhere else.
    """
    world_a = tmp_path / "world-a"
    world_b = tmp_path / "world-b"
    for kind, pfx in KINDS:
        _write_jsonl(world_a / f"{kind}.jsonl", [_rec(f"{pfx}-A-legacy")])
        _write_jsonl(world_a / f"{kind}-2026-08-31.jsonl", [_rec(f"{pfx}-A-seg")])
        _write_jsonl(world_b / f"{kind}.jsonl", [_rec(f"{pfx}-B-legacy")])
        _write_jsonl(world_b / f"{kind}-2026-08-31.jsonl", [_rec(f"{pfx}-B-seg")])

        paths = _retrieve._store_paths(world_b / f"{kind}.jsonl", kind)
        assert all(p.parent == world_b for p in paths), _names(paths)
        ids = [r["id"] for r in _retrieve._read_store(world_b / f"{kind}.jsonl", kind)]
        assert ids == [f"{pfx}-B-legacy", f"{pfx}-B-seg"]
        assert not any("-A-" in i for i in ids), f"leaked world A: {ids}"

    # And the module's OWN WORLD_DIR is neither of them, so a passing run here
    # cannot be an accident of the two happening to coincide.
    assert _retrieve.RB_PATH.parent not in (world_a, world_b)


def test_read_jsonl_is_resolved_as_a_module_global(tmp_path, monkeypatch):
    """The daemon patches `_r.read_jsonl` with a jsonl_cache-backed reader. A
    reference captured at def time would not see the patch, and the daemon would
    silently bypass its cache — and, more importantly, its ensure_local."""
    legacy = tmp_path / "reasoning-bank.jsonl"
    _write_jsonl(legacy, [_rec("rb-on-disk")])
    _write_jsonl(tmp_path / "reasoning-bank-2026-08-31.jsonl", [_rec("rb-seg")])

    seen = []

    def _fake_read_jsonl(path):
        seen.append(Path(path).name)
        return [{"id": "patched-" + Path(path).name}]

    monkeypatch.setattr(_retrieve, "read_jsonl", _fake_read_jsonl)
    out = _retrieve._read_store(legacy, "reasoning-bank")

    assert seen == ["reasoning-bank.jsonl", "reasoning-bank-2026-08-31.jsonl"]
    assert [r["id"] for r in out] == [
        "patched-reasoning-bank.jsonl", "patched-reasoning-bank-2026-08-31.jsonl"]
    assert not any(r["id"] == "rb-on-disk" for r in out), "bypassed the patch"


# ---------------------------------------------------------------------------
# Fail-safe direction: never toward an empty store.
# ---------------------------------------------------------------------------

def test_cold_box_legacy_is_pinned_when_the_seam_enumerates_nothing(tmp_path, monkeypatch):
    """`store_paths` returns [] when the base is unresolvable, and omits the
    legacy file when it can see neither a local copy nor a backend listing. On a
    cold own-cloud box that is reachable, and an empty guardrail read means "no
    guardrails apply" — so the legacy path is pinned regardless."""
    legacy = tmp_path / "guardrails.jsonl"
    _write_jsonl(legacy, [_rec("guard-1")])
    monkeypatch.setattr(_retrieve, "_seg_store_paths", lambda kind, base: [])

    assert _retrieve._store_paths(legacy, "guardrails") == [legacy]
    assert [r["id"] for r in _retrieve._read_store(legacy, "guardrails")] == ["guard-1"]


def test_seam_exception_degrades_to_the_legacy_read(tmp_path, monkeypatch):
    """An ImportError, a bad kind, or any other seam fault must degrade to the
    pre-change behaviour — never to a silent empty corpus."""
    legacy = tmp_path / "reasoning-bank.jsonl"
    _write_jsonl(legacy, [_rec("rb-1")])

    def _boom(kind, base):
        raise RuntimeError("seam unavailable")

    monkeypatch.setattr(_retrieve, "_seg_store_paths", _boom)
    assert _retrieve._store_paths(legacy, "reasoning-bank") == [legacy]
    assert [r["id"] for r in _retrieve._read_store(legacy, "reasoning-bank")] == ["rb-1"]


# ---------------------------------------------------------------------------
# Call-site pin: the two loaders must go through the segmented reader.
# ---------------------------------------------------------------------------

def test_both_loaders_read_through_the_segmented_reader():
    """Guards the wiring, not the helper. A correct `_read_store` that nothing
    calls is exactly the shape g-306-227 shipped (a fixed writer with no caller),
    and its own tests stayed green throughout (guard-1943)."""
    import inspect
    for fn_name, kind in (("load_reasoning_bank", "reasoning-bank"),
                          ("load_guardrails", "guardrails")):
        src = inspect.getsource(getattr(_retrieve, fn_name))
        assert f'_read_store(' in src, f"{fn_name} does not call _read_store"
        assert f'"{kind}"' in src, f"{fn_name} does not name kind {kind}"
        assert "read_jsonl(RB_PATH)" not in src and "read_jsonl(GUARD_PATH)" not in src, \
            f"{fn_name} still reads the legacy file directly"


def test_counter_bumps_carry_the_spool_kind():
    """The counter WRITES were the 93% churn; the writer unit () moved
    them behind the sidecar spool. Pinned so a future edit does not silently
    drop the `kind` argument — a kind-less call is a permanent full-store RMW
    per retrieval regardless of the UTILIZATION_COUNTERS_SPOOLED flag."""
    import inspect
    rb_src = inspect.getsource(_retrieve.load_reasoning_bank)
    guard_src = inspect.getsource(_retrieve.load_guardrails)
    assert '_locked_bump_jsonl(RB_PATH, _should_bump, kind="reasoning-bank")' in rb_src
    assert '_locked_bump_jsonl(GUARD_PATH, _should_bump, kind="guardrails")' in guard_src


# ---------------------------------------------------------------------------
# MUTATION CONTROL: prove the segment pins can fail.
# ---------------------------------------------------------------------------

def test_mutation_control_single_file_resolver_would_miss_the_segment(tmp_path):
    """Reproduces the PRE-CHANGE shape inline and asserts it WOULD miss.

    Without this, every assertion above could in principle pass for a reason
    unrelated to the change — a regression test never seen fail is not evidence.
    """
    legacy = tmp_path / "guardrails.jsonl"
    _write_jsonl(legacy, [_rec("guard-legacy")])
    _write_jsonl(tmp_path / "guardrails-2026-08-31.jsonl", [_rec("guard-seg")])

    old_way = _retrieve.read_jsonl(legacy)              # the pre-change read
    new_way = _retrieve._read_store(legacy, "guardrails")

    assert [r["id"] for r in old_way] == ["guard-legacy"]
    assert [r["id"] for r in new_way] == ["guard-legacy", "guard-seg"]
    assert old_way != new_way, "control drifted — the segment must be visible only to the new reader"


# ---------------------------------------------------------------------------
# Dual residency: an id in BOTH legacy and a segment ( C2).
#
# The CLI twin of the daemon pin in mind_api/tests/test_rbguard_segment_reads.py.
# Both halves matter: `retrieve.sh` runs this path, the daemon endpoints run the
# other, and a stale-wins fix applied to only one leaves half the fleet reading
# a retired guardrail as active. Every test above uses DISTINCT ids across the
# files, so none of them can observe this.
# ---------------------------------------------------------------------------

def test_a_record_mutated_into_a_segment_reads_as_the_segment_copy(tmp_path):
    """Newest path wins, and the record is returned exactly once."""
    for kind, pfx in KINDS:
        legacy = tmp_path / f"{kind}.jsonl"
        rid = f"{pfx}-dual"
        _write_jsonl(legacy, [{"id": rid, "status": "active", "category": "test"}])
        _write_jsonl(tmp_path / f"{kind}-2026-08-31.jsonl",
                     [{"id": rid, "status": "retired", "category": "test"}])

        got = _retrieve._read_store(legacy, kind)

        assert [r["id"] for r in got] == [rid], "record emitted twice"
        assert got[0]["status"] == "retired", (
            "the stale legacy copy won — a retired record still reads active")


def test_mutation_control_a_bare_concatenation_returns_the_stale_copy(tmp_path):
    """Positive control: the pre-change body, reproduced inline, shows BOTH
    halves of the defect — so the test above cannot be passing merely because
    the segment was never read."""
    kind, pfx = KINDS[0]
    legacy = tmp_path / f"{kind}.jsonl"
    rid = f"{pfx}-dual"
    _write_jsonl(legacy, [{"id": rid, "status": "active", "category": "test"}])
    _write_jsonl(tmp_path / f"{kind}-2026-08-31.jsonl",
                 [{"id": rid, "status": "retired", "category": "test"}])

    old = []                                   # pre-change _read_store, verbatim
    for p in _retrieve._store_paths(legacy, kind):
        old.extend(_retrieve.read_jsonl(p))

    assert [r["id"] for r in old] == [rid, rid], "control drifted"
    assert old[0]["status"] == "active", "control drifted — stale must win here"

    new = _retrieve._read_store(legacy, kind)
    assert len(new) == 1 and new[0]["status"] == "retired"


def test_records_without_an_id_survive_the_dedup(tmp_path):
    """Folding id-less records together would read as a short store."""
    kind, _pfx = KINDS[0]
    legacy = tmp_path / f"{kind}.jsonl"
    _write_jsonl(legacy, [{"note": "no id"}, {"note": "also no id"}])
    _write_jsonl(tmp_path / f"{kind}-2026-08-31.jsonl", [{"note": "third"}])

    assert len(_retrieve._read_store(legacy, kind)) == 3
