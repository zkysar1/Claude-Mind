"""Terminal-verdict guard on pipeline move ().

THE DEFECT. `pipeline_write.move` did `rec["stage"] = target_stage`
unconditionally. VALID_STAGES enumerates legal target NAMES and
_validate_record / _validate_formation_quality check the record's SHAPE — none
of them looked at the stage the record was ALREADY in. So moving an
already-resolved record to `resolved` a second time silently replaced its
outcome, outcome_date and merged resolution fields, and the surviving verdict
was whichever write ran last.

WHY IT MATTERED NOW rather than whenever it was written: today exactly one Body
resolves (the reducer, once, at its own pace), so nothing races. g-306-417 and
g-306-418's outcome 2 introduce a SECOND resolver — the worker that executed the
unit — while the hyp_capture lane keeps handing the reducer evidence for the
same hypothesis_id to resolve independently. That is a writer pair with no
arbitration, and the loser's verdict vanishes without a trace in the store: a
re-resolved record looks identical to a resolved one. This guard is the
precondition both siblings need, not the feature either of them asks for.

THE ASYMMETRY THESE TESTS EXIST TO PIN. The guard refuses re-RESOLUTION and
allows re-ARCHIVAL, and that split is the whole design rather than an oversight.
guard-1080 requires enumerating the callers that legitimately perform the
behaviour being forbidden; that enumeration found
test_move_to_archived_idempotent_no_double_append, which asserts a SECOND move
to archived returns 200. Tombstone-in-live archival (g-115-1986) depends on it —
a pre-removal peer copy can resurrect a record at its old stage and the re-move
is how the fleet re-converges. Re-resolving has no such caller and no such
purpose. The over-broad version of this guard (refuse any terminal move with no
forward progress) passes every test named in the goal and BREAKS that one, which
is why the legitimate-write assertions below are not decoration: they are the
half that stops the fix from becoming the next g-115-4821, where a whole-record
check made 70 records immutable to every later mutation "including archive_sweep,
which is the mechanism that would have retired it".

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_pipeline_terminal_transition_guard.py -q
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


# --------------------------------------------------------------------------
# Harness (same shape as test_pipeline_tombstone_archival.py — deliberately
# duplicated rather than imported: that file pins ARCHIVAL behaviour against
# this record shape and a shared fixture would let one file's edit silently
# change the other's subject.)
# --------------------------------------------------------------------------

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
        "position": "YES the second resolve must not overwrite the first",
        "formed_date": "2026-07-01",
        "category": "framework-meta",
        "slug": rec_id.split("_", 1)[1],
        "rationale": "seeded by test",
        # >=20 chars: _validate_formation_quality requires a claim on every
        # non-discovered hypothesis, and it fires on moves INTO active and
        # resolved. The tombstone harness omits it because it only ever moves
        # TO archived, which is not gated.
        "claim": "the second resolve does not overwrite the first verdict",
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


def _seed(tmp_path: Path, live, archive=()):
    world = tmp_path / "world"
    world.mkdir(exist_ok=True)
    _write_jsonl(world / "pipeline.jsonl", live)
    _write_jsonl(world / "pipeline-archive.jsonl", list(archive))
    return world


def _live(world: Path, rid: str):
    for r in _read_jsonl(world / "pipeline.jsonl"):
        if r["id"] == rid:
            return r
    raise AssertionError(f"{rid} vanished from live")


def _move(world: Path, rid: str, stage: str, body: dict | None = None):
    return pipeline_write.move(FakeCtx(
        world, {"id": rid, "stage": stage},
        body=json.dumps(body).encode() if body else b""))


def _update_field(world: Path, rid: str, field: str, value: str):
    """The SECOND write path (). Deliberately exercised through the
    endpoint rather than the predicate: the predicate has its own matrix test
    below, and what this successor had to prove is that update_field actually
    CONSULTS it — a guard function nothing calls is the shape g-306-421's own
    docstring warned was still open here."""
    return pipeline_write.update_field(FakeCtx(
        world, {"id": rid, "field": field, "value": value}))


# --------------------------------------------------------------------------
# The defect: resolve twice
# --------------------------------------------------------------------------

def test_second_resolve_is_REFUSED_and_the_first_verdict_survives_byte_identical(tmp_path):
    """The load-bearing case, and it asserts SURVIVAL, not just refusal.

    A test that only checks the second call errors would pass against a guard
    that refuses AFTER mutating the record in memory, or one that refuses and
    leaves a half-merged row on disk. The point of the guard is that the first
    verdict is still there afterwards, so that is what is compared — the whole
    record, byte for byte, not merely `outcome`.
    """
    rid = "2026-07-01_resolve-twice"
    world = _seed(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED",
                                  outcome_date="2026-07-02",
                                  outcome_detail="confirmed by g-306-421")])
    before = json.dumps(_live(world, rid), sort_keys=True)

    resp = _move(world, rid, "resolved",
                 {"outcome": "REFUTED", "outcome_detail": "second opinion"})

    assert resp.status == 400, getattr(resp, "body", resp)
    assert "invalid_stage_transition" in resp.body.decode()
    after = json.dumps(_live(world, rid), sort_keys=True)
    assert after == before, "the FIRST verdict must survive byte-identical"


def test_the_refusal_names_the_legal_route_rather_than_only_saying_no(tmp_path):
    """The aspirations `invalid_status_transition` refusal is the model this
    goal named, and what makes it usable is that it enumerates the routes. A
    bare 'not allowed' sends the next Body looking for an override flag."""
    rid = "2026-07-01_resolve-route"
    world = _seed(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    body = _move(world, rid, "resolved").body.decode()
    assert "pipeline-update-field.sh" in body, "must name the correction route"
    # THE SHAPE, not just the script name (). This assertion read
    # `"--field outcome" in body` until 2026-09-03, pinning a command the
    # wrapper REFUSES: pipeline-update-field.sh parses three positionals and its
    # `-*)` arm calls argv_strict_refuse_unknown, so the route this guard names
    # as its only escape exited 2 with "unknown option '--field'". Measured
    # live, both directions: the flag form rc=2, `<id> outcome CONFIRMED`
    # reached the daemon. A guard with no override handing out a dead command
    # is worse than one that says nothing.
    assert "pipeline-update-field.sh <id> outcome <v>" in body, body
    assert "--field" not in body, "must not resurrect the refused flag form"
    assert "archiv" in body.lower(), "must say archival is still allowed"


def test_a_merge_body_carrying_stage_cannot_talk_past_the_guard(tmp_path):
    """The decision reads the stage ON DISK, before the merge loop. If it read
    the post-merge record instead, a body of {"stage": "active"} would launder
    a resolved record into a re-resolvable one."""
    rid = "2026-07-01_resolve-launder"
    world = _seed(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    resp = _move(world, rid, "resolved", {"stage": "active"})
    assert resp.status == 400, getattr(resp, "body", resp)
    # The ERROR CODE, not merely the status. Measured by positive control: with
    # the guard disabled this move still returns 400, from the
    # resolution-evidence gate further down the handler — so a bare status
    # assertion passes with the fix REVERTED and pins nothing.
    assert "invalid_stage_transition" in resp.body.decode(), resp.body
    assert _live(world, rid)["stage"] == "resolved"
    assert _live(world, rid)["outcome"] == "CONFIRMED"


def test_moving_BACK_from_archived_to_resolved_is_refused(tmp_path):
    """Backward out of a terminal stage. The merge layer has always assumed the
    lifecycle is forward-only (`_PIPELINE_STAGE_RANK`: 'a resolution/archival
    must never be reverted by a peer's concurrent metadata bump'); the write
    layer did not enforce it, so the two disagreed."""
    rid = "2026-07-01_backward"
    world = _seed(tmp_path, [_rec(rid, "archived", outcome="CONFIRMED",
                                  archived_date="2026-07-03")])
    resp = _move(world, rid, "resolved")
    assert resp.status == 400, getattr(resp, "body", resp)
    # Same positive-control finding as above: assert WHICH refusal fired.
    assert "invalid_stage_transition" in resp.body.decode(), resp.body
    assert _live(world, rid)["stage"] == "archived"


# --------------------------------------------------------------------------
# guard-1080: the legitimate writes must still succeed
# --------------------------------------------------------------------------

def test_resolved_to_archived_STILL_SUCCEEDS(tmp_path):
    """The one that stops this fix from stranding the corpus.

    Archival is the ordinary forward lifecycle and archive_sweep is built on
    it; a guard keyed on 'already carries an outcome' would refuse it and every
    resolved record would become permanently unarchivable — byte-for-byte the
    g-115-4821 failure this file has already paid for once.
    """
    rid = "2026-07-01_forward-archive"
    world = _seed(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    resp = _move(world, rid, "archived")
    assert resp.status == 200, getattr(resp, "body", resp)
    assert _live(world, rid)["stage"] == "archived"


def test_archived_to_archived_STILL_SUCCEEDS_idempotently(tmp_path):
    """The enumerated legitimate caller (guard-1080).

    Pinned independently here as well as in test_pipeline_tombstone_archival,
    because THIS file is where someone will come to 'simplify' the two
    same-stage cases into one rank comparison. That simplification is green
    against every other test in this file and breaks tombstone convergence.
    """
    rid = "2026-07-01_idempotent-archive"
    world = _seed(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    assert _move(world, rid, "archived").status == 200
    resp = _move(world, rid, "archived")
    assert resp.status == 200, getattr(resp, "body", resp)
    arch = _read_jsonl(world / "pipeline-archive.jsonl")
    assert len(arch) == 1, "re-moving a tombstone must not duplicate the archive copy"


def test_a_FIRST_resolve_from_a_live_stage_still_succeeds(tmp_path):
    """The guard keys on the CURRENT stage, so nothing about an ordinary
    resolution changes. Without this the suite could not tell 'refuses the
    second resolve' from 'refuses resolution'."""
    rid = "2026-07-01_first-resolve"
    world = _seed(tmp_path, [_rec(rid, "measurement-pending")])
    resp = _move(world, rid, "resolved",
                 {"outcome": "CONFIRMED",
                  "outcome_detail": "resolved by g-306-421 test"})
    assert resp.status == 200, getattr(resp, "body", resp)
    rec = _live(world, rid)
    assert rec["stage"] == "resolved" and rec["outcome"] == "CONFIRMED"


