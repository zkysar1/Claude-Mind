"""test_stale_read_gate.py -- regression for the never-read fail-open change
(g-115-1569).

Bug shape: gates.stale_read.evaluate() Block 1 ("agent never read parent")
BLOCKED when goal-reads.jsonl had no receipt for the cited parent_goal. But in
the daemon-only architecture the read path (mind_api/src/endpoints/
aspirations.py + aspirations_query.py) does NOT write goal-reads.jsonl -- a
documented Phase-A/B scope-cut. So an absent receipt is the normal state, not
evidence of an unread parent: the branch fired on a dead signal that callers
overrode 100% of the time (all 29 ledger overrides were this never-read branch;
zero were genuine Block-2 stale-reads).

Fix: the never-read branch now FAILS OPEN (would_block=False, _fail_open=True)
instead of blocking. The genuine check (Block 2: a tracked receipt EXISTS AND
the parent changed after it) is unchanged and still blocks.

Pattern: direct evaluate() unit tests against tmp world/agent dirs (the logic
is file-read-dependent, so a subprocess test against real state would be
non-deterministic). _gate_log is monkeypatched to a no-op so telemetry is not
written to the real meta/gate-firings.jsonl during the run.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates import stale_read  # noqa: E402

# Neutralize telemetry: evaluate() calls _gate_log (-> real META_DIR/
# gate-firings.jsonl). _telemetry() resolves _gate_log from this module's
# global namespace at call time, so reassigning the module reference to a no-op
# keeps unit tests from polluting the real firing log.
stale_read._gate_log = lambda *a, **k: None  # noqa: E305

PARENT_LM = "2026-06-19T18:00:00"
OLDER = "2026-06-19T17:00:00"   # read BEFORE parent_lm -> stale
NEWER = "2026-06-19T19:00:00"   # read AFTER parent_lm  -> fresh
AGENT = "delta"
PARENT_ID = "g-100-01"


def _setup(tmp: Path, *, parent_last_modified=PARENT_LM, reads=None):
    """Build a tmp world (with PARENT_ID goal) + agent dir. When `reads` is a
    list of {goal_id,agent,read_at} dicts, writes goal-reads.jsonl; when None,
    no receipt file is created (the never-read case)."""
    world = tmp / "world"
    world.mkdir()
    agent = tmp / AGENT
    (agent / "session").mkdir(parents=True)
    goal = {"id": PARENT_ID, "title": "Parent goal"}
    if parent_last_modified is not None:
        goal["last_modified"] = parent_last_modified
    asp = {"id": "asp-100", "title": "fixture", "goals": [goal]}
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (agent / "aspirations.jsonl").write_text("", encoding="utf-8")
    if reads is not None:
        lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reads)
        (agent / "session" / "goal-reads.jsonl").write_text(lines, encoding="utf-8")
    return world, agent


def _eval(world, agent, *, override=None):
    return stale_read.evaluate(
        {"parent_goal": PARENT_ID, "agent": AGENT},
        override=override, agent_name=AGENT, world_dir=world, agent_dir=agent)


def test_never_read_fails_open():
    """No goal-reads.jsonl at all -> fail-open (was block). 9."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent = _setup(Path(tmpd), reads=None)
        out = _eval(world, agent)
        assert out["would_block"] is False, out
        assert out["_fail_open"] is True, out
        assert "receipt" in out["reason"].lower(), out["reason"]
        assert out["agent_last_read"] is None


def test_receipt_file_present_but_no_matching_entry_fails_open():
    """goal-reads.jsonl exists but no entry for this (goal,agent) -> fail-open."""
    with tempfile.TemporaryDirectory() as tmpd:
        # Receipts for a DIFFERENT goal and a DIFFERENT agent -> last_read None.
        world, agent = _setup(Path(tmpd), reads=[
            {"goal_id": "g-999-99", "agent": AGENT, "read_at": NEWER},
            {"goal_id": PARENT_ID, "agent": "alpha", "read_at": NEWER},
        ])
        out = _eval(world, agent)
        assert out["would_block"] is False, out
        assert out["_fail_open"] is True, out


def test_stale_read_still_blocks():
    """Tracked receipt OLDER than parent_lm -> Block 2 still blocks."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent = _setup(Path(tmpd), reads=[
            {"goal_id": PARENT_ID, "agent": AGENT, "read_at": OLDER}])
        out = _eval(world, agent)
        assert out["would_block"] is True, out
        assert out["_fail_open"] is False, out
        assert out["agent_last_read"] == OLDER
        assert "stale read" in out["reason"].lower()


def test_stale_read_override_passes_and_logs_ledger():
    """Block 2 + override -> pass + audit-ledger record written."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent = _setup(Path(tmpd), reads=[
            {"goal_id": PARENT_ID, "agent": AGENT, "read_at": OLDER}])
        out = _eval(world, agent, override="modification irrelevant to child")
        assert out["would_block"] is False, out
        assert out["override_applied"] == "modification irrelevant to child"
        ledger = world / "stale-read-overrides.jsonl"
        assert ledger.is_file(), "override must write the audit ledger"
        recs = [json.loads(x) for x in
                ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(recs) == 1 and recs[0]["parent_goal"] == PARENT_ID, recs


def test_fresh_read_passes():
    """Tracked receipt NEWER than parent_lm -> pass (fresh read)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent = _setup(Path(tmpd), reads=[
            {"goal_id": PARENT_ID, "agent": AGENT, "read_at": NEWER}])
        out = _eval(world, agent)
        assert out["would_block"] is False, out
        assert out["_fail_open"] is False, out
        assert out["reason"] == "fresh read"


def test_no_parent_goal_skips():
    """No parent_goal field -> gate not applicable (pass)."""
    out = stale_read.evaluate({"agent": AGENT}, agent_name=AGENT)
    assert out["would_block"] is False
    assert out["parent_goal"] is None


def test_parent_not_found_fail_open():
    """parent_goal cites a goal absent from live queues -> fail-open."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent = _setup(Path(tmpd), reads=None)
        out = stale_read.evaluate(
            {"parent_goal": "g-404-04", "agent": AGENT},
            agent_name=AGENT, world_dir=world, agent_dir=agent)
        assert out["would_block"] is False, out
        assert out["_fail_open"] is True, out


def test_legacy_parent_without_last_modified_passes():
    """Parent has no last_modified (legacy goal) -> pass, never block."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent = _setup(Path(tmpd), parent_last_modified=None, reads=None)
        out = _eval(world, agent)
        assert out["would_block"] is False, out
        assert out["parent_last_modified"] is None


if __name__ == "__main__":
    test_never_read_fails_open()
    test_receipt_file_present_but_no_matching_entry_fails_open()
    test_stale_read_still_blocks()
    test_stale_read_override_passes_and_logs_ledger()
    test_fresh_read_passes()
    test_no_parent_goal_skips()
    test_parent_not_found_fail_open()
    test_legacy_parent_without_last_modified_passes()
    print("ok")
