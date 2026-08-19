"""Tests for the reasoning-bank / guardrails reader seam (_utilization_store).

g-358-05. The seam exists so the two largest class-B stores can be split — counters
into a small sidecar, content into date segments — without touching the 37 direct
`utilization` read sites across 18 files. It lands BEFORE any writer, mirroring
_gate_log.firings_paths (g-328-38).

Four properties carry real risk and are tested as such rather than structurally:

1. ARCHIVE EXCLUSION. `reasoning-bank-archive.jsonl` and `guardrails-archive.jsonl`
   EXIST TODAY and both match a loose `<kind>-*.jsonl` glob. If the seam picked
   them up, every consumer would fold retired records back into the live store —
   306 retired reasoning-bank records reappearing as active. This is the sibling
   of the spool-exclusion risk in test_gate_firings_paths, but strictly worse:
   the spool was a tail to double-count, this is a resurrection.

2. BYTE-IDENTICAL TODAY. With no sidecar and no segments — the state of every box
   right now — store_paths must return exactly the legacy file and utilization_of
   must return exactly the embedded field. A consumer converted today must behave
   identically to one that was not, or the reader-before-writer ordering buys
   nothing.

3. SIDECAR WINS OVER EMBEDDED. During cutover an id can carry both. The embedded
   copy is a frozen pre-split snapshot; preferring it would pin every converted
   consumer to stale counts while looking correct. test_sidecar_beats_embedded is
   the mutation proof — it fails if the precedence is flipped.

4. WRITER/READER CONTRACT. segment_name and _segment_re are two halves of one
   contract; when they drift the writer emits files the reader ignores and
   consumers read a partial store while reporting it as whole. The round-trip
   test makes that drift a test failure rather than a silent data window.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _utilization_store import (  # noqa: E402
    KINDS,
    _segment_re,
    counters_name,
    counters_path,
    load_counters,
    segment_name,
    store_paths,
    utilization_of,
)


def _write(p: Path, text: str = '{"id": "rb-1"}\n') -> Path:
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# 1. Archive exclusion — the decisive collision case
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_archive_is_excluded(tmp_path, kind):
    """The archive sibling exists in every real world dir and must never join."""
    legacy = _write(tmp_path / f"{kind}.jsonl")
    _write(tmp_path / f"{kind}-archive.jsonl")

    assert store_paths(kind, tmp_path) == [legacy]


@pytest.mark.parametrize("kind", KINDS)
def test_counter_sidecar_is_excluded_from_content_store(tmp_path, kind):
    """The sidecar shares the stem but is counters, not content."""
    legacy = _write(tmp_path / f"{kind}.jsonl")
    _write(tmp_path / counters_name(kind))

    assert store_paths(kind, tmp_path) == [legacy]


@pytest.mark.parametrize("kind", KINDS)
def test_arbitrary_siblings_excluded_by_construction(tmp_path, kind):
    """Not a denylist: anything not date-shaped is excluded because it is not
    date-shaped. Pins the property for files that do not exist yet."""
    legacy = _write(tmp_path / f"{kind}.jsonl")
    for name in (f"{kind}-backup.jsonl", f"{kind}-2026-08.jsonl",
                 f"{kind}-2026-8-1.jsonl", f"{kind}-tmp-2026-08-01.jsonl"):
        _write(tmp_path / name)

    assert store_paths(kind, tmp_path) == [legacy]


# --------------------------------------------------------------------------
# 2. Byte-identical today
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_legacy_only_is_todays_state(tmp_path, kind):
    legacy = _write(tmp_path / f"{kind}.jsonl")
    assert store_paths(kind, tmp_path) == [legacy]


@pytest.mark.parametrize("kind", KINDS)
def test_no_sidecar_yields_empty_counters(tmp_path, kind):
    assert load_counters(kind, tmp_path) == {}


def test_utilization_of_falls_through_to_embedded_today():
    """With no sidecar loaded, the join is exactly today's read."""
    rec = {"id": "rb-1", "utilization": {"times_helpful": 3}}
    assert utilization_of(rec, {}) == {"times_helpful": 3}
    assert utilization_of(rec, None) == {"times_helpful": 3}


def test_empty_dir_returns_nothing(tmp_path):
    assert store_paths("reasoning-bank", tmp_path) == []


# --------------------------------------------------------------------------
# 3. Sidecar precedence — the stale-pin hazard
# --------------------------------------------------------------------------

