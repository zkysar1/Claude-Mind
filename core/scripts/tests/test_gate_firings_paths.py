"""Tests for the gate-firings store-composition seam (_gate_log.firings_paths).

g-328-38. The seam exists so `meta/gate-firings.jsonl` can be split into date
segments without touching its three consumers (gate-stats.py,
gate-retirement-eval.py, override-ledger-consume.py), each of which previously
hardcoded the filename.

Two properties carry real risk and are tested as such rather than structurally:

1. SPOOL EXCLUSION. The machine-local spool files share the `gate-firings`
   stem (`gate-firings.spool.jsonl`) but are NOT part of the shared store --
   they are drained into it by gate-firings-flush.py and are listed in
   owncloud_sync._EXCLUDE_NAMES. If the seam picked them up, every consumer
   would double-count the un-flushed tail.

2. SEGMENTS ARE ACTUALLY ADDED. test_segments_change_the_result is the mutation
   proof: it fails if firings_paths is reverted to returning only the legacy
   file, which is exactly the regression that would silently starve consumers
   to a few hours of data while they report a 30-day window.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _gate_log import firings_paths  # noqa: E402


def _write(p: Path, text: str = '{"ts": "2026-07-31T00:00:00"}\n') -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def test_empty_dir_returns_nothing(tmp_path):
    assert firings_paths(tmp_path) == []


def test_legacy_only(tmp_path):
    legacy = _write(tmp_path / "gate-firings.jsonl")
    assert firings_paths(tmp_path) == [legacy]


def test_segments_follow_legacy_in_chronological_order(tmp_path):
    legacy = _write(tmp_path / "gate-firings.jsonl")
    # Deliberately created out of order -- ISO dates sort lexically, so the
    # seam must return them chronologically regardless of creation order.
    seg_c = _write(tmp_path / "gate-firings-2026-08-03.jsonl")
    seg_a = _write(tmp_path / "gate-firings-2026-08-01.jsonl")
    seg_b = _write(tmp_path / "gate-firings-2026-08-02.jsonl")

    assert firings_paths(tmp_path) == [legacy, seg_a, seg_b, seg_c]


def test_segments_without_legacy(tmp_path):
    """After the legacy file ages out, segments alone are the store."""
    seg = _write(tmp_path / "gate-firings-2026-08-01.jsonl")
    assert firings_paths(tmp_path) == [seg]


@pytest.mark.parametrize("spool_name", [
    "gate-firings.spool.jsonl",
    "gate-firings.spool.flushing.jsonl",
])
def test_spool_files_are_excluded(tmp_path, spool_name):
    """The spool shares the stem but is machine-local and NOT part of the store.

    Including it would double-count every un-flushed record in all three
    consumers.
    """
    legacy = _write(tmp_path / "gate-firings.jsonl")
    _write(tmp_path / spool_name)

    assert firings_paths(tmp_path) == [legacy]


def test_spool_excluded_even_when_hyphenated(tmp_path):
    """A hyphenated spool name must still be excluded.

    This test FAILED on first run and the implementation was wrong, not the
    expectation. The original seam globbed `gate-firings-*.jsonl` and excluded
    spools with `name.startswith("gate-firings.spool")` -- keyed on the DOTTED
    production form, which that glob can never produce. So the exclusion was
    structurally dead: it could only ever fire for names that never reached it,
    while `gate-firings-spool.jsonl` sailed through. Replaced by a strict
    date-shaped `_SEGMENT_RE`, which excludes non-segments by construction
    rather than by a denylist needing sync with every future sibling file.
    """
    legacy = _write(tmp_path / "gate-firings.jsonl")
    _write(tmp_path / "gate-firings-spool.jsonl")

    assert firings_paths(tmp_path) == [legacy]


def test_directories_are_not_returned(tmp_path):
    _write(tmp_path / "gate-firings.jsonl")
    (tmp_path / "gate-firings-2026-08-01.jsonl").mkdir()
    assert all(p.is_file() for p in firings_paths(tmp_path))


def test_segments_change_the_result(tmp_path):
    """MUTATION PROOF -- fails if firings_paths reverts to the legacy file only.

    Reading the union must yield strictly more records than the legacy file
    alone. A seam that quietly returns just the legacy path passes every
    structural test above but starves consumers in production, so this asserts
    on CONTENT rather than on path membership.
    """
    _write(tmp_path / "gate-firings.jsonl", '{"ts": "2026-07-01T00:00:00"}\n')
    _write(tmp_path / "gate-firings-2026-08-01.jsonl",
           '{"ts": "2026-08-01T00:00:00"}\n{"ts": "2026-08-01T01:00:00"}\n')

    lines = [ln for p in firings_paths(tmp_path)
             for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    assert len(lines) == 3, "segments were not included in the store composition"
    assert any("2026-08-01T01:00:00" in ln for ln in lines)


# ---------------------------------------------------------------------------
# Stage-2 writer contract (). The reader half is tested above; these
# pin the WRITER against it. Splitting a filename convention across two modules
# fails silently -- the writer keeps emitting files the reader ignores, so
# consumers read a few hours and report it as a 30-day window -- so the
# round-trip is asserted directly rather than each half in isolation.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402
import importlib.util  # noqa: E402

from _gate_log import _SEGMENT_RE, segment_name  # noqa: E402


def _load_flush_module():
    """Import gate-firings-flush.py, whose hyphenated name blocks a plain import."""
    path = Path(__file__).resolve().parents[1] / "gate-firings-flush.py"
    spec = importlib.util.spec_from_file_location("gate_firings_flush", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_segment_name_matches_the_readers_pattern():
    """The writer's filename must satisfy the reader's matcher -- the contract."""
    assert _SEGMENT_RE.match(segment_name(_dt.date(2026, 8, 1)))
    assert segment_name(_dt.date(2026, 8, 1)) == "gate-firings-2026-08-01.jsonl"


def test_segment_name_defaults_to_today():
    assert segment_name() == f"gate-firings-{_dt.datetime.now().date().isoformat()}.jsonl"


def test_writer_target_defaults_to_legacy_store(tmp_path, monkeypatch):
    """Flag OFF must be byte-identical to pre-flag behaviour."""
    mod = _load_flush_module()
    monkeypatch.delenv(mod.SEGMENTED_ENV, raising=False)
    assert mod._store_path(tmp_path) == tmp_path / "gate-firings.jsonl"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_writer_target_is_a_segment_when_enabled(tmp_path, monkeypatch, value):
    mod = _load_flush_module()
    monkeypatch.setenv(mod.SEGMENTED_ENV, value)
    target = mod._store_path(tmp_path)
    assert target.name == segment_name()
    assert _SEGMENT_RE.match(target.name)


@pytest.mark.parametrize("value", ["", "0", "no", "off", "false"])
def test_flag_is_off_unless_explicitly_truthy(tmp_path, monkeypatch, value):
    mod = _load_flush_module()
    monkeypatch.setenv(mod.SEGMENTED_ENV, value)
    assert mod._store_path(tmp_path) == tmp_path / "gate-firings.jsonl"


def test_writer_output_is_discoverable_by_the_reader(tmp_path, monkeypatch):
    """ROUND-TRIP -- what the writer emits, the reader must find.

    This is the test that would have caught a writer/reader filename drift.
    Asserting each half separately passes even when they disagree; only the
    round-trip binds them.
    """
    mod = _load_flush_module()
    monkeypatch.setenv(mod.SEGMENTED_ENV, "1")

    target = mod._store_path(tmp_path)
    target.write_text('{"ts": "2026-08-01T00:00:00"}\n', encoding="utf-8")

    assert target in firings_paths(tmp_path), (
        "writer emitted a path the reader does not recognise as part of the store"
    )


# ---------------------------------------------------------------------------
#  -- two defects /fresh-eyes-code found in the seam above, ~2h after it
# landed. Both are one-liners; both were invisible to the 22 tests already in
# this file, and the two blind spots have DIFFERENT shapes worth naming.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag_on", [False, True])
def test_flush_report_names_the_resolved_target(tmp_path, monkeypatch, capsys,
                                                flag_on):
    """The flush's success line must name the file it ACTUALLY wrote.

    Defect: the report interpolated STORE_NAME -- the hardcoded legacy constant
    -- while the flush wrote `store`, the resolved target. So with segmentation
    ON it appended to gate-firings-YYYY-MM-DD.jsonl and announced
    gate-firings.jsonl: wrong in exactly the configuration the flag exists to
    enable, and correct everywhere else. An operator rolling the flag out reads
    the line as confirmation the flag did nothing.

    PARAMETRIZED OVER BOTH FLAG STATES ON PURPOSE (rb-4133). A flag-off-only
    assertion passes against the buggy code -- indeed it would have passed
    before the flag existed at all -- so it has no discriminating power. Only
    the flag-ON case fails against the defect, and it is only meaningful
    alongside flag-OFF proving the legacy path did not regress.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")  # guard-955
    mod = _load_flush_module()
    if flag_on:
        monkeypatch.setenv(mod.SEGMENTED_ENV, "1")
    else:
        monkeypatch.delenv(mod.SEGMENTED_ENV, raising=False)

    _write(tmp_path / mod.SPOOL_NAME, '{"ts": "2026-08-01T00:00:00"}\n')
    # main() parses sys.argv rather than taking argv, so drive it the way the
    # cron wrapper does rather than reaching past the entry point.
    monkeypatch.setattr(
        sys, "argv",
        ["gate-firings-flush.py", "--meta-dir", str(tmp_path), "--force"],
    )
    mod.main()

    line = capsys.readouterr().out
    expected = mod._store_path(tmp_path).name
    assert "flushed" in line, f"no flush report emitted: {line!r}"
    assert expected in line, (
        f"report names the wrong target: expected {expected!r} in {line!r}"
    )
    if flag_on:
        # The discriminating half: under the defect this said the legacy name.
        assert _SEGMENT_RE.match(expected)
        assert "gate-firings.jsonl" not in line


