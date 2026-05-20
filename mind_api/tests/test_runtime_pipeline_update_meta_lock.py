""" root-cause regression: pipeline_write._update_meta lock invariant.

The historical symptom was: /v1/pipeline/read --counts intermittently
returned {discovered:0, active:0, measurement-pending:0, resolved:0,
archived:0} despite pipeline.jsonl having real records. Self-recovered
within minutes. Observed in bravo iter-9 2026-05-14T00:13, alpha
session-71 iter-3 14:32. Was masked by a defensive re-derive in
pipeline-read.sh (lines 60-150).

The root cause:
  1. _atomic_write_with_fallback's fallback path (when OneDrive blocks
     os.replace) does open(path, "w") which TRUNCATES the file to 0
     bytes the moment it opens, BEFORE write_to_handle runs.
  2. _update_meta read pipeline.jsonl WITHOUT holding the live_path
     lock. A reader landing in the truncate window saw [] and
     _compute_meta rolled it up as all-zero stage_counts, written
     persistently to pipeline-meta.json.
  3. The next move/update/archive-sweep that called _update_meta saw
     the un-corrupted file and overwrote the all-zeros — hence the
     "self-recovers within minutes" symptom.

The fix (this regression guards it):
  _update_meta now acquires live_path.lock for the read and
  meta_path.lock for the write. A concurrent writer holding live_path's
  lock (which is what every pipeline writer does, including the
  atomic-write fallback that needs caller-held lock per _fileops.py:333)
  blocks the _update_meta read until it's done — eliminating the empty-
  file window.

This test reproduces the race deterministically without actually
exercising the OneDrive fallback path:
  - Thread A: holds live_path.lock, simulates the truncate window
    (the file is empty on disk during the hold), then writes content.
  - Thread B: calls _update_meta concurrently and verifies it does
    NOT observe an empty file → meta NOT all-zero.

If _update_meta drops the lock acquisition, Thread B reads during
Thread A's empty window → stage_counts all-zero → test fails.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def pipeline_files(tmp_path: Path):
    """Create a world dir with pipeline.jsonl containing 3 real records."""
    world = tmp_path / "world"
    world.mkdir()
    live = world / "pipeline.jsonl"
    archive = world / "pipeline-archive.jsonl"
    meta = world / "pipeline-meta.json"

    # Three real records spanning stages so all-zero is distinguishable
    # from any-correct-state.
    records = [
        {"id": "2026-05-18_test-a", "title": "Hypothesis A about X",
         "stage": "discovered", "horizon": "session", "type": "calibration",
         "confidence": 0.7, "position": 0, "formed_date": "2026-05-18",
         "category": "framework"},
        {"id": "2026-05-18_test-b", "title": "Hypothesis B about Y",
         "stage": "active", "horizon": "session", "type": "calibration",
         "confidence": 0.7, "position": 1, "formed_date": "2026-05-18",
         "category": "framework"},
        {"id": "2026-05-18_test-c", "title": "Hypothesis C about Z",
         "stage": "resolved", "horizon": "session", "type": "calibration",
         "confidence": 0.7, "position": 2, "formed_date": "2026-05-18",
         "category": "framework", "outcome": "CONFIRMED",
         "outcome_date": "2026-05-18"},
    ]
    with live.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    # Empty archive file.
    archive.write_text("", encoding="utf-8")
    return live, archive, meta, world


def test_update_meta_blocks_on_concurrent_live_lock(pipeline_files):
    """_update_meta MUST acquire live_path.lock for the read.

    If it doesn't, a concurrent writer holding live_path.lock and
    mid-truncate-rewrite (the atomic-write fallback shape) lets
    _update_meta read an empty file → all-zero stage_counts → persisted
    to disk. This test catches a regression that removes the lock.
    """
    from mind_api.src import file_locks
    from mind_api.src.world import pipeline_write

    live, archive, meta, world = pipeline_files

    # The "truncate window" simulator: hold live_path.lock for a beat
    # while the file is empty on disk, then restore the content. Mirrors
    # exactly what the _atomic_write_with_fallback fallback path looks
    # like to a reader between open("w") and the first .write() call —
    # only here we extend the window deterministically.
    saved_content = live.read_text(encoding="utf-8")

    barrier = threading.Barrier(2)
    truncate_window_open = threading.Event()
    writer_done = threading.Event()

    def truncating_writer():
        # Same lock _update_meta acquires.
        with file_locks.locked(live):
            # Truncate to simulate open("w") having just fired.
            live.write_text("", encoding="utf-8")
            truncate_window_open.set()
            barrier.wait()  # release reader to attempt its read NOW
            # Hold the lock for a measurable window so a buggy reader
            # (no-lock _update_meta) would read [] here.
            time.sleep(0.20)
            # Restore content (simulates write_to_handle completing).
            live.write_text(saved_content, encoding="utf-8")
        writer_done.set()

    reader_result = {}

    def reader():
        truncate_window_open.wait()  # wait until writer has truncated
        barrier.wait()  # synchronize: writer is mid-window
        # Now call _update_meta. If it holds the lock for the read,
        # this BLOCKS until writer releases — and reads the restored
        # content (3 records). If it does NOT hold the lock, it reads
        # the empty file and writes all-zero stage_counts.
        try:
            pipeline_write._update_meta(live, archive, meta)
            reader_result["ok"] = True
        except Exception as e:  # pragma: no cover — surface real errors
            reader_result["error"] = repr(e)

    t_writer = threading.Thread(target=truncating_writer, daemon=True)
    t_reader = threading.Thread(target=reader, daemon=True)
    t_writer.start()
    t_reader.start()
    t_writer.join(timeout=5)
    t_reader.join(timeout=5)

    assert "error" not in reader_result, (
        f"_update_meta raised unexpectedly: {reader_result.get('error')}")
    assert reader_result.get("ok") is True
    assert writer_done.is_set(), "writer never finished — test infra bug"

    # The crux: meta MUST reflect the post-restore content, not the
    # empty-window content. If the fix is reverted, reader sees [] and
    # writes all-zero counts → this assertion fails.
    assert meta.exists(), "meta file not written"
    meta_data = json.loads(meta.read_text(encoding="utf-8"))
    stage_counts = meta_data.get("stage_counts", {})
    assert stage_counts.get("discovered") == 1, (
        f"discovered stage_count {stage_counts.get('discovered')} != 1 — "
        f"_update_meta likely read during the truncate window. Full meta: "
        f"{stage_counts}")
    assert stage_counts.get("active") == 1, (
        f"active stage_count {stage_counts.get('active')} != 1 — see full: "
        f"{stage_counts}")
    assert stage_counts.get("resolved") == 1, (
        f"resolved stage_count {stage_counts.get('resolved')} != 1 — see "
        f"full: {stage_counts}")
