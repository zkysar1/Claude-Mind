"""Phase 1D (): stale-Body WM staging + generalize-down staged consumer.

The orphan-WM preservation loop has two halves:

  PRODUCER  cleanup-stale-bindings.sh `_preserve_unmerged_body_wm` copies a stale
            Body's forked WM to session/pending-body-merges/<unitKey>-wm.yaml
            BEFORE `rm -rf` reaps the Body dir -- but ONLY for a non-reducer
            worker (forked WM exists) whose manifest is not already `merged`.

  CONSUMER  body-merge.py `generalize_down` -> `_consume_staged` scans
            session/pending-body-merges/*-wm.yaml (independent of sessions/),
            merges each orphan (no forked_wm_hash baseline => unconditional),
            and deletes the staged file (consumed exactly once).

Daemon-safe (no daemon_integration marker -- pure path/file arithmetic; the
consumer takes project_root explicitly, the producer is exercised via a copy of
the IRREDUCIBLY-LOCAL script into a tmp repo skeleton so its self-derived
PROJECT_ROOT points at tmp instead of the real repo).

Run:
  python -m pytest core/scripts/tests/test_body_merge_staged.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent                # core/scripts/
CLEANUP_SH = CORE_SCRIPTS / "cleanup-stale-bindings.sh"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402

SID = "44444444-4444-4444-8444-444444444444"


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge = _load("body_merge", "body-merge.py")


# ─────────────────────────── helpers ───────────────────────────

def _mk_agent(tmp_path: Path, reducer_wm: dict | None = None, name: str = "alpha") -> Path:
    state = tmp_path / "agents" / name / "session"
    state.mkdir(parents=True, exist_ok=True)
    if reducer_wm is not None:
        with open(state / "working-memory.yaml", "w", encoding="utf-8") as f:
            yaml.dump(reducer_wm, f, default_flow_style=False, sort_keys=False)
    return tmp_path


def _stage(pr: Path, sid: str, body_wm: dict, name: str = "alpha") -> Path:
    staged = pr / "agents" / name / "session" / "pending-body-merges"
    staged.mkdir(parents=True, exist_ok=True)
    p = staged / f"{sid}-wm.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(body_wm, f, default_flow_style=False, sort_keys=False)
    return p


def _read_reducer(pr: Path, name: str = "alpha") -> dict:
    return yaml.safe_load(
        (pr / "agents" / name / "session" / "working-memory.yaml").read_text(encoding="utf-8")) or {}


# ─────────────────────── CONSUMER: _consume_staged via generalize_down ───────────────────────

def test_staged_orphan_merged_and_consumed(tmp_path):
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"active_context": {"a": 1}}})
    staged_file = _stage(pr, SID, {"slots": {"body_only_slot": {"from": "orphan"}}})
    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["staged_merged"]
    red = _read_reducer(pr)
    # body-only slot carried into the reducer; reducer's own slot preserved
    assert red["slots"].get("body_only_slot") == {"from": "orphan"}
    assert red["slots"].get("active_context") == {"a": 1}
    # consumed exactly once -> staged file deleted
    assert not staged_file.exists()


def test_no_staging_dir_is_noop(tmp_path):
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"x": 1}})
    summary = merge.generalize_down("alpha", project_root=pr)
    assert summary["staged_merged"] == []
    assert _read_reducer(pr) == {"slots": {"x": 1}}


def test_staged_malformed_skipped_and_deleted(tmp_path):
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"x": 1}})
    staged = pr / "agents" / "alpha" / "session" / "pending-body-merges"
    staged.mkdir(parents=True, exist_ok=True)
    bad = staged / f"{SID}-wm.yaml"
    bad.write_text(": : not valid yaml : :\n", encoding="utf-8")
    summary = merge.generalize_down("alpha", project_root=pr)
    # malformed -> skipped, but still consumed (no infinite retry next pass)
    assert SID in summary["skipped"]
    assert SID not in summary["staged_merged"]
    assert not bad.exists()
    assert _read_reducer(pr) == {"slots": {"x": 1}}  # reducer untouched


# ─────────────────────── PRODUCER: cleanup-stale-bindings staging ───────────────────────

def _repo_skeleton(tmp_path: Path) -> Path:
    """Copy cleanup-stale-bindings.sh into tmp/core/scripts/ so its self-derived
    PROJECT_ROOT (SCRIPT_DIR/../..) points at tmp -- the only way to redirect the
    IRREDUCIBLY-LOCAL script off the real repo without a production seam."""
    dst = tmp_path / "core" / "scripts"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CLEANUP_SH, dst / "cleanup-stale-bindings.sh")
    return dst / "cleanup-stale-bindings.sh"


def _call_preserve(script: Path, agent: str, body_dir: Path, sid: str):
    # Source the copied script (its sweeps no-op on the empty tmp repo -- the
    # Body dir we create has no binding.yaml, so the Phase-2.6 sweep skips it),
    # then invoke the function under test directly.
    code = (f'source "{script.as_posix()}"; '
            f'_preserve_unmerged_body_wm "{agent}" "{body_dir.as_posix()}" "{sid}"')
    return subprocess.run([BASH, "-c", code], capture_output=True, text=True, timeout=30)


def _mk_body_dir(tmp_path: Path, sid: str, body_state: str, name: str = "alpha",
                 with_wm: bool = True) -> Path:
    bd = tmp_path / "agents" / name / "sessions" / sid
    bd.mkdir(parents=True, exist_ok=True)
    if with_wm:
        (bd / "working-memory.yaml").write_text("slots:\n  forked: true\n", encoding="utf-8")
    (bd / "body-manifest.yaml").write_text(
        f"unitKey: {sid}\nmindKey: {name}\nbody_state: {body_state}\n", encoding="utf-8")
    return bd


def _staged_path(tmp_path: Path, sid: str, name: str = "alpha") -> Path:
    return tmp_path / "agents" / name / "session" / "pending-body-merges" / f"{sid}-wm.yaml"


def test_preserve_stages_unmerged_worker_wm(tmp_path):
    script = _repo_skeleton(tmp_path)
    bd = _mk_body_dir(tmp_path, SID, "closed-pending-merge")
    r = _call_preserve(script, "alpha", bd, SID)
    assert r.returncode == 0, r.stderr
    staged = _staged_path(tmp_path, SID)
    assert staged.is_file(), f"expected staged WM; stderr={r.stderr}"
    assert "forked: true" in staged.read_text(encoding="utf-8")


def test_preserve_skips_already_merged(tmp_path):
    script = _repo_skeleton(tmp_path)
    bd = _mk_body_dir(tmp_path, SID, "merged")
    r = _call_preserve(script, "alpha", bd, SID)
    assert r.returncode == 0, r.stderr
    assert not _staged_path(tmp_path, SID).exists(), \
        "already-merged Body must NOT be staged (double-merge guard)"


def test_preserve_noop_when_no_forked_wm(tmp_path):
    # A reducer/observer Body dir with NO working-memory.yaml -> nothing to stage.
    script = _repo_skeleton(tmp_path)
    bd = _mk_body_dir(tmp_path, SID, "closed-stale", with_wm=False)
    r = _call_preserve(script, "alpha", bd, SID)
    assert r.returncode == 0, r.stderr
    assert not _staged_path(tmp_path, SID).parent.exists(), \
        "no forked WM -> no staging dir created"


# ─────────────────────── END-TO-END: stage then reclaim ───────────────────────

def test_stage_then_generalize_down_reclaims(tmp_path):
    script = _repo_skeleton(tmp_path)
    _mk_agent(tmp_path, reducer_wm={"slots": {"active_context": {"x": 1}}})
    bd = tmp_path / "agents" / "alpha" / "sessions" / SID
    bd.mkdir(parents=True, exist_ok=True)
    (bd / "working-memory.yaml").write_text(
        "slots:\n  orphan_slot:\n    reclaimed: true\n", encoding="utf-8")
    (bd / "body-manifest.yaml").write_text(
        f"unitKey: {SID}\nbody_state: closed-pending-merge\n", encoding="utf-8")
    # PRODUCER stages the WM
    r = _call_preserve(script, "alpha", bd, SID)
    assert r.returncode == 0, r.stderr
    assert _staged_path(tmp_path, SID).is_file()
    # Real cleanup-stale-bindings.sh removes the Body dir (rm -rf) right after
    # staging, so by generalize-down time only the staged copy survives. Simulate
    # that here — otherwise the sessions-pass would ALSO find sessions/<SID>/ and
    # the  dedup guard would (correctly) skip the staged copy as a
    # double-merge. The race where BOTH are visible is covered by
    # test_staged_dedup_skips_when_already_merged_in_sessions below.
    shutil.rmtree(bd)
    # CONSUMER reclaims it from staging (and deletes the staged file)
    summary = merge.generalize_down("alpha", project_root=tmp_path)
    assert SID in summary["staged_merged"]
    assert _read_reducer(tmp_path)["slots"].get("orphan_slot") == {"reclaimed": True}
    assert not _staged_path(tmp_path, SID).exists()


# ─────────────── : dedup guard + hash no-op short-circuit ───────────────
# These exercise the three guards _consume_staged gained in Phase 2B (the earlier
# "merge unconditionally" path is the regression class they pin).

def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_staged_dedup_skips_when_already_merged_in_sessions(tmp_path):
    # The cleanup-vs-generalize-down race: BOTH sessions/<SID>/ and the staged
    # copy are visible. The sessions-pass merges the Body (adds SID to `already`);
    # _consume_staged must then DEDUP the staged copy (consume, NOT re-merge),
    # else the counter double-counts. Discriminating assertion: a numeric counter
    # merges exactly ONCE (8+10=18), not twice (8+10+10=28).
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"loop_state": {"goals_completed": 8}}})
    bd = pr / "agents" / "alpha" / "sessions" / SID
    bd.mkdir(parents=True, exist_ok=True)
    body_wm = "slots:\n  loop_state:\n    goals_completed: 10\n"
    (bd / "working-memory.yaml").write_text(body_wm, encoding="utf-8")
    # No forked_wm_hash -> sessions-pass MERGES (not a no-op short-circuit).
    (bd / "body-manifest.yaml").write_text(
        f"unitKey: {SID}\nbody_state: closed-pending-merge\n", encoding="utf-8")
    # Staged copy of the SAME Body (what cleanup would have left if it raced).
    staged_file = _stage(pr, SID, {"slots": {"loop_state": {"goals_completed": 10}}})

    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["merged"], "sessions-pass should merge the Body once"
    assert SID in summary["staged_dedup"], "staged copy must be deduped, not re-merged"
    assert SID not in summary["staged_merged"]
    # Merged exactly once: 8 + (10 with no baseline -> 2-way SUM) = 18, NOT 28.
    assert _read_reducer(pr)["slots"]["loop_state"]["goals_completed"] == 18
    assert not staged_file.exists()


def test_staged_noop_short_circuit_when_hash_matches(tmp_path):
    # A staged orphan whose WM still hashes to its fork baseline never diverged:
    # _consume_staged must NO-OP it (consume without merging), mirroring the
    # sessions-pass hash short-circuit that staged orphans previously lacked.
    # No sessions/ dir -> generalize_down hits the early-return staged-only path.
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"x": 1}})
    staged_file = _stage(pr, SID, {"slots": {"body_slot": {"v": 1}}})
    hash_file = staged_file.parent / f"{SID}-wm.hash"
    hash_file.write_text(_sha256_file(staged_file), encoding="utf-8")  # matches on-disk bytes

    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["noop"], "hash match -> no-op (never diverged)"
    assert SID not in summary["staged_merged"]
    # Reducer untouched: the never-diverged orphan contributed nothing.
    assert _read_reducer(pr) == {"slots": {"x": 1}}
    assert not staged_file.exists() and not hash_file.exists()  # both consumed


def test_staged_merges_when_hash_mismatch(tmp_path):
    # A staged orphan whose WM does NOT match its sidecar hash genuinely diverged
    # -> merge (2-way; only the hash, not baseline content, is staged).
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"x": 1}})
    staged_file = _stage(pr, SID, {"slots": {"body_slot": {"v": 1}}})
    hash_file = staged_file.parent / f"{SID}-wm.hash"
    hash_file.write_text("0" * 64, encoding="utf-8")  # deliberately wrong hash

    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["staged_merged"], "hash mismatch -> diverged -> merge"
    assert SID not in summary["noop"]
    assert _read_reducer(pr)["slots"].get("body_slot") == {"v": 1}
    assert not staged_file.exists() and not hash_file.exists()


# ─────────────── : PRODUCER stages the forked_wm_hash sidecar ───────────────

def test_preserve_stages_forked_wm_hash(tmp_path):
    # When the manifest carries forked_wm_hash, _preserve_unmerged_body_wm must
    # stage it to <sid>-wm.hash alongside the WM (the no-op short-circuit input).
    script = _repo_skeleton(tmp_path)
    bd = tmp_path / "agents" / "alpha" / "sessions" / SID
    bd.mkdir(parents=True, exist_ok=True)
    (bd / "working-memory.yaml").write_text("slots:\n  forked: true\n", encoding="utf-8")
    (bd / "body-manifest.yaml").write_text(
        f"unitKey: {SID}\nmindKey: alpha\nbody_state: closed-pending-merge\n"
        f"forked_wm_hash: abc123def456\n", encoding="utf-8")
    r = _call_preserve(script, "alpha", bd, SID)
    assert r.returncode == 0, r.stderr
    staged_wm = _staged_path(tmp_path, SID)
    staged_hash = staged_wm.parent / f"{SID}-wm.hash"
    assert staged_wm.is_file()
    assert staged_hash.is_file(), f"expected hash sidecar; stderr={r.stderr}"
    assert staged_hash.read_text(encoding="utf-8").strip() == "abc123def456"


def test_preserve_no_hash_sidecar_when_manifest_lacks_forked_wm_hash(tmp_path):
    # The dormant/legacy manifest has no forked_wm_hash -> WM staged, no sidecar.
    script = _repo_skeleton(tmp_path)
    bd = _mk_body_dir(tmp_path, SID, "closed-pending-merge")  # _mk_body_dir omits the hash
    r = _call_preserve(script, "alpha", bd, SID)
    assert r.returncode == 0, r.stderr
    assert _staged_path(tmp_path, SID).is_file()
    staged_hash = _staged_path(tmp_path, SID).parent / f"{SID}-wm.hash"
    assert not staged_hash.exists(), "no forked_wm_hash in manifest -> no hash sidecar"


# ───────────── -c: staged 3-way delta (baseline-aware _consume_staged) ─────────────
#
# THE DEFECT UNDER TEST is counter DOUBLE-COUNTING, not "did a merge happen".
# Both sides forked from baseline B, so 2-way `r + b` counts B twice. Every
# assertion below is therefore on a COUNTER THE WORKER NEVER TOUCHED — the case
# where the correct answer is "unchanged" and the buggy answer is "doubled".
# A test asserting only that a merge occurred passes under both policies.

def _stage_baseline(pr: Path, sid: str, baseline_wm: dict, name: str = "alpha") -> Path:
    staged = pr / "agents" / name / "session" / "pending-body-merges"
    staged.mkdir(parents=True, exist_ok=True)
    p = staged / f"{sid}-wm-baseline.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(baseline_wm, f, default_flow_style=False, sort_keys=False)
    return p


def test_staged_3way_does_not_double_count_untouched_counter(tmp_path):
    # Worker forked at goals=10 and NEVER advanced it. Reducer is still at 10.
    # 2-way: 10 + 10 == 20 (wrong). 3-way: 10 + (10 - 10) == 10 (right).
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"goals": 10}})
    _stage(pr, SID, {"slots": {"goals": 10}})
    _stage_baseline(pr, SID, {"slots": {"goals": 10}})
    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["staged_merged"]
    assert _read_reducer(pr)["slots"]["goals"] == 10, \
        "untouched counter was double-counted -> staged merge is still 2-way"


def test_staged_3way_credits_only_the_worker_delta(tmp_path):
    # Concurrent advance: baseline 10, worker -> 13 (+3), reducer -> 12 (+2).
    # 2-way: 12 + 13 == 25 (double-counts the shared 10). 3-way: 12 + 3 == 15.
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"goals": 12}})
    _stage(pr, SID, {"slots": {"goals": 13}})
    _stage_baseline(pr, SID, {"slots": {"goals": 10}})
    merge.generalize_down("alpha", project_root=pr)
    assert _read_reducer(pr)["slots"]["goals"] == 15


def test_staged_without_baseline_retains_2way_sum_fallback(tmp_path):
    # No baseline sidecar (pre--c staging, or a crash-preserve that
    # could not capture one) -> the RETAINED 2-way union+SUM must still apply.
    # This is a strict upgrade, never a replacement.
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"goals": 10}})
    _stage(pr, SID, {"slots": {"goals": 10}})
    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["staged_merged"]
    assert _read_reducer(pr)["slots"]["goals"] == 20, \
        "2-way fallback regressed -- a baseline-less orphan must still union+SUM"


def test_staged_baseline_is_consumed_with_the_wm(tmp_path):
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"goals": 1}})
    staged_wm = _stage(pr, SID, {"slots": {"goals": 2}})
    staged_base = _stage_baseline(pr, SID, {"slots": {"goals": 1}})
    merge.generalize_down("alpha", project_root=pr)
    assert not staged_wm.exists()
    assert not staged_base.exists(), "baseline sidecar leaked -- not consumed once"


def test_staged_malformed_baseline_falls_back_to_2way_without_raising(tmp_path):
    # Truncated/half-copied baseline is a REAL shape here: staging is the
    # crash-preserve path. It must degrade to 2-way, not abort the whole drain.
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"goals": 10}})
    _stage(pr, SID, {"slots": {"goals": 10}})
    bad = pr / "agents" / "alpha" / "session" / "pending-body-merges" / f"{SID}-wm-baseline.yaml"
    bad.write_text(": : not valid yaml : :\n", encoding="utf-8")
    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["staged_merged"]
    assert _read_reducer(pr)["slots"]["goals"] == 20  # 2-way fallback
    assert not bad.exists()


def test_staged_never_diverged_noop_outranks_the_3way_branch(tmp_path):
    # Guard 2 (hash short-circuit) must still win over Guard 3: a never-diverged
    # orphan is consumed WITHOUT merging even when a baseline is present.
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"goals": 10}})
    staged_wm = _stage(pr, SID, {"slots": {"goals": 10}})
    _stage_baseline(pr, SID, {"slots": {"goals": 10}})
    h = hashlib.sha256(staged_wm.read_bytes()).hexdigest()
    (staged_wm.parent / f"{SID}-wm.hash").write_text(h, encoding="utf-8")
    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["noop"]
    assert SID not in summary["staged_merged"]


def test_orphan_baseline_alone_is_not_drained_as_a_wm(tmp_path):
    # Naming guard: `-wm-baseline.yaml` must NOT match the `*-wm.yaml` drain
    # glob, or a baseline would be merged as if it were a worker WM.
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"goals": 10}})
    orphan = _stage_baseline(pr, SID, {"slots": {"goals": 999}})
    summary = merge.generalize_down("alpha", project_root=pr)
    assert summary["scanned"] == 0
    assert summary["staged_merged"] == []
    assert _read_reducer(pr)["slots"]["goals"] == 10
    assert orphan.exists(), "a lone baseline is not a WM -- must not be drained"


# ─────────── -c PRODUCER: cleanup-stale-bindings stages the baseline ───────────

def test_preserve_stages_baseline_when_the_fork_writer_left_one(tmp_path):
    # Without this the consumer's 3-way branch has no producer and is dead code.
    script = _repo_skeleton(tmp_path)
    bd = _mk_body_dir(tmp_path, SID, "closed-pending-merge")
    (bd / "forked-wm-baseline.yaml").write_text("slots:\n  goals: 7\n", encoding="utf-8")
    r = _call_preserve(script, "alpha", bd, SID)
    assert r.returncode == 0, r.stderr
    staged_base = _staged_path(tmp_path, SID).parent / f"{SID}-wm-baseline.yaml"
    assert staged_base.is_file(), f"expected staged baseline; stderr={r.stderr}"
    assert "goals: 7" in staged_base.read_text(encoding="utf-8")


def test_preserve_no_baseline_sidecar_when_fork_left_none(tmp_path):
    # Absent baseline is NORMAL (legacy fork / crash-preserve) -> silent, and the
    # WM is still staged so the 2-way fallback can serve it.
    script = _repo_skeleton(tmp_path)
    bd = _mk_body_dir(tmp_path, SID, "closed-pending-merge")  # no baseline written
    r = _call_preserve(script, "alpha", bd, SID)
    assert r.returncode == 0, r.stderr
    assert _staged_path(tmp_path, SID).is_file()
    staged_base = _staged_path(tmp_path, SID).parent / f"{SID}-wm-baseline.yaml"
    assert not staged_base.exists()


def test_preserve_writes_trigger_last_so_sidecars_are_never_missed(tmp_path):
    # ORDERING PIN (-c fresh-eyes). _consume_staged globs `*-wm.yaml`,
    # so the WM is the CONSUMER'S TRIGGER and every sidecar must exist before it
    # appears. If the WM is written first, a concurrent generalize_down sees it
    # without its sidecars and silently degrades: no baseline -> 2-way SUM (the
    # double-count), no hash -> guard 2 skipped. Both degraded paths are valid for
    # a genuinely sidecar-less staging, so the race produces a wrong number with
    # NO error. Asserting on mtime is unreliable at this resolution; assert on the
    # SOURCE ORDER instead, which is what actually governs.
    src = CLEANUP_SH.read_text(encoding="utf-8")
    body = src[src.index("_preserve_unmerged_body_wm() {"):]
    body = body[: body.index("\n}\n")]
    wm_at = body.index('"$_STAGE_DIR/${_SID}-wm.yaml"')
    hash_at = body.index('"$_STAGE_DIR/${_SID}-wm.hash"')
    base_at = body.index('"$_STAGE_DIR/${_SID}-wm-baseline.yaml"')
    assert hash_at < wm_at, "hash sidecar must be staged BEFORE the -wm.yaml trigger"
    assert base_at < wm_at, "baseline sidecar must be staged BEFORE the -wm.yaml trigger"


def test_preserve_stages_all_three_with_baseline_present(tmp_path):
    # End-to-end companion to the ordering pin: all three land for a worker fork
    # that has a baseline AND a forked_wm_hash.
    script = _repo_skeleton(tmp_path)
    bd = _mk_body_dir(tmp_path, SID, "closed-pending-merge")
    (bd / "body-manifest.yaml").write_text(
        f"unitKey: {SID}\nmindKey: alpha\nbody_state: closed-pending-merge\n"
        f"forked_wm_hash: abc123def456\n", encoding="utf-8")
    (bd / "forked-wm-baseline.yaml").write_text("slots:\n  goals: 7\n", encoding="utf-8")
    r = _call_preserve(script, "alpha", bd, SID)
    assert r.returncode == 0, r.stderr
    staged = _staged_path(tmp_path, SID)
    assert staged.is_file()
    assert (staged.parent / f"{SID}-wm.hash").is_file()
    assert (staged.parent / f"{SID}-wm-baseline.yaml").is_file()


# ───────── : the SAME trigger-last rule on the G2b close-time path ─────────
#
# The pin above covers the bash reap path only. body-manifest.py's close-time
# path had the rule INVERTED at both of its stages until . These two
# pins assert the OBSERVED sequence rather than a source-text index, because
# here the order is executable: the write loop and the push loop can both be
# run against a recorder.

bm = _load("body_manifest", "body-manifest.py")


class _RecordingBackend:
    """Records the ORDER of write_bytes targets.

    guard-2220: answers by ARGUMENT IDENTITY (it records whatever path it is
    handed and always returns the same thing), never by a positional queue of
    returns. A `side_effect=[...]` list would silently encode the production
    call ORDER — the very thing these tests exist to assert — so adding a call
    upstream would shift every later answer and redden unrelated tests.
    """

    def __init__(self):
        self.pushed: list[str] = []

    def write_bytes(self, target, data):  # noqa: D102 — stub
        self.pushed.append(Path(target).name)
        return True


def _install_stub_backend(monkeypatch, backend):
    """push_staged_files imports storage_backend INSIDE the function, so the
    stub has to be in sys.modules at call time rather than patched on bm."""
    mod = types.ModuleType("storage_backend")
    mod.get_backend = lambda: backend
    monkeypatch.setitem(sys.modules, "storage_backend", mod)


def _mk_staged(tmp_path: Path, unit_key: str) -> Path:
    staged = tmp_path / "session" / "pending-body-merges"
    staged.mkdir(parents=True)
    (staged / f"{unit_key}-wm.yaml").write_text("slots: {}\n", encoding="utf-8")
    (staged / f"{unit_key}-wm-baseline.yaml").write_text("slots: {}\n", encoding="utf-8")
    (staged / f"{unit_key}-wm.hash").write_text("deadbeef\n", encoding="utf-8")
    return staged


def test_push_staged_files_pushes_trigger_last(tmp_path, monkeypatch):
    # Asserts the PUSH sequence. This half matters more than the write half:
    # each write_bytes is a separate backend round trip to a store a reducer on
    # ANOTHER box polls, so a trigger pushed first is remotely visible for two
    # more round trips before its sidecars land.
    uk = "sid-push-order"
    staged = _mk_staged(tmp_path, uk)
    be = _RecordingBackend()
    _install_stub_backend(monkeypatch, be)

    assert bm.push_staged_files(staged, uk) is True
    assert len(be.pushed) == 3, f"expected all three pushed, got {be.pushed}"
    assert be.pushed[-1] == f"{uk}-wm.yaml", (
        f"the -wm.yaml TRIGGER must be pushed LAST; observed order {be.pushed}. "
        "body-merge.py globs '*-wm.yaml' as the only enumerator of the staging "
        "dir, so a trigger visible before its sidecars is consumed without them "
        "(2-way SUM double-count, guard 2 skipped) and then unlinks all three — "
        "orphaning the sidecars that arrive afterwards permanently.")


def test_stage_and_push_writes_trigger_last(tmp_path, monkeypatch):
    # Asserts the local WRITE sequence, the sibling of the push pin above.
    uk = "sid-write-order"
    session_dir = tmp_path / "sessions" / uk
    session_dir.mkdir(parents=True)
    (session_dir / bm._WM_FILENAME).write_text("slots: {}\n", encoding="utf-8")
    (session_dir / bm._BASELINE_FILENAME).write_text("slots: {}\n", encoding="utf-8")
    state_dir = tmp_path / "session"
    (state_dir / "pending-body-merges").mkdir(parents=True)

    written: list[str] = []
    real_write = bm._write_atomic_bytes
    monkeypatch.setattr(
        bm, "_write_atomic_bytes",
        lambda target, data: (written.append(Path(target).name),
                              real_write(target, data))[1])
    _install_stub_backend(monkeypatch, _RecordingBackend())

    assert bm._stage_and_push(
        session_dir, state_dir,
        {"unitKey": uk, "forked_wm_hash": "deadbeef"}) is True
    assert len(written) == 3, f"expected all three staged, got {written}"
    assert written[-1] == f"{uk}-wm.yaml", (
        f"the -wm.yaml TRIGGER must be WRITTEN LAST; observed order {written}")


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