def test_firings_paths_returns_empty_when_meta_dir_is_unresolved(monkeypatch):
    """A no-arg call with an unresolved META_DIR must return [], not raise.

    Defect: `_Path(meta_dir) if meta_dir is not None else _Path(META_DIR)`
    guarded the PARAMETER but not the module constant, so _Path(None) raised
    TypeError. Unresolved paths are a real runtime state, not an error -- the
    writer already treats them as "nothing to do"
    (gate-firings-flush.py:191), and the reader now matches it.

    WHY 22 GREEN TESTS MISSED THIS: every one of them passes an explicit
    tmp_path, so they only ever exercise the parameter branch. The untested
    lane is the no-arg call -- which is the shape the docstring invites, and
    the only shape that can reach the bug. Coverage of a function is not
    coverage of its default argument.
    """
    import _gate_log

    monkeypatch.setattr(_gate_log, "META_DIR", None)
    assert _gate_log.firings_paths() == []


def test_unresolved_constant_does_not_affect_an_explicit_arg(tmp_path,
                                                             monkeypatch):
    """The None guard must not swallow a caller who passed a real directory.

    Pins that the fix is a guard on the fallback, not a short-circuit on the
    whole function -- the failure mode a careless `if META_DIR is None: return
    []` at the top would introduce, silently blinding all three consumers.
    """
    import _gate_log

    legacy = _write(tmp_path / "gate-firings.jsonl")
    monkeypatch.setattr(_gate_log, "META_DIR", None)
    assert _gate_log.firings_paths(tmp_path) == [legacy]


