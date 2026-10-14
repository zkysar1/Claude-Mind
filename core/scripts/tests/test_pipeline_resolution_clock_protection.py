"""Tests for the writer-owned resolution clock ().

WHY these exist: pipeline.md says `resolved_at` / `resolved_by` are
"Writer-stamped (do not supply)", but the pre-fix code stored a caller value
in all three write paths:

  1. move INTO resolved: the caller merge loop (rec[key] = val for every key)
     runs BEFORE `_stamp_resolution_provenance`, which only fills ABSENT
     values — so a caller-supplied resolved_at/resolved_by won. First-person
     instance: g-001-02 occ45 stored a hand-built 05:55:00 against a write at
     ~05:38, then corrected it via update-field.
  2. add directly at stage=resolved: `_stamp_resolution_provenance` is called
     on the caller's dict and fills only absent fields — same posture, same
     hole.
  3. update-field: no field protection at all — `rec[field] = value` for
     ANY field, so the clock could be rewritten afterwards. `PIPELINE_KNOWN_FIELDS`
     is a WARN allowlist, not write-once.

The fix REFUSES (not silently strips): a silently stripped field reads as
accepted, and the file's established posture for a forged transition is a 400
with a route to the correct field (`_terminal_transition_refusal` is the model
— outcome, not stage, is the correction path here). update-field keeps an
explicit override reason because the first-person instance (a corrected clock)
is a legitimate, audited repair; the reason lands in outcome_detail.

CONSUMER CONTRACT (zeta 2026-10-12 consumer notice, g-115-8295):
close_phase_skip._resolve_pass_close reads {resolved_at, resolved_by} off the
live pipeline and TRUSTS both. These tests pin that the fix does not rename,
retype or relocate either field, and that the writer's clock is still stamped
on the resolve path — the reader only ever sees tighter, never missing, values.
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
# Harness (mirrors test_pipeline_provenance_stamps.py)
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
        "claim": "the resolution clock belongs to the writer",
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


def _archive(world: Path, rec_id: str):
    for r in _read_jsonl(world / "pipeline-archive.jsonl"):
        if r.get("id") == rec_id:
            return r
    return None


def _resolve_merge():
    """A normal resolve merge that must keep working (negative control)."""
    return {"outcome": "CONFIRMED", "experience_ref": "exp-clock-protection"}


FUTURE = "2099-01-01T00:00:00"


# ---------------------------------------------------------------------------
# 1. move INTO resolved — the merge may not carry the writer's clock
# ---------------------------------------------------------------------------

def test_move_forge_resolved_at_is_refused_and_writes_nothing(tmp_path):
    """A merge carrying a caller resolved_at must NOT store it. Pre-fix this
    returned 200 and stored the caller's value (the stamp only fills absent
    fields and runs after the merge loop)."""
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_forge-at", "active")])
    merge = _resolve_merge()
    merge["resolved_at"] = FUTURE
    resp = pipeline_write.move(FakeCtx(
        world,
        {"id": "2026-07-01_forge-at", "stage": "resolved"},
        body=json.dumps(merge).encode(),
        agent="echo",
    ))
    assert resp.status == 400, (
        f"expected a 400 refusal for a caller-supplied resolved_at, got "
        f"{resp.status}: {getattr(resp, 'body', resp)[:300]}")
    stored = _live(world, "2026-07-01_forge-at")
    assert stored["stage"] == "active"
    assert "resolved_at" not in stored
    assert "resolved_by" not in stored


def test_move_forge_resolved_by_is_refused(tmp_path):
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_forge-by", "active")])
    merge = _resolve_merge()
    merge["resolved_by"] = "not-the-writer"
    resp = pipeline_write.move(FakeCtx(
        world,
        {"id": "2026-07-01_forge-by", "stage": "resolved"},
        body=json.dumps(merge).encode(),
        agent="echo",
    ))
    assert resp.status == 400, (
        f"expected a 400 refusal for a caller-supplied resolved_by, got "
        f"{resp.status}: {getattr(resp, 'body', resp)[:300]}")
    stored = _live(world, "2026-07-01_forge-by")
    assert stored["stage"] == "active"
    assert "resolved_by" not in stored


def test_move_legit_resolve_still_lands_with_writer_clock(tmp_path):
    """Negative control: a normal resolve merge (no forged fields) still
    succeeds and the WRITER's clock is stamped — the consumer contract
    (close_phase_skip) requires resolved_at present after a resolve."""
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_legit", "active")])
    resp = pipeline_write.move(FakeCtx(
        world,
        {"id": "2026-07-01_legit", "stage": "resolved"},
        body=json.dumps(_resolve_merge()).encode(),
        agent="echo",
    ))
    assert resp.status == 200, getattr(resp, "body", resp)
    stored = _live(world, "2026-07-01_legit")
    assert stored["stage"] == "resolved"
    assert stored["resolved_by"] == "echo"
    assert "T" in stored["resolved_at"]
    assert stored["resolved_at"] != FUTURE


# ---------------------------------------------------------------------------
# 2. add directly at stage=resolved — same posture, same chokepoint
# ---------------------------------------------------------------------------

def test_add_at_resolved_forge_resolved_at_is_refused(tmp_path):
    world = _seed_world(tmp_path)
    rid = "2026-07-31_add-forge"
    rec = _rec(rid, "resolved", outcome="CONFIRMED",
               experience_ref="exp-add-forge", resolved_at=FUTURE)
    resp = pipeline_write.add(FakeCtx(world, body=json.dumps(rec).encode(),
                                      agent="echo"))
    assert resp.status == 400, (
        f"expected a 400 refusal for a caller-supplied resolved_at on add, "
        f"got {resp.status}: {getattr(resp, 'body', resp)[:300]}")
    assert _live(world, rid) is None


def test_add_at_resolved_forge_resolved_by_is_refused(tmp_path):
    world = _seed_world(tmp_path)
    rid = "2026-07-31_add-forge-by"
    rec = _rec(rid, "resolved", outcome="CONFIRMED",
               experience_ref="exp-add-forge-by", resolved_by="ghost")
    resp = pipeline_write.add(FakeCtx(world, body=json.dumps(rec).encode(),
                                      agent="echo"))
    assert resp.status == 400
    assert _live(world, rid) is None


def test_add_at_resolved_clean_lands_with_writer_clock(tmp_path):
    world = _seed_world(tmp_path)
    rid = "2026-07-31_add-clean"
    rec = _rec(rid, "resolved", outcome="CONFIRMED",
               experience_ref="exp-add-clean")
    resp = pipeline_write.add(FakeCtx(world, body=json.dumps(rec).encode(),
                                      agent="echo"))
    assert resp.status == 200, getattr(resp, "body", resp)
    stored = _live(world, rid)
    assert stored["stage"] == "resolved"
    assert stored["resolved_by"] == "echo"
    assert "T" in stored["resolved_at"]


# ---------------------------------------------------------------------------
# 3. update-field — refuse without an override reason; allow with one
# ---------------------------------------------------------------------------

def test_update_field_resolved_at_without_override_is_refused(tmp_path):
    """Pre-fix: rec[field] = value for ANY field — the clock was rewritable
    afterwards (this is the path that 'corrected' the occ45 value, which is
    the legitimate use an override reason now exists for)."""
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_uf-forge", "active")])
    resp = pipeline_write.update_field(FakeCtx(
        world,
        {"id": "2026-07-01_uf-forge", "field": "resolved_at",
         "value": FUTURE},
        agent="echo",
    ))
    assert resp.status == 400, (
        f"expected a 400 refusal without an override reason, got "
        f"{resp.status}: {getattr(resp, 'body', resp)[:300]}")
    stored = _live(world, "2026-07-01_uf-forge")
    assert "resolved_at" not in stored


def test_update_field_resolved_by_without_override_is_refused(tmp_path):
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_uf-by", "active")])
    resp = pipeline_write.update_field(FakeCtx(
        world,
        {"id": "2026-07-01_uf-by", "field": "resolved_by", "value": "ghost"},
        agent="echo",
    ))
    assert resp.status == 400
    stored = _live(world, "2026-07-01_uf-by")
    assert "resolved_by" not in stored


def _q_with_override(**extra):
    q = {"id": "2026-07-01_uf-ovr",
         "override_reason": "occ45-style repair: the hand-built merge forged the clock"}
    q.update(extra)
    return q


def test_update_field_resolved_at_with_override_lands_and_is_audited(tmp_path):
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_uf-ovr", "active")])
    resp = pipeline_write.update_field(FakeCtx(
        world, _q_with_override(field="resolved_at", value=FUTURE),
        agent="echo",
    ))
    assert resp.status == 200, getattr(resp, "body", resp)
    stored = _live(world, "2026-07-01_uf-ovr")
    assert stored["resolved_at"] == FUTURE
    # The override is AUDITABLE, not silent: the reason is recorded on the
    # record itself (outcome_detail append), so a later reader can see this
    # value was not writer-stamped.
    od = stored.get("outcome_detail") or ""
    assert "override_reason" in od or "g-115-10788" in od, (
        f"override must be recorded on the record; outcome_detail={od[:200]!r}")


def test_update_field_resolved_by_with_override_lands(tmp_path):
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_uf-by-ovr", "active")])
    q = _q_with_override(id="2026-07-01_uf-by-ovr",
                         field="resolved_by", value="bravo")
    resp = pipeline_write.update_field(FakeCtx(world, q, agent="echo"))
    assert resp.status == 200, getattr(resp, "body", resp)
    assert _live(world, "2026-07-01_uf-by-ovr")["resolved_by"] == "bravo"


def test_update_field_other_fields_unaffected_by_the_guard(tmp_path):
    """Negative control: the guard is narrow to the two clock fields — every
    other field (including the adjacent caller-owned outcome_date) still
    updates. outcome_date is the CALLER's date-only field; it must stay
    writable (pipeline.md's window math depends on callers supplying it)."""
    world = _seed_world(tmp_path, live=[_rec("2026-07-01_uf-other", "active")])
    for field, value in (("outcome_date", "2026-07-02"),
                         ("outcome", "CONFIRMED"),
                         ("surprise", "3")):
        resp = pipeline_write.update_field(FakeCtx(
            world,
            {"id": "2026-07-01_uf-other", "field": field, "value": value},
            agent="echo",
        ))
        assert resp.status == 200, (
            f"field {field} must stay writable: {resp.status} "
            f"{getattr(resp, 'body', resp)[:200]}")
    stored = _live(world, "2026-07-01_uf-other")
    assert stored["outcome_date"] == "2026-07-02"


def test_update_field_resolved_at_override_reaches_archived_record(tmp_path):
    """update_field probes live then archive; the guard must hold on BOTH
    files (the stage-transition guard already does)."""
    world = _seed_world(tmp_path, archive=[_rec("2026-01-01_arch-forged", "archived")])
    resp = pipeline_write.update_field(FakeCtx(
        world,
        {"id": "2026-01-01_arch-forged", "field": "resolved_at",
         "value": FUTURE},
        agent="echo",
    ))
    assert resp.status == 400
    assert _archive(world, "2026-01-01_arch-forged").get("resolved_at") != FUTURE


# ---------------------------------------------------------------------------
# 4. update (whole-record) — rb-2239 round-trip tolerated, rewrite refused
# ---------------------------------------------------------------------------
#
# update() resends the WHOLE record, so unlike move/add a caller value for the
# clock can be a legitimate ROUND-TRIP: the rb-2239 repair of a multi-field-
# corrupt record fetches the stored record (writer stamp included) and resends
# it, and the guard must let an UNCHANGED clock through while refusing any
# deviation — including a DROP (a replacement that omits the stored stamp
# erases the writer's clock silently). The guard therefore runs IN-LOCK,
# where the stored record is visible, not at the entry.


def _stamped_resolved(world: Path, rid: str):
    """Seed a writer-stamped resolved record through the LEGIT add path so
    the stored clock is exactly what the writer stamped (the round-trip
    reference), then return the stored record."""
    rec = _rec(rid, "resolved", outcome="CONFIRMED",
               experience_ref=f"exp-{rid}")
    resp = pipeline_write.add(FakeCtx(world, body=json.dumps(rec).encode(),
                                      agent="echo"))
    assert resp.status == 200, getattr(resp, "body", resp)
    stored = _live(world, rid)
    assert stored["resolved_by"] == "echo"
    assert stored.get("resolved_at")
    return stored


def test_update_unchanged_roundtrip_with_stamp_succeeds(tmp_path):
    """rb-2239's repair resends the whole stored record, writer stamp
    included — an UNCHANGED clock must round-trip (pre-refinement this
    guard was caller-key based and would have refused the legit repair)."""
    world = _seed_world(tmp_path)
    rid = "2026-07-15_rt-ok"
    stored = _stamped_resolved(world, rid)
    replacement = dict(stored)  # byte-identical resupply
    resp = pipeline_write.update(FakeCtx(
        world, {"id": rid}, body=json.dumps(replacement).encode(),
        agent="echo",
    ))
    assert resp.status == 200, getattr(resp, "body", resp)
    after = _live(world, rid)
    assert after["resolved_at"] == stored["resolved_at"]
    assert after["resolved_by"] == "echo"
    # The round-trip is NOT an override: nothing gets audited onto it.
    assert "override_reason" not in (after.get("outcome_detail") or "")


def test_update_changed_clock_is_refused_and_writes_nothing(tmp_path):
    world = _seed_world(tmp_path)
    rid = "2026-07-15_rt-forged"
    stored = _stamped_resolved(world, rid)
    replacement = dict(stored)
    replacement["resolved_at"] = FUTURE  # forged rewrite
    resp = pipeline_write.update(FakeCtx(
        world, {"id": rid}, body=json.dumps(replacement).encode(),
        agent="echo",
    ))
    assert resp.status == 400, (
        f"expected a 400 refusal for a changed resolved_at, got "
        f"{resp.status}: {getattr(resp, 'body', resp)[:300]}")
    assert _live(world, rid)["resolved_at"] == stored["resolved_at"]


def test_update_dropped_clock_is_refused(tmp_path):
    """A replacement that OMITS a stored clock erases the writer's stamp
    silently — same class as a deviation, refused. (An un-stamped record
    resupplied without the fields is a round-trip and stays 200 — the
    runtime endpoint tests cover that population.)"""
    world = _seed_world(tmp_path)
    rid = "2026-07-15_rt-drop"
    stored = _stamped_resolved(world, rid)
    replacement = dict(stored)
    del replacement["resolved_at"]
    del replacement["resolved_by"]
    resp = pipeline_write.update(FakeCtx(
        world, {"id": rid}, body=json.dumps(replacement).encode(),
        agent="echo",
    ))
    assert resp.status == 400, (
        f"expected a 400 refusal for a dropped clock, got "
        f"{resp.status}: {getattr(resp, 'body', resp)[:300]}")
    after = _live(world, rid)
    assert after["resolved_at"] == stored["resolved_at"]
    assert after["resolved_by"] == "echo"


def test_update_unchanged_roundtrip_reaches_archive(tmp_path):
    """The archive half of rb-2239: an archived record WITH a stored clock
    must be repairable by an identical whole-record resupply, and a deviating
    resupply must be refused — on the ARCHIVE file, where update_field's
    stage guard cannot reach a corrupt record anyway."""
    world = _seed_world(tmp_path)
    rid = "2026-01-01_arch-rt"
    stamp_at = "2026-01-02T10:00:00"
    stamp_by = "echo"
    seed = _rec(rid, "archived", outcome="CONFIRMED",
                experience_ref="exp-arch-rt",
                resolved_at=stamp_at, resolved_by=stamp_by)
    _write_jsonl(world / "pipeline-archive.jsonl", [seed])

    replacement = dict(seed)
    resp = pipeline_write.update(FakeCtx(
        world, {"id": rid}, body=json.dumps(replacement).encode(),
        agent="echo",
    ))
    assert resp.status == 200, getattr(resp, "body", resp)
    assert _archive(world, rid)["resolved_at"] == stamp_at

    forged = dict(seed)
    forged["resolved_at"] = FUTURE
    resp = pipeline_write.update(FakeCtx(
        world, {"id": rid}, body=json.dumps(forged).encode(),
        agent="echo",
    ))
    assert resp.status == 400
    assert _archive(world, rid)["resolved_at"] == stamp_at
