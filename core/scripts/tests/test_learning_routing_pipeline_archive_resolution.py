"""learning-routing-audit must resolve pipeline hypothesis refs against
pipeline.jsonl AND pipeline-archive.jsonl (g-115-11522).

WHY THIS EXISTS. The audit's resolver unioned only `pipeline.jsonl`, so a
`source_hypothesis` / `hypothesis_id` ref whose record had aged into
`pipeline-archive.jsonl` read as DANGLING. That is not a reporting defect:
`learning-routing-repair.py --apply` NULLS whatever the audit calls dangling,
and `tree.py::_post_remove_sweep_dangling` fires that --apply automatically
after every tree-node removal. Measured when the write-class gate was filed
(g-115-5659): 191 `hypothesis_id` refs resolved only in the archive and were
being nulled out of experience records. This is the same archive-blindness
`load_all_experiences` already fixed for the experience axis (g-115-5646),
one axis later — the fix is the id union in `build_id_sets`, and these tests
pin every direction of it:

  ARCHIVED-ONLY REF      ->  resolves (NOT dangling)
  LIVE REF               ->  still resolves
  TRULY-MISSING REF      ->  still flagged (the union must not swallow drift)
  ARCHIVE RECORDS        ->  never scanned OUT-BOUND (age-out is a one-way
                             door; re-auditing them resurrects records the
                             pipeline aged on purpose)
                             [UNVERIFIED -- design assertion from the g-115-11522
                             defect report; no standalone ruling record in this
                             world states the one-way-door policy, so it is
                             carried un-sourced]
  REPAIR WRITER          ->  dry-run proposes no repair for the archived ref
  ALL THREE CALL SITES   ->  audit.main, repair.main, ratchet
                             _compute_drift_total all pass the archive in

NOTHING HERE RUNS `--apply`; the writer is asserted on by dry-run output only
(a test that proves the writer safe by invoking the writer has already lost —
the principle this goal's family pins, g-115-5646: see
test_learning_routing_world_scope.py).
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

REPO = SCRIPTS.parents[1]
AUDIT_PY = SCRIPTS / "learning-routing-audit.py"
REPAIR_PY = SCRIPTS / "learning-routing-repair.py"
RATCHET_PY = SCRIPTS / "learning-routing-ratchet.py"

LIVE_HYP = "2026-09-01_live-hypothesis"
ARCHIVED_HYP = "2026-09-02_archived-hypothesis"
GONE_HYP = "2026-09-03_gone-hypothesis"

AUDIT = None


def _load_audit():
    global AUDIT
    if AUDIT is None:
        spec = importlib.util.spec_from_file_location("_lr_archive_audit", AUDIT_PY)
        AUDIT = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(AUDIT)
    return AUDIT


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _fixture_world(tmp_path):
    """A fixture world whose rb refs split exactly across the three outcomes.

    The audit exits 2 before it reports anything unless rb OR guardrails is
    non-empty, so the rb carries the probe refs; guardrails stays empty. No
    record carries an experience_ref, so the (deliberately skipped, foreign)
    experience axis cannot contribute findings.
    """
    world = tmp_path / "world"
    (world / "knowledge" / "tree").mkdir(parents=True)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text("nodes: {}\n",
                                                             encoding="utf-8")
    _write(world / "pipeline.jsonl", [{"id": LIVE_HYP, "status": "active"}])
    _write(world / "pipeline-archive.jsonl",
           [{"id": ARCHIVED_HYP, "status": "resolved"}])
    _write(world / "pattern-signatures.jsonl", [])
    _write(world / "reasoning-bank.jsonl", [
        {"id": "rb-live-ref", "status": "active",
         "source_hypothesis": LIVE_HYP},
        {"id": "rb-archive-ref", "status": "active",
         "source_hypothesis": ARCHIVED_HYP},
        {"id": "rb-gone-ref", "status": "active",
         "source_hypothesis": GONE_HYP},
    ])
    (world / "guardrails.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "meta").mkdir(parents=True, exist_ok=True)
    return world


def _run(script, world, meta):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["STORAGE_BACKEND"] = "local"
    env.pop("MIND_AGENT", None)
    # env= is load-bearing: _paths resolves WORLD_DIR at import time in the
    # child, so omitting it runs the audit against the REAL world and the test
    # asserts against production data while looking hermetic.
    p = subprocess.run([sys.executable, str(script)], env=env,
                       capture_output=True, text=True, timeout=180)
    return p.returncode, p.stdout + p.stderr


# ---------------------------------------------------------------------------
# The hermetic end-to-end pins (subprocess, fixture world).
# ---------------------------------------------------------------------------

def test_audit_resolves_archived_ref_and_still_flags_missing(tmp_path):
    """THE load-bearing test. Pre-fix, the archived ref appeared in the
    dangling list alongside the genuinely missing one; post-fix only the
    missing one does. Both directions are asserted in one run so a resolver
    that flags nothing (and masks real drift) fails the same test that a
    live-only resolver fails."""
    world, meta = _fixture_world(tmp_path), tmp_path / "meta"
    rc, out = _run(AUDIT_PY, world, meta)
    assert rc == 1, "expected drift (the gone ref) and exit 1, got rc=%d:\n%s" % (rc, out)
    dangling_lines = [ln for ln in out.splitlines() if "missing in pipeline" in ln]
    refs = [ln.split(" = ", 1)[1].split(" -> ")[0] for ln in dangling_lines]
    assert "'%s'" % GONE_HYP in refs, \
        "a truly-missing hypothesis id must STILL be flagged:\n" + out
    assert "'%s'" % ARCHIVED_HYP not in refs, \
        "an archived hypothesis id must RESOLVE (the archive is a target):\n" + out
    assert "'%s'" % LIVE_HYP not in refs, \
        "a live hypothesis id must resolve:\n" + out


def test_repair_dry_run_would_not_null_the_archived_ref(tmp_path):
    """The writer-side pin. `--apply` nulls whatever the audit calls dangling,
    so the dry-run listing is the contract: the archived ref must be absent
    from it (no repair proposed, no file named for it) while the genuinely
    missing ref IS proposed. Asserting on paths as well as refs follows the
    world-scope suite: a count that falls for the wrong reason still passes
    a count-only test, and this writer's wrong-reason is the incident."""
    world, meta = _fixture_world(tmp_path), tmp_path / "meta"
    rc, out = _run(REPAIR_PY, world, meta)
    assert rc == 1, "dry run with a dangling ref exits 1:\n%s" % out
    assert ARCHIVED_HYP not in out, \
        "repair proposes touching a ref that resolves in the archive:\n" + out
    assert LIVE_HYP not in out, \
        "repair proposes touching a live ref:\n" + out
    assert GONE_HYP in out, "the genuinely missing ref must still be listed:\n" + out
    # The proposal names its target file; the archive must never be one.
    assert "pipeline-archive.jsonl" not in out, \
        "repair names the archive as a target file:\n" + out