# ---------------------------------------------------------------------------
#  (Stage 2 cutover) -- the ECONOMIC claim, which nothing above pins.
#
# Every writer test above asserts where the flush GOES. None asserts what it
# LEAVES ALONE, and that is the entire point of the flag: the seam exists to
# stop re-PUTting a 64MB / 176k-record object to append ~1KB (measured cc-05
# 2026-08-12; 44.75MB/127k when the seam landed 12 days earlier, so the object
# is still growing). If a future change makes the flush touch the legacy file
# as well -- a migration pass, a compaction, an "append to both for safety"
# fallback -- every assertion in this file still passes, the reader still finds
# every record, consumers still report the full window, and the ~40x saving is
# silently gone with nothing looking broken. That is why this is asserted on
# BYTES rather than on record counts: a correctness-preserving rewrite of the
# legacy file is exactly the regression, and it is invisible to any check that
# only asks whether the data is still readable.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag_on", [False, True])
def test_flush_writes_only_the_live_segment(tmp_path, monkeypatch, flag_on):
    """Flag ON: the legacy object is left byte-for-byte untouched.

    PARAMETRIZED OVER BOTH FLAG STATES (rb-4133, same reasoning as
    test_flush_report_names_the_resolved_target). The flag-OFF leg is not
    ceremony -- it is the positive control that proves this test can observe a
    legacy write at all. Without it, a test that broke and stopped exercising
    the flush would report the legacy file unchanged and PASS, which is the
    vacuous green this assertion is least able to afford.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")  # guard-955
    mod = _load_flush_module()
    if flag_on:
        monkeypatch.setenv(mod.SEGMENTED_ENV, "1")
    else:
        monkeypatch.delenv(mod.SEGMENTED_ENV, raising=False)

    legacy = _write(tmp_path / "gate-firings.jsonl",
                    '{"ts": "2026-07-23T00:00:00", "gate_id": "old"}\n')
    before = legacy.read_bytes()

    _write(tmp_path / mod.SPOOL_NAME,
           '{"ts": "2026-08-12T00:00:00", "gate_id": "new"}\n')
    monkeypatch.setattr(
        sys, "argv",
        ["gate-firings-flush.py", "--meta-dir", str(tmp_path), "--force"],
    )
    mod.main()

    if flag_on:
        assert legacy.read_bytes() == before, (
            "segmented flush rewrote the legacy object -- the ~40x write-"
            "amplification saving is gone even though every record is still "
            "readable"
        )
        segment = tmp_path / segment_name()
        assert segment.is_file(), "no live segment was written"
        assert b'"gate_id": "new"' in segment.read_bytes()
    else:
        # Positive control: with the flag off the flush DOES write the legacy
        # file, so the assertion above is capable of failing.
        assert legacy.read_bytes() != before

    # Either way the reader must still see both records -- segmenting must not
    # cost a consumer any part of its window.
    seen = sum(sum(1 for line in p.read_text().splitlines() if line.strip())
               for p in firings_paths(tmp_path))
    assert seen == 2, f"reader lost records across the cutover: {seen} != 2"


# ---------------------------------------------------------------------------
# 2026-08-18 -- the DIRECT lane. `_gate_log.log()` has two writer lanes: the
# own-cloud spool (drained by the flush above) and a direct locked append used
# by every other backend -- and, until this fix, by any own-cloud process whose
# env did not literally carry STORAGE_BACKEND=own-cloud (a bare CLI gate script
# on a registry-native box). That lane never consulted the segment flag, so for
# the 12h after the fleet flip it kept re-heating the 68 MB legacy object
# (~1000 whole-object RMWs, measured on the store tail). Both defects are pinned
# here: the direct lane must honour the flag, and the spool decision must be
# made from the RESOLVED backend, not a raw env read.
# ---------------------------------------------------------------------------

import _gate_log as _gl  # noqa: E402


def _direct_lane(monkeypatch, tmp_path):
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    monkeypatch.setenv("STORAGE_BACKEND", "local")     # direct lane, hermetic
    return tmp_path


def _lines(p: Path):
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()] \
        if p.is_file() else []


def test_direct_lane_default_is_the_legacy_file(tmp_path, monkeypatch):
    meta = _direct_lane(monkeypatch, tmp_path)
    monkeypatch.delenv(_gl.SEGMENTED_ENV, raising=False)
    _gl.log("direct-lane-test", "pass", caller="test", meta_dir=meta)
    assert len(_lines(meta / _gl.LEGACY_STORE_NAME)) == 1
    assert not (meta / segment_name()).exists()


def test_direct_lane_writes_the_segment_when_enabled(tmp_path, monkeypatch):
    """The regression: flag ON, direct lane -> today's segment, legacy untouched."""
    meta = _direct_lane(monkeypatch, tmp_path)
    monkeypatch.setenv(_gl.SEGMENTED_ENV, "1")
    legacy = _write(meta / _gl.LEGACY_STORE_NAME,
                    '{"ts": "2026-07-23T00:00:00", "gate_id": "old"}\n')
    before = legacy.read_bytes()

    _gl.log("direct-lane-test", "block", caller="test", meta_dir=meta)

    assert legacy.read_bytes() == before, (
        "direct lane appended to the legacy object with the segment flag on")
    seg = meta / segment_name()
    assert len(_lines(seg)) == 1 and '"gate_id": "direct-lane-test"' in _lines(seg)[0]
    # Round-trip: what the direct lane wrote, the reader finds.
    assert seg in firings_paths(meta)
    seen = sum(len(_lines(p)) for p in firings_paths(meta))
    assert seen == 2


