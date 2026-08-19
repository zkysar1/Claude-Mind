""": the sweep's snapshot->file section is serialized by a RUN MUTEX.

THE DEFECT. load_converted_ids() is a pre-loop SNAPSHOT and the filing loop never
adds a newly-filed msg_id back into it, so two OVERLAPPING runs both snapshot
before either writes and both file the same trigger. Measured: g-115-5989 and
g-115-5990 carry the IDENTICAL origin_signal
insight_trigger:msg-20260811-230106-bravo-5014 and identical titles, filed 11
seconds apart, from a board post that exists exactly once.

WHY THESE TESTS OVERLAP FOR REAL RATHER THAN INSPECTING. g-115-6193's acceptance
criterion is explicit that verification must be adversarial -- "demonstrated by a
test that actually overlaps them rather than by inspection" -- because SEQUENTIAL
runs already dedup correctly, so any test that does not genuinely contend passes
against the unfixed code. test_overlapping_run_files_nothing therefore holds the
mutex from a SEPARATE OS PROCESS, which is the production failure mode; a
same-process holder would be served by _fileops' in-process per-path FIFO and
would exercise a different code path than the one that broke.

THE NON-VACUITY CONTROL IS THE LOAD-BEARING TEST. "filed nothing" is the expected
result of a broken sweep, an empty board, and a working mutex alike (guard-2421 --
positive-control a zero before believing it). test_control_without_lock_files_it
runs the same fixture with the mutex FREE and asserts the trigger IS filed, so the
skip in the overlap test is attributable to contention rather than to inertness.
Sabotage check: deleting the acquire_lock call from main() turns
test_overlapping_run_files_nothing red (it files 1) while every other test here
stays green -- verified, not assumed.

Run: py -3 -m pytest core/scripts/tests/test_insight_trigger_sweep_run_mutex.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

SWEEP_PATH = CORE_SCRIPTS / "insight-trigger-sweep.py"
_spec = importlib.util.spec_from_file_location("its_run_mutex", SWEEP_PATH)
its = importlib.util.module_from_spec(_spec)
sys.modules["its_run_mutex"] = its
_spec.loader.exec_module(its)


def _findings_line(msg_id, *, author, target, action, severity, timestamp):
    return json.dumps({
        "id": msg_id,
        "author": author,
        "channel": "findings",
        "type": "finding",
        "text": f"run-mutex trigger {msg_id}",
        "tags": [
            f"requires_action_by:{target}",
            f"action_type:{action}",
            f"severity:{severity}",
        ],
        "timestamp": timestamp,
    }) + "\n"


def _trigger_timestamp(hours_ago=2.0):
    """Past GRACE_HOURS (1.0) and inside WINDOW_HOURS (24.0)."""
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


@pytest.fixture
def sandbox(monkeypatch, tmp_path: Path):
    """Sandbox shape borrowed from test_insight_trigger_sweep_conservation.py,
    plus the two things this file needs: SWEEP_LOCK pointed into tmp (so a test
    can never contend with -- or break -- the REAL world's run mutex), and
    STORAGE_BACKEND pinned local.

    The STORAGE_BACKEND pin is guard-955, and it is not ceremony here: on an
    own-cloud box the lock resolves to the DISTRIBUTED lock table rather than to
    the tmp path, so without the pin these tests would contend against the live
    fleet's real mutex and could block a production sweep.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")

    world = tmp_path / "world"
    world.mkdir()
    findings = world / "board" / "findings.jsonl"
    findings.parent.mkdir(parents=True)
    asp_jsonl = world / "aspirations.jsonl"
    asp_jsonl.write_text("", encoding="utf-8")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    test_agent = agents_dir / "zeta-test"
    test_agent.mkdir()
    (test_agent / "local-paths.conf").write_text(
        f'WORLD_PATH="{world}"\nMETA_PATH="{tmp_path / "meta"}"\n', encoding="utf-8",
    )
    (test_agent / "aspirations.jsonl").write_text("", encoding="utf-8")

    lock_path = tmp_path / "insight-trigger-sweep.lock"

    monkeypatch.setattr(its, "WORLD_ASPS", asp_jsonl)
    monkeypatch.setattr(its, "BOARD_DIR", findings.parent)
    monkeypatch.setattr(its, "_agents_root", lambda: agents_dir)
    monkeypatch.setattr(its, "SWEEP_LOCK", lock_path)
    # Keep the contention wait short so a red test fails fast rather than
    # stalling the suite. The production value is 5s; the property under test is
    # "does it skip", which is timeout-independent.
    monkeypatch.setattr(its, "SWEEP_LOCK_TIMEOUT", 1)
    # Hermeticity (): never read the REAL registry/roster.
    monkeypatch.setattr(its, "ENV_REGISTRY_DIR", tmp_path / "no-environments")
    monkeypatch.setattr(its, "_self_env", lambda: "test-env")
    monkeypatch.setattr(its, "_local_roster", lambda: set())

    filed_calls = []

    def fake_file_goal(trigger, *, dry_run=False):
        filed_calls.append({"trigger": trigger, "dry_run": dry_run})
        return {"would_file": dry_run, "rc": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(its, "file_goal", fake_file_goal)
    monkeypatch.setattr(its, "_emit_audit_stale_note",
                        lambda t, s: {"posted": True, "msg_id": "fake"})

    findings.write_text(_findings_line(
        "msg-20260814-000000-bravo-9001",
        author="bravo", target="alpha", action="fix", severity="constrains",
        timestamp=_trigger_timestamp(),
    ), encoding="utf-8")

    return {"world": world, "findings": findings, "asp_jsonl": asp_jsonl,
            "lock_path": lock_path, "filed_calls": filed_calls}


def _run(argv):
    saved = sys.argv
    sys.argv = ["insight-trigger-sweep.py"] + argv
    try:
        return its.main()
    finally:
        sys.argv = saved


class _LockHolder:
    """A REAL second OS process holding the run mutex, via the same
    _fileops.acquire_lock the sweep uses.

    A separate process rather than a thread on purpose: _fileops.acquire_lock
    routes same-path callers in ONE process through an in-process FIFO
    (_write_queue), so a thread holder would be a queueing test, not the
    cross-process contention that produced g-115-5989/g-115-5990.
    """

    def __init__(self, lock_path: Path, tmp_path: Path):
        self.lock_path = lock_path
        self.ready = tmp_path / "holder-ready"
        self.release = tmp_path / "holder-release"
        self.proc = None

    def __enter__(self):
        src = textwrap.dedent(f"""
            import sys, time, pathlib
            sys.path.insert(0, {str(CORE_SCRIPTS)!r})
            from _fileops import acquire_lock, release_lock
            lock = pathlib.Path({str(self.lock_path)!r})
            ready = pathlib.Path({str(self.ready)!r})
            release = pathlib.Path({str(self.release)!r})
            acquire_lock(lock, timeout=10, stale_seconds=600)
            ready.write_text("held")
            while not release.exists():
                time.sleep(0.05)
            release_lock(lock)
        """)
        env = dict(os.environ, STORAGE_BACKEND="local")
        self.proc = subprocess.Popen([sys.executable, "-c", src], env=env)
        deadline = time.time() + 30
        while not self.ready.exists():
            if time.time() > deadline:
                self.proc.kill()
                raise AssertionError("lock holder process never acquired the mutex")
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"lock holder exited early rc={self.proc.returncode}")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.release.write_text("go")
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return False


def test_control_without_lock_files_it(sandbox, capsys):
    """NON-VACUITY CONTROL -- run this before believing any 'filed nothing'.

    Same fixture, same trigger, mutex FREE. The trigger IS filed. Without this,
    the overlap test's zero is indistinguishable from an empty board or a sweep
    broken in some unrelated way (guard-2421).
    """
    rc = _run(["--json"])
    summary = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert summary["mode"] != "skipped-another-run-holds-lock"
    assert summary["filed"] == 1
    assert len(sandbox["filed_calls"]) == 1


def test_overlapping_run_files_nothing(sandbox, tmp_path, capsys):
    """THE ACCEPTANCE TEST. A second run that genuinely overlaps a live one
    files ZERO goals -- so the pair can no longer produce two goals from one
    board post.
    """
    with _LockHolder(sandbox["lock_path"], tmp_path):
        rc = _run(["--json"])
        summary = json.loads(capsys.readouterr().out)

    assert rc == 0, "contention is not a failure -- a phantom rc would redden the tick"
    assert summary["mode"] == "skipped-another-run-holds-lock"
    assert summary["lock_skipped"] is True
    # The whole point: nothing was filed while the other run held the mutex.
    assert sandbox["filed_calls"] == []
    assert summary["filed"] == 0


def test_dry_run_is_not_blocked_by_the_mutex(sandbox, tmp_path, capsys):
    """The --dry-run carve-out, which is deliberate and load-bearing.

    A dry run writes nothing so it cannot duplicate, and g-115-6193's own
    regression guard calls for a --dry-run IMMEDIATELY AFTER a live run to
    confirm filed=0/skipped=N. If the dry run blocked on the live run's mutex it
    could report nothing at all, destroying the diagnostic. /prime Step 5.5b's
    `--dry-run --json` consumer depends on the same property.
    """
    with _LockHolder(sandbox["lock_path"], tmp_path):
        rc = _run(["--dry-run", "--json"])
        summary = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert summary["mode"] == "dry-run"
    assert summary.get("lock_skipped") is not True


def test_lock_is_released_after_a_normal_run(sandbox, capsys):
    """Release is in a finally, so a run must not leave the mutex held -- a
    leaked lock would suppress every subsequent sweep until the 600s stale-break,
    converting this fix into an outage.
    """
    rc = _run(["--json"])
    capsys.readouterr()

    assert rc == 0
    assert not sandbox["lock_path"].exists(), "run mutex leaked after a normal run"


def test_lock_released_even_when_filing_raises(sandbox, monkeypatch, capsys):
    """The finally must survive an exception mid-filing. Without it, one crash
    inside the critical section wedges the sweep for the full stale window.
    """
    def boom(trigger, *, dry_run=False):
        raise RuntimeError("filing exploded")

    monkeypatch.setattr(its, "file_goal", boom)

    with pytest.raises(RuntimeError):
        _run(["--json"])
    capsys.readouterr()

    assert not sandbox["lock_path"].exists(), "run mutex leaked after a mid-filing raise"


def test_sequential_runs_still_dedup(sandbox, capsys):
    """ outcome 3 (regression guard): the mutex must not disturb the
    dedup that already worked. A --dry-run immediately after a live run reports
    filed=0 and recognises the live run's own filing as already-converted.

    file_goal is faked, so the fixture writes the origin_signal the real filing
    would have produced -- the dedup path under test is load_converted_ids
    reading the queue, and that is exercised faithfully either way.
    """
    _run(["--json"])
    live = json.loads(capsys.readouterr().out)
    assert live["filed"] == 1
    msg_id = live["filed_details"][0]["trigger"]["msg_id"]

    sandbox["asp_jsonl"].write_text(json.dumps({
        "id": "asp-115", "status": "active",
        "goals": [{"id": "g-115-9999", "status": "pending",
                   "origin_signal": f"insight_trigger:{msg_id}"}],
    }) + "\n", encoding="utf-8")

    _run(["--dry-run", "--json"])
    after = json.loads(capsys.readouterr().out)

    assert after["filed"] == 0
    assert after["skipped_already_converted"] == 1
