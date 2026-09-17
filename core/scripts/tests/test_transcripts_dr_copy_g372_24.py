"""Pins for the transcripts DR lane and the copier's multipart-compare mode
(g-372-24, 2026-09-16).

THE DEFECT THE `size` MODE EXISTS TO FIX. `owncloud-store-enumerate.py copy`
decides whether a MULTIPART object already matches at the destination from
LastModified, because a multipart ETag is not comparable across stores
(guard-6371). That rule reads "source newer than destination means the source
moved on", which holds only when the destination was written FROM the source.
On the transcripts DR lane the destination is the PRE-cutover AWS original and
the source is the migrated basement copy, so every multipart object is newer
forever. Measured on the live stores the same day: the default arm proposed 615
objects / 19.80 GiB where the true delta was 516 / 4.16 GiB.

The flag selects between two SEMANTICS, not two features, so every test here
that touches it uses an input on which the two modes MUST disagree (guard-3292):
a same-size multipart object whose source is newer. On the simplest inputs the
modes agree, which is exactly why an end-to-end hand-run would have passed.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
COPIER = SCRIPT_DIR / "owncloud-store-enumerate.py"
TICK = SCRIPT_DIR / "cold-snapshot-tick.py"

OLD = "2026-09-10T00:00:00Z"
NEW = "2026-09-14T19:00:00Z"


def _load(path: Path, name: str):
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def copier():
    return _load(COPIER, "_ose_copy_under_test")


@pytest.fixture(scope="module")
def tick():
    return _load(TICK, "_tick_under_test")


class _Paginator:
    def __init__(self, objs):
        self._objs = objs

    def paginate(self, **_kw):
        return [{"Contents": self._objs}]


class _SrcS3:
    def __init__(self, objs):
        self._objs = objs

    def get_paginator(self, _name):
        return _Paginator(self._objs)

    def get_object(self, Bucket, Key):  # noqa: N803 -  signature
        return {"Body": io.BytesIO(b"x" * 10)}


class _DstS3:
    def __init__(self, state):
        self.state = state
        self.uploads: list[str] = []

    def head_object(self, Bucket, Key):  # noqa: N803 -  signature
        if Key not in self.state:
            raise _NotFound()
        return self.state[Key]

    def upload_fileobj(self, _body, _bucket, key):
        self.uploads.append(key)


class _NotFound(Exception):
    response = {"Error": {"Code": "404"}}


class _Backend:
    def __init__(self, s3):
        self.s3 = s3


def _run_copy(copier, monkeypatch, src_objs, dst_state, mode):
    """Drive cmd_copy with fake stores and return its report."""
    dst_s3 = _DstS3(dst_state)
    built = iter([_Backend(_SrcS3(src_objs)), _Backend(dst_s3)])
    monkeypatch.setattr(copier, "_build_backend", lambda *a, **k: next(built))
    args = types.SimpleNamespace(
        bucket="b", dest_bucket="b", source_endpoint="http://src:9000",
        dest_endpoint="", region="us-east-2", prefix="ayoai-mind/transcripts/",
        limit=0, dry_run=False, workers=2, progress_every=0,
        multipart_compare=mode)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = copier.cmd_copy(args)
    monkeypatch.undo()
    return rc, json.loads(out.getvalue()), dst_s3


# --- the disagreeing input (guard-3292) ---------------------------------------

MULTIPART_SAME_SIZE_SRC_NEWER = (
    [{"Key": "ayoai-mind/transcripts/cc-13/a.jsonl", "Size": 100,
      "ETag": '"aaaa-3"', "LastModified": NEW}],
    {"ayoai-mind/transcripts/cc-13/a.jsonl":
     {"ContentLength": 100, "ETag": '"bbbb-2"', "LastModified": OLD}},
)


def test_timestamp_mode_recopies_the_migrated_multipart_object(copier, monkeypatch):
    """The DEFAULT arm, on the DR lane's real shape: same size, both ETags
    multipart, source newer because the cutover wrote it. It re-copies — this is
    the 19.80 GiB/day behaviour, pinned so the fix cannot be silently reverted."""
    src, dst = MULTIPART_SAME_SIZE_SRC_NEWER
    _rc, rep, dst_s3 = _run_copy(copier, monkeypatch, src, dict(dst), "timestamp")
    assert rep["copied"] == 1 and rep["skipped_already_present"] == 0
    assert dst_s3.uploads == ["ayoai-mind/transcripts/cc-13/a.jsonl"]


def test_size_mode_skips_the_same_object(copier, monkeypatch):
    """The SAME input under `size`: present at the destination at the same size,
    so nothing transfers. The two modes MUST disagree here or the flag is inert."""
    src, dst = MULTIPART_SAME_SIZE_SRC_NEWER
    _rc, rep, dst_s3 = _run_copy(copier, monkeypatch, src, dict(dst), "size")
    assert rep["copied"] == 0 and rep["skipped_already_present"] == 1
    assert dst_s3.uploads == []


# --- the arms the flag must NOT touch ----------------------------------------

def test_size_mode_still_copies_when_the_size_differs(copier, monkeypatch):
    """`size` is a skip rule, never a blanket skip: a GROWN transcript (the
    append-only case this lane is built for) still copies."""
    src = [{"Key": "k", "Size": 250, "ETag": '"aaaa-3"', "LastModified": NEW}]
    dst = {"k": {"ContentLength": 100, "ETag": '"bbbb-2"', "LastModified": OLD}}
    _rc, rep, dst_s3 = _run_copy(copier, monkeypatch, src, dst, "size")
    assert rep["copied"] == 1 and dst_s3.uploads == ["k"]


def test_size_mode_leaves_the_comparable_single_part_arm_alone(copier, monkeypatch):
    """Single-part ETags ARE content md5s (guard-6371), so that arm stays an
    ETag comparison under both modes: same size, different ETag => copy."""
    src = [{"Key": "k", "Size": 100, "ETag": '"aaaa"', "LastModified": NEW}]
    dst = {"k": {"ContentLength": 100, "ETag": '"bbbb"', "LastModified": OLD}}
    for mode in ("timestamp", "size"):
        _rc, rep, _d = _run_copy(copier, monkeypatch, src, dict(dst), mode)
        assert rep["copied"] == 1, mode


def test_absent_at_destination_copies_under_both_modes(copier, monkeypatch):
    src = [{"Key": "k", "Size": 100, "ETag": '"aaaa-3"', "LastModified": NEW}]
    for mode in ("timestamp", "size"):
        _rc, rep, _d = _run_copy(copier, monkeypatch, src, {}, mode)
        assert rep["copied"] == 1, mode


def test_report_always_names_the_mode(copier, monkeypatch):
    """Shape-stable: the field is present under BOTH modes, never only when the
    flag was passed (guard-527), so a reader can always tell which ran."""
    src = [{"Key": "k", "Size": 100, "ETag": '"aaaa"', "LastModified": NEW}]
    for mode in ("timestamp", "size"):
        _rc, rep, _d = _run_copy(copier, monkeypatch, src, {}, mode)
        assert rep["multipart_compare"] == mode


# --- the DR lane -------------------------------------------------------------

@pytest.fixture(scope="module")
def dr():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import transcripts_dr_copy
    return transcripts_dr_copy


def test_prefix_is_built_from_the_same_expression_the_copier_uses(dr):
    assert dr._prefix_for("ayoai-mind", "") == "ayoai-mind/transcripts/"
    assert dr._prefix_for("ayoai-mind", "acme/") == "acme/ayoai-mind/transcripts/"


# Every target carries the keys `resolve_cold_target` always sets in its `base`
# dict — `target_line` reads them unconditionally, so a thinner fake would test a
# shape the resolver never produces.
_BASE = {"backend": "own-cloud", "live_bucket": "live-bucket",
         "live_endpoint": "http://100.76.251.73:9000"}


@pytest.mark.parametrize("target,expected", [
    ({**_BASE, "mode": "refused", "verdict": "refused-colocated", "reason": "r"},
     "refused-colocated"),
    ({**_BASE, "mode": "local", "reason": "r"}, "skipped-local-backend"),
    ({**_BASE, "mode": "live", "endpoint": "aws-regional", "bucket": "b",
      "reason": "r"}, "skipped-live-is-dr"),
])
def test_non_pinned_targets_never_transfer(dr, monkeypatch, target, expected):
    """A refusal is a deliberate state, not a crash — and none of them may reach
    the copier. `refused-colocated` is the load-bearing one: it is what stops the
    DR copy landing on the store it exists to protect."""
    import cold_snapshot
    monkeypatch.setattr(cold_snapshot, "resolve_cold_target", lambda _e: target)
    def _boom(*_a, **_k):
        raise AssertionError("the copier must not run for a non-pinned target")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert dr.run(False, 4, 60)["verdict"] == expected


def test_pinned_run_pins_the_size_mode_and_the_regional_dest(dr, monkeypatch):
    """The mandatory flag, asserted at the argv. `--multipart-compare size` is
    what keeps this lane from re-uploading the whole archive daily, and the
    aws-regional sentinel must reach the copier as an EMPTY endpoint (which is
    how its `_endpoint` helper selects regional AWS) — not as the literal string."""
    import cold_snapshot
    monkeypatch.setattr(cold_snapshot, "resolve_cold_target", lambda _e: {
        "mode": "pinned", "endpoint": cold_snapshot.AWS_REGIONAL,
        "bucket": "dr-bucket", "live_bucket": "live-bucket",
        "live_endpoint": "http://100.76.251.73:9000", "backend": "own-cloud"})
    monkeypatch.setattr(dr, "_resolve_prefix", lambda: "ayoai-mind/transcripts/")
    seen = {}

    def _fake_run(argv, **_kw):
        seen["argv"] = argv
        return types.SimpleNamespace(
            returncode=0, stderr="",
            stdout=json.dumps({"copied": 3, "skipped_already_present": 9,
                               "failed": 0, "scanned": 12, "copied_gib": 0.1,
                               "elapsed_s": 1.0, "prefix": "ayoai-mind/transcripts/"}))
    monkeypatch.setattr(subprocess, "run", _fake_run)

    out = dr.run(False, 4, 60)
    assert out["verdict"] == "ok" and out["copied"] == 3
    argv = seen["argv"]
    assert argv[argv.index("--multipart-compare") + 1] == "size"
    assert argv[argv.index("--dest-endpoint") + 1] == ""
    assert argv[argv.index("--source-endpoint") + 1] == "http://100.76.251.73:9000"
    assert argv[argv.index("--dest-bucket") + 1] == "dr-bucket"
    assert argv[argv.index("--prefix") + 1] == "ayoai-mind/transcripts/"


def test_a_failed_copy_is_a_failed_verdict(dr, monkeypatch):
    import cold_snapshot
    monkeypatch.setattr(cold_snapshot, "resolve_cold_target", lambda _e: {
        "mode": "pinned", "endpoint": cold_snapshot.AWS_REGIONAL,
        "bucket": "d", "live_bucket": "l", "live_endpoint": "http://x:9000",
        "backend": "own-cloud"})
    monkeypatch.setattr(dr, "_resolve_prefix", lambda: "p/")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps({"copied": 1, "failed": 2})))
    assert dr.run(False, 4, 60)["verdict"] == "failed"


# --- the tick's second leg ---------------------------------------------------

def test_the_transcripts_leg_never_raises(tick, monkeypatch):
    """It runs inside the one daily claim; an exception here would abort the
    snapshot's own marker write."""
    def _boom(*_a, **_k):
        raise RuntimeError("transport gone")
    monkeypatch.setattr(tick.subprocess, "run", _boom)
    out = tick._transcripts_leg()
    assert out["verdict"] == "error" and out["ok"] is False


