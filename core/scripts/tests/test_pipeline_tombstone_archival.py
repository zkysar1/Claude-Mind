"""Tests for tombstone-in-live archival ().

Root cause being guarded: the own-cloud merge handler
(coordination_merge.merge_pipeline) is a per-file union-by-id — a cross-file
MOVE's remove half is inexpressible in it, so a pre-removal remote copy
resurrected archived records at their OLD stage (94 discovered-stage records
came back after the g-115-1976 archival). The fix keeps archived records in
pipeline.jsonl as stage=archived tombstones (the monotonic stage rank makes
the flip converge fleet-wide), prunes them after PRUNE_GRACE_DAYS, and dedups
the live+archive join in meta/replay readers.

Covers:
  - move-to-archived keeps a live tombstone + appends archive copy once
  - idempotent re-move (no within-archive duplicate)
  - archive_sweep flips in place, prunes aged tombstones, keeps young ones,
    stamps unstamped ones
  - the prune folds a tombstone's post-archival writes into its archive copy
    first, and never prunes the only copy (g-115-10778)
  - merge_pipeline convergence: tombstone flip survives a pre-flip remote
    (and the removal-resurrection root cause is pinned as a semantics proof)
  - compute_meta dedup parity (CLI + daemon)
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import coordination_merge  # noqa: E402
import pipeline as cli_pipeline  # noqa: E402
from mind_api.src.world import pipeline as pipeline_read  # noqa: E402
from mind_api.src.world import pipeline_write  # noqa: E402

# Fleet adoption landed (, 2026-07-16): tombstone-in-live archival
# now lives in pipeline_write.move / archive_sweep (+ compute_meta dedup on
# both CLI and daemon) — these are hard regression tests. (History: the fix
# was first cc-02-local, , superseded at the  unwedge
# merge; this file carried the spec as strict=False xfail pins until the
# fleet fix shipped.)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _Paths:
    def __init__(self, world: Path):
        self.world = world


class FakeCtx:
    def __init__(self, world: Path, query=None, body: bytes = b""):
        self.paths = _Paths(world)
        self.query = query or {}
        self.body = body
        self.headers = {}


def _rec(rec_id: str, stage: str, **over):
    base = {
        "id": rec_id,
        "title": f"hypothesis {rec_id}",
        "stage": stage,
        "horizon": "micro",
        "type": "exploration",
        "confidence": 0.5,
        "position": "YES this converges under union merge",
        "formed_date": "2026-07-01",
        "category": "framework-meta",
        "slug": rec_id.split("_", 1)[1],
        "rationale": "seeded by test",
        "outcome": None,
        "reflected": False,
        "surprise": None,
        "experience_ref": None,
    }
    base.update(over)
    return base


def _write_jsonl(path: Path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=True) + "\n")


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _seed_world(tmp_path: Path, live, archive=()):
    world = tmp_path / "world"
    world.mkdir(exist_ok=True)
    _write_jsonl(world / "pipeline.jsonl", live)
    _write_jsonl(world / "pipeline-archive.jsonl", list(archive))
    return world


# ---------------------------------------------------------------------------
# move-to-archived
# ---------------------------------------------------------------------------

def test_move_to_archived_keeps_live_tombstone(tmp_path):
    rid = "2026-07-01_tombstone-a"
    world = _seed_world(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    resp = pipeline_write.move(FakeCtx(world, {"id": rid, "stage": "archived"}))
    assert resp.status == 200, getattr(resp, "body", resp)

    live = _read_jsonl(world / "pipeline.jsonl")
    arch = _read_jsonl(world / "pipeline-archive.jsonl")
    assert [r["id"] for r in live] == [rid], "record must STAY in live as tombstone"
    assert live[0]["stage"] == "archived"
    assert live[0].get("archived_date"), "tombstone must carry its prune clock"
    assert [r["id"] for r in arch] == [rid], "archive copy appended exactly once"


def test_move_to_archived_idempotent_no_double_append(tmp_path):
    rid = "2026-07-01_tombstone-b"
    world = _seed_world(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    for _ in range(2):
        resp = pipeline_write.move(FakeCtx(world, {"id": rid, "stage": "archived"}))
        assert resp.status == 200
    arch = _read_jsonl(world / "pipeline-archive.jsonl")
    assert len(arch) == 1, "re-moving a tombstone must not duplicate the archive copy"


# ---------------------------------------------------------------------------
# archive_sweep
# ---------------------------------------------------------------------------

def _old(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def test_archive_sweep_flips_in_place_and_appends_once(tmp_path):
    rid = "2026-07-01_sweep-flip"
    world = _seed_world(
        tmp_path,
        [_rec(rid, "resolved", outcome="CONFIRMED", outcome_date=_old(10))])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["archived_count"] == 1

    live = _read_jsonl(world / "pipeline.jsonl")
    arch = _read_jsonl(world / "pipeline-archive.jsonl")
    assert [r["id"] for r in live] == [rid], "swept record stays as live tombstone"
    assert live[0]["stage"] == "archived"
    assert live[0]["archived_date"] == date.today().isoformat()
    assert [r["id"] for r in arch] == [rid]


def test_archive_sweep_prunes_aged_tombstone(tmp_path):
    rid = "2026-07-01_sweep-prune"
    aged = _rec(rid, "archived", outcome="CONFIRMED",
                archived_date=_old(pipeline_write.PRUNE_GRACE_DAYS + 3))
    world = _seed_world(tmp_path, [aged], archive=[dict(aged)])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["pruned_count"] == 1
    live = _read_jsonl(world / "pipeline.jsonl")
    assert live == [], "aged tombstone physically pruned from live"
    arch = _read_jsonl(world / "pipeline-archive.jsonl")
    assert [r["id"] for r in arch] == [rid], "archive copy untouched by prune"


def test_archive_sweep_keeps_young_tombstone(tmp_path):
    rid = "2026-07-01_sweep-young"
    young = _rec(rid, "archived", outcome="CONFIRMED",
                 archived_date=date.today().isoformat())
    world = _seed_world(tmp_path, [young], archive=[dict(young)])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["archived_count"] == 0
    assert body["pruned_count"] == 0
    live = _read_jsonl(world / "pipeline.jsonl")
    assert [r["id"] for r in live] == [rid], "young tombstone kept until grace elapses"


def test_archive_sweep_stamps_unstamped_tombstone(tmp_path):
    rid = "2026-07-01_sweep-stamp"
    unstamped = _rec(rid, "archived", outcome="CONFIRMED")
    world = _seed_world(tmp_path, [unstamped], archive=[dict(unstamped)])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    live = _read_jsonl(world / "pipeline.jsonl")
    assert [r["id"] for r in live] == [rid]
    assert live[0].get("archived_date") == date.today().isoformat(), \
        "unstamped tombstone gets its prune clock started (persisted)"


def test_archive_sweep_no_dup_append_for_resurrected_resolved(tmp_path):
    # A resolved record whose id ALREADY exists in the archive (resurrection
    # residue) must not be appended a second time — the pre-fix code did,
    # producing within-archive duplicate groups.
    rid = "2026-07-01_sweep-dup"
    resolved = _rec(rid, "resolved", outcome="CONFIRMED", outcome_date=_old(10))
    archived_copy = _rec(rid, "archived", outcome="CONFIRMED")
    world = _seed_world(tmp_path, [resolved], archive=[archived_copy])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    arch = _read_jsonl(world / "pipeline-archive.jsonl")
    assert len(arch) == 1, "no second archive copy for an id already archived"


def test_archive_sweep_skips_invalid_record_not_fatal(tmp_path):
    # : one corrupt record must NOT wedge the whole batch. A valid
    # aged record archives; an invalid aged record (confidence out of the
    # 0-1 range) is skipped-and-reported, stays in live un-flipped (visible),
    # and the sweep still returns 200 instead of 400-aborting everything.
    valid_id = "2026-07-01_sweep-valid"
    invalid_id = "2026-07-01_sweep-invalid"
    valid = _rec(valid_id, "resolved", outcome="CONFIRMED", outcome_date=_old(10))
    invalid = _rec(invalid_id, "resolved", outcome="CONFIRMED",
                   outcome_date=_old(10), confidence=5.0)
    world = _seed_world(tmp_path, [valid, invalid])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200, "one bad record must not 400 the whole batch"
    body = json.loads(resp.body)
    assert body["archived_count"] == 1, "the valid record still archives"
    skipped = body.get("skipped_invalid", [])
    assert [s["id"] for s in skipped] == [invalid_id], "invalid record reported"
    assert skipped[0]["branch"] == "resolved-archive"
    assert "confidence" in skipped[0]["reason"].lower()

    live = {r["id"]: r for r in _read_jsonl(world / "pipeline.jsonl")}
    arch = {r["id"]: r for r in _read_jsonl(world / "pipeline-archive.jsonl")}
    assert live[valid_id]["stage"] == "archived", "valid record flipped to tombstone"
    assert valid_id in arch, "valid record appended to archive"
    assert live[invalid_id]["stage"] == "resolved", \
        "invalid record left un-flipped in live so it stays visible"
    assert invalid_id not in arch, "invalid record must not reach the archive"


# ---------------------------------------------------------------------------
# effective resolution date ()
# ---------------------------------------------------------------------------
# Pre-fix, archive_sweep aged resolved records on outcome_date ONLY via a bare
# date.fromisoformat: 33/73 live resolved records were sweep-invisible (31 had
# no outcome_date — the move-to-resolved path never stamped one — and 2 carried
# datetime-format values the bare parse rejects). Fix: [:10] slice + fallback
# to resolution_date_actual/reflected_date in the sweep, plus an outcome_date
# stamp at the move-INTO-resolved chokepoint.


def test_archive_sweep_datetime_outcome_date_archives(tmp_path):
    rid = "2026-07-01_sweep-dt-od"
    world = _seed_world(
        tmp_path,
        [_rec(rid, "resolved", outcome="CONFIRMED",
              outcome_date=f"{_old(10)}T03:37:27")])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    assert json.loads(resp.body)["archived_count"] == 1, \
        "datetime-format outcome_date must parse via [:10], not except-keep"
    live = _read_jsonl(world / "pipeline.jsonl")
    assert live[0]["stage"] == "archived"


def test_archive_sweep_falls_back_to_resolution_date_actual(tmp_path):
    rid = "2026-07-01_sweep-rda"
    world = _seed_world(
        tmp_path,
        [_rec(rid, "resolved", outcome="CONFIRMED",
              resolution_date_actual=_old(10))])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    assert json.loads(resp.body)["archived_count"] == 1, \
        "no outcome_date: resolution_date_actual is the effective clock"


def test_archive_sweep_falls_back_to_reflected_date(tmp_path):
    rid = "2026-07-01_sweep-refd"
    world = _seed_world(
        tmp_path,
        [_rec(rid, "resolved", outcome="CONFIRMED", reflected=True,
              reflected_date=_old(10))])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    assert json.loads(resp.body)["archived_count"] == 1, \
        "no outcome_date/resolution_date_actual: reflected_date is the clock"


def test_archive_sweep_dateless_resolved_stays_live(tmp_path):
    rid = "2026-07-01_sweep-dateless"
    world = _seed_world(
        tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    live = _read_jsonl(world / "pipeline.jsonl")
    assert live[0]["stage"] == "resolved", \
        "a resolution with NO date field stays live (conservative keep)"


def test_archive_sweep_fresh_fallback_not_archived(tmp_path):
    rid = "2026-07-01_sweep-fresh"
    world = _seed_world(
        tmp_path,
        [_rec(rid, "resolved", outcome="CONFIRMED", reflected_date=_old(1))])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    assert json.loads(resp.body)["archived_count"] == 0
    live = _read_jsonl(world / "pipeline.jsonl")
    assert live[0]["stage"] == "resolved", \
        "fallback clock younger than ARCHIVE_AGE_DAYS keeps the record live"


def test_move_to_resolved_stamps_outcome_date(tmp_path):
    rid = "2026-07-01_move-stamp-od"
    world = _seed_world(
        tmp_path,
        [_rec(rid, "active", outcome="CONFIRMED",
              claim="the sweep archives records whose clock is stampable",
              outcome_detail="verified via g-115-2613",
              resolution_criteria="seeded", resolution_method="seeded by test")])
    resp = pipeline_write.move(FakeCtx(world, {"id": rid, "stage": "resolved"}))
    assert resp.status == 200, getattr(resp, "body", resp)
    live = _read_jsonl(world / "pipeline.jsonl")
    assert live[0].get("outcome_date") == date.today().isoformat(), \
        "move-to-resolved stamps the resolution clock the sweep ages on"


def test_move_to_resolved_keeps_explicit_outcome_date(tmp_path):
    rid = "2026-07-01_move-keep-od"
    explicit = _old(2)
    world = _seed_world(
        tmp_path,
        [_rec(rid, "active", outcome="CONFIRMED",
              claim="an explicit resolution clock survives the move stamp",
              outcome_detail="verified via g-115-2613",
              resolution_criteria="seeded", resolution_method="seeded by test",
              outcome_date=explicit)])
    resp = pipeline_write.move(FakeCtx(world, {"id": rid, "stage": "resolved"}))
    assert resp.status == 200, getattr(resp, "body", resp)
    live = _read_jsonl(world / "pipeline.jsonl")
    assert live[0].get("outcome_date") == explicit, \
        "an explicit caller-set outcome_date wins over the stamp"


# ---------------------------------------------------------------------------
# prune folds the tombstone into its archive copy ()
# ---------------------------------------------------------------------------
# update and update_field write the LIVE copy first, so every post-archival
# write lands on the tombstone while the archive copy stays frozen at first
# archival. The prune used to drop the tombstone and write nothing back, so
# those writes reverted: 10 of 28 at-risk records were measured reverted
# (replay_count down, an outcome edit about to flip back).

def _age_live_tombstone(world: Path, days: int) -> None:
    live = _read_jsonl(world / "pipeline.jsonl")
    for r in live:
        r["archived_date"] = _old(days)
    _write_jsonl(world / "pipeline.jsonl", live)


def test_archive_sweep_prune_keeps_post_archival_writes(tmp_path):
    rid = "2026-07-01_sweep-post-archival"
    world = _seed_world(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    resp = pipeline_write.move(FakeCtx(world, {"id": rid, "stage": "archived"}))
    assert resp.status == 200, getattr(resp, "body", resp)

    replay = {"replay_count": 2, "last_replayed": "2026-07-20",
              "next_review_date": "2026-08-20"}
    for field, value in (("replay_metadata", json.dumps(replay)),
                         ("outcome", "UNRESOLVABLE")):
        resp = pipeline_write.update_field(
            FakeCtx(world, {"id": rid, "field": field, "value": value}))
        assert resp.status == 200, getattr(resp, "body", resp)
    _age_live_tombstone(world, pipeline_write.PRUNE_GRACE_DAYS + 1)

    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["pruned_count"] == 1
    assert body["folded_count"] == 1
    assert _read_jsonl(world / "pipeline.jsonl") == [], "the tombstone is still pruned"

    got = pipeline_read.read(FakeCtx(world, {"id": rid}))
    assert got.status == 200
    rec = json.loads(got.body)
    assert rec["replay_metadata"] == replay, "post-archival replay stamp survives the prune"
    assert rec["outcome"] == "UNRESOLVABLE", "post-archival outcome edit survives the prune"


def test_archive_sweep_prune_unchanged_tombstone_writes_no_archive(tmp_path):
    rid = "2026-07-01_sweep-unchanged"
    aged = _rec(rid, "archived", outcome="CONFIRMED",
                archived_date=_old(pipeline_write.PRUNE_GRACE_DAYS + 3))
    world = _seed_world(tmp_path, [aged], archive=[dict(aged)])
    before = (world / "pipeline-archive.jsonl").read_bytes()
    body = json.loads(pipeline_write.archive_sweep(FakeCtx(world)).body)
    assert body["pruned_count"] == 1
    assert body["folded_count"] == 0
    assert (world / "pipeline-archive.jsonl").read_bytes() == before, \
        "a tombstone identical to its archive copy must not rewrite the archive"


def test_archive_sweep_never_prunes_the_only_copy(tmp_path):
    rid = "2026-07-01_sweep-no-archive-copy"
    aged = _rec(rid, "archived", outcome="CONFIRMED",
                archived_date=_old(pipeline_write.PRUNE_GRACE_DAYS + 3))
    world = _seed_world(tmp_path, [aged], archive=[])
    body = json.loads(pipeline_write.archive_sweep(FakeCtx(world)).body)
    assert body["pruned_count"] == 1
    assert body["archive_missing_count"] == 1
    assert _read_jsonl(world / "pipeline.jsonl") == []
    assert _read_jsonl(world / "pipeline-archive.jsonl") == [aged], \
        "a tombstone with no archive copy is archived before it is pruned"


def test_archive_sweep_fold_never_walks_the_archive_back(tmp_path):
    # A stale copy the union merge resurrected into live (guard-1072) must not
    # regress what the archive already holds: replay_count only grows, newer
    # dates win, reflected=True dominates, and a null never erases a value.
    rid = "2026-07-01_sweep-stale-tombstone"
    archive_copy = _rec(rid, "archived", outcome="CONFIRMED", reflected=True,
                        reflected_by="foxtrot", reflected_date="2026-07-15",
                        replay_metadata={"replay_count": 3,
                                         "last_replayed": "2026-07-25"})
    stale = _rec(rid, "archived", outcome="CONFIRMED", reflected=False,
                 reflected_by=None,
                 replay_metadata={"replay_count": 1,
                                  "last_replayed": "2026-07-10"},
                 archived_date=_old(pipeline_write.PRUNE_GRACE_DAYS + 3))
    world = _seed_world(tmp_path, [stale], archive=[archive_copy])
    body = json.loads(pipeline_write.archive_sweep(FakeCtx(world)).body)
    assert body["pruned_count"] == 1
    [rec] = _read_jsonl(world / "pipeline-archive.jsonl")
    assert rec["replay_metadata"] == {"replay_count": 3, "last_replayed": "2026-07-25"}
    assert rec["reflected"] is True
    assert rec["reflected_by"] == "foxtrot"
    assert rec["reflected_date"] == "2026-07-15"


def test_archive_sweep_fold_collapses_duplicate_archive_rows(tmp_path):
    # guard-2449: the archive carries historical duplicate-id rows. Folding
    # only the FIRST would leave a stale twin for the union merge to pick.
    rid = "2026-07-01_sweep-duplicate-rows"
    frozen = _rec(rid, "archived", outcome="CONFIRMED")
    live = _rec(rid, "archived", outcome="CONFIRMED",
                replay_metadata={"replay_count": 1, "last_replayed": "2026-07-20"},
                archived_date=_old(pipeline_write.PRUNE_GRACE_DAYS + 3))
    other = _rec("2026-07-02_sweep-bystander", "archived", outcome="CONFIRMED")
    world = _seed_world(tmp_path, [live],
                        archive=[dict(frozen), other, dict(frozen)])
    body = json.loads(pipeline_write.archive_sweep(FakeCtx(world)).body)
    assert body["folded_count"] == 1
    arch = _read_jsonl(world / "pipeline-archive.jsonl")
    assert [r["id"] for r in arch] == [rid, other["id"]], \
        "one folded row per id, bystanders untouched and in place"
    assert arch[0]["replay_metadata"]["replay_count"] == 1


def test_archive_sweep_idle_path_returns_full_key_set(tmp_path):
    # Fresh-eyes 2026-09-27 (): the early return (nothing
    # archived, pruned or stamped) used to omit folded_count and
    # archive_missing_count, so a caller branching on those keys saw a
    # different response shape depending on whether the sweep did anything.
    rid = "2026-07-01_sweep-idle"
    young = _rec(rid, "archived", outcome="CONFIRMED",
                 archived_date=date.today().isoformat())
    world = _seed_world(tmp_path, [young], archive=[dict(young)])
    resp = pipeline_write.archive_sweep(FakeCtx(world))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["archived_count"] == 0
    assert body["pruned_count"] == 0
    assert body["folded_count"] == 0, \
        "the idle path must carry the same key set as the normal return"
    assert body["archive_missing_count"] == 0


def test_archive_sweep_fold_then_flip_same_id_appends_once(tmp_path):
    # Fresh-eyes 2026-09-28 (): archive_ids is read at the top of
    # the live-locked block, BEFORE the fold. The fold APPENDS a pruned id
    # that had no archive copy (archive_missing_count), so the pre-fold set
    # is stale for exactly that id. If the append-once loop still checks the
    # pre-fold set, an id that is BOTH pruned (no copy -> the fold appends
    # it) AND flipped to archived in the same sweep lands TWO archive rows —
    # the duplicate group (guard-2449) the loop's own comment says cannot
    # form. Fails on the pre-update code.
    rid = "2026-07-01_sweep-fold-and-flip"
    aged = _rec(rid, "archived", outcome="CORRECTED",
                archived_date=_old(pipeline_write.PRUNE_GRACE_DAYS + 3))
    flipping = _rec(rid, "resolved", outcome="UNRESOLVABLE",
                    outcome_date=_old(pipeline_write.ARCHIVE_AGE_DAYS + 2))
    world = _seed_world(tmp_path, [aged, flipping])
    body = json.loads(pipeline_write.archive_sweep(FakeCtx(world)).body)
    assert body["pruned_count"] == 1
    assert body["archived_count"] == 1
    assert body["archive_missing_count"] == 1, \
        "the aged tombstone had no archive copy; the fold must append it"
    arch = _read_jsonl(world / "pipeline-archive.jsonl")
    assert [r["id"] for r in arch] == [rid], \
        "one archive row per id: the fold's append must be visible to the " \
        "append-once loop in the same sweep"


# ---------------------------------------------------------------------------
# merge semantics (the root cause + the fix's design property)
# ---------------------------------------------------------------------------

def _blob(recs) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in recs).encode()


def test_merge_removal_resurrects_root_cause_semantics():
    # Pins the ROOT CAUSE: union-by-id cannot express a removal. Local removed
    # the record (post-archival pipeline.jsonl); remote still has it at
    # discovered → the merge restores it at its OLD stage. This is the exact
    # mechanism that resurrected 94 records; if this test ever fails, the
    # merge semantics changed and the tombstone design should be revisited.
    rid = "2026-07-01_merge-root-cause"
    remote = [_rec(rid, "discovered")]
    merged = coordination_merge.merge_pipeline(_blob([]), _blob(remote))
    out = [json.loads(ln) for ln in merged.decode().splitlines() if ln.strip()]
    assert [r["id"] for r in out] == [rid]
    assert out[0]["stage"] == "discovered", \
        "union-by-id restores a removed record at its old stage (root cause)"


def test_merge_tombstone_flip_converges_to_archived():
    # The FIX's design property: an in-place stage flip IS expressible — the
    # monotonic stage rank converges the pre-flip remote copy to archived,
    # commutatively (both argument orders byte-identical).
    rid = "2026-07-01_merge-tombstone"
    local = [_rec(rid, "archived", archived_date="2026-07-11")]
    remote = [_rec(rid, "discovered")]
    ab = coordination_merge.merge_pipeline(_blob(local), _blob(remote))
    ba = coordination_merge.merge_pipeline(_blob(remote), _blob(local))
    assert ab == ba, "merge must stay commutative (guard-907)"
    out = [json.loads(ln) for ln in ab.decode().splitlines() if ln.strip()]
    assert len(out) == 1
    assert out[0]["stage"] == "archived", \
        "stage rank must converge the pre-flip copy forward to archived"
    assert out[0].get("archived_date") == "2026-07-11", \
        "tombstone's prune clock survives the merge (side-only field union)"


# ---------------------------------------------------------------------------
# meta dedup (parity: CLI + daemon)
# ---------------------------------------------------------------------------

METAS = [
    pytest.param(cli_pipeline.compute_meta, id="cli"),
    pytest.param(pipeline_write._compute_meta, id="daemon"),
]


@pytest.mark.parametrize("compute", METAS)
def test_compute_meta_dedups_cross_file_duplicate(compute):
    rid = "2026-07-01_meta-dedup"
    live_copy = _rec(rid, "archived", outcome="CONFIRMED",
                     archived_date="2026-07-11")
    archive_copy = _rec(rid, "archived", outcome="CONFIRMED")
    meta = compute([live_copy], [archive_copy])
    assert meta["accuracy"]["total_resolved"] == 1, \
        "a tombstoned id present in both files must count once"
    assert meta["stage_counts"]["archived"] == 1
    assert meta["stage_counts"]["discovered"] == 0


@pytest.mark.parametrize("compute", METAS)
def test_compute_meta_live_tombstone_not_counted_as_live_stage(compute):
    rid = "2026-07-01_meta-stage"
    live = [_rec(rid, "archived", archived_date="2026-07-11"),
            _rec("2026-07-01_meta-live", "discovered")]
    archive = [_rec(rid, "archived")]
    meta = compute(live, archive)
    assert meta["stage_counts"]["discovered"] == 1
    assert meta["stage_counts"]["archived"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