def test_direct_lane_and_flush_lane_share_one_target_rule(tmp_path, monkeypatch):
    """Mutation proof for the fix's shape: the two writer lanes must resolve the
    same basename for the same flag value. A future edit that re-inlines the
    rule in one lane fails here the moment the two drift."""
    mod = _load_flush_module()
    for value in ("", "0", "1", "yes"):
        monkeypatch.setenv(_gl.SEGMENTED_ENV, value)
        assert mod._store_path(tmp_path).name == _gl.store_name()


def test_spool_decision_uses_the_resolved_backend_when_env_is_unset(monkeypatch):
    """Registry-native box: STORAGE_BACKEND absent from env, bootstrap resolves
    own-cloud -> spool. Explicit pin -> no bootstrap call at all."""
    import storage_backend as sb
    calls = []

    def fake_bootstrap(*a, **k):
        calls.append(1)
        import os
        os.environ.setdefault("STORAGE_BACKEND", "own-cloud")

    monkeypatch.setattr(sb, "_bootstrap_env_defaults", fake_bootstrap)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    assert _gl._spool_active() is True
    assert calls == [1]

    # Explicit pins short-circuit: neither value consults the bootstrap.
    calls.clear()
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    assert _gl._spool_active() is False
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    assert _gl._spool_active() is True
    assert calls == []