@pytest.mark.parametrize("verdict,ok", [
    ("ok", True), ("skipped-local-backend", True), ("skipped-live-is-dr", True),
    ("failed", False), ("refused-colocated", False),
])
def test_leg_ok_classification(tick, monkeypatch, verdict, ok):
    monkeypatch.setattr(tick.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps({"verdict": verdict})))
    assert tick._transcripts_leg()["ok"] is ok


def test_a_failing_transcripts_leg_cannot_mark_the_snapshot_failed(tick, monkeypatch):
    """The bounded-coupling property, asserted rather than asserted-in-prose: a
    dead transcripts copy still lets the snapshot record status=ok, and the ONE
    Investigate names both legs so neither failure is swallowed by the other's
    dedup."""
    monkeypatch.setattr(tick, "_backend", lambda: None)
    filed = {}
    monkeypatch.setattr(tick, "_file_investigate",
                        lambda reason, detail: filed.update(reason=reason, detail=detail))

    def _fake_run(argv, **_kw):
        joined = " ".join(str(a) for a in argv)
        if "transcripts-dr-copy" in joined:
            return types.SimpleNamespace(returncode=2, stderr="dr boom",
                                         stdout=json.dumps({"verdict": "failed"}))
        return types.SimpleNamespace(returncode=0, stderr="",
                                     stdout=json.dumps({"verdict": "ok",
                                                        "archive_key": "k"}))
    monkeypatch.setattr(tick.subprocess, "run", _fake_run)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = tick.do_run(types.SimpleNamespace(prefix="cold-snapshots", run=True))
    monkeypatch.undo()
    assert rc == 0
    payload = json.loads(out.getvalue().strip().splitlines()[-1])
    assert payload["verdict"] == "ok"                     # snapshot unaffected
    assert payload["transcripts_verdict"] == "failed"
    assert "transcripts" in filed["reason"] and "snapshot" not in filed["reason"]