def test_same_stage_moves_on_NON_terminal_records_are_untouched(tmp_path):
    """move-to-same-stage is a metadata-merge idiom on live records. Nothing
    terminal is at stake there, so the guard must not reach it."""
    rid = "2026-07-01_active-merge"
    world = _seed(tmp_path, [_rec(rid, "active")])
    resp = _move(world, rid, "active", {"rationale": "updated in place"})
    assert resp.status == 200, getattr(resp, "body", resp)
    assert _live(world, rid)["rationale"] == "updated in place"


# --------------------------------------------------------------------------
# The predicate itself
# --------------------------------------------------------------------------

def test_predicate_matrix():
    """Pins the decision table directly, so a future reader can see the whole
    contract without reconstructing it from eight endpoint tests."""
    f = pipeline_write._terminal_transition_refusal
    assert f("resolved", "resolved") is not None
    assert f("archived", "resolved") is not None
    assert f("archived", "archived") is None
    assert f("resolved", "archived") is None
    assert f("active", "active") is None
    assert f("active", "resolved") is None
    assert f("discovered", "archived") is None


def test_the_write_layer_rank_matches_the_merge_layer_rank():
    """Two copies exist because core/scripts (Layer 1) cannot be imported from
    mind_api/src (Layer 2). A silent divergence would put the write guard and
    the merge's conflict resolution on different orderings — this is the thing
    that makes that loud."""
    import coordination_merge
    assert pipeline_write._STAGE_RANK == coordination_merge._PIPELINE_STAGE_RANK