def test_spool_decision_is_false_when_nothing_resolves(monkeypatch):
    """Fresh local-only clone: env unset AND the bootstrap has nothing to give
    (or raises) -> direct lane, exactly as before this change."""
    import storage_backend as sb
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(sb, "_bootstrap_env_defaults", lambda *a, **k: None)
    assert _gl._spool_active() is False

    def boom(*a, **k):
        raise RuntimeError("no registry")
    monkeypatch.setattr(sb, "_bootstrap_env_defaults", boom)
    assert _gl._spool_active() is False


def test_log_spools_instead_of_appending_when_env_unset_resolves_own_cloud(
        tmp_path, monkeypatch):
    """End-to-end for the measured incident: a bare process on a registry-
    native box must land the record in the machine-local spool, never in the
    shared store file."""
    import storage_backend as sb
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(sb, "_bootstrap_env_defaults",
                        lambda *a, **k: __import__("os").environ.setdefault(
                            "STORAGE_BACKEND", "own-cloud"))
    monkeypatch.setenv(_gl.SEGMENTED_ENV, "1")

    _gl.log("bare-process-gate", "pass", caller="test", meta_dir=tmp_path)

    spool = tmp_path / _gl._SPOOL_NAME
    assert len(_lines(spool)) == 1 and '"gate_id": "bare-process-gate"' in _lines(spool)[0]
    assert not (tmp_path / _gl.LEGACY_STORE_NAME).exists()
    assert not (tmp_path / segment_name()).exists()
    assert firings_paths(tmp_path) == []      # the spool is not part of the store
