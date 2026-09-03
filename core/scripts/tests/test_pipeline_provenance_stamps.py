"""Tests for pipeline provenance stamps — author/formed_at + resolved_by/resolved_at ().

WHY these exist: "who formed this hypothesis" and "who resolved it" were both
unanswerable after the fact. Measured by the g-306-106 audit — on the CONFIRMED
corpus, filed_by_agent 0/260, agent 0/260, author 1/260; the two derivation
routes (experience_ref -> agent dir, source_goal -> goal.filed_by_agent) cover
70/260 and 50/260 and CONFLICT 10 times against 12 agreements. On the resolved
side (474 records): reflected_by 73, resolved_by 10, resolver 2. So every
authorship-sensitive audit was blocked.

The load-bearing cases here are the WIRING tests, not the helper unit tests. A
stamp helper that is correct but not called from the write path is the exact
failure this goal exists to end — the audit found fields that *looked* like
provenance and carried nothing. So each stamp is asserted end-to-end through the
real endpoint, reading the record back off disk, plus a negative control proving
the stamp does NOT fire on unrelated transitions.

Historical records are deliberately NOT backfilled (~73% of authorship is
genuinely unrecoverable and would have to be guessed; a guessed provenance stamp
reads as authority to every later reader — guard-1925). The back-compat tests
pin that non-rewrite.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mind_api.src.world import pipeline_write  # noqa: E402


# ---------------------------------------------------------------------------
# Harness (mirrors test_pipeline_tombstone_archival.py)
# ---------------------------------------------------------------------------

class _Paths:
    def __init__(self, world: Path):
        self.world = world


class FakeCtx:
    def __init__(self, world: Path, query=None, body: bytes = b"", agent="echo"):
        self.paths = _Paths(world)
        self.query = query or {}
        self.body = body
        self.headers = {"x-mind-agent": agent} if agent else {}


def _rec(rec_id: str, stage: str, **over):
    base = {
        "id": rec_id,
        "title": f"hypothesis {rec_id}",
        "stage": stage,
        "horizon": "micro",
        "type": "exploration",
        "confidence": 0.5,
        "position": "YES this claim is decidable from the record",
        # >=20 chars: _validate_formation_quality requires a claim on every
        # non-discovered record, and these tests move records to active/resolved.
        "claim": "provenance stamps land on the written record",
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


def _seed_world(tmp_path: Path, live=(), archive=()):
    world = tmp_path / "world"
    world.mkdir(exist_ok=True)
    _write_jsonl(world / "pipeline.jsonl", list(live))
    _write_jsonl(world / "pipeline-archive.jsonl", list(archive))
    return world


def _live(world: Path, rec_id: str):
    for r in _read_jsonl(world / "pipeline.jsonl"):
        if r.get("id") == rec_id:
            return r
    return None


# ---------------------------------------------------------------------------
# Helper semantics
# ---------------------------------------------------------------------------

def test_formation_stamp_sets_author_and_timestamped_formed_at():
    rec = {}
    pipeline_write._stamp_formation_provenance(rec, "echo")
    assert rec["author"] == "echo"
    # A time component is the whole point — formed_date is date-only, and
    # intra-day ordering is exactly the interval the audit could not resolve
    # (canonical case: posted 06:56, filed 06:58).
    assert "T" in rec["formed_at"]
    assert len(rec["formed_at"]) == len("2026-07-31T21:35:12")


def test_formation_stamp_never_overwrites_explicit_values():
    """A migration or backfill supplying the TRUE author must win."""
    rec = {"author": "bravo", "formed_at": "2026-01-01T00:00:00"}
    pipeline_write._stamp_formation_provenance(rec, "echo")
    assert rec["author"] == "bravo"
    assert rec["formed_at"] == "2026-01-01T00:00:00"


def test_formation_stamp_is_idempotent():
    rec = {}
    pipeline_write._stamp_formation_provenance(rec, "echo")
    first = dict(rec)
    pipeline_write._stamp_formation_provenance(rec, "zeta")
    assert rec == first


def test_formation_stamp_with_no_agent_still_records_the_clock():
    """Defensive: never write an empty author, but formed_at is agent-independent."""
    rec = {}
    pipeline_write._stamp_formation_provenance(rec, "")
    assert "author" not in rec
    assert "T" in rec["formed_at"]


def test_resolution_stamp_sets_resolver_and_clock():
    rec = {}
    pipeline_write._stamp_resolution_provenance(rec, "echo")
    assert rec["resolved_by"] == "echo"
    assert "T" in rec["resolved_at"]


def test_resolution_stamp_never_overwrites():
    rec = {"resolved_by": "alpha", "resolved_at": "2026-01-01T00:00:00"}
    pipeline_write._stamp_resolution_provenance(rec, "echo")
    assert rec["resolved_by"] == "alpha"
    assert rec["resolved_at"] == "2026-01-01T00:00:00"


def test_resolved_at_does_not_collide_with_the_resolved_date_rename():
    """_normalize_record renames resolved_date -> outcome_date on an EXACT key
    match. `resolved_at` must survive that untouched, or the resolver clock
    would silently land in outcome_date."""
    rec = _rec("2026-07-01_rename-check", "resolved",
               resolved_at="2026-07-01T10:00:00", resolved_date="2026-07-01")
    out = pipeline_write._normalize_record(rec)
    assert out["resolved_at"] == "2026-07-01T10:00:00"
    assert out["outcome_date"] == "2026-07-01"
    assert "resolved_date" not in out


# ---------------------------------------------------------------------------
# Wiring — the load-bearing half
# ---------------------------------------------------------------------------

def test_add_stamps_formation_provenance_end_to_end(tmp_path):
    world = _seed_world(tmp_path)
    rid = "2026-07-31_add-formation"
    body = json.dumps(_rec(rid, "discovered")).encode()
    resp = pipeline_write.add(FakeCtx(world, body=body, agent="echo"))
    assert resp.status == 200, getattr(resp, "body", resp)

    stored = _live(world, rid)
    assert stored is not None
    assert stored["author"] == "echo"
    assert "T" in stored["formed_at"]


def test_add_preserves_formed_date_for_back_compat(tmp_path):
    """formed_at is ADDITIVE. formed_date stays byte-identical — the archive
    sweep's age math and resolves_by defaulting both parse it."""
    world = _seed_world(tmp_path)
    rid = "2026-07-31_formed-date-intact"
    body = json.dumps(_rec(rid, "discovered", formed_date="2026-07-01")).encode()
    resp = pipeline_write.add(FakeCtx(world, body=body))
    assert resp.status == 200

    stored = _live(world, rid)
    assert stored["formed_date"] == "2026-07-01"
    assert stored["formed_at"] != stored["formed_date"]