# --------------------------------------------------------------------------
# The update_field launder path ()
#
#  guarded `move` and said so in its own docstring: update_field has no
# field whitelist, so a bare `stage` write could walk a resolved record back to
# a live stage and a re-resolve then entered through the front door with the
# move guard never firing. These pin the successor.
#
# THE guard-1080 ENUMERATION behind the shape of this fix, recorded here because
# it is what stops the next reader "simplifying" it into a field refusal: ZERO
# scripted writers of `stage` through update-field exist in the tree (.sh, .py,
# SKILL.md, .yaml) and ZERO tests wrote one. That empty set makes refusing the
# field outright *available* — and it is still the wrong fix, because ad-hoc
# agent use is real and legitimate: bravo resolved a record in place with
# `pipeline-update-field <id> stage resolved` while pipeline-move was blocked by
# the rb-3080 write-block (journal 2026-07-12; exp--resolution-survives-
# writeblock records it landing cleanly). Refusing the field would have deleted
# that path. The forward test below is that caller.
# --------------------------------------------------------------------------

def test_update_field_cannot_launder_resolved_BACK_to_active(tmp_path):
    """The defect this successor exists to close, asserted by ERROR CODE and by
    SURVIVAL. Status alone would be weak here for the same reason the move
    tests give: 400 is reachable from _validate_record too, so a bare status
    assertion can pass with the guard reverted."""
    rid = "2026-07-01_uf-launder"
    world = _seed(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED",
                                  outcome_date="2026-07-02",
                                  outcome_detail="confirmed by g-306-421")])
    before = json.dumps(_live(world, rid), sort_keys=True)

    resp = _update_field(world, rid, "stage", "active")

    assert resp.status == 400, getattr(resp, "body", resp)
    assert "invalid_stage_transition" in resp.body.decode(), resp.body
    after = json.dumps(_live(world, rid), sort_keys=True)
    assert after == before, "the verdict must survive the refused stage write"


