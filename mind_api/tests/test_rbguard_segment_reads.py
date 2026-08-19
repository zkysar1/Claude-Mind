"""GET /v1/rb/read + /v1/guard/read must read the WHOLE content store ().

These two endpoints are the fleet's live read path for reasoning-bank and
guardrails: `reasoning-bank-read.sh` and `guardrails-read.sh` are daemon-only
wrappers, so every `--recent` / `--summary` / `--id` / `--category` read in
every skill arrives here. Once the g-358-05 writer emits date segments, a
single-file resolver here would make every one of those reads SHORT — and a
short guardrails read is "these guardrails do not apply", the worst available
failure direction.

WHY THESE TESTS EXIST BEFORE THE WRITER. No segment exists today, so every
assertion about segment pickup would pass against a single-file resolver too
if it were written the obvious way (assert the legacy records come back). Each
segment test below therefore CREATES the segment files itself, which is what
makes the pin able to fail: the pre-change code returns the legacy file only,
so the segment records are absent and the assertion breaks. Every test here was
mutation-verified against the pre-change shape (see the module's own
"MUTATION CONTROL" test, which reproduces that shape inline and asserts it
would MISS — a test that has never been seen to fail is not protection,
guard-3534).

The tests exercise `_store_paths` / `_load` directly rather than over HTTP: the
handlers are thin filters over `_load`'s output, and a hermetic tmp world costs
no daemon lifecycle. `test_every_handler_reads_through_load` is what keeps that
substitution honest — it pins that no handler still calls `jc.get` on a
single path.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "mind_api" / "src"
SCRIPTS = ROOT / "core" / "scripts"
for _p in (str(SRC), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mind_api.src.world import reasoning_bank as rbmod  # noqa: E402

KINDS = ("reasoning-bank", "guardrails")


class _Ctx:
    """The only part of the request context these helpers touch."""

    class _Paths:
        def __init__(self, world):
            self.world = world

    def __init__(self, world):
        self.paths = _Ctx._Paths(world)


class _FakeCache:
    """Stands in for jsonl_cache: parses a path, [] when missing.

    Records every path it was asked for, so a test can assert on the
    enumeration itself and not only on the records that came back.
    """

    def __init__(self):
        self.asked = []

    def get(self, path):
        self.asked.append(Path(path))
        p = Path(path)
        if not p.is_file():
            return []
        return [json.loads(line) for line in
                p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def _rec(rid, **over):
    r = {"id": rid, "status": "active", "category": "test"}
    r.update(over)
    return r


# ---------------------------------------------------------------------------
# Byte-compatibility: today's store has no segments, so nothing may change.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_legacy_only_store_is_byte_identical(tmp_path, kind):
    """With no segments, the read is exactly the legacy file — same records,
    same order, and exactly ONE path consulted."""
    legacy = tmp_path / f"{kind}.jsonl"
    _write(legacy, [_rec("a-1"), _rec("a-2"), _rec("a-3")])

    jc = _FakeCache()
    paths = rbmod._store_paths(_Ctx(tmp_path), kind)
    items = rbmod._load(jc, paths)

    assert paths == [legacy]
    assert jc.asked == [legacy]
    assert [r["id"] for r in items] == ["a-1", "a-2", "a-3"]


@pytest.mark.parametrize("kind", KINDS)
def test_missing_legacy_yields_no_records_not_an_error(tmp_path, kind):
    """A world with no store file reads as empty, exactly as jc.get did."""
    jc = _FakeCache()
    items = rbmod._load(jc, rbmod._store_paths(_Ctx(tmp_path), kind))
    assert items == []


# ---------------------------------------------------------------------------
# The point of the change: segments are picked up, in order, without dupes.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_segments_are_read_after_legacy_in_date_order(tmp_path, kind):
    """Legacy first, then segments oldest-first — the order consumers window on.

    Written out of order on disk to prove the ordering comes from the sort and
    not from directory iteration order.
    """
    _write(tmp_path / f"{kind}.jsonl", [_rec("legacy-1")])
    _write(tmp_path / f"{kind}-2026-09-02.jsonl", [_rec("seg-sep02")])
    _write(tmp_path / f"{kind}-2026-08-31.jsonl", [_rec("seg-aug31")])
    _write(tmp_path / f"{kind}-2026-09-01.jsonl", [_rec("seg-sep01")])

    items = rbmod._load(_FakeCache(), rbmod._store_paths(_Ctx(tmp_path), kind))

    assert [r["id"] for r in items] == [
        "legacy-1", "seg-aug31", "seg-sep01", "seg-sep02"]


@pytest.mark.parametrize("kind", KINDS)
def test_legacy_is_never_read_twice(tmp_path, kind):
    """`_store_paths` pins the legacy path unconditionally AND the seam already
    returns it when it can see it. Duplicating it would double every record in
    the store — a silent corruption of every count the fleet reads."""
    _write(tmp_path / f"{kind}.jsonl", [_rec("only-1")])
    _write(tmp_path / f"{kind}-2026-08-31.jsonl", [_rec("seg-1")])

    jc = _FakeCache()
    paths = rbmod._store_paths(_Ctx(tmp_path), kind)
    items = rbmod._load(jc, paths)

    assert len(paths) == len(set(paths))
    assert jc.asked.count(tmp_path / f"{kind}.jsonl") == 1
    assert [r["id"] for r in items] == ["only-1", "seg-1"]


@pytest.mark.parametrize("kind", KINDS)
def test_archive_and_sidecar_are_excluded(tmp_path, kind):
    """`<kind>-archive.jsonl` and `<kind>-utilization.jsonl` both match a loose
    `<kind>-*.jsonl` glob and neither belongs in the content store.

    The archive is the sharper of the two: both archives EXIST in the live world
    today, and folding one in would resurrect retired records as active.
    """
    _write(tmp_path / f"{kind}.jsonl", [_rec("live-1")])
    _write(tmp_path / f"{kind}-archive.jsonl", [_rec("retired-1")])
    _write(tmp_path / f"{kind}-utilization.jsonl",
           [{"id": "live-1", "utilization": {"times_helpful": 3}}])

    items = rbmod._load(_FakeCache(), rbmod._store_paths(_Ctx(tmp_path), kind))

    assert [r["id"] for r in items] == ["live-1"]


def test_the_two_kinds_do_not_bleed_into_each_other(tmp_path):
    """Both stores sit in the SAME directory, so a matcher that is loose about
    the kind prefix would fold guardrails into a reasoning-bank read."""
    _write(tmp_path / "reasoning-bank.jsonl", [_rec("rb-1")])
    _write(tmp_path / "reasoning-bank-2026-08-31.jsonl", [_rec("rb-seg")])
    _write(tmp_path / "guardrails.jsonl", [_rec("guard-1")])
    _write(tmp_path / "guardrails-2026-08-31.jsonl", [_rec("guard-seg")])

    ctx = _Ctx(tmp_path)
    rb = rbmod._load(_FakeCache(), rbmod._store_paths(ctx, "reasoning-bank"))
    gu = rbmod._load(_FakeCache(), rbmod._store_paths(ctx, "guardrails"))

    assert [r["id"] for r in rb] == ["rb-1", "rb-seg"]
    assert [r["id"] for r in gu] == ["guard-1", "guard-seg"]


# ---------------------------------------------------------------------------
# The cold-box guarantee: the legacy path is consulted even when nothing can
# SEE it, because jc.get materialises it through the backend.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_legacy_path_is_asked_for_even_when_it_is_not_on_disk(tmp_path, kind):
    """This is the whole reason `_store_paths` pins the legacy path rather than
    trusting the seam's enumeration.

    `store_paths` includes the legacy file only when it can see it locally or in
    the backend listing. On a cold box whose backend listing just failed, both
    are false — and the seam correctly returns [] because it cannot prove what
    the store holds. But `jc.get` calls `ensure_local` before its stat, so
    handing it the path is how the object gets pulled at all. Dropping it would
    turn a recoverable cold read into zero records, which for guardrails reads
    as a clean all-clear.
    """
    jc = _FakeCache()
    paths = rbmod._store_paths(_Ctx(tmp_path), kind)
    rbmod._load(jc, paths)

    assert jc.asked == [tmp_path / f"{kind}.jsonl"]


@pytest.mark.parametrize("kind", KINDS)
def test_import_failure_degrades_to_the_legacy_path_not_to_empty(
        tmp_path, monkeypatch, kind):
    """If `_utilization_store` cannot be imported the read must fall back to
    exactly today's behaviour, never to nothing."""
    monkeypatch.setitem(sys.modules, "_utilization_store", None)
    _write(tmp_path / f"{kind}.jsonl", [_rec("a-1")])

    paths = rbmod._store_paths(_Ctx(tmp_path), kind)
    items = rbmod._load(_FakeCache(), paths)

    assert paths == [tmp_path / f"{kind}.jsonl"]
    assert [r["id"] for r in items] == ["a-1"]