def test_add_without_agent_header_attributes_to_system(tmp_path):
    """Consistent with how history.snapshot/changelog.append in this module
    already attribute an unidentified write — not a silent null."""
    world = _seed_world(tmp_path)
    rid = "2026-07-31_no-header"
    body = json.dumps(_rec(rid, "discovered")).encode()
    resp = pipeline_write.add(FakeCtx(world, body=body, agent=None))
    assert resp.status == 200
    assert _live(world, rid)["author"] == "system"


def test_move_to_resolved_stamps_resolver_end_to_end(tmp_path):
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_move-res", "active")])
    resp = pipeline_write.move(FakeCtx(
        world,
        {"id": "2026-07-01_move-res", "stage": "resolved"},
        body=json.dumps({"outcome": "CONFIRMED",
                         "experience_ref": "exp-provenance-test"}).encode(),
        agent="echo",
    ))
    assert resp.status == 200, getattr(resp, "body", resp)

    stored = _live(world, "2026-07-01_move-res")
    assert stored["resolved_by"] == "echo"
    assert "T" in stored["resolved_at"]


def test_move_to_non_resolved_stage_does_not_stamp_resolver(tmp_path):
    """NEGATIVE CONTROL. Without this, a _stamp_resolution_provenance call that
    fired on every move would pass every other test in this file while making
    resolved_by meaningless — it would name whoever last touched the record."""
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_to-active", "discovered")])
    resp = pipeline_write.move(
        FakeCtx(world, {"id": "2026-07-01_to-active", "stage": "active"}))
    assert resp.status == 200, getattr(resp, "body", resp)

    stored = _live(world, "2026-07-01_to-active")
    assert "resolved_by" not in stored
    assert "resolved_at" not in stored