def test_update_field_backward_from_archived_is_refused(tmp_path):
    rid = "2026-07-01_uf-backward"
    world = _seed(tmp_path, [_rec(rid, "archived", outcome="CONFIRMED",
                                  archived_date="2026-07-03")])
    resp = _update_field(world, rid, "stage", "resolved")
    assert resp.status == 400, getattr(resp, "body", resp)
    assert "invalid_stage_transition" in resp.body.decode(), resp.body
    assert _live(world, rid)["stage"] == "archived"


def test_update_field_re_resolving_a_resolved_record_is_refused(tmp_path):
    """Same asymmetry as the move path: re-RESOLVE is refused, re-ARCHIVE is
    not. Pinned separately so the two same-stage cases cannot be collapsed into
    one rank comparison here either."""
    rid = "2026-07-01_uf-re-resolve"
    world = _seed(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    resp = _update_field(world, rid, "stage", "resolved")
    assert resp.status == 400, getattr(resp, "body", resp)
    assert "invalid_stage_transition" in resp.body.decode(), resp.body


# --------------------------------------------------------------------------
# guard-1080 again: the legitimate update_field writes must still succeed.
# These are the half that stops the over-fix, exactly as above.
# --------------------------------------------------------------------------

def test_update_field_FORWARD_stage_write_STILL_SUCCEEDS(tmp_path):
    """THE enumerated legitimate caller: the in-place resolve bravo used when
    pipeline-move was blocked. A field-level refusal passes every other test in
    this section and deletes this path."""
    rid = "2026-07-01_uf-forward"
    world = _seed(tmp_path, [_rec(rid, "active")])
    resp = _update_field(world, rid, "stage", "resolved")
    assert resp.status == 200, getattr(resp, "body", resp)
    assert _live(world, rid)["stage"] == "resolved"


def test_update_field_archived_to_archived_STILL_SUCCEEDS(tmp_path):
    """Tombstone convergence () reaches this path too."""
    rid = "2026-07-01_uf-idempotent-archive"
    world = _seed(tmp_path, [_rec(rid, "archived", outcome="CONFIRMED",
                                  archived_date="2026-07-03")])
    resp = _update_field(world, rid, "stage", "archived")
    assert resp.status == 200, getattr(resp, "body", resp)
    assert _live(world, rid)["stage"] == "archived"


def test_update_field_NON_stage_writes_on_a_resolved_record_are_untouched(tmp_path):
    """The correction route both refusal messages name. If the guard reached
    any field on a terminal record, the guard would strand the very escape it
    advertises — the g-115-4821 shape, one path over."""
    rid = "2026-07-01_uf-outcome-route"
    world = _seed(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    resp = _update_field(world, rid, "outcome", "CORRECTED")
    assert resp.status == 200, getattr(resp, "body", resp)
    rec = _live(world, rid)
    assert rec["outcome"] == "CORRECTED" and rec["stage"] == "resolved"


def test_update_field_stage_writes_on_NON_terminal_records_are_untouched(tmp_path):
    rid = "2026-07-01_uf-live-stage"
    world = _seed(tmp_path, [_rec(rid, "discovered")])
    resp = _update_field(world, rid, "stage", "active")
    assert resp.status == 200, getattr(resp, "body", resp)
    assert _live(world, rid)["stage"] == "active"


def test_update_field_refusal_names_the_correction_route_in_a_shape_that_RUNS(tmp_path):
    """Symmetric with the move-path route test, and pinning the same lesson:
    the message must not name the `--field/--value` form, which the wrapper
    refuses with exit 2."""
    rid = "2026-07-01_uf-route"
    world = _seed(tmp_path, [_rec(rid, "resolved", outcome="CONFIRMED")])
    body = _update_field(world, rid, "stage", "active").body.decode()
    assert "pipeline-update-field.sh <id> outcome <v>" in body, body
    assert "--field" not in body, "must not resurrect the refused flag form"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