@pytest.mark.parametrize("kind", KINDS)
def test_world_comes_from_ctx_not_from_a_module_global(tmp_path, kind):
    """Per-request path resolution (.claude/rules/path-resolution.md). Two ctxs
    pointing at different worlds must read different stores — a frozen
    module-level WORLD_DIR would collapse them onto one."""
    a, b = tmp_path / "world-a", tmp_path / "world-b"
    _write(a / f"{kind}.jsonl", [_rec("from-a")])
    _write(b / f"{kind}.jsonl", [_rec("from-b")])

    got_a = rbmod._load(_FakeCache(), rbmod._store_paths(_Ctx(a), kind))
    got_b = rbmod._load(_FakeCache(), rbmod._store_paths(_Ctx(b), kind))

    assert [r["id"] for r in got_a] == ["from-a"]
    assert [r["id"] for r in got_b] == ["from-b"]


# ---------------------------------------------------------------------------
# Structural pins: the substitution above only holds while every handler goes
# through _load, and the returned list must stay safe to sort.
# ---------------------------------------------------------------------------

def test_every_handler_reads_through_load():
    """No handler may resolve a single store path of its own.

    These tests drive `_load` directly, so a handler that still called
    `jc.get(path)` would be entirely uncovered by them AND segment-blind in
    production. Pinning the absence is what keeps the direct-call tests
    representative of the HTTP path.
    """
    src = Path(rbmod.__file__).read_text(encoding="utf-8")
    body = src.split("# RB read", 1)[1]

    assert "jc.get(" not in body, (
        "a handler resolves its own path — route it through _load(jc, paths)")
    assert body.count("_load(jc, paths)") >= 10
    for dead in ("_rb_path", "_guard_path"):
        assert dead not in body, f"{dead} is dead — remove it, do not re-add"