def test_add_directly_at_resolved_carries_both_stamps(tmp_path):
    """A record added straight at stage=resolved is BOTH a formation and a
    resolution event — the module already treats it that way (it runs the
    resolution-evidence gate on this path)."""
    world = _seed_world(tmp_path)
    rid = "2026-07-31_born-resolved"
    body = json.dumps(_rec(rid, "resolved", outcome="CONFIRMED",
                           experience_ref="exp-born-resolved")).encode()
    resp = pipeline_write.add(FakeCtx(world, body=body, agent="echo"))
    assert resp.status == 200, getattr(resp, "body", resp)

    stored = _live(world, rid)
    assert stored["author"] == "echo"
    assert stored["resolved_by"] == "echo"
    assert "T" in stored["formed_at"]
    assert "T" in stored["resolved_at"]


def test_historical_record_is_not_backfilled_on_unrelated_move(tmp_path):
    """Back-compat pin: moving a pre-change record through a NON-resolving
    transition must not invent a formation stamp for it. Authorship for ~73% of
    the historical corpus is unrecoverable; a guessed stamp reads as authority
    (guard-1925)."""
    world = _seed_world(tmp_path, live=[_rec("2026-05-01_historical", "discovered")])
    resp = pipeline_write.move(
        FakeCtx(world, {"id": "2026-05-01_historical", "stage": "active"}))
    assert resp.status == 200

    stored = _live(world, "2026-05-01_historical")
    assert "author" not in stored
    assert "formed_at" not in stored


def test_resolver_stamp_does_not_clobber_a_prior_resolver_on_re_move(tmp_path):
    """A record re-moved into resolved keeps the FIRST resolver — the decision
    is owned by whoever made it, not whoever last touched the record.

    CONTRACT STRENGTHENED 2026-09-03 (g-306-421). The second move used to
    return 200 and this test's whole protection was the write-once stamp: the
    re-resolve LANDED and only `resolved_by` survived, while `outcome`,
    `outcome_date` and every merged resolution field were replaced by the
    second writer. The move handler now REFUSES a resolved -> resolved
    transition outright, so the second call is a 400 and the entire first
    verdict survives, not just its author.

    The stamp itself is deliberately NOT removed and this test still asserts
    it. `update_field` has no field whitelist, so a caller can still set
    `stage` directly and reach a re-resolve around the move guard — the stamp
    is what holds on that path. Deleting a defence because a newer one covers
    the case you happened to test is how the remaining case gets found the
    hard way.
    """
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_re-move", "active")])
    q = {"id": "2026-07-01_re-move", "stage": "resolved"}
    body = json.dumps({"outcome": "CONFIRMED",
                       "experience_ref": "exp-re-move"}).encode()
    assert pipeline_write.move(
        FakeCtx(world, q, body=body, agent="echo")).status == 200
    second = pipeline_write.move(FakeCtx(world, q, body=body, agent="zeta"))
    assert second.status == 400, getattr(second, "body", second)
    assert "invalid_stage_transition" in second.body.decode()

    assert _live(world, "2026-07-01_re-move")["resolved_by"] == "echo"
