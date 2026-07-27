"""test_owncloud_sweep_stats_log.py — sweep-outcome telemetry sink ().

The union-lane counters (pushed_merged / diverged_merged / nobaseline_merged)
were in-memory per-run and stdout-transient: during the
2026-07-16_cc02-gate-firings-self-heal resolution, WHICH lane healed the store
could not be attributed post-hoc (grep of daemon.log + core/logs found zero
lane-counter records) and had to be reconstructed from forensic snapshots.
g-115-2468 adds:

  1. per-file ``merge_events`` entries in the stats dict at the
     ``_try_merge_put`` chokepoint (covers all three union lanes), and
  2. ``_log_sweep_stats`` — one JSON line per NON-BORING run appended to the
     machine-local ``core/logs/owncloud-sweep-stats.jsonl`` (core/ is not a
     sync root and core/logs/ is gitignored, so the sink can never recurse
     through the sync layer it observes).

Cases:
  A  interesting stats -> one valid JSON line with ts/source/counters
  B  boring stats (pure in-sync heartbeat) -> NO write
  C  sync_file extra_boring: plain-push-only stats suppressed
  D  fail-open: unwritable sink path -> no raise
  E  _try_merge_put appends a merge_events entry with the firing lane
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import owncloud_sync  # noqa: E402


def _read_lines(p: Path):
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


@pytest.fixture(autouse=True)
def _redirect_merge_events_log(tmp_path, monkeypatch):
    """: _try_merge_put now appends a durable merge-event line via
    _persist_merge_event on every successful union merge. Redirect that write
    into tmp so no test in this module touches the real
    mind_api/state/owncloud-merge-events.jsonl."""
    monkeypatch.setattr(
        owncloud_sync, "_merge_events_path",
        lambda: tmp_path / "owncloud-merge-events.jsonl")


def test_interesting_stats_written(tmp_path, monkeypatch):
    sink = tmp_path / "logs" / "owncloud-sweep-stats.jsonl"
    monkeypatch.setattr(owncloud_sync, "_SWEEP_STATS_LOG", sink)
    stats = {"scanned": 40, "in_sync": 38, "pushed": 1, "errors": 0,
             "pushed_merged": 1,
             "merge_events": [{"file": "/x/meta/gate-firings.jsonl",
                               "lane": "pushed_merged"}]}
    owncloud_sync._log_sweep_stats(stats, source="sweep")
    lines = _read_lines(sink)
    assert len(lines) == 1, "interesting sweep must write exactly one line"
    rec = lines[0]
    assert rec["source"] == "sweep"
    assert rec["pushed_merged"] == 1
    assert rec["merge_events"][0]["lane"] == "pushed_merged"
    assert "ts" in rec and rec["ts"][:2] == "20"


def test_boring_stats_suppressed(tmp_path, monkeypatch):
    sink = tmp_path / "logs" / "owncloud-sweep-stats.jsonl"
    monkeypatch.setattr(owncloud_sync, "_SWEEP_STATS_LOG", sink)
    stats = {"scanned": 800, "in_sync": 780, "skipped_unchanged": 20,
             "pruned_agents": 4, "pushed": 0, "errors": 0, "push_paths": []}
    owncloud_sync._log_sweep_stats(stats, source="sweep")
    assert not sink.exists(), "pure in-sync heartbeat must not be logged"


def test_sync_file_plain_push_suppressed(tmp_path, monkeypatch):
    sink = tmp_path / "logs" / "owncloud-sweep-stats.jsonl"
    monkeypatch.setattr(owncloud_sync, "_SWEEP_STATS_LOG", sink)
    stats = {"scanned": 1, "in_sync": 0, "pushed": 1, "errors": 0,
             "push_paths": ["agents/zeta/journal.jsonl"]}
    owncloud_sync._log_sweep_stats(
        stats, source="sync_file",
        extra_boring=frozenset({"pushed", "would_push"}))
    assert not sink.exists(), "plain PostToolUse push must not flood the sink"
    # ...but a merge on the single-file path still logs:
    stats["diverged_merged"] = 1
    stats["merge_events"] = [{"file": "x", "lane": "diverged_merged"}]
    owncloud_sync._log_sweep_stats(
        stats, source="sync_file",
        extra_boring=frozenset({"pushed", "would_push"}))
    assert len(_read_lines(sink)) == 1


def test_fail_open_on_unwritable_sink(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not dir", encoding="utf-8")
    sink = blocker / "logs" / "owncloud-sweep-stats.jsonl"  # parent is a FILE
    monkeypatch.setattr(owncloud_sync, "_SWEEP_STATS_LOG", sink)
    stats = {"scanned": 1, "errors": 1}
    owncloud_sync._log_sweep_stats(stats, source="sweep")  # must not raise
    err = capsys.readouterr().err
    assert "telemetry write failed" in err


def test_self_trim_keeps_newest_tail(tmp_path, monkeypatch):
    sink = tmp_path / "owncloud-sweep-stats.jsonl"
    monkeypatch.setattr(owncloud_sync, "_SWEEP_STATS_LOG", sink)
    monkeypatch.setattr(owncloud_sync, "_SWEEP_LOG_MAX_BYTES", 600)
    monkeypatch.setattr(owncloud_sync, "_SWEEP_LOG_KEEP_BYTES", 300)
    for i in range(20):
        owncloud_sync._log_sweep_stats(
            {"scanned": 1, "errors": 1, "seq": i}, source="sweep")
    assert sink.stat().st_size <= 600 + 200, "trim must bound the sink"
    lines = _read_lines(sink)
    assert lines, "sink must retain a tail after trim"
    for rec in lines:  # every retained line is COMPLETE JSON (whole-line trim)
        assert rec["source"] == "sweep"
    assert lines[-1]["seq"] == 19, "newest record must survive the trim"
    seqs = [r["seq"] for r in lines]
    assert seqs == sorted(seqs) and seqs[0] > 0, "oldest records were dropped"


class _StubBackend:
    def __init__(self):
        self.calls = []

    def merge_put(self, full, local_bytes):
        self.calls.append(str(full))
        return "merged-ok"


def test_try_merge_put_records_merge_event(tmp_path):
    # gate-firings.jsonl is merge-registered by basename in coordination_merge
    target = tmp_path / "gate-firings.jsonl"
    target.write_text('{"a": 1}\n', encoding="utf-8")
    stats = {"errors": 0}
    be = _StubBackend()
    res = owncloud_sync._try_merge_put(
        be, target, target.read_bytes(), stats, counter="nobaseline_merged")
    assert res is not owncloud_sync._MERGE_NA and res is not None
    assert stats["nobaseline_merged"] == 1
    assert stats["merge_events"] == [
        {"file": str(target), "lane": "nobaseline_merged"}]


def test_try_merge_put_persists_durable_event(tmp_path):
    """: a merge SUCCESS also appends a durable {ts,file,lane,md5}
    JSON line to _merge_events_path() (the mind_api/state tier), so a
    pushed/diverged/nobaseline merge stays observable post-hoc after the
    in-memory stats['merge_events'] list dies with the sweep. The autouse
    fixture points _merge_events_path at tmp."""
    import hashlib
    target = tmp_path / "gate-firings.jsonl"      # merge-registered by basename
    target.write_text('{"a": 1}\n', encoding="utf-8")
    stats = {"errors": 0}
    owncloud_sync._try_merge_put(
        _StubBackend(), target, target.read_bytes(), stats,
        counter="diverged_merged")
    durable = _read_lines(owncloud_sync._merge_events_path())
    assert len(durable) == 1, "one durable line per merge success"
    rec = durable[0]
    assert rec["file"] == str(target)
    assert rec["lane"] == "diverged_merged"
    assert rec["ts"][:2] == "20"                  # ISO timestamp present
    assert rec["md5"] == hashlib.md5(target.read_bytes()).hexdigest()


def test_persist_merge_event_fail_open(tmp_path, monkeypatch, capsys):
    """: a durable-log write failure must NEVER break the merge/push
    path — mirrors _log_sweep_stats fail-open (test_fail_open_on_unwritable_sink).
    Parent is a FILE, so mkdir raises inside the helper and is swallowed."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not dir", encoding="utf-8")
    monkeypatch.setattr(
        owncloud_sync, "_merge_events_path",
        lambda: blocker / "sub" / "owncloud-merge-events.jsonl")
    owncloud_sync._persist_merge_event(           # must not raise
        {"ts": "2026-07-23T00:00:00", "file": "x", "lane": "pushed_merged",
         "md5": None})
    assert "merge-events persist failed" in capsys.readouterr().err


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