def test_load_returns_a_fresh_list_not_the_shared_cache_list():
    """`jsonl_cache.get` hands back the SHARED cache list and its own docstring
    forbids mutating it. Handlers here sort their results, so `_load` must
    return a list that is safe to reorder."""
    shared = [_rec("x-1"), _rec("x-2")]

    class _One:
        def get(self, path):
            return shared

    out = rbmod._load(_One(), [Path("a.jsonl")])
    out.sort(key=lambda r: r["id"], reverse=True)

    assert [r["id"] for r in shared] == ["x-1", "x-2"]
    assert out is not shared


# ---------------------------------------------------------------------------
# MUTATION CONTROL (guard-3534): prove the segment pins can fail.
# ---------------------------------------------------------------------------

def test_mutation_control_single_file_resolver_would_miss_the_segment(tmp_path):
    """Reproduces the PRE-CHANGE shape inline — `[world / f"{kind}.jsonl"]` —
    and asserts it misses a segment the converted code picks up.

    Without this, every segment assertion above could in principle be passing
    for a reason unrelated to the change. This is the positive control that
    says the difference is real and is the one being measured.
    """
    kind = "guardrails"
    _write(tmp_path / f"{kind}.jsonl", [_rec("legacy-1")])
    _write(tmp_path / f"{kind}-2026-08-31.jsonl", [_rec("seg-1")])

    old_paths = [tmp_path / f"{kind}.jsonl"]          # pre-change resolver
    new_paths = rbmod._store_paths(_Ctx(tmp_path), kind)

    old = [r["id"] for r in rbmod._load(_FakeCache(), old_paths)]
    new = [r["id"] for r in rbmod._load(_FakeCache(), new_paths)]

    assert old == ["legacy-1"], "control drifted — it must be the old shape"
    assert new == ["legacy-1", "seg-1"]
    assert "seg-1" not in old


def test_segment_name_shape_matches_what_the_reader_accepts():
    """The tests above hard-code `<kind>-YYYY-MM-DD.jsonl`. If the writer's
    `segment_name` ever stops producing that shape these pins would keep
    passing against a filename production never emits — the exact
    writer/reader drift `_utilization_store` centralises to prevent.
    """
    import datetime

    from _utilization_store import _segment_re, segment_name

    for kind in KINDS:
        name = segment_name(kind, datetime.date(2026, 8, 31))
        assert name == f"{kind}-2026-08-31.jsonl"
        assert _segment_re(kind).match(name)
        assert re.match(r"^[a-z-]+-\d{4}-\d{2}-\d{2}\.jsonl$", name)


# ---------------------------------------------------------------------------
# Dual residency: an id present in BOTH legacy and a segment ( C2).
#
# `_store_paths` yields legacy first, so a bare concatenation returns the OLDEST
# copy from `find_by_id` and emits the record twice in every list read. The
# tests above all use DISTINCT ids across the two files, so none of them can
# see this — which is why it survived the seam review that found it.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_a_record_mutated_into_a_segment_reads_as_the_segment_copy(
        tmp_path, kind):
    """Newest path wins, and the record appears exactly once."""
    _write(tmp_path / f"{kind}.jsonl", [_rec("x-1", status="active")])
    _write(tmp_path / f"{kind}-2026-08-31.jsonl",
           [_rec("x-1", status="retired")])

    items = rbmod._load(_FakeCache(), rbmod._store_paths(_Ctx(tmp_path), kind))

    assert [r["id"] for r in items] == ["x-1"], "the record must not be emitted twice"
    assert items[0]["status"] == "retired", (
        "the STALE legacy copy won — a retired record still reads as active")