def test_sidecar_beats_embedded():
    """MUTATION PROOF: fails if precedence is flipped to prefer embedded.

    The embedded value is the frozen pre-split snapshot; the sidecar is live.
    """
    rec = {"id": "rb-1", "utilization": {"times_helpful": 1}}
    counters = {"rb-1": {"times_helpful": 99}}
    assert utilization_of(rec, counters) == {"times_helpful": 99}


def test_embedded_used_when_id_absent_from_sidecar():
    """A partially-migrated sidecar must not blank un-migrated records."""
    rec = {"id": "rb-2", "utilization": {"times_helpful": 7}}
    counters = {"rb-1": {"times_helpful": 99}}
    assert utilization_of(rec, counters) == {"times_helpful": 7}


def test_missing_counters_everywhere_returns_empty_dict():
    """{} not None, so callers can .get() without a None check."""
    assert utilization_of({"id": "rb-3"}, {}) == {}
    assert utilization_of({}, {}) == {}
    assert utilization_of(None, {}) == {}


def test_non_dict_embedded_is_not_returned():
    assert utilization_of({"id": "rb-4", "utilization": "corrupt"}, {}) == {}


# --------------------------------------------------------------------------
# 4. Writer/reader contract
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_segment_name_round_trips_through_the_matcher(kind):
    """The name the writer will emit must be the name the reader accepts."""
    name = segment_name(kind, date(2026, 8, 15))
    assert _segment_re(kind).match(name), f"reader rejects its own writer's name: {name}"


@pytest.mark.parametrize("kind", KINDS)
def test_segments_change_the_result(tmp_path, kind):
    """MUTATION PROOF: fails if store_paths reverts to legacy-only — the
    regression that would starve consumers to a partial store."""
    legacy = _write(tmp_path / f"{kind}.jsonl")
    seg = _write(tmp_path / segment_name(kind, date(2026, 8, 1)))

    assert store_paths(kind, tmp_path) == [legacy, seg]


@pytest.mark.parametrize("kind", KINDS)
def test_segments_are_chronological_regardless_of_creation_order(tmp_path, kind):
    legacy = _write(tmp_path / f"{kind}.jsonl")
    seg_c = _write(tmp_path / segment_name(kind, date(2026, 8, 3)))
    seg_a = _write(tmp_path / segment_name(kind, date(2026, 8, 1)))
    seg_b = _write(tmp_path / segment_name(kind, date(2026, 8, 2)))

    assert store_paths(kind, tmp_path) == [legacy, seg_a, seg_b, seg_c]


@pytest.mark.parametrize("kind", KINDS)
def test_segments_without_legacy(tmp_path, kind):
    """After the legacy file ages out, segments alone are the store."""
    seg = _write(tmp_path / segment_name(kind, date(2026, 8, 1)))
    assert store_paths(kind, tmp_path) == [seg]


def test_segment_name_of_one_kind_is_not_matched_by_the_other(tmp_path):
    """reasoning-bank and guardrails segments must not cross-contaminate."""
    rb_seg = _write(tmp_path / segment_name("reasoning-bank", date(2026, 8, 1)))
    gr_seg = _write(tmp_path / segment_name("guardrails", date(2026, 8, 1)))

    assert store_paths("reasoning-bank", tmp_path) == [rb_seg]
    assert store_paths("guardrails", tmp_path) == [gr_seg]


# --------------------------------------------------------------------------
# Sidecar parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_load_counters_parses_sidecar(tmp_path, kind):
    path = tmp_path / counters_name(kind)
    path.write_text(
        json.dumps({"id": "x-1", "utilization": {"times_helpful": 2}}) + "\n"
        + json.dumps({"id": "x-2", "utilization": {"times_helpful": 5}}) + "\n",
        encoding="utf-8")

    assert load_counters(kind, tmp_path) == {
        "x-1": {"times_helpful": 2},
        "x-2": {"times_helpful": 5},
    }