def test_report_announces_the_archive_count(tmp_path):
    """The report header must say the archive was read (N records) — a header
    that silently omits the axis reads as a clean one for anyone skimming
    (guard-1760: report what you looked at, not only what you scanned)."""
    world, meta = _fixture_world(tmp_path), tmp_path / "meta"
    rc, out = _run(AUDIT_PY, world, meta)
    assert "+archive 1" in out, "report must state the archive record count:\n" + out


# ---------------------------------------------------------------------------
# In-process pins: the id union, and the out-bound door stays closed.
# ---------------------------------------------------------------------------

def test_build_id_sets_unions_live_and_archive():
    a = _load_audit()
    stores = {
        "reasoning_bank": [], "guardrails": [],
        "pipeline": [{"id": LIVE_HYP}],
        "pipeline_archive": [{"id": ARCHIVED_HYP}, {"id": "2026-08-31_older"}],
        "pattern_signatures": [], "experience": [],
    }
    ids = a.build_id_sets(stores)
    assert ids["pipeline"] == {LIVE_HYP, ARCHIVED_HYP, "2026-08-31_older"}


def test_build_id_sets_without_archive_key_is_backward_compatible():
    """A caller that has not been updated must not crash on the union: the
    archive half is an empty set, not a KeyError. This is what makes the fix
    deployable from the audit alone before a caller is touched."""
    a = _load_audit()
    stores = {
        "reasoning_bank": [], "guardrails": [],
        "pipeline": [{"id": LIVE_HYP}],
        "pattern_signatures": [], "experience": [],
    }
    assert a.build_id_sets(stores)["pipeline"] == {LIVE_HYP}