@pytest.mark.parametrize("kind", KINDS)
def test_find_by_id_returns_the_segment_copy_not_the_legacy_one(tmp_path, kind):
    """`?id=` is a FIRST-match lookup, so it is the sharpest edge of the dedup.

    Asserted through the real `find_by_id` the handlers call, not a local
    re-implementation of first-match — the defect lives in the interaction
    between that function and the path order, so a hand-rolled stand-in could
    pass while the shipped pair still fails.
    """
    from mind_api.src.endpoints._jsonl_common import find_by_id

    _write(tmp_path / f"{kind}.jsonl", [_rec("x-1", status="active")])
    _write(tmp_path / f"{kind}-2026-08-31.jsonl",
           [_rec("x-1", status="retired")])

    items = rbmod._load(_FakeCache(), rbmod._store_paths(_Ctx(tmp_path), kind))
    found = find_by_id(items, "x-1")

    assert found is not None
    assert found[1]["status"] == "retired"


def test_mutation_control_a_bare_concatenation_returns_the_stale_copy(tmp_path):
    """The positive control: reproduce the pre-change `_load` inline and assert
    it exhibits BOTH halves of the defect the two tests above pin.

    Without this, those tests could be passing because no segment was read at
    all rather than because the dedup works.
    """
    kind = "guardrails"
    _write(tmp_path / f"{kind}.jsonl", [_rec("x-1", status="active")])
    _write(tmp_path / f"{kind}-2026-08-31.jsonl",
           [_rec("x-1", status="retired")])

    jc = _FakeCache()
    paths = rbmod._store_paths(_Ctx(tmp_path), kind)

    old = []                                  # pre-change _load, verbatim
    for p in paths:
        old.extend(jc.get(p))

    assert [r["id"] for r in old] == ["x-1", "x-1"], "control drifted"
    assert old[0]["status"] == "active", "control drifted — stale must win here"

    new = rbmod._load(_FakeCache(), paths)
    assert len(new) == 1 and new[0]["status"] == "retired"


def test_records_without_an_id_are_never_collapsed(tmp_path):
    """Malformed lines must survive the dedup as themselves.

    Keying on a missing id would fold every id-less record into ONE, which
    reads as a short store — the failure direction this module refuses to take
    silently.
    """
    kind = "reasoning-bank"
    _write(tmp_path / f"{kind}.jsonl",
           [{"note": "no id"}, {"note": "also no id"}])
    _write(tmp_path / f"{kind}-2026-08-31.jsonl", [{"note": "third"}])

    items = rbmod._load(_FakeCache(), rbmod._store_paths(_Ctx(tmp_path), kind))
    assert len(items) == 3


@pytest.mark.parametrize("kind", KINDS)
def test_newest_segment_wins_across_three_copies_not_merely_the_last_path(
        tmp_path, kind):
    """The JOIN between two components, asserted as one thing (guard-4323).

    `dedup_by_id` keeps the LAST occurrence; `_store_paths` returns paths
    oldest-first. "Newest wins" is true only because those two hold TOGETHER,
    and neither component's own tests can see the join: the helper's unit tests
    know nothing about dates, and the ordering test knows nothing about
    duplicate ids. If `_store_paths` ever sorted descending, dedup would
    silently select the OLDEST copy and every test on both sides would stay
    green — an invariant satisfied by the interaction of two files, stated
    nowhere, is one edit from being violated.

    Three copies, not two, on purpose: with only legacy + one segment, "last
    path" and "newest date" are the same position, so the assertion cannot
    distinguish a correct implementation from one that just takes the final
    element of a two-element list.
    """
    _write(tmp_path / f"{kind}.jsonl", [_rec("x-1", status="active")])
    _write(tmp_path / f"{kind}-2026-08-31.jsonl", [_rec("x-1", status="paused")])
    _write(tmp_path / f"{kind}-2026-09-02.jsonl", [_rec("x-1", status="retired")])

    paths = rbmod._store_paths(_Ctx(tmp_path), kind)
    assert [p.name for p in paths] == [
        f"{kind}.jsonl",
        f"{kind}-2026-08-31.jsonl",
        f"{kind}-2026-09-02.jsonl",
    ], "ordering precondition broke — dedup's last-wins no longer means newest-wins"

    items = rbmod._load(_FakeCache(), paths)
    assert len(items) == 1
    assert items[0]["status"] == "retired", "the newest segment must win"