@pytest.mark.parametrize("kind", KINDS)
def test_load_counters_skips_malformed_without_raising(tmp_path, kind):
    """A torn line loses one record's advisory stats; raising would take down a
    retrieval call for a cosmetic field."""
    path = tmp_path / counters_name(kind)
    path.write_text(
        json.dumps({"id": "x-1", "utilization": {"times_helpful": 2}}) + "\n"
        + "{not json\n"
        + "\n"
        + json.dumps(["not", "a", "dict"]) + "\n"
        + json.dumps({"no_id": True, "utilization": {}}) + "\n"
        + json.dumps({"id": "x-3", "utilization": "not-a-dict"}) + "\n"
        + json.dumps({"id": "x-4", "utilization": {"times_helpful": 9}}) + "\n",
        encoding="utf-8")

    assert load_counters(kind, tmp_path) == {
        "x-1": {"times_helpful": 2},
        "x-4": {"times_helpful": 9},
    }


@pytest.mark.parametrize("kind", KINDS)
def test_counters_path_is_not_date_shaped(kind):
    """Guards the invariant that keeps the sidecar out of store_paths."""
    assert not _segment_re(kind).match(counters_name(kind))


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------

def test_unknown_kind_raises():
    for fn in (store_paths, load_counters, counters_name, _segment_re):
        with pytest.raises(ValueError):
            fn("aspirations")


def test_unresolved_world_dir_is_loud_and_empty(tmp_path, monkeypatch, capsys):
    """Returning empty SILENTLY would read as 'no guardrails apply' — the worst
    available failure direction for these two stores."""
    import _utilization_store as us
    monkeypatch.setattr(us, "WORLD_DIR", None)

    assert us.store_paths("guardrails") == []
    assert us.load_counters("guardrails") == {}
    assert us.counters_path("guardrails") is None

    err = capsys.readouterr().err
    assert "WORLD_DIR unresolved" in err


def test_explicit_world_dir_overrides_module_constant(tmp_path, monkeypatch):
    """The tmp-dir seam every test above relies on must not silently read the
    live store when the module constant happens to be set."""
    import _utilization_store as us
    monkeypatch.setattr(us, "WORLD_DIR", Path("/nonexistent/live/world"))
    legacy = _write(tmp_path / "guardrails.jsonl")

    assert us.store_paths("guardrails", tmp_path) == [legacy]


# ---------------------------------------------------------------------------
# Backend-aware enumeration ( step 3)
#
# Every test above enumerates a tmp dir, where the local filesystem IS the whole
# truth. On a real own-cloud box it is not: the local tree is a read-through
# CACHE, so a file materialises only once something has read it and `glob`
# reports what THIS BOX HAPPENS TO HOLD. Measured 2026-08-16 (alpha, cc-08) at
# WORLD_DIR itself: backend 62 `.jsonl` names vs local 60 — 4 store-only and 2
# local-only, so the gap runs in BOTH directions and the fix is a UNION.
#
# The failure this prevents is silent and worst-direction: a missed segment
# makes the store read SHORT, and a short guardrails read is "these guardrails
# do not apply". It is latent today (no segments exist) and would become live
# the moment the writer lands, which is why it is pinned now.
# ---------------------------------------------------------------------------

class _FakeBackend:
    """Minimal stand-in for the storage backend's enumeration surface."""

    def __init__(self, names=None, raises=None):
        self._names = list(names or [])
        self._raises = raises

    def list_dir(self, path):
        if self._raises is not None:
            raise self._raises
        return list(self._names)


def _patch_backend(monkeypatch, backend):
    """Patch the symbol `_backend_names` imports lazily, per call."""
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: backend)


@pytest.mark.parametrize("kind", KINDS)
def test_segment_present_only_in_the_backend_is_enumerated(
        tmp_path, monkeypatch, kind):
    """THE POINT OF THE WHOLE CHANGE. The segment exists in the store of record
    and has never been read on this box, so it is absent from the local tree.
    The pre-fix `base.glob(...)` could not see it and the store read short."""
    legacy = _write(tmp_path / ("%s.jsonl" % kind))
    remote_only = segment_name(kind, date(2026, 8, 17))
    _patch_backend(monkeypatch, _FakeBackend([legacy.name, remote_only]))

    got = store_paths(kind, tmp_path)

    assert got == [legacy, tmp_path / remote_only]
    assert not (tmp_path / remote_only).exists(), (
        "the fixture must keep the segment non-local — an .is_file() check on "
        "backend-sourced names would re-impose the exact defect")


