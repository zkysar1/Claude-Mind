"""trigger-firings own-cloud spool lane ().

The lane is a PORT of the gate-firings spool (g-115-2405). These tests pin the
three things a port can silently get wrong:

  1. the WRITER takes the spool only under own-cloud, and never touches the
     shared store there (that touch is the whole 810 MB/24h cost);
  2. the READER spans the spool, so `report` totals do not silently drop by
     everything not yet flushed — the consumer side of guard-4348;
  3. the writer, the flusher and owncloud_sync agree on ONE spool basename.
     A rename on one side only strands records in a file nobody reads, which is
     exactly what the hyphenated/dotted mismatch did to the gate-firings lane.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import trigger_firings as tf  # noqa: E402


@pytest.fixture
def meta(tmp_path, monkeypatch):
    """Point the module's three path globals at a tmp meta dir."""
    monkeypatch.setattr(tf, "FIRINGS_PATH", tmp_path / "trigger-firings.jsonl")
    monkeypatch.setattr(tf, "SPOOL_PATH", tmp_path / tf.SPOOL_NAME)
    monkeypatch.setattr(tf, "FLUSHING_PATH", tmp_path / tf.FLUSHING_NAME)
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setenv("MIND_SID", "deadbeef-test")
    return tmp_path


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def test_owncloud_writes_the_spool_and_never_the_store(meta, monkeypatch):
    """The store must not be touched on the spool lane. Touching it is the
    whole defect: a whole-object S3 RMW *plus* a .history snapshot per firing,
    since trigger-firings.jsonl is NOT in _fileops._SNAPSHOT_BLACKLIST."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    for i in range(5):
        tf.record_firing("T1", context={"i": i})
    assert tf.SPOOL_PATH.exists()
    assert len(tf.SPOOL_PATH.read_text(encoding="utf-8").splitlines()) == 5
    assert not tf.FIRINGS_PATH.exists(), "spool lane must not write the store"


def test_local_backend_keeps_the_direct_append(meta, monkeypatch):
    """RECALL CONTROL. Non-own-cloud backends keep the direct locked append —
    a cheap raw local append there, and the shape existing tests assert on."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    tf.record_firing("T1")
    assert tf.FIRINGS_PATH.exists(), "local backend must still write the store"
    assert not tf.SPOOL_PATH.exists(), "local backend must not spool"


# ---------------------------------------------------------------------------
# Reader — the half a port forgets
# ---------------------------------------------------------------------------

def test_reader_spans_the_spool(meta, monkeypatch):
    """Without this the report silently under-reports by everything unflushed."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    for i in range(6):
        tf.record_firing("T1", context={"i": i})
    assert len(tf._load_firings()) == 6


def test_reader_dedups_a_mid_flush_overlap(meta, monkeypatch):
    """A flush that died between its store-append and its unlink leaves the
    same record in BOTH files. Counting it twice would make the totals move
    for a reason that is not a firing."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    tf.record_firing("T1")
    line = tf.SPOOL_PATH.read_text(encoding="utf-8").splitlines()[0]
    tf.FIRINGS_PATH.write_text(line + "\n", encoding="utf-8")
    tf.FLUSHING_PATH.write_text(line + "\n", encoding="utf-8")
    assert len(tf._load_firings()) == 1


def test_reader_tolerates_a_torn_tail(meta, monkeypatch):
    """The spool is lockless O_APPEND, so its last line can be a partial write.
    One torn tail must not take the whole report down."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    tf.record_firing("T1")
    with open(tf.SPOOL_PATH, "a", encoding="utf-8") as f:
        f.write('{"trigger_id": "T9", "ts": "2026')
    assert len(tf._load_firings()) == 1


def test_reader_rejects_a_valid_json_scalar(meta, monkeypatch):
    """A torn append can land on a bare `7`, which parses fine and then kills
    every consumer doing row.get(). That exact line took both gate-telemetry
    tools down for 11 days."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    tf.record_firing("T1")
    with open(tf.SPOOL_PATH, "a", encoding="utf-8") as f:
        f.write("7\n")
    rows = tf._load_firings()
    assert len(rows) == 1
    assert all(isinstance(r, dict) for r in rows)


# ---------------------------------------------------------------------------
# Flusher
# ---------------------------------------------------------------------------

def _flush(meta_dir, *extra):
    env = dict(os.environ)
    env.update(MIND_META=str(meta_dir), STORAGE_BACKEND="local",
               MIND_AGENT="alpha", MIND_SID="deadbeef-test")
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "trigger-firings-flush.py"),
         "--meta-dir", str(meta_dir), *extra],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )


def _spool_rows(meta_dir, n, start=0):
    lines = [json.dumps({"trigger_id": "T1", "ts": f"2026-09-06T19:00:{i:02d}",
                         "agent": "alpha", "sid": "dead"}, ensure_ascii=True)
             for i in range(start, start + n)]
    (meta_dir / tf.SPOOL_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_flush_drains_spool_into_store(tmp_path):
    _spool_rows(tmp_path, 5)
    r = _flush(tmp_path, "--force")
    assert r.returncode == 0, r.stderr
    store = tmp_path / "trigger-firings.jsonl"
    assert len(store.read_text(encoding="utf-8").splitlines()) == 5
    assert not (tmp_path / tf.SPOOL_NAME).exists()
    assert (tmp_path / "trigger-firings.spool.last-flush").exists()


def test_reflushing_crash_residue_is_duplicate_safe(tmp_path):
    """Step 3 of the protocol: a flusher that died between its landed store
    append and its unlink must not double-append on the next tick."""
    _spool_rows(tmp_path, 5)
    assert _flush(tmp_path, "--force").returncode == 0
    store = tmp_path / "trigger-firings.jsonl"
    (tmp_path / tf.FLUSHING_NAME).write_text(
        store.read_text(encoding="utf-8"), encoding="utf-8")
    assert _flush(tmp_path, "--force").returncode == 0
    assert len(store.read_text(encoding="utf-8").splitlines()) == 5


def test_interval_gate_holds_a_small_spool(tmp_path):
    """The gate is what bounds S3 churn to ~1 RMW per interval per box; without
    it the flush would re-PUT the whole object on every tick."""
    _spool_rows(tmp_path, 5)
    assert _flush(tmp_path, "--force").returncode == 0
    _spool_rows(tmp_path, 1, start=90)
    r = _flush(tmp_path)  # no --force, stamp is fresh
    assert r.returncode == 0
    assert (tmp_path / tf.SPOOL_NAME).exists(), "gate must retain the spool"
    store = tmp_path / "trigger-firings.jsonl"
    assert len(store.read_text(encoding="utf-8").splitlines()) == 5


def test_burst_overrides_the_interval_gate(tmp_path):
    """A spool at/over --burst-records drains even inside the interval, so a
    hot box cannot accumulate unboundedly between ticks."""
    _spool_rows(tmp_path, 5)
    assert _flush(tmp_path, "--force").returncode == 0
    _spool_rows(tmp_path, 12, start=100)
    r = _flush(tmp_path, "--burst-records", "10")
    assert r.returncode == 0, r.stderr
    store = tmp_path / "trigger-firings.jsonl"
    assert len(store.read_text(encoding="utf-8").splitlines()) == 17


def test_empty_spool_is_a_silent_noop(tmp_path):
    r = _flush(tmp_path, "--force")
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    assert not (tmp_path / "trigger-firings.jsonl").exists()


# ---------------------------------------------------------------------------
# One basename, three files (the strand-records failure)
# ---------------------------------------------------------------------------

def test_writer_and_flusher_agree_on_the_spool_basename():
    flusher = (SCRIPT_DIR / "trigger-firings-flush.py").read_text(encoding="utf-8")
    assert "from trigger_firings import" in flusher, (
        "the flusher must IMPORT the basenames, not restate them — a second "
        "copy is what stranded records in the gate-firings lane")
    assert tf.SPOOL_NAME == "trigger-firings.spool.jsonl"
    assert tf.FLUSHING_NAME == "trigger-firings.spool.flushing.jsonl"


def test_spool_artifacts_are_excluded_from_owncloud_sync():
    """Machine-local by construction. Syncing a per-box spool clobbers peers'
    spools — the franken-copy class the spool exists to avoid. Enumerated so
    that forgetting one is a red test rather than a silent fleet-wide sync
    (the pattern test_utilization_spool.py uses)."""
    import owncloud_sync
    for name in (tf.SPOOL_NAME, tf.FLUSHING_NAME,
                 "trigger-firings.spool.last-flush"):
        assert name in owncloud_sync._EXCLUDE_NAMES, f"{name} must never sync"


def test_flush_is_reachable_from_both_orchestrators():
    """guard-3448 — a gate is only as broad as its ENTRY POINTS. The reducer's
    productivity-check is a phase the worker loop skips by design, so a flush
    wired only there is dead on every worker box. That exact miss left gate
    firings stranded on workers for 17h (g-306-432); assert reachability, not
    mere presence."""
    close = (SCRIPT_DIR / "iteration-close.sh").read_text(encoding="utf-8")
    worker = (PROJECT_ROOT / ".claude" / "skills" / "worker-loop"
              / "SKILL.md").read_text(encoding="utf-8")
    assert "trigger-firings-flush" in close, "reducer tick missing the flush"
    assert "trigger-firings-flush" in worker, "worker preamble missing the flush"