def test_archived_ref_resolves_and_missing_ref_does_not():
    a = _load_audit()
    stores = {
        "reasoning_bank": [
            {"id": "rb-archive-ref", "source_hypothesis": ARCHIVED_HYP},
            {"id": "rb-gone-ref", "source_hypothesis": GONE_HYP},
        ],
        "guardrails": [],
        "pipeline": [{"id": LIVE_HYP}],
        "pipeline_archive": [{"id": ARCHIVED_HYP}],
        "pattern_signatures": [],
        "experience": [],
    }
    ids = a.build_id_sets(stores)
    dangling, _prose = a.audit_cross_refs(stores, ids, tree_keys=set())
    flagged = {(d["record_id"], d["field"], d["ref"]) for d in dangling}
    assert flagged == {("rb-gone-ref", "source_hypothesis", GONE_HYP)}, \
        "exactly the missing ref is dangling, nothing more, nothing less: %r" % flagged


def test_archive_records_are_never_scanned_outbound():
    """The age-out door is one-way. If the resolver started auditing
    ARCHIVED records' outbound refs, an archived record carrying a now-dangling
    experience_ref would re-enter the sweep — resurrecting records the
    pipeline aged on purpose. experience is non-empty so the axis IS
    evaluable, which is what makes this discriminating rather than a test of
    the skip banner."""
    a = _load_audit()
    stores = {
        "reasoning_bank": [], "guardrails": [],
        "pipeline": [{"id": LIVE_HYP}],
        "pipeline_archive": [
            {"id": ARCHIVED_HYP, "experience_ref": "exp-vanished-999"},
        ],
        "pattern_signatures": [],
        "experience": [{"id": "exp-live-1"}],
    }
    ids = a.build_id_sets(stores)
    dangling, _prose = a.audit_cross_refs(stores, ids, tree_keys=set())
    assert dangling == [], \
        "archived records must not be scanned out-bound: %r" % dangling


def test_call_sites_pass_the_archive_into_stores():
    """Wiring pin (guard-1943: a correct helper nothing calls ships green).
    All three call sites build the SAME id sets the audit reports — if one
    forgets the archive key, its dangling numbers silently diverge from the
    audit's again, exactly the drift the union exists to remove. Source-level
    by design: the call is a dict literal, invisible to AST call-graphs."""
    for path, needle in (
        (AUDIT_PY, "load_pipeline_archive()"),
        (REPAIR_PY, "audit.load_pipeline_archive()"),
        (RATCHET_PY, "audit.load_pipeline_archive()"),
    ):
        src = path.read_text(encoding="utf-8")
        assert needle in src, \
            "%s does not pass the pipeline archive into its stores dict" % path.name


# ---------------------------------------------------------------------------
# MUTATION CONTROL: the live-only read, reproduced inline, WOULD have flagged
# the archived ref — and `--apply` would have nulled it.
# ---------------------------------------------------------------------------

def test_mutation_control_live_only_resolver_flags_the_archived_ref(tmp_path):
    a = _load_audit()
    world = _fixture_world(tmp_path)
    stores = {
        "reasoning_bank": a._read_jsonl(world / "reasoning-bank.jsonl",
                                        active_only=True),
        "guardrails": a._read_jsonl(world / "guardrails.jsonl",
                                    active_only=True),
        "pipeline": a._read_jsonl(world / "pipeline.jsonl"),
        # pre-fix shape: no "pipeline_archive" key at all
        "pattern_signatures": a._read_jsonl(world / "pattern-signatures.jsonl"),
        "experience": [],
    }
    ids = a.build_id_sets(stores)
    old_ids = {r.get("id") for r in stores["pipeline"] if r.get("id")}
    assert ARCHIVED_HYP not in old_ids, "fixture drifted — the archived id is live now"
    dangling, _prose = a.audit_cross_refs(stores, ids, tree_keys=set())
    flagged = {d["ref"] for d in dangling}
    assert ARCHIVED_HYP in flagged and GONE_HYP in flagged, \
        "the pre-fix resolver must flag BOTH the archived and the missing ref"

    # The consequence, spelled out: under the pre-fix ids, repair proposes to
    # null a VALID ref. The post-fix ids must not.
    ids_fixed = a.build_id_sets(dict(stores, pipeline_archive=[
        {"id": ARCHIVED_HYP}]))
    dangling_fixed, _ = a.audit_cross_refs(stores, ids_fixed, tree_keys=set())
    assert ARCHIVED_HYP not in {d["ref"] for d in dangling_fixed}
    assert flagged != {d["ref"] for d in dangling_fixed}, \
        "control drifted — the union changed nothing"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