@pytest.mark.parametrize("kind", KINDS)
def test_segment_present_only_locally_is_still_enumerated(
        tmp_path, monkeypatch, kind):
    """UNION, NOT REPLACEMENT. A segment written on this box and not yet pushed
    is equally real; a backend-only read would drop it. Measured: 2 such files
    at WORLD_DIR on cc-08 the day this landed."""
    legacy = _write(tmp_path / ("%s.jsonl" % kind))
    local_only = _write(tmp_path / segment_name(kind, date(2026, 8, 18)))
    _patch_backend(monkeypatch, _FakeBackend([legacy.name]))  # backend lags

    assert store_paths(kind, tmp_path) == [legacy, local_only]


@pytest.mark.parametrize("kind", KINDS)
def test_legacy_present_only_in_the_backend_is_enumerated(
        tmp_path, monkeypatch, kind):
    """The worst-direction case: the legacy file itself uncached. Pre-fix this
    returned [] — an empty read of the entire store, indistinguishable from a
    genuinely empty one."""
    _patch_backend(monkeypatch, _FakeBackend(["%s.jsonl" % kind]))

    assert store_paths(kind, tmp_path) == [tmp_path / ("%s.jsonl" % kind)]


@pytest.mark.parametrize("kind", KINDS)
def test_backend_names_pass_the_same_segment_filter(tmp_path, monkeypatch, kind):
    """The archive-resurrection risk, re-entering through the new door. The
    backend view must be filtered by `_segment_re` exactly as the local one is —
    an archive or sidecar that exists only in the store must stay excluded."""
    legacy = _write(tmp_path / ("%s.jsonl" % kind))
    _patch_backend(monkeypatch, _FakeBackend([
        legacy.name,
        "%s-archive.jsonl" % kind,
        counters_name(kind),
        "%s-spool.jsonl" % kind,
        "%s-notes.md" % kind,
    ]))

    assert store_paths(kind, tmp_path) == [legacy]


@pytest.mark.parametrize("kind", KINDS)
def test_segment_matching_is_shape_only_not_calendar_valid(
        tmp_path, monkeypatch, kind):
    """`_segment_re` is `\\d{4}-\\d{2}-\\d{2}` — a SHAPE, not a real date, so
    `<kind>-2026-13-99.jsonl` IS admitted. That is correct (the matcher's job is
    to separate segments from archives and sidecars, and `segment_name` is the
    only writer), but it is easy to assume otherwise: this test was first
    written asserting the opposite. Pinned so that tightening it to calendar
    validity is a deliberate choice rather than a silent behaviour change."""
    impossible = "%s-2026-13-99.jsonl" % kind
    _patch_backend(monkeypatch, _FakeBackend([impossible]))

    assert store_paths(kind, tmp_path) == [tmp_path / impossible]


def test_backend_only_segment_of_one_kind_never_leaks_into_the_other(
        tmp_path, monkeypatch):
    """Cross-kind isolation has to hold on the backend path too — the prefix
    test is `kind + "-"`, and `reasoning-bank`/`guardrails` share no prefix, but
    a future KINDS entry might."""
    _patch_backend(monkeypatch, _FakeBackend([
        "reasoning-bank.jsonl",
        "guardrails.jsonl",
        segment_name("guardrails", date(2026, 8, 17)),
    ]))

    rb = store_paths("reasoning-bank", tmp_path)
    assert rb == [tmp_path / "reasoning-bank.jsonl"]


@pytest.mark.parametrize("kind", KINDS)
def test_mixed_sources_stay_chronological_with_legacy_first(
        tmp_path, monkeypatch, kind):
    """Order is the contract consumers rely on, and it must not depend on which
    side of the cache a segment happened to come from."""
    legacy = _write(tmp_path / ("%s.jsonl" % kind))
    local_mid = _write(tmp_path / segment_name(kind, date(2026, 8, 15)))
    early = segment_name(kind, date(2026, 8, 14))
    late = segment_name(kind, date(2026, 8, 16))
    _patch_backend(monkeypatch, _FakeBackend([legacy.name, late, early]))

    assert store_paths(kind, tmp_path) == [
        legacy, tmp_path / early, local_mid, tmp_path / late,
    ]


