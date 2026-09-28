"""test_retrieval_trace_segment_starvation.py —  OUTCOME 3.

VERIFIED BY STARVATION, NOT BY GREP.

g-358-220 outcome 2 proved the SEAM works (`_retrieval_trace.trace_paths`
unions legacy + segments, pinned in test_retrieval_trace_segments.py). This
file proves the record-READERS are actually wired to it — the twin of
test_board_segment_starvation.py for the retrieval-trace store. No live
segment exists on the store yet (the writer lands default-OFF in outcome 4),
so a consumer enumerating through the seam and one still hardcoding
`retrieval-trace.jsonl` are OBSERVATIONALLY IDENTICAL today; a mis-routed
reader stays so until the segmented writer lands, which is exactly when it is
too late (board-reader-segment-starvation: the routing is unobservable to
every passing test, every grep, and every live read).

THE SHAPE. The fixture builds a tmp world dir holding a legacy
`retrieval-trace.jsonl` (1 record) PLUS a `retrieval-trace-<today>.jsonl`
segment (2 records); each consumer case runs the REAL consumer against it and
asserts it sees all three.

EVERY RECORD-READER CASE CARRIES ITS OWN CONTROL: the same consumer, same
fixture, with the seam monkeypatched back to the pre-seam single-file join,
must see ONLY the legacy record. A test that passes against both the routed
and the un-routed consumer is testing nothing (guard-2435); the discrimination
is checked by construction — `test_the_fixture_discriminates` asserts the two
expectations DIFFER before either is used.

THE POPULATION (measured 2026-09-27, alpha worker, zc-02: a census of every
file naming the key or the seam, plus a dynamic-name sweep for f-string/glob
consumers — the filing's list is a FLOOR, not the population):

  RECORD-READERS (have a record-level oracle, tested below with a control):
    gate-d-trace-contaminated  — the only consumer that parses trace ROWS;
                                 its join was the one hardcoded line outcome 3
                                 names (NEXT (a)).

  NAME-CLASSIFIERS (route on the BASENAME, no record oracle — a fixture would
  starve them of nothing; pinned by classification + shape tests instead):
    goal-reference-scan        — segments classify HISTORICAL (same append-only
                                 telemetry as the legacy name), siblings do NOT
                                 (NEXT (b)); the matcher is a LOCAL exact-date
                                 regex, not the seam module, by the file's own
                                 "keeps working when the seam module is absent"
                                 contract — so this file pins the two agree.
    fixture-leak-scan          — the legacy name is out of scope by DECISION
                                 (no retrieval surface, g-115-4371); SCANNED_
                                 STORES is exact-basename, so a segment can
                                 never enter the scan set — pinned by test,
                                 since the ruling lives only in the docstring.

  NOT IN THE POPULATION, deliberately (name the question, guard-3336):
    s3_gzip_gate_check         — a monitoring predicate over the S3 VERSION
                                 LISTING, not a record reader; its allowlist
                                 gain (`world/retrieval-trace-*.jsonl`) is owed
                                 by outcome 4 in the SAME change as the flip,
                                 and a segment added to the store but not to the
                                 list would read as a plain-PUT suspect, so the
                                 writer unit pins it at flip time.
    _owncloud_codec / retrieve.py / mind_api endpoints — the WRITER half,
                                 outcome 4 (the reader half must not touch the
                                 writer's path: outcome 4 keeps the bare append
                                 and flips behind RETRIEVAL_TRACE_SEGMENTED).
    iteration-close.sh / iteration-push.sh — comments only (verified at filing
                                 and re-verified in this census).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _retrieval_trace as rt  # noqa: E402


def _load(stem: str, filename: str):
    """Import a hyphen-named script by path (not a legal module name)."""
    spec = importlib.util.spec_from_file_location(stem, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LIVE_ID = "trace-live-0001"
SEG1_ID = "trace-seg-0002"
SEG2_ID = "trace-seg-0003"

ALL_THREE = {LIVE_ID, SEG1_ID, SEG2_ID}
STARVED = {LIVE_ID}
AGENT = "alpha"
TODAY = dt.date.today().isoformat()
SEGMENT_NAME = f"retrieval-trace-{TODAY}.jsonl"


def _row(row_id: str) -> dict:
    """A trace row shaped like the writer's (retrieve.py _log_retrieval_trace):
    the consumer keys on (agent, goal_id) and reads ts/category, so the row
    carries those; the discriminator id rides in goal_id."""
    return {
        "ts": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": AGENT,
        "goal_id": row_id,
        "category": "infrastructure-cost",
        "depth": "shallow",
        "tier_satisfied": 1,
    }


def _build_world(tmp_path: Path) -> Path:
    """A world dir whose trace store holds the legacy file AND one segment."""
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)

    def _w(name: str, recs):
        (world / name).write_text(
            "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")

    _w(rt.LEGACY_STORE_NAME, [_row(LIVE_ID)])
    _w(SEGMENT_NAME, [_row(SEG1_ID), _row(SEG2_ID)])
    # Siblings a prefix-glob reader would swallow (and that the seam's exact
    # date shape must NOT): they hold marker ids that MUST stay unseen.
    _w("retrieval-trace-archive.jsonl", [_row("trace-archive-9999")])
    _w("retrieval-trace-2026-09-27.jsonl.bak", [_row("trace-bak-9999")])
    _w("retrieval-trace-2026-09-27.spool.jsonl", [_row("trace-spool-9999")])
    return world


def _single_file_join(world_dir):
    """THE PRE-SEAM BEHAVIOUR, restored verbatim: one hardcoded
    `retrieval-trace.jsonl` join that cannot see a segment. The control."""
    p = Path(world_dir) / rt.LEGACY_STORE_NAME
    return [p] if p.exists() else []


def _seen_ids(rows) -> set:
    return {str(r.get("goal_id")) for r in rows}


# ------------------------------------------------- gate-d-trace-contaminated ---
# The one record-reading consumer. It exposes `_read_trace_rows` as the seam
# entry point precisely so this test can run the REAL production path (not a
# re-implementation of it) with the control monkeypatching the seam module
# reference the function looks up.

def _run_gate_d(mod, world):
    return _seen_ids(mod._read_trace_rows(str(world)))


def test_gate_d_reader_sees_the_segment(tmp_path):
    """ROUTED: the tracer must see the segment's rows, exactly — including
    that it did NOT swallow the same-stem siblings."""
    world = _build_world(tmp_path)
    mod = _load("gdtc_starve", "gate-d-trace-contaminated.py")
    seen = _run_gate_d(mod, world)
    assert seen == ALL_THREE, (
        f"gate-d tracer saw {sorted(seen)} but the store holds {sorted(ALL_THREE)} "
        f"— the {SEGMENT_NAME} segment was not enumerated through trace_paths "
        f"(a prefix-glob would also drag in the archive/.bak/spool siblings; "
        f"exact-set assertion catches both)")


def test_gate_d_control_without_routing_starves(tmp_path, monkeypatch):
    """CONTROL: with the seam returning the pre-seam single-file join, the
    SAME reader must miss the segment. Proves the assertion above measures the
    routing, not something incidental (guard-2435)."""
    world = _build_world(tmp_path)
    mod = _load("gdtc_starve", "gate-d-trace-contaminated.py")
    monkeypatch.setattr(mod, "trace_paths", _single_file_join)
    seen = _run_gate_d(mod, world)
    assert seen == STARVED, (
        f"gate-d tracer saw {sorted(seen)} with routing disabled; expected only "
        f"{sorted(STARVED)}. The fixture is not discriminating, so the routed "
        f"assertion proves nothing.")


# ------------------------------------------------------- goal-reference-scan ---
# Name-classifier: no record oracle (it routes on basenames). Pinned two ways —
# the classification itself, and that its LOCAL matcher (kept seam-free by the
# file's own contract) agrees with the seam module's predicate.

def _load_scan_mod():
    return _load("grs_starve", "goal-reference-scan.py")


def test_scan_classifies_the_segment_historical(tmp_path):
    world = _build_world(tmp_path)
    mod = _load_scan_mod()
    seg = world / SEGMENT_NAME
    rel = seg.relative_to(tmp_path)
    assert mod.is_historical(seg, rel) is True, (
        f"{SEGMENT_NAME} is the same append-only telemetry as the legacy name — "
        f"an unmatched segment re-introduces the always-fires failure mode for "
        f"every goal that ever retrieved (the block g-358-220 NEXT (b) exists for)")


def test_scan_classifies_the_legacy_name_historical(tmp_path):
    """Positive control for the classifier itself: the enumerated legacy name
    MUST classify historical, so the segment result above cannot be a
    classifier-wide inversion (guard-6119: a control is only a control if it
    shares the writer and surface — same function, same fixture)."""
    world = _build_world(tmp_path)
    mod = _load_scan_mod()
    legacy = world / rt.LEGACY_STORE_NAME
    rel = legacy.relative_to(tmp_path)
    assert mod.is_historical(legacy, rel) is True


def test_scan_does_not_sweep_in_same_stem_siblings(tmp_path):
    """The matcher is EXACT date shape, not a stem: .bak/spool siblings (which
    match NO other rule) must stay BLOCKING — a stem-match would have swallowed
    them and waved live referents through. `retrieval-trace-archive.jsonl` is
    asserted separately: it IS historical, but by the pre-existing
    `<store>-archive.jsonl` SUFFIX rule (the 3,211-hit design), not by the
    segment matcher — pinning that so the two rules stay attributable."""
    world = _build_world(tmp_path)
    mod = _load_scan_mod()
    for name in ("retrieval-trace-2026-09-27.jsonl.bak",
                 "retrieval-trace-2026-09-27.spool.jsonl"):
        p = world / name
        rel = p.relative_to(tmp_path)
        assert mod.is_historical(p, rel) is False, (
            f"{name} is a same-stem sibling, not a date segment — sweeping it "
            f"in would block nothing it should and could wave live referents "
            f"through")
    p = world / "retrieval-trace-archive.jsonl"
    rel = p.relative_to(tmp_path)
    assert mod.is_historical(p, rel) is True, (
        "the archive sibling is historical BY THE SUFFIX RULE (any "
        "<store>-archive.jsonl is narration); if it stopped classifying, the "
        "always-fires failure mode returns for the whole archive family")


def test_scan_local_shape_agrees_with_the_seam_module():
    """The scanner's matcher is restated (not imported) BY DESIGN — its
    contract is to keep working when the seam module is absent. Pin the two
    literal shapes so they cannot drift apart: they must agree on the whole
    discriminating population."""
    scan = _load_scan_mod()
    names = [
        SEGMENT_NAME,                          # today's segment (routed day)
        "retrieval-trace-2026-09-27.jsonl",    # a segment on a fixed day
        "retrieval-trace.jsonl",               # legacy: not a segment
        "retrieval-trace-archive.jsonl",
        "retrieval-trace-2026-09-27.jsonl.bak",
        "retrieval-trace-2026-09-27.spool.jsonl",
        "retrieval-trace-2026-9-27.jsonl",     # unpadded day: not a segment
        "xretrieval-trace-2026-09-27.jsonl",   # other stem sharing the date
        "some-other-ledger-2026-09-27.jsonl",  # date shape, other store
        "",
    ]
    for name in names:
        local = bool(scan._RETRIEVAL_TRACE_SEGMENT_RE.match(name))
        seam = rt.is_segment(name)
        assert local == seam, (
            f"goal-reference-scan's local matcher and _retrieval_trace disagree "
            f"on {name!r}: local={local} seam={seam} — the restated shape "
            f"drifted from the shared predicate")


# ---------------------------------------------------------- fixture-leak-scan ---
# Scope-predicate: the legacy name's out-of-scope ruling () lives in
# the docstring; SCANNED_STORES is exact-basename, so pin that a segment can
# never enter the scan set — by construction AND by the decision it inherits.

def _load_leak_mod():
    return _load("fls_starve", "fixture-leak-scan.py")


def test_leak_scan_scope_excludes_the_segment(tmp_path):
    world = _build_world(tmp_path)
    mod = _load_leak_mod()
    scanned = {name for name, _ in mod.SCANNED_STORES}
    assert rt.LEGACY_STORE_NAME not in scanned, (
        "the legacy name is out of scope by the g-115-4371 decision — "
        "adding it would contradict the docstring ruling")
    assert SEGMENT_NAME not in scanned
    # A segment can never enter the set even if a future writer lands: the set
    # is exact basenames, and no exact basename matches the date pattern.
    for name in scanned:
        assert not rt.is_segment(name), (
            f"SCANNED_STORES entry {name!r} is itself a retrieval-trace "
            f"segment shape — the scope ruling is violated by construction")


# ------------------------------------------------------------ the oracle itself ---

def test_the_fixture_discriminates():
    """If routed and starved expectations ever coincide, every case above is
    vacuous. Asserted once, up front, rather than assumed (exact sets, never
    non-emptiness: non-emptiness is satisfied BY THE DEFECT)."""
    assert ALL_THREE != STARVED
    assert STARVED < ALL_THREE
    assert len(ALL_THREE) == 3 and len(STARVED) == 1