def test_backend_fault_falls_back_to_local_and_says_so(
        tmp_path, monkeypatch, capsys):
    """A missing ListBucket grant or a network fault makes the enumeration
    silently SHORT. Falling back to local is right; doing it quietly is not —
    the caller is now reading a lower bound and nothing else would say so."""
    legacy = _write(tmp_path / "guardrails.jsonl")
    local_seg = _write(tmp_path / segment_name("guardrails", date(2026, 8, 15)))
    _patch_backend(monkeypatch, _FakeBackend(raises=RuntimeError("AccessDenied")))

    assert store_paths("guardrails", tmp_path) == [legacy, local_seg]

    err = capsys.readouterr().err
    assert "backend enumeration unavailable" in err
    assert "LOWER BOUND" in err
    assert "AccessDenied" in err, "the cause must survive into the warning"


def test_foreign_base_falls_back_silently(tmp_path, monkeypatch, capsys):
    """A ValueError means `base` is not under a configured root — a tmp world in
    a test. There is no backend copy of a tmp world, so the local view IS
    complete and a warning here would be pure noise on every test in this file.
    Pinned because the quiet path and the loud path are one `except` apart."""
    legacy = _write(tmp_path / "guardrails.jsonl")
    _patch_backend(monkeypatch, _FakeBackend(
        raises=ValueError("%s is not under any configured root" % tmp_path)))

    assert store_paths("guardrails", tmp_path) == [legacy]
    assert capsys.readouterr().err == ""


def test_import_failure_degrades_to_the_local_view(tmp_path, monkeypatch):
    """`storage_backend` is imported lazily inside `_backend_names`; this module
    otherwise depends on stdlib plus `_paths` alone. An import that fails must
    not take the seam down with it."""
    import builtins
    legacy = _write(tmp_path / "guardrails.jsonl")
    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "storage_backend":
            raise ImportError("no storage_backend here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)

    assert store_paths("guardrails", tmp_path) == [legacy]


# ---------------------------------------------------------------------------
# Backend-aware sidecar READ ( step 4)
#
# `store_paths` was routed through the backend above and its own docstring then
# told every caller "CALLERS MUST READ THROUGH THE BACKEND". `load_counters`
# lives in the same file and did not: `path.is_file()` then `path.read_text()`.
# Under own-cloud a sidecar this box has never fetched is ABSENT locally while
# present in the store, so that pair returned {}.
#
# The severity is the SILENCE, not the miss. {} is also the correct reading
# during the reader-before-writer window, so the two are indistinguishable:
# every consumer falls through to the embedded field and nothing looks wrong
# while a box reports zero counters for a store that has them (guard-3992's
# quiet direction). These four tests pin the routing and both fallbacks.
# ---------------------------------------------------------------------------

class _FakeCounterBackend:
    """Stand-in for the backend's byte-read surface."""

    def __init__(self, blobs=None, raises=None):
        self._blobs = dict(blobs or {})
        self._raises = raises

    def read_bytes(self, path, *, force_fresh=False):
        if self._raises is not None:
            raise self._raises
        try:
            return self._blobs[Path(path).name]
        except KeyError:
            raise FileNotFoundError(str(path))


def _counter_blob(*pairs):
    return b"".join(
        (json.dumps({"id": i, "utilization": u}) + "\n").encode("utf-8")
        for i, u in pairs)


@pytest.mark.parametrize("kind", KINDS)
def test_load_counters_reads_a_sidecar_present_only_in_the_backend(
        tmp_path, monkeypatch, kind):
    """THE POINT OF THE CHANGE. The sidecar exists in the store of record and
    has never been read on this box, so it is absent from the local tree. The
    pre-fix `path.is_file()` returned False and the whole store read as
    zero-counters — plausibly, and therefore invisibly."""
    _patch_backend(monkeypatch, _FakeCounterBackend({
        counters_name(kind): _counter_blob(("x-1", {"times_helpful": 4})),
    }))

    assert load_counters(kind, tmp_path) == {"x-1": {"times_helpful": 4}}
    assert not (tmp_path / counters_name(kind)).exists(), (
        "the fixture must keep the sidecar non-local, or the assertion above "
        "would pass through the pre-fix local read and prove nothing")


@pytest.mark.parametrize("kind", KINDS)
def test_load_counters_keeps_a_local_sidecar_the_backend_has_never_seen(
        tmp_path, monkeypatch, kind):
    """A sidecar written HERE and not yet pushed is equally real — the same
    both-directions asymmetry `store_paths` unions for segments. A
    FileNotFoundError from the store must fall through to the local file, NOT
    short-circuit to {}: returning {} there would newly DROP data that the
    pre-fix bare local read handled correctly."""
    (tmp_path / counters_name(kind)).write_text(
        json.dumps({"id": "x-2", "utilization": {"times_helpful": 7}}) + "\n",
        encoding="utf-8")
    _patch_backend(monkeypatch, _FakeCounterBackend({}))  # store has nothing

    assert load_counters(kind, tmp_path) == {"x-2": {"times_helpful": 7}}


def test_load_counters_backend_fault_falls_back_to_local_and_says_so(
        tmp_path, monkeypatch, capsys):
    """A network fault or a missing grant must degrade to the local view, which
    under a read-through cache may be ABSENT. Silence there would re-create the
    exact defect this section fixes, one layer down."""
    (tmp_path / counters_name("guardrails")).write_text(
        json.dumps({"id": "g-1", "utilization": {"times_helpful": 1}}) + "\n",
        encoding="utf-8")
    _patch_backend(monkeypatch, _FakeCounterBackend(
        raises=RuntimeError("AccessDenied")))

    assert load_counters("guardrails", tmp_path) == {
        "g-1": {"times_helpful": 1}}

    err = capsys.readouterr().err
    assert "backend read unavailable" in err
    assert "AccessDenied" in err, "the cause must survive into the warning"


def test_load_counters_foreign_base_falls_back_silently(
        tmp_path, monkeypatch, capsys):
    """A ValueError means the base is not under a configured root — a tmp world
    in a test, where no backend copy exists and the local view IS complete. A
    warning here would fire on every other test in this file."""
    (tmp_path / counters_name("guardrails")).write_text(
        json.dumps({"id": "g-1", "utilization": {"times_helpful": 1}}) + "\n",
        encoding="utf-8")
    _patch_backend(monkeypatch, _FakeCounterBackend(
        raises=ValueError("%s is not under any configured root" % tmp_path)))

    assert load_counters("guardrails", tmp_path) == {
        "g-1": {"times_helpful": 1}}
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# dedup_by_id — the READ-side companion to store_paths ().
#
# store_paths returns oldest-first (legacy, then segments ascending), so a bare
# concatenation over it makes the OLDEST copy of a mutated record win every
# first-match lookup and emits that record twice in every list read. Both
# readers route through this one helper — retrieve.py on the CLI,
# mind_api/src/world/reasoning_bank.py on the daemon — so these pin the
# contract its docstring states, which neither reader's own tests assert
# directly.
# ---------------------------------------------------------------------------

def test_dedup_keeps_the_last_occurrence_content():
    """Newest path wins. This is the whole point of the helper."""
    import _utilization_store as us
    got = us.dedup_by_id([
        {"id": "a", "status": "active"},
        {"id": "a", "status": "retired"},
    ])
    assert got == [{"id": "a", "status": "retired"}]


def test_dedup_keeps_the_first_occurrence_position():
    """Position is first-seen, content is newest.

    That combination is deliberate: it is the minimal delta over the
    concatenation this replaces, so callers that do not sort observe no
    reordering.
    """
    import _utilization_store as us
    got = us.dedup_by_id([
        {"id": "a", "n": 1},
        {"id": "b", "n": 1},
        {"id": "a", "n": 2},
    ])
    assert [r["id"] for r in got] == ["a", "b"]
    assert got[0]["n"] == 2


def test_dedup_is_a_no_op_when_no_id_repeats():
    """The single-file-residency case, and the state of every box today: byte
    identical output, so wiring this in front of both readers changed nothing
    yet. It is also why the helper does not need the writer's residency
    question settled before it can ship."""
    import _utilization_store as us
    items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert us.dedup_by_id(items) == items


def test_dedup_never_collapses_records_without_a_usable_id():
    """Missing, None, empty and non-string ids each pass through as themselves.

    Keying on any of those would fold unrelated records into one, which
    surfaces as a SHORT store — the failure direction this module refuses to
    take silently.
    """
    import _utilization_store as us
    items = [{"no": "id"}, {"id": None}, {"id": ""}, {"id": 7}, {"no": "id"}]
    assert len(us.dedup_by_id(items)) == 5


def test_dedup_does_not_mutate_its_input():
    """Callers hand it a list built from the shared jsonl cache."""
    import _utilization_store as us
    items = [{"id": "a", "n": 1}, {"id": "a", "n": 2}]
    before = [dict(r) for r in items]
    us.dedup_by_id(items)
    assert items == before
